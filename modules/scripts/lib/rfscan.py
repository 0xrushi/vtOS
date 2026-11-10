#
# Sub-GHz spectrum view: sweep state, scaling and rendering for `rfspec`.
# License: MIT
#
# Pure logic with no hardware imports, so it runs on the host under pytest
# (utils/tests/test_rfscan.py). The app in bin/rfspec.py owns the radio and the
# keyboard; this module turns sweeps of channel power into a trace plus a
# waterfall on the character grid (the font has no block glyphs, so bars are
# background-coloured cells).
#
# The SX1262 only measures power in its own channel, so a "sweep" retunes it
# step by step and reads RSSI at each one (sx126x.SX126x.sweep). The channel
# width set for the radio is the resolution bandwidth (RBW).
#

import math

from meshdraw import BG, BG_BAR, BG_TAB, FG, DIM, ACCENT, YELLOW, S, paint, rjust

SPANS = (2, 5, 10, 20, 30, 50, 100, 200)          # MHz across the screen
RBWS = (62.5, 125.0, 250.0, 500.0)                # kHz, the SX1262 LoRa bandwidths
MIN_MHZ = 150.0                                   # SX1262 tuning range
MAX_MHZ = 960.0
MAX_POINTS = 160                                  # retunes per sweep (~0.2 s on the T-Deck)
HISTORY = 40                                      # waterfall rows kept
LEFT_MARGIN = 5                                   # y-axis label columns
MIN_RANGE_DB = 30

# blue -> cyan -> green -> yellow -> red, as xterm-256 colours
HEAT = (17, 18, 19, 20, 21, 27, 33, 39, 45, 44, 43, 42, 41, 40, 46, 82, 118, 154, 190, 226,
        220, 214, 208, 202, 196)
TRACE_FILL = 23
TRACE_TOP = 45


def _floor10(v):
    return int(math.floor(v / 10.0)) * 10


def _ceil10(v):
    return int(math.ceil(v / 10.0)) * 10


def default_view(cfg):
    """(centre MHz, span MHz) around the configured region with room to spare, else the EU 868 band."""
    reg = None
    try:
        reg = cfg.reg() if cfg is not None else None
    except Exception:
        reg = None
    if reg:
        start, end = reg[2], reg[3]
        need = ((end - start) + 2) * 1.6     # show the whole band with room around it
        span = SPANS[-1]
        for s in SPANS:
            if s >= need and s >= 30:
                span = s
                break
        return round((start + end) / 2.0, 1), float(span)
    return 868.0, 50.0


class Scan:
    """One spectrum view: centre, span, RBW, the latest sweep, peak hold and waterfall."""

    def __init__(self, center, span, rbw=125.0, width=40):
        self.width = max(8, width)
        self.rbw = rbw if rbw in RBWS else 125.0
        self.paused = False
        self.hold_on = True
        self.set_view(center, span)

    # -- view --

    def set_view(self, center, span):
        span = float(min(max(span, SPANS[0]), SPANS[-1]))
        half = span / 2.0
        center = min(max(center, MIN_MHZ + half), MAX_MHZ - half)
        self.span = span
        self.center = round(center, 3)
        self.start = self.center - half
        self.end = self.center + half
        self.step = max(self.rbw / 1000.0, span / (MAX_POINTS - 1))
        self.points = int(span / self.step) + 1
        self.reset()

    def reset(self):
        self.cur = None            # latest sweep, binned to the plot width (dBm)
        self.hold = None           # peak hold, same shape
        self.rows = []             # waterfall, newest last: bytes of -dBm per column
        self.peak = None           # (MHz, dBm) of the latest sweep
        self.sweeps = 0
        self.lo = -130
        self.hi = -80

    def freqs(self):
        s = self.step
        return [self.start + i * s for i in range(self.points)]

    def pan(self, direction):
        self.set_view(self.center + direction * self.span / 4.0, self.span)

    def zoom(self, direction):
        """direction > 0 widens the span."""
        i = SPANS.index(self.span) if self.span in SPANS else 0
        i = min(max(i + (1 if direction > 0 else -1), 0), len(SPANS) - 1)
        self.set_view(self.center, SPANS[i])

    def next_rbw(self):
        i = RBWS.index(self.rbw)
        self.rbw = RBWS[(i + 1) % len(RBWS)]
        self.set_view(self.center, self.span)

    # -- data --

    def add(self, levels):
        """Take one sweep (dBm per frequency from freqs()); bins it to the plot width."""
        n = len(levels)
        if n == 0:
            return
        w = self.width
        cur = []
        for c in range(w):
            a = c * n // w
            b = (c + 1) * n // w
            if b <= a:
                cur.append(levels[min(a, n - 1)])
            else:
                m = levels[a]
                for j in range(a + 1, b):
                    if levels[j] > m:
                        m = levels[j]
                cur.append(m)
        self.cur = cur
        if self.hold is None:
            self.hold = list(cur)
        else:
            self.hold = [h if h > v else v for h, v in zip(self.hold, cur)]
        top = max(range(n), key=lambda i: levels[i])
        self.peak = (self.start + top * self.step, levels[top])
        self.rows.append(bytes(min(255, max(0, int(-v))) for v in cur))
        if len(self.rows) > HISTORY:
            del self.rows[0]
        self.sweeps += 1
        self._rescale()

    def _rescale(self):
        lo = _floor10(min(self.cur) - 3)
        hi = _ceil10(max(self.cur) + 3)
        if hi - lo < MIN_RANGE_DB:
            hi = lo + MIN_RANGE_DB
        self.lo, self.hi = lo, hi

    # -- keys --

    def key(self, k):
        """Apply a key. Returns "quit", "rbw" (the radio must be reconfigured) or None."""
        if k in ("q", "Q", "ESC", "\x03"):
            return "quit"
        if k == "LEFT":
            self.pan(-1)
        elif k == "RIGHT":
            self.pan(1)
        elif k == "UP":
            self.zoom(-1)
        elif k == "DOWN":
            self.zoom(1)
        elif k == "b":
            self.next_rbw()
            return "rbw"
        elif k in ("p", " "):
            self.paused = not self.paused
        elif k == "h":
            self.hold_on = not self.hold_on
        elif k == "c":
            self.hold = list(self.cur) if self.cur else None
        return None


