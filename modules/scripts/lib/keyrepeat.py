"""Add held Backspace repeat without changing the keyboard's character map."""

import io
import time

_ADDRESS = 0x55
_RAW_MODE = b"\x03"
_KEY_MODE = b"\x04"
_BACKSPACE = 8
_D_KEYS = (ord("d"), ord("D"))
# Private byte the shell treats as "open the launcher" (Alt+D).
LAUNCHER_KEY = 0x1D
MICROPHONE_KEY = 0x1C
_POLLIN = 1
_POLLOUT = 4
_POLL = 3


class BackspaceRepeat:
    def __init__(self, delay_ms=400, interval_ms=60):
        self.delay_ms = delay_ms
        self.interval_ms = interval_ms
        self.deadline = None

    def due(self, now):
        return self.deadline is not None and time.ticks_diff(now, self.deadline) >= 0

    def update(self, key, held, now):
        if not held:
            self.deadline = None
            return key
        if key is not None:
            self.deadline = time.ticks_add(now, self.delay_ms) if key == _BACKSPACE else None
            return key
        if self.due(now):
            # Emit one repeat, without catching up in a burst after a stall.
            self.deadline = time.ticks_add(now, self.interval_ms)
            return _BACKSPACE
        return None


class KeyboardRepeatTerminal(io.IOBase):
    def __init__(self, stream, bus):
        self.stream = stream
        self.bus = bus
        self.repeat = BackspaceRepeat()
        self._one = bytearray(1)
        self._matrix = bytearray(5)
        self._pending = None
        self._key_mode = True
        self._alt = False
        self._mic_down = False
        self._mic_known = False
        self._mic_pressed = False
        self._held = self._read_held()
        self._last_state = time.ticks_ms()

    def _read_held(self):
        self._key_mode = False
        try:
            self.bus.writeto(_ADDRESS, _RAW_MODE)
            self.bus.readfrom_into(_ADDRESS, self._matrix)
            # Raw mode reports five columns of seven row bits. Older keyboard
            # firmware returns a character plus padding instead; reject it.
            if any(value & 0x80 for value in self._matrix):
                raise ValueError("Keyboard does not report held keys")
            # ALT is column 0, row 4; Backspace is column 4, row 3.
            self._alt = bool(self._matrix[0] & (1 << 4))
            mic_down = bool(self._matrix[0] & (1 << 6))
            if self._mic_known and mic_down and not self._mic_down:
                self._mic_pressed = True
            self._mic_down = mic_down
            self._mic_known = True
            return bool(self._matrix[4] & (1 << 3))
        finally:
            self.bus.writeto(_ADDRESS, _KEY_MODE)
            self._key_mode = True

    def _poll(self):
        if self._pending is not None:
            return
        if not self._key_mode:
            # Never let the original driver interpret a matrix byte as text.
            try:
                self.bus.writeto(_ADDRESS, _KEY_MODE)
                self._key_mode = True
            except OSError:
                self.repeat.deadline = None
                return

        key = None
        if self.stream.ioctl(_POLL, _POLLIN) & _POLLIN:
            if self.stream.readinto(self._one):
                key = self._one[0]

        now = time.ticks_ms()
        if key in _D_KEYS or key == _BACKSPACE or self.repeat.due(now) or time.ticks_diff(now, self._last_state) >= 20:
            try:
                self._held = self._read_held()
            except (OSError, ValueError):
                # A missing/failed state read must never sustain deletion.
                self._held = False
                self._alt = False
                self._mic_known = False
                self._mic_pressed = False
            self._last_state = now
        if key in _D_KEYS and self._alt:
            # The keyboard reports ALT only in its key matrix, not in the
            # character stream, so Alt+D arrives as a plain "d".
            self._pending = LAUNCHER_KEY
            return
        if key is None and self._mic_pressed:
            self._mic_pressed = False
            key = MICROPHONE_KEY
        self._pending = self.repeat.update(key, self._held, now)

    def readinto(self, buf):
        try:
            if not len(buf):
                return 0
            self._poll()
            if self._pending is None:
                return None
            buf[0] = self._pending
            self._pending = None
            return 1
        except KeyboardInterrupt:
            # Raising inside a Python dupterm stream detaches it. Return the
            # interrupt byte so MicroPython raises it outside this callback.
            buf[0] = 3
            return 1

    def write(self, data):
        return self.stream.write(data)

    def ioctl(self, request, arg):
        try:
            if request == _POLL:
                if arg & _POLLIN:
                    self._poll()
                return (arg & _POLLOUT) | (_POLLIN if arg & _POLLIN and self._pending is not None else 0)
            return 0
        except KeyboardInterrupt:
            self._pending = 3
            return (arg & _POLLOUT) | (arg & _POLLIN)


def install(env):
    import machine
    import os

    previous = os.dupterm(None)
    stream = previous if previous is not None else env.kvm
    terminal = stream
    try:
        if not isinstance(stream, KeyboardRepeatTerminal):
            # Share the existing legacy I2C0 driver; normal keypresses and
            # terminal replies still pass through the original native KVM.
            bus = machine.I2C(0, sda=machine.Pin(18), scl=machine.Pin(8),
                              freq=400000, timeout=50000)
            terminal = KeyboardRepeatTerminal(stream, bus)
    except (OSError, ValueError):
        # Keep normal typing on keyboards whose firmware predates raw mode.
        pass
    finally:
        os.dupterm(terminal)
    return terminal
