"""Forward onboard microphone PCM over an authenticated SSH exec channel."""

import select
import time

import hardware
import modssh

_COMMAND = 'python3 -u "$HOME/.local/share/vtos/tdeck_mic_bridge.py" --stdio'
_READY = b"VTOS_MIC_READY 16000 1\n"


class MicrophoneForwarder:
    def __init__(self, env, host, port, username, password):
        self.client = None
        self.recorder = None
        self.buffer = bytearray(2048)
        self.view = memoryview(self.buffer)
        self.offset = self.length = 0
        self.sent_bytes = 0
        self.blocked_since = None
        try:
            if getattr(env, "rec", None) is not None:
                raise OSError("Microphone is already in use")
            self.client = modssh.Client()
            try:
                self.client.connect(host, port, username, password, _COMMAND)
            except TypeError:
                raise OSError("Microphone forwarding requires the updated firmware")
            start = time.ticks_ms()
            while self.client.status() == modssh.CONNECTING:
                if time.ticks_diff(time.ticks_ms(), start) > 15000:
                    raise OSError("Microphone SSH connection timed out")
                time.sleep_ms(10)
            if self.client.status() != modssh.CONNECTED:
                raise OSError("Microphone SSH authentication/connect failed")

            reply = bytearray()
            while not bytes(reply).endswith(b"\n"):
                if self.client.status() != modssh.CONNECTED:
                    raise OSError("Install tdeck_mic_bridge.py on the SSH host first")
                if time.ticks_diff(time.ticks_ms(), start) > 20000:
                    raise OSError("Remote microphone setup timed out")
                if select.select([self.client], [], [], 0)[0]:
                    n = self.client.readinto(self.buffer)
                    if n:
                        reply.extend(self.view[:n])
                        if len(reply) > 1024:
                            raise OSError("Invalid microphone bridge response")
                time.sleep_ms(10)
            if bytes(reply) != _READY:
                raise OSError(bytes(reply).decode("utf-8", "replace").strip())

            self.recorder = hardware.create_mic(sample_rate=16000, mic_gain=0)
            self.recorder.record(None)
        except BaseException:
            self.close()
            raise

    def pump(self):
        if self.client.status() != modssh.CONNECTED:
            raise OSError("Microphone SSH channel closed")
        if self.offset == self.length:
            self.offset = self.length = 0
            if select.select([self.recorder], [], [], 0)[0]:
                n = self.recorder.readinto(self.buffer)
                if n:
                    self.length = n
            if not self.length:
                if not self.recorder.is_recording():
                    raise OSError("Microphone capture stopped")
                return
        try:
            n = self.client.write(self.view[self.offset:self.length])
        except OSError as e:
            if not e.args or e.args[0] not in (11, 35):
                raise
            n = 0
        if n:
            self.offset += n
            self.sent_bytes += n
            self.blocked_since = None
        else:
            now = time.ticks_ms()
            if self.blocked_since is None:
                self.blocked_since = now
            elif time.ticks_diff(now, self.blocked_since) > 2000:
                raise OSError("Microphone SSH stream stalled")

    def close(self):
        try:
            if self.recorder is not None:
                try:
                    self.recorder.stop()
                finally:
                    self.recorder.deinit()
        finally:
            self.recorder = None
            if self.client is not None:
                self.client.disconnect()
                self.client = None
