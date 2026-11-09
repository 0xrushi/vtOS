#
# Meshchat drawing primitives: palette, styled segments, row painting, text helpers.
# License: MIT
#
# Shared by lib/meshui.py (chat) and lib/meshset.py (settings screens). The font
# is ASCII plus box drawing only, so everything here stays within that.
#

import time

# Palette (xterm-256): quiet greys, one soft-blue accent, colour only for state.
BG = 234
BG_BAR = 236
BG_TAB = 235
BG_SEL = 237
RULE = 238
FG = 252
DIM = 244
ACCENT = 110
GREEN = 114
YELLOW = 221
RED = 203
SENDER_COLORS = (109, 144, 139, 108, 174)  # teal, sand, mauve, sage, rose


# --- small helpers -----------------------------------------------------------

def S(fg, text, bg=None, bold=False):
    """One styled run of text; bg None inherits the row background."""
    return (fg, bg, text, bold)


def _goto(row, col):
    return "\x1b[%d;%dH" % (row, col)


def _sgr(fg, bg):
    return "\x1b[38;5;%d;48;5;%dm" % (fg, bg)


def seg_len(segs):
    n = 0
    for s in segs:
        n += len(s[2])
    return n


def clip(segs, width):
    out = []
    used = 0
    for s in segs:
        if used >= width:
            break
        t = s[2]
        if used + len(t) > width:
            t = t[:width - used]
        if t:
            out.append((s[0], s[1], t, s[3]))
            used += len(t)
    return out, used


def paint(row, segs, bg, cols, right=False, left_col=1, width=None):
    """One full row (or box slice) as an escape string, padded with bg."""
    width = cols if width is None else width
    segs, used = clip(segs, width)
    lead = max(0, width - used - 1) if right else 0
    out = [_goto(row, left_col), _sgr(FG, bg), " " * lead]
    for fg, sbg, text, bold in segs:
        out.append(_sgr(fg, bg if sbg is None else sbg))
        out.append("\x1b[1m" + text + "\x1b[22m" if bold else text)
    tail = width - lead - used
    if tail > 0:
        out.append(_sgr(FG, bg) + " " * tail)
    out.append("\x1b[0m")
    return "".join(out)


def ljust(text, width):
    """Pad on the right (MicroPython's str has no ljust)."""
    return text + " " * (width - len(text)) if len(text) < width else text


def rjust(text, width):
    return " " * (width - len(text)) + text if len(text) < width else text


def sanitize(text):
    """The font is ASCII-only, and a stray ESC would corrupt the screen."""
    out = []
    for ch in text:
        o = ord(ch)
        if ch == "\n":
            out.append(ch)
        elif ch == "\t":
            out.append(" ")
        elif 32 <= o < 127:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def wrap(text, width):
    """Word-wrap to width, hard-splitting words longer than a line."""
    width = max(width, 1)
    lines = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            if not word:
                continue
            while len(word) > width:
                if line:
                    lines.append(line)
                    line = ""
                lines.append(word[:width])
                word = word[width:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def age_str(secs):
    if secs is None:
        return "--"
    secs = int(secs)
    if secs < 10:
        return "now"
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def clock_ok(now):
    """RTC may be unset (year 2000/1970); then show ages, not wall time."""
    try:
        return time.localtime(int(now))[0] >= 2024
    except (OverflowError, ValueError, OSError):
        return False


def stamp(ts, now, wall):
    if wall:
        t = time.localtime(int(ts))
        return "%02d:%02d" % (t[3], t[4])
    return age_str(now - ts)


def name_color(sid):
    total = 0
    for ch in str(sid):
        total += ord(ch)
    return SENDER_COLORS[total % len(SENDER_COLORS)]


def snr_color(snr):
    if snr is None:
        return DIM
    if snr >= 0:
        return GREEN
    if snr >= -10:
        return YELLOW
    return RED


def hops_str(hops):
    if hops is None:
        return "?"
    if hops == 0:
        return "direct"
    if hops == 1:
        return "1 hop"
    return "%d hops" % hops
