#
# Clipboard service: receives text from a phone over HTTP and keeps it in
# env.clipboard. See docs/CLIPBOARD.md.
#
#   GET  /ping   -> {"name": "vtos", "clip": <chars>}      (validates the token)
#   POST /clip   -> body = UTF-8 text, stored as the clipboard
#   POST /pair   -> body = 6-digit code from `clip pair`; returns the token
#
# The first two require "Authorization: Bearer <token>". A UDP datagram
# "VTOS?" to the same port is answered with the device's name and port so the
# phone app can find it. The server has no hardware
# timer of its own (Timer 3 belongs to telnetd/vncd/webvncd): boot.py calls
# tick(env) from its 30 ms loop, and poll() does a zero-timeout select.
#

import json
import os
import select
import socket
import time

import network

CONFIG = "/flash/.clip.json"
DEFAULT_PORT = 8787
MAX_BODY = 4096
MAX_HEAD = 1024
CONN_TIMEOUT_MS = 3000
FLASH_MS = 3000
PAIR_MS = 120000
PAIR_TRIES = 3
PASTE_CHUNK = 8  # chars per 30 ms tick; the key-injection ring is only 32 bytes

_REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
    403: "Forbidden", 405: "Method Not Allowed", 408: "Request Timeout", 413: "Payload Too Large",
    431: "Request Header Fields Too Large",
}


def _new_token():
    return "".join("%02x" % b for b in os.urandom(16))


def load_config():
    cfg = {}
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    changed = False
    token = cfg.get("token")
    if not isinstance(token, str) or len(token) != 32:
        cfg["token"] = _new_token()
        changed = True
    if not isinstance(cfg.get("port"), int):
        cfg["port"] = DEFAULT_PORT
        changed = True
    if not isinstance(cfg.get("autostart"), bool):
        cfg["autostart"] = False
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG, "w") as f:
        json.dump(cfg, f)


def get_ip():
    sta = network.WLAN(network.STA_IF)
    if sta.active() and sta.isconnected():
        return sta.ifconfig()[0]
    return None


