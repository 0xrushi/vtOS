#
# MicroPython SSH Client (wolfSSH-backed, via the modssh C module)
# Copyright (c) 2026 8bitmcu
# License: MIT
#

import select
import time
import sys

import modssh

_CONNECT_TIMEOUT_MS = 15000
_READ_CHUNK = 256

class SSHSession:
    def __init__(self, host, port, username, password):
        self.client = modssh.Client()
        self.connected = False

        print(f"Connecting to {host}:{port}...\n")
        self.client.connect(host, port, username, password)

        waited_ms = 0
        while self.client.status() == modssh.CONNECTING:
            time.sleep_ms(10)
            waited_ms += 10
            if waited_ms >= _CONNECT_TIMEOUT_MS:
                self.client.disconnect()
                raise OSError("connect timed out")

        if self.client.status() != modssh.CONNECTED:
            raise OSError(
                f"connect failed (error_code={self.client.error_code()}, "
                f"auth_attempts={self.client.auth_attempts()}, "
                f"last_auth_type={self.client.last_auth_type()})"
            )

        self.connected = True
        print("Connected!")

    def run(self):
        try:
            self.process()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def process(self):
        inbuf = bytearray(_READ_CHUNK)
        # SSH carries terminal bytes unchanged. MicroPython's text stdin turns
        # Enter (CR) into LF, which TUIs can interpret as an inserted newline.
        # Text stdout also expands LF to CRLF, corrupting remote line endings.
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer

        while self.connected:
            if self.client.status() != modssh.CONNECTED:
                break

            microphone = getattr(self, "microphone", None)
            if microphone is not None:
                try:
                    microphone.pump()
                except OSError as e:
                    microphone.close()
                    self.microphone = None
                    print("\r\nMicrophone stopped:", e)

            sock_ready, _, _ = select.select([self.client], [], [], 0)
            stdin_ready, _, _ = select.select([stdin], [], [], 0)
            if not sock_ready and not stdin_ready:
                time.sleep_ms(10)

            if sock_ready:
                try:
                    n = self.client.readinto(inbuf)
                except OSError:
                    n = 0
                if n:
                    stdout.write(inbuf[:n])

            if stdin_ready:
                char = stdin.read(1)
                if char:
                    if char == b'\x1c':  # physical microphone key
                        if getattr(self, "microphone", None) is not None:
                            self.client.write(b' ')  # Claude /voice tap
                        continue
                    if char == b'\x1d':  # Ctrl-]: matches telnet-style escape
                        break
                    try:
                        self.client.write(char)
                    except OSError:
                        pass  # tx ring buffer momentarily full; drop and retry next key

    def close(self):
        if self.connected:
            self.connected = False
            self.client.disconnect()
            print("\nDisconnected.\n")

_CONFIG_PATHS = ("/flash/.ssh/config", "/sd/.ssh/config")


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_config(text):
    """OpenSSH-style config -> [{alias, host, port, user, password}].

    Understands Host / HostName / Port / User blocks, plus a vtOS-specific
    Password line (the client only does password auth). Wildcard blocks
    such as `Host *` provide defaults for the profiles that don't set a
    value; they are never listed themselves.
    """
    blocks = []      # [(aliases, {key: value})]
    current = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line.split(None, 1)[0]:
            key, value = line.split("=", 1)
        else:
            parts = line.split(None, 1)
            key, value = parts[0], (parts[1] if len(parts) > 1 else "")
        key = key.strip().lower()
        value = value.strip()
        if value.startswith("="):
            value = value[1:].strip()
        if key == "host":
            current = ({}, [_unquote(a) for a in value.split()])
            blocks.append(current)
        elif current is not None and key in ("hostname", "port", "user", "password"):
            current[0].setdefault(key, _unquote(value))

    defaults = {}
    for opts, aliases in blocks:
        if "*" in aliases:
            for k, v in opts.items():
                defaults.setdefault(k, v)

    profiles = []
    for opts, aliases in blocks:
        for alias in aliases:
            if not alias or alias[0] == "!" or "*" in alias or "?" in alias:
                continue
            merged = dict(defaults)
            merged.update(opts)
            try:
                port = int(merged.get("port", 22))
            except ValueError:
                port = 22
            profiles.append({
                "alias": alias,
                "host": merged.get("hostname", alias),
                "port": port,
                "user": merged.get("user", ""),
                "password": merged.get("password"),
            })
    return profiles


def load_profiles(paths=_CONFIG_PATHS):
    """Profiles from the first config file that exists, else []."""
    for path in paths:
        try:
            with open(path, "r") as f:
                return parse_config(f.read())
        except OSError:
            continue
    return []


def describe(profile):
    target = profile["host"]
    if profile["port"] != 22:
        target += ":%d" % profile["port"]
    if profile["user"]:
        target = profile["user"] + "@" + target
    return target


def _choose_profile(env, profiles):
    try:
        launcher = __import__("bin.launcher", None, None, [""])
    except ImportError:
        import launcher
    entries = [(p["alias"], describe(p)) for p in profiles]
    return launcher.pick(env, entries, "ssh")


def _read_password(prompt):
    sys.stdout.write(prompt)
    chars = []
    while True:
        ch = sys.stdin.read(1)
        if ch in ("\r", "\n"):
            sys.stdout.write("\r\n")
            return "".join(chars)
        if ch in ("\x08", "\x7f"):
            if chars:
                chars.pop()
        elif ch == "\x1b":
            sys.stdout.write("\r\n")
            return None
        elif ch and 32 <= ord(ch) <= 126:
            chars.append(ch)


def main(env, args):
    forward_microphone = bool(args and args[0] == "--mic")
    if forward_microphone:
        args = args[1:]
    profiles = load_profiles()
    by_alias = {p["alias"]: p for p in profiles}

    profile = None
    if len(args) == 0 and profiles:
        name = _choose_profile(env, profiles)
        if name is None:
            return
        profile = by_alias[name]
    elif len(args) == 1 and args[0] in by_alias:
        profile = by_alias[args[0]]

    if profile is not None:
        host, port, auth_user = profile["host"], profile["port"], profile["user"]
        auth_pass = profile["password"]
        if auth_pass is None:
            auth_pass = _read_password("%s password: " % describe(profile))
            if auth_pass is None:
                return
    else:
        host = args[0] if len(args) > 0 else None
        port = int(args[1]) if len(args) > 1 else 22
        auth_user = args[2] if len(args) > 2 else ""
        auth_pass = args[3] if len(args) > 3 else ""

    if host is None:
        print("Usage: ssh [--mic] <host> [port] [user] [password]")
        print("       ssh <profile>   (Host entries in /flash/.ssh/config)")
        return

    try:
        session = SSHSession(host, port, auth_user, auth_pass)
    except OSError as e:
        print(f"ssh: {e}")
        return

    previous_scrollback = getattr(env, "ssh_scrollback", False)
    env.ssh_scrollback = True
    session_started = False
    try:
        if forward_microphone:
            from ssh_mic import MicrophoneForwarder
            session.microphone = MicrophoneForwarder(env, host, port, auth_user, auth_pass)
            print("Microphone streaming over SSH. Use the mic key with Claude /voice tap.")
        session_started = True
        session.run()
    finally:
        try:
            microphone = getattr(session, "microphone", None)
            if microphone is not None:
                microphone.close()
        finally:
            try:
                if not session_started:
                    session.close()
            finally:
                env.ssh_scrollback = previous_scrollback
