#
# MicroPython YouTube-stream protocol (tools/ytstream/ytstream.py)
# License: MIT
#
# The server sends a marker, then packets [type:1][length:4 LE][payload]:
#   M json (once), V one JPEG, A MP3 bytes, E text (end or error).
# Written to run on MicroPython and CPython (tests): no f-strings, no ljust.
#

MARKER = b"\xffYTSTRM1"
MAX_PACKET = 64 * 1024


class Demux:
    """Feed received bytes; get back whole packets as (kind, payload)."""

    def __init__(self):
        self.buf = bytearray()
        self.synced = False

    def feed(self, data):
        self.buf += data
        events = []
        if not self.synced:
            at = self.buf.find(MARKER)
            if at < 0:
                # keep a possible partial marker, drop the shell noise before it
                keep = len(MARKER) - 1
                if len(self.buf) > keep:
                    self.buf = self.buf[-keep:]
                return events
            self.buf = self.buf[at + len(MARKER):]
            self.synced = True
        pos = 0
        buf = self.buf
        while len(buf) - pos >= 5:
            kind = bytes(buf[pos:pos + 1])
            size = int.from_bytes(buf[pos + 1:pos + 5], "little")
            if size > MAX_PACKET:
                raise ValueError("bad packet length")
            if len(buf) - pos - 5 < size:
                break
            events.append((kind, bytes(buf[pos + 5:pos + 5 + size])))
            pos += 5 + size
        if pos:
            self.buf = buf[pos:]
        return events


class AudioPipe:
    """Stream-like object for AudioPlayer.play(): MP3 chunks in, readinto out.

    readinto() returns None when nothing is queued (the player treats that as
    "no data yet", not end of stream)."""

    def __init__(self):
        self.chunks = []
        self.offset = 0
        self.size = 0

    def push(self, data):
        if data:
            self.chunks.append(data)
            self.size += len(data)

    def readinto(self, buf):
        if not self.chunks:
            return None
        want = len(buf)
        done = 0
        while done < want and self.chunks:
            chunk = self.chunks[0]
            take = min(want - done, len(chunk) - self.offset)
            buf[done:done + take] = chunk[self.offset:self.offset + take]
            done += take
            self.offset += take
            if self.offset >= len(chunk):
                self.chunks.pop(0)
                self.offset = 0
        self.size -= done
        return done

    def read(self, n=-1):
        buf = bytearray(n if n and n > 0 else self.size)
        got = self.readinto(buf)
        return bytes(buf[:got]) if got else b""
