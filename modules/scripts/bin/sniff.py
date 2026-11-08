#
# MicroPython 802.11 Sniffer (passive capture to PCAP)
# Copyright (c) 2026 8bitmcu
# License: MIT
#
# Passive RF monitoring for networks you own or are authorized to
# assess. Captures management/control/data frames on a locked or
# hopping channel and saves a libpcap file (LINKTYPE_IEEE802_11) for
# Wireshark. It takes exclusive radio control: BLE and nm connections
# are dropped for the duration of the capture.
#
# Usage:
#   sniff                      # defaults: ch 6 mgmt-only, 10s
#   sniff 11                   # lock channel 11
#   sniff 11 30                # 30 second capture
#   sniff 11 0 all             # capture every type, n until Ctrl+C
#   sniff 1 0 mgmt 200         # hop every 200ms across ch 1-11
#
# Positional args: [channel] [seconds] [all|mgmt|data|ctrl] [hop_ms]
#

import network
import time
import gc

import modwifi
# lib/pcap.py is frozen as a flat top-level module (see manifest.py
# -- freeze(base_path + "/scripts/lib")), matching epubfmt/epubzip.
import pcap

FILTER_MAP = {"mgmt": modwifi.FILT_MGMT,
              "data": modwifi.FILT_DATA,
              "ctrl": modwifi.FILT_CTRL}
FILTER_ALL = 1 | 2 | 4

DRAIN_DEFAULT_BYTES = 16384  # 16 KB: fits 60+ records per drain
DEFAULT_CAPTURE_S = 10
DEFAULT_HOP_MS = 0           # no hop -- lock a single channel
HIGHEST_CHAN = 11            # keep inside the indoor band


def _storage_root():
    import os
    for root in ("/sd", "/flash"):
        try:
            os.stat(root)
            return root
        except OSError:
            continue
    raise OSError("no writable filesystem (mount /sd or /flash)")


def main(env, args):
    channel = 6
    seconds = DEFAULT_CAPTURE_S
    filt = modwifi.FILT_MGMT
    hop_ms = DEFAULT_HOP_MS
    hop_max = HIGHEST_CHAN

    # positional args: [channel] [seconds] [type] [hop_ms]
    if args:
        channel = int(args[0])
    if len(args) > 1:
        seconds = int(args[1])
    if len(args) > 2:
        word = args[2].lower()
        if word not in FILTER_MAP and word != "all":
            print("type must be mgmt | data | ctrl | all")
            return
        filt = FILTER_ALL if word == "all" else FILTER_MAP[word]
    if len(args) > 3:
        hop_ms = int(args[3])

    # Full-type captures see 1500-byte data frames: capture them whole
    # (ring spills to PSRAM if needed). Mgmt-only sticks to 256 so the
    # ring stays in internal SRAM.
    if filt == FILTER_ALL:
        frame_len = 1600
    else:
        frame_len = 256

    if seconds == 0:
        print("capture until Ctrl+C; press Ctrl+C to stop and save")

    # Exclusive radio: BLE mesh + nm connections go down for the
    # capture. BLE stays off afterward -- the app can stay up.
    try:
        import bluetooth
        bluetooth.BLE().active(False)
        print("[i] BLE off for the capture duration")
    except Exception:
        pass

    def _wifi_on():
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        return wlan

    print("[*] writing to %s" % _storage_root())

    wlan = _wifi_on()
    # STA connecting to an AP breaks channel-lock capture: drop it.
    if wlan.isconnected():
        wlan.disconnect()
        print("[i] dropped sta connection")

    drain = bytearray(DRAIN_DEFAULT_BYTES)

    pcap_path = None
    writer = None
    n_pkts = 0
    started = time.time()

    try:
        modwifi.sniff_start(channel=channel, filter=filt,
                            slots=96, frame_len=frame_len,
                            hop_ms=hop_ms, hop_max=hop_max)
        mode_word = "hop 1-%d every %dms" % (hop_max, hop_ms) if hop_ms else "ch %d" % channel
        type_word = "mgmt+data+ctrl" if filt == FILTER_ALL else \
            next(k for k, v in FILTER_MAP.items() if v == filt)
        print("[*] capturing %s frames -- %s" % (type_word, mode_word))

        stopped = False
        next_report = started + 2
        while not stopped:
            n = modwifi.sniff_read(drain)
            if n:
                if writer is None:
                    pcap_path = "%s/sniff-%d.pcap" % (_storage_root(), time.time())
                    writer = pcap.PcapWriter(pcap_path)
                for _ln, _ch, _rssi, payload in pcap.iter_records(drain, n):
                    writer.write(payload)
                    n_pkts += 1
            now = time.time()
            if now >= next_report:
                capt, dropped, skipped, psram = modwifi.sniff_stats()
                print("  ch %2d  %5d pkts  drop %4d  skip %4d%s"
                      % (modwifi.sniff_channel(), capt, dropped, skipped,
                         "  [psram]" if psram else ""))
                next_report = now + 2
            if seconds and (now - started) >= seconds:
                stopped = True
                break
            time.sleep_ms(20)
    finally:
        modwifi.sniff_stop()
        if writer is not None:
            writer.close()
        if pcap_path:
            print("[*] saved %s (%d frames, %.1fs)"
                  % (pcap_path, n_pkts, time.time() - started))
        elif seconds:
            print("[*] nothing captured, no file written")
        gc.collect()
    return
