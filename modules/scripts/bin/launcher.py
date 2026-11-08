#
# MicroPython minimal app launcher (rofi-style)
# License: MIT
#
# Opened with Alt+D at the shell prompt. Type to filter, roll the trackball
# up/down to move, Enter (or trackball click on a match) to run, Esc,
# Alt+D again, or a long trackball press to dismiss.
#

import sys

# Palette (xterm-256): quiet greys with the shell prompt's green accent.
BG = 234
BG_INPUT = 236
FG = 250
FG_DIM = 243
FG_BRIGHT = 255
ACCENT = 85

MAX_VISIBLE = 8
MAX_WIDTH = 34

# Opening key as seen by the shell (see lib/keyrepeat.py).
TOGGLE_KEY = "\x1d"

DESCRIPTIONS = {
    "agent": "chat agent (openai api)",
    "blechat": "bluetooth mesh chat",
    "blescan": "ble device scan",
    "c2": "command & control",
    "chess": "play chess",
    "dict": "dictionary",
    "epub": "e-book reader",
    "fc": "font chooser",
    "fm": "file manager",
    "ftp": "ftp client",
    "ftpd": "ftp server",
    "gemini": "gemini browser",
    "gopher": "gopher browser",
    "irc": "irc client",
    "iqtx": "iq lab transmitter",
    "loracfg": "lora settings",
    "lorachat": "lora chat",
    "loramon": "lora monitor",
    "md": "markdown viewer",
    "menu": "shortcut menu",
    "mines": "minesweeper",
    "neofetch": "system info",
    "nm": "network manager",
    "ping": "ping a host",
    "play": "audio player",
    "pop3": "mail reader",
    "rec": "voice recorder",
    "rss": "feed reader",
    "sftp": "sftp client",
    "sftpd": "sftp server",
    "smtp": "mail sender",
    "ssh": "secure shell",
    "sshd": "ssh server",
    "sniff": "802.11 pcap sniffer",
    "spectrum": "2.4ghz spectrum",
    "stream": "internet radio",
    "wardrive": "gps wifi logger",
    "telnet": "telnet client",
    "telnetd": "telnet server",
    "usbmsc": "usb mass storage",
    "vi": "text editor",
    "vncd": "vnc server",
    "webvncd": "web vnc server",
    "wiki": "offline wikipedia",
    "yt": "youtube via ssh server",
    "zm": "z-machine games",
}


def build_entries(apps, aliases=None):
    """(name, description) pairs: favs first, then registered commands."""
    entries = []
    seen = set()
    for name in sorted(aliases or {}):
        entries.append((name, "-> " + aliases[name]))
        seen.add(name)
    for name in sorted(apps):
        if name not in seen:
            entries.append((name, DESCRIPTIONS.get(name, "")))
    return entries


def _subsequence(needle, hay):
    i = 0
    for ch in hay:
        if i < len(needle) and ch == needle[i]:
            i += 1
    return i == len(needle)


def filter_entries(entries, query):
    """Rank by prefix, then substring, then loose subsequence match."""
    q = query.strip().lower()
    if not q:
        return list(entries)
    scored = []
    for index, entry in enumerate(entries):
        name = entry[0].lower()
        if name.startswith(q):
            rank = 0
        elif q in name:
            rank = 1
        elif q in entry[1].lower():
            rank = 2
        elif _subsequence(q, name):
            rank = 3
        else:
            continue
        scored.append((rank, index, entry))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [s[2] for s in scored]


