"""Protocol parser, audio pipe and the ytstream server, tested without a device."""

import importlib.util
import json
import shutil
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
def test_server_streams_paced_jpeg_and_mp3(yp):
    """ytstream --test: real JPEG frames and MP3 audio, demuxable end to end."""
    script = ROOT / "tools/ytstream/ytstream.py"
    proc = subprocess.Popen(
        [sys.executable, str(script), "--test", "--seconds", "3", "--fps", "8", "x"],
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
    assert json.loads(events[0][1])["fps"] == 8
    assert 18 <= len(frames) <= 26, len(frames)
    assert all(f[:2] == b"\xff\xd8" and f[-2:] == b"\xff\xd9" for f in frames)
    assert all(len(f) < 20000 for f in frames)
    assert audio[:1] in (b"\xff", b"I")  # MP3 frame sync or ID3
    assert kinds[-1] == b"E"
    assert 2.0 <= elapsed <= 6.0, "stream must arrive at playback speed"
