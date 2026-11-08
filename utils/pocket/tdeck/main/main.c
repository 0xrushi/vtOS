/* SPDX-License-Identifier: MIT
 * PocketJS / Pocket Shell on the LilyGO T-Deck (ESP32-S3), dual-boot slot 3.
 *
 * Host contract (pocket.host.json, id tdeck-home): the launcher lays out a
 * 400x240 primary surface that stays off screen, and draws its picture on a
 * 320x240 auxiliary surface, which is the panel. Same shape as the Tab5
 * firmware in pocket-m5.
 *
 * Board bring-up:
 *   GPIO10 = peripheral power, GPIO42 = backlight (pulse dimmer, high = full)
 *   ST7789 320x240 on SPI2: SCK 40, MOSI 41, MISO 38, CS 12, DC 11
 *   I2C (SDA 18, SCL 8): keyboard 0x55, GT911 touch 0x5D or 0x14
 *   BOOT button GPIO0
 * Input: touch contacts on the auxiliary surface, plus buttons: w/a/s/d or
 * i/j/k/l = D-pad, Enter = cross, Backspace = circle, Space = start,
 * BOOT = select.
 * Press RST to return to the loader (this image is never marked valid, so the
 * bootloader rolls back to the loader on the next reset).
 */
#include "pocket_profile.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_st7789.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/task.h"
#include "pocket/display_esp_lcd.h"
#include "pocket/guest_idf.h"
#include "pocket/http_esp.h"
#include "pocket/worker_esp.h"
#include "pocket/worker_ui.h"
#include "pocket_quickjs_abi.h"
#include "tdeck_wifi.h"
#include "pocket/package_source.h"
#include "pocket/runner.h"
#include "pocketjs_package_app.h"
#include "pocket_spec.h"
#include "tdeck_system.h"
#include <assert.h>
#include <inttypes.h>
#include <string.h>

#define TAG "pocket_tdeck"

#define PIN_POWERON 10
#define PIN_BACKLIGHT 42
#define PIN_SD_CS 39
#define PIN_LORA_CS 9
#define PIN_SCK 40
#define PIN_MOSI 41
#define PIN_MISO 38
#define PIN_TFT_CS 12
#define PIN_TFT_DC 11
#define PIN_BOOT 0
#define PIN_I2C_SDA 18
#define PIN_I2C_SCL 8
#define KBD_ADDR 0x55
#define KEY_HOLD_US 140000
#define TOUCH_RELEASE_US 70000

#define PANEL_W 320
#define PANEL_H 240
#define PRIMARY_W 400
#define PRIMARY_H 240
#define STRIP_ROWS 16
#define STRIP_COUNT 2

/* QuickJS runs on the calling task during boot and needs a deep C stack, far
 * more than internal RAM can spare: run everything on a PSRAM stack. */
#define POCKET_TASK_STACK (384 * 1024)

static void require_status(int32_t status) {
  assert(status == POCKET_STATUS_OK);
}

static void board_power(void) {
  const gpio_config_t out = {
      .pin_bit_mask = (1ULL << PIN_POWERON) | (1ULL << PIN_BACKLIGHT) |
                      (1ULL << PIN_SD_CS) | (1ULL << PIN_LORA_CS),
      .mode = GPIO_MODE_OUTPUT,
  };
  gpio_config(&out);
  gpio_set_level(PIN_POWERON, 1);
  gpio_set_level(PIN_SD_CS, 1);
  gpio_set_level(PIN_LORA_CS, 1);
  gpio_set_level(PIN_BACKLIGHT, 0);
  esp_rom_delay_us(100 * 1000);
  const gpio_config_t in = {
      .pin_bit_mask = 1ULL << PIN_BOOT,
      .mode = GPIO_MODE_INPUT,
      .pull_up_en = GPIO_PULLUP_ENABLE,
  };
  gpio_config(&in);
}

