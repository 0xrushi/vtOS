"""Test the VNC server's full-screen mode: dirty rows, pixel formats, input."""

import importlib.util
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

W, H = 8, 6


class FakeTft:
    def __init__(self):
        self.rows = [bytes([y]) * (W * 2) for y in range(H)]
        self.dirty = [0] * H
        self.enabled = False

    def shadow(self, on):
        self.enabled = on
        return on

    def shadow_invalidate(self):
        self.dirty = [1] * H

    def shadow_dirty(self, buf):
        for i, d in enumerate(self.dirty):
            buf[i] = d
        return sum(self.dirty)

    def shadow_row(self, y, buf):
        buf[:W * 2] = self.rows[y]
        self.dirty[y] = 0
        return W * 2


@pytest.fixture
def vncd():
    path = Path(__file__).resolve().parents[2] / "modules/scripts/bin/vncd.py"
    spec = importlib.util.spec_from_file_location("vtos_vncd_test", path)
    mod = importlib.util.module_from_spec(spec)
    stubs = {"network": SimpleNamespace(), "machine": SimpleNamespace(),
             "micropython": SimpleNamespace()}
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(mod)
    return mod


def make_env():
    typed = []
    touch = SimpleNamespace(remote=None)
    return SimpleNamespace(
        tft=FakeTft(), screen_width=W, screen_height=H, rows=3, fb_shadow=True,
        kvm=SimpleNamespace(inject=typed.append), typed=typed,
        quickpanel=SimpleNamespace(touch=touch), sts=SimpleNamespace(notify=lambda m: None))


def make_conn(vncd, env):
    conn = vncd._Conn(SimpleNamespace(close=lambda: None), "1.2.3.4", env)
    conn.outq.clear()
    return conn


def test_dirty_runs_coalesce(vncd):
    assert vncd._dirty_runs([0, 1, 1, 0, 1, 0], 6) == [(1, 2), (4, 1)]
    assert vncd._dirty_runs([1] * 30, 30) == [(0, 30)]
    assert vncd._dirty_runs([0] * 6, 6) == []


def test_native_update_sends_raw_rows_and_clears_dirty(vncd):
    env = make_env()
    conn = make_conn(vncd, env)
    env.tft.dirty = [0, 1, 1, 0, 0, 1]
    assert vncd._queue_fb_update(conn) is True
    assert len(conn.outq) == 1
    blob = conn.outq[0]
    rowb = W * 2
    assert blob[:4] == struct.pack(">BBH", 0, 0, 2)
    assert blob[4:16] == struct.pack(">HHHHI", 0, 1, W, 2, 0)
    assert blob[16:16 + 2 * rowb] == env.tft.rows[1] + env.tft.rows[2]
    assert blob[16 + 2 * rowb:28 + 2 * rowb] == struct.pack(">HHHHI", 0, 5, W, 1, 0)
    assert env.tft.dirty == [0] * H


def test_full_request_streams_all_rows_despite_redraw_and_partial_send(vncd, monkeypatch):
    env = make_env()
    conn = make_conn(vncd, env)
    conn.sock = TrickleSock(room=40)
    server = vncd._Server.__new__(vncd._Server)
    server.conn = conn
    server.listen_sock = object()
    server._rsocks = []
    monkeypatch.setattr(vncd.select, "select", lambda *args: ([], [], []))
    monkeypatch.setattr(vncd, "_FB_BAND_ROWS", 2)
    monkeypatch.setattr(vncd, "_FB_BANDS_PER_TICK", 1)

    env.tft.dirty = [0] * H
    conn.state = vncd._ST_FBUR
    vncd._advance(conn, struct.pack(">BHHHH", 0, 0, 0, W, H))
    server._do_tick()
    assert len(conn.sock.got) == 40  # new data was sent in the same tick
    assert conn.outoff == 40
    for _ in range(H):
        env.tft.dirty[0] = 1  # a redraw must not starve the lower rows
        server._do_tick()

    assert conn.sock.got == struct.pack(">BBHHHHHI", 0, 0, 1, 0, 0, W, H, 0) + b"".join(env.tft.rows)
    assert env.tft.dirty == [1] + [0] * (H - 1)
    assert conn.fb_update is None and not conn.awaiting_update


def test_new_connection_requests_full_frame(vncd):
    env = make_env()
    env.tft.dirty = [0] * H
    conn = make_conn(vncd, env)
    assert env.tft.dirty == [1] * H and conn.fb


