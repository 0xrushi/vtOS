#
# MicroPython 2.4 GHz Spectrum Profiler (passive RX)
# Copyright (c) 2026 8bitmcu
# License: MIT
#
# Sweeps channels 1..N with the modwifi promiscuous capture ring and
# renders a live per-channel activity meter: frame counts, type split,
# peak RSSI and unique-BSSID AP counts. Receive only -- it never
# transmits. Takes exclusive radio control like sniff: BLE and any STA
# connection are dropped for the duration.
#
# Usage:
#   spectrum                    # sweep ch 1-11, 250ms dwell, until q/Ctrl+C
#   spectrum 30                 # run 30 seconds, then summarize
#   spectrum 0 100              # 100ms dwell
#   spectrum 0 250 13           # include ch 12-13
#
# Positional args: [seconds] [hop_ms] [hop_max]
#

import network
import sys
import select
import time
import gc

import modwifi

HIGHEST_CHAN = 11           # indoor band top, matches sniff
DEFAULT_HOP_MS = 250
DRAIN_BYTES = 16384         # same drain size as sniff: 60+ records each
BSSID_CAP = 64              # unique AP IDs tracked per channel
REDRAW_MS = 200
MIN_COLS_TWO = 38           # below this, draw one channel column only


def _signed(v):
    """u8 slot RSSI -> signed dBm."""
    return v - 256 if v >= 128 else v


def _new_ch():
    # count, max_rssi, mgmt, data, ctrl, unique-BSSID set
    return [0, -128, 0, 0, 0, set()]


def _fmt_rssi(v):
    return "%4d" % v if v > -128 else "  --"


def _record(drain, n, chans, overflow):
    """Walk sniff_read's packed records: [u16 len][u8 ch][u8 rssi][payload]."""
    pos = 0
    cap = len(drain)
    for _ in range(n):
        if pos + 4 > cap:
            break
        flen = drain[pos] | (drain[pos + 1] << 8)
        ch = drain[pos + 2]
        rssi = _signed(drain[pos + 3])
        end = pos + 4 + flen
        if flen < 1 or end > cap:
            if end > cap:
                break
            pos = end
            continue
        slot = chans[ch] if ch < len(chans) else None
        if slot is None:
            overflow[0] += 1
        else:
            slot[0] += 1
            if rssi > slot[1]:
                slot[1] = rssi
            ftype = (drain[pos + 4] >> 2) & 0x3
            if ftype == 0:
                slot[2] += 1
                # mgmt A2 (transmitter address) sits at payload 10..15
                if flen >= 16 and len(slot[5]) < BSSID_CAP:
                    slot[5].add(bytes(drain[pos + 14:pos + 20]))
            elif ftype == 2:
                slot[3] += 1
            else:
                slot[4] += 1
        pos = end


def _bar(count, busiest, width):
    """ASCII activity bar, scaled against the busiest channel."""
    n = (count * width + busiest - 1) // busiest if (busiest > 0 and width > 0) else 0
    n = min(n, width)
    return "#" * n + "." * (width - n)


def _draw_frame(env, hop_ms, started, hop_max, chans, stats, overflow):
    capt, dropped, skipped, psram = stats
    dur = time.ticks_diff(time.ticks_ms(), started) / 1000.0
    busiest = 0
    busy_n = 0
    for c in chans[1:]:
        if c[0]:
            busy_n += 1
        busiest = max(busiest, c[0])

    bars = 12
    lines = ["\x1b[H\x1b[0m2.4 GHz RX  hop %4dms  %6.1fs  %d/%d ch busy"
             % (hop_ms, dur, busy_n, hop_max)]
    half = (hop_max + 1) // 2
    two_col = env.cols >= MIN_COLS_TWO
    col_w = bars + 16
    for i in range(half):
        left = chans[i + 1]
        line = "ch%-2d %-7d%s %s %2d" % (
            i + 1, left[0], _bar(left[0], busiest, bars),
            _fmt_rssi(left[1]), len(left[5]))
        if two_col:
            j = i + 1 + half
            if j <= hop_max:
                right = chans[j]
                pad = col_w - len(line) - 4
                if pad < 1:
                    pad = 1
                line += " " * pad + "ch%-2d %-7d%s %s %2d" % (
                    j, right[0], _bar(right[0], busiest, bars),
                    _fmt_rssi(right[1]), len(right[5]))
        lines.append(line)
    lines.append("drop %d  skip %d%s  oc %d" % (
        dropped, skipped, "  psram" if psram else "", overflow[0]))
    # \x1b[K erases each line's tail so shorter bars never leave residue
    sys.stdout.write("\r\n".join(l + "\x1b[K" for l in lines) + "\r\n")


