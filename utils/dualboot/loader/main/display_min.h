/*
 * display_min -- minimal ST7789 driver (T-Deck, 320x240 RGB565).
 * Copyright (c) 2026 8bitmcu
 * License: MIT
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#define DISPLAY_WIDTH  320
#define DISPLAY_HEIGHT 240

esp_err_t display_init(void);
void display_fill(uint16_t color);
void display_rect(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color);
void display_text(uint16_t x, uint16_t y, const char *str,
                  uint16_t fg, uint16_t bg, uint8_t scale);

/* RGB565 helpers */
#define RGB565(r, g, b) \
    (uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3))

#define COLOR_BLACK 0x0000
#define COLOR_WHITE 0xFFFF
#define COLOR_CYAN  RGB565(0, 200, 230)
#define COLOR_GREEN RGB565(0, 210, 80)
#define COLOR_RED   RGB565(230, 60, 60)
#define COLOR_DARK  RGB565(18, 18, 30)
#define COLOR_DIM   RGB565(90, 90, 110)
