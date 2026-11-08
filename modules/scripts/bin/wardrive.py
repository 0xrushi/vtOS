#
# MicroPython Wardrive (GPS-tagged WiFi scan -> WiGLE CSV + KML)
# Copyright (c) 2026 8bitmcu
# License: MIT
#
# GPS-tagged passive AP scan logging, in the WiGLE CSV format with a
# KML export converter for Google Earth. Designed for the T-Deck Plus
# (GPS on the UART0 pins -- GPIO 43/44). On a vanilla T-Deck there is
# no GPS hardware: the app exits with a clear error after a short
# NMEA-listen timeout.
#
# Usage:
#   wardrive <name> [interval_sec]     # run a session (default 3s)
#   wardrive kml <name>                # convert <name>.csv -> <name>.kml
#
# Session output: /sd/<name>.csv (falls back to /flash).
# Treats the radio exclusively: BLE stays off, STA is disconnected for
# the duration (same deal as the sniff app).
#

import machine
import network
import time
import gc
import os
import ustruct as struct

GPS_UART_ID = 1
GPS_BAUD = 9600
GPS_RX_PIN = 43   # GPS TX -> ESP32 RX (UART0 pins on T-Deck Plus)
GPS_TX_PIN = 44
GPS_LISTEN_TIMEOUT_S = 10   # no NMEA bytes at all => likely not a Plus
FIX_REQUIRED = True         # rows are only logged when a fix is valid

EPOCH7 = 946684800          # 2000-01-01 00:00:00 UTC, GPS era start


def _storage_root():
    for root in ("/sd", "/flash"):
        try:
            os.stat(root)
            return root
        except OSError:
            continue
    raise OSError("no writable filesystem (mount /sd or /flash)")


class Gps:
    """Minimal NMEA listener: GGA (fix/lat/lon/alt/sats) and RMC (UTC
    date/time). On the Plus the GPS streams continuously; vanilla
    boards deliver nothing, which the listen timeout detects."""

    def __init__(self):
        self.uart = machine.UART(GPS_UART_ID, baudrate=GPS_BAUD,
                                 rx=GPS_RX_PIN, tx=GPS_TX_PIN, timeout=0)
        self._buf = ""      # partial line accumulator (never blocks)
        self.lat = None     # signed decimal degrees
        self.lon = None
        self.alt = None     # meters
        self.sats = 0
        self.hdop = None
        self.fix = False
        self.utc = None     # 'YYYY-MM-DD HH:MM:SS'
        self.bytes_seen = 0
        self.lines_seen = 0  # only VALID NMEA sentences count
        # Floating console pins on a vanilla T-Deck deliver the odd
        # stray byte; detection must key on parseable $Gx sentences,
        # not raw traffic.

    def _sentence(self):
        # readline() can block indefinitely on a silent line on some
        # firmware builds, so drain with read() and reassemble lines.
        data = self.uart.read(self.uart.any() or 1)
        if not data:
            return
        self.bytes_seen += len(data)
        try:
            self._buf += str(data, "ascii")
        except ValueError:
            self._buf = ""
            return
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)[0], \
                self._buf.split("\n", 1)[1]
            self._handle_line(line.strip())

    def _handle_line(self, line):
        if len(line) < 10 or line[0] != "$":
            return
        self.lines_seen += 1
        # checksum strip: '$GPGGA,...*hh'
        if "*" in line:
            line = line.split("*")[0]
        parts = line.split(",")
        talker = parts[0][:6]
        if talker in ("$GPGGA", "$GNGGA", "$GAGGA", "$GBGGA"):
            self._gga(parts)
        elif talker in ("$GPRMC", "$GNRMC", "$GARMC", "$GBRMC"):
            self._rmc(parts)

    def _dms(self, val, hemi):
        # ddmm.mmmm -> signed decimal degrees
        if not val:
            return None
        deg = int(float(val) / 100)
        minutes = float(val) - deg * 100
        out = deg + minutes / 60
        if hemi in ("S", "W"):
            out = -out
        return out

    def _gga(self, p):
        if len(p) < 10:
            return
        if p[6] in ("0", ""):
            self.fix = False
            return
        self.fix = True
        lat = self._dms(p[2], p[3])
        lon = self._dms(p[4], p[5])
        if lat is not None:
            self.lat = lat
        if lon is not None:
            self.lon = lon
        try:
            self.sats = int(p[7])
            if p[8]:
                self.hdop = float(p[8])
            if p[9]:
                self.alt = float(p[9])
        except ValueError:
            pass

    def _rmc(self, p):
        if len(p) < 10 or p[2] == "":
            return
        try:
            hhmmss = p[1]
            ddmmyy = p[9]
            self.utc = "20%s-%s-%s %s:%s:%s" % (ddmmyy[4:6], ddmmyy[2:4],
                                                ddmmyy[0:2], hhmmss[0:2],
                                                hhmmss[2:4], hhmmss[4:6])
        except (ValueError, IndexError):
            pass

    def poll(self, budget_ms=50):
        # Non-blocking drain for up to budget_ms; read() with the
        # timeout=0 UART returns None/b'' when silent, and the sleep
        # loop keeps the shell responsive.
        deadline = time.ticks_add(time.ticks_ms(), budget_ms)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            if self.uart.any():
                self._sentence()
            else:
                time.sleep_ms(10)

    def listen_hint(self):
        """Wait up to GPS_LISTEN_TIMEOUT_S for a *valid NMEA sentence*,
        then bail with the vanilla-T-Deck message. Stray noise bytes on
        floating pins (seen on vanilla hardware) don't satisfy it."""
        deadline = time.time() + GPS_LISTEN_TIMEOUT_S
        while time.time() < deadline:
            self.poll(budget_ms=10)
            if self.lines_seen:
                return True
            time.sleep_ms(50)
        raise OSError(
            "no valid NMEA sentences on GPIO %d/%d in %ds -- GPS "
            "requires the T-Deck Plus (vanilla T-Deck has no GPS "
            "hardware)" % (GPS_RX_PIN, GPS_TX_PIN, GPS_LISTEN_TIMEOUT_S))

    def wait_fix(self, timeout_s=120):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.poll()
            if self.fix and self.utc:
                return True
            time.sleep_ms(100)
        return False


