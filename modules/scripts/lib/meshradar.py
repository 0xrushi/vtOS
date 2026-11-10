#
# Meshchat radar: your node in the middle, the nodes you have heard around it
# at their real bearing and distance.
# License: MIT
#
# A mixin for meshui.App (like meshset.SettingsMixin). Pure logic, no hardware
# imports. Only nodes that have shared a position can be placed, and the
# centre needs your own position (fixed in Settings > Position, or the GPS on
# the T-Deck Plus). The rest are counted in the footer, not guessed at.
#
# The screen is a character grid with taller-than-wide cells, so the circle is
# stretched horizontally by the cell aspect (derived from the 320x240 panel).
# The font has no block or bullet glyphs: rings are dots, nodes are 'o',
# the sweep is a line drawn with | / - \ .
#

import math
import time

from meshdraw import BG, ACCENT, FG, DIM, GREEN, YELLOW, S, paint, seg_len, age_str, snr_color, hops_str
from meshset import distance_bearing, _compass

RANGES_KM = (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000)
SWEEP_MS = 6000                    # one turn of the sweep line
TRAIL_DEG = 30                     # nodes just behind the beam stay bright
RING_FG = 238
OUTER_FG = 241
BEAM = (37, 30, 24)                # lead line and two fading trails
BEAM_STEP_DEG = 12
PANEL_W, PANEL_H = 320.0, 240.0    # T-Deck panel, for the cell aspect


def _now_ms():
    try:
        return time.ticks_ms()
    except AttributeError:         # host tests
        return int(time.time() * 1000)


def nice_range(km):
    """Smallest preset range that holds `km` (with a little margin)."""
    for r in RANGES_KM:
        if r >= km * 1.05:
            return r
    return RANGES_KM[-1]


def range_text(km):
    return "%d m" % int(km * 1000) if km < 1 else "%g km" % km


def cell_aspect(cols, rows):
    """Cell height over cell width on the 320x240 panel."""
    return (PANEL_H / rows) / (PANEL_W / cols)


def _beam_char(deg):
    d = deg % 180.0
    if d < 22.5 or d >= 157.5:
        return "|"
    if d < 67.5:
        return "/"
    if d < 112.5:
        return "-"
    return "\\"


def short_label(node):
    short = getattr(node, "short_name", "") or ""
    return (short or node.name)[:4]


