"""Manual VPN setup, SSH policy and the actual native socket preparation loop."""
import ctypes
import importlib.util
import io
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_module(path, stubs=None):
    spec = importlib.util.spec_from_file_location("vtos_ts_test", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", stubs or {}):
        spec.loader.exec_module(module)
    return module


class Clock:
    now = 0

    def ticks_ms(self):
        return self.now

    def ticks_diff(self, a, b):
        return a - b

    def sleep_ms(self, ms):
        self.now += ms


class VPN:
    def __init__(self, states=None, configured=True):
        self.states = list(states or ["connected"])
        self.configured = configured
        self.calls = []

    def status(self):
        state = self.states[0]
        if len(self.states) > 1:
            self.states.pop(0)
        return dict(state=state, configured=self.configured,
                    connected=state == "connected", ip="100.64.1.2" if state == "connected" else None,
                    internal_free=40000, internal_largest=20000, psram_free=3000000)

    def configure(self, key, name):
        self.calls.append(("configure", key, name))
        self.configured = True

    def up(self):
        self.calls.append("up")

    def down(self):
        self.calls.append("down")

    def ip(self):
        return "100.64.1.2"

    def peers(self):
        return [("100.64.1.3", "video-server", True)]


def command(vpn=None, wifi=True, keys=""):
    vpn = vpn or VPN()
    network = SimpleNamespace(STA_IF=0, WLAN=lambda _: SimpleNamespace(isconnected=lambda: wifi))
    module = load_module("modules/scripts/bin/tailscale.py", {"network": network})
    output = io.StringIO()
    module.sys = SimpleNamespace(stdin=io.StringIO(keys), stdout=output)
    module.time = Clock()

    def run(args):
        with patch.dict("sys.modules", {"modtailscale": vpn}), patch("sys.stdout", output):
            module.main(None, args)
        return output.getvalue()

    return module, run, vpn


def test_import_and_status_never_start_vpn():
    _, run, vpn = command()
    assert vpn.calls == []
    assert "startup: manual" in run([])
    assert vpn.calls == []


def test_first_up_hides_key_saves_default_hostname_and_joins():
    key = "test-only-tailscale-key"
    _, run, vpn = command(VPN(configured=False), keys=key + "\r\r")
    output = run(["up"])
    assert key not in output
    assert vpn.calls == [("configure", key, "vtos-tdeck"), "up"]
    assert "100.64.1.2" in output


def test_hidden_entry_backspace_and_hostname_echo():
    module, _, _ = command(keys="ab\bcd\rhost\r")
    assert module._read_line("key: ", hidden=True) == "acd"
    assert module.sys.stdout.getvalue() == "key: \r\n"
    assert module._read_line("name: ") == "host"
    assert "host" in module.sys.stdout.getvalue()


@pytest.mark.parametrize("keys", ["\x1b", "tskey-auth-hidden\r\x1b"])
def test_cancel_setup_never_saves_or_starts(keys):
    _, run, vpn = command(VPN(configured=False), keys=keys)
    run(["up"])
    assert vpn.calls == []


def test_up_without_wifi_does_not_prompt_or_allocate():
    _, run, vpn = command(VPN(configured=False), wifi=False)
    assert "connect Wi-Fi" in run(["up"])
    assert vpn.calls == []


@pytest.mark.parametrize("state, message", [
    ("error", "registration failed"), ("registering", "registration timed out"),
    ("down", "disconnected while joining"),
])
def test_failed_registration_cleans_up(state, message):
    module, run, vpn = command(VPN([state]))
    assert message in run(["up"])
    assert vpn.calls == ["up", "down"]
    assert module.time.now <= 60000


def test_cancel_wait_stops_vpn():
    module, run, vpn = command(VPN(["registering"]))
    module.time.sleep_ms = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
    assert "cancelled" in run(["up"])
    assert vpn.calls == ["up", "down"]


def test_initial_wifi_wait_is_not_a_disconnect():
    _, run, vpn = command(VPN(["down", "wifi_wait", "registering", "connected"]))
    assert "100.64.1.2" in run(["up"])
    assert vpn.calls == ["up"]


def test_wifi_loss_during_join_cleans_up():
    module, run, vpn = command(VPN(["registering"]))
    connected = iter([True, False])
    module.network.WLAN = lambda _: SimpleNamespace(isconnected=lambda: next(connected))
    assert "disconnected while joining" in run(["up"])
    assert vpn.calls == ["up", "down"]


def test_partial_native_start_is_cleaned_up():
    _, run, vpn = command()
    def fail():
        vpn.calls.append("up")
        raise OSError("allocation failed")
    vpn.up = fail
    assert "allocation failed" in run(["up"])
    assert vpn.calls == ["up", "down"]


@pytest.mark.parametrize("action, expected", [("down", "disconnected"), ("ip", "100.64.1.2"), ("peers", "video-server")])
def test_command_dispatch(action, expected):
    _, run, vpn = command()
    assert expected in run([action])
    assert vpn.calls == (["down"] if action == "down" else [])


def test_reconfigure_is_explicit_and_does_not_connect():
    _, run, vpn = command(VPN(["down"]), keys="tskey-auth-new\rcustom-name\r")
    assert "configuration saved" in run(["configure"])
    assert vpn.calls == [("configure", "tskey-auth-new", "custom-name")]


def test_reconfigure_running_vpn_is_rejected():
    _, run, vpn = command()
    assert "tailscale down" in run(["configure"])
    assert vpn.calls == []


def load_ssh():
    return load_module("modules/scripts/bin/ssh.py", {"modssh": SimpleNamespace(CONNECTING=1, CONNECTED=2)})


@pytest.mark.parametrize("host, expected", [
    ("100.64.0.0", True), ("100.127.255.255", True), ("100.100.100.100", True),
    ("100.63.255.255", False), ("100.128.0.0", False), ("100.1.2.3", False),
    ("192.168.1.2", False), ("101.64.0.1", False), ("100.64.256.1", False),
    ("100.64.-1.1", False), ("100.64.1", False), ("server.ts.net", False),
    ("100.64.1.1.extra", False), ("100.64.1. 1", False),
    ("100.064.1.1", False),
])
def test_tailnet_ip_boundaries(host, expected):
    assert load_ssh().is_tailnet_host(host) is expected


@pytest.mark.parametrize("host, budget", [("100.64.1.3", 45000), ("192.168.1.3", 15000)])
def test_ssh_connection_budget_and_cleanup(host, budget):
    ssh = load_ssh()
    clock = Clock()
    client = SimpleNamespace(connect=lambda *args: None, status=lambda: 1,
                             disconnect=lambda: disconnected.append(True))
    disconnected = []
    ssh.modssh.Client = lambda: client
    ssh.time = clock
    with pytest.raises(OSError, match="timed out"):
        ssh.SSHSession(host, 22, "me", "secret")
    assert clock.now == budget
    assert disconnected == [True]


@pytest.mark.parametrize("code, message", [(-2010, "tailscale up"), (-2011, "peer unavailable"), (-2012, "timed out"), (-2013, "bind SSH")])
def test_ssh_native_errors_are_actionable_and_disconnect(code, message):
    ssh = load_ssh()
    disconnected = []
    ssh.modssh.Client = lambda: SimpleNamespace(connect=lambda *args: None, status=lambda: 3,
        error_code=lambda: code, disconnect=lambda: disconnected.append(True))
    ssh.time = Clock()
    with pytest.raises(OSError, match=message):
        ssh.SSHSession("100.64.1.3", 22, "me", "secret")
    assert disconnected == [True]


@pytest.fixture(scope="module")
def native_transport(tmp_path_factory):
    """Compile the real C transport functions against deterministic RTOS/lwIP mocks."""
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler unavailable")
    work = tmp_path_factory.mktemp("tailscale-native")
    source = (ROOT / "modules/modtailscale/modtailscale.c").read_text()
    transport = source[source.index("bool vtos_tailscale_ip("):source.index("static void network_changed(")]
    fixture = (ROOT / "utils/tests/fixtures/tailscale_transport.c").read_text()
    (work / "test.c").write_text(fixture.replace("/* TRANSPORT */", transport))
    library = work / "transport.so"
    flags = []
    if sys.platform == "darwin":
        linker = subprocess.check_output(["xcrun", "--find", "ld"], text=True).strip()
        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        flags = ["--ld-path=" + linker, "-isysroot", sdk]
    subprocess.run([compiler, *flags, "-shared", "-fPIC", "-std=c11", "-Wall", "-Werror",
                    "-I", str(ROOT / "modules/modtailscale"), str(work / "test.c"),
                    "-o", str(library)], check=True, capture_output=True, text=True)
    return ctypes.CDLL(str(library))


@pytest.mark.parametrize("case, result", [(0, 0), (1, -2010), (2, -2011), (3, -2012),
                                         (4, -2007), (5, -2013), (6, -2010), (7, -2010)])
def test_native_transport_outcomes(native_transport, case, result):
    assert native_transport.run_case(case) == result
    assert native_transport.tracked_count() == 0  # SSH closes/releases on all paths


def test_native_socket_is_bound_to_vpn_source_after_handshake(native_transport):
    assert native_transport.run_case(0) == 0
    assert ctypes.c_uint32.in_dll(native_transport, "bound_ip").value == 0x64400102
    assert ctypes.c_int.in_dll(native_transport, "probes").value > 0


@pytest.mark.parametrize("ip, expected", [(0x64400000, True), (0x647fffff, True),
    (0x643fffff, False), (0x64800000, False), (0xc0a80101, False)])
def test_native_tailnet_range(native_transport, ip, expected):
    native_transport.vtos_tailscale_ip.argtypes = [ctypes.c_uint32]
    native_transport.vtos_tailscale_ip.restype = ctypes.c_bool
    assert native_transport.vtos_tailscale_ip(ip) is expected
