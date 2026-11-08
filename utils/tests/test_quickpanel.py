"""Touch gestures must not change radios accidentally or corrupt live SSH output."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

ROOT = Path(__file__).resolve().parents[2] / 'modules/scripts'


def module(path, name, fakes=None):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules', fakes or {}):
        spec.loader.exec_module(mod)
    return mod


class Switch:
    def __init__(self, on=False, connected=False):
        self.on, self.connected = on, connected
        self.changes = []
    def active(self, value=None):
        if value is not None:
            self.on = value
            self.changes.append(value)
        return self.on
    def isconnected(self): return self.connected
    def connect(self): pass


@pytest.fixture
def panel():
    hw = SimpleNamespace(set_brightness=Mock(side_effect=lambda x: x),
                         GPS_TX=43, GPS_RX=44, init_radio=Mock())
    mod = module('sys/quickpanel.py', 'test_panel', {'hardware': hw})
    mod.time = SimpleNamespace(ticks_ms=lambda: 100, ticks_diff=lambda a,b:a-b)
    wifi, ble = Switch(), Switch()
    font = module('fonts/terminus_mpy_12.py', 'test_panel_font')
    env = SimpleNamespace(term=Mock(), status_height=14, sd_busy=False,
                          volume=70, brightness=16, audio=None, ble=None,
                          radio=None, gps=None, app_uses_radio=False)
    fake = {'network': SimpleNamespace(STA_IF=0, WLAN=lambda _: wifi),
            'bluetooth': SimpleNamespace(BLE=lambda: ble),
            'fonts': SimpleNamespace(terminus_mpy_12=font)}
    with patch.dict('sys.modules', fake):
        p = mod.QuickPanel(env, Mock(), Mock())
    return p, hw, wifi, ble, mod


def gesture(p, points):
    for point in points: p.contact(point)
    p.contact(None)


def test_only_top_edge_swipe_opens(panel):
    p,*_ = panel
    gesture(p, [(50,60),(50,130)])
    assert not p.opened
    gesture(p, [(50,5),(50,65)])
    assert p.opened


def test_opening_swipe_does_not_activate_tile(panel):
    p,_,wifi,*_ = panel
    gesture(p, [(40,0),(40,60)])
    assert p.opened and wifi.changes == []


def test_up_swipe_restores_terminal(panel):
    p,*_ = panel
    p.open()
    gesture(p, [(150,90),(150,30)])
    assert not p.opened
    p.env.term.restore.assert_called_once()


def test_wifi_off_requires_second_tap_when_connected(panel):
    p,_,wifi,*_ = panel
    wifi.on = wifi.connected = True
    p.tap(30,60)
    assert wifi.changes == []
    p.tap(30,60)
    assert wifi.changes == [False]


def test_tile_gaps_do_not_toggle_radio(panel):
    p,_,wifi,ble,_ = panel
    p.tap(78,60)
    assert wifi.changes == ble.changes == []


def test_slider_drag_clamps_and_does_not_hit_tiles(panel):
    p,hw,wifi,*_ = panel
    p.open()
    gesture(p, [(100,130),(-10,130),(340,130)])
    assert p.env.brightness == 16
    assert hw.set_brightness.call_args_list[1].args == (1,)
    assert not wifi.changes


def test_volume_changes_live_audio_and_future_default(panel):
    p,*_ = panel
    p.env.audio = Mock()
    p.open()
    gesture(p, [(24,170),(160,170)])
    assert p.env.volume == 50
    p.env.audio.volume.assert_called_with(50)


def test_busy_radios_are_not_destroyed(panel):
    p,hw,_,ble,_ = panel
    p.env.ble = Mock()
    p.tap(110,60)
    assert not ble.changes
    p.env.radio = Mock(); p.env.app_uses_radio = True
    p.tap(270,60)
    p.env.radio.deinit.assert_not_called()
    hw.init_radio.assert_not_called()


def test_overlay_keeps_live_terminal_writes_and_restores_bars(panel):
    p,_,_,_,mod = panel
    native = Mock()
    overlay = mod.TerminalOverlay(native,p)
    p.open()
    overlay.write(b'new SSH output')
    overlay.draw(); overlay.top_bar(b'Wi-Fi ON')
    native.write.assert_called_with(b'new SSH output')
    native.draw.assert_not_called(); native.top_bar.assert_not_called()
    overlay.restore()
    native.top_offset.assert_called_with(14)
    native.top_bar.assert_called_with(b'Wi-Fi ON')
    native.draw.assert_called_once()


def test_notification_history_is_bounded_and_browsable(panel):
    p,*_ = panel
    for i in range(8): p.notify(str(i))
    assert p.notifications == ['3','4','5','6','7']
    p.tap(50,200)
    assert p.notifications[p.notice_index] == '6'


def test_touch_bus_error_closes_panel_without_breaking_terminal(panel):
    p,*_ = panel
    p.open(); p.touch.read.side_effect = OSError('bus')
    p.poll()
    assert not p.opened and p.last_error == 'bus'
    p.env.term.restore.assert_called_once()


def test_sd_transfers_defer_touch_and_drawing(panel):
    p,*_ = panel
    p.open(); p.env.sd_busy = True
    p.poll()
    p.touch.read.assert_not_called(); p.display.fill_rect.assert_not_called()


def test_render_uses_bundled_font_with_valid_display_bounds(panel):
    p,*_ = panel
    p.notify('Connected')
    p.draw()
    for call in p.display.blit_buffer.call_args_list:
        pixels,x,y,width,height = call.args
        assert len(pixels) == width*height*2
        assert 0 <= x < x+width <= 320 and 0 <= y < y+height <= 240


def test_gt911_landscape_and_no_new_frame_preserves_contact():
    mod = module('lib/touchscreen.py','test_gt911')
    bus = Mock()
    bus.readfrom_mem.side_effect = [b'911\0', b'\x81', bytes([0,20,0,100,0,0,0,0]), b'\0', b'\x80']
    touch = mod.GT911(bus)
    assert touch.read() == (100,219)
    assert touch.read() == (100,219)
    assert touch.read() is None
    assert bus.writeto_mem.call_count == 2


def test_gt911_missing_controller_is_optional():
    mod = module('lib/touchscreen.py','test_gt911_missing')
    bus = Mock(); bus.readfrom_mem.side_effect = OSError('absent')
    with pytest.raises(OSError,match='not found'): mod.GT911(bus)
