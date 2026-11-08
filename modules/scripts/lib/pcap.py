#
# PCAP file serializer for modwifi captures.
# Writes classic libpcap with LINKTYPE_IEEE802_11 (105) so captures
# open directly in Wireshark with full 802.11 dissection.
#
# The MicroPython side drains modwifi's ring into a bytearray of
# packed records produced by wifi.sniff_read():
#   [u16 len (LE)][u8 channel][u8 rssi][payload bytes]
# iter_records() parses that stream back into (len, channel, rssi,
# payload) tuples for consumers (the sniff app today, KML/CSV tooling
# tomorrow).
#
# Copyright (c) 2026 8bitmcu
# License: MIT
#

import ustruct as struct
import time

REC_HDR = 4  # u16 len + u8 channel + u8 rssi

# DLT_IEEE802_11 -- Wireshark treats the payload as raw 802.11 frames.
LINKTYPE_IEEE802_11 = 105

# Classic pcap magic (microsecond resolution).
PCAP_MAGIC = 0xA1B2C3D4
PCAP_VERSION = (2, 4)


class PcapWriter:
    """Incremental libpcap writer; call .write() per frame."""

    def __init__(self, path):
        self.f = open(path, "wb")
        # global header: magic, v2.4, thiszone=0, sigfigs=0, snaplen,
        # network=LINKTYPE_IEEE802_11
        self.f.write(
            struct.pack("<IHHiIII", PCAP_MAGIC, *PCAP_VERSION, 0, 0, 1600, LINKTYPE_IEEE802_11)
        )

    def write(self, payload, ts=None, ts_us=0):
        """Append one frame. ts in seconds (float ok); ticks default."""
        if ts is None:
            ts = time.time()
        sec = int(ts)
        self.f.write(struct.pack("<IIII", sec, ts_us, len(payload), len(payload)))
        self.f.write(payload)

    def close(self):
        self.f.close()

    def flush(self):
        self.f.flush()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def iter_records(buf, count):
    """Yield (len, channel, rssi, payload_bytes) from a sniff_read
    buffer that filled `count` records. Records are back-to-back with
    no padding; count comes straight from sniff_read()'s return so we
    stop at the last full record even if the caller's buffer is
    oversized."""
    pos = 0
    for _ in range(count):
        if pos + REC_HDR > len(buf):
            break
        ln = buf[pos] | (buf[pos + 1] << 8)
        channel = buf[pos + 2]
        rssi = buf[pos + 3]
        payload = bytes(buf[pos + REC_HDR: pos + REC_HDR + ln])
        pos += REC_HDR + ln
        yield ln, channel, rssi, payload
