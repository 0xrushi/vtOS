import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def load_microphone():
    path = Path(__file__).resolve().parents[2] / "modules/scripts/lib/ssh_mic.py"
    spec = importlib.util.spec_from_file_location("ssh_mic_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"modssh": SimpleNamespace(CONNECTED=1),
                                     "hardware": SimpleNamespace()}):
        spec.loader.exec_module(module)
    return module


def test_partial_ssh_writes_do_not_drop_or_duplicate_pcm():
    module = load_microphone()
    sent = bytearray()
    chunks = [b"\x01\x02\x03\x04\x05\x06"]

    class Recorder:
        def readinto(self, buf):
            chunk = chunks.pop(0)
            buf[:len(chunk)] = chunk
            return len(chunk)

        def is_recording(self):
            return True

    class Client:
        def status(self):
            return 1

        def write(self, data):
            sent.extend(data[:2])
            return min(2, len(data))

    microphone = module.MicrophoneForwarder.__new__(module.MicrophoneForwarder)
    microphone.client = Client()
    microphone.recorder = Recorder()
    microphone.buffer = bytearray(16)
    microphone.view = memoryview(microphone.buffer)
    microphone.offset = microphone.length = microphone.sent_bytes = 0
    microphone.blocked_since = None
    module.select = SimpleNamespace(select=lambda *args: ([microphone.recorder], [], []))
    for _ in range(3):
        microphone.pump()
    assert sent == b"\x01\x02\x03\x04\x05\x06"
    assert microphone.sent_bytes == 6


def test_audio_channel_disconnect_is_reported():
    module = load_microphone()
    microphone = module.MicrophoneForwarder.__new__(module.MicrophoneForwarder)
    microphone.client = SimpleNamespace(status=lambda: 2)
    with pytest.raises(OSError, match="closed"):
        microphone.pump()
