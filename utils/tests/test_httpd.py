"""Test the HTTP file server: routing, path safety, uploads, downloads."""

import importlib.util
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

TOKEN = "0123456789abcdef0123456789abcdef"
AUTH = True


@pytest.fixture
def httpd(tmp_path):
    base = Path(__file__).resolve().parents[2] / "modules/scripts"
    sys.path.insert(0, str(base / "lib"))
    path = base / "sys/httpd.py"
    spec = importlib.util.spec_from_file_location("vtos_httpd_test", path)
    mod = importlib.util.module_from_spec(spec)
    fake_net = SimpleNamespace(STA_IF=0, WLAN=lambda _: SimpleNamespace(
        active=lambda: True, isconnected=lambda: True,
        ifconfig=lambda: ("127.0.0.1",)))
    with patch.dict(sys.modules, {"network": fake_net}):
        spec.loader.exec_module(mod)
    import time
    mod.time = SimpleNamespace(
        ticks_ms=lambda: int(time.monotonic() * 1000),
        ticks_add=lambda a, b: a + b, ticks_diff=lambda a, b: a - b)
    (tmp_path / "flash").mkdir()
    (tmp_path / "sd").mkdir()
    mod.ROOTS = {"flash": str(tmp_path / "flash"), "sd": str(tmp_path / "sd")}
    mod.CONFIG = str(tmp_path / "http.json")
    yield mod
    sys.path.remove(str(base / "lib"))


def req(method, path, headers=None, query=""):
    h = {"authorization": "Bearer " + TOKEN}
    h.update(headers or {})
    return httpd_route(method, path, query, h)


def httpd_route(*a):
    return _mod.route(*a, TOKEN)


@pytest.fixture(autouse=True)
def _bind(httpd):
    global _mod
    _mod = httpd


def test_auth_required_and_query_token(httpd):
    assert httpd.route("GET", "/", "", {}, TOKEN)[:2] == ("err", 401)
    assert httpd.route("GET", "/", "token=" + "x" * 32, {}, TOKEN)[:2] == ("err", 401)
    assert httpd.route("GET", "/", "token=" + TOKEN, {}, TOKEN) == ("page",)


@pytest.mark.parametrize("path", ["/files/flash/../etc/passwd", "/files/etc/x",
                                  "/files/flash/%2e%2e/x", "/files/flash", "/files/"])
def test_paths_outside_roots_forbidden(httpd, path):
    assert req("GET", path)[0] == "err"


def test_route_methods(httpd):
    assert req("GET", "/files/flash/a")[0] == "get"
    assert req("DELETE", "/files/sd/a")[0] == "delete"
    assert req("PUT", "/files/sd/a", {"content-length": "5"})[0] == "put"
    assert req("PUT", "/files/sd/a")[:2] == ("err", 411)
    assert req("PATCH", "/files/sd/a")[:2] == ("err", 405)
    assert req("GET", "/nope")[:2] == ("err", 404)


def test_parse_head(httpd):
    assert httpd.parse_head(bytearray(b"GET / HTTP/1.1\r\nHost: x")) is None
    m, p, q, h, start = httpd.parse_head(bytearray(b"GET /a?b=1 HTTP/1.1\r\nX-Y: z\r\n\r\nbody"))
    assert (m, p, q, h["x-y"]) == ("GET", "/a", "b=1", "z")
    assert httpd.parse_head(bytearray(b"A" * 5000)) == 431


def serve(httpd, tmp_path, raw_chunks):
    """Drive a server over a real socketpair; returns the full response bytes."""
    a, b = socket.socketpair()
    b.setblocking(False)
    srv = httpd.HttpServer(SimpleNamespace(), {"port": 0, "token": TOKEN, "auth": AUTH})
    srv.conn = httpd._Conn(b, httpd.time.ticks_ms())
    srv.sock = object()   # poll() only touches it when no conn is active
    out = b""
    a.settimeout(0.05)
    chunks = list(raw_chunks)
    for _ in range(400):
        if chunks:
            a.sendall(chunks.pop(0))
        with patch.object(httpd.select, "select", lambda r, w, x, t: (
                [s for s in r if s is srv.conn.sock] if srv.conn else [], [], [])):
            if srv.conn is None:
                break
            srv.poll()
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                out += d
        except (socket.timeout, BlockingIOError):
            pass
        if srv.conn is None:
            break
    a.close()
    return out