def _same(a, b):
    """Constant-time string compare."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


def process(buf, token, pair_code=None):
    """Evaluate a (possibly partial) request.

    Returns None while more bytes are needed, otherwise
    (code, payload_dict, text_or_None). text is set only for an accepted
    POST /clip. pair_code is the active pairing code (None = not pairing)."""
    end = buf.find(b"\r\n\r\n")
    if end < 0:
        if len(buf) > MAX_HEAD:
            return 431, {"error": "headers too large"}, None
        return None
    if end > MAX_HEAD:
        return 431, {"error": "headers too large"}, None
    try:
        head = bytes(buf[:end]).decode("utf-8")
    except UnicodeError:
        return 400, {"error": "bad request"}, None
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return 400, {"error": "bad request"}, None
    method = parts[0]
    path = parts[1].split("?")[0]
    headers = {}
    for ln in lines[1:]:
        k, sep, v = ln.partition(":")
        if sep:
            headers[k.strip().lower()] = v.strip()

    if path == "/pair":
        if method != "POST":
            return 405, {"error": "method not allowed"}, None
        if pair_code is None:
            return 403, {"error": "pairing not active (run: clip pair)"}, None
        r = _body(buf, end, headers)
        if r is None or isinstance(r[0], int):
            return r
        if _same(r[0].strip(), pair_code):
            return 200, {"token": token}, None
        return 401, {"error": "wrong code"}, None

    auth = headers.get("authorization", "")
    if not (auth.startswith("Bearer ") and _same(auth[7:], token)):
        return 401, {"error": "unauthorized"}, None

    if path == "/ping":
        if method != "GET":
            return 405, {"error": "method not allowed"}, None
        return 200, {"name": "vtos"}, None
    if path != "/clip":
        return 404, {"error": "not found"}, None
    if method != "POST":
        return 405, {"error": "method not allowed"}, None

    r = _body(buf, end, headers)
    if r is None or isinstance(r[0], int):
        return r
    return 200, {"ok": True, "len": len(r[0])}, r[0]


def _body(buf, end, headers):
    """None = need more bytes; (text,) = body; (code, payload, None) = error."""
    try:
        length = int(headers.get("content-length", ""))
    except ValueError:
        return 400, {"error": "content-length required"}, None
    if length < 0:
        return 400, {"error": "bad content-length"}, None
    if length > MAX_BODY:
        return 413, {"error": "too large (max %d bytes)" % MAX_BODY}, None
    body = buf[end + 4:]
    if len(body) < length:
        return None
    try:
        return (bytes(body[:length]).decode("utf-8"),)
    except UnicodeError:
        return 400, {"error": "body is not utf-8"}, None


class ClipServer:
    def __init__(self, env, cfg):
        self.env = env
        self.port = cfg["port"]
        self.token = cfg["token"]
        self.sock = None
        self.conn = None
        self.buf = bytearray()
        self.deadline = 0
        self.next_bind = 0
        self.continued = False
        self.udp = None
        self.pair_code = None
        self.pair_until = 0
        self.pair_tries = 0

    @property
    def listening(self):
        return self.sock is not None

    def _bind(self):
        if get_ip() is None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except (OSError, AttributeError):
                pass
            s.bind(("0.0.0.0", self.port))
            s.listen(1)
            s.setblocking(False)
        except OSError:
            s.close()
            return
        self.sock = s
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.bind(("0.0.0.0", self.port))
            u.setblocking(False)
            self.udp = u
        except OSError:
            self.udp = None

    def start_pairing(self, code):
        self.pair_code = code
        self.pair_tries = 0
        self.pair_until = time.ticks_add(time.ticks_ms(), PAIR_MS)

    def _pair_code(self):
        if self.pair_code is not None and time.ticks_diff(time.ticks_ms(), self.pair_until) >= 0:
            self.pair_code = None
        return self.pair_code

    def _drop_conn(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None
        self.buf = bytearray()
        self.continued = False

    def close(self):
        self._drop_conn()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        if self.udp is not None:
            try:
                self.udp.close()
            except OSError:
                pass
        self.udp = None

    def _reply(self, code, payload):
        body = json.dumps(payload)
        data = ("HTTP/1.1 %d %s\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n%s"
                % (code, _REASONS.get(code, "Error"), len(body), body))
        try:
            self.conn.send(data.encode())
        except OSError:
            pass
        self._drop_conn()

    def _advance(self):
        pairing = self.buf.startswith(b"POST /pair")
        result = process(self.buf, self.token, self._pair_code())
        if result is None:
            if len(self.buf) > MAX_HEAD + MAX_BODY + 4:
                self._reply(413, {"error": "too large"})
            elif not self.continued and b"100-continue" in bytes(self.buf).lower():
                # curl sends Expect: 100-continue for bodies over ~1 KB.
                self.continued = True
                try:
                    self.conn.send(b"HTTP/1.1 100 Continue\r\n\r\n")
                except OSError:
                    pass
            return
        code, payload, text = result
        if pairing:
            if code == 401:
                self.pair_tries += 1
                if self.pair_tries >= PAIR_TRIES:
                    self.pair_code = None
            elif code == 200:
                self.pair_code = None
                sts = getattr(self.env, "sts", None)
                if sts is not None:
                    sts.notify("clip: phone paired")
        if text is not None:
            payload["len"] = len(text)
            self.set_clip(text)
        elif code == 200 and "name" in payload:
            payload["clip"] = len(self.env.clipboard)
        self._reply(code, payload)

    def set_clip(self, text):
        env = self.env
        env.clipboard = text
        env.clip_flash_until = time.ticks_add(time.ticks_ms(), FLASH_MS)
        sts = getattr(env, "sts", None)
        if sts is not None:
            sts.notify("clip: %d chars received" % len(text))

    def poll(self):
        now = time.ticks_ms()
        if self.sock is None:
            if time.ticks_diff(now, self.next_bind) >= 0:
                self.next_bind = time.ticks_add(now, 1000)
                self._bind()
            return

        rsocks = [self.sock]
        if self.udp is not None:
            rsocks.append(self.udp)
        if self.conn is not None:
            rsocks.append(self.conn)
        try:
            ready, _, _ = select.select(rsocks, [], [], 0)
        except OSError:
            self.close()
            return

        if self.udp is not None and self.udp in ready:
            try:
                msg, addr = self.udp.recvfrom(64)
                if msg.strip() == b"VTOS?":
                    reply = json.dumps({"name": "vtos", "port": self.port,
                                        "pairing": self._pair_code() is not None})
                    self.udp.sendto(reply.encode(), addr)
            except OSError:
                pass

        if self.sock in ready:
            try:
                csock, _ = self.sock.accept()
            except OSError:
                csock = None
            if csock is not None:
                self._drop_conn()
                csock.setblocking(False)
                self.conn = csock
                self.deadline = time.ticks_add(now, CONN_TIMEOUT_MS)

        if self.conn is not None and self.conn in ready:
            try:
                data = self.conn.recv(1024)
            except OSError:
                data = b""
            if not data:
                self._drop_conn()
            else:
                self.buf += data
                self._advance()

        if self.conn is not None and time.ticks_diff(now, self.deadline) >= 0:
            self._reply(408, {"error": "timeout"})


def start(env, port=None):
    if env.clipd is not None:
        return env.clipd
    cfg = load_config()
    if port is not None:
        cfg["port"] = port
        save_config(cfg)
    env.clipd = ClipServer(env, cfg)
    return env.clipd


def stop(env):
    if env.clipd is not None:
        env.clipd.close()
        env.clipd = None


def _printable(text):
    """Keep pasted text from carrying terminal control codes."""
    text = text.replace("\r\n", "\r").replace("\n", "\r")
    return "".join(c for c in text if c in "\r\t" or (ord(c) >= 32 and ord(c) != 127))


def paste(env, text=None):
    """Queue text to be typed into whatever is reading the keyboard."""
    env.paste_buf = _printable(env.clipboard if text is None else text)


def tick(env):
    """Called from boot.py's 30 ms loop."""
    d = env.clipd
    if d is not None:
        try:
            d.poll()
        except Exception:
            pass
    p = env.paste_buf
    if p:
        env.paste_buf = p[PASTE_CHUNK:]
        env.kvm.inject(p[:PASTE_CHUNK])
