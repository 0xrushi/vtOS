#
# MicroPython Meshtastic chat TUI
# License: MIT
#
# Usage: meshchat [name] [--demo] [--raw] [--meshcore]
#
# A real Meshtastic node on the SX1262: encrypted channels, direct messages,
# acks, relaying, node list, position and telemetry, plus a settings TUI for
# region, preset, channels and device options. Region and everything else lives
# in /flash/.mesh.json (Menu > Settings). `name` sets the node's long name on
# first use.
#
# --raw   speak lorachat's plain "nick: text" LoRa format instead
# --meshcore  MeshCore client instead of Meshtastic (contacts, repeater login; /help)
# --demo  a scripted fake mesh (also the fallback when the radio can't start)
#
# Trackball: left/right switch chats, up/down scroll, click opens the menu
# (or goes back), long press quits. Type to compose, Enter sends. /help lists
# the slash commands. All rendering lives in lib/meshui.py.
#

import json
import random
import select
import sys
import time

import meshback
import meshui

STATE_FILE = "/flash/.meshchat.json"
DRAW_INTERVAL_MS = 50   # back-to-back redraws once tripped the watchdog (see irc.py)
TICK_MS = 1000          # relative times and notices expire on this beat
ANIM_MS = 250           # faster beat while the radar sweep is on screen


def _read_key(timeout_ms=40):
    """One keypress; trackball arrows become 'UP' etc., a bare click 'ESC'."""
    ch = sys.stdin.read(1)
    if ch != "\x1b":
        return ch
    try:
        poller = select.poll()
        poller.register(sys.stdin, select.POLLIN)
        if not poller.poll(timeout_ms):
            return "ESC"
    except (OSError, AttributeError):
        pass
    if sys.stdin.read(1) != "[":
        return "ESC"
    seq = sys.stdin.read(1)
    return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(seq, "")


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


def _default_nick():
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "vtOS_" + "".join(random.choice(chars) for _ in range(4))


def main(env, args):
    state = _load_state()
    names = [a for a in args if not a.startswith("--")]
    nick = (names[0] if names else state.get("nick") or _default_nick())[:16]
    state["nick"] = nick

    backend, note = meshback.open_backend(env, "--demo" in args, nick, "--raw" in args,
                                          "--meshcore" in args)
    # The quick panel refuses to switch LoRa off while an app owns the radio;
    # shell._app restores the previous value when we return.
    env.app_uses_radio = not backend.demo
    app = meshui.App(backend, time.time())
    if state.get("last") in app.convs:
        app.cur = state["last"]
    if note:
        app.notify(note, meshui.YELLOW, time.time())

    env.tui.enter_altscreen()
    env.tui.cursor_hide()
    out = sys.stdout
    out.write("\x1b[0m\x1b[2J")

    drawn_at = -DRAW_INTERVAL_MS
    ticked_at = time.ticks_ms()
    dirty = True
    radar = None
    radar_pixels = getattr(env, "tft", None) is not None and hasattr(env, "app_display")
    try:
        while True:
            now = time.time()
            if app.poll(now):
                dirty = True
            radar_mode = app.animating()
            if time.ticks_diff(time.ticks_ms(), ticked_at) >= (ANIM_MS if radar_mode else TICK_MS):
                ticked_at = time.ticks_ms()
                dirty = True

            if radar is not None and not radar_mode:
                restore_terminal(env)
                radar = None
                dirty = True
            elif radar_mode and radar_pixels and radar is None:
                try:
                    from pixelui import restore_terminal
                    from radarscreen import Screen
                    radar = Screen(env)
                    env.app_display = radar
                    dirty = True
                except (ImportError, MemoryError):
                    radar_pixels = False

            if radar is not None and radar.dirty:
                dirty = True
            if dirty and time.ticks_diff(time.ticks_ms(), drawn_at) >= DRAW_INTERVAL_MS:
                if radar is None:
                    out.write(app.render(env.cols, env.rows, now))
                else:
                    radar.draw(app, now)
                drawn_at = time.ticks_ms()
                dirty = False

            rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
            if sys.stdin in rlist:
                try:
                    key = _read_key()
                except UnicodeError:
                    continue
                if app.key(key, time.time()) == "quit":
                    return
                dirty = True
    except KeyboardInterrupt:
        pass
    finally:
        if radar is not None:
            restore_terminal(env)
        state["last"] = app.cur
        _save_state(state)
        backend.close()
        out.write("\x1b[0m")
        env.tui.exit_altscreen()
        env.tui.cursor_show()
