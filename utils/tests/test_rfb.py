"""The RFB (VNC) protocol layer, lib/rfb.py, against a scripted fake server."""

import importlib.util
import os
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rfb():
    return _load("vtos_rfb_test", ROOT / "modules/scripts/lib/rfb.py")


@pytest.fixture(scope="module")
def fake():
    return _load("vtos_fake_rfb_test", HERE / "fake_rfb.py")


@pytest.fixture
def connect(rfb, fake):
    """connect(**server_kwargs) -> (server, client); servers are stopped after."""
    servers = []

    def make(**kwargs):
        server = fake.FakeServer(**kwargs)
        servers.append(server)
        return server, rfb.Client(fake.SockIO(server.start()))

    yield make
    for server in servers:
        server.stop()


# --- DES / VNC authentication ----------------------------------------------

def test_des_matches_the_standard_test_vector(rfb):
    out = rfb.des_encrypt_block(bytes.fromhex("133457799BBCDFF1"),
                                bytes.fromhex("0123456789ABCDEF"))
    assert out == bytes.fromhex("85E813540F0AB405")


def test_des_matches_an_independent_implementation(rfb):
    from Crypto.Cipher import DES
    for _ in range(100):
        key, block = os.urandom(8), os.urandom(8)
        assert rfb.des_encrypt_block(key, block) == DES.new(key, DES.MODE_ECB).encrypt(block)


@pytest.mark.parametrize("password", ["a", "secret", "12345678", "longerthan8chars", ""])
def test_vnc_auth_response_matches_the_mirrored_key_scheme(rfb, fake, password):
    challenge = os.urandom(16)
    assert rfb.vnc_auth_response(password, challenge) == fake.vnc_answer(password, challenge)


def test_only_the_first_eight_password_bytes_count(rfb):
    challenge = bytes(range(16))
    assert rfb.vnc_auth_response("12345678", challenge) == rfb.vnc_auth_response("12345678XYZ", challenge)


# --- handshake -------------------------------------------------------------

def test_no_auth_3_8(connect):
    server, client = connect(security=(1,), width=320, height=200, name="desk")
    client.handshake()
    assert (client.width, client.height, client.name) == (320, 200, "desk")
    assert server.chosen_version == b"RFB 003.008\n"
    assert server.chosen_security == 1


@pytest.mark.parametrize("version,reply", [
    (b"RFB 003.007\n", b"RFB 003.007\n"),
    (b"RFB 003.003\n", b"RFB 003.003\n"),
    (b"RFB 003.005\n", b"RFB 003.003\n"),     # in-between versions act as 3.3
    (b"RFB 003.889\n", b"RFB 003.003\n"),     # Apple Remote Desktop acts as 3.3
    (b"RFB 004.001\n", b"RFB 003.008\n"),     # newer majors get the best we speak
])
def test_version_negotiation(connect, version, reply):
    server, client = connect(version=version, security=(1,))
    client.handshake()
    assert server.chosen_version == reply
    assert client.width == 64


@pytest.mark.parametrize("version", [b"RFB 003.008\n", b"RFB 003.007\n", b"RFB 003.003\n"])
def test_vnc_password_auth_every_version(connect, version):
    server, client = connect(version=version, security=(2,), password="hunter2")
    client.handshake(password="hunter2")
    assert server.auth_ok is True
    assert client.width == 64


def test_wrong_password_is_reported(rfb, connect):
    server, client = connect(security=(2,), password="right")
    with pytest.raises(rfb.RFBError, match="authentication failed: Authentication failure"):
        client.handshake(password="wrong")


def test_wrong_password_3_3_has_no_reason_text(rfb, connect):
    _, client = connect(version=b"RFB 003.003\n", security=(2,), password="right")
    with pytest.raises(rfb.RFBError, match="wrong password"):
        client.handshake(password="wrong")


def test_password_is_asked_for_only_when_needed(connect):
    asked = []
    _, client = connect(security=(2,), password="pw")
    client.handshake(ask_password=lambda: asked.append(1) or "pw")
    assert asked == [1]

    asked.clear()
    _, client = connect(security=(1,))
    client.handshake(ask_password=lambda: asked.append(1) or "pw")
    assert asked == []


