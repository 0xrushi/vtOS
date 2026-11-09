"""Host checks for saving and restoring the last successful Wi-Fi connection."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "modules" / "scripts"


@pytest.fixture
def manager(monkeypatch):
    class Station:
        enabled = False
        connected = False
        succeeds = True

        def active(self, enabled=None):
            if enabled is not None:
                self.enabled = enabled
            return self.enabled

        def connect(self, ssid, password):
            self.last = ssid, password
            self.connected = self.succeeds

        def isconnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

        def ifconfig(self):
            return ("192.0.2.2",)

    station = Station()
    store = SimpleNamespace(profile=None)

    def save():
        assert station.isconnected()
        store.profile = station.last

    def forget():
        store.profile = None

    monkeypatch.setitem(sys.modules, "network", SimpleNamespace(WLAN=lambda _: station, STA_IF=0))
    monkeypatch.setitem(sys.modules, "modwifi", SimpleNamespace(save_station=save,
                        load_station=lambda: store.profile, forget_station=forget))
    for name, path in (("wificonfig", SCRIPTS / "sys/wificonfig.py"),
                       ("netmgr", SCRIPTS / "bin/netmgr.py")):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    module.time = SimpleNamespace(sleep=lambda _: None)
    return module, station, store


def test_boot_reconnects_after_successful_nm_connect(manager):
    nm, station, store = manager
    nm.wificonfig.start_at_boot()
    assert not station.active()
    nm.main(None, ["connect", "home", "test-password"])
    assert store.profile == ("home", "test-password")
    station.disconnect()
    station.active(False)
    nm.wificonfig.start_at_boot()
    assert station.active() and station.isconnected()
    assert station.last == store.profile


def test_failed_connection_preserves_profile_and_forget_clears_it(manager):
    nm, station, store = manager
    store.profile = (b" home ", b"")
    station.succeeds = False
    nm.main(None, ["connect", "unavailable", "test-password"])
    assert not station.isconnected()
    assert store.profile == (b" home ", b"")
    station.succeeds = True
    nm.main(None, ["reconnect"])
    assert station.last == store.profile
    nm.main(None, ["forget"])
    assert store.profile is None
    assert not station.isconnected()
