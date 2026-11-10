#
# RFB (VNC) client protocol: handshake, VNC password authentication and
# framebuffer updates. No display or network code lives here -- it talks to
# any object with readinto(buf) and write(data), so the same code runs on the
# T-Deck (a MicroPython socket) and in the host tests (a socket adapter).
# License: MIT
#
# Supported: RFB 3.3 / 3.7 / 3.8, security types None (1) and VNC (2),
# Raw rectangles and the DesktopSize pseudo-encoding. The pixel format is
# fixed to 16-bit RGB565, big-endian, which is exactly the byte order the
# ST7789 panel takes, so a Raw rectangle can be blitted without conversion.
#
# Not supported: ARD / TLS / VeNCrypt security (the server must offer plain
# VNC password or no authentication), and any compressed encoding.
#

import struct

ENC_RAW = 0
ENC_LAST_RECT = -224
ENC_DESKTOP_SIZE = -223

BYTES_PER_PIXEL = 2

KEY_SHIFT_L = 0xFFE1

_MAX_NAME = 256
_MAX_REASON = 256
_MAX_CUT = 4096

# Button masks for PointerEvent.
BTN_LEFT = 1
BTN_MIDDLE = 2
BTN_RIGHT = 4
BTN_WHEEL_UP = 8
BTN_WHEEL_DOWN = 16
BTN_WHEEL_LEFT = 32
BTN_WHEEL_RIGHT = 64


class RFBError(Exception):
    pass


# --- VNC password authentication: single-DES ECB over the 16-byte challenge ---

_IP = (58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
       62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
       57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
_FP = (40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
       38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
       36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25)
_E = (32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13,
      12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
      24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1)
_P = (16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
      2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25)
_PC1 = (57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
        10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
        63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
        14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4)
_PC2 = (14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
        23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
        44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32)
_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_SBOX = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
     0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
     15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
     3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
     13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
     13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
     1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
     13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
     3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
     14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
     11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
     10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
     4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
     13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
     6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
     1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
     2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)


def _permute(value, table, width):
    out = 0
    for pos in table:
        out = (out << 1) | ((value >> (width - pos)) & 1)
    return out


def _subkeys(key):
    k = _permute(key, _PC1, 64)
    c = k >> 28
    d = k & 0xFFFFFFF
    keys = []
    for shift in _SHIFTS:
        c = ((c << shift) | (c >> (28 - shift))) & 0xFFFFFFF
        d = ((d << shift) | (d >> (28 - shift))) & 0xFFFFFFF
        keys.append(_permute((c << 28) | d, _PC2, 56))
    return keys


def _feistel(r, subkey):
    x = _permute(r, _E, 32) ^ subkey
    out = 0
    for i in range(8):
        six = (x >> (42 - 6 * i)) & 0x3F
        row = ((six >> 4) & 2) | (six & 1)
        col = (six >> 1) & 0xF
        out = (out << 4) | _SBOX[i][row * 16 + col]
    return _permute(out, _P, 32)


def des_encrypt_block(key8, block8):
    """Plain DES (ECB, one block): 8-byte key and block in, 8 bytes out."""
    keys = _subkeys(int.from_bytes(key8, "big"))
    b = _permute(int.from_bytes(block8, "big"), _IP, 64)
    left = b >> 32
    right = b & 0xFFFFFFFF
    for k in keys:
        left, right = right, left ^ _feistel(right, k)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


def _reverse_bits(byte):
    out = 0
    for _ in range(8):
        out = (out << 1) | (byte & 1)
        byte >>= 1
    return out


def vnc_auth_response(password, challenge):
    """The 16-byte answer to a VNC-authentication challenge.

    The classic scheme uses the first 8 password bytes (zero padded) as a DES
    key with every byte's bits mirrored, and encrypts each 8-byte half of the
    challenge separately.
    """
    if isinstance(password, str):
        password = password.encode()
    key = bytearray(8)
    for i in range(min(8, len(password))):
        key[i] = _reverse_bits(password[i])
    key = bytes(key)
    return des_encrypt_block(key, challenge[:8]) + des_encrypt_block(key, challenge[8:16])


