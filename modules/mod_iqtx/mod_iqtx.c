/*
 * mod_iqtx: MicroPython C module exposing the ESP32-S3 RF SRAM
 * arbitrary-IQ playback path (esp32-iq-tx) to the vtOS shell as an
 * app-level lab mode.
 *
 * Ported from whstudio123/esp32-iq-tx (firmware/src/iq_tx.c and
 * firmware/src/targets/esp32s3.c), which extracts the verified S3
 * iq_sdr_prepare_tx path and derives from lozaning/ESP32SDR and
 * ESPARGOS/esp-sdr. Provenance and register documentation live in that
 * repository's docs/REGISTERS.md and docs/PROVENANCE.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * (this file only -- the rest of vtOS remains MIT)
 *
 * While active, playback owns the RF exclusively: WiFi/BLE must be down
 * before init (the Python app drops them), and the vendor CW preparation
 * briefly produces a carrier before the NCO clears. The 64 KB playback
 * bank at 0x3fcd0000 is reserved out of the IDF heap at link time, so it
 * can never overlap the MicroPython GC heap (which is allocated through
 * heap_caps after reservations).
 *
 * Known port caveat (TODO 2026-11-08): TX power is not stable across
 * stop/start cycles on the 5.5.1 PHY blob -- the S3 backend lacks the
 * TX power-control-field capture/restore the S31 backend has. If a
 * session needs guaranteed full power, deinit+re-init (iqtx.deinit()
 * then iqtx.init()).
 */

#include "py/runtime.h"
#include "py/mphal.h"

#include "esp_phy_cert_test.h"
#include "esp_phy_init.h"
#include "esp_private/periph_ctrl.h"
#include "esp_rom_sys.h"
#include "esp_wifi.h"
#include "heap_memory_layout.h"
#include "soc/soc.h"

#include <string.h>
#include <stdint.h>

// ---- S3 register map (esp32-iq-tx, validated on IDF 5.5.1 2026-11-08) --
#define IQTX_SRAM_BASE 0x3fcd0000u
#define IQTX_SRAM_END  0x3fce0000u
#define IQTX_OWNER     0x600c101cu   // SRAM ownership: idle 0, playback 4
#define IQTX_ADC       0x60033d5cu   // ADC capture disabled during TX
#define IQTX_PLAY      0x60033d64u   // count/loop/enable (vendor dactrig)
#define IQTX_NCO1      0x60006040u   // NCO enables: bit 18, cleared
#define IQTX_NCO2      0x60006044u
#define IQTX_NCO3      0x6000604cu
// BISECT-STEP-2: link-time reservation disabled -- boot regression test.
// The RF SRAM bank must NOT be handed to RF while heap_caps or static
// data owns it; see TODO before re-enabling playback on this build.
// SOC_RESERVE_MEMORY_REGION(IQTX_SRAM_BASE, IQTX_SRAM_END, iqtx_rf_sram);

#define IQTX_MAX_SAMPLES 16128
#define IQTX_LOOP_ALIGN  256
#define IQTX_MAX_CHANNELS 13
#define IQTX_MAX_BACKOFF  120

// 1 when the playback bank is reserved out of the heap at link time
// (SOC_RESERVE_MEMORY_REGION active). 0 means the bank lives inside the
// MicroPython heap: init/caps/status are safe, but load+start would let
// RF overwrite live Python objects -- the app must refuse playback.
#define IQTX_BANK_PROTECTED 0

// Vendor/ROM entry points (librftest.a, libphy.a, S3 ROM).
extern void rom_pbus_debugmode(void), rom_pbus_workmode(void);
extern void rom_pbus_xpd_tx_on(unsigned, unsigned), rom_pbus_xpd_tx_off(void);
extern void rom_pbus_xpd_rx_off(void);
extern void rom_set_txclk_en(unsigned), rom_set_rxclk_en(unsigned), rom_dac_rate_set(unsigned);
extern void rom_loopback_mode_en(unsigned), rom_set_loopback_gain(unsigned, unsigned, unsigned);
extern void force_txon_mode(unsigned, unsigned, unsigned), stop_tx_tone(unsigned);
extern void set_chanfreq(unsigned, unsigned);