def geometry(cols, rows, count):
    """Panel (left, top, width, visible) -- centred, one input row on top."""
    width = min(cols - 2, MAX_WIDTH)
    if width < 10:
        width = cols
    visible = max(1, min(MAX_VISIBLE, rows - 3, max(count, 1)))
    left = (cols - width) // 2 + 1
    top = max(1, (rows - (visible + 1)) // 2)
    return left, top, width, visible


def _fit(text, width):
    if len(text) > width:
        return text[:width]
    return text + " " * (width - len(text))


def _goto(row, col):
    return "\x1b[%d;%dH" % (row, col)


def _sgr(fg, bg):
    return "\x1b[38;5;%d;48;5;%dm" % (fg, bg)


def render(cols, rows, query, matches, sel, total, label="run"):
    """Full panel as one escape-sequence string."""
    left, top, width, visible = geometry(cols, rows, len(matches))
    out = ["\x1b[?25l"]

    # Input row:  run  query_                         3/28
    count = ("%d/%d" % (len(matches), total)) if matches else "0/%d" % total
    tag = " %s " % label
    room = width - len(tag) - len(count) - 3
    shown = query[-room:] if room > 0 else ""
    out.append(_goto(top, left) + _sgr(ACCENT, BG_INPUT) + tag)
    line = _fit(shown + "_", max(room + 1, 1))
    out.append(_sgr(FG_BRIGHT, BG_INPUT) + line)
    out.append(_sgr(FG_DIM, BG_INPUT) + " " + count + " ")

    # Result rows, windowed around the selection.
    start = 0
    if sel >= visible:
        start = sel - visible + 1
    for i in range(visible):
        row = top + 1 + i
        out.append(_goto(row, left))
        idx = start + i
        if idx >= len(matches):
            if i == 0 and not matches:
                out.append(_sgr(FG_DIM, BG) + _fit("  no matches", width))
            else:
                out.append(_sgr(FG, BG) + " " * width)
            continue
        name, desc = matches[idx]
        label_w = width - 2
        if desc:
            gap = label_w - len(name) - len(desc)
            if gap < 1:
                desc = desc[:max(label_w - len(name) - 1, 0)]
                gap = label_w - len(name) - len(desc)
        else:
            gap = label_w - len(name)
        gap = max(gap, 0)
        if idx == sel:
            out.append(_sgr(ACCENT, BG) + "> ")
            out.append(_sgr(FG_BRIGHT, BG) + "\x1b[1m" + name + "\x1b[22m")
            out.append(_sgr(FG_DIM, BG) + " " * gap + desc)
        else:
            out.append(_sgr(FG, BG) + "  " + name)
            out.append(_sgr(FG_DIM, BG) + " " * gap + desc)
        used = 2 + len(name) + gap + len(desc)
        if used < width:
            out.append(" " * (width - used))
    out.append("\x1b[0m")
    return "".join(out)


def _read_key(timeout_ms=40):
    """One keypress. Arrow sequences from the trackball become 'UP' etc."""
    ch = sys.stdin.read(1)
    if ch != "\x1b":
        return ch
    # A bare ESC (trackball click) has nothing after it; arrows are
    # injected whole, so a short wait distinguishes the two.
    try:
        import select
        poller = select.poll()
        poller.register(sys.stdin, select.POLLIN)
        if not poller.poll(timeout_ms):
            return "ESC"
    except (ImportError, OSError, AttributeError):
        pass
    if sys.stdin.read(1) != "[":
        return "ESC"
    seq = sys.stdin.read(1)
    return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(seq, "")


def pick(env, entries, label="run"):
    """Show the picker; return the chosen entry name or None."""
    if not entries:
        return None
    cols, rows = env.cols, env.rows
    query = ""
    sel = 0
    matches = list(entries)
    out = sys.stdout

    out.write("\x1b[2J\x1b[H")
    try:
        while True:
            out.write(render(cols, rows, query, matches, sel, len(entries), label))
            try:
                key = _read_key()
            except UnicodeError:
                continue
            except KeyboardInterrupt:
                return None

            if key in ("ESC", TOGGLE_KEY):
                return None
            if key in ("\r", "\n"):
                return matches[sel][0] if matches else None
            if key == "UP":
                if matches:
                    sel = (sel - 1) % len(matches)
            elif key == "DOWN":
                if matches:
                    sel = (sel + 1) % len(matches)
            elif key in ("\x08", "\x7f"):
                if query:
                    query = query[:-1]
                    matches = filter_entries(entries, query)
                    sel = 0
            elif len(key) == 1 and 32 <= ord(key) <= 126:
                query += key
                matches = filter_entries(entries, query)
                sel = 0
    finally:
        out.write("\x1b[0m\x1b[2J\x1b[H\x1b[?25h")


def main(env, args):
    """`launcher` from the shell: pick an app and run it."""
    shell = env.shell
    choice = pick(env, build_entries(shell.apps, shell.aliases))
    if choice:
        shell.execute(choice)