class RadarMixin:
    def radar_init(self):
        self.radar_sel = 0
        self.radar_range = None        # None = auto-fit the farthest node
        self.node_back = None          # where the node screen returns to
        self.anim_clock = _now_ms

    def radar_open(self):
        self.mode = "radar"
        self.radar_sel = 0

    def animating(self):
        """True while a screen needs frequent redraws (the sweep line)."""
        return self.mode == "radar" and not self.prompt and not self.dialog

    # -- model --

    def radar_points(self):
        """([(node, km, bearing)] nearest first, nodes without a position, my position)."""
        mine = self.my_position()
        pts = []
        nopos = 0
        for n in self.nodes:
            if getattr(n, "ignored", False):
                continue
            lat = getattr(n, "lat", None)
            if lat is None or getattr(n, "lon", None) is None:
                nopos += 1
                continue
            if mine is None:
                continue
            km, brg = distance_bearing(mine[0], mine[1], lat, n.lon)
            pts.append((n, km, brg))
        pts.sort(key=lambda p: p[1])
        return pts, nopos, mine

    def _radar_scale(self, pts):
        if self.radar_range is not None:
            return self.radar_range, False
        far = pts[-1][1] if pts else 0.0
        return nice_range(max(far, 0.05)), True

    # -- input --

    def key_radar(self, k, now):
        pts, _, _ = self.radar_points()
        n = len(pts)
        if k == "ESC":
            self.mode = "menu"
            self.sel = 0
        elif k == "LEFT" and n:
            self.radar_sel = (self.radar_sel - 1) % n
        elif k == "RIGHT" and n:
            self.radar_sel = (self.radar_sel + 1) % n
        elif k in ("UP", "+", "="):
            self._radar_zoom(pts, -1)
        elif k in ("DOWN", "-"):
            self._radar_zoom(pts, 1)
        elif k == "a":
            self.radar_range = None
        elif k in ("\r", "\n", "i") and n:
            self.node_back = "radar"
            self.open_node(pts[min(self.radar_sel, n - 1)][0])
        return None

    def _radar_zoom(self, pts, direction):
        cur, _ = self._radar_scale(pts)
        i = RANGES_KM.index(cur) if cur in RANGES_KM else 0
        i = min(max(i + direction, 0), len(RANGES_KM) - 1)
        self.radar_range = RANGES_KM[i]

    # -- drawing --

    def render_radar(self, out, cols, rows, now):
        pts, nopos, mine = self.radar_points()
        rng, auto = self._radar_scale(pts)
        self._subheader(out, cols, "Radar (%d)" % len(pts),
                        "%s %s" % ("auto" if auto else "range", range_text(rng)))
        top, bottom = 4, rows - 2                 # plot rows; info row and hint row follow
        h = bottom - top + 1
        grid = [[(" ", FG, False)] * cols for _ in range(h)]

        def put(x, y, ch, fg, bold=False, force=True):
            if 0 <= x < cols and 0 <= y < h and (force or grid[y][x][0] == " "):
                grid[y][x] = (ch, fg, bold)

        if mine is None:
            self._radar_note(grid, cols, h)
        else:
            self._radar_plot(put, grid, cols, h, rows, pts, rng, now)

        for i in range(h):
            segs = []
            for ch, fg, bold in grid[i]:
                if segs and segs[-1][0] == fg and segs[-1][3] == bold:
                    segs[-1] = (fg, None, segs[-1][2] + ch, bold)
                else:
                    segs.append((fg, None, ch, bold))
            out.append(paint(top + i, segs, BG, cols))
        out.append(paint(rows - 1, self._radar_info(pts, nopos, mine, rng, now, cols), BG, cols))
        hint = " < > node  ^ v range  a auto  Enter info"
        if len(hint) > cols:
            hint = " <> ^v a Enter"
        out.append(paint(rows, [S(DIM, hint)], BG, cols))

    def _radar_note(self, grid, cols, h):
        lines = ("Radar needs your position.", "Set it in Settings > Position,",
                 "or wait for a GPS fix.")
        start = max(0, (h - len(lines)) // 2)
        for i, text in enumerate(lines):
            if start + i < h:
                x0 = max(0, (cols - len(text)) // 2)
                for j, ch in enumerate(text[:cols]):
                    grid[start + i][x0 + j] = (ch, DIM, False)

    def _radar_plot(self, put, grid, cols, h, rows, pts, rng, now):
        aspect = getattr(self, "radar_aspect", None) or cell_aspect(cols, rows)
        ry = (h - 1) // 2
        rx = int(ry * aspect)
        if rx > (cols - 1) // 2 - 1:
            rx = (cols - 1) // 2 - 1
            ry = int(rx / aspect)
        rx, ry = max(rx, 2), max(ry, 1)
        cx = cols // 2
        cy = (h - 1) // 2

        # range rings at 1/3, 2/3 and the edge
        for frac, fg in ((1 / 3.0, RING_FG), (2 / 3.0, RING_FG), (1.0, OUTER_FG)):
            steps = max(24, int(frac * (rx + ry) * 3))
            for s in range(steps):
                a = 2 * math.pi * s / steps
                put(cx + int(round(frac * rx * math.sin(a))), cy - int(round(frac * ry * math.cos(a))),
                    ".", fg, force=False)

        # the sweep: a lead line with two fading trails
        ms = self.anim_clock()
        theta = (ms % SWEEP_MS) * 360.0 / SWEEP_MS
        length = max(rx, ry * 2)
        for k, color in enumerate(BEAM):
            ang = theta - k * BEAM_STEP_DEG
            sa, ca = math.sin(math.radians(ang)), math.cos(math.radians(ang))
            ch = _beam_char(ang)
            for t in range(1, length + 1):
                f = t / float(length)
                put(cx + int(round(f * rx * sa)), cy - int(round(f * ry * ca)), ch, color)
        put(cx, cy - ry, "N", ACCENT, True)

        # nodes, nearest last so they stay on top; the selected one and its label first
        sel = min(self.radar_sel, len(pts) - 1) if pts else -1
        placed = []
        for idx, (n, km, brg) in enumerate(pts):
            if km > rng:
                continue
            f = km / rng
            sb, cb = math.sin(math.radians(brg)), math.cos(math.radians(brg))
            x = cx + int(round(f * rx * sb))
            y = cy - int(round(f * ry * cb))
            if x == cx and y == cy:             # too close to resolve: next cell over, same bearing
                if abs(sb) * aspect > abs(cb):
                    x += 1 if sb > 0 else -1
                else:
                    y += -1 if cb > 0 else 1
            behind = (theta - brg) % 360.0
            fresh = behind <= TRAIL_DEG
            age = None if n.heard is None else now - n.heard
            color = GREEN if (age is not None and age < 600) else (ACCENT if (age is not None and age < 3600) else DIM)
            if idx == sel:
                put(x, y, "@", YELLOW, True)
            else:
                put(x, y, "o", color, fresh)
            placed.append((idx == sel, x, y, n, color))
        placed.sort(key=lambda p: not p[0])
        taken = set((p[1], p[2]) for p in placed)
        for is_sel, x, y, n, color in placed:
            label = short_label(n)
            fg = YELLOW if is_sel else color
            for lx in (x + 2, x - 1 - len(label)):
                spots = [(lx + j, y) for j in range(len(label))]
                if lx >= 0 and lx + len(label) <= cols and not any(p in taken for p in spots):
                    for j, ch in enumerate(label):
                        put(lx + j, y, ch, fg, is_sel)
                    taken.update(spots)
                    break
        put(cx, cy, "+", ACCENT, True)

    def _radar_info(self, pts, nopos, mine, rng, now, cols):
        if mine is None:
            return [S(DIM, " %d with position, %d without" % (len(pts), nopos))]
        if pts:
            n, km, brg = pts[min(self.radar_sel, len(pts) - 1)]
            age = None if n.heard is None else now - n.heard
            snr = "--" if n.snr is None else "%+.1fdB" % n.snr
            dist = "%d m" % int(km * 1000) if km < 1 else "%.1f km" % km
            segs = [S(FG, " " + n.name[:12], bold=True), S(DIM, "  " + hops_str(n.hops)),
                    S(snr_color(n.snr), "  " + snr), S(FG, "  %s %s" % (dist, _compass(brg))),
                    S(DIM, "  " + age_str(age))]
        else:
            segs = [S(DIM, " No node with a position heard yet")]
        if nopos:
            segs.append(S(DIM, "  +%d no pos" % nopos))
        while len(segs) > 1 and seg_len(segs) > cols:     # drop the least useful tail first
            segs.pop()
        return segs
