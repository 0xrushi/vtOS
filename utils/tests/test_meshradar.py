"""Meshchat radar: projection, range, selection, drawing at every grid size."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "modules/scripts/lib"))

import meshback  # noqa: E402
import meshradar  # noqa: E402
import meshui  # noqa: E402

_spec = importlib.util.spec_from_file_location("mesh_preview", ROOT / "utils/mesh_preview.py")
preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preview)

SIZES = [(40, 16), (53, 19), (64, 29), (32, 12)]
T0 = 1_800_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def rig():
    clock = Clock()
    app = meshui.App(meshback.SimBackend(clock=clock), clock())
    app.anim_clock = lambda: 0
    clock.t += 1
    app.poll(clock.t)
    app.radar_open()
    return app, clock


def screen(app, clock, cols=40, rows=16):
    plain, _ = preview.emulate(app.render(cols, rows, clock.t), cols, rows)
    return plain


def test_menu_and_command_open_the_radar():
    clock = Clock()
    app = meshui.App(meshback.SimBackend(clock=clock), clock())
    app.key("ESC", clock.t)
    app.key("DOWN", clock.t)
    assert meshui.MENU_ITEMS[app.sel] == "Radar"
    app.key("\r", clock.t)
    assert app.mode == "radar"
    app.mode = "chat"
    for ch in "/radar":
        app.key(ch, clock.t)
    app.key("\r", clock.t)
    assert app.mode == "radar"


def test_points_use_real_bearing_and_distance_and_skip_unplaced_nodes(rig):
    app, _ = rig
    pts, nopos, mine = app.radar_points()
    names = [p[0].name for p in pts]
    assert names == ["Bob", "Alice", "Ridge-R"]                    # nearest first
    assert nopos == 2                                              # Sam and Cabin shared no position
    brg = {p[0].name: (p[1], p[2]) for p in pts}
    assert brg["Bob"][0] == pytest.approx(0.47, abs=0.05) and 0 <= brg["Bob"][1] < 45
    assert 30 < brg["Alice"][1] < 60                               # north-east
    assert 270 < brg["Ridge-R"][1] < 340                           # north-west
    assert mine == (37.7749, -122.4194)


def test_ignored_nodes_leave_the_radar(rig):
    app, _ = rig
    next(n for n in app.nodes if n.name == "Bob").ignored = True
    assert [p[0].name for p in app.radar_points()[0]] == ["Alice", "Ridge-R"]


@pytest.mark.parametrize("cols,rows", SIZES)
def test_radar_draws_at_every_grid_size(rig, cols, rows):
    app, clock = rig
    plain = screen(app, clock, cols, rows)
    assert "Radar (3)" in plain[1] and "auto 10 km" in plain[1]
    body = "\n".join(plain[3:rows - 2])
    assert "+" in body and "N" in body and "@" in body          # centre, north mark, selected node
    assert "o" in body                                          # other nodes
    assert "Bob" in plain[rows - 2] or "BOB" in plain[rows - 2]   # selected node details
    assert "no pos" in plain[rows - 2] or cols < 53


def test_north_is_up_and_rings_are_round(rig):
    app, clock = rig
    cols, rows = 64, 29
    plain = screen(app, clock, cols, rows)
    n_row = next(i for i, line in enumerate(plain) if i >= 3 and "N" in line[:cols] and "." in "".join(plain[i + 1]))
    centre = next(i for i, line in enumerate(plain) if "+" in line)
    assert n_row < centre                                       # north above the centre
    # circle: horizontal radius is the vertical radius times the cell aspect
    ys = [i for i in range(3, rows - 2) if "." in plain[i]]
    xs = [x for i in ys for x, ch in enumerate(plain[i]) if ch == "."]
    v_radius = (max(ys) - min(ys)) / 2.0
    h_radius = (max(xs) - min(xs)) / 2.0
    assert h_radius / v_radius == pytest.approx(meshradar.cell_aspect(cols, rows), rel=0.2)


def test_bearing_places_nodes_on_the_correct_side(rig):
    app, clock = rig
    cols, rows = 64, 29
    app.radar_range = 10
    app.radar_sel = 99 % 3
    plain = screen(app, clock, cols, rows)
    centre_row = next(i for i, line in enumerate(plain) if "+" in line)
    centre_col = plain[centre_row].index("+")
    labels = {}
    for i in range(3, rows - 2):
        for name in ("BOB", "ALIC", "RIDG"):
            if name in plain[i]:
                labels[name] = (i, plain[i].index(name))
    assert labels["ALIC"][1] > centre_col and labels["ALIC"][0] < centre_row         # NE
    assert labels["RIDG"][1] < centre_col and labels["RIDG"][0] < centre_row         # NW


def test_selection_moves_and_enter_opens_the_node_then_returns(rig):
    app, clock = rig
    plain = screen(app, clock)
    assert "Bob" in plain[14]
    app.key("RIGHT", clock.t)
    assert "Alice" in screen(app, clock)[14]
    app.key("LEFT", clock.t)
    app.key("LEFT", clock.t)
    assert "Ridge-R" in screen(app, clock)[14]                    # wraps around
    app.key("\r", clock.t)
    assert app.mode == "node" and app.node_view.name == "Ridge-R"
    app.key("ESC", clock.t)
    assert app.mode == "radar"                                    # back where we came from
    app.key("ESC", clock.t)
    assert app.mode == "menu"


def test_node_list_still_returns_to_the_list():
    clock = Clock()
    app = meshui.App(meshback.SimBackend(clock=clock), clock())
    app._open_list("nodes")
    app.key("i", clock.t)
    assert app.mode == "node"
    app.key("ESC", clock.t)
    assert app.mode == "nodes"


def test_range_zoom_and_auto(rig):
    app, clock = rig
    app.key("UP", clock.t)
    assert app.radar_range == 5 and "range 5 km" in screen(app, clock)[1]
    # at 5 km Ridge-R (6.5 km away) falls off the plot but stays selectable
    assert "Ridge" not in "\n".join(screen(app, clock, 64, 29)[3:27])
    for _ in range(30):
        app.key("UP", clock.t)
    assert app.radar_range == meshradar.RANGES_KM[0]
    app.key("a", clock.t)
    assert app.radar_range is None and "auto 10 km" in screen(app, clock)[1]
    for _ in range(30):
        app.key("DOWN", clock.t)
    assert app.radar_range == meshradar.RANGES_KM[-1]


def test_nice_range():
    assert meshradar.nice_range(0.04) == 0.1
    assert meshradar.nice_range(0.47) == 0.5
    assert meshradar.nice_range(0.5) == 1                          # margin: 0.5 km would sit on the rim
    assert meshradar.nice_range(6.5) == 10
    assert meshradar.nice_range(1e9) == meshradar.RANGES_KM[-1]


def test_sweep_line_moves_with_the_clock(rig):
    app, clock = rig
    frames = set()
    for ms in range(0, meshradar.SWEEP_MS, 750):
        app.anim_clock = lambda ms=ms: ms
        frames.add(app.render(64, 29, clock.t))
    assert len(frames) > 4
    assert app.animating()
    app.mode = "chat"
    assert not app.animating()


def test_without_a_position_the_radar_explains_itself(rig):
    app, clock = rig
    app.backend.my_position = lambda: None
    plain = screen(app, clock)
    assert "needs your position" in "\n".join(plain)
    app.key("RIGHT", clock.t)                                      # nothing to select, must not crash
    app.key("\r", clock.t)
    assert app.mode == "radar"


def test_no_positioned_nodes(rig):
    app, clock = rig
    for n in app.nodes:
        n.lat = None
    plain = screen(app, clock)
    assert "No node with a position" in plain[14]
    app.key("\r", clock.t)
    assert app.mode == "radar"


def test_a_very_distant_node_still_fits(rig):
    app, clock = rig
    next(n for n in app.nodes if n.name == "Ridge-R").lat = -33.9
    next(n for n in app.nodes if n.name == "Ridge-R").lon = 151.2     # Sydney
    for cols, rows in SIZES:
        screen(app, clock, cols, rows)
    assert "auto 20000" not in screen(app, clock)[1]
