#
# MicroPython VNC Server
# Copyright (c) 2026 8bitmcu
# License: MIT
#
# Minimal RFB server mirroring the LCD's shadow framebuffer, with a
# terminal-only renderer for firmware without shadow capture.

import binascii
import errno
import struct
import socket
import select
import network
import machine
import micropython

_MAX_FONT_HEIGHT = 16
# Every tick makes one micropython.schedule() attempt, which competes for
# the same limited schedule queue as main.py's own timers (draw/status
# bar) -- neither side checks whether its schedule() call actually got
# queued, so under contention one silently loses its turn. 50ms (20/sec)
# was frequent enough to occasionally bump the status bar's once-a-second
# refresh; 150ms cuts that 3x while staying responsive for keyboard input.
_TICK_MS = 150
_FB_TICK_MS = 30
_FB_BAND_ROWS = 8
_FB_BANDS_PER_TICK = 4
_WOULD_BLOCK = (errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN))

_PIXEL_FORMAT = struct.pack(">BBBBHHHBBB",
                            32, 24,        # bits-per-pixel, depth
                            0, 1,          # big-endian-flag, true-color-flag
                            255, 255, 255, # red-max, green-max, blue-max
                            16, 8, 0) + b"\x00\x00\x00"  # red/green/blue-shift + padding
assert len(_PIXEL_FORMAT) == 16

_DEFAULT_FORMAT = (32, 0, 255, 255, 255, 16, 8, 0)

# Wire format of the shadow framebuffer: big-endian RGB565. Offering it as the
# server format means a client that keeps it needs no conversion at all.
_NATIVE_PIXEL_FORMAT = struct.pack(">BBBBHHHBBB", 16, 16, 1, 1, 31, 63, 31, 11, 5, 0) + b"\x00\x00\x00"
_NATIVE_FORMAT = (16, 1, 31, 63, 31, 11, 5, 0)

_SERVER_NAME = b"vtOS"

_CTRL_KEYSYMS = (0xFFE3, 0xFFE4)
_SHIFT_KEYSYMS = (0xFFE1, 0xFFE2)

_KEYSYM_MAP = {
    0xFF08: "\x08",    # BackSpace
    0xFF09: "\t",       # Tab
    0xFF0D: "\r",       # Return
    0xFF1B: "\x1b",     # Escape
    0xFF51: "\x1b[D",   # Left
    0xFF52: "\x1b[A",   # Up
    0xFF53: "\x1b[C",   # Right
    0xFF54: "\x1b[B",   # Down
    0xFFFF: "\x1b[3~",  # Delete
    0xFF50: "\x1b[H",   # Home
    0xFF57: "\x1b[F",   # End
    0xFF55: "\x1b[5~",  # Page Up
    0xFF56: "\x1b[6~",  # Page Down
    0xFF63: "\x1b[2~",  # Insert
}

# Connection states -- each maps to how many bytes are needed before
# _advance() can process the next step.
_ST_VERSION = 0
_ST_SECTYPE = 1
_ST_CLIENTINIT = 2
_ST_MSGTYPE = 3
_ST_SETPIXFMT = 4
_ST_SETENC_HDR = 5
_ST_SETENC_LIST = 6
_ST_FBUR = 7
_ST_KEYEVENT = 8
_ST_POINTER = 9
_ST_CUTTEXT_HDR = 10
_ST_CUTTEXT_BODY = 11

def get_ip():
    sta = network.WLAN(network.STA_IF)
    if sta.active():
        return sta.ifconfig()[0]
    ap = network.WLAN(network.AP_IF)
    if ap.active():
        return ap.ifconfig()[0]
    return "127.0.0.1"

def _inject_key(env, keysym, ctrl=False):
    if ctrl and 0x40 <= keysym <= 0x7e:
        env.kvm.inject(chr(keysym & 0x1f))      # Ctrl+A..Z, [ \\ ] ^ _
    elif 0x20 <= keysym <= 0x7e:
        env.kvm.inject(chr(keysym))
    else:
        seq = _KEYSYM_MAP.get(keysym)
        if seq:
            env.kvm.inject(seq)

