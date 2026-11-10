"""Test held-key timing, release handling and byte-stream compatibility."""

import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def keyboard():
    path = Path(__file__).resolve().parents[2] / "modules/scripts/lib/keyrepeat.py"
    spec = importlib.util.spec_from_file_location("vtos_keyrepeat_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = SimpleNamespace(now=0)
    module.time = SimpleNamespace(
        ticks_ms=lambda: clock.now,
        ticks_add=lambda now, delta: (now + delta) % 4096,
        ticks_diff=lambda a, b: ((a - b + 2048) % 4096) - 2048,
    )
    return module, clock


class NativeTerminal:
    def __init__(self):
        self.keys = []
        self.output = io.BytesIO()
        self.interrupt = False

    def ioctl(self, request, arg):
        if self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt
        return 1 if self.keys else 0

    def readinto(self, buf):
        buf[0] = self.keys.pop(0)
        return 1

    def write(self, data):
        return self.output.write(data)


class KeyboardBus:
    def __init__(self):
        self.held = False
        self.alt = False
        self.mic = False
        self.fail = False
        self.mode = b"\x04"
        self.invalid = False

    def writeto(self, address, mode):
        assert address == 0x55
        self.mode = mode

    def readfrom_into(self, address, buf):
        assert address == 0x55 and self.mode == b"\x03"
        if self.fail:
            raise OSError("read failed")
        buf[:] = bytes([(16 if self.alt else 0) | (64 if self.mic else 0),
                       0, 0, 0, 8 if self.held else 0])
        if self.invalid:
            buf[:] = b"\x00\xff\xff\xff\xff"


def make_terminal(keyboard):
    module, clock = keyboard
    native = NativeTerminal()
    bus = KeyboardBus()
    return module.KeyboardRepeatTerminal(native, bus), native, bus, clock


def read_key(terminal):
    buf = bytearray(1)
    return buf[0] if terminal.readinto(buf) == 1 else None


def test_backspace_repeats_only_after_delay_and_stops_on_release(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.held = True
    native.keys = [8]
    assert read_key(terminal) == 8
    clock.now = 399
    assert read_key(terminal) is None
    clock.now = 400
    assert read_key(terminal) == 8
    clock.now = 459
    assert read_key(terminal) is None
    clock.now = 460
    assert read_key(terminal) == 8
    bus.held = False
    clock.now = 520
    assert read_key(terminal) is None
    clock.now = 1200
    assert read_key(terminal) is None


def test_tap_and_injected_backspace_do_not_repeat(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    native.keys = [8]
    assert read_key(terminal) == 8
    clock.now = 800
    assert read_key(terminal) is None


def test_other_key_cancels_repeat_even_while_backspace_is_held(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.held = True
    native.keys = [8, ord("a")]
    assert read_key(terminal) == 8
    assert read_key(terminal) == ord("a")
    clock.now = 800
    assert read_key(terminal) is None


def test_no_catchup_burst_after_a_delayed_poll(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.held = True
    native.keys = [8]
    assert read_key(terminal) == 8
    clock.now = 1600
    assert read_key(terminal) == 8
    assert read_key(terminal) is None


def test_failed_state_read_stops_repeat_and_restores_character_mode(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.held = True
    native.keys = [8]
    assert read_key(terminal) == 8
    bus.fail = True
    clock.now = 400
    assert read_key(terminal) is None
    assert bus.mode == b"\x04"
    bus.fail = False
    clock.now = 800
    assert read_key(terminal) is None


def test_older_keyboard_firmware_is_rejected_and_character_mode_restored(keyboard):
    module, _ = keyboard
    bus = KeyboardBus()
    bus.invalid = True
    with pytest.raises(ValueError):
        module.KeyboardRepeatTerminal(NativeTerminal(), bus)
    assert bus.mode == b"\x04"


def test_poll_does_not_consume_a_key_twice(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    native.keys = [ord("a"), ord("b")]
    assert terminal.ioctl(3, 1) == 1
    assert terminal.ioctl(3, 1) == 1
    assert native.keys == [ord("b")]
    assert read_key(terminal) == ord("a")
    assert read_key(terminal) == ord("b")


def test_terminal_queries_and_output_remain_byte_exact_without_delay(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    response = b"\x1b[23;64R\r"
    native.keys = list(response)
    assert bytes(read_key(terminal) for _ in response) == response
    output = b"\x1b[2Jhello\r\n"
    assert terminal.write(output) == len(output)
    assert native.output.getvalue() == output


@pytest.mark.parametrize("via_poll", [False, True])
def test_interrupt_is_returned_as_control_c_without_detaching_stream(keyboard, via_poll):
    terminal, native, bus, clock = make_terminal(keyboard)
    native.interrupt = True
    if via_poll:
        assert terminal.ioctl(3, 1) == 1
    assert read_key(terminal) == 3
    native.keys = [ord("a")]
    assert read_key(terminal) == ord("a")


def test_repeat_deadline_handles_clock_wrap(keyboard):
    module, clock = keyboard
    repeat = module.BackspaceRepeat()
    assert repeat.update(8, True, 3900) == 8
    assert repeat.update(None, True, 203) is None
    assert repeat.update(None, True, 204) == 8


def test_alt_d_becomes_launcher_key(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.alt = True
    native.keys = [ord("d")]
    assert read_key(terminal) == 0x1D
    assert read_key(terminal) is None


def test_plain_d_and_alt_with_other_keys_pass_through(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    native.keys = [ord("d")]
    assert read_key(terminal) == ord("d")
    bus.alt = True
    native.keys = [ord("a")]
    assert read_key(terminal) == ord("a")


def test_failed_state_read_never_launches(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.alt = True
    bus.fail = True
    native.keys = [ord("d")]
    assert read_key(terminal) == ord("d")


def test_microphone_key_emits_once_per_press(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.mic = True
    clock.now = 20
    assert read_key(terminal) == 0x1C
    clock.now = 100
    assert read_key(terminal) is None
    bus.mic = False
    clock.now = 120
    assert read_key(terminal) is None
    bus.mic = True
    clock.now = 140
    assert read_key(terminal) == 0x1C


def test_microphone_does_not_trigger_after_failed_state_read(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.fail = True
    clock.now = 20
    assert read_key(terminal) is None
    bus.fail = False
    bus.mic = True
    clock.now = 40
    assert read_key(terminal) is None


def test_microphone_event_does_not_replace_a_character(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.mic = True
    native.keys = [ord("a")]
    clock.now = 20
    assert read_key(terminal) == ord("a")
    assert read_key(terminal) == 0x1C


def test_alt_v_calls_paste_hook_and_swallows_the_key(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    calls = []
    terminal.on_paste = lambda: calls.append(1)
    bus.alt = True
    native.keys = [ord("v")]
    assert read_key(terminal) is None
    assert calls == [1]


def test_plain_v_and_alt_v_without_hook_pass_through(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    terminal.on_paste = lambda: pytest.fail("must not paste")
    native.keys = [ord("v")]
    assert read_key(terminal) == ord("v")
    terminal.on_paste = None
    bus.alt = True
    native.keys = [ord("v")]
    assert read_key(terminal) == ord("v")


def test_alt_v_pastes_once_per_alt_press(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    calls = []
    terminal.on_paste = lambda: calls.append(1)
    bus.alt = True
    native.keys = [ord("v")]
    assert read_key(terminal) is None
    # a "v" inside the pasted text arrives while Alt is still held: it types
    native.keys = [ord("v")]
    assert read_key(terminal) == ord("v")
    assert calls == [1]
    # releasing and pressing Alt again re-arms it
    bus.alt = False
    native.keys = [ord("x")]
    clock.now = 100
    assert read_key(terminal) == ord("x")
    bus.alt = True
    clock.now = 200
    native.keys = [ord("v")]
    assert read_key(terminal) is None
    assert calls == [1, 1]


def test_alt_m_becomes_mode_key_and_plain_m_passes_through(keyboard):
    terminal, native, bus, clock = make_terminal(keyboard)
    bus.alt = True
    native.keys = [ord("m")]
    assert read_key(terminal) == 0x1E
    assert read_key(terminal) is None
    bus.alt = False
    native.keys = [ord("m")]
    assert read_key(terminal) == ord("m")
