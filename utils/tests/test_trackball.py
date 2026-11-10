"""Check trackball routing without a physical GPIO device."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _cfg():
    path = Path(__file__).resolve().parents[2] / "modules/scripts/lib/trackcfg.py"
    spec = importlib.util.spec_from_file_location("trackcfg", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._values = module._defaults()
    return module


def route(horizontal=0, vertical=0, click=False, ssh=False, move=None):
    tracker = SimpleNamespace(
        get_scroll_horiz=lambda: horizontal,
        get_scroll_vert=lambda: vertical,
        get_click=lambda: click,
    )
    path = Path(__file__).resolve().parents[2] / "modules/scripts/lib/trackball.py"
    spec = importlib.util.spec_from_file_location("vtos_trackball_test", path)
    module = importlib.util.module_from_spec(spec)
    cfg_path = Path(__file__).resolve().parents[2] / "modules/scripts/lib/trackcfg.py"
    cfg_spec = importlib.util.spec_from_file_location("trackcfg", cfg_path)
    cfg = importlib.util.module_from_spec(cfg_spec)
    cfg_spec.loader.exec_module(cfg)
    cfg._values = cfg._defaults()
    if move is not None:
        cfg._values["move"] = move
    with patch.dict("sys.modules", {"tdeck_trk": tracker, "trackcfg": cfg}):
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


# --- an app that takes the raw ball (the VNC client) -------------------------

def route_pointer(horizontal=0, vertical=0, click=False):
    tracker = SimpleNamespace(
        get_scroll_horiz=lambda: horizontal,
        get_scroll_vert=lambda: vertical,
        get_click=lambda: click,
    )
    path = Path(__file__).resolve().parents[2] / "modules/scripts/lib/trackball.py"
    spec = importlib.util.spec_from_file_location("vtos_trackball_pointer_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"tdeck_trk": tracker, "trackcfg": _cfg()}):
        spec.loader.exec_module(module)
    keys, calls = [], []
    env = SimpleNamespace(
        kvm=SimpleNamespace(inject=keys.append),
        term=SimpleNamespace(scrollup=lambda: keys.append("scrollup"),
                             scrolldown=lambda: keys.append("scrolldown")),
        pointer=lambda dx, dy, click: calls.append((dx, dy, click)),
    )
    module.poll(env)
    return keys, calls


def test_pointer_hook_gets_raw_deltas_with_down_positive():
    keys, calls = route_pointer(horizontal=3, vertical=5)       # right 3, up 5
    assert calls == [(3, -5, False)]
    assert keys == []


def test_pointer_hook_sees_single_pulses_the_arrow_threshold_would_drop():
    assert route_pointer(horizontal=-1, vertical=-1)[1] == [(-1, 1, False)]


def test_pointer_hook_gets_the_click_and_no_escape_is_injected():
    keys, calls = route_pointer(click=True)
    assert calls == [(0, 0, True)]
    assert keys == []


def test_pointer_hook_is_called_every_tick_even_when_idle():
    assert route_pointer()[1] == [(0, 0, False)]       # the click must be read every tick: long press -> KeyboardInterrupt


def test_no_hook_set_means_the_old_behaviour(monkeypatch):
    # env.pointer exists but is None (the Environment default)
    tracker = SimpleNamespace(get_scroll_horiz=lambda: 2, get_scroll_vert=lambda: 0,
                              get_click=lambda: True)
    path = Path(__file__).resolve().parents[2] / "modules/scripts/lib/trackball.py"
    spec = importlib.util.spec_from_file_location("vtos_trackball_none_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"tdeck_trk": tracker, "trackcfg": _cfg()}):
        spec.loader.exec_module(module)
    keys = []
    module.poll(SimpleNamespace(kvm=SimpleNamespace(inject=keys.append), pointer=None))
    assert keys == ["\x1b[C", "\x1b"]


@pytest.mark.parametrize("move,delta,keys", [
    (2, 1, []), (2, 2, ["\x1b[D"]),             # default: two pulses to move
    (1, 1, ["\x1b[D"]),                          # most sensitive: one pulse
    (4, 3, []), (4, 4, ["\x1b[D"]),
])
def test_move_setting_sets_how_many_pulses_the_shell_needs(move, delta, keys):
    assert route(horizontal=-delta, move=move)[0] == keys