static esp_err_t panel_init(esp_lcd_panel_io_handle_t *io_out,
                            esp_lcd_panel_handle_t *panel_out) {
  const spi_bus_config_t bus = {
      .mosi_io_num = PIN_MOSI,
      .miso_io_num = PIN_MISO,
      .sclk_io_num = PIN_SCK,
      .quadwp_io_num = -1,
      .quadhd_io_num = -1,
      .max_transfer_sz = PANEL_W * STRIP_ROWS * 2,
  };
  esp_err_t err = spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO);
  if (err != ESP_OK)
    return err;
  const esp_lcd_panel_io_spi_config_t io_config = {
      .cs_gpio_num = PIN_TFT_CS,
      .dc_gpio_num = PIN_TFT_DC,
      .spi_mode = 0,
      .pclk_hz = 80 * 1000 * 1000,
      .trans_queue_depth = 4,
      .lcd_cmd_bits = 8,
      .lcd_param_bits = 8,
  };
  err = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI2_HOST,
                                 &io_config, io_out);
  if (err != ESP_OK)
    return err;
  const esp_lcd_panel_dev_config_t panel_config = {
      .reset_gpio_num = -1,
      .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
      .bits_per_pixel = 16,
  };
  err = esp_lcd_new_panel_st7789(*io_out, &panel_config, panel_out);
  if (err != ESP_OK)
    return err;
  esp_lcd_panel_reset(*panel_out);
  esp_lcd_panel_init(*panel_out);
  esp_lcd_panel_invert_color(*panel_out, true);
  /* landscape 320x240, same as vtOS rotation=1 (MADCTL 0x60) */
  esp_lcd_panel_swap_xy(*panel_out, true);
  esp_lcd_panel_mirror(*panel_out, true, false);
  esp_lcd_panel_disp_on_off(*panel_out, true);
  return ESP_OK;
}

/* ---- the display the panel is: auxiliary surface ------------------------- */

static const pocket_display_output_v1 *lcd_base;
static pocket_display_output_v1 lcd_aux;

static int32_t lcd_aux_descriptor(void *context,
                                  pocket_display_descriptor *out) {
  int32_t status = lcd_base->descriptor(context, out);
  if (status == POCKET_STATUS_OK)
    out->role = POCKET_DISPLAY_ROLE_AUXILIARY;
  return status;
}

/* ---- the off-screen primary surface -------------------------------------- */

typedef struct {
  uint16_t *pixels;
  pocket_rect region;
  pocket_surface surface;
  uint64_t next;
} blank_t;

static int32_t blank_descriptor(void *context,
                                pocket_display_descriptor *out) {
  (void)context;
  if (!out || out->struct_size < sizeof(*out))
    return POCKET_STATUS_INVALID;
  *out = (pocket_display_descriptor){
      .struct_size = sizeof(*out),
      .role = POCKET_DISPLAY_ROLE_PRIMARY,
      .width = PRIMARY_W,
      .height = PRIMARY_H,
      .pixel_format = POCKET_PIXEL_FORMAT_RGB565,
      .stride_bytes = (size_t)PRIMARY_W * 2U,
      .target_kind = POCKET_TARGET_CPU_VISIBLE,
      .flags = POCKET_DISPLAY_FRAME_STORAGE | POCKET_DISPLAY_PARTIAL_REFRESH,
      .refresh_width = 1,
      .refresh_height = 1,
      .window_x_alignment = 1,
      .window_y_alignment = 1};
  return POCKET_STATUS_OK;
}

static int32_t blank_acquire(void *context, const pocket_rect *region,
                             pocket_target *out) {
  blank_t *blank = context;
  if (!blank || !region || region->struct_size < sizeof(*region) || !out ||
      out->struct_size < sizeof(*out) || region->x ||
      region->width != PRIMARY_W || !region->height ||
      region->y > PRIMARY_H - region->height)
    return POCKET_STATUS_INVALID;
  blank->region = *region;
  blank->surface = (pocket_surface){
      .struct_size = sizeof(blank->surface),
      .pixels = (uint8_t *)(blank->pixels + (size_t)region->y * PRIMARY_W),
      .byte_length = (size_t)PRIMARY_W * region->height * 2U,
      .region = &blank->region,
      .stride_bytes = (size_t)PRIMARY_W * 2U,
      .pixel_format = POCKET_PIXEL_FORMAT_RGB565};
  *out = (pocket_target){.struct_size = sizeof(*out),
                         .kind = POCKET_TARGET_CPU_VISIBLE,
                         .region = &blank->region,
                         .pixel_format = POCKET_PIXEL_FORMAT_RGB565,
                         .surface = &blank->surface};
  return POCKET_STATUS_OK;
}

static int32_t blank_flush(void *context, const pocket_rect *regions,
                           size_t count, uint64_t *token) {
  blank_t *blank = context;
  (void)regions;
  if (!blank || !count || !token)
    return POCKET_STATUS_INVALID;
  *token = ++blank->next;
  return POCKET_STATUS_OK;
}

static int32_t blank_wait(void *context, uint64_t token) {
  (void)context;
  (void)token;
  return POCKET_STATUS_OK;
}

