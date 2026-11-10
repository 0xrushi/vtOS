"""A scripted RFB server and a fake display for the VNC client tests.

FakeServer talks over a socketpair from a thread. Its framebuffer is a fixed
function of position (pixel_at), so a test can say exactly which pixel must
land on which display coordinate. Like a real server it answers a
non-incremental update request at once and holds an incremental one until
something has changed.
"""

import select
import socket
import struct
import threading

from Crypto.Cipher import DES


def pixel_at(x, y):
    return (x * 257 + y * 31) & 0xFFFF


def raw_pixels(x, y, w, h):
    out = bytearray()
    for row in range(y, y + h):
        for col in range(x, x + w):
            out += struct.pack(">H", pixel_at(col, row))
    return bytes(out)


def rect_header(x, y, w, h, enc):
    return struct.pack(">HHHHi", x, y, w, h, enc)


def update(*rects):
    """A FramebufferUpdate carrying (x, y, w, h, enc, payload) rectangles."""
    msg = struct.pack(">BBH", 0, 0, len(rects))
    for x, y, w, h, enc, payload in rects:
        msg += rect_header(x, y, w, h, enc) + payload
    return msg


def raw_rect(x, y, w, h):
    return (x, y, w, h, 0, raw_pixels(x, y, w, h))


def vnc_key(password):
    key = bytearray(8)
    for i, c in enumerate(password.encode()[:8]):
        key[i] = int(format(c, "08b")[::-1], 2)
    return bytes(key)


def vnc_answer(password, challenge):
    cipher = DES.new(vnc_key(password), DES.MODE_ECB)
    return cipher.encrypt(challenge)


class SockIO:
    """The readinto/write interface rfb.Client expects, over a host socket."""

    def __init__(self, sock):
        self.sock = sock

    def readinto(self, buf):
        return self.sock.recv_into(buf)

    def write(self, data):
        self.sock.sendall(data)

    # select() and cleanup, as on a MicroPython socket
    def fileno(self):
        return self.sock.fileno()

    def close(self):
        self.sock.close()


