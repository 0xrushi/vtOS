#!/usr/bin/env python3
"""Preview Meshchat frames in a host terminal at T-Deck grid sizes.

    python3 utils/mesh_preview.py              # chat screen at 40x16, 53x19, 64x29
    python3 utils/mesh_preview.py nodes 40x16  # one screen at one size

Screens: chat, scrolled, menu, nodes, node, chats, channels, channel, keydialog,
settings, radio, device, position, advanced, prompt, share, stats, info, help.
"""

import re
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "modules/scripts/lib"
sys.path.insert(0, str(LIB))

import meshback  # noqa: E402
import meshui  # noqa: E402

TOKEN = re.compile(r"\x1b\[(\d+);(\d+)H|\x1b\[([0-9;]*)m|\x1b\[\?25[lh]|([^\x1b]+)")


def emulate(frame, cols, rows):
    """Replay a frame onto a cols x rows grid. Raises if anything overflows.

    Returns (plain_rows, ansi_rows).
    """
    grid = [[(" ", "")] * cols for _ in range(rows)]
    row = col = 0
    st = {"fg": None, "bg": None, "bold": False}

    def current():
        if st["fg"] is None and not st["bold"]:
            return ""
        s = "\x1b[38;5;%s;48;5;%s" % (st["fg"], st["bg"])
        return s + (";1m" if st["bold"] else "m")

    for m in TOKEN.finditer(frame):
        if m.group(1):
            row, col = int(m.group(1)) - 1, int(m.group(2)) - 1
            if not (0 <= row < rows and 0 <= col < cols):
                raise AssertionError("cursor outside screen: %s,%s" % (row + 1, col + 1))
        elif m.group(3) is not None:
            code = m.group(3)
            if code in ("", "0"):
                st.update(fg=None, bg=None, bold=False)
            elif code == "1":
                st["bold"] = True
            elif code == "22":
                st["bold"] = False
            elif code.startswith("38;5;"):
                _, _, fg, _, _, bg = code.split(";")
                st.update(fg=fg, bg=bg)
        elif m.group(4):
            for ch in m.group(4):
                if col >= cols:
                    raise AssertionError("row %d overflows %d cols" % (row + 1, cols))
                grid[row][col] = (ch, current())
                col += 1
    plain = ["".join(c for c, _ in line) for line in grid]
    ansi = ["".join(s + c for c, s in line) + "\x1b[0m" for line in grid]
    return plain, ansi


def make_app(clock):
    backend = meshback.SimBackend(clock=clock)
    app = meshui.App(backend, clock())
    return app, backend


D, U, E = "DOWN", "UP", "\r"
MENU = ["ESC"]
SCRIPTS = {
    "nodes": MENU + [E],
    "chats": MENU + [D, E],
    "channels": MENU + [D, D, E],
    "settings": MENU + [D, D, D, E],
    "radio": MENU + [D, D, D, E, E],
    "device": MENU + [D, D, D, E, D, D, E],
    "position": MENU + [D, D, D, E, D, D, D, E],
    "advanced": MENU + [D, D, D, E, D, D, D, D, D, E],
    "channel": MENU + [D, D, E, E],
    "keydialog": MENU + [D, D, E, E, D, E],
    "prompt": MENU + [D, D, D, E, D, D, E, E],
    "node": MENU + [E, "i"],
    "stats": MENU + [D, D, D, D, D, E],
    "info": MENU + [D, D, D, D, E],
    "help": MENU + [D, D, D, D, D, D, E],
    "share": MENU + [D, D, E, "s"],
}


def scene(name, cols, rows):
    t = [1_800_000_000.0]
    app, backend = make_app(lambda: t[0])
    t[0] += 1
    app.poll(t[0])
    t[0] += 45
    app.poll(t[0])
    for ch in "On my way, ETA 10 min":
        app.key(ch, t[0])
    app.key("\r", t[0])
    t[0] += 3
    app.poll(t[0])
    if name == "chat":
        for ch in "ok":
            app.key(ch, t[0])
    elif name == "nodes":
        app.key("ESC", t[0])
        app.key("\r", t[0])
    elif name == "chats":
        app.key("ESC", t[0])
        app.key("DOWN", t[0])
        app.key("\r", t[0])
    elif name == "menu":
        app.key("ESC", t[0])
        app.key("DOWN", t[0])
    elif name == "scrolled":
        app.render(cols, rows, t[0])
        for _ in range(5):
            app.key("UP", t[0])
    else:
        script = SCRIPTS.get(name)
        if script is None:
            raise SystemExit("unknown screen %r; try: %s" % (name, ", ".join(sorted(["chat", "menu", "scrolled"] + list(SCRIPTS)))))
        for k in script:
            app.key(k, t[0])
    return app.render(cols, rows, t[0])


def main(argv):
    plain_only = "--plain" in argv
    argv = [a for a in argv if a != "--plain"]
    names = [argv[0]] if argv else ["chat"]
    sizes = [tuple(int(x) for x in argv[1].split("x"))] if len(argv) > 1 \
        else [(40, 16), (53, 19), (64, 29)]
    for name in names:
        for cols, rows in sizes:
            plain, ansi = emulate(scene(name, cols, rows), cols, rows)
            print("\x1b[0m-- %s %dx%d --" % (name, cols, rows))
            print("\n".join(plain if plain_only else ansi))
            print()


if __name__ == "__main__":
    main(sys.argv[1:])
