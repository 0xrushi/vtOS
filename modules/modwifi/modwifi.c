/*
 * modwifi: MicroPython C module exposing ESP-IDF WiFi promiscuous-mode
 * capture (passive 802.11 sniffer) to the vtOS shell.
 *
 * Passive capture of management, control and data frames for protocol
 * analysis and PCAP export of networks you own or are authorized to
 * assess. It does not inject frames or clone APs -- no deauth, evil
 * twin, captive portal, or cyclical-protocol attack surface of
 * upstream toolkits.
 *
 * Ring design: strict single-producer (WiFi rx task) /
 * single-consumer (MicroPython task) ring with relaxed atomics on the
 * cursors -- release store on advance, acquire load when peeking, so
 * a full slot copy is always visible before the cursor moves.
 *
 * Sizing: the ring is allocated once at sniff_start -- internal SRAM
 * first (fast enough for the rx-callback path), PSRAM as a fallback
 * (slower but leaves SRAM for BLE/audio). Ring memory is freed at
 * sniff_stop.
 *
 * Copyright (c) 2026 8bitmcu
 * License: MIT
 */

#include "py/runtime.h"
#include "py/mphal.h"

#include "esp_wifi.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "nvs.h"

#include <string.h>
#include <stdint.h>

// Per-slot payload cap: beacons/probes (+EAPOL) are typically well
// under 256 bytes; longer frames are counted (skipped), not copied.
#define MODWIFI_DEFAULT_FRAME_LEN   256
#define MODWIFI_MAX_FRAME_LEN       1600
#define MODWIFI_DEFAULT_SLOTS       96
#define MODWIFI_MIN_SLOTS           8
#define MODWIFI_MAX_SLOTS           512

// Filter bitmask passed from Python: 1 = management, 2 = data,
// 4 = control. Maps onto the 802.11 frame-control type field
// (payload byte 0, bits 2..3). The ESP-IDF hardware filter stays wide
// open (all types) and dropping happens in the callback.
enum {
    MODWIFI_FILT_MGMT = 1,
    MODWIFI_FILT_DATA = 2,
    MODWIFI_FILT_CTRL = 4,
    MODWIFI_FILT_ALL  = 7,
};

typedef struct {
    uint16_t len;       // payload bytes stored just past this struct
    uint8_t  channel;   // capture channel at rx time
    uint8_t  rssi;      // signed dBm, cast to u8 for the slot header
//  uint8_t  data[frame_len];  -- variable tail, constant stride ring
} sniff_slot_t;

#define SNIFF_SLOT_HDR sizeof(sniff_slot_t)

// Ring state -- cursors advanced with atomics only.
static sniff_slot_t *sn_ring = NULL;
static uint16_t sn_channel;          // last locked/hopped channel
static uint16_t sn_slots_n;          // number of slots
static uint16_t sn_frame_len;        // payload bytes per slot
static uint16_t sn_slot_size;        // sizeof(sniff_slot_t) + frame_len
static uint8_t  sn_filter;           // MODWIFI_FILT_* mask
static volatile uint8_t  sn_psram;   // ring lives in PSRAM (slow mode)
static volatile uint32_t sn_write;   // producer cursor (WiFi task)
static volatile uint32_t sn_read;    // consumer cursor (MicroPython task)
static volatile uint32_t sn_captured;
static volatile uint32_t sn_dropped;    // ring was full
static volatile uint32_t sn_skipped;    // frame longer than sn_frame_len
static volatile uint8_t  sn_active;

// Channel-hop timer (optional, esp_timer periodic).
static esp_timer_handle_t sn_hop_timer;
static int  sn_hop_ch;               // next channel to visit
static int  sn_hop_max;              // inclusive top of 1..sn_hop_max
static volatile uint8_t sn_hop_on;

