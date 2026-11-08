"""Check trackball routing without a physical GPIO device."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def route(horizontal=0, vertical=0, click=False, ssh=False):
    tracker = SimpleNamespace(
        get_scroll_horiz=lambda: horizontal,
        get_scroll_vert=lambda: vertical,
        get_click=lambda: click,
    )
    path = Path(__file__).resolve().parents[2] / "modules/scripts/lib/trackball.py"
    spec = importlib.util.spec_from_file_location("vtos_trackball_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"tdeck_trk": tracker}):
        spec.loader.exec_module(module)
    keys = []
    scrolled = []
    env = SimpleNamespace(
        kvm=SimpleNamespace(inject=keys.append),
        term=SimpleNamespace(
            scrollup=lambda: scrolled.append("up"),
            scrolldown=lambda: scrolled.append("down"),
        ),
    )
    if ssh:
        env.ssh_scrollback = True
    module.poll(env)
    return keys, scrolled


@pytest.mark.parametrize("delta,direction", [(2, "up"), (-2, "down")])
def test_ssh_scrolls_display_without_changing_remote_prompt(delta, direction):
    keys, scrolled = route(vertical=delta, ssh=True)
    assert keys == []
    assert scrolled == [direction]


@pytest.mark.parametrize("delta,key", [(2, "\x1b[A"), (-2, "\x1b[B")])
def test_local_apps_keep_vertical_arrow_navigation(delta, key):
    keys, scrolled = route(vertical=delta)
    assert keys == [key]
    assert scrolled == []


@pytest.mark.parametrize("ssh", [False, True])
@pytest.mark.parametrize("delta,key", [(-2, "\x1b[D"), (2, "\x1b[C")])
def test_horizontal_navigation_stays_unchanged(ssh, delta, key):
    keys, scrolled = route(horizontal=delta, ssh=ssh)
    assert keys == [key]
    assert scrolled == []


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_small_movements_keep_existing_threshold(delta):
    assert route(horizontal=delta, vertical=delta, ssh=True) == ([], [])


def test_short_click_still_sends_escape():
    assert route(click=True, ssh=True) == (["\x1b"], [])