# MicroPython's wlan.scan() authmode is an enum-index (0=OPEN,
# 1=WEP, 2=WPA, 3=WPA2, ...), not a bitmask.
SEC_NAMES = ("OPEN", "WEP", "WPA", "WPA2", "WPA/WPA2", "WPA3",
             "WPA2/WPA3", "WPA/WPA3")


def _authmode_str(a):
    try:
        v = int(a[0]) if isinstance(a, tuple) else int(a)
    except (ValueError, TypeError, IndexError):
        return "OPEN"
    return SEC_NAMES[v] if 0 <= v < len(SEC_NAMES) else "OPEN"


def _csv_field(s):
    if ("," in s) or ('"' in s) or ("\n" in s):
        return '"' + s.replace('"', '""') + '"'
    return s


def _scan_rows(wlan, gps):
    """One WiGLE CSV row per AP found in one scan."""
    ts = gps.utc or _now_utc()
    lat = gps.lat
    lon = gps.lon
    acc = "10" if gps.hdop is None else str(max(2, round(gps.hdop * 3.0)))
    rows = []
    for (ssid, bssid, chan, rssi, auth, hidden) in wlan.scan():
        if lat is None or lon is None:
            continue  # WiGLE rows need a fix; skip this scan tick
        s = ssid if isinstance(ssid, str) else str(ssid, "utf-8", "ignore") if ssid else ""
        s = _csv_field(s)
        mac = "%02X:%02X:%02X:%02X:%02X:%02X" % tuple(bssid)
        try:
            auth_s = _authmode_str(auth)
        except Exception:
            auth_s = "OPEN"
        rows.append(",".join((
            mac,
            s,
            auth_s,
            ts,
            str(chan),
            str(rssi),
            "%.6f" % lat,
            "%.6f" % lon,
            ("%.1f" % gps.alt) if gps.alt is not None else "0",
            acc,
        )))
    return rows


def _now_utc():
    t = time.localtime(time.time())
    return "%04d-%02d-%02d %02d:%02d:%02d" % t[0:6]