// ------------------------------------------------------------------
// rx callback -- runs in the WiFi driver task at elevated priority.
// Keep it allocation-free, lock-free and short: copy or count, move
// the cursor, return.
// ------------------------------------------------------------------
static void IRAM_ATTR sniff_promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type)
{
    (void)type;
    if (!sn_active) {
        return;
    }

    const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t *)buf;
    uint32_t len = (uint32_t)pkt->rx_ctrl.sig_len + 4; // FCS trailer

    if (len > (uint32_t)sn_frame_len) {
        sn_skipped++;
        return;
    }

    // 802.11 frame-control type: 0 mgmt, 1 ctrl, 2 data.
    const uint8_t type_bit[3] = {
        MODWIFI_FILT_MGMT, MODWIFI_FILT_CTRL, MODWIFI_FILT_DATA
    };
    uint8_t ftype = 3;
    if (len > 0) {
        ftype = (pkt->payload[0] >> 2) & 0x3;
    }
    if (ftype > 2 || !(sn_filter & type_bit[ftype])) {
        return;
    }

    uint32_t wr = sn_write;
    uint32_t next = wr + 1;
    if (next >= (uint32_t)sn_slots_n) {
        next = 0;
    }
    uint32_t rd = sn_read;
    if (next == rd) {               // consumer hasn't drained: full
        sn_dropped++;
        return;
    }

    sniff_slot_t *slot = (sniff_slot_t *)((uint8_t *)sn_ring + (size_t)wr * sn_slot_size);
    slot->len = (uint16_t)len;
    slot->channel = (uint8_t)pkt->rx_ctrl.channel;
    slot->rssi = (uint8_t)pkt->rx_ctrl.rssi;
    memcpy((uint8_t *)slot + SNIFF_SLOT_HDR, pkt->payload, len);

    // Release store: frame bytes are visible before the cursor claim.
    __atomic_store_n(&sn_write, next, __ATOMIC_RELEASE);
    sn_captured++;
}

// ------------------------------------------------------------------
// channel-hop timer callback (esp_timer task context)
// ------------------------------------------------------------------
static void sniff_hop_tick(void *arg)
{
    (void)arg;
    if (!sn_active) {
        return;
    }
    int nxt = sn_hop_ch;
    esp_wifi_set_channel((uint8_t)nxt, WIFI_SECOND_CHAN_NONE);
    nxt += 1;
    if (nxt > sn_hop_max) {
        nxt = 1;
    }
    sn_hop_ch = nxt;
}

static inline uint32_t sniff_ring_load(volatile uint32_t *v)
{
    return __atomic_load_n(v, __ATOMIC_ACQUIRE);
}

static inline void sniff_precision_reset(void)
{
    sn_captured = 0;
    sn_dropped = 0;
    sn_skipped = 0;
    sn_write = 0;
    sn_read = 0;
}

// ------------------------------------------------------------------
// Python API
// ------------------------------------------------------------------