def test_converts_for_32bpp_client(vncd):
    env = make_env()
    env.tft.rows[0] = bytes([0xF8, 0x00]) * W    # pure red in RGB565
    env.tft.dirty = [1, 0, 0, 0, 0, 0]
    conn = make_conn(vncd, env)
    env.tft.dirty = [1, 0, 0, 0, 0, 0]
    conn.current_format = vncd._DEFAULT_FORMAT
    vncd._queue_fb_update(conn)
    pixels = conn.outq[0][4 + 12:]
    assert pixels[:4] == bytes([0x00, 0x00, 0xFF, 0x00])   # little-endian BGRX
    assert len(pixels) == W * 4


@pytest.mark.parametrize("big_endian", [0, 1])
def test_uses_driver_rgb888_conversion_when_available(vncd, big_endian):
    env = make_env()
    calls = []

    def rgb888(y, buf, endian):
        calls.append((y, endian))
        buf[:W * 4] = bytes([y]) * (W * 4)
        env.tft.dirty[y] = 0
        return W * 4

    env.tft.shadow_row_rgb888 = rgb888
    conn = make_conn(vncd, env)
    conn.current_format = (32, big_endian) + vncd._DEFAULT_FORMAT[2:]
    env.tft.dirty = [0, 0, 1, 0, 0, 0]
    assert vncd._queue_fb_update(conn)
    assert calls == [(2, big_endian)]
    assert conn.outq[0][16:] == bytes([2]) * (W * 4)


def test_hextile_is_negotiated_and_streamed_from_driver(vncd):
    env = make_env()
    calls = []

    def pixels(y, rows, buf, fmt, hextile):
        calls.append((y, rows, fmt, hextile))
        buf[:3] = b"\x02\xf8\x00"  # one solid red Hextile tile
        env.tft.dirty[y:y + rows] = [0] * rows
        return 3

    env.tft.shadow_pixels = pixels
    conn = make_conn(vncd, env)
    conn.state = vncd._ST_SETENC_LIST
    vncd._advance(conn, struct.pack(">iii", 16, 5, 0))
    assert conn.encoding == 5
    assert vncd._queue_fb_update(conn)
    assert conn.outq[0] == (struct.pack(">BBHHHHHI", 0, 0, 1, 0, 0, W, H, 5) +
                            b"\x02\xf8\x00")
    assert calls == [(0, H, vncd._NATIVE_FORMAT, True)]


def test_hextile_requires_driver_support_and_client_offer(vncd):
    conn = make_conn(vncd, make_env())
    conn.state = vncd._ST_SETENC_LIST
    vncd._advance(conn, struct.pack(">i", 5))
    assert conn.encoding == 0
    conn.shadow_pixels = lambda *args: 0
    conn.state = vncd._ST_SETENC_LIST
    vncd._advance(conn, struct.pack(">i", 0))
    assert conn.encoding == 0


def test_snapshot_keeps_redraws_pending_for_following_update(vncd):
    env = make_env()
    original = list(env.tft.rows)
    captured = []

    def snapshot(flags):
        count = env.tft.shadow_dirty(flags)
        captured[:] = env.tft.rows
        env.tft.dirty[:] = [0] * H
        return count

    def pixels(y, rows, buf, fmt, hextile):
        # The app redraws after the snapshot and before transmission finishes.
        env.tft.rows[-1] = b"\xff" * (W * 2)
        env.tft.dirty[-1] = 1
        data = b"".join(captured[y:y + rows])
        buf[:len(data)] = data
        return len(data)

    env.tft.shadow_snapshot = snapshot
    env.tft.shadow_pixels = pixels
    conn = make_conn(vncd, env)
    assert vncd._queue_fb_update(conn)
    assert conn.outq[0][16:] == b"".join(original)
    assert env.tft.dirty == [0] * (H - 1) + [1]


def test_pixel_format_upgrade_forces_refresh_on_next_request(vncd):
    conn = make_conn(vncd, make_env())
    conn.state = vncd._ST_SETPIXFMT
    vncd._advance(conn, b"\0" * 3 + vncd._PIXEL_FORMAT)
    assert conn.current_format == vncd._DEFAULT_FORMAT
    assert conn.full_update and not conn.awaiting_update


def test_rgb565_conversion_preserves_all_native_values(vncd):
    src = bytearray()
    for value in range(65536):
        src.extend(value.to_bytes(2, "big"))
    dst = bytearray(len(src))
    vncd._rgb565_row_to_pixfmt(src, len(src), dst, (16, 0, 31, 63, 31, 11, 5, 0))
    assert dst[::2] == src[1::2] and dst[1::2] == src[::2]


def test_server_init_advertises_native_format(vncd):
    env = make_env()
    conn = make_conn(vncd, env)
    conn.state = vncd._ST_CLIENTINIT
    vncd._advance(conn, b"\x01")
    init = conn.outq[0]
    assert init[4:20] == vncd._NATIVE_PIXEL_FORMAT


