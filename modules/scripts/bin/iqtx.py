#
# MicroPython TUI IQ TX Lab (RF SRAM playback, ESP32-S3)
# Copyright (c) 2026 8bitmcu
# License: MIT (app); the mod_iqtx C module is GPL-3.0-or-later
#
# App-level lab mode over mod_iqtx: preloads a 64-sample IQ circle
# into the RF SRAM bank and plays it back at a native rate as a
# hardware loop. While active this app owns the RF exclusively --
# WiFi and BLE are taken down for the session (the same exclusive-radio
# discipline as sniff), and the vendor CW preparation briefly keys a
# carrier before the NCO clears hand playback to SRAM.
#
# Usage:
#   iqtx                    # ch 6, backoff 60 qdB
#   iqtx 11                 # channel 11
#   iqtx 11 30              # channel 11, RF backoff 30 quarter-dB
#
# Input-line commands:
#   tone 40 | tone 80       # 64-sample circle at native rate (1024-word loop)
#   stop                    # stop playback (keeps the session)
#   reinit                  # deinit+init: restores fresh vendor RF state
#                           # and full TX power after stop/start cycles
#   info                    # caps + status
#   quit (or ESC)           # stop, release RF, restore WiFi
#
# Known behavior (TODO 2026-11-08): TX power is not stable across
# stop/start cycles on the IDF 5.5.1 PHY blob -- use `reinit` before a
# transmission whose power you care about.
#

import sys
import select
import math
import time

import iqtx


CIRCLE_CIRCLES = 16      # 16 x 64 = 1024 words: satisfies 256-alignment


def _pack_tone(amp=64):
    """64-sample IQ rotation -> packed uint32 words, looped x CIRCLE_CIRCLES."""
    one = []
    for n in range(64):
        phase = 2 * math.pi * n / 64
        i = int(round(amp * math.cos(phase))) & 1023
        q = int(round(amp * math.sin(phase))) & 1023
        one.append(i | (q << 10))
    buf = bytearray()
    for _ in range(CIRCLE_CIRCLES):
        for w in one:
            buf += bytes((w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF))
    return buf


def _radio_down():
    """Exclusive RF: BLE off, WiFi stack down. Returns True if WiFi was up."""
    was_up = False
    try:
        import bluetooth
        bluetooth.BLE().active(False)
    except Exception:
        pass
    try:
        import network
        wlan = network.WLAN(network.STA_IF)
        was_up = wlan.active()
        if was_up:
            if wlan.isconnected():
                wlan.disconnect()
            wlan.active(False)
    except Exception:
        pass
    return was_up


def _radio_restore(was_up):
    if was_up:
        try:
            import network
            network.WLAN(network.STA_IF).active(True)
        except Exception:
            pass


def _leave(env, was_up):
    try:
        iqtx.stop()
        iqtx.deinit()
    except Exception:
        pass
    _radio_restore(was_up)
    env.tui.exit_altscreen()
    env.tui.cursor_show()


def main(env, args):
    channel = int(args[0]) if args else 6
    backoff = int(args[1]) if len(args) > 1 else 60

    dim = "\x1b[38;5;244m"
    hi = "\x1b[38;5;210m"
    ok = "\x1b[38;5;114m"
    reset = "\x1b[0m"

    print("[*] iq lab tx: exclusive RF -- wifi/BLE down for the session")
    was_up = _radio_down()
    if was_up:
        print("[i] wifi stack down; restored on quit")

    try:
        iqtx.init(channel=channel, backoff_qdb=backoff)
    except Exception as exc:
        print("[!] init failed: %s" % exc)
        _radio_restore(was_up)
        return

    env.tui.enter_altscreen()
    env.tui.cursor_hide()

    log = env.tui.make_block("", 0, 0,
                             width=env.cols, height=env.rows - 1,
                             fg=252, bg=18,
                             scroll=True, wrap=True)
    line = env.tui.make_input("", 0, env.rows - 1,
                              width=env.cols,
                              fg=252, bg=0, input_bg=0,
                              decorations=False)

    tone = _pack_tone()
    if not iqtx.BANK_PROTECTED:
        log.push("iq lab tx  ch %d  backoff %d qdB\n"
                 "%sRF BANK UNPROTECTED in this build: playback disabled.\n"
                 "init/info work; tone needs a reservation-enabled firmware.%s\n"
                 % (channel, backoff, dim, reset))
    else:
        log.push("iq lab tx  ch %d  backoff %d qdB\n"
                 "tone 40|80  stop  reinit  info  quit\n"
                 % (channel, backoff))
    log.draw()
    env.tui.draw()
    env.tui.cursor_show()

    quit_ = False
    started = time.ticks_ms()
    while not quit_:
        log.draw()
        line.draw()
        env.tui.draw()

        rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
        if sys.stdin not in rlist:
            continue
        char = sys.stdin.read(1)
        if char in ("\r", "\n"):
            cmd = line.value
            line.set("")
            if not cmd:
                continue

            if cmd.startswith("tone "):
                try:
                    mhz = int(cmd.split()[1])
                except (IndexError, ValueError):
                    log.push("--- usage: tone 40 | tone 80 ---\n")
                    continue
                if mhz not in (40, 80):
                    log.push("--- rate must be 40 or 80 ---\n")
                    continue
                try:
                    iqtx.stop()
                    iqtx.rate(mhz * 1000000)
                    n = iqtx.load(tone)
                    iqtx.start()
                    running, loaded, rate, play = iqtx.status()
                    log.push("%stone %d %s  %d words, hw loop, "
                             "expected offset ~%.0f kHz%s\n"
                             % (hi, mhz, "ON" if running else "off", n,
                                rate / 64000, reset))
                except Exception as exc:
                    log.push("--- tone failed: %s ---\n" % exc)

            elif cmd == "stop":
                iqtx.stop()
                log.push("%splayback stopped%s\n" % (ok, reset))

            elif cmd == "reinit":
                iqtx.deinit()
                try:
                    iqtx.init(channel=channel, backoff_qdb=backoff)
                    log.push("%sreinit ok -- fresh vendor RF state, full power%s\n"
                             % (ok, reset))
                except Exception as exc:
                    log.push("--- reinit failed: %s ---\n" % exc)
                    _leave(env, was_up)
                    return

            elif cmd == "info":
                chip, maxn, align, rates, rate = iqtx.caps()
                running, loaded, cur_rate, play = iqtx.status()
                log.push("ch %d  backoff %d qdB  max %d  align %d  "
                         "rate %d Hz  loaded %d  running %s  PLAY 0x%08x\n"
                         % (channel, backoff, maxn, align, cur_rate,
                            loaded, running, play))

            elif cmd == "quit":
                quit_ = True
            else:
                log.push("--- unknown: tone 40|80, stop, reinit, info, quit ---\n")

        elif char in ("\x08", "\x7f"):
            line.backspace()
        elif char == "\x1b":
            quit_ = True
        else:
            line.push(char)

    _leave(env, was_up)
    print("[*] iq lab tx done, RF released, %.1fs"
          % (time.ticks_diff(time.ticks_ms(), started) / 1000.0))
