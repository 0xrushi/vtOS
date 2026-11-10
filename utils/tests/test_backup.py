"""Test the encrypted settings backup: format, auth, whitelist, round trip."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules/scripts/lib"))

import backup  # noqa: E402


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(backup, "ITERATIONS", 5)


def make_root(tmp_path):
    root = tmp_path / "flash"
    (root / ".ssh").mkdir(parents=True)
    (root / "menu").mkdir()
    (root / ".agent").write_text('{"api_key": "sk-secret"}')
    (root / ".ssh/config").write_text("Host a\n")
    (root / "menu/.md.json").write_text("{}")
    (root / "menu/other.txt").write_text("skip")
    (root / "random.bin").write_bytes(b"not a setting")
    return str(root)


class FakeWifi:
    def __init__(self):
        self.saved = None

    def load_station(self):
        return (b"home", b"pw\x00x")

    def save_station(self, ssid, pw):
        self.saved = (ssid, pw)


class FakeTs:
    def __init__(self):
        self.cfg = None

    def get_config(self):
        return ("tskey-abc", "vtos")

    def configure(self, key, name):
        self.cfg = (key, name)


def test_roundtrip_files_and_nvs(tmp_path):
    root = make_root(tmp_path)
    payload = backup.collect(root, FakeWifi(), FakeTs())
    assert set(payload["files"]) == {".agent", ".ssh/config", "menu/.md.json"}
    blob = backup.seal(payload, "hunter2")
    assert b"sk-secret" not in blob and b"home" not in blob

    out = tmp_path / "restored"
    out.mkdir()
    wifi, ts = FakeWifi(), FakeTs()
    got = backup.apply(backup.unseal(blob, "hunter2"), str(out), wifi, ts)
    assert set(got) == {".agent", ".ssh/config", "menu/.md.json", "wifi", "tailscale"}
    assert (out / ".agent").read_text() == '{"api_key": "sk-secret"}'
    assert (out / ".ssh/config").read_text() == "Host a\n"
    assert wifi.saved == (b"home", b"pw\x00x")
    assert ts.cfg == ("tskey-abc", "vtos")


def test_wrong_passphrase_and_tamper_rejected(tmp_path):
    blob = backup.seal({"v": 1, "files": {}}, "right")
    with pytest.raises(backup.BackupError):
        backup.unseal(blob, "wrong")
    bad = bytearray(blob)
    bad[-1] ^= 1
    with pytest.raises(backup.BackupError):
        backup.unseal(bytes(bad), "right")
    with pytest.raises(backup.BackupError):
        backup.unseal(b"hello", "right")


def test_empty_passphrase_refused():
    with pytest.raises(backup.BackupError):
        backup.seal({"v": 1}, "")


@pytest.mark.parametrize("name", ["../etc/x", "/abs", "boot.py", "menu/../x",
                                  "menu/.a.txt", "menu/sub/.a.json", ".ssh/id"])
def test_apply_refuses_unlisted_names(tmp_path, name):
    payload = {"v": 1, "files": {".agent": "e30=", name: "e30="}}
    with pytest.raises(backup.BackupError):
        backup.apply(payload, str(tmp_path))
    assert not (tmp_path / ".agent").exists()  # nothing written on failure


def test_export_import_file(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(backup, "_modules", lambda: (None, None))
    dest = str(tmp_path / "sd/backups/b.vtbk")
    size = backup.export_file(dest, "pw", root)
    assert size > 0
    assert backup.list_backups(str(tmp_path / "sd/backups")) == ["b.vtbk"]
    (Path(root) / ".agent").unlink()
    backup.import_file(dest, "pw", root)
    assert (Path(root) / ".agent").exists()


def test_hmac_matches_stdlib():
    import hashlib
    import hmac
    for key in (b"k", b"x" * 100):
        assert backup._hmac(key, b"msg") == hmac.new(key, b"msg", hashlib.sha256).digest()
