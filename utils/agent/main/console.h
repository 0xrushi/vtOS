/* SPDX-License-Identifier: MIT
 * Text console drawn straight to the panel, one 8-pixel row per transfer.
 * No framebuffer: 40x29 visible rows from a 150-line scrollback ring (about
 * 6 KB), plus one input row at the bottom.
 */
#pragma once

#include <stdint.h>

#define CON_COLS 40
#define CON_ROWS 29 /* visible text rows; row 29 (y=232) is the input line */

void con_init(void);
/* Append text (handles '\\n' and wraps at 40 columns), then redraw. */
void con_print(uint16_t color, const char *text);
void con_printf(uint16_t color, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
/* Scroll the view: positive = back in history, negative = toward the bottom. */
void con_scroll(int rows);
/* Redraw the input row: prompt followed by the tail of `text`. */
void con_input(const char *prompt, const char *text);
void con_clear(void);