class FakeServer:
    CHALLENGE = bytes(range(16))

    def __init__(self, version=b"RFB 003.008\n", security=(1,), password=None,
                 width=64, height=48, name="fake", refuse=None, init_garbage=False):
        self.version = version
        self.security = security
        self.password = password
        self.width = width
        self.height = height
        self.name = name
        self.refuse = refuse          # reason string: refuse the connection
        self.events = []
        self.error = None
        self.chosen_version = None
        self.chosen_security = None
        self.auth_ok = None
        self.dirty = None             # (x, y, w, h) changed since last update
        self.held = None              # incremental request waiting for a change
        self.lock = threading.Condition()
        self.to_send = []             # raw messages queued by the test
        self.done = threading.Event()
        self.client_sock, self.sock = socket.socketpair()
        self.client_sock.settimeout(5)
        self.sock.settimeout(5)
        self.thread = threading.Thread(target=self._main, daemon=True)

    # -- plumbing --

    def start(self):
        self.thread.start()
        return self.client_sock

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.done.set()

    def _read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _send(self, data):
        self.sock.sendall(data)

    def wait_for(self, predicate, timeout=5):
        end = threading.Event()
        for _ in range(int(timeout * 200)):
            with self.lock:
                if predicate(self.events):
                    return True
            end.wait(0.005)
        return False

    def queue(self, data):
        """Send a raw message to the client as soon as the server loop runs."""
        with self.lock:
            self.to_send.append(data)

    def change(self, x, y, w, h):
        """Pretend the remote screen changed there (releases a held request)."""
        with self.lock:
            self.dirty = (x, y, w, h)

    # -- protocol --

    def _main(self):
        try:
            self._handshake()
            self._loop()
        except (EOFError, OSError):
            pass
        except Exception as err:   # surfaced by tests through .error
            self.error = err
        finally:
            self.done.set()

    def _handshake(self):
        self._send(self.version)
        reply = self._read(12)
        self.chosen_version = reply
        minor = int(reply[8:11])
        if self.refuse is not None and minor == 3:
            self._send(struct.pack(">II", 0, len(self.refuse)) + self.refuse.encode())
            return self._stop_after_refusal()
        if minor == 3:
            sectype = self.security[0]
            self._send(struct.pack(">I", sectype))
        else:
            if self.refuse is not None:
                self._send(bytes([0]) + struct.pack(">I", len(self.refuse)) + self.refuse.encode())
                return self._stop_after_refusal()
            self._send(bytes([len(self.security)]) + bytes(self.security))
            sectype = self._read(1)[0]
        self.chosen_security = sectype
        if sectype == 2:
            self._send(self.CHALLENGE)
            answer = self._read(16)
            good = answer == vnc_answer(self.password, self.CHALLENGE)
            self.auth_ok = good
            if good:
                self._send(struct.pack(">I", 0))
            else:
                self._send(struct.pack(">I", 1))
                if minor >= 8:
                    reason = b"Authentication failure"
                    self._send(struct.pack(">I", len(reason)) + reason)
                raise EOFError
        elif minor >= 8:
            self._send(struct.pack(">I", 0))
        self._read(1)                  # ClientInit (shared flag)
        fmt = struct.pack(">BBBBHHHBBBBBB", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0, 0, 0, 0)
        name = self.name.encode()
        self._send(struct.pack(">HH", self.width, self.height) + fmt +
                   struct.pack(">I", len(name)) + name)

    def _stop_after_refusal(self):
        raise EOFError

    def _loop(self):
        # Poll with select(), not a short socket timeout: that timeout would
        # also apply to sendall() and kill the thread on a slow reader.
        self.sock.settimeout(5)
        while not self.done.is_set():
            with self.lock:
                outgoing = self.to_send
                self.to_send = []
            for data in outgoing:
                self._send(data)
            self._release_held()
            if not select.select([self.sock], [], [], 0.01)[0]:
                continue
            head = self.sock.recv(1)
            if not head:
                return
            self._message(head[0])

    def _message(self, mtype):
        if mtype == 0:
            body = self._read(19)
            with self.lock:
                self.events.append(("pixfmt", body[3:]))
        elif mtype == 2:
            head = self._read(3)
            count = struct.unpack(">H", head[1:])[0]
            encs = list(struct.unpack(">%di" % count, self._read(4 * count)))
            with self.lock:
                self.events.append(("encodings", encs))
        elif mtype == 3:
            inc, x, y, w, h = struct.unpack(">BHHHH", self._read(9))
            with self.lock:
                self.events.append(("fbur", inc, x, y, w, h))
            if inc:
                self.held = (x, y, w, h)
            else:
                self._send(update(raw_rect(x, y, w, h)))
        elif mtype == 4:
            down, _pad, key = struct.unpack(">BHI", self._read(7))
            with self.lock:
                self.events.append(("key", down, key))
        elif mtype == 5:
            buttons, x, y = struct.unpack(">BHH", self._read(5))
            with self.lock:
                self.events.append(("pointer", buttons, x, y))
        elif mtype == 6:
            _pad = self._read(3)
            n = struct.unpack(">I", self._read(4))[0]
            self._read(n)
        else:
            raise RuntimeError("client sent unknown message %d" % mtype)

    def _release_held(self):
        with self.lock:
            dirty, held = self.dirty, self.held
            if dirty is None or held is None:
                return
            self.dirty = self.held = None
        # the changed area clipped to what the client asked about
        x0 = max(dirty[0], held[0])
        y0 = max(dirty[1], held[1])
        x1 = min(dirty[0] + dirty[2], held[0] + held[2])
        y1 = min(dirty[1] + dirty[3], held[1] + held[3])
        if x1 > x0 and y1 > y0:
            self._send(update(raw_rect(x0, y0, x1 - x0, y1 - y0)))
        else:
            with self.lock:
                self.held = held


class FakeTft:
    """Records what the client draws; screen[y][x] = RGB565 or None."""

    def __init__(self, width=320, height=240):
        self.width = width
        self.height = height
        self.screen = [[None] * width for _ in range(height)]
        self.blits = 0
        self.rects = []

    def fill(self, color):
        self.screen = [[color] * self.width for _ in range(self.height)]

    def fill_rect(self, x, y, w, h, color):
        self.rects.append((x, y, w, h, color))
        for row in range(y, y + h):
            for col in range(x, x + w):
                if 0 <= row < self.height and 0 <= col < self.width:
                    self.screen[row][col] = color

    def blit_buffer(self, buf, x, y, w, h):
        data = bytes(buf)
        assert len(data) >= w * h * 2, "blit buffer shorter than w*h*2"
        assert 0 <= x and x + w <= self.width and 0 <= y and y + h <= self.height, \
            "blit outside the display: %r" % ((x, y, w, h),)
        self.blits += 1
        i = 0
        for row in range(y, y + h):
            for col in range(x, x + w):
                self.screen[row][col] = (data[i] << 8) | data[i + 1]
                i += 2