// wifi.sniff_start(channel=6, filter=7, slots=96, frame_len=256,
//                  hop_ms=0, hop_max=11)
static mp_obj_t sniff_start(size_t n_args, const mp_obj_t *args, mp_map_t *kw_args)
{
    static const mp_arg_t allowed[] = {
        { MP_QSTR_channel,   MP_ARG_INT | MP_ARG_KW_ONLY, {.u_int = 6} },
        { MP_QSTR_filter,    MP_ARG_INT | MP_ARG_KW_ONLY, {.u_int = MODWIFI_FILT_ALL} },
        { MP_QSTR_slots,     MP_ARG_INT | MP_ARG_KW_ONLY, {.u_int = MODWIFI_DEFAULT_SLOTS} },
        { MP_QSTR_frame_len, MP_ARG_INT | MP_ARG_KW_ONLY, {.u_int = MODWIFI_DEFAULT_FRAME_LEN} },
        { MP_QSTR_hop_ms,    MP_ARG_INT | MP_ARG_KW_ONLY, {.u_int = 0} },
        { MP_QSTR_hop_max,   MP_ARG_INT | MP_ARG_KW_ONLY, {.u_int = 11} },
    };
    mp_arg_val_t vals[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, args, kw_args, MP_ARRAY_SIZE(allowed), allowed, vals);

    int channel = vals[0].u_int;
    int filter = vals[1].u_int;
    int nslots = vals[2].u_int;
    int flen = vals[3].u_int;
    int hop_ms = vals[4].u_int;
    int hop_max = vals[5].u_int;

    if (sn_active) {
        mp_raise_ValueError(MP_ERROR_TEXT("sniffer already active"));
    }
    if (channel < 1 || channel > 14) {
        mp_raise_ValueError(MP_ERROR_TEXT("channel 1-14"));
    }
    if (!(filter > 0 && filter <= MODWIFI_FILT_ALL)) {
        mp_raise_ValueError(MP_ERROR_TEXT("filter: 1=mgmt 2=data 4=ctrl"));
    }
    if (nslots < MODWIFI_MIN_SLOTS || nslots > MODWIFI_MAX_SLOTS) {
        mp_raise_ValueError(MP_ERROR_TEXT("slots out of range (8-512)"));
    }
    if (flen < 32 || flen > MODWIFI_MAX_FRAME_LEN) {
        mp_raise_ValueError(MP_ERROR_TEXT("frame_len out of range (32-1600)"));
    }

    // WiFi must already be up; the app calls
    // network.WLAN(network.STA_IF).active(True) first.
    wifi_mode_t mode = WIFI_MODE_NULL;
    if (esp_wifi_get_mode(&mode) != ESP_OK || mode == WIFI_MODE_NULL) {
        mp_raise_msg(&mp_type_OSError,
            MP_ERROR_TEXT("wifi not started -- network.WLAN(STA_IF).active(True) first"));
    }

    sn_slots_n = (uint16_t)nslots;
    sn_frame_len = (uint16_t)flen;
    sn_slot_size = (uint16_t)(SNIFF_SLOT_HDR + flen);
    size_t total = (size_t)sn_slot_size * sn_slots_n;

    // Internal SRAM first (fast for the rx callback); PSRAM fallback:
    // slower, but leaves SRAM for BLE/audio. If both fail, clean exit.
    sn_psram = 0;
    sn_ring = heap_caps_malloc(total, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (sn_ring == NULL) {
        sn_psram = 1;
        sn_ring = heap_caps_malloc(total, MALLOC_CAP_SPIRAM);
    }
    if (sn_ring == NULL) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("ring alloc failed"));
    }

    sn_filter = (uint8_t)filter;
    sn_channel = (uint16_t)channel;
    sn_write = 0;
    sn_read = 0;
    sn_captured = 0;
    sn_dropped = 0;
    sn_skipped = 0;

    // Background hop, if requested: cycle 1..hop_max from a fresh page.
    sn_hop_on = 0;
    sn_hop_ch = channel + 1;
    if (sn_hop_ch > hop_max) {
        sn_hop_ch = 1;
    }
    sn_hop_max = hop_max < 1 ? 1 : hop_max;
    if (hop_ms > 0) {
        const esp_timer_create_args_t targs = {
            .callback = sniff_hop_tick,
            .name = "sniffhop",
        };
        if (esp_timer_create(&targs, &sn_hop_timer) != ESP_OK) {
            heap_caps_free(sn_ring);
            sn_ring = NULL;
            mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("hop timer alloc failed"));
        }
    }

    // Channel lock must land before the capture callback runs.
    if (esp_wifi_set_channel((uint8_t)channel, WIFI_SECOND_CHAN_NONE) != ESP_OK) {
        if (hop_ms > 0) {
            esp_timer_delete(sn_hop_timer);
        }
        heap_caps_free(sn_ring);
        sn_ring = NULL;
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("set channel failed"));
    }

    sn_active = 1;
    if (hop_ms > 0) {
        if (esp_timer_start_periodic(sn_hop_timer, (uint64_t)hop_ms * 1000) == ESP_OK) {
            sn_hop_on = 1;
        }
    }

    // Wide-open hardware mask; per-type choices happen in the rx
    // callback via the frame-control parse (see MODWIFI_FILT_*).
    wifi_promiscuous_filter_t pfilt = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA
                     | WIFI_PROMIS_FILTER_MASK_CTRL,
    };
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_promiscuous_filter(&pfilt);
    esp_wifi_set_promiscuous_rx_cb(sniff_promisc_cb);

    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(sniff_start_obj, 0, sniff_start);

