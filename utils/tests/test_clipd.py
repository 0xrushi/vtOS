"""Test the phone clipboard service: HTTP parsing, auth, limits, paste."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def clipd(tmp_path):
    path = Path(__file__).resolve().parents[2] / "modules/scripts/sys/clipd.py"
    spec = importlib.util.spec_from_file_location("vtos_clipd_test", path)
    mod = importlib.util.module_from_spec(spec)
    import sys
    from unittest.mock import patch
    fake_net = SimpleNamespace(STA_IF=0, WLAN=lambda _: SimpleNamespace(
        active=lambda: True, isconnected=lambda: True,
        ifconfig=lambda: ("192.168.1.50",)))
    with patch.dict(sys.modules, {"network": fake_net}):
        spec.loader.exec_module(mod)
    mod.CONFIG = str(tmp_path / "clip.json")
    mod.os = SimpleNamespace(urandom=lambda n: bytes(range(n)))
    clock = SimpleNamespace(now=0)
    mod.time = SimpleNamespace(
        ticks_ms=lambda: clock.now,
        ticks_add=lambda a, b: a + b,
        ticks_diff=lambda a, b: a - b,
    )
    mod.clock = clock
    return mod


def req(method, path, body=b"", token=TOKEN, extra=""):
    head = "%s %s HTTP/1.1\r\nHost: x\r\n" % (method, path)
    if token:
        head += "Authorization: Bearer %s\r\n" % token
    if method == "POST":
        head += "Content-Length: %d\r\n" % len(body)
    return (head + extra + "\r\n").encode() + body


def test_post_stores_text(clipd):
    code, payload, text = clipd.process(bytearray(req("POST", "/clip", "héllo".encode())), TOKEN)
    assert (code, text) == (200, "héllo")
    assert payload["ok"] is True


def test_partial_request_needs_more_bytes(clipd):
    full = req("POST", "/clip", b"hello world")
    assert clipd.process(bytearray(full[:20]), TOKEN) is None
    assert clipd.process(bytearray(full[:-3]), TOKEN) is None
    assert clipd.process(bytearray(full), TOKEN)[2] == "hello world"


def test_auth_required_and_checked_before_body(clipd):
    assert clipd.process(bytearray(req("POST", "/clip", b"x", token=None)), TOKEN)[0] == 401
    assert clipd.process(bytearray(req("POST", "/clip", b"x", token="wrong")), TOKEN)[0] == 401
    # unauthenticated callers get 401 even with the body still in flight
    head_only = req("POST", "/clip", b"x" * 10, token="bad")[:-10]
    assert clipd.process(bytearray(head_only), TOKEN)[0] == 401


def test_body_size_limit(clipd):
    big = b"a" * (clipd.MAX_BODY + 1)
    head_only = req("POST", "/clip", big)[:-len(big)]
    assert clipd.process(bytearray(head_only), TOKEN)[0] == 413
    ok = b"a" * clipd.MAX_BODY
    assert clipd.process(bytearray(req("POST", "/clip", ok)), TOKEN)[0] == 200


def test_ping_routes_and_methods(clipd):
    assert clipd.process(bytearray(req("GET", "/ping")), TOKEN)[0] == 200
    assert clipd.process(bytearray(req("POST", "/ping", b"")), TOKEN)[0] == 405
    assert clipd.process(bytearray(req("GET", "/clip")), TOKEN)[0] == 405
    assert clipd.process(bytearray(req("GET", "/nope")), TOKEN)[0] == 404


def test_bad_inputs(clipd):
    assert clipd.process(bytearray(req("POST", "/clip", b"\xff\xfe")), TOKEN)[0] == 400
    no_len = b"POST /clip HTTP/1.1\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n\r\n"
    assert clipd.process(bytearray(no_len), TOKEN)[0] == 400
    assert clipd.process(bytearray(b"x" * (clipd.MAX_HEAD + 1)), TOKEN)[0] == 431


def test_config_generates_and_persists_token(clipd):
    cfg = clipd.load_config()
    assert len(cfg["token"]) == 32 and cfg["port"] == 8787 and cfg["autostart"] is False
    assert clipd.load_config()["token"] == cfg["token"]
    with open(clipd.CONFIG) as f:
        assert json.load(f)["token"] == cfg["token"]


class FakeConn:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""
        self.closed = False

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def send(self, data):
        self.sent += data
        return len(data)

    def setblocking(self, flag):
        pass

    def close(self):
        self.closed = True


class FakeListen:
    def __init__(self, conn=None):
        self.conn = conn

    def accept(self):
        conn, self.conn = self.conn, None
        return conn, ("1.2.3.4", 5)

    def close(self):
        pass


def make_server(clipd, conn):
    notes = []
    env = SimpleNamespace(
        clipboard="", clip_flash_until=None, paste_buf="", clipd=None,
        sts=SimpleNamespace(notify=notes.append),
        kvm=SimpleNamespace(injected=[], inject=lambda s: env.kvm.injected.append(s)))
    server = clipd.ClipServer(env, {"port": 8787, "token": TOKEN})
    server.sock = FakeListen(conn)

    def fake_select(r, w, x, t):
        ready = [s for s in r if (s is server.conn and s.chunks) or (s is server.sock and server.sock.conn)]
        return ready, [], []

    clipd.select = SimpleNamespace(select=fake_select)
    return server, env, notes


def test_poll_end_to_end_sets_clipboard_and_notifies(clipd):
    data = req("POST", "/clip", b"from phone")
    conn = FakeConn([data[:15], data[15:]])
    server, env, notes = make_server(clipd, conn)
    server.poll()   # accepts the connection
    server.poll()   # reads the first fragment
    assert env.clipboard == ""
    server.poll()   # second fragment completes the request
    assert env.clipboard == "from phone"
    assert notes == ["clip: 10 chars received"]
    assert conn.sent.startswith(b"HTTP/1.1 200 OK") and conn.closed
    assert env.clip_flash_until == 3000


def test_wrong_token_leaves_clipboard_untouched(clipd):
    conn = FakeConn([req("POST", "/clip", b"evil", token="nope")])
    server, env, notes = make_server(clipd, conn)
    env.clipboard = "keep"
    server.poll()
    server.poll()
    assert env.clipboard == "keep" and not notes
    assert conn.sent.startswith(b"HTTP/1.1 401")


def test_idle_connection_times_out(clipd):
    conn = FakeConn([b"POST /clip HT"])
    server, env, notes = make_server(clipd, conn)
    server.poll()
    server.poll()
    clipd.clock.now = clipd.CONN_TIMEOUT_MS + 1
    server.poll()
    assert conn.sent.startswith(b"HTTP/1.1 408") and conn.closed


def test_paste_strips_control_codes_and_drips(clipd):
    env = SimpleNamespace(clipboard="ls\r\n\x1b[31mrm\x03\ttab\x7f", paste_buf="", clipd=None,
                          kvm=SimpleNamespace(injected=[]))
    env.kvm.inject = env.kvm.injected.append
    clipd.paste(env)
    assert env.paste_buf == "ls\r[31mrm\ttab"
    while env.paste_buf:
        clipd.tick(env)
    assert "".join(env.kvm.injected) == "ls\r[31mrm\ttab"
    assert all(len(chunk) <= clipd.PASTE_CHUNK for chunk in env.kvm.injected)


def test_pair_requires_active_window_and_right_code(clipd):
    post = lambda body: bytearray(req("POST", "/pair", body, token=None))
    assert clipd.process(post(b"123456"), TOKEN, None)[0] == 403
    assert clipd.process(post(b"000000"), TOKEN, "123456")[0] == 401
    code, payload, text = clipd.process(post(b"123456"), TOKEN, "123456")
    assert (code, payload["token"], text) == (200, TOKEN, None)
    # the token is never handed out through other routes without auth
    assert clipd.process(bytearray(req("GET", "/ping", token=None)), TOKEN, "123456")[0] == 401


def test_pairing_locks_after_three_wrong_codes_and_expires(clipd):
    conns = [FakeConn([req("POST", "/pair", b"000000", token=None)]) for _ in range(3)]
    server, env, notes = make_server(clipd, conns[0])
    server.start_pairing("123456")
    for c in conns:
        server.sock.conn = c
        server.poll()
        server.poll()
        assert c.sent.startswith(b"HTTP/1.1 401")
    assert server.pair_code is None
    good = FakeConn([req("POST", "/pair", b"123456", token=None)])
    server.sock.conn = good
    server.poll()
    server.poll()
    assert good.sent.startswith(b"HTTP/1.1 403")

    server.start_pairing("654321")
    clipd.clock.now += clipd.PAIR_MS + 1
    assert server._pair_code() is None


def test_successful_pairing_notifies_and_closes_window(clipd):
    conn = FakeConn([req("POST", "/pair", b"123456", token=None)])
    server, env, notes = make_server(clipd, conn)
    server.start_pairing("123456")
    server.poll()
    server.poll()
    assert TOKEN in conn.sent.decode() and server.pair_code is None
    assert notes == ["clip: phone paired"]


class FakeUdp:
    def __init__(self):
        self.pending = [(b"VTOS?", ("192.168.1.9", 4000)), (b"junk", ("192.168.1.8", 1))]
        self.sent = []

    def recvfrom(self, n):
        return self.pending.pop(0)

    def sendto(self, data, addr):
        self.sent.append((json.loads(data), addr))

    def close(self):
        pass


def test_udp_discovery_answers_only_vtos_probe(clipd):
    server, env, notes = make_server(clipd, None)
    server.udp = FakeUdp()
    real = clipd.select.select
    clipd.select = SimpleNamespace(select=lambda r, w, x, t: ([server.udp] if server.udp.pending else [], [], []))
    server.poll()
    server.poll()
    assert server.udp.sent == [({"name": "vtos", "port": 8787, "pairing": False}, ("192.168.1.9", 4000))]