# --- rendering ---------------------------------------------------------------

def heat(v, lo, hi):
    t = (v - lo) / float(hi - lo) if hi > lo else 0.0
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return HEAT[int(t * (len(HEAT) - 1))]


def _runs(cells, row, cols):
    """[(char, fg, bg)] -> painted row string; consecutive equal styles share one run."""
    segs = []
    for ch, fg, bg in cells:
        if segs and segs[-1][0] == fg and segs[-1][1] == bg:
            segs[-1][2] += ch
        else:
            segs.append([fg, bg, ch, False])
    return paint(row, [tuple(s) for s in segs], BG, cols)


def format_mhz(v, span):
    return "%.1f" % v if span >= 10 else "%.2f" % v


def layout(rows):
    """(trace rows, waterfall rows) for a screen of `rows` lines."""
    body = rows - 4                 # title, info, axis, hint
    ph = max(3, body * 2 // 5)
    wf = body - ph
    if wf < 2:
        wf = 2
        ph = max(2, body - wf)
    return ph, wf


def render(scan, cols, rows, note=""):
    """The whole screen as one escape-sequence string."""
    out = ["\x1b[?25l"]
    ph, wf = layout(rows)
    pw = scan.width
    lo, hi = scan.lo, scan.hi

    # title and info
    title = [S(ACCENT, " Spectrum", bold=True)]
    if scan.paused:
        title.append(S(YELLOW, "  paused"))
    peak = "peak --" if scan.peak is None else "peak %d @ %s" % (round(scan.peak[1]), format_mhz(scan.peak[0], scan.span))
    gap = max(1, cols - 1 - 9 - (8 if scan.paused else 0) - len(peak))
    title += [S(FG, " " * gap), S(YELLOW if scan.peak else DIM, peak + " ")]
    out.append(paint(1, title, BG_BAR, cols))
    rbw = ("%gk" % scan.rbw)
    info = " RBW %s  span %gM  %s dB" % (rbw, scan.span, "%d..%d" % (lo, hi))
    if note:
        info = " " + note
    out.append(paint(2, [S(DIM, info[:cols])], BG_TAB, cols))

    # trace
    cur = scan.cur
    hold = scan.hold if scan.hold_on else None
    heights = hold_h = None
    if cur is not None:
        rng = float(hi - lo)
        heights = [min(ph, max(0, int((v - lo) / rng * ph + 0.5))) for v in cur]
        if hold is not None:
            hold_h = [min(ph, max(0, int((v - lo) / rng * ph + 0.5))) for v in hold]
    labels = {0: str(hi), ph // 2: str((hi + lo) // 2), ph - 1: str(lo)}
    for r in range(ph):
        label = labels.get(r, "")
        cells = [(ch, DIM, BG) for ch in rjust(label, LEFT_MARGIN - 1)] + [(" ", FG, BG)]
        for c in range(pw):
            ch, fg, bg = " ", FG, BG
            if heights is not None:
                level = ph - r                  # this row's height, 1 at the bottom
                h = heights[c]
                if h >= level:
                    bg = TRACE_TOP if h == level else TRACE_FILL
                if hold_h is not None and hold_h[c] == level and hold_h[c] > h:
                    ch, fg = "-", YELLOW
            cells.append((ch, fg, bg))
        out.append(_runs(cells, 3 + r, cols))

    # frequency axis
    axis = [" "] * cols
    left, mid, right = (format_mhz(scan.start, scan.span), format_mhz(scan.center, scan.span) + " MHz",
                        format_mhz(scan.end, scan.span))
    for text, pos in ((left, LEFT_MARGIN), (mid, LEFT_MARGIN + (pw - len(mid)) // 2),
                      (right, LEFT_MARGIN + pw - len(right))):
        for i, ch in enumerate(text):
            if 0 <= pos + i < cols:
                axis[pos + i] = ch
    out.append(paint(3 + ph, [S(DIM, "".join(axis))], BG, cols))

    # waterfall: newest at the top
    hist = scan.rows
    for i in range(wf):
        row = 4 + ph + i
        idx = len(hist) - 1 - i
        cells = [(" ", FG, BG)] * LEFT_MARGIN
        if idx >= 0:
            data = hist[idx]
            cells += [(" ", FG, heat(-b, lo, hi)) for b in data]
        out.append(_runs(cells, row, cols))

    hint = " < > pan  ^ v span  b rbw  p pause  h hold  q quit"
    if len(hint) > cols:
        hint = " <> pan ^v span b p h q"
    out.append(paint(rows, [S(DIM, hint[:cols])], BG, cols))
    return "".join(out)


# --- demo source ---------------------------------------------------------------

class SimRadio:
    """A fake sub-GHz band so the view can be tried and tested without a radio."""

    demo = True

    def __init__(self, rng=None):
        if rng is None:
            import random
            rng = random.random
        self.rng = rng
        self.n = 0
        # (MHz, width MHz, dBm, duty): a few LoRa-style channels and a keyfob-like burst
        self.signals = ((868.1, 0.25, -78, 0.9), (868.3, 0.25, -92, 0.6), (906.875, 0.30, -70, 0.5),
                        (915.0, 0.10, -96, 0.3), (433.92, 0.20, -85, 0.4), (869.525, 0.25, -104, 0.8))
        self.bw = 125.0

    def begin(self, **kw):
        self.bw = kw.get("bw", 125.0)

    def rx_boost(self, on):
        pass

    def deinit(self):
        pass

    def sweep(self, freqs, settle_us=0, chunk=16):
        self.n += 1
        on = [self.rng() < s[3] for s in self.signals]
        out = []
        for f in freqs:
            v = -118.0 + (self.rng() - 0.5) * 4.0
            for s, active in zip(self.signals, on):
                if active:
                    width = s[1] + self.bw / 2000.0
                    d = abs(f - s[0])
                    if d < width:
                        v = max(v, s[2] - 30.0 * d / width + (self.rng() - 0.5) * 3.0)
            out.append(v)
        return out


# --- radio start-up ------------------------------------------------------------

def start_radio(radio, cfg, freq, rbw):
    """Put the SX1262 in LoRa receive at `freq` with channel width `rbw` kHz.

    Reuses the TCXO voltage and RF-switch settings Meshtastic found for this
    board. Returns (ok, message). Nothing is transmitted.
    """
    tcxo = getattr(cfg, "tcxo", None)
    found = getattr(cfg, "tcxo_found", None)
    cands = [tcxo] if tcxo is not None else ([found] if found is not None else [0.0, 1.8, 2.8, 2.4, 3.3])
    last = "radio did not start"
    for t in cands:
        try:
            radio.begin(freq=freq, bw=rbw, sf=7, cr=5, sync_word=0x12, power=0, preamble=8, tcxo=t,
                        dio2_rf_switch=getattr(cfg, "dio2", True),
                        current_limit=getattr(cfg, "current_limit", 0))
        except TypeError:
            return False, "needs the Python SX126x driver (rebuild vtOS)"
        except RuntimeError as e:
            last = str(e)
            continue
        try:
            errs = radio.get_device_errors()
        except Exception:
            errs = 0
        if errs:
            last = "radio error 0x%04X" % errs
            continue
        try:
            radio.rx_boost(True)
        except Exception:
            pass
        return True, "ok"
    return False, last