static struct {
    uint32_t owner_idle;
    unsigned channel;
    unsigned backoff_qdb;
    uint32_t rate;        // selected native playback rate
    size_t loaded;        // samples currently in the bank
    bool initialized;
    bool running;
} iqtx;

static const uint32_t iqtx_rates[] = {40000000, 80000000};

static inline void iqtx_fence(void)
{
    __asm__ volatile("memw" ::: "memory");
}

// ------------------------------------------------------------------
// backend: init / load / start / stop (single S3 target)
// ------------------------------------------------------------------

static esp_err_t iqtx_backend_stop(void)
{
    REG_WRITE(IQTX_PLAY, 0);
    REG_WRITE(IQTX_ADC, 0);
    stop_tx_tone(0);
    force_txon_mode(0, 0, 0);
    rom_loopback_mode_en(0);
    rom_pbus_xpd_tx_off();
    rom_pbus_xpd_rx_off();
    rom_set_txclk_en(0);
    rom_set_rxclk_en(0);
    rom_pbus_workmode();
    REG_WRITE(IQTX_OWNER, iqtx.owner_idle);
    iqtx_fence();
    return ESP_OK;
}

static esp_err_t iqtx_backend_init(unsigned channel, unsigned backoff_qdb)
{
    // The verified bring-up: run the full WiFi driver once, then unload
    // it before taking the RF. MicroPython's wlan.active(False) only
    // stops the driver (esp_wifi_stop, not esp_wifi_deinit), so the
    // takeover finishes the job itself -- this whole session owns the
    // RF exclusively by design.
    wifi_mode_t mode = WIFI_MODE_NULL;
    if (esp_wifi_get_mode(&mode) != ESP_OK) {
        return ESP_ERR_INVALID_STATE;
    }
    if (mode != WIFI_MODE_NULL) {
        esp_wifi_stop();
        esp_err_t err = esp_wifi_deinit();
        if (err != ESP_OK) {
            return err;
        }
    }

    wifi_init_config_t wifi = WIFI_INIT_CONFIG_DEFAULT();
    wifi.nvs_enable = false;
    wifi.static_rx_buf_num = 4;
    wifi.dynamic_rx_buf_num = 8;
    wifi.dynamic_tx_buf_num = 8;
    esp_err_t err = esp_wifi_init(&wifi);
    if (err != ESP_OK) return err;
    if ((err = esp_wifi_set_storage(WIFI_STORAGE_RAM)) != ESP_OK) return err;
    if ((err = esp_wifi_set_mode(WIFI_MODE_STA)) != ESP_OK) return err;
    if ((err = esp_wifi_start()) != ESP_OK) return err;
    if ((err = esp_wifi_set_ps(WIFI_PS_NONE)) != ESP_OK) return err;
    if ((err = esp_wifi_stop()) != ESP_OK) return err;
    if ((err = esp_wifi_deinit()) != ESP_OK) return err;

    esp_wifi_power_domain_on();
    periph_module_enable(PERIPH_WIFI_MODULE);
    esp_phy_enable(PHY_MODEM_WIFI);
    esp_phy_rftest_config(1);
    esp_phy_rftest_init();
    esp_phy_tx_contin_en(false);
    esp_phy_cbw40m_en(false);
    iqtx.owner_idle = REG_READ(IQTX_OWNER) & ~15u;
    iqtx.channel = channel;
    iqtx.backoff_qdb = backoff_qdb;
    return iqtx_backend_stop();
}

