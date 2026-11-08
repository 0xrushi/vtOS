#
# MicroPython TUI LoRa Monitor (RX dump + conforming TX)
# Copyright (c) 2026 8bitmcu
# License: MIT
#
# Lists packets decoded by the SX1262 as they land: index, length,
# timestamp, RSSI, SNR and a printable preview. Transmit is opt-in and
# conforming: plain text on the configured frequency/power, driven
# through the same lora.LoRa native module as lorachat. Radio settings
# (freq, bw, sf, cr, power) come from loracfg's /flash/.radio.json;
# /freq retunes additionally at runtime.
#
# Usage:
#   loramon                     # monitor using .radio.json
#
# Input-line commands:
#   /tx <text>                  transmit text on the current frequency
#   /freq <mhz> [bw] [sf] [cr] [pwr]
#                               retune; e.g. /freq 869.525 125 10 6 10
#   /clear                      wipe the packet log
#   /quit (or ESC)              leave; releases the radio
#

import select
import sys
import time
import json


def _leave(env):
    """Release the LoRa radio's SPI device handle and heap-allocated
    RadioLib/HAL objects on the way out (see lora.LoRa.deinit's docstring
    in lora.cpp). Matches lorachat's teardown-on-exit pattern."""
    env.radio.deinit()
    env.radio = None
    env.tui.exit_altscreen()
    env.tui.cursor_show()


def _preview(pkt, limit=48):
    """Decode if printable text, else a hex dump prefix."""
    try:
        return pkt.decode("utf-8")
    except UnicodeError:
        return "<%s>" % " ".join("%02X" % b for b in pkt[:limit])


def _stamp(ms):
    return "%3d.%03d" % (ms // 1000, ms % 1000)


def main(env, args):
    dim = "\x1b[38;5;244m"
    rx_col = "\x1b[38;5;45m"
    tx_col = "\x1b[38;5;210m"
    reset = "\x1b[0m"

    with open("/flash/.radio.json", "r") as f:
        cfg = json.load(f)
    power = int(cfg.get("pwr", 10))
    freq = float(cfg["freq"])

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

    started = time.ticks_ms()
    counters = {"rx": 0, "tx_ok": 0, "bytes": 0}
    log.push("loramon  %.3f MHz  bw/sf/cr from .radio.json  pwr %d dBm\n"
             "/tx /freq [mhz] [bw] [sf] [cr] [pwr]  /clear  /quit\n"
             % (freq, power))
    log.draw()
    env.tui.draw()
    env.tui.cursor_show()
    env.radio.start_receive()

    quit_ = False
    while not quit_:
        log.draw()
        line.draw()
        env.tui.draw()

        rlist, _, _ = select.select([sys.stdin], [], [], 0.1)

        # SX1262 has no file descriptor: poll every iteration like
        # lorachat. DIO1 gate inside receive() avoids re-reading the
        # same packet; chip returns to standby after read -> re-arm.
        pkt = env.radio.receive()
        if pkt:
            counters["rx"] += 1
            counters["bytes"] += len(pkt)
            rssi = env.radio.rssi()
            try:
                snr = env.radio.snr()
                snr_word = "%+5.1f dB" % snr
            except (AttributeError, RuntimeError):
                snr_word = "  --  "  # firmware without the snr() method
            stamp = _stamp(time.ticks_diff(time.ticks_ms(), started))
            log.push("%s%s  len %3d  %5d dBm  %s  %s%s\n"
                     % (rx_col, stamp, len(pkt), rssi, snr_word,
                        _preview(pkt), reset))
            env.radio.start_receive()

        if sys.stdin in rlist:
            char = sys.stdin.read(1)
            if char in ("\r", "\n"):
                text = line.value
                if not text:
                    continue
                if text.startswith("/tx "):
                    payload = text.split(" ", 1)[1]
                    stamp = _stamp(time.ticks_diff(time.ticks_ms(), started))
                    try:
                        env.radio.transmit(payload.encode("utf-8"))
                        counters["tx_ok"] += 1
                        counters["bytes"] += len(payload)
                        log.push("%s%s  TX %3d B  %r%s\n"
                                 % (tx_col, stamp, len(payload), payload, reset))
                    except RuntimeError as exc:
                        log.push("--- TX failed: %s ---\n" % exc)
                    finally:
                        # transmit() ends in standby (win or lose): re-arm
                        env.radio.start_receive()
                    line.set("")
                elif text.startswith("/freq"):
                    parts = text.split()[1:]
                    stamp = _stamp(time.ticks_diff(time.ticks_ms(), started))
                    try:
                        freq = float(parts[0])
                        bw = float(parts[1]) if len(parts) > 1 else 125.0
                        sf = int(parts[2]) if len(parts) > 2 else 10
                        cr = int(parts[3]) if len(parts) > 3 else 6
                        pwr = int(parts[4]) if len(parts) > 4 else power
                    except (IndexError, ValueError):
                        log.push("--- usage: /freq <mhz> [bw] [sf] [cr] [pwr] ---\n")
                        continue
                    try:
                        env.radio.begin(freq=freq, bw=bw, sf=sf, cr=cr,
                                        sync_word=0x12, power=pwr)
                        power = pwr
                        log.push("--- retuned: %.3f MHz bw %.1f sf %d cr %d pwr %d dBm ---\n"
                                 % (freq, bw, sf, cr, power))
                    except RuntimeError as exc:
                        log.push("--- retune failed: %s ---\n" % exc)
                    env.radio.start_receive()
                    line.set("")
                elif text == "/clear":
                    log.push("\n")
                    line.set("")
                elif text == "/quit":
                    quit_ = True
                else:
                    log.push("--- unknown: /tx /freq /clear /quit ---\n")
                    line.set("")
            elif char in ("\x08", "\x7f"):
                line.backspace()
            elif char == "\x1b":
                quit_ = True
            else:
                line.push(char)

    _leave(env)
    print("[*] rx %d pkt(s), tx %d, %d B, %.1fs"
          % (counters["rx"], counters["tx_ok"], counters["bytes"],
             time.ticks_diff(time.ticks_ms(), started) / 1000.0))
