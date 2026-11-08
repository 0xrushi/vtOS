"""Check ssh config parsing and profile selection without a network."""

import io
import sys
from types import SimpleNamespace

from .test_ssh_terminal import load_ssh

CONFIG = """
# work boxes
Host *
    User admin
    Port 2200

Host pi pi-lan
    HostName 192.168.1.50
    User pi
    Password "raspberry pass"

Host dev
    HostName=dev.example.com
    Port 22
    Password hunter2

Host *.internal !bad
Host bad
    HostName nope
    Port notanumber
"""


def test_parse_config_resolves_aliases_defaults_and_skips_wildcards():
    ssh = load_ssh()
    profiles = {p["alias"]: p for p in ssh.parse_config(CONFIG)}
    assert list(profiles) == ["pi", "pi-lan", "dev", "bad"]
    assert profiles["pi"] == {"alias": "pi", "host": "192.168.1.50", "port": 2200,
                              "user": "pi", "password": "raspberry pass"}
    assert profiles["pi-lan"]["host"] == "192.168.1.50"
    assert profiles["dev"]["host"] == "dev.example.com"
    assert profiles["dev"]["user"] == "admin"      # from Host *
    assert profiles["dev"]["port"] == 22            # explicit beats default
    assert profiles["bad"]["port"] == 22            # unparsable port falls back
    assert profiles["bad"]["password"] is None


def test_load_profiles_uses_first_existing_file_else_empty(tmp_path):
    ssh = load_ssh()
    cfg = tmp_path / "config"
    cfg.write_text("Host a\n  HostName h\n")
    assert [p["alias"] for p in ssh.load_profiles([str(tmp_path / "no"), str(cfg)])] == ["a"]
    assert ssh.load_profiles([str(tmp_path / "no")]) == []


def test_describe():
    ssh = load_ssh()
    assert ssh.describe({"host": "h", "port": 22, "user": ""}) == "h"
    assert ssh.describe({"host": "h", "port": 2222, "user": "u"}) == "u@h:2222"


def run_main(ssh, args, profiles, picked=None, typed=""):
    calls = []

    class Session:
        def __init__(self, *a):
            calls.append(a)

        def run(self):
            pass

    ssh.SSHSession = Session
    ssh.load_profiles = lambda: profiles
    ssh._choose_profile = lambda env, p: picked
    out, old = io.StringIO(), (sys.stdin, sys.stdout)
    sys.stdin, sys.stdout = io.StringIO(typed), out
    ssh.sys = sys
    try:
        ssh.main(SimpleNamespace(), args)
    finally:
        sys.stdin, sys.stdout = old
    return calls, out.getvalue()


def profiles_for(ssh):
    return ssh.parse_config(CONFIG)


def test_no_args_opens_picker_and_connects_with_stored_password():
    ssh = load_ssh()
    calls, _ = run_main(ssh, [], profiles_for(ssh), picked="dev")
    assert calls == [("dev.example.com", 22, "admin", "hunter2")]


def test_cancelled_picker_connects_nothing():
    ssh = load_ssh()
    assert run_main(ssh, [], profiles_for(ssh), picked=None)[0] == []


def test_named_profile_without_password_prompts_without_echo():
    ssh = load_ssh()
    profiles = ssh.parse_config("Host box\n  HostName 10.0.0.2\n  User me\n")
    calls, out = run_main(ssh, ["box"], profiles, typed="secx\x7fret\r")
    assert calls == [("10.0.0.2", 22, "me", "secret")]
    assert "secret" not in out and "secx" not in out


def test_escape_at_password_prompt_cancels():
    ssh = load_ssh()
    profiles = ssh.parse_config("Host box\n  HostName 10.0.0.2\n")
    assert run_main(ssh, ["box"], profiles, typed="ab\x1b")[0] == []


def test_manual_arguments_and_usage_are_unchanged():
    ssh = load_ssh()
    profiles = profiles_for(ssh)
    calls, _ = run_main(ssh, ["1.2.3.4", "22", "me", "pw"], profiles)
    assert calls == [("1.2.3.4", 22, "me", "pw")]
    calls, out = run_main(ssh, [], [], picked=None)
    assert calls == [] and "Usage: ssh" in out
    calls, _ = run_main(ssh, ["pi", "22", "x", "y"], profiles)  # not a bare alias
    assert calls == [("pi", 22, "x", "y")]