def test_upload_then_download(httpd, tmp_path):
    body = bytes(range(256)) * 20
    head = ("PUT /files/sd/dir/x.bin HTTP/1.1\r\nAuthorization: Bearer %s\r\n"
            "Content-Length: %d\r\n\r\n" % (TOKEN, len(body))).encode()
    out = serve(httpd, tmp_path, [head + body[:100], body[100:3000], body[3000:]])
    assert out.startswith(b"HTTP/1.1 201")
    assert (tmp_path / "sd/dir/x.bin").read_bytes() == body
    assert not list((tmp_path / "sd/dir").glob("*.part"))

    get = ("GET /files/sd/dir/x.bin HTTP/1.1\r\nAuthorization: Bearer %s\r\n\r\n" % TOKEN).encode()
    out = serve(httpd, tmp_path, [get])
    h, _, data = out.partition(b"\r\n\r\n")
    assert h.startswith(b"HTTP/1.1 200") and b"Content-Length: %d" % len(body) in h
    assert data == body


def test_list_delete_and_unauthorized(httpd, tmp_path):
    (tmp_path / "flash/a.txt").write_text("hi")
    (tmp_path / "flash/sub").mkdir()
    lst = ("GET /api/list?path=/flash HTTP/1.1\r\nAuthorization: Bearer %s\r\n\r\n" % TOKEN).encode()
    out = serve(httpd, tmp_path, [lst])
    assert b'"n": "a.txt"' in out and b'"d": true' in out
    dele = ("DELETE /files/flash/a.txt HTTP/1.1\r\nAuthorization: Bearer %s\r\n\r\n" % TOKEN).encode()
    assert serve(httpd, tmp_path, [dele]).startswith(b"HTTP/1.1 200")
    assert not (tmp_path / "flash/a.txt").exists()
    assert serve(httpd, tmp_path, [b"GET /api/list HTTP/1.1\r\n\r\n"]).startswith(b"HTTP/1.1 401")


def test_aborted_upload_removes_part(httpd, tmp_path):
    head = ("PUT /files/flash/z HTTP/1.1\r\nAuthorization: Bearer %s\r\n"
            "Content-Length: 100\r\n\r\nabc" % TOKEN).encode()
    a, b = socket.socketpair()
    b.setblocking(False)
    srv = httpd.HttpServer(SimpleNamespace(), {"port": 0, "token": TOKEN, "auth": AUTH})
    srv.conn = httpd._Conn(b, httpd.time.ticks_ms())
    srv.sock = object()
    a.sendall(head)
    with patch.object(httpd.select, "select", lambda r, w, x, t: (list(r), [], [])):
        srv.poll()
        assert (tmp_path / "flash/z.part").exists()
        a.close()
        srv.poll()
    assert srv.conn is None
    assert not (tmp_path / "flash/z.part").exists()


def test_auth_off_allows_requests_without_token(httpd):
    assert httpd.route("GET", "/", "", {}, None) == ("page",)
    assert httpd.route("GET", "/files/sd/a", "", {}, None)[0] == "get"
    # path safety still applies without a token
    assert httpd.route("GET", "/files/flash/../x", "", {}, None)[0] == "err"


def test_config_defaults_auth_off(httpd):
    assert httpd.load_config()["auth"] is False


def test_mkdir_rename_and_delete_folder(httpd, tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "AUTH", False)
    def call(method, target):
        raw = ("%s %s HTTP/1.1\r\n\r\n" % (method, target)).encode()
        return serve(httpd, tmp_path, [raw])

    assert call("POST", "/api/mkdir?path=/sd/new").startswith(b"HTTP/1.1 201")
    assert (tmp_path / "sd/new").is_dir()
    (tmp_path / "sd/a.txt").write_text("x")
    assert call("POST", "/api/rename?path=/sd/a.txt&to=/sd/new/b.txt").startswith(b"HTTP/1.1 200")
    assert (tmp_path / "sd/new/b.txt").exists()
    assert call("POST", "/api/rename?path=/sd/new/b.txt&to=/sd/new/b.txt").startswith(b"HTTP/1.1 400")
    assert call("DELETE", "/files/sd/new").startswith(b"HTTP/1.1 400")   # not empty
    (tmp_path / "sd/new/b.txt").unlink()
    assert call("DELETE", "/files/sd/new").startswith(b"HTTP/1.1 200")
    assert not (tmp_path / "sd/new").exists()


@pytest.mark.parametrize("target", ["/api/mkdir?path=/etc/x", "/api/mkdir?path=/sd",
                                    "/api/rename?path=/sd/a&to=/etc/a", "/api/mkdir?path=/sd/../x"])
def test_mkdir_rename_refuse_outside_roots(httpd, target):
    assert httpd.route("POST", target.split("?")[0], target.split("?")[1], {}, None)[0] == "err"
