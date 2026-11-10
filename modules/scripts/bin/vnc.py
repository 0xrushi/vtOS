#
# MicroPython VNC client
# License: MIT
#
#   vnc <host>[:display | ::port] [password]
#   vnc <alias>
#
# Shows a remote desktop on the T-Deck's 320x240 display and sends the
# keyboard and trackball back to it. Pixels are requested as RGB565, big-endian
# (the display's native byte order), so a Raw rectangle is blitted without any
# conversion. A desktop larger than the display is shown through a viewport
# that scrolls to follow the pointer; the easiest setup is a server resolution
# of 320x240 (e.g. Xvnc -geometry 320x240).
#
# Trackball modes (the Mic key cycles them; a small label shows the new mode):
#   MOUSE   move the pointer, click = left button
#   RIGHT   move the pointer, click = right button
#   SCROLL  up/down/left/right = mouse wheel, click = left button
#   KEYS    arrow keys, click = Escape
# A long trackball press quits.
#
# Aliases live in /flash/.vnc (or /sd/.vnc), one per line:
#   alias host[:display | ::port] [password]
#

import sys
import time
import select
import socket

import rfb

_DEFAULT_PORT = 5900
_CONNECT_TIMEOUT = 10
_IO_TIMEOUT = 10
_TAILNET_PEER_WAIT_MS = 30000      # tunnel to the peer coming up
_TAILNET_CONNECT_TIMEOUT = 15
_PROFILE_PATHS = ("/flash/.vnc", "/sd/.vnc")

_TICK_S = 0.02            # select() wait per loop pass
_EDGE = 24                # pointer this close to a viewport edge scrolls it
_PAN_MIN_MS = 120         # minimum gap between viewport-move refreshes
_TOAST_MS = 1200
_MAX_KEYS_PER_PASS = 16
_MAX_WHEEL_PER_PASS = 4

MIC_KEY = 0x1C            # the T-Deck's microphone key arrives as this byte

MODE_MOUSE = 0
MODE_RIGHT = 1
MODE_SCROLL = 2
MODE_KEYS = 3
_MODE_LABELS = ("MOUSE", "RIGHT", "SCROLL", "KEYS")
_MODE_COLORS = (0x0400, 0xA800, 0x001F, 0xA500)   # green, red, blue, orange

KEY_BACKSPACE = 0xFF08
KEY_TAB = 0xFF09
KEY_RETURN = 0xFF0D
KEY_ESCAPE = 0xFF1B
KEY_LEFT = 0xFF51
KEY_UP = 0xFF52
KEY_RIGHT = 0xFF53
KEY_DOWN = 0xFF54


def _ticks():
    try:
        return time.ticks_ms()
    except AttributeError:
        return int(time.monotonic() * 1000)


def _since(start, now):
    try:
        return time.ticks_diff(now, start)
    except AttributeError:
        return now - start


# What tailscale reports when the tunnel is not usable (same codes as ssh).
_TS_ERRORS = {
    -2010: "Tailscale is down: connect Wi-Fi and run `tailscale up` first",
    -2011: "Tailscale peer unavailable: check its IP, device approval and tailnet access rules",
    -2012: "Tailscale connection timed out: check peer connectivity and tailnet access to the VNC port",
    -2013: "cannot bind to the Tailscale interface: reconnect with tailscale down/up",
}


# --- command line ------------------------------------------------------------

def split_target(text):
    """'host', 'host:1' (display), 'host::5901' (port) -> (host, port)."""
    if "::" in text:
        host, _, port = text.partition("::")
        if not port.isdigit() or int(port) > 65535:
            raise ValueError("bad port in " + text)
        return host, int(port)
    if ":" in text:
        host, _, num = text.partition(":")
        if not num.isdigit() or int(num) > 65535:
            raise ValueError("bad display in " + text)
        n = int(num)
        return host, (_DEFAULT_PORT + n if n < 100 else n)
    return text, _DEFAULT_PORT


def parse_profiles(text):
    """{alias: (host, port, password or None)} from the .vnc file text."""
    profiles = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            host, port = split_target(parts[1])
        except ValueError:
            continue
        profiles[parts[0]] = (host, port, parts[2] if len(parts) > 2 else None)
    return profiles


def load_profiles(paths=_PROFILE_PATHS):
    for path in paths:
        try:
            with open(path, "r") as f:
                return parse_profiles(f.read())
        except OSError:
            continue
    return {}