def run_session(env, args):
    if not args:
        print("Usage: wardrive <name> [interval_sec] | wardrive kml <name>")
        return
    name = args[0]
    interval = int(args[1]) if len(args) > 1 else 3
    if interval < 1 or name.startswith("kml"):
        print("Usage: wardrive <name> [interval_sec]")
        return

    root = _storage_root()
    path = "%s/%s.csv" % (root, name)

    gps = Gps()
    print("[*] listening for GPS on GPIO %d/%d (Plus only)..."
          % (GPS_RX_PIN, GPS_TX_PIN))
    gps.listen_hint()
    print("[*] GPS detected -- acquiring fix (sats may take minutes)...")
    if FIX_REQUIRED and not gps.wait_fix():
        print("[!] no GPS fix in time -- rows need lat/lon; retry outdoors")
        return

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        wlan.disconnect()
    try:
        import bluetooth
        bluetooth.BLE().active(False)
    except Exception:
        pass

    n_rows = 0
    with open(path, "w") as f:
        print("[*] logging to %s (Ctrl+C to stop)" % path)
        print("[*] running until Ctrl+C")
        try:
            while True:
                gps.poll()
                for row in _scan_rows(wlan, gps):
                    f.write(row + "\n")
                    n_rows += 1
                lat = "%s" % ("" if gps.lat is None else "%.6f" % gps.lat)
                print("  fix=%s sats=%2d rows=%d lat=%-10s lon=%-11s"
                      % ("yes" if gps.fix else "no ", gps.sats, n_rows, lat,
                         "" if gps.lon is None else "%.6f" % gps.lon))
                f.flush()
                time.sleep(interval)
        finally:
            try:
                wlan.active(False)
            except Exception:
                pass
    print("[*] %d AP rows in %s; export with: wardrive kml %s"
          % (n_rows, path, name))
    gc.collect()


# ------------------------------------------------------------------
# KML export
# ------------------------------------------------------------------

KML_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>%(name)s</name>
%(placemarks)s
  </Document>
</kml>
"""


def _kml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _split_csv(line):
    """Minimal RFC-4180-ish splitter (handles quoted SSID commas)."""
    if '"' not in line:
        return line.split(",")
    out = []
    cur = []
    inq = False
    i = 0
    while i < len(line):
        c = line[i]
        if inq:
            if c == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    cur.append('"')
                    i += 1
                else:
                    inq = False
            else:
                cur.append(c)
        elif c == '"':
            inq = True
        elif c == ",":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def export_kml(env, args):
    if len(args) < 1 or args[0] == "kml" and len(args) < 2:
        print("Usage: wardrive kml <name>   (expects <name>.csv in /sd or /flash)")
        return
    name = args[1] if args[0] == "kml" else args[0]
    found = None
    for root in ("/sd", "/flash"):
        p = "%s/%s.csv" % (root, name)
        try:
            os.stat(p)
            found = p
            break
        except OSError:
            continue
    if not found:
        print("[!] no %s.csv in /sd or /flash" % name)
        return

    placemarks = []
    with open(found) as f:
        for line in f:
            # fixed field order we wrote:
            # MAC,SSID,auth,ts,chan,rssi,lat,lon,alt,acc
            fields = _split_csv(line.strip())
            if len(fields) != 10:
                continue
            try:
                lat = float(fields[6])
                lon = float(fields[7])
            except ValueError:
                continue
            mac = fields[0]
            ssid = fields[1] or "(hidden)"
            ts = fields[3]
            rssi = fields[5]
            title = "%s (%s, %s dBm)" % (ssid, mac, rssi)
            placemarks.append(
                '    <Placemark><name>%s</name><description>%s</description>'
                '<Point><coordinates>%.6f,%.6f</coordinates></Point>'
                "</Placemark>" % (_kml_escape(title), _kml_escape(ts), lon, lat))

    if not placemarks:
        print("[!] no geo rows in %s" % found)
        return
    out_path = found[:-4] + ".kml"
    with open(out_path, "w") as f:
        f.write(KML_TMPL % {"name": _kml_escape(name),
                            "placemarks": "\n".join(placemarks)})
    print("[*] %s (%d placemarks)" % (out_path, len(placemarks)))


def main(env, args):
    if args and args[0] == "kml":
        export_kml(env, args)
    else:
        run_session(env, args)
