"""Trackball sensitivity settings (lib/trackcfg.py) and the `ball` command."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    module = _load("trackcfg", ROOT / "modules/scripts/lib/trackcfg.py")
    module.PATH = str(tmp_path / ".trackball")
    monkeypatch.setitem(sys.modules, "trackcfg", module)
    return module


@pytest.fixture
def ball(cfg):
    return _load("vtos_ball", ROOT / "modules/scripts/bin/ball.py")


def test_defaults_when_there_is_no_file(cfg):
    assert (cfg.get("move"), cfg.get("gain")) == (2, 4)


def test_set_saves_and_a_fresh_load_sees_it(cfg):
    cfg.set("gain", "9")
    assert cfg.get("gain") == 9
    cfg._values = None
    assert (cfg.get("gain"), cfg.get("move")) == (9, 2)


def test_a_damaged_file_never_breaks_the_ball(cfg):
    Path(cfg.PATH).write_text("gain=banana\nmove=99\nbogus=1\nnonsense\ngain=7\n")
    cfg.load()
    assert (cfg.get("gain"), cfg.get("move")) == (7, 2)       # bad lines ignored


@pytest.mark.parametrize("name,value", [("gain", "0"), ("gain", "17"), ("move", "x"), ("speed", "3")])
def test_bad_values_are_refused_and_change_nothing(cfg, name, value):
    with pytest.raises(ValueError):
        cfg.set(name, value)
    assert not Path(cfg.PATH).exists()


def test_reset_restores_defaults(cfg):
    cfg.set("move", 1)
    cfg.reset()
    assert cfg.get("move") == 2


def test_ball_command_shows_sets_and_reports_errors(ball, cfg, capsys):
    ball.main(None, [])
    assert "gain  4" in capsys.readouterr().out
    ball.main(None, ["gain", "10"])
    assert "gain  10" in capsys.readouterr().out and cfg.get("gain") == 10
    ball.main(None, ["gain", "99"])
    assert "ball: gain must be 1 to 16" in capsys.readouterr().out
    assert cfg.get("gain") == 10
    ball.main(None, ["reset"])
    assert cfg.get("gain") == 4