static mp_obj_t sniff_stop(void)
{
    if (!sn_active) {
        return mp_const_none;
    }

    // Detach and synchronize first: with promiscuous off, the driver
    // stops delivering callbacks -- the producer is quiescent and the
    // ring can be freed.
    esp_wifi_set_promiscuous(false);
    esp_wifi_set_promiscuous_rx_cb(NULL);
    sn_active = 0;

    if (sn_hop_on) {
        esp_timer_stop(sn_hop_timer);
        sn_hop_on = 0;
    }
    if (sn_hop_timer != NULL) {
        esp_timer_delete(sn_hop_timer);
        sn_hop_timer = NULL;
    }

    // Settle delay so an in-flight callback cannot touch the ring
    // after free. The sn_active gate (checked first in the callback)
    // makes this belt-and-braces.
    mp_hal_delay_ms(10);

    heap_caps_free(sn_ring);
    sn_ring = NULL;
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(sniff_stop_obj, sniff_stop);

// wifi.sniff_stats() -> (captured, dropped, skipped, psram_flag)
static mp_obj_t sniff_stats(void)
{
    mp_obj_t items[4];
    items[0] = MP_OBJ_NEW_SMALL_INT((mp_int_t)sn_captured);
    items[1] = MP_OBJ_NEW_SMALL_INT((mp_int_t)sn_dropped);
    items[2] = MP_OBJ_NEW_SMALL_INT((mp_int_t)sn_skipped);
    items[3] = MP_OBJ_NEW_SMALL_INT(sn_psram);
    return mp_obj_new_tuple(4, items);
}
static MP_DEFINE_CONST_FUN_OBJ_0(sniff_stats_obj, sniff_stats);

// wifi.sniff_read(buf) -> n
// Drains complete frames into a writable buffer as packed records:
//   [u16 len (LE)][u8 channel][u8 rssi][payload bytes]
// Records are never split; returns records copied.
static mp_obj_t sniff_read(mp_obj_t buf_obj)
{
    if (!sn_active || sn_ring == NULL) {
        return MP_OBJ_NEW_SMALL_INT(0);
    }

    mp_buffer_info_t bi;
    mp_get_buffer_raise(buf_obj, &bi, MP_BUFFER_WRITE);
    uint8_t *out = (uint8_t *)bi.buf;
    size_t cap = bi.len;
    size_t pos = 0;
    int n = 0;

    while (sn_read != sniff_ring_load(&sn_write)) {
        sniff_slot_t *slot = (sniff_slot_t *)((uint8_t *)sn_ring + (size_t)sn_read * sn_slot_size);
        uint16_t len = slot->len;
        size_t need = SNIFF_SLOT_HDR + len;
        if (pos + need > cap) {
            break;  // drain buffer full -- the rest stays in the ring
        }
        out[pos + 0] = (uint8_t)(len & 0xFF);
        out[pos + 1] = (uint8_t)(len >> 8);
        out[pos + 2] = slot->channel;
        out[pos + 3] = slot->rssi;
        memcpy(out + pos + SNIFF_SLOT_HDR, (uint8_t *)slot + SNIFF_SLOT_HDR, len);
        pos += need;
        n++;
        // Copy first, then publish the cursor: a slot the consumer
        // still holds can never be reused by the producer.
        __atomic_store_n(&sn_read, (uint32_t)((sn_read + 1) % sn_slots_n), __ATOMIC_RELEASE);
    }

    return MP_OBJ_NEW_SMALL_INT(n);
}
static MP_DEFINE_CONST_FUN_OBJ_1(sniff_read_obj, sniff_read);

// wifi.sniff_channel() -> int
// wifi.sniff_channel(new_ch) -> set lock channel
static mp_obj_t sniff_channel(size_t n_args, const mp_obj_t *args)
{
    if (n_args == 0) {
        return MP_OBJ_NEW_SMALL_INT((mp_int_t)sn_channel);
    }
    int ch = mp_obj_get_int(args[0]);
    if (ch < 1 || ch > 14) {
        mp_raise_ValueError(MP_ERROR_TEXT("channel 1-14"));
    }
    sn_channel = (uint16_t)ch;
    sn_hop_ch = ch;   // restart the hop cycle wherever we lock
    if (esp_wifi_set_channel((uint8_t)ch, WIFI_SECOND_CHAN_NONE) != ESP_OK) {
        mp_raise_msg(&mp_type_RuntimeError, MP_ERROR_TEXT("set channel failed"));
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(sniff_channel_obj, 0, 1, sniff_channel);

// Keep one confirmed station profile independently of the SDK's RAM config.
// Store only the fields needed to reconnect, with a layout stable across SDKs.
typedef struct {
    uint8_t ssid[32];
    uint8_t password[64];
} station_profile_t;

static mp_obj_t save_station(void) {
    wifi_ap_record_t ap;
    esp_err_t err = esp_wifi_sta_get_ap_info(&ap);
    if (err != ESP_OK) {
        mp_raise_OSError(err);
    }
    wifi_config_t config;
    err = esp_wifi_get_config(WIFI_IF_STA, &config);
    if (err != ESP_OK) {
        mp_raise_OSError(err);
    }
    station_profile_t profile;
    memcpy(profile.ssid, config.sta.ssid, sizeof(profile.ssid));
    memcpy(profile.password, config.sta.password, sizeof(profile.password));
    nvs_handle_t handle;
    err = nvs_open("vtos_wifi", NVS_READWRITE, &handle);
    if (err == ESP_OK) {
        err = nvs_set_blob(handle, "station", &profile, sizeof(profile));
        if (err == ESP_OK) {
            err = nvs_commit(handle);
        }
        nvs_close(handle);
    }
    if (err != ESP_OK) {
        mp_raise_OSError(err);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(save_station_obj, save_station);

static mp_obj_t load_station(void) {
    station_profile_t profile;
    size_t size = sizeof(profile);
    nvs_handle_t handle;
    esp_err_t err = nvs_open("vtos_wifi", NVS_READONLY, &handle);
    if (err == ESP_OK) {
        err = nvs_get_blob(handle, "station", &profile, &size);
        nvs_close(handle);
    }
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return mp_const_none;
    }
    if (err != ESP_OK) {
        mp_raise_OSError(err);
    }
    if (size != sizeof(profile) || !profile.ssid[0]) {
        mp_raise_ValueError(MP_ERROR_TEXT("invalid saved Wi-Fi profile"));
    }
    mp_obj_t pair[] = {
        mp_obj_new_bytes(profile.ssid, strnlen((const char *)profile.ssid, sizeof(profile.ssid))),
        mp_obj_new_bytes(profile.password, strnlen((const char *)profile.password, sizeof(profile.password))),
    };
    return mp_obj_new_tuple(2, pair);
}
static MP_DEFINE_CONST_FUN_OBJ_0(load_station_obj, load_station);

static mp_obj_t forget_station(void) {
    nvs_handle_t handle;
    esp_err_t err = nvs_open("vtos_wifi", NVS_READWRITE, &handle);
    if (err == ESP_OK) {
        err = nvs_erase_key(handle, "station");
        if (err == ESP_OK) {
            err = nvs_commit(handle);
        }
        nvs_close(handle);
    }
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
        mp_raise_OSError(err);
    }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(forget_station_obj, forget_station);

// ------------------------------------------------------------------
// module registration (modc2/lora style)
// ------------------------------------------------------------------

static const mp_rom_map_elem_t modwifi_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__),      MP_ROM_QSTR(MP_QSTR_modwifi) },
    { MP_ROM_QSTR(MP_QSTR_save_station),  MP_ROM_PTR(&save_station_obj) },
    { MP_ROM_QSTR(MP_QSTR_load_station),  MP_ROM_PTR(&load_station_obj) },
    { MP_ROM_QSTR(MP_QSTR_forget_station), MP_ROM_PTR(&forget_station_obj) },
    { MP_ROM_QSTR(MP_QSTR_sniff_start),   MP_ROM_PTR(&sniff_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_sniff_stop),    MP_ROM_PTR(&sniff_stop_obj) },
    { MP_ROM_QSTR(MP_QSTR_sniff_stats),   MP_ROM_PTR(&sniff_stats_obj) },
    { MP_ROM_QSTR(MP_QSTR_sniff_read),    MP_ROM_PTR(&sniff_read_obj) },
    { MP_ROM_QSTR(MP_QSTR_sniff_channel), MP_ROM_PTR(&sniff_channel_obj) },
    // filter masks
    { MP_ROM_QSTR(MP_QSTR_FILT_MGMT),     MP_ROM_INT(MODWIFI_FILT_MGMT) },
    { MP_ROM_QSTR(MP_QSTR_FILT_DATA),     MP_ROM_INT(MODWIFI_FILT_DATA) },
    { MP_ROM_QSTR(MP_QSTR_FILT_CTRL),     MP_ROM_INT(MODWIFI_FILT_CTRL) },
    { MP_ROM_QSTR(MP_QSTR_FILT_ALL),      MP_ROM_INT(MODWIFI_FILT_ALL) },
};
static MP_DEFINE_CONST_DICT(modwifi_globals, modwifi_globals_table);

const mp_obj_module_t modwifi_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&modwifi_globals,
};

MP_REGISTER_MODULE(MP_QSTR_modwifi, modwifi_user_cmodule);