def main(env, args):
    seconds = int(args[0]) if args else 0
    hop_ms = int(args[1]) if len(args) > 1 else DEFAULT_HOP_MS
    hop_max = int(args[2]) if len(args) > 2 else HIGHEST_CHAN
    if hop_ms < 20:
        hop_ms = 20
    if hop_max < 1:
        hop_max = 1

    print("[*] 2.4 GHz spectrum: RX only, exclusive radio -- sweep 1-%d, %d ms dwell"
          % (hop_max, hop_ms))
    print("[i] q or Ctrl+C to stop and summarize")

    try:
        import bluetooth
        bluetooth.BLE().active(False)
    except Exception:
        pass

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        wlan.disconnect()
        print("[i] dropped sta connection")

    # index 0 unused so record channel == index; ch beyond hop_max lands
    # in overflow, not a fake channel row
    chans = [_new_ch() for _ in range(hop_max + 1)]
    overflow = [0]
    drain = bytearray(DRAIN_BYTES)

    n_records = 0
    started = time.ticks_ms()
    env.tui.enter_altscreen()
    env.tui.cursor_hide()
    sys.stdout.write("\x1b[2J")  # first frame: wipe the alt screen
    try:
        modwifi.sniff_start(channel=1, filter=modwifi.FILT_ALL,
                            slots=96, frame_len=256,
                            hop_ms=hop_ms, hop_max=hop_max)
        running = True
        next_frame = started
        while running:
            n = modwifi.sniff_read(drain)
            if n:
                _record(drain, n, chans, overflow)
                n_records += n

            now = time.ticks_ms()
            if time.ticks_diff(now, next_frame) >= 0:
                next_frame = time.ticks_add(now, REDRAW_MS)
                _draw_frame(env, hop_ms, started, hop_max, chans,
                            modwifi.sniff_stats(), overflow)
            if seconds and time.ticks_diff(now, started) >= seconds * 1000:
                running = False
            if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                if sys.stdin.read(1) in ("q", "\x03"):
                    running = False
    except KeyboardInterrupt:
        pass
    finally:
        modwifi.sniff_stop()
        env.tui.exit_altscreen()
        env.tui.cursor_show()

    dur = time.ticks_diff(time.ticks_ms(), started) / 1000.0
    print("[*] %d frames in %.1fs" % (n_records, dur))
    rows = [(ch, c) for ch, c in enumerate(chans) if ch and c[0]]
    rows.sort(key=lambda kv: -kv[1][0])
    if rows:
        busiest = rows[0][1][0]
        print("ch  pkts   m/d/c      rssi  ap")
        for ch, (count, rssi, mgmt, data, ctrl, aps) in rows[:8]:
            print("%2d %5d  %3d/%2d/%2d  %s  %2d%s"
                  % (ch, count, mgmt, data, ctrl, _fmt_rssi(rssi), len(aps),
                     " +" if count == busiest else ""))
    capt, dropped, skipped, psram = modwifi.sniff_stats()
    note = []
    if dropped:
        note.append("drop %d" % dropped)
    if skipped:
        note.append("skip %d" % skipped)
    if psram:
        note.append("psram")
    if overflow[0]:
        note.append("och %d" % overflow[0])
    if note:
        print("[i] " + "  ".join(note))
    gc.collect()
