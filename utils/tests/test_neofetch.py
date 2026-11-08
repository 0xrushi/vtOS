"""Check neofetch formatting and layout without a device."""

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def nf():
    path = Path(__file__).resolve().parents[2] / "modules/scripts/bin/neofetch.py"
    spec = importlib.util.spec_from_file_location("vtos_neofetch_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plain(line):
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


@pytest.mark.parametrize("n,text", [
    (0, "0B"), (1023, "1023B"), (2048, "2K"), (3_400_000, "3.2M"),
    (8 * 1024 * 1024, "8.0M"), (5 * 1024 ** 3 // 2, "2.5G"),
])
def test_human_sizes(nf, n, text):
    assert nf.human(n) == text


def test_uptime_and_usage_text(nf):
    assert nf.uptime_text(125) == "2m 5s"
    assert nf.uptime_text(3725) == "1h 2m 5s"
    assert nf.uptime_text(90000) == "1d 1h 0m"
    assert nf.usage_text(1024, 4096) == "1K/4K 25%"
    assert nf.usage_text(0, 0) == "n/a"


@pytest.mark.parametrize("cols", [40, 64])
def test_render_fits_columns_and_keeps_logo_aligned(nf, cols):
    info = [("OS", "vtOS (MicroPython 1.27.0-with-a-very-long-version-string)"),
            ("Net", "192.168.100.200 (SomeVeryLongNetworkNameHere)")]
    lines = [plain(l) for l in nf.render(info, cols)]
    assert all(len(l) <= cols for l in lines)
    assert lines[0].startswith("  .---------.")
    assert lines[0][nf.LOGO_WIDTH:].startswith("vtos@tdeck")
    assert any(l[nf.LOGO_WIDTH:].startswith("OS") for l in lines)


def test_narrow_screen_drops_logo(nf):
    lines = [plain(l) for l in nf.render([("OS", "x")], 30)]
    assert lines[0].startswith("vtos@tdeck")


def test_collect_survives_missing_hardware(nf):
    env = SimpleNamespace(cols=40, rows=16, font_name="terminus_mpy_14",
                          sts=SimpleNamespace(start_ticks=0), sd=None,
                          shell=SimpleNamespace(apps={"a": 1, "b": 2}))
    # CPython lacks machine/esp32/ticks_ms; collect() must not raise.
    info = dict(nf.collect(SimpleNamespace(cols=40, rows=16, font_name="f")))
    assert info["Term"] == "40x16 chars"
    assert info["Host"] == "T-Deck" or info["Host"]
    assert info["Heap"] == "n/a"  # no MicroPython gc on CPython
    assert "Cmds" not in info
    assert dict(nf.collect(env))["Cmds"] == "2 apps"


def test_avoids_str_methods_missing_from_micropython(nf):
    source = Path(nf.__file__).read_text()
    for name in (".ljust(", ".rjust(", ".center(", ".zfill("):
        assert name not in source
