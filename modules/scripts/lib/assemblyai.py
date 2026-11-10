"""Voice typing: record from the T-Deck microphone, transcribe with AssemblyAI.

Push-to-talk, not streaming. Audio is 16 kHz mono 16-bit PCM kept in one
buffer (a 44-byte WAV header sits in front of it, so the buffer can be
uploaded as it is), then: upload -> submit a transcript -> poll until done.
The key is the `assemblyai_key` that `agent set assemblyai_key ...` stores in
/flash/.agent; it is never printed.
"""

import json
import time

CONFIG = "/flash/.agent"
API = "https://api.assemblyai.com/v2"
RATE = 16000
HEADER = 44
MAX_SECONDS = 15
POLL_MS = 1000
TIMEOUT_MS = 40000


class VoiceError(Exception):
    pass


def load_key(path=None):
    try:
        with open(path or CONFIG) as f:
            return json.loads(f.read()).get("assemblyai_key", "") or ""
    except (OSError, ValueError):
        return ""


def wav_header(pcm_bytes):
    """44-byte WAV header for 16 kHz mono 16-bit PCM of pcm_bytes bytes."""
    def u32(v):
        return bytes((v & 255, (v >> 8) & 255, (v >> 16) & 255, (v >> 24) & 255))
    return (b"RIFF" + u32(36 + pcm_bytes) + b"WAVEfmt " + u32(16) +
            b"\x01\x00\x01\x00" + u32(RATE) + u32(RATE * 2) + b"\x02\x00\x10\x00" +
            b"data" + u32(pcm_bytes))


class Recorder:
    """Records into a preallocated buffer; pump() is called from the main loop."""

    def __init__(self, make_mic, select_fn=None, seconds=MAX_SECONDS):
        self.mic = make_mic()
        capacity = RATE * 2 * seconds
        while True:
            try:
                self.buf = bytearray(HEADER + capacity)
                break
            except MemoryError:
                capacity //= 2
                if capacity < RATE * 2:
                    raise VoiceError("not enough memory to record")
        self.view = memoryview(self.buf)
        self.capacity = capacity
        self.length = 0
        self._select = select_fn
        self.mic.record(None)

    @property
    def full(self):
        return self.length >= self.capacity

    @property
    def seconds(self):
        return self.length / (RATE * 2)

    def pump(self):
        """Move whatever the microphone has captured into the buffer."""
        while not self.full:
            if self._select is not None and not self._select([self.mic], [], [], 0)[0]:
                return
            room = self.view[HEADER + self.length:HEADER + self.capacity]
            n = self.mic.readinto(room)
            if not n:
                return
            self.length += n

    def stop(self):
        """Stop capturing and return the buffer view ready to upload (WAV)."""
        self.pump()
        try:
            self.mic.stop()
        finally:
            self.mic.deinit()
        self.length -= self.length % 2
        self.buf[:HEADER] = wav_header(self.length)
        return self.view[:HEADER + self.length]


def _check(resp, what):
    if resp.status_code == 401 or resp.status_code == 403:
        resp.close()
        raise VoiceError("AssemblyAI refused the key")
    if not 200 <= resp.status_code <= 299:
        text = ""
        try:
            text = resp.text[:60]
        except Exception:
            pass
        resp.close()
        raise VoiceError("%s failed (%d) %s" % (what, resp.status_code, text))
    try:
        return resp.json()
    finally:
        resp.close()


def transcribe(wav, key, requests, sleep_ms=None, ticks_ms=None, ticks_diff=None,
               language=None):
    """Upload `wav` (header + PCM) and return the transcript text ('' if silent)."""
    sleep_ms = sleep_ms or time.sleep_ms
    ticks_ms = ticks_ms or time.ticks_ms
    ticks_diff = ticks_diff or time.ticks_diff
    if not key:
        raise VoiceError("no key: agent set assemblyai_key ...")
    auth = {"authorization": key}
    up = _check(requests.post(API + "/upload", data=wav, headers={
        "authorization": key, "Content-Type": "application/octet-stream"}), "upload")
    body = {"audio_url": up["upload_url"], "speech_models": ["universal-2"]}
    if language:
        body["language_code"] = language
        body["language_detection"] = False
    job = _check(requests.post(API + "/transcript", json=body, headers=auth), "submit")
    url = API + "/transcript/" + job["id"]
    start = ticks_ms()
    while True:
        sleep_ms(POLL_MS)
        state = _check(requests.get(url, headers=auth), "poll")
        status = state.get("status")
        if status == "completed":
            return (state.get("text") or "").strip()
        if status == "error":
            raise VoiceError(str(state.get("error") or "transcription failed")[:60])
        if ticks_diff(ticks_ms(), start) > TIMEOUT_MS:
            raise VoiceError("transcription timed out")