def test_pointer_becomes_touch_and_release(vncd):
    env = make_env()
    conn = make_conn(vncd, env)
    touch = env.quickpanel.touch
    vncd._apply_pointer(conn, 1, 100, 50)
    assert touch.remote == (100, 50)
    vncd._apply_pointer(conn, 1, 400, 300)       # clamped to the 320x240 panel
    assert touch.remote == (319, 239)
    vncd._apply_pointer(conn, 0, 10, 10)
    assert touch.remote is False
    vncd._apply_pointer(conn, 0, 20, 20)         # hover must not retouch
    assert touch.remote is False


def test_ctrl_and_special_keys(vncd):
    env = make_env()
    conn = make_conn(vncd, env)
    conn.state = vncd._ST_KEYEVENT
    for keysym, down in ((0xFFE3, 1), (ord("c"), 1), (ord("c"), 0), (0xFFE3, 0), (0xFFFF, 1), (ord("a"), 1)):
        conn.state = vncd._ST_KEYEVENT
        vncd._advance(conn, struct.pack(">BHI", down, 0, keysym))
    assert env.typed == ["\x03", "\x1b[3~", "a"]


def test_fb_request_stays_pending_until_screen_changes(vncd):
    env = make_env()
    conn = make_conn(vncd, env)
    conn.awaiting_update = True
    conn.state = vncd._ST_FBUR
    vncd._advance(conn, struct.pack(">BHHHH", 1, 0, 0, W, H))
    assert conn.awaiting_update
    env.tft.dirty = [0] * H
    # Simulate many idle ticks of the server loop's wait logic: still pending.
    for _ in range(30):
        assert vncd._queue_fb_update(conn) is False
    assert conn.awaiting_update
    env.tft.dirty[2] = 1
    assert vncd._queue_fb_update(conn) is True


def test_non_incremental_request_latches_full_refresh(vncd):
    env = make_env()
    conn = make_conn(vncd, env)
    env.tft.dirty = [0] * H
    conn.state = vncd._ST_FBUR
    vncd._advance(conn, struct.pack(">BHHHH", 0, 0, 0, W, H))
    assert conn.full_update and conn.awaiting_update
    assert env.tft.dirty == [0] * H  # invalidation waits for the previous update


class TrickleSock:
    """Accepts `room` bytes per call, then raises EAGAIN like a full lwIP buffer."""

    def __init__(self, room, fail_after=None):
        self.room, self.got, self.calls, self.fail_after = room, b"", 0, fail_after

    def send(self, data):
        self.calls += 1
        if self.calls > 1 and self.fail_after is not None and self.calls > self.fail_after:
            raise OSError(11, "EAGAIN")
        n = min(self.room, len(data))
        self.got += bytes(data[:n])
        return n


def test_drain_sends_whole_queue_in_one_tick(vncd):
    conn = make_conn(vncd, make_env())
    conn.outq[:] = [b"a" * 100] * 30           # the old code sent ONE item per tick
    conn.sock = TrickleSock(room=10**6)
    vncd._drain_tx(conn)
    assert conn.outq == [] and len(conn.sock.got) == 3000


def test_drain_resumes_after_partial_send_and_eagain(vncd):
    conn = make_conn(vncd, make_env())
    conn.outq[:] = [bytes(range(200)), bytes(range(100))]
    conn.sock = TrickleSock(room=150, fail_after=2)
    vncd._drain_tx(conn)                        # 150 of 200 sent, buffer full
    assert conn.outoff == 150 and not conn.pending_close
    conn.sock.calls = 0
    conn.sock.fail_after = None
    vncd._drain_tx(conn)
    assert conn.outq == [] and conn.sock.got == bytes(range(200)) + bytes(range(100))


def test_real_send_error_closes(vncd):
    conn = make_conn(vncd, make_env())
    conn.outq[:] = [b"x"]

    class Dead:
        def send(self, d):
            raise OSError(104, "ECONNRESET")
    conn.sock = Dead()
    vncd._drain_tx(conn)
    assert conn.pending_close


def test_new_connection_makes_screen_repaint(vncd):
    calls = []
    env = make_env()
    env.app_display = None
    env.status_height = 12
    env.term = SimpleNamespace(top_offset=lambda h: calls.append(("off", h)),
                               top_bar_invalidate=lambda: calls.append("top"),
                               bottom_bar_invalidate=lambda: calls.append("bot"))
    make_conn(vncd, env)
    assert calls == [("off", 12), "top", "bot"]


def test_repaint_marks_pixel_app_dirty(vncd):
    env = make_env()
    env.app_display = SimpleNamespace(dirty=False)
    vncd._repaint(env)
    assert env.app_display.dirty
