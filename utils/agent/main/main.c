/* SPDX-License-Identifier: MIT
 * T-Deck agent, phase A1: text console + keyboard + SD file tools.
 * Model, Wi-Fi and voice come in later phases (see TODO.md).
 */
#include "console.h"
#include "display_min.h"
#include "llm.h"
#include "net.h"
#include "tools.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <stdlib.h>
#include <string.h>

#define TAG "agent"
#define KBD_ADDR 0x55
#define PIN_I2C_SDA 18
#define PIN_I2C_SCL 8
#define PIN_TB_UP 3
#define PIN_TB_DOWN 15
#define INPUT_MAX 119

#define C_TEXT COLOR_WHITE
#define C_DIM COLOR_DIM
#define C_ERR COLOR_RED
#define C_OK COLOR_GREEN
#define C_USER COLOR_CYAN

static char input[INPUT_MAX + 1];
static size_t input_len;
static char pending[INPUT_MAX + 1];
static volatile bool has_pending;

static volatile uint32_t tb_up, tb_down;

static void IRAM_ATTR tb_isr(void *arg)
{
    if ((uintptr_t)arg == 0) {
        tb_up++;
    } else {
        tb_down++;
    }
}

static void trackball_init(void)
{
    static const int pins[2] = {PIN_TB_UP, PIN_TB_DOWN};
    gpio_install_isr_service(0);
    for (uintptr_t i = 0; i < 2; i++) {
        const gpio_config_t cfg = {
            .pin_bit_mask = 1ULL << pins[i],
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,
            .intr_type = GPIO_INTR_NEGEDGE,
        };
        gpio_config(&cfg);
        gpio_isr_handler_add(pins[i], tb_isr, (void *)i);
    }
}

/* ---- keyboard: one ASCII byte per read, 0 = none --------------------------- */

