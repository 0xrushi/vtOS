/*
 * dualboot loader -- vtOS on the LilyGO T-Deck (ESP32-S3)
 *
 * Boot selector between the two OTA payload slots defined in
 * utils/dualboot/partitions-dual.csv:
 *   ota_0 = vtos  (full vtOS, MicroPython v1.28)
 *   ota_1 = iqlab (esp32-iq-tx sample_rates / ofdm lab firmware)
 *
 * Mechanism (mesh-loader, https://github.com/eliahreeves/mesh-loader):
 * payloads are built with CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y and
 * never call esp_ota_mark_app_valid, so after every payload reset the
 * bootloader rolls back to this factory partition.
 *
 * UI: 3 s window on the T-Deck display.
 *   - highlighted entry boots at timeout (last-used is the default)
 *   - BOOT key tap while the menu is up switches the highlight
 *   - BOOT key held >= 1 s boots the highlighted entry immediately
 *   - serial byte on the USB-JTAG console: '1'/'2' picks a slot, any
 *     other byte toggles -- host-driven switching with no timing race
 *
 * Copyright (c) 2026 8bitmcu
 * License: MIT
 */

#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/usb_serial_jtag.h"
#include "esp_err.h"
#include "display_min.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_rom_sys.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "nvs.h"
#include "nvs_flash.h"

#define BOOT_BTN_GPIO  0
#define WINDOW_MS      3000
#define POLL_MS        20
#define HOLD_BOOT_MS   1000

/* layout (320x240) */
#define TITLE_Y   20
#define ITEM0_Y   64
#define ITEM_STEP 44
#define BAR_Y     200
#define FOOT_Y    218
#define ITEM_X    76
#define ITEM_W    168
#define ITEM_H    32

#define NUM_SLOTS 3

static const char *const SLOT_NAMES[NUM_SLOTS] = {"vtOS", "iq lab", "wadamesh"};
static const esp_partition_subtype_t SLOT_SUBTYPES[NUM_SLOTS] = {
    ESP_PARTITION_SUBTYPE_APP_OTA_0,
    ESP_PARTITION_SUBTYPE_APP_OTA_1,
    ESP_PARTITION_SUBTYPE_APP_OTA_2,
};

static const char *name_for(int target)
{
    return (target >= 0 && target < NUM_SLOTS) ? SLOT_NAMES[target] : "?";
}

static const esp_partition_t *slot_partition(int slot)
{
    return esp_partition_find_first(ESP_PARTITION_TYPE_APP, SLOT_SUBTYPES[slot], NULL);
}

/* A slot is bootable only if its partition exists and holds an app image
 * (ESP image magic 0xE9 at offset 0). An erased slot reads 0xFF. */
static bool slot_valid(int slot)
{
    const esp_partition_t *p = slot_partition(slot);
    uint8_t magic = 0;
    if (p == NULL || esp_partition_read(p, 0, &magic, 1) != ESP_OK) {
        return false;
    }
    return magic == 0xE9;
}

/* Next valid slot after `from` (wraps); `from` itself if it is the only one. */
static int next_slot(int from)
{
    for (int i = 1; i <= NUM_SLOTS; ++i) {
        int s = (from + i) % NUM_SLOTS;
        if (slot_valid(s)) {
            return s;
        }
    }
    return from;
}

static int read_last_target(void)
{
    nvs_handle_t h;
    if (nvs_open("dualboot", NVS_READONLY, &h) != ESP_OK) {
        return 0;
    }
    int32_t v = 0;
    if (nvs_get_i32(h, "target", &v) != ESP_OK) {
        v = 0;
    }
    nvs_close(h);
    return (int)v;
}

