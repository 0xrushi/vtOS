#!/usr/bin/env python3
"""Read a ytstream capture on stdin and report what it contains (host-side test)."""
import json
import sys

MARKER = b"\xffYTSTRM1"
data = sys.stdin.buffer.read()
assert data.startswith(MARKER), "no marker"
pos = len(MARKER)
counts = {b"M": 0, b"V": 0, b"A": 0, b"E": 0}
sizes = {b"V": [], b"A": 0}
meta = None
last = b""
while pos + 5 <= len(data):
    kind = data[pos:pos + 1]
    length = int.from_bytes(data[pos + 1:pos + 5], "little")
    payload = data[pos + 5:pos + 5 + length]
    assert len(payload) == length, "truncated packet"
    pos += 5 + length
    counts[kind] += 1
    if kind == b"M":
        meta = json.loads(payload)
    elif kind == b"V":
        assert payload[:2] == b"\xff\xd8" and payload[-2:] == b"\xff\xd9", "bad jpeg"
        sizes[b"V"].append(len(payload))
    elif kind == b"A":
        sizes[b"A"] += len(payload)
    elif kind == b"E":
        last = payload
print("meta", meta)
print("packets", {k.decode(): v for k, v in counts.items()})
v = sizes[b"V"]
print("video frames", len(v), "avg", sum(v) // max(1, len(v)), "max", max(v or [0]), "bytes")
print("audio bytes", sizes[b"A"], "end", last)
