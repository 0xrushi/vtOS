"""Exercise the SSH terminal bridge with MicroPython-style text conversion."""

import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class TextInput:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)

    def read(self, size):
        return self.buffer.read(size).replace(b"\r", b"\n").decode("utf-8")


class TextOutput:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.buffer.write(data.replace(b"\n", b"\r\n"))

    def flush(self):
        pass


def load_ssh():
    path = Path(__file__).resolve().parents[2] / "modules/scripts/bin/ssh.py"
    spec = importlib.util.spec_from_file_location("vtos_ssh_test", path)
    ssh = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"modssh": SimpleNamespace(CONNECTED=1)}):
        spec.loader.exec_module(ssh)
    return ssh


def run_bridge(keys, remote_output=b""):
    ssh = load_ssh()

    stdin = TextInput(keys)
    stdout = TextOutput()
    ssh.sys = SimpleNamespace(stdin=stdin, stdout=stdout)

    class Client:
        def __init__(self):
            self.received = bytearray()
            self.output = remote_output

        def status(self):
            return 1

        def readinto(self, buf):
            data = self.output[:len(buf)]
            buf[:len(data)] = data
            self.output = self.output[len(data):]
            return len(data)

        def write(self, data):
            self.received.extend(data)

    client = Client()

    def select(readers, writers, errors, timeout):
        ready = []
        for reader in readers:
            if reader is client and client.output:
                ready.append(reader)
            elif reader is stdin or reader is stdin.buffer:
                if stdin.buffer.tell() < len(keys):
                    ready.append(reader)
        return ready, [], []

    ssh.select = SimpleNamespace(select=select)
    session = ssh.SSHSession.__new__(ssh.SSHSession)
    session.client = client
    session.connected = True
    session.process()
    return bytes(client.received), stdout.buffer.getvalue()


def test_enter_and_control_j_remain_distinct():
    sent, _ = run_bridge(b"hi\r\n\x1d")
    assert sent == b"hi\r\n"


def test_remote_line_endings_and_escape_sequences_are_unchanged():
    remote = b"\x1b[2J\x1b[Hhello\r\nworld\n\xc2\xb7\x1b[1G"
    _, displayed = run_bridge(b"\x1d", remote)
    assert displayed == remote


def test_local_escape_is_not_forwarded_to_remote():
    sent, _ = run_bridge(b"x\x1dignored")
    assert sent == b"x"


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_ssh_owns_local_scrollback_only_until_session_exits(previous, fails):
    ssh = load_ssh()
    env = SimpleNamespace(ssh_scrollback=previous)
    observed = []

    class Session:
        def __init__(self, *args):
            pass

        def run(self):
            observed.append(env.ssh_scrollback)
            if fails:
                raise RuntimeError("session interrupted")

    ssh.SSHSession = Session
    if fails:
        with pytest.raises(RuntimeError):
            ssh.main(env, ["example.com"])
    else:
        ssh.main(env, ["example.com"])
    assert observed == [True]
    assert env.ssh_scrollback is previous
