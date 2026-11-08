"""Protocol parser, audio pipe and the ytstream server, tested without a device."""

import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def yp():
    path = ROOT / "modules/scripts/lib/ytproto.py"
    spec = importlib.util.spec_from_file_location("vtos_ytproto_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def packet(kind, payload):
    return kind + len(payload).to_bytes(4, "little") + payload


def test_demux_skips_shell_noise_and_parses_packets(yp):
    stream = b"user@host:~$ stty raw -echo\r\n" + yp.MARKER
    stream += packet(b"M", b'{"title":"x"}') + packet(b"V", b"\xff\xd8abc\xff\xd9")
    stream += packet(b"A", b"mp3data") + packet(b"E", b"end")
    events = yp.Demux().feed(stream)
    assert [k for k, _ in events] == [b"M", b"V", b"A", b"E"]
    assert events[2][1] == b"mp3data"


def test_demux_handles_any_split(yp):
    stream = yp.MARKER + packet(b"V", bytes(range(200))) + packet(b"A", b"z" * 50)
    for step in (1, 3, 7, 64):
        demux = yp.Demux()
        events = []
        for i in range(0, len(stream), step):
            events += demux.feed(stream[i:i + step])
        assert [k for k, _ in events] == [b"V", b"A"], step
        assert events[0][1] == bytes(range(200))


def test_demux_partial_marker_across_feeds(yp):
    demux = yp.Demux()
    assert demux.feed(b"noise" + yp.MARKER[:4]) == []
    assert demux.feed(yp.MARKER[4:] + packet(b"E", b"hi")) == [(b"E", b"hi")]


def test_demux_rejects_absurd_length(yp):
    with pytest.raises(ValueError):
        yp.Demux().feed(yp.MARKER + b"V" + (10**6).to_bytes(4, "little"))


def test_audio_pipe_is_an_io_stream_object(yp):
    import io
    pipe = yp.AudioPipe()
    assert isinstance(pipe, io.IOBase)
    pipe.push(b"abc")
    pipe.close()  # the player calls close() on stop()
    assert pipe.size == 0 and pipe.readinto(bytearray(2)) is None


def test_audio_pipe_returns_none_when_empty_and_preserves_order(yp):
    pipe = yp.AudioPipe()
    buf = bytearray(8)
    assert pipe.readinto(buf) is None
    pipe.push(b"abcde")
    pipe.push(b"fghijkl")
    assert pipe.size == 12
    assert pipe.readinto(buf) == 8 and bytes(buf) == b"abcdefgh"
    assert pipe.readinto(buf) == 4 and bytes(buf[:4]) == b"ijkl"
    assert pipe.size == 0 and pipe.readinto(buf) is None


def test_no_cpython_only_string_methods_in_device_code():
    for rel in ("modules/scripts/lib/ytproto.py", "modules/scripts/bin/yt.py"):
        text = (ROOT / rel).read_text()
        for bad in (".ljust(", ".rjust(", ".center(", ".zfill("):
            assert bad not in text, (rel, bad)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
@pytest.mark.parametrize("width,height,fps,fit", [
    (320, 240, 8, "contain"), (256, 144, 30, "contain"),
    (176, 96, 60, "contain"), (320, 240, 8, "cover")])
def test_server_streams_paced_jpeg_and_mp3(yp, width, height, fps, fit):
    """ytstream --test: real JPEG frames and MP3 audio, demuxable end to end."""
    script = ROOT / "tools/ytstream/ytstream.py"
    proc = subprocess.Popen(
        [sys.executable, str(script), "--test", "--seconds", "3", "--fps", str(fps),
         "--w", str(width), "--h", str(height), "--fit", fit, "x"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    started = time.time()
    data = proc.stdout.read()
    elapsed = time.time() - started
    proc.stdin.close()
    proc.wait(timeout=10)
    events = yp.Demux().feed(data)
    kinds = [k for k, _ in events]
    frames = [p for k, p in events if k == b"V"]
    audio = b"".join(p for k, p in events if k == b"A")
    meta = json.loads(events[0][1])
    assert (meta["w"], meta["h"], meta["fps"]) == (width, height, fps)
    assert 3 * fps - 6 <= len(frames) <= 3 * fps + 2, len(frames)
    assert all(f[:2] == b"\xff\xd8" and f[-2:] == b"\xff\xd9" for f in frames)
    assert all(len(f) < 20000 for f in frames)
    if width == 320 and height == 240:
        rgb = subprocess.check_output(["ffmpeg", "-loglevel", "error", "-i", "pipe:0",
                                       "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                                      input=frames[0])
        assert len(rgb) == width * height * 3
        # 16:9 source: contain has black bars; cover paints the top of the screen.
        assert (max(rgb[:width * 10 * 3]) > 30) == (fit == "cover")
    assert audio[:1] in (b"\xff", b"I")  # MP3 frame sync or ID3
    assert kinds[-1] == b"E"
    assert 2.0 <= elapsed <= 6.0, "stream must arrive at playback speed"


def test_yt_option_parsing_and_presets(yp, monkeypatch):
    import importlib.util, sys, types
    monkeypatch.setitem(sys.modules, "ytproto", yp)
    for name in ("modssh",):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    spec = importlib.util.spec_from_file_location("vtos_yt_test", ROOT / "modules/scripts/bin/yt.py")
    yt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(yt)
    assert yt.parse_options(["big", "buck"]) == ("smooth", ["big", "buck"])
    assert yt.parse_options(["--fast", "big"]) == ("fast", ["big"])
    assert yt.parse_options(["--tiny"]) == ("tiny", [])
    # a later word that looks like a preset is part of the search, not an option
    assert yt.parse_options(["cats", "--fast"]) == ("smooth", ["cats", "--fast"])
    assert yt.PRESETS["smooth"] == (256, 144, 30)
    assert yt.PRESETS["fast"] == (176, 96, 60)
    assert yt.PRESETS["full"] == (320, 240, 30)
    for w, h, fps in yt.PRESETS.values():
        assert w * 16 * 2 <= 10240 and 30 <= fps <= 60


@pytest.mark.parametrize("strip,expected_fps", [(True, 30), (False, 10)])
def test_player_drains_burst_and_draws_only_newest_frame(yp, monkeypatch, capsys, strip, expected_fps):
    import itertools
    import types

    monkeypatch.setitem(sys.modules, "ytproto", yp)
    display_module = types.SimpleNamespace(SLOW=1)
    if strip:
        display_module.STRIP = 2
    monkeypatch.setitem(sys.modules, "st7789", display_module)
    spec = importlib.util.spec_from_file_location("vtos_yt_player_test", ROOT / "modules/scripts/bin/yt.py")
    yt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(yt)
    ticks = itertools.count()
    monkeypatch.setattr(time, "ticks_ms", lambda: next(ticks), raising=False)
    monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)

    class Client:
        def __init__(self):
            self.chunks = [yp.MARKER + packet(b"M", b'{"w":256,"h":144}')
                           + packet(b"V", b"old"),
                           packet(b"A", b"audio") + packet(b"V", b"new"),
                           packet(b"E", b"end")]
            self.sent = []

        def readinto(self, buf):
            data = self.chunks.pop(0)
            buf[:len(data)] = data
            return len(data)

        def write(self, data):
            self.sent.append(data)

    client = Client()
    monkeypatch.setattr(yt.select, "select", lambda r, w, e, timeout:
                        ([client] if r == [client] and client.chunks else [], [], []))
    draws = []
    fills = []
    env = types.SimpleNamespace(tft=types.SimpleNamespace(
        jpg=lambda *args: draws.append(args), fill=lambda color: fills.append(color)),
        audio=None, sd_busy=False)
    assert yt.play(env, client, "test") == "end"
    assert draws == [(b"new", 32, 48, 2 if strip else 1)]
    assert fills == [0]
    assert b"--fps %d" % expected_fps in client.sent[0]
    assert client.sent[-1] == b"q"
    assert env.sd_busy is False
    assert "1 frames drawn, 1 skipped" in capsys.readouterr().out


def test_lan_server_leaves_control_bytes_after_command():
    spec = importlib.util.spec_from_file_location("vtos_lan_server", ROOT / "tools/ytstream/lan_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    left, right = socket.socketpair()
    with left, right:
        right.sendall(b"stty raw -echo; exec ignored command\nq")
        server.read_request(left)
        assert left.recv(1) == b"q"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_lan_server_streams_to_tcp_client(yp):
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "tools/ytstream/lan_server.py"),
         "--bind", "127.0.0.1", "--port", "0", "--test", "--seconds", "1",
         "--w", "320", "--h", "240", "--fps", "5", "--fit", "cover", "x"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ready = proc.stdout.readline().decode().strip()
        assert ready.startswith("Listening on 127.0.0.1:"), ready
        port = int(ready.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            client.sendall(b"ignored shell command\n")
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        _, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr.decode()
        events = yp.Demux().feed(b"".join(chunks))
        meta = json.loads(events[0][1])
        assert (meta["w"], meta["h"], meta["fps"]) == (320, 240, 5)
        assert len([p for k, p in events if k == b"V"]) == 5
        assert any(k == b"A" for k, p in events)
        assert events[-1] == (b"E", b"end")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