def test_giving_up_at_the_password_prompt(rfb, connect):
    _, client = connect(security=(2,), password="pw")
    with pytest.raises(rfb.RFBError, match="password required"):
        client.handshake(ask_password=lambda: None)


def test_none_is_preferred_when_offered_alongside_vnc_auth(connect):
    server, client = connect(security=(2, 1), password="x")
    client.handshake()
    assert server.chosen_security == 1


@pytest.mark.parametrize("version", [b"RFB 003.008\n", b"RFB 003.003\n"])
def test_server_refusal_reason_is_surfaced(rfb, connect, version):
    _, client = connect(version=version, refuse="Too many connections")
    with pytest.raises(rfb.RFBError, match="refused: Too many connections"):
        client.handshake()


def test_unsupported_security_names_what_was_offered(rfb, connect):
    _, client = connect(security=(30, 18))     # ARD and VeNCrypt-style types
    with pytest.raises(rfb.RFBError, match=r"unsupported security types \[30, 18\]"):
        client.handshake()


def test_not_a_vnc_server(rfb, connect):
    _, client = connect(version=b"SSH-2.0-Open")
    with pytest.raises(rfb.RFBError, match="not a VNC server"):
        client.handshake()


def test_connection_closed_mid_handshake(rfb, fake):
    import socket
    a, b = socket.socketpair()
    b.sendall(b"RFB 003")
    b.close()
    client = rfb.Client(fake.SockIO(a))
    with pytest.raises(rfb.RFBError, match="connection closed"):
        client.handshake()
    a.close()


def test_server_name_longer_than_the_cap_is_trimmed_not_desynced(connect):
    server, client = connect(name="n" * 1000)
    client.handshake()
    assert client.name == "n" * 256
    # the stream is still in sync: the next message parses
    server.queue(bytes([2]))
    seen = []

    class Sink:
        def bell(self):
            seen.append("bell")
    client.read_message(Sink())
    assert seen == ["bell"]


# --- client messages --------------------------------------------------------

def test_client_messages_reach_the_server_as_specified(rfb, connect):
    server, client = connect()
    client.handshake()
    client.set_pixel_format()
    client.set_encodings([rfb.ENC_RAW, rfb.ENC_DESKTOP_SIZE])
    client.request_update(10, 20, 30, 40, incremental=True)
    client.request_update(0, 0, 64, 48, incremental=False)
    client.key_press(0xFF0D)
    client.pointer_event(0, 5, 6)
    client.pointer_click(rfb.BTN_RIGHT, 7, 8)
    assert server.wait_for(lambda ev: len(ev) >= 9)
    ev = server.events
    # 16 bpp, depth 16, big-endian, true colour, max 31/63/31, shifts 11/5/0
    assert ev[0] == ("pixfmt", struct.pack(">BBBBHHHBBB", 16, 16, 1, 1, 31, 63, 31, 11, 5, 0) + b"\0\0\0")
    assert ev[1] == ("encodings", [0, -223])
    assert ev[2] == ("fbur", 1, 10, 20, 30, 40)
    assert ev[3] == ("fbur", 0, 0, 0, 64, 48)
    assert ev[4:6] == [("key", 1, 0xFF0D), ("key", 0, 0xFF0D)]
    assert ev[6] == ("pointer", 0, 5, 6)
    assert ev[7:9] == [("pointer", 4, 7, 8), ("pointer", 0, 7, 8)]


# --- server messages --------------------------------------------------------

class Recorder:
    def __init__(self, rfb):
        self.rfb = rfb
        self.rects = []
        self.events = []

    def raw_rect(self, client, x, y, w, h):
        data = bytearray(w * h * 2)
        client.read_into(data)
        self.rects.append((x, y, w, h, bytes(data)))

    def resize(self, w, h):
        self.events.append(("resize", w, h))

    def update_done(self):
        self.events.append("done")

    def bell(self):
        self.events.append("bell")

    def cut_text(self, text):
        self.events.append(("cut", text))


def test_raw_update_with_two_rectangles(rfb, fake, connect):
    server, client = connect()
    client.handshake()
    server.queue(fake.update(fake.raw_rect(0, 0, 4, 3), fake.raw_rect(10, 20, 2, 2)))
    rec = Recorder(rfb)
    assert client.read_message(rec) == 0
    assert rec.rects[0] == (0, 0, 4, 3, fake.raw_pixels(0, 0, 4, 3))
    assert rec.rects[1] == (10, 20, 2, 2, fake.raw_pixels(10, 20, 2, 2))
    assert rec.events == ["done"]


