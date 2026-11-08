import importlib.util
import io
from pathlib import Path

import pytest


def bridge_module():
    path = Path(__file__).resolve().parents[1] / "tdeck_mic_bridge.py"
    spec = importlib.util.spec_from_file_location("tdeck_mic_bridge_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FragmentedSource:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read(self, size):
        return next(self.chunks, b"")


class ShortWriter(io.BytesIO):
    def write(self, data):
        return super().write(data[:3])


def test_relay_preserves_pcm_across_fragmented_reads_and_short_writes():
    bridge = bridge_module()
    destination = ShortWriter()
    chunks = [b"\x01", b"\x02\x03", b"\x04\x05\x06", b"\x07\x08"]
    bridge.relay(FragmentedSource(chunks), destination)
    assert destination.getvalue() == b"".join(chunks)


def test_relay_rejects_partial_sample_at_eof():
    with pytest.raises(ValueError, match="Truncated"):
        bridge_module().relay(io.BytesIO(b"\x01\x02\x03"), io.BytesIO())


def test_existing_audio_devices_are_not_unloaded(monkeypatch):
    bridge = bridge_module()
    monkeypatch.setattr(bridge, "device_names", lambda kind: {bridge.SINK, bridge.SOURCE})
    monkeypatch.setattr(bridge, "pactl", lambda *args: pytest.fail("Existing devices modified"))
    assert bridge.create_microphone() == []


def test_partial_setup_is_cleaned_up(monkeypatch):
    bridge = bridge_module()
    monkeypatch.setattr(bridge, "device_names", lambda kind: set())
    calls = []

    def pactl(*args):
        calls.append(args)
        if args[1] == "module-null-sink":
            return "42"
        raise RuntimeError("Source creation failed")

    removed = []
    monkeypatch.setattr(bridge, "pactl", pactl)
    monkeypatch.setattr(bridge, "remove_microphone", lambda owned: removed.extend(owned))
    with pytest.raises(RuntimeError):
        bridge.create_microphone()
    assert removed == ["42"]
    assert not any("set-default" in arg for call in calls for arg in call)


def test_claude_uses_only_forwarded_source_without_changing_parent_environment(monkeypatch):
    bridge = bridge_module()
    monkeypatch.setattr(bridge, "device_names", lambda kind: {bridge.SOURCE})
    monkeypatch.setenv("SSH_CONNECTION", "test-connection")
    monkeypatch.setenv("PULSE_SOURCE", "original_mic")
    executed = []
    monkeypatch.setattr(bridge.os, "execvpe", lambda *args: executed.append(args))
    bridge.launch_claude(["--continue"])
    name, argv, env = executed[0]
    assert name == "claude" and "--continue" in argv
    assert env["PULSE_SOURCE"] == bridge.SOURCE
    assert "SSH_CONNECTION" not in env
    assert bridge.os.environ["PULSE_SOURCE"] == "original_mic"
