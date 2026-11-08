#
# MicroPython BLE Scanner (passive GAP scan + fingerprint table)
# Copyright (c) 2026 8bitmcu
# License: MIT
#
# Passive BLE advertisement scan: address, RSSI, connectable flag,
# advertised name, and 16-bit company ID from manufacturer-specific
# AD structures for each nearby device. Mirrors the recon side of
# upstream BLE tooling (scanning only -- it does not advertise, spam
# or connect).
#
# Usage:
#   blescan                # 10 second scan
#   blescan 30             # 30 second scan
#
# Requires BLE radio exclusivity: blechat/mesh transport is assumed idle.
#

import bluetooth
import time
import gc

DEFAULT_SCAN_S = 10
SCAN_INTERVAL_US = 200000       # 200ms between scan windows
SCAN_WINDOW_US = 200000         # window == interval: continuous duty

# Small subset of SIG 16-bit company IDs seen in the wild.
COMPANY_NAMES = {
    0x004C: "Apple",
    0x00E0: "Google",
    0x0006: "Microsoft",
    0x0075: "Samsung",
    0xFFFF: "mesh/reserved",
}

_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_DONE = 6


def _parse_records(adv):
    """Walk AD structures; pull (name, company_id, ad_bytes)."""
    name = None
    company = None
    i = 0
    n = len(adv)
    while i + 1 < n:
        length = adv[i]
        if length == 0:
            break
        if i + 1 + length > n:
            break  # truncated structure
        atype = adv[i + 1]
        body = adv[i + 2: i + 1 + length]
        if atype == 0x08 or atype == 0x09:  # short/complete local name
            try:
                name = str(body, "utf-8") if name is None else name
            except UnicodeError:
                pass
        elif atype == 0xFF and len(body) >= 2:  # manufacturer-specific data
            company = body[0] | (body[1] << 8)
        i += 1 + length
    return name, company


class Scanner:
    """One device per row; later advertisements merge/refresh."""

    def __init__(self, ble):
        self.ble = ble
        self.ble.irq(self._irq)
        self.seen = {}  # addr -> [rssi_min_rssi? keep max, name, company, count, connectable]
        self.done = False
        self.latest = {}

    def start(self):
        self.ble.gap_scan(0, SCAN_INTERVAL_US, SCAN_WINDOW_US, False)

    def stop(self):
        self.ble.gap_scan(None)
        try:
            self.ble.active(False)
        except Exception:
            pass

    def _irq(self, event, data):
        if event == _IRQ_SCAN_RESULT:
            # 6-tuple with active scan (resp_data present), 5-tuple on
            # passive scan (some builds omit resp_data) -- index, never
            # fixed-arity unpack, and never raise out of the NimBLE
            # callback (an exception here resets the chip).
            addr_type, addr = data[0], data[1]
            adv_type = data[2]
            rssi = data[3]
            adv_data = bytes(data[4]) if len(data) > 4 else b""
            resp = bytes(data[5]) if len(data) > 5 else b""
            key = (addr_type, bytes(addr))
            name, company = _parse_records(adv_data)
            rname, rcompany = _parse_records(resp)
            name = rname or name
            company = rcompany if rcompany is not None else company
            row = self.seen.get(key)
            if row is None:
                row = [rssi, name, company, 1, adv_type in (0, 2)]
                self.seen[key] = row
            else:
                row[0] = max(row[0], rssi)  # print best RSSI seen
                row[3] += 1
                if not row[1] and name:
                    row[1] = name
                if row[2] is None and company is not None:
                    row[2] = company
        elif event == _IRQ_SCAN_DONE:
            self.done = True


def main(env, args):
    seconds = int(args[0]) if args else DEFAULT_SCAN_S

    ble = bluetooth.BLE()
    if not ble.active():
        ble.active(True)

    sc = Scanner(ble)
    print("[*] passive BLE scan for %ds..." % seconds)
    sc.start()
    deadline = time.time() + seconds
    last_count = -1
    try:
        while time.time() < deadline and not sc.done:
            time.sleep_ms(100)
            if len(sc.seen) != last_count:
                last_count = len(sc.seen)
    finally:
        sc.stop()

    rows = sorted(sc.seen.items(), key=lambda kv: (kv[1][0], kv[0]))
    if not rows:
        print("[*] no devices seen")
        return
    print("%-13s %5s  %-4s %-9s %s" % ("addr", "rssi", "cnct", "company", "name"))
    for (addr_type, addr), (rssi, name, company, count, connectable) in rows:
        cs = COMPANY_NAMES.get(company, "0x%04X" % company if company is not None else "-")
        a = "%02X:%02X:%02X:%02X:%02X:%02X" % tuple(addr)
        if count > 1:
            cs += " x%d" % count if cs == "-" else " (%d)" % count
        print("%-13s %5d  %-4s %-9s %s" % (a, rssi,
              "yes" if connectable else "no", cs, name or ""))
    print("[*] %d device(s), scan done" % len(rows))
    gc.collect()