# --- client ---------------------------------------------------------------

class Sink:
    """Receives what the server sends. Override what you use."""

    def raw_rect(self, client, x, y, w, h):
        """Consume exactly w*h*2 bytes with client.read_into(); the default
        discards them."""
        client.skip(w * h * BYTES_PER_PIXEL)

    def resize(self, width, height):
        pass

    def update_done(self):
        pass

    def bell(self):
        pass

    def cut_text(self, text):
        pass


def _latin1(data):
    return "".join([chr(b) for b in data])


def _safe_text(data):
    try:
        return bytes(data).decode()
    except Exception:
        return _latin1(data)


class Client:
    def __init__(self, io):
        self.io = io
        self.width = 0
        self.height = 0
        self.name = ""
        self.minor = 8
        self.security = 0
        self._count = 0        # bytes consumed, used to check rectangle handlers
        self._scratch = bytearray(512)

    # -- reading --

    def read_into(self, buf, n=None):
        """Fill buf[:n] completely, blocking. Closing mid-way is an error."""
        view = memoryview(buf)
        if n is None:
            n = len(buf)
        got = 0
        while got < n:
            r = self.io.readinto(view[got:n])
            if not r:
                raise RFBError("connection closed")
            got += r
        self._count += n
        return n

    def read(self, n):
        buf = bytearray(n)
        self.read_into(buf, n)
        return buf

    def skip(self, n):
        scratch = self._scratch
        while n > 0:
            step = min(n, len(scratch))
            self.read_into(scratch, step)
            n -= step

    def _read_u32(self):
        return struct.unpack(">I", self.read(4))[0]

    def _read_text(self, cap):
        n = self._read_u32()
        data = self.read(min(n, cap))
        if n > cap:
            self.skip(n - cap)
        return _safe_text(data)

    def write(self, data):
        self.io.write(data)

    # -- handshake --

    def handshake(self, password=None, ask_password=None, shared=True):
        """Version, security and initialisation. Sets width, height, name.

        password is the VNC password if known; ask_password() is called (once,
        only if the server wants one) to get it otherwise and may return None
        to give up.
        """
        version = bytes(self.read(12))
        if version[:4] != b"RFB " or version[7:8] != b"." or version[11:12] != b"\n":
            raise RFBError("not a VNC server")
        try:
            major = int(version[4:7].decode())
            minor = int(version[8:11].decode())
        except (ValueError, UnicodeError):
            raise RFBError("not a VNC server")
        if major < 3:
            raise RFBError("VNC server too old (RFB %d.%d)" % (major, minor))
        if major == 3 and minor == 889:    # Apple Remote Desktop speaks 3.3
            chosen = 3
        elif major > 3 or minor >= 8:
            chosen = 8
        elif minor == 7:
            chosen = 7
        else:
            chosen = 3
        self.minor = chosen
        self.write(b"RFB 003.00" + str(chosen).encode() + b"\n")

        if chosen == 3:
            sectype = self._read_u32()
            if sectype == 0:
                raise RFBError("refused: " + self._read_text(_MAX_REASON))
            if sectype not in (1, 2):
                raise RFBError("unsupported security type %d" % sectype)
        else:
            count = self.read(1)[0]
            if count == 0:
                raise RFBError("refused: " + self._read_text(_MAX_REASON))
            offered = list(self.read(count))
            if 1 in offered:
                sectype = 1
            elif 2 in offered:
                sectype = 2
            else:
                raise RFBError("unsupported security types %s (need None or VNC "
                               "password; set the server to VNC authentication)"
                               % (offered,))
            self.write(bytes([sectype]))
        self.security = sectype

        if sectype == 2:
            challenge = self.read(16)
            if password is None and ask_password is not None:
                password = ask_password()
            if password is None:
                raise RFBError("password required")
            self.write(vnc_auth_response(password, challenge))

        # SecurityResult: always for VNC auth, and for every type from 3.8 on.
        if sectype == 2 or chosen == 8:
            if self._read_u32() != 0:
                reason = "wrong password"
                if chosen == 8:
                    reason = self._read_text(_MAX_REASON) or reason
                raise RFBError("authentication failed: " + reason)

        self.write(b"\x01" if shared else b"\x00")
        init = self.read(24)
        self.width, self.height = struct.unpack(">HH", bytes(init[:4]))
        name_len = struct.unpack(">I", bytes(init[20:24]))[0]
        name = self.read(min(name_len, _MAX_NAME))
        if name_len > _MAX_NAME:
            self.skip(name_len - _MAX_NAME)
        self.name = _safe_text(name)

    # -- client messages --

    def set_pixel_format(self):
        """16 bpp RGB565, big-endian, true colour."""
        self.write(struct.pack(">BBBBBBBBHHHBBBBBB",
                               0, 0, 0, 0,
                               16, 16, 1, 1,
                               31, 63, 31,
                               11, 5, 0,
                               0, 0, 0))

    def set_encodings(self, encodings):
        msg = struct.pack(">BBH", 2, 0, len(encodings))
        for enc in encodings:
            msg += struct.pack(">i", enc)
        self.write(msg)

    def request_update(self, x, y, w, h, incremental):
        self.write(struct.pack(">BBHHHH", 3, 1 if incremental else 0, x, y, w, h))

    def key_event(self, keysym, down):
        self.write(struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym))

    def pointer_event(self, buttons, x, y):
        self.write(struct.pack(">BBHH", 5, buttons, x, y))

    def key_press(self, keysym, shift=False):
        """Key down then up, in one write; shift=True holds Shift_L around it.

        The keyboard sends 'I', not Shift + 'i'. Without a Shift event some
        servers fake a Caps Lock press to produce a capital, which can leave
        the remote's lock state wrong.
        """
        msg = (struct.pack(">BBHI", 4, 1, 0, keysym) +
               struct.pack(">BBHI", 4, 0, 0, keysym))
        if shift:
            msg = (struct.pack(">BBHI", 4, 1, 0, KEY_SHIFT_L) + msg +
                   struct.pack(">BBHI", 4, 0, 0, KEY_SHIFT_L))
        self.write(msg)

    def pointer_click(self, buttons, x, y):
        """Press then release the given buttons at (x, y), in one write."""
        self.write(struct.pack(">BBHH", 5, buttons, x, y) +
                   struct.pack(">BBHH", 5, 0, x, y))

    # -- server messages --

    def read_message(self, sink):
        """Read and dispatch one server message; returns its type."""
        mtype = self.read(1)[0]
        if mtype == 0:
            hdr = self.read(3)
            count = (hdr[1] << 8) | hdr[2]
            for _ in range(count):
                x, y, w, h, enc = struct.unpack(">HHHHi", bytes(self.read(12)))
                if enc == ENC_RAW:
                    before = self._count
                    sink.raw_rect(self, x, y, w, h)
                    if self._count - before != w * h * BYTES_PER_PIXEL:
                        raise RFBError("rectangle handler lost sync")
                elif enc == ENC_DESKTOP_SIZE:
                    self.width, self.height = w, h
                    sink.resize(w, h)
                elif enc == ENC_LAST_RECT:
                    break
                else:
                    raise RFBError("server sent unsupported encoding %d" % enc)
            sink.update_done()
        elif mtype == 1:               # SetColourMapEntries (we are true colour)
            hdr = self.read(5)
            self.skip(((hdr[3] << 8) | hdr[4]) * 6)
        elif mtype == 2:
            sink.bell()
        elif mtype == 3:               # ServerCutText
            self.skip(3)
            sink.cut_text(self._read_text(_MAX_CUT))
        else:
            raise RFBError("unknown server message %d" % mtype)
        return mtype