def parse_target(args, profiles):
    """-> (host, port, password or None); ValueError if unusable."""
    if not args:
        raise ValueError("no host given")
    first = args[0]
    password = args[1] if len(args) > 1 else None
    if first in profiles:
        host, port, saved = profiles[first]
        return host, port, password if password is not None else saved
    host, port = split_target(first)
    if not host:
        raise ValueError("no host given")
    return host, port, password


def is_tailnet_host(host):
    """Numeric Tailscale IPv4 only (100.64.0.0 - 100.127.255.255)."""
    parts = host.split(".")
    if len(parts) != 4 or not all(p.isdigit() and len(p) <= 3 for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    return a == 100 and 64 <= b <= 127 and all(int(p) <= 255 for p in parts)


# --- input translation -------------------------------------------------------

def keysym_for(byte):
    """X11 keysym for one byte from the keyboard, or None to ignore it."""
    if byte in (13, 10):
        return KEY_RETURN
    if byte in (8, 127):
        return KEY_BACKSPACE
    if byte == 9:
        return KEY_TAB
    if byte == 27:
        return KEY_ESCAPE
    if 0x20 <= byte <= 0x7E or byte >= 0xA0:      # ASCII and Latin-1 keysyms
        return byte
    return None


_POINTER_GAIN = 4          # pixels per pulse at the slowest roll; raise for a faster pointer


def accelerate(count):
    """Trackball pulses in one pass -> pixels: slow rolls stay precise,
    fast rolls cover distance (with the gain of 4: 1 -> 4, 2 -> 12, 4 -> 40,
    8 -> 144)."""
    mag = abs(count)
    px = _POINTER_GAIN * mag * (mag + 1) // 2
    return -px if count < 0 else px


class Pending:
    """Trackball state gathered by the 30 ms system tick (env.pointer) and
    picked up by the session loop, so no socket write ever happens inside
    that tick."""

    def __init__(self):
        self.dx = 0
        self.dy = 0
        self.clicks = 0

    def add(self, dx, dy, click):
        self.dx += dx
        self.dy += dy
        if click:
            self.clicks += 1

    def take(self):
        # The system tick can run add() between any two statements here, so
        # subtract what was read instead of zeroing: input that arrives
        # mid-way stays counted for the next pass.
        dx = self.dx
        dy = self.dy
        clicks = self.clicks
        self.dx -= dx
        self.dy -= dy
        self.clicks -= clicks
        return dx, dy, clicks


# --- display -------------------------------------------------------------------

# 3x5 glyphs (rows top to bottom, 3 bits each) for the mode labels.
_GLYPHS = {
    "C": (3, 4, 4, 4, 3), "E": (7, 4, 6, 4, 7), "G": (3, 4, 5, 5, 3),
    "H": (5, 5, 7, 5, 5), "I": (7, 2, 2, 2, 7), "K": (5, 5, 6, 5, 5),
    "L": (4, 4, 4, 4, 7), "M": (5, 7, 7, 5, 5), "O": (2, 5, 5, 5, 2),
    "R": (6, 5, 6, 5, 5), "S": (3, 4, 2, 1, 6), "T": (7, 2, 2, 2, 2),
    "U": (5, 5, 5, 5, 7), "Y": (5, 5, 2, 2, 2),
}
_SCALE = 2
_PAD = 2


def label_size(text):
    return (len(text) * 4 * _SCALE - _SCALE + 2 * _PAD, 5 * _SCALE + 2 * _PAD)


def draw_label(tft, text, x, y, fg, bg):
    """Draw a small boxed label with fill_rect only (no font module needed);
    returns the (x, y, w, h) it covered."""
    w, h = label_size(text)
    tft.fill_rect(x, y, w, h, bg)
    cx = x + _PAD
    for ch in text:
        rows = _GLYPHS.get(ch)
        if rows:
            for r in range(5):
                bits = rows[r]
                for c in range(3):
                    if bits & (4 >> c):
                        tft.fill_rect(cx + c * _SCALE, y + _PAD + r * _SCALE,
                                      _SCALE, _SCALE, fg)
        cx += 4 * _SCALE
    return x, y, w, h


class Screen(rfb.Sink):
    """Maps the remote framebuffer onto the display through a viewport and
    draws Raw rectangles straight from the socket, a chunk of rows at a time
    (there is no framebuffer copy in RAM)."""

    CHUNK = 16384

    def __init__(self, tft, width, height):
        self.tft = tft
        self.sw = width
        self.sh = height
        self.rw = 0
        self.rh = 0
        self.vx = 0
        self.vy = 0
        self.vw = 0
        self.vh = 0
        self.ox = 0
        self.oy = 0
        self.buf = bytearray(self.CHUNK)
        self.view = memoryview(self.buf)
        self.resized = False
        self.on_cut = None

    def set_remote(self, w, h):
        self.rw = w
        self.rh = h
        self.vw = min(self.sw, w)
        self.vh = min(self.sh, h)
        self.ox = (self.sw - self.vw) // 2
        self.oy = (self.sh - self.vh) // 2
        self.vx = max(0, min(self.vx, w - self.vw))
        self.vy = max(0, min(self.vy, h - self.vh))
        self.tft.fill(0)

    def region(self):
        return self.vx, self.vy, self.vw, self.vh

    def follow(self, px, py):
        """Scroll the viewport so the pointer stays visible; True if moved."""
        nx = self.vx
        ny = self.vy
        if self.rw > self.vw:
            if px < nx + _EDGE:
                nx = px - _EDGE
            elif px > nx + self.vw - 1 - _EDGE:
                nx = px - (self.vw - 1 - _EDGE)
            nx = max(0, min(nx, self.rw - self.vw))
        if self.rh > self.vh:
            if py < ny + _EDGE:
                ny = py - _EDGE
            elif py > ny + self.vh - 1 - _EDGE:
                ny = py - (self.vh - 1 - _EDGE)
            ny = max(0, min(ny, self.rh - self.vh))
        moved = nx != self.vx or ny != self.vy
        self.vx = nx
        self.vy = ny
        return moved

    def restore(self, x, y, w, h):
        """Undo an overlay at display rect (x, y, w, h): black out whatever
        lies outside the picture and return the remote rect to re-request
        (or None if the overlay was entirely outside it)."""
        self.tft.fill_rect(x, y, w, h, 0)
        x0 = max(x, self.ox)
        y0 = max(y, self.oy)
        x1 = min(x + w, self.ox + self.vw)
        y1 = min(y + h, self.oy + self.vh)
        if x1 <= x0 or y1 <= y0:
            return None
        return (self.vx + x0 - self.ox, self.vy + y0 - self.oy, x1 - x0, y1 - y0)

    # rfb.Sink

    def resize(self, width, height):
        self.set_remote(width, height)
        self.resized = True

    def cut_text(self, text):
        if self.on_cut is not None:
            self.on_cut(text)

    def raw_rect(self, client, x, y, w, h):
        if w == 0 or h == 0:
            return
        row_bytes = w * rfb.BYTES_PER_PIXEL
        if row_bytes > len(self.buf):
            self.buf = bytearray(row_bytes)
            self.view = memoryview(self.buf)
        buf = self.buf
        view = self.view
        rows_per = len(buf) // row_bytes
        vx, vy, vw, vh = self.vx, self.vy, self.vw, self.vh
        cx0 = max(x, vx)
        cx1 = min(x + w, vx + vw)
        cw = cx1 - cx0
        skip_bytes = (cx0 - x) * rfb.BYTES_PER_PIXEL
        cut_bytes = cw * rfb.BYTES_PER_PIXEL
        blit = self.tft.blit_buffer
        r = 0
        while r < h:
            n = min(rows_per, h - r)
            client.read_into(buf, n * row_bytes)
            ry0 = max(y + r, vy)
            ry1 = min(y + r + n, vy + vh)
            if cw > 0 and ry1 > ry0:
                first = ry0 - (y + r)
                count = ry1 - ry0
                sx = self.ox + cx0 - vx
                sy = self.oy + ry0 - vy
                if cw == w:
                    blit(view[first * row_bytes:(first + count) * row_bytes],
                         sx, sy, w, count)
                else:
                    for i in range(count):
                        start = (first + i) * row_bytes + skip_bytes
                        blit(view[start:start + cut_bytes], sx, sy + i, cw, 1)
            r += n


# --- the session ---------------------------------------------------------------

class Session:
    def __init__(self, env, sock, client, screen):
        self.env = env
        self.sock = sock
        self.client = client
        self.screen = screen
        self.mode = MODE_MOUSE
        self.px = screen.vx + screen.vw // 2
        self.py = screen.vy + screen.vh // 2
        self.pending = Pending()
        self.one = bytearray(1)
        self.toast = None            # (expiry ticks, display rect)
        self.pan_due = False
        self.last_pan = _ticks()

    def start(self):
        self.client.request_update(*self.screen.region(), incremental=False)
        self.show_mode()

    def run(self):
        """Run until the connection ends or the user quits; returns a reason."""
        env = self.env
        env.pointer = self.pending.add
        try:
            self.start()
            while True:
                self.step(_TICK_S)
        except KeyboardInterrupt:
            return "closed"
        except rfb.RFBError as err:
            return str(err)
        except OSError as err:
            return "connection lost: %s" % (err,)
        finally:
            env.pointer = None

    def step(self, timeout):
        screen = self.screen
        client = self.client
        if select.select([self.sock], [], [], timeout)[0]:
            mtype = client.read_message(screen)
            if screen.resized:
                screen.resized = False
                self.px = min(self.px, screen.rw - 1)
                self.py = min(self.py, screen.rh - 1)
                screen.follow(self.px, self.py)
                client.request_update(*screen.region(), incremental=False)
            elif mtype == 0:
                client.request_update(*screen.region(), incremental=True)
        self.read_keys()
        self.apply_trackball()
        self.housekeeping()

    # keyboard

    def read_keys(self):
        kvm = self.env.kvm
        one = self.one
        for _ in range(_MAX_KEYS_PER_PASS):
            if not kvm.readinto(one):
                return
            byte = one[0]
            if byte == MIC_KEY:
                self.set_mode((self.mode + 1) % 4)
                continue
            sym = keysym_for(byte)
            if sym is not None:
                self.client.key_press(sym, 0x41 <= sym <= 0x5A)

    # trackball

    def apply_trackball(self):
        dx, dy, clicks = self.pending.take()
        mode = self.mode
        client = self.client
        if mode == MODE_MOUSE or mode == MODE_RIGHT:
            if dx or dy:
                self.px = max(0, min(self.px + accelerate(dx), self.screen.rw - 1))
                self.py = max(0, min(self.py + accelerate(dy), self.screen.rh - 1))
                client.pointer_event(0, self.px, self.py)
                if self.screen.follow(self.px, self.py):
                    self.pan_due = True
            if clicks:
                button = rfb.BTN_RIGHT if mode == MODE_RIGHT else rfb.BTN_LEFT
                client.pointer_click(button, self.px, self.py)
        elif mode == MODE_SCROLL:
            self.wheel(dy, rfb.BTN_WHEEL_DOWN, rfb.BTN_WHEEL_UP)
            self.wheel(dx, rfb.BTN_WHEEL_RIGHT, rfb.BTN_WHEEL_LEFT)
            if clicks:
                client.pointer_click(rfb.BTN_LEFT, self.px, self.py)
        else:
            self.arrows(dx, KEY_RIGHT, KEY_LEFT)
            self.arrows(dy, KEY_DOWN, KEY_UP)
            if clicks:
                client.key_press(KEY_ESCAPE)

    def wheel(self, delta, positive, negative):
        if delta:
            button = positive if delta > 0 else negative
            for _ in range(min(abs(delta), _MAX_WHEEL_PER_PASS)):
                self.client.pointer_click(button, self.px, self.py)

    def arrows(self, delta, positive, negative):
        # Same dead zone the shell uses, so a nudge does not fire a key.
        n = min(3, abs(delta) // 2)
        for _ in range(n):
            self.client.key_press(positive if delta > 0 else negative)

    # modes and the label

    def set_mode(self, mode):
        self.mode = mode
        self.show_mode()

    def show_mode(self):
        self.clear_toast()
        rect = draw_label(self.env.tft, _MODE_LABELS[self.mode], 0, 0,
                          0xFFFF, _MODE_COLORS[self.mode])
        self.toast = (_ticks(), rect)

    def clear_toast(self):
        if self.toast is not None:
            rect = self.screen.restore(*self.toast[1])
            self.toast = None
            if rect is not None:
                self.client.request_update(*rect, incremental=False)

    def housekeeping(self):
        now = _ticks()
        if self.toast is not None and _since(self.toast[0], now) >= _TOAST_MS:
            self.clear_toast()
        if self.pan_due and _since(self.last_pan, now) >= _PAN_MIN_MS:
            self.pan_due = False
            self.last_pan = now
            self.client.request_update(*self.screen.region(), incremental=False)


# --- entry point ---------------------------------------------------------------

def _read_password(prompt):
    sys.stdout.write(prompt)
    chars = []
    while True:
        ch = sys.stdin.read(1)
        if ch in ("\r", "\n"):
            sys.stdout.write("\r\n")
            return "".join(chars)
        if ch in ("\x08", "\x7f"):
            if chars:
                chars.pop()
        elif ch == "\x1b":
            sys.stdout.write("\r\n")
            return None
        elif ch and 32 <= ord(ch) <= 126:
            chars.append(ch)


def _wifi_no_sleep():
    """Turn Wi-Fi power save off for lower pointer latency; returns a function
    that puts it back."""
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        if sta.isconnected():
            previous = sta.config("pm")
            if previous != sta.PM_NONE:
                sta.config(pm=sta.PM_NONE)

                def restore():
                    try:
                        sta.config(pm=previous)
                    except OSError:
                        pass
                return restore
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    return lambda: None


def tailnet_source(host):
    """This device's own tailnet address once the tunnel to `host` is up.
    A socket bound to it is routed through the tunnel (see modtailscale)."""
    try:
        import modtailscale
    except ImportError:
        raise OSError("this firmware has no Tailscale support")
    try:
        return modtailscale.wait_peer(host, _TAILNET_PEER_WAIT_MS)
    except OSError as err:
        code = err.args[0] if err.args else None
        raise OSError(_TS_ERRORS.get(code, str(err)))


def connect(host, port):
    tailnet = is_tailnet_host(host)
    source = tailnet_source(host) if tailnet else None
    addr = socket.getaddrinfo(host, port)[0][-1]
    sock = socket.socket()
    try:
        if source:
            sock.bind((source, 0))
        sock.settimeout(_TAILNET_CONNECT_TIMEOUT if tailnet else _CONNECT_TIMEOUT)
        sock.connect(addr)
        sock.settimeout(_IO_TIMEOUT)
        try:
            sock.setsockopt(6, 1, 1)      # IPPROTO_TCP, TCP_NODELAY
        except (OSError, AttributeError):
            pass
    except OSError:
        sock.close()
        raise
    return sock


def usage():
    print("Usage: vnc <host>[:display | ::port] [password]")
    print("       vnc <alias>        (aliases: /flash/.vnc)")
    print("Mic key: trackball mode (mouse/right/scroll/keys)")
    print("Long trackball press: quit")


def main(env, args):
    args = list(args)
    if not args or args[0] in ("-h", "--help"):
        usage()
        return
    if getattr(env, "tft", None) is None:
        print("vnc: this firmware does not expose the display (env.tft)")
        return
    try:
        host, port, password = parse_target(args, load_profiles())
    except ValueError as err:
        print("vnc: " + str(err))
        return
    via = " over Tailscale" if is_tailnet_host(host) else ""
    print("vnc: connecting to %s:%d%s ..." % (host, port, via))
    try:
        sock = connect(host, port)
    except OSError as err:
        print("vnc: cannot connect: %s" % (err,))
        return

    restore_wifi = _wifi_no_sleep()
    client = rfb.Client(sock)
    reason = None
    drew = False
    try:
        try:
            client.handshake(password,
                             ask_password=lambda: _read_password("VNC password: "))
        except rfb.RFBError as err:
            reason = str(err)
        except OSError as err:
            reason = "connection lost: %s" % (err,)
        else:
            print("vnc: %s (%dx%d)" % (client.name, client.width, client.height))
            client.set_pixel_format()
            client.set_encodings([rfb.ENC_RAW, rfb.ENC_DESKTOP_SIZE])
            screen = Screen(env.tft, env.screen_width, env.screen_height)
            screen.on_cut = lambda text: setattr(env, "clipboard", text)
            env.sd_busy = True          # stop terminal and status bar redraws
            drew = True
            screen.set_remote(client.width, client.height)
            reason = Session(env, sock, client, screen).run()
    except KeyboardInterrupt:
        reason = "closed"
    finally:
        try:
            sock.close()
        except OSError:
            pass
        restore_wifi()
        env.pointer = None
        env.sd_busy = False
        if drew:
            sys.stdout.write("\x1b[2J\x1b[H")
    if reason and reason != "closed":
        print("vnc: " + reason)
