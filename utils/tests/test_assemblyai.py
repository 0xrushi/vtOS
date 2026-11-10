"""Voice typing: lib/assemblyai.py with a fake microphone and a fake HTTP API."""

import importlib.util
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def aai():
    spec = importlib.util.spec_from_file_location("vtos_aai", ROOT / "modules/scripts/lib/assemblyai.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeMic:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.log = []

    def record(self, path):
        self.log.append(("record", path))

    def readinto(self, buf):
        if not self.chunks:
            return 0
        data = self.chunks.pop(0)[:len(buf)]            # a driver never overfills
        buf[:len(data)] = data
        return len(data)

    def stop(self):
        self.log.append("stop")

    def deinit(self):
        self.log.append("deinit")


def ready(mics, rlist=True):
    return lambda r, w, x, t: ([r[0]] if rlist else [], [], [])


def test_wav_header_describes_16k_mono_16bit(aai):
    h = aai.wav_header(1000)
    assert len(h) == 44 and h[:4] == b"RIFF" and h[8:16] == b"WAVEfmt "
    assert struct.unpack("<IHHIIHH", h[16:36]) == (16, 1, 1, 16000, 32000, 2, 16)
    assert h[36:40] == b"data" and struct.unpack("<I", h[40:44])[0] == 1000
    assert struct.unpack("<I", h[4:8])[0] == 36 + 1000


def test_recorder_collects_pcm_and_returns_a_wav(aai):
    mic = FakeMic([b"\x01\x02" * 100, b"\x03\x04" * 50])
    rec = aai.Recorder(lambda: mic, ready(mic), seconds=2)
    assert mic.log == [("record", None)]
    rec.pump()
    wav = rec.stop()
    assert len(wav) == 44 + 300 and bytes(wav[44:46]) == b"\x01\x02" and bytes(wav[-2:]) == b"\x03\x04"
    assert struct.unpack("<I", bytes(wav[40:44]))[0] == 300
    assert mic.log[-2:] == ["stop", "deinit"]


def test_recorder_stops_filling_when_the_buffer_is_full(aai):
    mic = FakeMic([b"\x00" * 40000] * 4)
    rec = aai.Recorder(lambda: mic, ready(mic), seconds=1)       # 32000 bytes
    rec.pump()
    assert rec.full and rec.length == 32000


def test_recorder_reads_nothing_when_the_mic_has_nothing(aai):
    mic = FakeMic([b"\x00\x00"])
    rec = aai.Recorder(lambda: mic, ready(mic, rlist=False), seconds=1)
    rec.pump()
    assert rec.length == 0


class FakeHttp:
    def __init__(self, statuses=("processing", "completed"), text=" hello world ", upload_status=200):
        self.calls = []
        self.statuses = list(statuses)
        self.text = text
        self.upload_status = upload_status

    def _resp(self, status, payload):
        body = json.dumps(payload) if isinstance(payload, dict) else payload
        return SimpleNamespace(status_code=status, text=body, json=lambda: json.loads(body),
                               close=lambda: None)

    def post(self, url, data=None, json=None, headers=None):
        self.calls.append(("POST", url, headers, bytes(data) if data is not None else json))
        if url.endswith("/upload"):
            return self._resp(self.upload_status, {"upload_url": "https://cdn/x"})
        return self._resp(200, {"id": "abc", "status": "queued"})

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers, None))
        status = self.statuses.pop(0)
        return self._resp(200, {"status": status, "text": self.text, "error": "bad audio"})


def run(aai, http, key="k", **kw):
    clock = [0]
    return aai.transcribe(b"WAVDATA", key, http, sleep_ms=lambda ms: clock.__setitem__(0, clock[0] + ms),
                          ticks_ms=lambda: clock[0], ticks_diff=lambda a, b: a - b, **kw)


def test_transcribe_uploads_submits_and_polls(aai):
    http = FakeHttp()
    assert run(aai, http) == "hello world"
    up, sub, poll1, poll2 = http.calls
    assert up[1].endswith("/v2/upload") and up[3] == b"WAVDATA"
    assert up[2]["authorization"] == "k" and up[2]["Content-Type"] == "application/octet-stream"
    assert sub[1].endswith("/v2/transcript")
    assert sub[3] == {"audio_url": "https://cdn/x", "speech_models": ["universal-2"]}
    assert poll1[1].endswith("/v2/transcript/abc") and poll2[0] == "GET"


def test_language_is_sent_only_when_asked_for(aai):
    http = FakeHttp()
    run(aai, http, language="en_us")
    assert http.calls[1][3]["language_code"] == "en_us" and http.calls[1][3]["language_detection"] is False


def test_an_error_status_raises_with_the_reason(aai):
    with pytest.raises(aai.VoiceError, match="bad audio"):
        run(aai, FakeHttp(statuses=("error",)))


def test_a_missing_key_never_touches_the_network(aai):
    http = FakeHttp()
    with pytest.raises(aai.VoiceError, match="no key"):
        run(aai, http, key="")
    assert http.calls == []


def test_a_rejected_key_is_reported_without_the_key(aai):
    http = FakeHttp(upload_status=401)
    with pytest.raises(aai.VoiceError) as err:
        run(aai, http, key="sekrit-key-123")
    assert "sekrit" not in str(err.value)


def test_timeout_when_it_never_completes(aai):
    with pytest.raises(aai.VoiceError, match="timed out"):
        run(aai, FakeHttp(statuses=["processing"] * 100))


def test_load_key_reads_the_agent_config(aai, tmp_path):
    p = tmp_path / ".agent"
    assert aai.load_key(str(p)) == ""
    p.write_text(json.dumps({"assemblyai_key": "abc", "model": "x"}))
    assert aai.load_key(str(p)) == "abc"
    p.write_text("not json")
    assert aai.load_key(str(p)) == ""
