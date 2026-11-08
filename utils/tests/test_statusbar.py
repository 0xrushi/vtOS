"""Check the colored status bar keeps its width and recolors by state."""

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


class Term:
    def __init__(self):
        self.bar = None

    def top_bar(self, data):
        self.bar = bytes(data)


def load(cols=40, battery=80, wifi=False, audio=None, ble=None):
    path = Path(__file__).resolve().parents[2] / "modules/scripts/sys/statusbar.py"
    spec = importlib.util.spec_from_file_location("vtos_statusbar_test", path)
    mod = importlib.util.module_from_spec(spec)
    clock = SimpleNamespace(now=0)
    fakes = {
        "network": SimpleNamespace(
            STA_IF=0,
            WLAN=lambda _: SimpleNamespace(isconnected=lambda: wifi)),
        "hardware": SimpleNamespace(
            init_bat=lambda: None, bat_value=lambda pin: battery),
    }
    with patch.dict("sys.modules", fakes):
        spec.loader.exec_module(mod)
    mod.time = SimpleNamespace(
        ticks_ms=lambda: clock.now,
        ticks_diff=lambda a, b: a - b,
        ticks_add=lambda a, b: a + b,
    )
    mod.gc = SimpleNamespace(mem_free=lambda: 3_200_000)
    env = SimpleNamespace(cols=cols, term=Term(), sd_busy=False, audio=audio, ble=ble)
    return mod, mod.StatusBar(env), env, clock


def visible(bar):
    """Text with colour codes removed; wide icons count as two columns."""
    text = re.sub(rb"\x1b\[[0-9;]*m", b"", bar.rstrip(b"\x00")).decode("utf-8")
    return text, sum(2 if ord(c) > 0xDFFF else 1 for c in text)


@pytest.mark.parametrize("cols", [40, 64])
def test_bar_fills_exactly_the_screen_width(cols):
    mod, bar, env, clock = load(cols)
    clock.now = 125_000
    bar.refresh()
    text, width = visible(env.term.bar)
    assert width == cols
    assert "02m05" in text and "3.0M" in text


def test_battery_color_follows_level():
    for level, code in [(90, b"114"), (35, b"221"), (10, b"203")]:
        mod, bar, env, clock = load(battery=level)
        bar.refresh()
        assert env.term.bar[bar.off_bat_c:bar.off_bat_c + 3] == code


def test_wifi_and_ble_recolor_when_active():
    mod, bar, env, clock = load(wifi=False)
    bar.refresh()
    assert env.term.bar[bar.off_wifi_c:bar.off_wifi_c + 3] == b"244"
    assert env.term.bar[bar.off_ble_c:bar.off_ble_c + 3] == b"244"
    mod, bar, env, clock = load(wifi=True, ble=object())
    bar.refresh()
    assert env.term.bar[bar.off_wifi_c:bar.off_wifi_c + 3] == b"114"
    assert env.term.bar[bar.off_ble_c:bar.off_ble_c + 3] == b"114"
    text, _ = visible(env.term.bar)
    assert text.count("ON ") == 2


def test_volume_colored_only_when_audio_exists():
    mod, bar, env, clock = load(audio=SimpleNamespace(volume=lambda: 70))
    bar.refresh()
    assert env.term.bar[bar.off_vol_c:bar.off_vol_c + 3] == b"114"
    assert " 70" in visible(env.term.bar)[0]


def test_notification_replaces_bar_then_reverts():
    mod, bar, env, clock = load()
    bar.notify("sshd: connected")
    text, width = visible(env.term.bar)
    assert "sshd: connected" in text and width == 40
    clock.now = 6000
    bar.refresh()
    assert "sshd" not in visible(env.term.bar)[0]