static int32_t blank_identity(void *context, pocket_buffer_identity *out) {
  if (!context || !out || out->struct_size < sizeof(*out))
    return POCKET_STATUS_INVALID;
  *out = (pocket_buffer_identity){
      .struct_size = sizeof(*out), .buffer = 2, .generation = 1};
  return POCKET_STATUS_OK;
}

static const pocket_display_output_v1 BLANK = {.struct_size = sizeof(BLANK),
                                               .descriptor = blank_descriptor,
                                               .acquire = blank_acquire,
                                               .flush = blank_flush,
                                               .wait = blank_wait,
                                               .frame_identity =
                                                   blank_identity};

/* ---- input: keyboard + BOOT buttons, GT911 touch contacts --------------- */

typedef struct {
  i2c_master_dev_handle_t kbd;
  i2c_master_dev_handle_t touch;
  uint32_t held;      /* trackball direction held */
  int64_t expire_us;
  uint32_t last_edges[4];
  bool click_down;
  int64_t click_us;
  bool click_long;
  uint32_t click_pending; /* a short click, reported as CIRCLE for a moment */
  int64_t click_expire_us;
  bool touching;
  int64_t touch_us;
  float touch_x, touch_y;
  int64_t log_us;
} input_state;

static esp_err_t i2c_init(input_state *st) {
  const i2c_master_bus_config_t bus_cfg = {
      .clk_source = I2C_CLK_SRC_DEFAULT,
      .i2c_port = I2C_NUM_0,
      .sda_io_num = PIN_I2C_SDA,
      .scl_io_num = PIN_I2C_SCL,
      .glitch_ignore_cnt = 7,
      .flags.enable_internal_pullup = true,
  };
  i2c_master_bus_handle_t bus;
  esp_err_t err = i2c_new_master_bus(&bus_cfg, &bus);
  if (err != ESP_OK)
    return err;
  i2c_device_config_t dev_cfg = {
      .dev_addr_length = I2C_ADDR_BIT_LEN_7,
      .device_address = KBD_ADDR,
      .scl_speed_hz = 400000,
  };
  err = i2c_master_bus_add_device(bus, &dev_cfg, &st->kbd);
  if (err != ESP_OK)
    ESP_LOGW(TAG, "keyboard add failed: %s", esp_err_to_name(err));
  /* GT911: 0x5D or 0x14 depending on the INT level at reset. */
  static const uint8_t addrs[] = {0x5D, 0x14};
  for (size_t i = 0; i < sizeof(addrs); i++) {
    if (i2c_master_probe(bus, addrs[i], 50) == ESP_OK) {
      dev_cfg.device_address = addrs[i];
      if (i2c_master_bus_add_device(bus, &dev_cfg, &st->touch) == ESP_OK) {
        ESP_LOGI(TAG, "GT911 touch at 0x%02x", addrs[i]);
        break;
      }
    }
  }
  if (!st->touch)
    ESP_LOGW(TAG, "no touch controller found");
  return ESP_OK;
}

/* ---- trackball: four pulse outputs (one edge per notch) + the click ------- */

#define PIN_TB_UP 3
#define PIN_TB_DOWN 15
#define PIN_TB_LEFT 1
#define PIN_TB_RIGHT 2
#define TB_HOLD_US 120000
#define CLICK_LONG_US 700000

static volatile uint32_t tb_edges[4]; /* up, down, left, right */

static void IRAM_ATTR tb_isr(void *arg) {
  tb_edges[(uintptr_t)arg]++;
}