// Full teardown so a later iqtx.init() gets fresh vendor RF state (and
// full TX power -- see the power caveat in the header comment).
static esp_err_t iqtx_backend_deinit(void)
{
    esp_err_t err = iqtx_backend_stop();
    periph_module_disable(PERIPH_WIFI_MODULE);
    // MicroPython's network layer keeps an "initialized" flag that our
    // esp_wifi_deinit() invalidated behind its back; re-create the
    // stopped IDF driver so wlan.active(True) only needs to start it.
    wifi_init_config_t wifi = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&wifi);
    return err;
}

static esp_err_t iqtx_backend_start(size_t count)
{
    unsigned dac_clock = iqtx.rate == 40000000 ? 1 : 0;
    set_chanfreq(2412 + 5 * (iqtx.channel - 1), 0);
    stop_tx_tone(0);
    rom_pbus_debugmode();
    rom_set_txclk_en(1);
    rom_dac_rate_set(dac_clock);
    rom_pbus_xpd_tx_on(0, 128);
    rom_set_rxclk_en(1);
    rom_set_loopback_gain(0, 0x104, 128);
    rom_loopback_mode_en(1);
    force_txon_mode(1, 0, 10);
    esp_rom_delay_us(20);
    rom_loopback_mode_en(0);
    esp_phy_wifi_tx_tone(1, iqtx.channel, iqtx.backoff_qdb);
    rom_set_txclk_en(1);
    rom_set_rxclk_en(0);
    rom_dac_rate_set(dac_clock);

    // The historical failure: leaving any NCO enabled after vendor CW
    // prep radiates the tone generator instead of the SRAM IQ. Clear
    // all three, then hand the bank to RF and hit PLAY.
    REG_CLR_BIT(IQTX_NCO1, BIT(18));
    REG_CLR_BIT(IQTX_NCO2, BIT(18));
    REG_CLR_BIT(IQTX_NCO3, BIT(18));

    REG_WRITE(IQTX_ADC, 0);
    REG_WRITE(IQTX_PLAY, 0);
    REG_WRITE(IQTX_OWNER, iqtx.owner_idle | 4u);
    uint32_t control = (uint32_t)count | (((uint32_t)count & 255u) << 20) | BIT(19);
    if (!dac_clock) {
        control |= BIT(15);
    }
    REG_WRITE(IQTX_PLAY, control);
    iqtx_fence();
    REG_WRITE(IQTX_PLAY, control | BIT(31));
    iqtx_fence();
    return ESP_OK;
}

// ------------------------------------------------------------------
// Python API
// ------------------------------------------------------------------

// iqtx.caps() -> (chip, max_samples, loop_align, rates_tuple, rate)
static mp_obj_t iqtx_caps(void)
{
    mp_obj_t rates[2] = {
        MP_OBJ_NEW_SMALL_INT(iqtx_rates[0]),
        MP_OBJ_NEW_SMALL_INT(iqtx_rates[1]),
    };
    mp_obj_t items[5] = {
        MP_OBJ_NEW_QSTR(MP_QSTR_ESP32_S3),
        MP_OBJ_NEW_SMALL_INT(IQTX_MAX_SAMPLES),
        MP_OBJ_NEW_SMALL_INT(IQTX_LOOP_ALIGN),
        mp_obj_new_tuple(2, rates),
        MP_OBJ_NEW_SMALL_INT((mp_int_t)iqtx.rate),
    };
    return mp_obj_new_tuple(5, items);
}
static MP_DEFINE_CONST_FUN_OBJ_0(iqtx_caps_obj, iqtx_caps);

