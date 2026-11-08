"""Pulse-controlled, 16-step T-Deck backlight; not a PWM input."""

import machine
import time

_backlight_level = None


def set_brightness(level):
    """Set the T-Deck's pulse-controlled backlight (0 off, 1..16 lit)."""
    global _backlight_level
    level = min(16, max(0, int(level)))
    pin = machine.Pin(42, machine.Pin.OUT)
    if _backlight_level is None or level == 0:
        pin.value(0)
        time.sleep_ms(3)
        _backlight_level = 0
    if level and not _backlight_level:
        pin.value(1)
        time.sleep_us(30)
        _backlight_level = 16
    if level:
        state = machine.disable_irq()
        try:
            for _ in range((_backlight_level - level) % 16):
                pin.value(0)
                time.sleep_us(1)
                pin.value(1)
                time.sleep_us(1)
        finally:
            machine.enable_irq(state)
    _backlight_level = level
    return level

