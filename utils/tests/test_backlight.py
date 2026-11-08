"""Exercise the board's pulse-counting protocol against a simulated driver IC."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def test_backlight_reaches_requested_step_including_off_and_wakeup():
    chip = SimpleNamespace(level=7, pin=1, off=False)
    class Pin:
        OUT=1
        def __init__(self,*args): pass
        def value(self,value):
            if value and not chip.pin:
                chip.level = 16 if chip.off else (chip.level-2)%16+1
                chip.off = False
            chip.pin=value
    def reset_delay(ms):
        assert chip.pin==0
        chip.off=True; chip.level=0
    machine=SimpleNamespace(Pin=Pin, disable_irq=lambda:0,enable_irq=lambda _:None)
    clock=SimpleNamespace(sleep_ms=reset_delay,sleep_us=lambda _:None)
    path=Path(__file__).resolve().parents[2]/'modules/scripts/lib/backlight.py'
    spec=importlib.util.spec_from_file_location('test_backlight_driver',path)
    mod=importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules',{'machine':machine,'time':clock}): spec.loader.exec_module(mod)
    for target in [16,1,8,4,15,3,3,0,7,16,0,1]:
        assert mod.set_brightness(target)==target
        assert chip.level==target