// iqtx.init(channel=6, backoff_qdb=60)
static mp_obj_t iqtx_init(size_t n_args, const mp_obj_t *args, mp_map_t *kw_args)
{
    static const mp_arg_t allowed[] = {
        { MP_QSTR_channel,     MP_ARG_INT | MP_ARG_KW_ONLY, {.u_int = 6} },
        { MP_QSTR_backoff_qdb, MP_ARG_INT | MP_ARG_KW_ONLY, {.u_int = 60} },
    };
    mp_arg_val_t vals[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, args, kw_args, MP_ARRAY_SIZE(allowed), allowed, vals);

    int channel = vals[0].u_int;
    int backoff = vals[1].u_int;
    if (channel < 1 || channel > IQTX_MAX_CHANNELS) {
        mp_raise_ValueError(MP_ERROR_TEXT("channel 1-13"));
    }
    if (backoff < 0 || backoff > IQTX_MAX_BACKOFF) {
        mp_raise_ValueError(MP_ERROR_TEXT("backoff_qdb 0-120"));
    }
    if (iqtx.initialized) {
        mp_raise_ValueError(MP_ERROR_TEXT("already initialized"));
    }
    esp_err_t err = iqtx_backend_init(channel, backoff);
    if (err == ESP_ERR_INVALID_STATE) {
        mp_raise_msg(&mp_type_OSError,
            MP_ERROR_TEXT("wifi stack still up -- wlan.active(False) first"));
    }
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("init failed: %d"), (int)err);
    }
    iqtx.initialized = true;
    iqtx.running = false;
    iqtx.loaded = 0;
    iqtx.rate = iqtx_rates[0];
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(iqtx_init_obj, 0, iqtx_init);

// iqtx.deinit() -- release RF so a later init() gets fresh vendor state
static mp_obj_t iqtx_deinit(void)
{
    if (!iqtx.initialized) {
        return mp_const_none;
    }
    iqtx_backend_deinit();
    iqtx.initialized = false;
    iqtx.running = false;
    iqtx.loaded = 0;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(iqtx_deinit_obj, iqtx_deinit);

// iqtx.rate(hz) -- stopped only; 40000000 or 80000000
static mp_obj_t iqtx_rate(mp_obj_t hz_obj)
{
    if (!iqtx.initialized) {
        mp_raise_ValueError(MP_ERROR_TEXT("not initialized"));
    }
    if (iqtx.running) {
        mp_raise_ValueError(MP_ERROR_TEXT("stop first"));
    }
    uint32_t hz = (uint32_t)mp_obj_get_int(hz_obj);
    if (hz != iqtx_rates[0] && hz != iqtx_rates[1]) {
        mp_raise_ValueError(MP_ERROR_TEXT("rate: 40000000 or 80000000"));
    }
    iqtx.rate = hz;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(iqtx_rate_obj, iqtx_rate);

// iqtx.load(buf) -- packed uint32 LE samples, upper 12 bits zero.
// A bytes/bytearray of length 4*n, or an array('I') of n words.
static mp_obj_t iqtx_load(mp_obj_t buf_obj)
{
    if (!iqtx.initialized || iqtx.running) {
        mp_raise_ValueError(MP_ERROR_TEXT("stopped-only operation"));
    }
    if (!IQTX_BANK_PROTECTED) {
        mp_raise_msg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("RF bank unprotected in this build -- load would overwrite the MicroPython heap"));
    }
    mp_buffer_info_t bi;
    mp_get_buffer_raise(buf_obj, &bi, MP_BUFFER_READ);
    if (bi.len == 0 || (bi.len & 3u)) {
        mp_raise_ValueError(MP_ERROR_TEXT("buffer must be whole uint32 words"));
    }
    size_t count = bi.len / 4;
    if (count > IQTX_MAX_SAMPLES) {
        mp_raise_ValueError(MP_ERROR_TEXT("too many samples (max 16128)"));
    }
    const uint32_t *words = (const uint32_t *)bi.buf;
    for (size_t n = 0; n < count; ++n) {
        if (words[n] & 0xfff00000u) {
            mp_raise_ValueError(MP_ERROR_TEXT("sample has non-zero upper 12 bits"));
        }
    }
    REG_WRITE(IQTX_OWNER, iqtx.owner_idle);
    volatile uint32_t *dest = (volatile uint32_t *)IQTX_SRAM_BASE;
    for (size_t n = 0; n < count; ++n) {
        dest[n] = words[n];
    }
    iqtx_fence();
    iqtx.loaded = count;
    return MP_OBJ_NEW_SMALL_INT((mp_int_t)count);
}
static MP_DEFINE_CONST_FUN_OBJ_1(iqtx_load_obj, iqtx_load);

// iqtx.start() -- hardware loop; count must be 256-aligned (16 circles
// of the 64-sample tone give 1024, which satisfies alignment)
static mp_obj_t iqtx_start(void)
{
    if (!iqtx.initialized || iqtx.running) {
        mp_raise_ValueError(MP_ERROR_TEXT("already running or not initialized"));
    }
    if (!iqtx.loaded) {
        mp_raise_ValueError(MP_ERROR_TEXT("nothing loaded"));
    }
    if (iqtx.loaded % IQTX_LOOP_ALIGN) {
        mp_raise_ValueError(MP_ERROR_TEXT("loop playback needs a 256-aligned count"));
    }
    esp_err_t err = iqtx_backend_start(iqtx.loaded);
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_RuntimeError,
            MP_ERROR_TEXT("start failed: %d"), (int)err);
    }
    iqtx.running = true;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(iqtx_start_obj, iqtx_start);