def _parse_pixel_format(body):
    (bpp, _depth, big_endian, true_color, r_max, g_max, b_max,
     r_shift, g_shift, b_shift) = struct.unpack(">BBBBHHHBBB", body[:13])
    return (bpp, big_endian, true_color, r_max, g_max, b_max,
           r_shift, g_shift, b_shift)

def _rgb565_row_to_pixfmt(src, n_bytes, dst, fmt):
    """Converts n_bytes of big-endian RGB565 (render_row()'s native
    output) into the true-color format `fmt` describes -- (bpp,
    big_endian, red_max, green_max, blue_max, red_shift, green_shift,
    blue_shift). Returns the number of bytes written to dst.

    5/6-bit source channels are expanded to 8 bits by bit replication,
    then scaled to the target's own max-value range before shifting into
    place.
    """
    bpp, big_endian, r_max, g_max, b_max, r_shift, g_shift, b_shift = fmt
    nbytes = bpp // 8
    oi = 0
    for i in range(0, n_bytes, 2):
        v = (src[i] << 8) | src[i + 1]
        r5 = (v >> 11) & 0x1F
        g6 = (v >> 5) & 0x3F
        b5 = v & 0x1F
        r8 = (r5 << 3) | (r5 >> 2)
        g8 = (g6 << 2) | (g6 >> 4)
        b8 = (b5 << 3) | (b5 >> 2)
        val = (((r8 * r_max + 127) // 255) << r_shift |
              ((g8 * g_max + 127) // 255) << g_shift |
              ((b8 * b_max + 127) // 255) << b_shift)
        if big_endian:
            for k in range(nbytes):
                dst[oi + k] = (val >> ((nbytes - 1 - k) * 8)) & 0xFF
        else:
            for k in range(nbytes):
                dst[oi + k] = (val >> (k * 8)) & 0xFF
        oi += nbytes
    return oi

def _scan_changed(conn):
    env = conn.env

    if conn.rows != env.rows:
        conn.rows = env.rows
        conn.row_indices = [-1] + list(range(env.rows))
        conn.prev_checksums = [None] * (1 + env.rows)

    term = env.term
    mv = conn.row565_mv
    found = []
    for idx, y in enumerate(conn.row_indices):
        n565 = term.render_row(y, conn.row565_buf)
        csum = binascii.crc32(mv[:n565])
        if csum != conn.prev_checksums[idx]:
            conn.prev_checksums[idx] = csum
            found.append(y)
    return found

def _queue_update(conn, changed):
    env = conn.env
    term = env.term
    width = env.screen_width
    f_height = env.font.HEIGHT
    top_offset = env.status_height
    fmt = conn.current_format

    conn.queue(struct.pack(">BBH", 0, 0, len(changed)))
    for y in changed:
        n565 = term.render_row(y, conn.row565_buf)
        n = _rgb565_row_to_pixfmt(conn.row565_buf, n565, conn.row_buf, fmt)
        y_pos = 0 if y == -1 else top_offset + y * f_height
        # Header and payload queued separately -- concatenating them would
        # allocate a second full-size copy of the row on top of the one
        # bytes() already has to make (conn.row_buf gets overwritten by
        # the next row before this one necessarily finishes sending).
        conn.queue(struct.pack(">HHHHI", 0, y_pos, width, f_height, 0))
        conn.queue(bytes(conn.row_buf[:n]))

def _dirty_runs(flags, height):
    """Contiguous runs of changed rows as (start, count)."""
    runs = []
    y = 0
    while y < height:
        if flags[y]:
            start = y
            while y < height and flags[y]:
                y += 1
            runs.append((start, y - start))
        else:
            y += 1
    return runs

def _queue_fb_update(conn):
    """Start a single update for all dirty rows and queue its first band."""
    snapshot = conn.shadow_snapshot or conn.env.tft.shadow_dirty
    if not snapshot(conn.flags):
        return False
    runs = _dirty_runs(conn.flags, conn.height)
    conn.fb_update = _fb_bands(conn, runs, conn.current_format, conn.encoding)
    _queue_fb_band(conn)
    return True

def _fb_bands(conn, runs, fmt, encoding):
    # Each rectangle's payload can span several sends. The viewer gets one
    # complete RFB update even when it waits for a response before asking again.
    tft = conn.env.tft
    parts = [struct.pack(">BBH", 0, 0, len(runs))]
    if conn.shadow_pixels is not None:
        for start, count in runs:
            parts.append(struct.pack(">HHHHI", 0, start, conn.width, count, encoding))
            for y in range(start, start + count, 16):
                rows = min(16, start + count - y)
                n = conn.shadow_pixels(y, rows, conn.pixel_band, fmt, encoding == 5)
                parts.append(bytes(conn.pixel_band_mv[:n]))
                yield b"".join(parts)
                parts = []
        return
    native = fmt == _NATIVE_FORMAT
    rgb888 = conn.shadow_row_rgb888 if fmt[0] == 32 and fmt[2:] == _DEFAULT_FORMAT[2:] else None
    rows = 0
    for start, count in runs:
        parts.append(struct.pack(">HHHHI", 0, start, conn.width, count, 0))
        for y in range(start, start + count):
            if rgb888 is not None:
                n = rgb888(y, conn.row_buf, fmt[1])
                parts.append(bytes(conn.row_mv[:n]))
            else:
                n = tft.shadow_row(y, conn.row565_buf)
                if native:
                    parts.append(bytes(conn.row565_mv[:n]))
                else:
                    m = _rgb565_row_to_pixfmt(conn.row565_buf, n, conn.row_buf, fmt)
                    parts.append(bytes(conn.row_mv[:m]))
            rows += 1
            if rows == _FB_BAND_ROWS:
                yield b"".join(parts)
                parts = []
                rows = 0
    if parts:
        yield b"".join(parts)

def _queue_fb_band(conn):
    try:
        conn.queue(next(conn.fb_update))
    except StopIteration:
        conn.fb_update = None

def _repaint(env):
    """Make whatever is on screen draw itself again.

    The shadow buffer only holds what was drawn after it was enabled, and the
    terminal redraws only changed rows, so without this a new viewer sees black
    until each row happens to change. Same recipe as QuickPanel's restore()."""
    try:
        app = getattr(env, "app_display", None)
        if app is not None:
            app.dirty = True
        else:
            env.term.top_offset(env.status_height)      # dirties every text row
            env.term.top_bar_invalidate()
            env.term.bottom_bar_invalidate()
    except Exception:
        pass


def _apply_pointer(conn, buttons, x, y):
    """Mouse -> touch screen: left button is a finger, so taps and drags work
    in the quick panel and in pixel apps that read the touch controller."""
    touch = getattr(getattr(conn.env, "quickpanel", None), "touch", None)
    if touch is None or not hasattr(touch, "remote"):
        return
    down = bool(buttons & 1)
    if down:
        touch.remote = (min(319, x), min(239, y))
    elif conn.pointer_down:
        touch.remote = False        # one release, then the real controller again
    conn.pointer_down = down

class _Conn:
    def __init__(self, sock, addr, env):
        self.sock = sock
        self.addr = addr
        self.env = env
        self.state = _ST_VERSION
        self.need = 12
        self.rxbuf = bytearray()
        self.outq = []
        self.outoff = 0
        self.closed = False
        self.pending_close = False

        self.fb = bool(getattr(env, "fb_shadow", False))
        self.width = env.screen_width
        self.height = env.screen_height
        row_pixels = self.width * (1 if self.fb else _MAX_FONT_HEIGHT)
        self.row565_buf = bytearray(row_pixels * 2)
        self.row565_mv = memoryview(self.row565_buf)
        self.row_buf = bytearray(row_pixels * 4)
        self.row_mv = memoryview(self.row_buf)
        self.flags = bytearray(self.height)
        self.fb_update = None
        self.encoding = 0
        self.shadow_pixels = None
        self.shadow_snapshot = None
        self.pointer_down = False
        self.ctrl = False
        self.current_format = _NATIVE_FORMAT if self.fb else _DEFAULT_FORMAT
        if self.fb:
            self.shadow_pixels = getattr(env.tft, "shadow_pixels", None)
            if self.shadow_pixels is not None:
                self.shadow_snapshot = getattr(env.tft, "shadow_snapshot", None)
                self.pixel_band = bytearray(self.width * 16 * 4 + (self.width + 15) // 16)
                self.pixel_band_mv = memoryview(self.pixel_band)
            self.shadow_row_rgb888 = getattr(env.tft, "shadow_row_rgb888", None)
            env.tft.shadow_invalidate()      # a new client needs a full frame
            _repaint(env)
        self.rows = env.rows  # rows row_indices/prev_checksums were built for -- see _scan_changed()
        self.row_indices = [-1] + list(range(env.rows))  # bar + text rows, computed once
        self.prev_checksums = [None] * (1 + env.rows)  # None forces a full first send

        self.awaiting_update = False
        self.full_update = False
        self._setenc_count = 0

        try:
            sock.setblocking(False)     # a send must never stall the tick
        except (AttributeError, OSError):
            pass
        self.queue(b"RFB 003.008\n")  # server speaks first, unprompted

    def queue(self, data):
        self.outq.append(data)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        self.env.sts.notify(f"vncd: {self.addr} disconnected")

def _advance(conn, chunk):
    st = conn.state

    if st == _ST_VERSION:
        conn.queue(b"\x01\x01")  # offer only security type 1 (None)
        conn.state, conn.need = _ST_SECTYPE, 1

    elif st == _ST_SECTYPE:
        if chunk != b"\x01":
            reason = b"Only security type 1 (None) is supported"
            conn.queue(struct.pack(">I", 1) +
                      struct.pack(">I", len(reason)) + reason)
            conn.pending_close = True
            return
        conn.queue(struct.pack(">I", 0))  # SecurityResult: OK
        conn.state, conn.need = _ST_CLIENTINIT, 1

    elif st == _ST_CLIENTINIT:
        env = conn.env
        conn.queue(struct.pack(">HH", env.screen_width, env.screen_height) +
                  (_NATIVE_PIXEL_FORMAT if conn.fb else _PIXEL_FORMAT) +
                  struct.pack(">I", len(_SERVER_NAME)) + _SERVER_NAME)
        conn.state, conn.need = _ST_MSGTYPE, 1

    elif st == _ST_MSGTYPE:
        mtype = chunk[0]
        if mtype == 0:      # SetPixelFormat
            conn.state, conn.need = _ST_SETPIXFMT, 3 + 16
        elif mtype == 2:    # SetEncodings
            conn.state, conn.need = _ST_SETENC_HDR, 3
        elif mtype == 3:    # FramebufferUpdateRequest
            conn.state, conn.need = _ST_FBUR, 9
        elif mtype == 4:    # KeyEvent
            conn.state, conn.need = _ST_KEYEVENT, 7
        elif mtype == 5:    # PointerEvent
            conn.state, conn.need = _ST_POINTER, 5
        elif mtype == 6:    # ClientCutText
            conn.state, conn.need = _ST_CUTTEXT_HDR, 7
        else:
            conn.pending_close = True  # unknown type -- can't trust framing

    elif st == _ST_SETPIXFMT:
        (bpp, big_endian, true_color, r_max, g_max, b_max,
         r_shift, g_shift, b_shift) = _parse_pixel_format(chunk[3:19])
        channels = ((r_max, r_shift), (g_max, g_shift), (b_max, b_shift))
        if (true_color and bpp in (8, 16, 24, 32) and
                all(0 < maximum <= 65535 and 0 <= shift < bpp and
                    maximum < (1 << (bpp - shift)) for maximum, shift in channels)):
            conn.current_format = (bpp, big_endian, r_max, g_max, b_max,
                                   r_shift, g_shift, b_shift)
            conn.full_update = True  # replace pixels quantized in the old format
        else:
            conn.pending_close = True
        conn.state, conn.need = _ST_MSGTYPE, 1

    elif st == _ST_SETENC_HDR:
        count = struct.unpack(">H", chunk[1:3])[0]
        conn.encoding = 0
        if count:
            conn._setenc_count = count
            conn.state, conn.need = _ST_SETENC_LIST, 4 * count
        else:
            conn.state, conn.need = _ST_MSGTYPE, 1

    elif st == _ST_SETENC_LIST:
        if conn.shadow_pixels is not None:
            for i in range(0, len(chunk), 4):
                if struct.unpack(">i", chunk[i:i + 4])[0] == 5:
                    conn.encoding = 5
                    break
        conn.state, conn.need = _ST_MSGTYPE, 1

    elif st == _ST_FBUR:
        if not chunk[0]:
            conn.full_update = True
        conn.awaiting_update = True
        conn.state, conn.need = _ST_MSGTYPE, 1

    elif st == _ST_KEYEVENT:
        keysym = struct.unpack(">I", chunk[3:7])[0]
        if keysym in _CTRL_KEYSYMS:
            conn.ctrl = bool(chunk[0])
        elif keysym in _SHIFT_KEYSYMS:
            pass
        elif chunk[0]:  # down-flag
            _inject_key(conn.env, keysym, conn.ctrl)
        conn.state, conn.need = _ST_MSGTYPE, 1

    elif st == _ST_POINTER:
        px, py = struct.unpack(">HH", chunk[1:5])
        _apply_pointer(conn, chunk[0], px, py)
        conn.state, conn.need = _ST_MSGTYPE, 1

    elif st == _ST_CUTTEXT_HDR:
        length = struct.unpack(">I", chunk[3:7])[0]
        if length:
            conn.state, conn.need = _ST_CUTTEXT_BODY, length
        else:
            conn.state, conn.need = _ST_MSGTYPE, 1

    elif st == _ST_CUTTEXT_BODY:
        conn.state, conn.need = _ST_MSGTYPE, 1

def _process_rx(conn):
    while not conn.pending_close and len(conn.rxbuf) >= conn.need:
        chunk = bytes(conn.rxbuf[:conn.need])
        conn.rxbuf = conn.rxbuf[conn.need:]  # bytearray slicing, not del -- MicroPython's bytearray doesn't support slice deletion
        _advance(conn, chunk)

def _drain_tx(conn):
    """Send as much of the queue as the socket takes right now.

    One item per tick (the old behaviour) meant a 24-row update of ~27 small
    items took ~2 s to leave the device. Loop until the socket's send buffer is
    full or the queue is empty; the socket is non-blocking, so EAGAIN just means
    "try again next tick", not "connection lost"."""
    while conn.outq:
        item = conn.outq[0]
        try:
            sent = conn.sock.send(memoryview(item)[conn.outoff:])
        except OSError as e:
            if not (e.args and e.args[0] in _WOULD_BLOCK):
                conn.pending_close = True
                conn.outq.clear()
            return
        if not sent:
            conn.pending_close = True
            conn.outq.clear()
            return
        conn.outoff += sent
        if conn.outoff < len(item):
            return                                 # send buffer full
        conn.outq.pop(0)
        conn.outoff = 0

class _Server:
    def __init__(self, env, port):
        self.env = env
        self.conn = None
        self._rsocks = []  # reused every tick instead of rebuilt -- see _do_tick()

        # Full-screen mode needs the display driver's shadow framebuffer; on
        # firmware without it, fall back to the terminal-only renderer.
        env.fb_shadow = False
        try:
            env.fb_shadow = bool(env.tft.shadow(True))
        except (AttributeError, OSError, ValueError, MemoryError):
            pass

        self.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except (OSError, AttributeError):
            pass
        self.listen_sock.bind(('0.0.0.0', port))
        self.listen_sock.listen(1)

        # This port's machine.Timer only accepts real hardware timer ids
        # (no -1/virtual-timer fallback like some other ports) and this
        # board appears to only have 4 (0-3) -- 0/1 are main.py's display/
        # status-bar timers, 2 is used transiently by the audio player, so
        # 3 is what's left for a persistent app-level background timer.
        self.timer = machine.Timer(3)
        self.timer.init(period=_FB_TICK_MS if env.fb_shadow else _TICK_MS, mode=machine.Timer.PERIODIC,
                        callback=self._tick_isr)

    def _tick_isr(self, t):
        try:
            micropython.schedule(self._tick, 0)
        except RuntimeError:
            pass

    def _tick(self, _):
        try:
            self._do_tick()
        except Exception:
            pass

    def _do_tick(self):
        conn = self.conn

        rsocks = self._rsocks
        rsocks.clear()
        rsocks.append(self.listen_sock)
        if conn and not conn.closed:
            rsocks.append(conn.sock)

        rready, _, _ = select.select(rsocks, [], [], 0)

        if self.listen_sock in rready:
            try:
                csock, addr = self.listen_sock.accept()
                if conn is None or conn.closed:
                    self.conn = conn = _Conn(csock, addr[0], self.env)
                    self.env.sts.notify(f"vncd: {addr[0]} connected")
                else:
                    csock.close()  # already have a client -- reject
            except OSError:
                pass

        if not conn or conn.closed:
            return

        if conn.sock in rready:
            try:
                data = conn.sock.recv(4096)
            except OSError as e:
                data = None
                if not (e.args and e.args[0] in _WOULD_BLOCK):
                    conn.close()
                    return
            if data == b"":
                conn.close()
                return
            elif data:
                conn.rxbuf.extend(data)

        if not conn.pending_close:
            _process_rx(conn)

        if not conn.pending_close and not conn.outq and conn.fb_update is None and conn.awaiting_update:
            if conn.full_update:
                if conn.fb:
                    conn.env.tft.shadow_invalidate()
                else:
                    conn.prev_checksums = [None] * len(conn.row_indices)
                conn.full_update = False
            if conn.fb:
                changed = _queue_fb_update(conn)
            else:
                changed = _scan_changed(conn)
                if changed:
                    _queue_update(conn, changed)
            if changed:
                conn.awaiting_update = False
            # Keep incremental requests pending when there are no changes.

        for _ in range(_FB_BANDS_PER_TICK):
            if not conn.outq and conn.fb_update is not None and not conn.pending_close:
                _queue_fb_band(conn)
            if not conn.outq:
                break
            _drain_tx(conn)
            if conn.outq:
                break  # socket backpressure

        if conn.pending_close and not conn.outq:
            conn.close()

    def stop(self):
        self.timer.deinit()
        if self.env.fb_shadow:
            self.env.fb_shadow = False
            try:
                self.env.tft.shadow(False)
            except (AttributeError, OSError):
                pass
        if self.conn:
            self.conn.close()
        try:
            self.listen_sock.close()
        except OSError:
            pass

_server = None  # module-level -- persists across repeated `vncd` calls

def main(env, args):
    global _server

    if args and args[0] == "stop":
        if _server:
            _server.stop()
            _server = None
            print("VNC server stopped")
        else:
            print("VNC server not running")
        return

    if _server:
        print("VNC server already running\nuse 'vncd stop' to stop it")
        return

    if args and args[0] == "start":
        args = args[1:]
    try:
        port = int(args[0]) if args else 5900
    except ValueError:
        print("Usage: vncd [start] [port] | vncd stop")
        return
    _server = _Server(env, port)
    mode = "full screen" if _server.env.fb_shadow else "terminal only"
    print(f"VNC server ({mode}) on {get_ip()}:{port}\n(background); use 'vncd stop' to stop")