static void save_target(int target)
{
    nvs_handle_t h;
    if (nvs_open("dualboot", NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_i32(h, "target", (int32_t)target);
    nvs_commit(h);
    nvs_close(h);
}

static void draw_item(int slot, bool selected)
{
    uint16_t y = (uint16_t)(ITEM0_Y + slot * ITEM_STEP);
    bool valid = slot_valid(slot);
    uint16_t bg = selected ? COLOR_CYAN : COLOR_DARK;
    uint16_t fg = selected ? COLOR_BLACK : (valid ? COLOR_WHITE : COLOR_DIM);
    display_rect(ITEM_X, y, ITEM_W, ITEM_H, bg);
    const char *name = name_for(slot);
    /* center the 8x2-scaled label: 8px*2scale = 16px per char */
    uint16_t tw = (uint16_t)(strlen(name) * 16);
    display_text((uint16_t)(ITEM_X + (ITEM_W - tw) / 2), (uint16_t)(y + 8),
                 name, fg, bg, 2);
}

static void draw_menu(int target, int elapsed)
{
    display_rect(0, TITLE_Y, DISPLAY_WIDTH, 16, COLOR_DARK);
    display_text(96, TITLE_Y, "vtOS dualboot", COLOR_WHITE, COLOR_DARK, 2);
    for (int i = 0; i < NUM_SLOTS; ++i) {
        draw_item(i, target == i);
    }
    /* countdown bar */
    display_rect(40, BAR_Y, 240, 10, COLOR_DARK);
    uint16_t filled = (uint16_t)(240 * (WINDOW_MS - elapsed) / WINDOW_MS);
    if (filled > 240) {
        filled = 240;
    }
    display_rect(40, BAR_Y, filled, 10, COLOR_GREEN);
    display_rect(0, FOOT_Y, DISPLAY_WIDTH, 16, COLOR_DARK);
    display_text(24, FOOT_Y, "BOOT: switch   hold: boot now", COLOR_DIM, COLOR_DARK, 1);
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }

    int target = read_last_target();
    if (target < 0 || target >= NUM_SLOTS || !slot_valid(target)) {
        target = 0;
    }

    gpio_config_t io = {
        .pin_bit_mask = 1ULL << BOOT_BTN_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);

    /* usb_serial_jtag_read_bytes() dereferences the driver's RX ringbuffer;
     * without an installed driver it panics (LoadProhibited, addr 0x4). */
    usb_serial_jtag_driver_config_t usj = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb_serial_jtag_driver_install(&usj);

    esp_err_t derr = display_init();
    printf("dualboot: display_init -> %s (0x%x)\r\n", esp_err_to_name(derr), (unsigned)derr);
    display_fill(COLOR_BLACK);
    draw_menu(target, 0);

    printf("\r\ndualboot: [1]vtOS [2]iq lab [3]wadamesh -- BOOT to switch, booting in %d ms\r\n",
           WINDOW_MS);

    int64_t t0 = esp_timer_get_time();
    int held_ms = 0;
    bool prev_down = false;
    int last_draw = -1;
    bool boot_now = false;

    while (!boot_now) {
        int64_t now = esp_timer_get_time();
        int elapsed = (int)((now - t0) / 1000);
        if (elapsed >= WINDOW_MS) {
            break;
        }

        /* serial control: '1'/'2' pick, anything else toggles */
        uint8_t c = 0;
        int n = usb_serial_jtag_read_bytes(&c, 1, 0);
        if (n > 0) {
            if (c >= '1' && c < '1' + NUM_SLOTS) {
                target = c - '1';
            } else {
                target = next_slot(target);
            }
            printf("dualboot: serial -> %s\r\n", name_for(target));
            boot_now = true;
            break;
        }

        /* BOOT key: tap = switch highlight, hold = boot now */
        bool down = gpio_get_level(BOOT_BTN_GPIO) == 0;
        if (down) {
            held_ms += POLL_MS;
        } else {
            if (prev_down && held_ms > 0 && held_ms < HOLD_BOOT_MS) {
                target = next_slot(target);
                printf("dualboot: BOOT tap -> %s\r\n", name_for(target));
            }
            held_ms = 0;
        }
        prev_down = down;
        if (held_ms >= HOLD_BOOT_MS) {
            printf("dualboot: BOOT hold -> %s\r\n", name_for(target));
            boot_now = true;
            break;
        }

        if (elapsed / 100 != last_draw) {
            last_draw = elapsed / 100;
            draw_menu(target, elapsed);
        }

        esp_rom_delay_us(POLL_MS * 1000);
    }

    /* diagnostics for post-mortem from the payloads */
    {
        nvs_handle_t h;
        if (nvs_open("dualboot", NVS_READWRITE, &h) == ESP_OK) {
            nvs_set_i32(h, "held", held_ms);
            nvs_commit(h);
            nvs_close(h);
        }
    }

    if (!slot_valid(target)) {
        printf("dualboot: slot %d (%s) empty, falling back to vtOS\r\n", target, name_for(target));
        target = 0;
    }
    const esp_partition_t *part = slot_partition(target);
    if (part == NULL) {
        printf("dualboot: ota_%d missing -- staying in loader\r\n", target);
        display_text(48, ITEM0_Y, "slot missing -- stuck", COLOR_RED, COLOR_BLACK, 2);
        return;
    }

    save_target(target);
    if (esp_ota_set_boot_partition(part) != ESP_OK) {
        printf("dualboot: set_boot_partition failed\r\n");
        return;
    }
    printf("dualboot: booting %s\r\n", name_for(target));
    esp_restart();
}