// iqtx.stop()
static mp_obj_t iqtx_stop(void)
{
    if (!iqtx.initialized) {
        return mp_const_none;
    }
    iqtx_backend_stop();
    iqtx.running = false;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(iqtx_stop_obj, iqtx_stop);

// iqtx.status() -> (running, loaded, rate, playback_register)
static mp_obj_t iqtx_status(void)
{
    uint32_t play = iqtx.initialized ? REG_READ(IQTX_PLAY) : 0;
    mp_obj_t items[4] = {
        mp_obj_new_bool(iqtx.running),
        MP_OBJ_NEW_SMALL_INT((mp_int_t)iqtx.loaded),
        MP_OBJ_NEW_SMALL_INT((mp_int_t)iqtx.rate),
        MP_OBJ_NEW_SMALL_INT((mp_int_t)play),
    };
    return mp_obj_new_tuple(4, items);
}
static MP_DEFINE_CONST_FUN_OBJ_0(iqtx_status_obj, iqtx_status);

static const mp_rom_map_elem_t iqtx_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_mod_iqtx) },
    { MP_ROM_QSTR(MP_QSTR_caps),     MP_ROM_PTR(&iqtx_caps_obj) },
    { MP_ROM_QSTR(MP_QSTR_init),    MP_ROM_PTR(&iqtx_init_obj) },
    { MP_ROM_QSTR(MP_QSTR_deinit),  MP_ROM_PTR(&iqtx_deinit_obj) },
    { MP_ROM_QSTR(MP_QSTR_rate),    MP_ROM_PTR(&iqtx_rate_obj) },
    { MP_ROM_QSTR(MP_QSTR_load),   MP_ROM_PTR(&iqtx_load_obj) },
    { MP_ROM_QSTR(MP_QSTR_start),   MP_ROM_PTR(&iqtx_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_stop),    MP_ROM_PTR(&iqtx_stop_obj) },
    { MP_ROM_QSTR(MP_QSTR_status),  MP_ROM_PTR(&iqtx_status_obj) },
    { MP_ROM_QSTR(MP_QSTR_MAX_SAMPLES), MP_ROM_INT(IQTX_MAX_SAMPLES) },
    { MP_ROM_QSTR(MP_QSTR_LOOP_ALIGN),  MP_ROM_INT(IQTX_LOOP_ALIGN) },
    { MP_ROM_QSTR(MP_QSTR_BANK_PROTECTED), MP_ROM_INT(IQTX_BANK_PROTECTED) },
};
static MP_DEFINE_CONST_DICT(iqtx_globals, iqtx_globals_table);

const mp_obj_module_t iqtx_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&iqtx_globals,
};

MP_REGISTER_MODULE(MP_QSTR_iqtx, iqtx_user_cmodule);
