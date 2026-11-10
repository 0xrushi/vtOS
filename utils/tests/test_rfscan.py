"""Sub-GHz spectrum view: sweep state, scaling, rendering at every grid size, key handling."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "modules/scripts/lib"))

import mtconfig  # noqa: E402
import rfscan  # noqa: E402

_spec = importlib.util.spec_from_file_location("mesh_preview", ROOT / "utils/mesh_preview.py")
preview = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preview)

SIZES = [(40, 16), (53, 19), (64, 29), (32, 12)]


class Rng:
    """Deterministic stand-in for random.random()."""

    def __init__(self):
        self.i = 0

    def __call__(self):
        self.i += 1
        return ((self.i * 37) % 100) / 100.0


def make_scan(cols=40, **kw):
    scan = rfscan.Scan(kw.get("center", 868.0), kw.get("span", 30), kw.get("rbw", 125.0),
                       cols - rfscan.LEFT_MARGIN)
    return scan


def test_default_view_follows_the_region():
    cfg = mtconfig.Settings()
    cfg.region = "US"
    assert rfscan.default_view(cfg) == (915.0, 50.0)
    cfg.region = "EU_868"
    centre, span = rfscan.default_view(cfg)
    assert centre == pytest.approx(869.5, abs=0.1) and span == 30.0
    assert rfscan.default_view(None) == (868.0, 50.0)


def test_points_are_capped_and_cover_the_span():
    scan = make_scan(span=200, rbw=62.5)
    assert scan.points <= rfscan.MAX_POINTS
    f = scan.freqs()
    assert f[0] == pytest.approx(scan.start) and f[-1] == pytest.approx(scan.end, abs=scan.step)
    narrow = make_scan(span=5, rbw=125.0)
    assert narrow.step == pytest.approx(0.125) and narrow.points == 41


def test_pan_and_zoom_stay_inside_the_chip_range():
    scan = make_scan(center=155.0, span=5)
    assert scan.start >= rfscan.MIN_MHZ
    scan.pan(-1)
    assert scan.start >= rfscan.MIN_MHZ
    scan = make_scan(center=955.0, span=5)
    scan.pan(1)
    assert scan.end <= rfscan.MAX_MHZ
    scan = make_scan(span=30)
    scan.zoom(-1)
    assert scan.span == 20
    for _ in range(20):
        scan.zoom(-1)
    assert scan.span == rfscan.SPANS[0]
    for _ in range(20):
        scan.zoom(1)
    assert scan.span == rfscan.SPANS[-1]


def test_add_bins_finds_the_peak_and_holds():
    scan = make_scan(cols=45, span=30)               # 40 plot columns, 31+ points
    levels = [-118.0] * scan.points
    spike = scan.points // 2
    levels[spike] = -70.0
    scan.add(levels)
    assert len(scan.cur) == 40 and max(scan.cur) == -70.0
    assert scan.peak[1] == -70.0
    assert scan.peak[0] == pytest.approx(scan.start + spike * scan.step)
    quiet = [-118.0] * scan.points
    scan.add(quiet)
    assert max(scan.cur) == -118.0 and max(scan.hold) == -70.0       # hold keeps the spike
    scan.key("c")
    assert max(scan.hold) == -118.0
    for _ in range(rfscan.HISTORY + 5):
        scan.add(quiet)
    assert len(scan.rows) == rfscan.HISTORY and len(scan.rows[0]) == 40


def test_add_with_fewer_points_than_columns_still_fills_every_column():
    scan = make_scan(cols=69, span=2, rbw=500.0)     # 64 columns, a handful of points
    scan.add([-100.0 + i for i in range(scan.points)])
    assert len(scan.cur) == 64 and all(v <= 0 for v in scan.cur)


def test_scale_tracks_the_data_with_a_minimum_range():
    scan = make_scan()
    scan.add([-117.0] * scan.points)
    assert scan.hi - scan.lo >= rfscan.MIN_RANGE_DB
    assert scan.lo <= -117 <= scan.hi and scan.lo % 10 == 0
    scan.add([-117.0] * (scan.points - 1) + [-35.0])
    assert scan.hi >= -35


def test_keys():
    scan = make_scan()
    assert scan.key("q") == "quit" and scan.key("ESC") == "quit"
    assert scan.key("b") == "rbw" and scan.rbw == 250.0
    for _ in range(4):
        scan.key("b")
    assert scan.rbw == 250.0                                     # wraps around the four widths
    assert scan.key("p") is None and scan.paused
    scan.key("p")
    assert not scan.paused
    scan.key("h")
    assert not scan.hold_on
    centre = scan.center
    scan.key("RIGHT")
    assert scan.center > centre


def test_view_change_clears_old_data():
    scan = make_scan()
    scan.add([-100.0] * scan.points)
    scan.key("RIGHT")
    assert scan.cur is None and scan.rows == [] and scan.peak is None


@pytest.mark.parametrize("cols,rows", SIZES)
def test_render_fits_every_grid_size(cols, rows):
    sim = rfscan.SimRadio(Rng())
    scan = make_scan(cols=cols, center=868.0, span=30)
    scan.add(sim.sweep(scan.freqs()))
    for _ in range(6):
        scan.add(sim.sweep(scan.freqs()))
    plain, _ = preview.emulate(rfscan.render(scan, cols, rows), cols, rows)
    assert "Spectrum" in plain[0] and "peak" in plain[0]
    assert "RBW 125k" in plain[1]
    assert "868.0 MHz" in "".join(plain) and "853.0" in "".join(plain) and "883.0" in "".join(plain)
    assert "pan" in plain[-1] or "<>" in plain[-1]


@pytest.mark.parametrize("cols,rows", SIZES)
def test_render_before_the_first_sweep(cols, rows):
    scan = make_scan(cols=cols)
    plain, _ = preview.emulate(rfscan.render(scan, cols, rows), cols, rows)
    assert "peak --" in plain[0]


def test_trace_height_follows_the_level():
    scan = make_scan(cols=45)
    levels = [-120.0] * scan.points
    levels[scan.points // 2] = -60.0
    scan.add(levels)
    cols, rows = 45, 16
    _, ansi = preview.emulate(rfscan.render(scan, cols, rows), cols, rows)
    top_edge = "48;5;%d" % rfscan.TRACE_TOP
    # the spike reaches the top trace row; a flat sweep leaves that row empty
    assert top_edge in ansi[2]
    flat = make_scan(cols=45)
    flat.add([-120.0] * flat.points)
    _, flat_ansi = preview.emulate(rfscan.render(flat, cols, rows), cols, rows)
    assert top_edge not in flat_ansi[2]
    assert ansi[2].count(top_edge) == 1


def test_sim_radio_shows_signals_above_the_noise_floor():
    sim = rfscan.SimRadio(lambda: 0.0)               # every signal on
    scan = make_scan(span=5, center=868.2)
    levels = sim.sweep(scan.freqs())
    assert max(levels) > -90 and min(levels) < -110


class FakeRadio:
    def __init__(self, fail_tcxo=(), errors=0):
        self.fail_tcxo = fail_tcxo
        self.errors = errors
        self.calls = []
        self.boost = None

    def begin(self, **kw):
        self.calls.append(kw)
        if kw["tcxo"] in self.fail_tcxo:
            raise RuntimeError("SX126x not found")

    def get_device_errors(self):
        return self.errors

    def rx_boost(self, on):
        self.boost = on


def test_start_radio_uses_the_remembered_tcxo_and_boosts_receive():
    cfg = mtconfig.Settings()
    cfg.tcxo_found = 1.8
    radio = FakeRadio()
    assert rfscan.start_radio(radio, cfg, 868.0, 62.5) == (True, "ok")
    kw = radio.calls[0]
    assert kw["tcxo"] == 1.8 and kw["bw"] == 62.5 and kw["freq"] == 868.0 and kw["power"] == 0
    assert radio.boost is True


def test_start_radio_tries_tcxo_voltages_and_reports_failure():
    cfg = mtconfig.Settings()
    cfg.tcxo = None
    cfg.tcxo_found = None
    radio = FakeRadio(fail_tcxo=(0.0, 1.8))
    assert rfscan.start_radio(radio, cfg, 868.0, 125.0)[0]
    assert [c["tcxo"] for c in radio.calls] == [0.0, 1.8, 2.8]
    bad = FakeRadio(fail_tcxo=(0.0, 1.8, 2.8, 2.4, 3.3))
    ok, msg = rfscan.start_radio(bad, cfg, 868.0, 125.0)
    assert not ok and "not found" in msg
    ok, msg = rfscan.start_radio(FakeRadio(errors=0x20), cfg, 868.0, 125.0)
    assert not ok and "0x0020" in msg


def test_start_radio_reports_the_native_module_cannot_do_it():
    class Native:
        def begin(self, freq, bw, sf, cr, sync_word, power):
            pass
    ok, msg = rfscan.start_radio(Native(), None, 868.0, 125.0)
    assert not ok and "Python SX126x driver" in msg
