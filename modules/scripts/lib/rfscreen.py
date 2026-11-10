"""Pixel spectrum and waterfall for the T-Deck's RGB565 display."""

import framebuf

from pixelui import (ACCENT, AMBER, BG, CHIP, DIM, EDGE, GRID, HEADER, PLOT, TEXT,
                     large, pixel as _pixel, single_line, small, text as _text, visible)
from rfscan import HISTORY, MAX_POINTS, format_mhz

HOLD = _pixel(0xB38D50)
HEAT = tuple(_pixel(rgb) for rgb in (
    0x1525A1, 0x1934C7, 0x2549EB, 0x2864FF, 0x2682FF, 0x229FEF,
    0x16BDD5, 0x10D3A6, 0x38E076, 0x7CE650, 0xBFE83D, 0xF4E43A,
    0xFFCB37, 0xFF9A34, 0xFF6637, 0xF43642,
))


class Screen:
    # Coordinates are relative to the application area below the status bar.
    PLOT_X = 40
    PLOT_Y = 56
    PLOT_H = 53
    WATER_Y = 116
    FOOT_H = 32

    def __init__(self, env):
        self.env = env
        self.width = env.screen_width
        self.top = env.status_height
        self.bottom_h = env.screen_height - self.top - self.WATER_Y
        self.plot_w = self.width - self.PLOT_X - 9
        self.bins = min(MAX_POINTS, self.plot_w - 2)
        self.dirty = True
        # Both bands are rendered and sent in turn using one scratch buffer.
        self.buf = bytearray(self.width * max(self.WATER_Y, self.bottom_h) * 2)
        self.upper = framebuf.FrameBuffer(self.buf, self.width, self.WATER_Y, framebuf.RGB565)
        self.lower = framebuf.FrameBuffer(self.buf, self.width, self.bottom_h, framebuf.RGB565)
        self.edges = [self.PLOT_X + 1 + i * (self.plot_w - 2) // self.bins
                      for i in range(self.bins + 1)]

    def visible(self):
        return visible(self.env)

    def _send(self, y, height):
        # Quick settings can open while the frame is being composed.
        if not self.visible():
            self.dirty = True
            return False
        self.env.tft.blit_buffer(self.buf, 0, self.top + y, self.width, height)
        return True

    def draw(self, scan, note):
        self.dirty = False
        self._draw_trace(scan)
        if self._send(0, self.WATER_Y):
            self._draw_waterfall(scan, note)
            self._send(self.WATER_Y, self.bottom_h)

    def _draw_trace(self, scan):
        f = self.upper
        f.fill(BG)
        for i, color in enumerate(HEADER):
            f.fill_rect(0, i * 7, self.width, 7, color)
        f.hline(0, 28, self.width, EDGE)
        _text(f, "Spectrum", 9, 7, TEXT, large)
        state = "PAUSED" if scan.paused else "LIVE"
        state_x = self.width - 14 - len(state) * small.WIDTH
        f.fill_rect(state_x - 5, 6, self.width - state_x - 4, 16, CHIP)
        _text(f, state, state_x, 8, AMBER if scan.paused else ACCENT)
        if scan.hold_on:
            _text(f, "HOLD", state_x - 36, 8)

        _text(f, "RBW %gk  span %gM" % (scan.rbw, scan.span), 9, 37)
        peak = "peak --"
        if scan.peak is not None:
            freq, level = scan.peak
            peak = "peak %d @ %s" % (round(level), format_mhz(freq, scan.span))
        _text(f, peak, self.width - 9 - len(peak) * small.WIDTH, 37, AMBER)

        x, y, w, h = self.PLOT_X, self.PLOT_Y, self.plot_w, self.PLOT_H
        f.fill_rect(x, y, w, h, PLOT)
        f.rect(x, y, w, h, EDGE)
        for dy, level in ((0, scan.hi), (h // 2, (scan.hi + scan.lo) // 2), (h - 1, scan.lo)):
            if dy:
                f.hline(x + 1, y + dy, w - 2, GRID)
            label = str(level)
            _text(f, label, x - 7 - len(label) * small.WIDTH, y + dy - small.HEIGHT // 2)
        for dx in (w // 4, w // 2, 3 * w // 4):
            f.vline(x + dx, y + 1, h - 2, GRID)

        if scan.cur is not None:
            if scan.hold_on and scan.hold is not None:
                self._line(f, scan.hold, scan.lo, scan.hi, HOLD, dashed=True)
            self._line(f, scan.cur, scan.lo, scan.hi, ACCENT)

    def _line(self, frame, levels, lo, hi, color, dashed=False):
        top = self.PLOT_Y + 1
        height = self.PLOT_H - 3
        bottom = top + height
        gain = height / (hi - lo)
        prev_x = self.edges[0]
        prev_y = bottom - int(max(0, min(height, (levels[0] - lo) * gain)) + 0.5)
        frame.pixel(prev_x, prev_y, color)
        for i in range(1, len(levels)):
            x = self.edges[i]
            y = bottom - int(max(0, min(height, (levels[i] - lo) * gain)) + 0.5)
            if not dashed or i % 4 < 2:
                frame.line(prev_x, prev_y, x, y, color)
            prev_x, prev_y = x, y
        frame.hline(prev_x, prev_y, self.edges[-1] - prev_x, color)

    def _draw_waterfall(self, scan, note):
        f = self.lower
        f.fill(BG)
        x, w = self.PLOT_X, self.plot_w
        h = self.bottom_h - self.FOOT_H
        f.fill_rect(x, 0, w, h, PLOT)
        f.rect(x, 0, w, h, EDGE)
        heat_max = len(HEAT) - 1
        scale = scan.hi - scan.lo
        colors = [HEAT[max(0, min(heat_max, (-b - scan.lo) * heat_max // scale))]
                  for b in range(256)]
        # A fixed height per history slot keeps old data from stretching as rows arrive.
        for age, data in enumerate(reversed(scan.rows)):
            y = 1 + age * (h - 2) // HISTORY
            end = 1 + (age + 1) * (h - 2) // HISTORY
            if end == y:
                continue
            start = 0
            color = colors[data[0]]
            for i in range(1, len(data)):
                next_color = colors[data[i]]
                if next_color != color:
                    f.fill_rect(self.edges[start], y, self.edges[i] - self.edges[start], end - y, color)
                    start, color = i, next_color
            f.fill_rect(self.edges[start], y, self.edges[-1] - self.edges[start], end - y, color)

        ramp_h = h - 27
        _text(f, "HI", 10, 0)
        for i, color in enumerate(reversed(HEAT)):
            y = 13 + i * ramp_h // len(HEAT)
            end = 13 + (i + 1) * ramp_h // len(HEAT)
            f.fill_rect(11, y, 10, end - y, color)
        _text(f, "LO", 10, h - 12)

        left = format_mhz(scan.start, scan.span)
        mid = format_mhz(scan.center, scan.span) + " MHz"
        right = format_mhz(scan.end, scan.span)
        _text(f, left, x, h + 3)
        _text(f, mid, x + (w - len(mid) * small.WIDTH) // 2, h + 3, TEXT)
        _text(f, right, x + w - len(right) * small.WIDTH, h + 3)

        f.hline(0, h + 17, self.width, GRID)
        hint = single_line(note) if note else (
            "<> pan  ^v span  b rbw  p pause  h hold  q quit")
        _text(f, hint[:(self.width - 18) // small.WIDTH], 9, h + 19, AMBER if note else DIM)
