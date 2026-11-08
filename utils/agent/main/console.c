/* SPDX-License-Identifier: MIT */
#include "console.h"

#include "display_min.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#define LINES 150

static char text_[LINES][CON_COLS];
static uint8_t len_[LINES];
static uint16_t color_[LINES];
static int head_;  /* index of the newest line */
static int count_; /* lines in use (1..LINES) */
static int view_;  /* lines scrolled back from the bottom */
static char prompt_[8] = "> ";
static char input_[CON_COLS + 1];
static SemaphoreHandle_t lock_;

#define LOCK() xSemaphoreTakeRecursive(lock_, portMAX_DELAY)
#define UNLOCK() xSemaphoreGiveRecursive(lock_)

static void new_line(uint16_t color)
{
    head_ = (head_ + 1) % LINES;
    if (count_ < LINES) {
        count_++;
    }
    len_[head_] = 0;
    color_[head_] = color;
}

static void draw_all(void)
{
    for (int row = 0; row < CON_ROWS; ++row) {
        int back = (CON_ROWS - 1 - row) + view_;
        if (back < count_) {
            int idx = ((head_ - back) % LINES + LINES) % LINES;
            display_row((uint16_t)(row * 8), text_[idx], len_[idx], color_[idx], COLOR_BLACK);
        } else {
            display_row((uint16_t)(row * 8), "", 0, COLOR_WHITE, COLOR_BLACK);
        }
    }
}

static void draw_input(void)
{
    char line[CON_COLS + 1];
    size_t plen = strlen(prompt_);
    size_t room = CON_COLS - plen;
    size_t ilen = strlen(input_);
    const char *tail = ilen > room ? input_ + (ilen - room) : input_;
    size_t tlen = ilen > room ? room : ilen;
    memcpy(line, prompt_, plen);
    memcpy(line + plen, tail, tlen);
    display_row(CON_ROWS * 8, line, plen + tlen, COLOR_GREEN, RGB565(18, 18, 30));
}

void con_init(void)
{
    lock_ = xSemaphoreCreateRecursiveMutex();
    head_ = 0;
    count_ = 1;
    view_ = 0;
    len_[0] = 0;
    color_[0] = COLOR_WHITE;
    display_fill(COLOR_BLACK);
    draw_all();
    draw_input();
}

void con_print(uint16_t color, const char *text)
{
    LOCK();
    if (len_[head_] == 0) {
        color_[head_] = color;
    }
    for (const char *p = text; *p; ++p) {
        if (*p == '\n') {
            new_line(color);
        } else if (*p == '\r') {
            continue;
        } else {
            if (len_[head_] >= CON_COLS) {
                new_line(color);
            }
            text_[head_][len_[head_]++] = (char)*p;
        }
    }
    view_ = 0;
    draw_all();
    UNLOCK();
}

void con_printf(uint16_t color, const char *fmt, ...)
{
    char buf[192];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    con_print(color, buf);
}

void con_scroll(int rows)
{
    LOCK();
    int max = count_ > CON_ROWS ? count_ - CON_ROWS : 0;
    int next = view_ + rows;
    if (next < 0) {
        next = 0;
    }
    if (next > max) {
        next = max;
    }
    if (next != view_) {
        view_ = next;
        draw_all();
    }
    UNLOCK();
}

void con_input(const char *prompt, const char *text)
{
    LOCK();
    snprintf(prompt_, sizeof prompt_, "%s", prompt);
    snprintf(input_, sizeof input_, "%s", text);
    draw_input();
    UNLOCK();
}

void con_clear(void)
{
    LOCK();
    head_ = 0;
    count_ = 1;
    view_ = 0;
    len_[0] = 0;
    draw_all();
    UNLOCK();
}
