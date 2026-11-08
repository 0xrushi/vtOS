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

def main(env, args):
    host = args[0] if len(args) > 0 else None
    port = int(args[1]) if len(args) > 1 else 22
    auth_user = args[2] if len(args) > 2 else ""
    auth_pass = args[3] if len(args) > 3 else ""

    if host is None:
        print("Usage: ssh <host> [port] [user] [password]")
        return

    try:
        session = SSHSession(host, port, auth_user, auth_pass)
    except OSError as e:
        print(f"ssh: {e}")
        return

    previous_scrollback = getattr(env, "ssh_scrollback", False)
    env.ssh_scrollback = True
    try:
        session.run()
    finally:
        env.ssh_scrollback = previous_scrollback
