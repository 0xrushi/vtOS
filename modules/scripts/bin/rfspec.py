#
# MicroPython sub-GHz spectrum analyzer (SX1262, passive RX)
# License: MIT
#
# Sweeps the LoRa chip across a band and plots channel power as a live trace
# plus a waterfall, so you can see which LoRa channels are busy, how high the
# noise floor sits and whether an antenna works. Receive only: nothing is
# ever transmitted. Takes the radio exclusively, like loramon and meshchat.
#
# Usage:
#   rfspec                      # the band of your Meshtastic region
#   rfspec 868 30               # centre 868 MHz, span 30 MHz
#   rfspec 433.9 5 62.5         # also set the resolution bandwidth (kHz)
#   rfspec --demo               # a fake band, no radio
#
# Keys: left/right pan, up/down span, b resolution bandwidth, p pause,
# h peak hold on/off, c clear peak hold, q or trackball click to quit.
#
# The radio can only measure inside its own channel width (62.5-500 kHz), so
# the view is a stepped sweep, not a true analyzer: narrow or short signals
# between steps are missed. The SX1262 tunes 150-960 MHz; your antenna and
# matching decide how much of that is useful.
#

import select
import sys

import rfscan


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
    return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(sys.stdin.read(1), "")


def _settings():
    try:
        import mtconfig
        return mtconfig.Settings.load()
    except Exception:
        return None


def _open_radio(env, cfg, freq, rbw, demo):
    """(radio, error). Reuses env.radio when another app left one."""
    if demo:
        return rfscan.SimRadio(), ""
    import meshback
    radio = getattr(env, "radio", None)
    try:
        if radio is None:
            radio = meshback._make_radio(env)
            env.radio = radio
    except Exception as e:
        return None, "no radio: %s" % e
    if not hasattr(radio, "sweep"):
        return None, "needs the Python SX126x driver (rebuild vtOS)"
    ok, msg = rfscan.start_radio(radio, cfg, freq, rbw)
    if not ok:
        return None, msg
    return radio, ""


def _release(env):
    radio = getattr(env, "radio", None)
    if radio is not None:
        try:
            radio.deinit()
        finally:
            env.radio = None


def main(env, args):
    demo = "--demo" in args
    nums = []
    for a in args:
        if not a.startswith("--"):
            try:
                nums.append(float(a))
            except ValueError:
                print("usage: rfspec [centre_mhz [span_mhz [rbw_khz]]] [--demo]")
                return
    cfg = _settings()
    center, span = rfscan.default_view(cfg)
    if nums:
        center = nums[0]
    if len(nums) > 1:
        span = nums[1]
    rbw = nums[2] if len(nums) > 2 else 125.0
    if rbw not in rfscan.RBWS:
        print("rbw must be one of: " + " ".join("%g" % r for r in rfscan.RBWS))
        return

    screen = None
    if getattr(env, "tft", None) is not None and hasattr(env, "app_display"):
        try:
            from rfscreen import Screen
            screen = Screen(env)
        except (ImportError, MemoryError):
            pass  # use the terminal on builds without framebuf or enough RAM

    width = screen.bins if screen is not None else env.cols - rfscan.LEFT_MARGIN
    scan = rfscan.Scan(center, span, rbw, width)
    radio, err = _open_radio(env, cfg, scan.center, scan.rbw, demo)
    if radio is None:
        print("[!] " + err)
        _release(env)
        return
    env.app_uses_radio = not demo

    out = sys.stdout
    note = ""
    redraw = True
    try:
        env.tui.enter_altscreen()
        env.tui.cursor_hide()
        out.write("\x1b[0m\x1b[2J")
        if screen is not None:
            env.app_display = screen
        while True:
            if not scan.paused:
                scan.add(radio.sweep(scan.freqs()))
                redraw = True
            if screen is not None:
                if (redraw or screen.dirty) and screen.visible():
                    screen.draw(scan, note)
                    redraw = False
            elif redraw:
                out.write(rfscan.render(scan, env.cols, env.rows, note))
                redraw = False
            # a real sweep already takes ~0.2 s; the fake band needs a pause
            wait = 0.2 if (demo or scan.paused) else 0.0
            if sys.stdin in select.select([sys.stdin], [], [], wait)[0]:
                try:
                    key = _read_key()
                except UnicodeError:
                    continue
                act = scan.key(key)
                redraw = True
                if act == "quit":
                    return
                if act == "rbw":
                    ok, msg = rfscan.start_radio(radio, cfg, scan.center, scan.rbw)
                    note = "" if ok else "RBW change failed: " + msg
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[0m")
        env.tui.exit_altscreen()
        env.tui.cursor_show()
        if screen is not None:
            env.app_display = None
            panel = env.quickpanel
            if panel is None or not panel.opened:
                env.term.top_offset(env.status_height)  # redraw the area covered by the app
        if not demo:
            _release(env)