def test_default_sink_discards_pixels_and_stays_in_sync(rfb, fake, connect):
    server, client = connect()
    client.handshake()
    server.queue(fake.update(fake.raw_rect(0, 0, 40, 30)) + bytes([2]))
    sink = rfb.Sink()
    assert client.read_message(sink) == 0
    assert client.read_message(sink) == 2


def test_desktop_size_and_last_rect(rfb, fake, connect):
    server, client = connect()
    client.handshake()
    server.queue(fake.update((0, 0, 800, 600, rfb.ENC_DESKTOP_SIZE, b""),
                             (0, 0, 0, 0, rfb.ENC_LAST_RECT, b"")))
    rec = Recorder(rfb)
    client.read_message(rec)
    assert rec.events == [("resize", 800, 600), "done"]
    assert (client.width, client.height) == (800, 600)


def test_bell_cut_text_and_colour_map(rfb, connect):
    server, client = connect()
    client.handshake()
    text = b"h\xe9llo"
    server.queue(bytes([2]) +
                 bytes([3, 0, 0, 0]) + struct.pack(">I", len(text)) + text +
                 bytes([1, 0]) + struct.pack(">HH", 0, 2) + b"\0" * 12 +
                 bytes([2]))
    rec = Recorder(rfb)
    assert [client.read_message(rec) for _ in range(4)] == [2, 3, 1, 2]
    assert rec.events == ["bell", ("cut", "h\xe9llo"), "bell"]


def test_oversized_cut_text_is_capped_and_skipped(rfb, connect):
    server, client = connect()
    client.handshake()
    big = b"x" * 10000
    server.queue(bytes([3, 0, 0, 0]) + struct.pack(">I", len(big)) + big + bytes([2]))
    rec = Recorder(rfb)
    client.read_message(rec)
    assert rec.events == [("cut", "x" * 4096)]
    assert client.read_message(rec) == 2          # still in sync


def test_unsupported_encoding_is_an_error_not_garbage(rfb, fake, connect):
    server, client = connect()
    client.handshake()
    server.queue(fake.update((0, 0, 2, 2, 6, b"\0" * 8)))      # zlib, never requested
    with pytest.raises(rfb.RFBError, match="unsupported encoding 6"):
        client.read_message(rfb.Sink())


def test_unknown_message_type(rfb, connect):
    server, client = connect()
    client.handshake()
    server.queue(bytes([99]))
    with pytest.raises(rfb.RFBError, match="unknown server message 99"):
        client.read_message(rfb.Sink())


def test_handler_that_misreads_a_rectangle_is_caught(rfb, fake, connect):
    server, client = connect()
    client.handshake()
    server.queue(fake.update(fake.raw_rect(0, 0, 4, 4)))

    class Lazy(rfb.Sink):
        def raw_rect(self, client, x, y, w, h):
            client.skip(10)                    # reads too little
    with pytest.raises(rfb.RFBError, match="lost sync"):
        client.read_message(Lazy())


def test_connection_drop_inside_a_rectangle(rfb, fake, connect):
    server, client = connect()
    client.handshake()
    import socket
    full = fake.update(fake.raw_rect(0, 0, 8, 8))
    server.sock.sendall(full[:-30])                 # handshake is done: the loop only sends queued data
    server.sock.shutdown(socket.SHUT_WR)
    rec = Recorder(rfb)
    with pytest.raises(rfb.RFBError, match="connection closed"):
        client.read_message(rec)
    assert rec.rects == []                           # the torn rectangle was never delivered


def test_shifted_key_press_wraps_the_key_in_shift(rfb, connect):
    server, client = connect()
    client.handshake()
    client.key_press(ord("I"), shift=True)
    client.key_press(ord("i"))
    assert server.wait_for(lambda ev: len(ev) >= 6)
    assert [e[1:] for e in server.events] == [
        (1, rfb.KEY_SHIFT_L), (1, 0x49), (0, 0x49), (0, rfb.KEY_SHIFT_L),
        (1, 0x69), (0, 0x69)]