static void trackball_init(void) {
  static const int pins[4] = {PIN_TB_UP, PIN_TB_DOWN, PIN_TB_LEFT, PIN_TB_RIGHT};
  gpio_install_isr_service(0);
  for (uintptr_t i = 0; i < 4; i++) {
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

/* Raw GT911 coordinates -> panel pixels (320x240 landscape). Calibrated on
 * the device; adjust TOUCH_SWAP/TOUCH_FLIP_* if taps land mirrored. */
#ifndef TOUCH_SWAP
#define TOUCH_SWAP 0
#endif
#ifndef TOUCH_FLIP_X
#define TOUCH_FLIP_X 0
#endif
#ifndef TOUCH_FLIP_Y
#define TOUCH_FLIP_Y 0
#endif

static void touch_poll(input_state *st, int64_t now) {
  if (!st->touch)
    return;
  uint8_t reg[2] = {0x81, 0x4E};
  uint8_t status = 0;
  if (i2c_master_transmit_receive(st->touch, reg, 2, &status, 1, 5) != ESP_OK)
    return;
  if (status & 0x80) {
    const uint8_t clear[3] = {0x81, 0x4E, 0x00};
    const uint8_t points = status & 0x0F;
    if (points > 0 && points < 6) {
      uint8_t pt[2] = {0x81, 0x50};
      uint8_t d[8] = {0};
      if (i2c_master_transmit_receive(st->touch, pt, 2, d, 8, 5) == ESP_OK) {
        /* Measured on the device: the point block arrives one byte late
         * (d[0] is not the track id), so x/y sit at d[2..5]. */
        uint32_t rx = (uint32_t)d[2] | ((uint32_t)d[3] << 8);
        uint32_t ry = (uint32_t)d[4] | ((uint32_t)d[5] << 8);
        float x, y;
#if TOUCH_SWAP
        x = (float)ry;
        y = (float)rx;
#else
        x = (float)rx;
        y = (float)ry;
#endif
#if TOUCH_FLIP_X
        x = (float)(PANEL_W - 1) - x;
#endif
#if TOUCH_FLIP_Y
        y = (float)(PANEL_H - 1) - y;
#endif
        if (x < 0) x = 0;
        if (y < 0) y = 0;
        if (x > PANEL_W - 1) x = PANEL_W - 1;
        if (y > PANEL_H - 1) y = PANEL_H - 1;
        st->touch_x = x;
        st->touch_y = y;
        st->touching = true;
        st->touch_us = now;
        if (now - st->log_us > 250000) {
          st->log_us = now;
          ESP_LOGI(TAG, "touch raw=(%u,%u) -> (%.0f,%.0f) bytes %02x %02x %02x %02x %02x %02x",
                   (unsigned)rx, (unsigned)ry, x, y, d[0], d[1], d[2], d[3], d[4], d[5]);
        }
      }
    } else {
      st->touching = false;
    }
    i2c_master_transmit(st->touch, clear, sizeof(clear), 5);
  } else if (st->touching && now - st->touch_us > TOUCH_RELEASE_US) {
    st->touching = false;
  }
}

static uint32_t input_classes(void *context) {
  (void)context;
  return POCKET_INPUT_CLASS_BUTTONS | POCKET_INPUT_CLASS_CONTACT |
         POCKET_INPUT_CLASS_TEXT;
}

#define TEXT_EVENTS 16
#define TEXT_BYTES 64

/* Append a key down/up pair named like KeyboardEvent.key. */
static size_t key_pair(pocket_text_event *events, size_t count, const char *name) {
  for (uint32_t kind = POCKET_TEXT_KEY_DOWN; kind <= POCKET_TEXT_KEY_UP; kind++)
    events[count++] = (pocket_text_event){.struct_size = sizeof(pocket_text_event),
                                          .kind = kind,
                                          .text = (const uint8_t *)name,
                                          .byte_length = strlen(name)};
  return count;
}

static int32_t input_poll(void *context, pocket_input_frame_builder *builder) {
  input_state *st = context;
  const int64_t now = esp_timer_get_time();

  /* Keyboard: every press becomes a text event. Characters commit; named keys
   * are a key pair. Up to four presses per tick. */
  pocket_text_event text[TEXT_EVENTS];
  char bytes[TEXT_BYTES];
  size_t text_count = 0, used = 0;
  for (int i = 0; st->kbd && i < 4 && text_count + 2 <= TEXT_EVENTS; i++) {
    uint8_t key = 0;
    if (i2c_master_receive(st->kbd, &key, 1, 5) != ESP_OK || !key)
      break;
    if (key == '\r' || key == '\n')
      text_count = key_pair(text, text_count, "Enter");
    else if (key == 0x08 || key == 0x7f)
      text_count = key_pair(text, text_count, "Backspace");
    else if (key == '\t')
      text_count = key_pair(text, text_count, "Tab");
    else if (key == 0x1b)
      text_count = key_pair(text, text_count, "Escape");
    else if (key >= 32 && key < 127 && used < sizeof bytes) {
      bytes[used] = (char)key;
      text[text_count++] = (pocket_text_event){.struct_size = sizeof(pocket_text_event),
                                               .kind = POCKET_TEXT_COMMIT,
                                               .text = (const uint8_t *)&bytes[used],
                                               .byte_length = 1};
      used++;
    }
  }

  /* Trackball: each notch holds a direction for a moment. */
  uint32_t buttons = 0;
  static const uint32_t DIRS[4] = {POCKET_BUTTON_UP, POCKET_BUTTON_DOWN,
                                   POCKET_BUTTON_LEFT, POCKET_BUTTON_RIGHT};
  for (int i = 0; i < 4; i++) {
    uint32_t edges = tb_edges[i];
    if (edges != st->last_edges[i]) {
      st->last_edges[i] = edges;
      st->held = DIRS[i];
      st->expire_us = now + TB_HOLD_US;
    }
  }
  if (st->held && now > st->expire_us)
    st->held = 0;
  buttons |= st->held;

  /* Click (the BOOT pin): a short press activates (circle); a long press is
   * select, which summons the launcher from an app. */
  bool down = gpio_get_level(PIN_BOOT) == 0;
  if (down && !st->click_down) {
    st->click_down = true;
    st->click_us = now;
    st->click_long = false;
  } else if (down && !st->click_long && now - st->click_us > CLICK_LONG_US) {
    st->click_long = true;
  } else if (!down && st->click_down) {
    st->click_down = false;
    if (!st->click_long) {
      st->click_pending = 1;
      st->click_expire_us = now + 100000;
    }
  }
  if (st->click_long && down)
    buttons |= POCKET_BUTTON_SELECT;
  if (st->click_pending) {
    if (now > st->click_expire_us)
      st->click_pending = 0;
    else
      buttons |= POCKET_BUTTON_CIRCLE;
  }

  touch_poll(st, now);
  pocket_contact contact = {.struct_size = sizeof(contact),
                            .surface = 1, /* auxiliary */
                            .id = 0,
                            .x = st->touch_x,
                            .y = st->touch_y};
  pocket_input_frame frame = {.struct_size = sizeof(frame),
                              .buttons = buttons,
                              .contacts = st->touching ? &contact : NULL,
                              .contact_count = st->touching ? 1 : 0,
                              .text = text_count ? text : NULL,
                              .text_count = text_count};
  return builder->append(builder->context, &frame);
}

static const pocket_input_source_v1 INPUT = {sizeof(INPUT), input_classes,
                                             input_poll};

static input_state *input_shared;

/* ---- the launcher's operations -------------------------------------------- */

static char launch_output[128];

static int32_t applications(void *context, uint16_t op,
                            const pocket_op_arg *args, size_t length,
                            pocket_result *out) {
  (void)context;
  out->tag = POCKET_RESULT_NONE;
  out->integer = 0;
  out->real = 0;
  out->bytes = NULL;
  out->byte_length = 0;
  switch (op) {
  case POCKET_OP_appTable: {
    const char *table = tdeck_app_table();
    out->tag = POCKET_RESULT_BYTES;
    out->bytes = (const uint8_t *)table;
    out->byte_length = strlen(table);
    return POCKET_STATUS_OK;
  }
  case POCKET_OP_appLaunch:
    if (!args || length < sizeof *args || args->struct_size < sizeof *args ||
        args->tag != POCKET_ARG_BYTES ||
        args->byte_length >= sizeof launch_output)
      return POCKET_STATUS_INVALID;
    memcpy(launch_output, args->bytes, args->byte_length);
    launch_output[args->byte_length] = 0;
    out->tag = POCKET_RESULT_I64;
    out->integer = tdeck_launch(launch_output) ? 1 : 0;
    return POCKET_STATUS_OK;
  case POCKET_OP_appShot:
    out->tag = POCKET_RESULT_I64;
    out->integer = -1;
    return POCKET_STATUS_OK;
  default:
    return POCKET_STATUS_UNSUPPORTED;
  }
}

static const pocket_op_provider_v1 APPLICATIONS = {sizeof APPLICATIONS,
                                                   applications};
static const uint16_t APPLICATION_OPS[] = {
    POCKET_OP_appTable, POCKET_OP_appLaunch, POCKET_OP_appShot};

/* ---- one running guest ------------------------------------------------------ */

typedef struct {
  pocket_runtime *runtime;
  pocket_guest_idf *guest;
  pocket_runner_t *runner;
  pocket_memory_package package;
  pocket_worker_esp *worker;
  pocket_worker_ui *worker_ui;
} guest_t;

static guest_t running;
static pocket_display_esp_lcd *panel_display;
static blank_t blank;
static size_t frame_budget;
static TaskHandle_t app_task;
static volatile bool switch_signalled;
static volatile bool guest_failed;

/* ---- the worker domain: one `http` node over Wi-Fi ------------------------- */

#define WORKER_HEAP (1024U * 1024U)
#define WORKER_STACK (24U * 1024U)
#define WORKER_STACK_LIMIT (16U * 1024U)
#define WORKER_PRIORITY 3U
#define HTTP_PRIORITY 3U

static pocket_worker_node worker_nodes[1];
static pocket_worker_esp_config worker_config;
static volatile bool worker_failed;

static bool network_online(void *context) {
  (void)context;
  return tdeck_wifi_online();
}

static bool network_clock_wait(void *context, uint32_t ms) {
  (void)context;
  return tdeck_wifi_clock_wait(ms);
}

static void start_worker_host(void) {
  const pocket_http_esp_config http = {
      .struct_size = sizeof(http),
      .online = network_online,
      .clock_wait = network_clock_wait,
      .memory_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT,
      .task_stack_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT,
      .task_core = 0,
      .task_priority = HTTP_PRIORITY,
      .companion = NULL, /* no Pocket Link: companion.pocket resolves by DNS */
  };
  pocket_http_esp *driver = pocket_http_esp_create(&http);
  assert(driver != NULL);
  worker_nodes[0] = (pocket_worker_node){
      .struct_size = sizeof(worker_nodes[0]),
      .kind = POCKET_WORKER_HTTP,
      .name = "http",
      .driver = pocket_http_esp_driver(driver),
  };
  worker_config = (pocket_worker_esp_config){
      .struct_size = sizeof(worker_config),
      .nodes = worker_nodes,
      .node_count = sizeof worker_nodes / sizeof worker_nodes[0],
      .heap_limit = WORKER_HEAP,
      .stack_limit = WORKER_STACK_LIMIT,
      .task_stack_size = WORKER_STACK,
      .task_core = 0,
      .task_priority = WORKER_PRIORITY,
      .heap_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT,
      .task_stack_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT,
  };
}

static void interrupt_guest(void *context) {
  (void)context;
  if (running.guest)
    pocket_guest_idf_interrupt(running.guest);
}

static void before_tick(void *context) {
  (void)context;
  /* The frame reads the worker channels latched here. A worker fault or a
   * failed latch ends the app. */
  if (running.worker != NULL && !worker_failed &&
      (pocket_worker_esp_failed(running.worker) ||
       pocket_worker_ui_latch(running.worker_ui) != POCKET_WORKER_OK)) {
    worker_failed = true;
    guest_failed = true;
    xTaskNotifyGive(app_task);
  }
  if (tdeck_switch_pending() && !switch_signalled) {
    switch_signalled = true;
    xTaskNotifyGive(app_task);
  }
}

static void after_tick(int32_t status, void *context) {
  (void)context;
  if (status != POCKET_STATUS_OK && !guest_failed) {
    guest_failed = true;
    xTaskNotifyGive(app_task);
  }
}

static void stop_guest(void) {
  if (running.runner)
    pocket_runner_stop(running.runner);
  /* The bridge borrows the realm and the worker core; native IO ends before
   * the package bytes holding the worker source go. */
  if (running.worker_ui)
    pocket_worker_ui_detach(running.worker_ui);
  if (running.worker)
    while (pocket_worker_esp_stop(running.worker, 1000) == ESP_ERR_TIMEOUT)
      ESP_LOGW(TAG, "waiting for the worker's native IO to stop");
  if (running.runtime)
    pocket_runtime_destroy(running.runtime);
  if (running.guest)
    pocket_guest_idf_destroy(running.guest);
  memset(&running, 0, sizeof running);
  /* The next guest draws a picture the panel has never seen. */
  pocket_display_esp_lcd_invalidate(panel_display);
}

static int32_t boot_app(tdeck_app_t *app) {
  size_t size = 0;
  const uint8_t *bytes = tdeck_app_bytes(app, &size);

  pocket_runtime_config config = {
      .struct_size = sizeof(config),
      .host_abi = pocket_profile.host_abi,
      .tick_hz = pocket_profile.tick_hz,
      .heap_limit = 6 * 1024 * 1024,
      .frame_buffer_budget = frame_budget,
      .profile = pocket_profile.facts};
  memcpy(config.profile_hash, pocket_profile.profile_hash, 32);
  running.runtime = pocket_runtime_create(&config);
  if (!running.runtime)
    return POCKET_STATUS_FAILED;

  const pocket_guest_idf_config guest_config = {5 * 1024 * 1024, 256 * 1024,
                                                true};
  running.guest = pocket_guest_idf_create(&guest_config);
  if (!running.guest)
    return POCKET_STATUS_FAILED;
  int32_t status = pocket_runtime_set_guest(
      running.runtime, pocket_guest_idf_vtable(), running.guest);
  running.package =
      (pocket_memory_package){bytes, size, POCKET_MEMORY_PACKAGE_VERIFIED};
  if (status == POCKET_STATUS_OK)
    status = pocket_runtime_set_package_source(
        running.runtime, pocket_memory_package_vtable(), &running.package);
  if (status == POCKET_STATUS_OK)
    status = pocket_runtime_add_input_source(running.runtime, &INPUT, input_shared);
  /* The off-screen primary registers first and takes its buffer. */
  if (status == POCKET_STATUS_OK)
    status = pocket_runtime_add_display(running.runtime, &BLANK, &blank,
                                        POCKET_RENDERER_SOFTWARE);
  if (status == POCKET_STATUS_OK)
    status = pocket_runtime_add_display(running.runtime, &lcd_aux,
                                        panel_display, POCKET_RENDERER_SOFTWARE);
  if (status == POCKET_STATUS_OK)
    status = pocket_runtime_add_op_provider(
        running.runtime, &APPLICATIONS, NULL, APPLICATION_OPS,
        sizeof APPLICATION_OPS / sizeof *APPLICATION_OPS);
  if (status == POCKET_STATUS_OK &&
      tdeck_bind(pocket_guest_idf_context(running.guest), app) != ESP_OK)
    status = POCKET_STATUS_FAILED;
  /* The worker declares its channels before the realm evaluates and runs its
   * first turn after; the bridge mirrors the channels into the realm. */
  worker_failed = false;
  const char *worker_source = NULL;
  size_t worker_size = 0;
  const uint8_t *worker_bytecode = NULL;
  size_t worker_bytecode_size = 0;
  if (!tdeck_app_worker(app, &worker_source, &worker_size))
    worker_source = NULL;
  if (!tdeck_app_worker_bytecode(app, &worker_bytecode, &worker_bytecode_size))
    worker_bytecode = NULL;
  if (status == POCKET_STATUS_OK && (worker_source || worker_bytecode)) {
    static uint32_t generation;
    pocket_worker_esp_config config = worker_config;
    config.source = worker_source;
    config.source_size = worker_size;
    config.bytecode = worker_bytecode;
    config.bytecode_size = worker_bytecode_size;
    config.engine_abi = POCKET_QUICKJS_ENGINE_ABI_ESP_IDF_QUICKJS_NG_0_14_0;
    config.generation = ++generation;
    config.defer_turns = true;
    esp_err_t started = pocket_worker_esp_start(&config, &running.worker);
    if (started == ESP_OK)
      running.worker_ui = pocket_worker_ui_attach(
          pocket_guest_idf_context(running.guest),
          pocket_worker_esp_core(running.worker));
    if (started != ESP_OK || running.worker_ui == NULL) {
      ESP_LOGE(TAG, "worker: %s",
               started != ESP_OK ? esp_err_to_name(started) : "bridge failed");
      status = POCKET_STATUS_FAILED;
    }
  }
  if (status == POCKET_STATUS_OK) {
    const int64_t started = esp_timer_get_time();
    status = pocket_runtime_boot(running.runtime);
    if (status != POCKET_STATUS_OK)
      ESP_LOGE(TAG, "boot status=%d: %s", (int)status,
               pocket_guest_idf_error(running.guest));
    else
      ESP_LOGI(TAG, "booted in %.1f ms",
               (esp_timer_get_time() - started) / 1000.0);
  }
  if (status != POCKET_STATUS_OK)
    return status;
  if (running.worker != NULL)
    pocket_worker_esp_resume(running.worker);

  pocket_runner_config_t runner_config;
  pocket_runner_config_defaults(&runner_config);
  runner_config.interrupt = interrupt_guest;
  runner_config.before_tick = before_tick;
  runner_config.after_tick = after_tick;
  runner_config.task_stack_bytes = POCKET_TASK_STACK;
  runner_config.task_stack_caps = MALLOC_CAP_SPIRAM;
  switch_signalled = false;
  guest_failed = false;
  return pocket_runner_start(running.runtime, &runner_config,
                             &running.runner) == ESP_OK
             ? POCKET_STATUS_OK
             : POCKET_STATUS_FAILED;
}

/* Boot `app`; on failure tear the guest down. */
static int32_t start_app(tdeck_app_t *app) {
  int32_t status = boot_app(app);
  if (status != POCKET_STATUS_OK) {
    stop_guest();
    tdeck_unbind();
  }
  return status;
}

/* Replace the running guest with the one the launcher asked for, or with the
 * launcher after a failure. Returns the app now running. */
static tdeck_app_t *switch_guest(tdeck_app_t *app, bool failed) {
  stop_guest();
  tdeck_unbind();
  tdeck_app_free(app);
  char slot[17] = {0};
  bool launcher = true;
  if (failed || !tdeck_take_switch(slot, &launcher))
    launcher = true;
  tdeck_app_t *next = NULL;
  int32_t status = POCKET_STATUS_FAILED;
  if (!launcher) {
    if (tdeck_load_slot(slot, &next) == ESP_OK) {
      status = start_app(next);
      if (status != POCKET_STATUS_OK) {
        tdeck_app_free(next);
        next = NULL;
      }
    }
    if (status != POCKET_STATUS_OK) {
      ESP_LOGE(TAG, "slot %s did not start", slot);
      tdeck_report_failure("the app did not start");
    }
  }
  if (status != POCKET_STATUS_OK) {
    ESP_ERROR_CHECK(tdeck_load_launcher(pocketjs_package_app.data,
                                        pocketjs_package_app.size, &next));
    if (start_app(next) != POCKET_STATUS_OK)
      abort();
  }
  return next;
}

static void pocket_main(void *arg) {
  (void)arg;
  app_task = xTaskGetCurrentTaskHandle();

  board_power();
  esp_lcd_panel_io_handle_t io = NULL;
  esp_lcd_panel_handle_t panel = NULL;
  ESP_ERROR_CHECK(panel_init(&io, &panel));
  tdeck_sd_mount();
  tdeck_store_start();
  tdeck_wifi_connect_from_card("/sd/pocket/wifi.txt");
  start_worker_host();

  /* The off-screen primary takes its buffer from the frame buffer budget, so
   * the budget covers it plus the panel's strips. */
  blank.pixels =
      heap_caps_malloc((size_t)PRIMARY_W * PRIMARY_H * 2U, MALLOC_CAP_SPIRAM);
  assert(blank.pixels);
  const size_t strips_bytes = (size_t)PANEL_W * STRIP_ROWS * 2U * STRIP_COUNT;
  frame_budget = strips_bytes + (size_t)PRIMARY_W * PRIMARY_H * 2U;

  pocket_display_esp_lcd_config display_config = {
      .struct_size = sizeof(display_config),
      .panel = panel,
      .io = io,
      .width = PANEL_W,
      .height = PANEL_H,
      .strip_rows = STRIP_ROWS,
      .buffer_count = STRIP_COUNT,
      .frame_buffer_budget = strips_bytes,
      .byte_swap = true,
  };
  panel_display = pocket_display_esp_lcd_create(&display_config);
  assert(panel_display);
  lcd_base = pocket_display_esp_lcd_vtable();
  lcd_aux = *lcd_base;
  lcd_aux.descriptor = lcd_aux_descriptor;

  static input_state input_hw;
  if (i2c_init(&input_hw) != ESP_OK)
    ESP_LOGW(TAG, "i2c init failed; BOOT button only");
  input_shared = &input_hw;
  trackball_init();

  tdeck_app_t *app = NULL;
  ESP_ERROR_CHECK(tdeck_load_launcher(pocketjs_package_app.data,
                                      pocketjs_package_app.size, &app));
  if (start_app(app) != POCKET_STATUS_OK) {
    ESP_LOGE(TAG, "launcher did not start");
    abort();
  }

  /* Panel is drawn by the first tick; light the backlight after it. */
  vTaskDelay(pdMS_TO_TICKS(500));
  gpio_set_level(PIN_BACKLIGHT, 1);
  ESP_LOGI(TAG, "running; free PSRAM %u, internal %u",
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));

  for (;;) {
    if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(5000)) > 0 ||
        tdeck_switch_pending() || guest_failed) {
      const bool failed = guest_failed;
      app = switch_guest(app, failed);
      continue;
    }
    pocket_runner_stats_t stats = {.struct_size = sizeof(stats)};
    if (running.runner &&
        pocket_runner_stats(running.runner, &stats) == ESP_OK)
      ESP_LOGI(TAG, "frames=%" PRIu32 " skipped=%" PRIu32 " errors=%" PRIu32
                    " max_frame=%" PRIu32 " us",
               stats.frames, stats.frames_skipped, stats.frame_errors,
               stats.max_frame_us);
  }
}

void app_main(void) {
  /* The radio's flash use (PHY calibration, NVS) needs an internal-RAM stack. */
  tdeck_wifi_init();
  TaskHandle_t task = NULL;
  BaseType_t ok = xTaskCreatePinnedToCoreWithCaps(
      pocket_main, "pocket", POCKET_TASK_STACK, NULL, 5, &task, 1,
      MALLOC_CAP_SPIRAM);
  if (ok != pdPASS)
    ESP_LOGE(TAG, "could not create the pocket task (PSRAM stack)");
}
