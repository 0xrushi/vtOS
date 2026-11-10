"""Pixel view of the existing MeshChat radar model."""

import math

import framebuf

import pixelui as ui
from meshdraw import age_str, hops_str
from meshradar import SWEEP_MS, range_text
from meshset import _compass

TRAIL = tuple(ui.pixel(rgb) for rgb in (0x231B2F, 0x332641, 0x4D3866, 0x74539B))
FRESH = ui.pixel(0xA7B7F3)
CARD = ui.pixel(0x222227)


def _right(frame, text, right, y, color=ui.DIM):
    ui.text(frame, text, right - len(text) * ui.small.WIDTH, y, color)


class Screen:
    def __init__(self, env):
        self.env = env
        self.w = env.screen_width
        self.h = env.screen_height - env.status_height
        self.buf = bytearray(self.w * self.h * 2)
        self.frame = framebuf.FrameBuffer(self.buf, self.w, self.h, framebuf.RGB565)
        self.dirty = True

    def draw(self, app, now):
        env = self.env
        if not ui.visible(env):
            return
        self.dirty = False
        pts, no_pos, mine = app.radar_points()
        scale, auto = app._radar_scale(pts)
        sel = min(app.radar_sel, len(pts) - 1)
        f = self.frame
        f.fill(ui.BG)
        for i, color in enumerate(ui.HEADER):
            f.fill_rect(0, i * 7, self.w, 7, color)
        f.hline(0, 28, self.w, ui.EDGE)
        ui.text(f, "Radar", 9, 7, ui.TEXT, ui.large)
        if app.info.get("demo"):
            ui.text(f, "DEMO", 59, 8, ui.DIM)
        mode = "%s %s" % ("AUTO" if auto else "RANGE", range_text(scale))
        w = len(mode) * ui.small.WIDTH + 12
        f.fill_rect(self.w - 9 - w, 6, w, 16, ui.CHIP)
        _right(f, mode, self.w - 15, 8, ui.ACCENT)

        split = self.w * 3 // 5
        top, bottom = 36, self.h - 24
        f.fill_rect(9, top, split - 15, bottom - top, ui.PLOT)
        f.rect(9, top, split - 15, bottom - top, ui.EDGE)
        if mine is not None:
            self._plot(app, pts, scale, sel, now, split, top, bottom)
        else:
            for i, line in enumerate(("Needs your position", "Set it in Settings >", "Position or wait for GPS.")):
                x = (split + 3 - len(line) * ui.small.WIDTH) // 2
                ui.text(f, line, x, (top + bottom) // 2 - 22 + i * 15, ui.TEXT if not i else ui.DIM)
        self._details(pts, no_pos, sel, scale, now, split, top, bottom)

        f.hline(0, self.h - 18, self.w, ui.GRID)
        notice = app.notice if app.notice and now < app.notice[2] else None
        hint = ui.single_line(notice[0]) if notice else "<> node  ^v range  a auto  Enter info  Esc back"
        ui.text(f, hint[:(self.w - 18) // ui.small.WIDTH], 9, self.h - 15, ui.AMBER if notice else ui.DIM)
        if ui.visible(env):
            env.tft.blit_buffer(self.buf, 0, env.status_height, self.w, self.h)
        else:
            self.dirty = True

    def _plot(self, app, pts, scale, sel, now, split, top, bottom):
        f = self.frame
        cx, cy = (split + 3) // 2, (top + bottom) // 2
        r = min((bottom - top) // 2 - 18, split // 2 - 25)
        f.hline(cx - r, cy, 2 * r + 1, ui.GRID)
        f.vline(cx, cy - r, 2 * r + 1, ui.GRID)
        for radius, color in ((r // 3, ui.GRID), (2 * r // 3, ui.GRID), (r, ui.EDGE)):
            f.ellipse(cx, cy, radius, radius, color)
        ui.text(f, "N", cx - 3, cy - r - 14, ui.ACCENT)
        ui.text(f, "S", cx - 3, cy + r + 3)
        ui.text(f, "W", cx - r - 12, cy - 6)
        ui.text(f, "E", cx + r + 7, cy - 6)

        angle = (app.anim_clock() % SWEEP_MS) * 360 / SWEEP_MS
        for step in range(16, -1, -1):
            a = math.radians(angle - step * 3)
            x = cx + int(round(r * math.sin(a)))
            y = cy - int(round(r * math.cos(a)))
            f.line(cx, cy, x, y, ui.ACCENT if step == 0 else TRAIL[4 - (step + 3) // 4])

        for idx in range(len(pts) - 1, -1, -1):
            if idx != sel:
                self._point(pts[idx], cx, cy, r, scale, now, False)
        f.hline(cx - 3, cy, 7, ui.ACCENT)
        f.vline(cx, cy - 3, 7, ui.ACCENT)
        if sel >= 0:
            self._point(pts[sel], cx, cy, r, scale, now, True)

    def _point(self, point, cx, cy, radius, scale, now, selected):
        node, km, bearing = point
        if km > scale:
            return
        a = math.radians(bearing)
        r = max(6, radius * km / scale)
        x = cx + int(round(r * math.sin(a)))
        y = cy - int(round(r * math.cos(a)))
        f = self.frame
        if selected:
            f.ellipse(x, y, 5, 5, ui.AMBER)
            f.ellipse(x, y, 2, 2, ui.AMBER, True)
        else:
            fresh = node.heard is not None and now - node.heard < 600
            f.ellipse(x, y, 2, 2, FRESH if fresh else ui.DIM, True)

    def _details(self, pts, no_pos, sel, scale, now, x, top, bottom):
        f = self.frame
        w = self.w - x - 9
        f.fill_rect(x, top, w, bottom - top, CARD)
        f.rect(x, top, w, bottom - top, ui.EDGE)
        x += 9
        if sel >= 0:
            node, km, bearing = pts[sel]
            ui.text(f, "NODE %d/%d" % (sel + 1, len(pts)), x, top + 8, ui.AMBER)
            name = ui.single_line(node.name)[:(w - 18) // ui.large.WIDTH]
            ui.text(f, name, x, top + 25, ui.TEXT, ui.large)
            distance = range_text(km) if km < 1 else "%.1f km" % km
            ui.text(f, "%s  %s" % (distance, _compass(bearing)), x, top + 45, ui.ACCENT)
            ui.text(f, "Outside range" if km > scale else "%03d deg bearing" % (round(bearing) % 360),
                    x, top + 61)
            f.hline(x, top + 80, w - 18, ui.EDGE)
            snr = "--" if node.snr is None else "%+.1f dB" % node.snr
            ui.text(f, "SNR  " + snr, x, top + 88)
            ui.text(f, hops_str(node.hops), x, top + 104)
            if node.heard is not None:
                ui.text(f, "Seen " + age_str(now - node.heard), x, top + 120)
        else:
            ui.text(f, "MESH NODES", x, top + 8, ui.ACCENT)
            ui.text(f, "No positions", x, top + 27, ui.TEXT)
            ui.text(f, "to plot yet.", x, top + 42)
        ui.text(f, "%d without GPS" % no_pos, x, bottom - 19)