static void keyboard_task(void *arg)
{
    (void)arg;
    const i2c_master_bus_config_t bus_cfg = {
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus;
    i2c_master_dev_handle_t dev;
    const i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = KBD_ADDR,
        .scl_speed_hz = 400000,
    };
    if (i2c_new_master_bus(&bus_cfg, &bus) != ESP_OK ||
        i2c_master_bus_add_device(bus, &dev_cfg, &dev) != ESP_OK) {
        con_print(C_ERR, "keyboard init failed\n");
        vTaskDelete(NULL);
    }
    for (;;) {
        uint8_t key = 0;
        if (i2c_master_receive(dev, &key, 1, 10) == ESP_OK && key) {
            if (key == '\r' || key == '\n') {
                if (input_len && !has_pending) {
                    memcpy(pending, input, input_len + 1);
                    has_pending = true;
                    input_len = 0;
                    input[0] = '\0';
                }
            } else if (key == 0x08 || key == 0x7f) {
                if (input_len) {
                    input[--input_len] = '\0';
                }
            } else if (key >= 32 && key < 127 && input_len < INPUT_MAX) {
                input[input_len++] = (char)key;
                input[input_len] = '\0';
            }
            con_input("> ", input);
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

/* ---- commands --------------------------------------------------------------- */

static void mem_report(void)
{
    size_t i_total = heap_caps_get_total_size(MALLOC_CAP_INTERNAL);
    size_t i_free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    size_t i_low = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    size_t p_total = heap_caps_get_total_size(MALLOC_CAP_SPIRAM);
    size_t p_free = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    con_printf(C_DIM, "internal used %u KB of %u KB (peak %u KB)\n",
               (unsigned)((i_total - i_free) / 1024), (unsigned)(i_total / 1024),
               (unsigned)((i_total - i_low) / 1024));
    con_printf(C_DIM, "psram used %u KB of %u KB\n",
               (unsigned)((p_total - p_free) / 1024), (unsigned)(p_total / 1024));
}

static void ls_emit(const char *name, long size)
{
    con_printf(C_TEXT, " %-28s %ld\n", name, size);
}

static void unescape(char *text)
{
    char *w = text;
    for (char *r = text; *r; ++r) {
        if (r[0] == '\\' && r[1] == 'n') {
            *w++ = '\n';
            r++;
        } else {
            *w++ = *r;
        }
    }
    *w = '\0';
}

static const char *errname(int err)
{
    switch (-err) {
    case 19: return "no SD card";
    case 22: return "bad file name";
    case 2: return "not found";
    case 27: return "too big";
    default: return "i/o error";
    }
}

/* Ask a yes/no question on the console and wait for the next line typed. */
static bool confirm_line(const char *prompt)
{
    con_printf(C_OK, "%s\n", prompt);
    has_pending = false;
    while (!has_pending) {
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    char c = pending[0];
    has_pending = false;
    con_printf(C_USER, "> %s\n", pending);
    return c == 'y' || c == 'Y';
}

static agent_config_t config;

static void show_config(void)
{
    const char *keys[] = {"wifi_ssid", "api_base", "model"};
    con_printf(C_DIM, "wifi_ssid=%s\napi_base=%s\nmodel=%s\n", config.wifi_ssid, config.api_base, config.model);
    (void)keys;
    con_printf(C_DIM, "wifi_pass=%s api_key=%s\n", config.wifi_pass[0] ? "(set)" : "(empty)",
               config.api_key[0] ? "(set)" : "(empty)");
}

static void run_command(char *line)
{
    con_printf(C_USER, "> %s\n", line);
    if (line[0] != '/') {
        llm_chat(line);
        return;
    }
    char *cmd = strtok(line, " ");
    if (!strcmp(cmd, "/help")) {
        con_print(C_DIM, "type to chat. /set KEY VALUE, /config\n/ls /read N /write N T /mem /clear /reset\n");
    } else if (!strcmp(cmd, "/ls")) {
        int n = tool_list(ls_emit);
        if (n < 0) {
            con_printf(C_ERR, "ls: %s\n", errname(n));
        } else if (n == 0) {
            con_print(C_DIM, "(empty)\n");
        }
    } else if (!strcmp(cmd, "/read")) {
        char *name = strtok(NULL, " ");
        static char buf[TOOL_MAX_BYTES + 1];
        int n = name ? tool_read(name, buf, sizeof buf) : -22;
        if (n < 0) {
            con_printf(C_ERR, "read: %s\n", errname(n));
        } else {
            con_print(C_TEXT, buf);
            con_print(C_DIM, "\n");
        }
    } else if (!strcmp(cmd, "/write")) {
        char *name = strtok(NULL, " ");
        char *text = strtok(NULL, "");
        if (!name || !text) {
            con_print(C_ERR, "usage: /write NAME TEXT\n");
        } else {
            unescape(text);
            int n = tool_write(name, text, strlen(text));
            if (n < 0) {
                con_printf(C_ERR, "write: %s\n", errname(n));
            } else {
                con_printf(C_OK, "wrote %d bytes to %s\n", n, name);
            }
        }
    } else if (!strcmp(cmd, "/set")) {
        char *key = strtok(NULL, " ");
        char *value = strtok(NULL, "");
        if (!key || !value || !config_set(&config, key, value)) {
            con_print(C_ERR, "usage: /set KEY VALUE  keys: wifi_ssid wifi_pass\napi_base api_key model assemblyai_key\n");
        } else if (!config_save(&config)) {
            con_print(C_ERR, "could not save config\n");
        } else {
            con_printf(C_OK, "saved %s\n", key);
            llm_init(&config, confirm_line);
            if (!strcmp(key, "wifi_ssid") || !strcmp(key, "wifi_pass")) {
                net_connect(config.wifi_ssid, config.wifi_pass);
            }
        }
    } else if (!strcmp(cmd, "/config")) {
        show_config();
    } else if (!strcmp(cmd, "/mem")) {
        mem_report();
    } else if (!strcmp(cmd, "/clear")) {
        con_clear();
    } else if (!strcmp(cmd, "/reset")) {
        llm_reset();
        con_print(C_DIM, "conversation cleared\n");
    } else {
        con_print(C_ERR, "unknown command (try /help)\n");
    }
}

void app_main(void)
{
    net_init(); /* internal-RAM stack: PHY calibration writes flash */
    ESP_ERROR_CHECK(display_init());
    con_init();
    con_print(C_OK, "agent  (T-Deck)\n");
    bool store = tools_init();
    con_print(store ? C_DIM : C_ERR, store ? "storage ok (internal flash)\n" : "storage missing: run the new partition table\n");
    if (config_load(&config)) {
        if (config.wifi_ssid[0] && net_connect(config.wifi_ssid, config.wifi_pass)) {
            con_printf(C_DIM, "Wi-Fi: connecting to %s\n", config.wifi_ssid);
        }
        llm_init(&config, confirm_line);
        con_printf(C_DIM, "model: %s\n", config.model[0] ? config.model : "(not set)");
    } else {
        memset(&config, 0, sizeof config);
        strlcpy(config.api_base, "https://api.openai.com/v1", sizeof config.api_base);
        llm_init(&config, confirm_line);
        con_print(C_ERR, "not configured: use /set wifi_ssid X,\n/set wifi_pass X, /set api_key X, /set model X\n");
    }
    mem_report();
    con_print(C_DIM, "type to chat, /help for commands\n");
    trackball_init();
    xTaskCreate(keyboard_task, "kbd", 3072, NULL, 4, NULL);

    uint32_t last_up = 0, last_down = 0;
    for (;;) {
        if (has_pending) {
            static char work[INPUT_MAX + 1];
            memcpy(work, pending, sizeof work);
            has_pending = false;
            con_input("> ", input);
            run_command(work);
        }
        if (tb_up != last_up) {
            con_scroll((int)(tb_up - last_up));
            last_up = tb_up;
        }
        if (tb_down != last_down) {
            con_scroll(-(int)(tb_down - last_down));
            last_down = tb_down;
        }
        vTaskDelay(pdMS_TO_TICKS(30));
    }
}
