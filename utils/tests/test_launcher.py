"""Check launcher filtering, layout and key handling without hardware."""

import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[2] / "modules/scripts/bin/launcher.py"
    spec = importlib.util.spec_from_file_location("vtos_launcher_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


APPS = {"chess": 1, "ssh": 1, "sshd": 1, "fm": 1, "mines": 1, "ping": 1}


def names(entries):
    return [e[0] for e in entries]


def test_favs_come_first_and_show_target(launcher):
    entries = launcher.build_entries(APPS, {"home": "ssh 1.2.3.4 22 me"})
    assert entries[0] == ("home", "-> ssh 1.2.3.4 22 me")
    assert names(entries)[1:] == sorted(APPS)


def test_filter_ranks_prefix_then_substring_then_subsequence(launcher):
    entries = [("sshd", ""), ("mines", ""), ("ssh", ""), ("chess", "play chess")]
    assert names(launcher.filter_entries(entries, "ss"))[:2] == ["sshd", "ssh"]
    assert names(launcher.filter_entries(entries, "es")) == ["mines", "chess"]
    assert names(launcher.filter_entries(entries, "mns")) == ["mines"]
    assert names(launcher.filter_entries(entries, "zzz")) == []
    assert names(launcher.filter_entries(entries, "")) == names(entries)


def test_filter_matches_description_and_ignores_case(launcher):
    entries = [("fm", "file manager"), ("ping", "ping a host")]
    assert names(launcher.filter_entries(entries, "MANAGER")) == ["fm"]


@pytest.mark.parametrize("cols,rows", [(40, 16), (64, 23), (20, 8)])
def test_panel_fits_the_screen(launcher, cols, rows):
    entries = [("app%d" % i, "desc") for i in range(30)]
    left, top, width, visible = launcher.geometry(cols, rows, len(entries))
    assert left >= 1 and left + width - 1 <= cols
    assert top >= 1 and top + visible <= rows


def test_render_marks_selection_and_count(launcher):
    entries = [("chess", "play chess"), ("ssh", "secure shell")]
    text = launcher.render(40, 16, "s", entries, 1, 9)
    assert "2/9" in text
    assert "> " in text and "ssh" in text
    assert "no matches" not in text
    assert "no matches" in launcher.render(40, 16, "q", [], 0, 9)


def run_pick(launcher, keys, entries):
    out = io.StringIO()
    old = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(keys), out
    launcher._read_key.__globals__["select"] = None
    try:
        env = SimpleNamespace(cols=40, rows=16)
        return launcher.pick(env, entries), out.getvalue()
    finally:
        sys.stdin, sys.stdout = old


def test_pick_types_to_filter_and_enter_runs(launcher):
    entries = launcher.build_entries(APPS)
    result, out = run_pick(launcher, "pin\r", entries)
    assert result == "ping"
    assert out.endswith("\x1b[?25h")  # cursor restored


def test_pick_arrows_wrap_and_backspace_refilters(launcher):
    entries = [("a1", ""), ("a2", ""), ("b1", "")]
    assert run_pick(launcher, "\x1b[B\r", entries)[0] == "a2"
    assert run_pick(launcher, "\x1b[A\r", entries)[0] == "b1"
    # Backspace drops one filter character, then the rest refilters.
    assert run_pick(launcher, "zz\x7f\x7fb\r", entries)[0] == "b1"


def test_pick_dismiss_keys_return_none(launcher):
    entries = [("a1", "")]
    assert run_pick(launcher, "\x1d", entries)[0] is None
    assert run_pick(launcher, "\x1b", entries)[0] is None
    assert run_pick(launcher, "zzz\r", entries)[0] is None
