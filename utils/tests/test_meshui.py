"""Check Meshchat layout, input handling and the demo backend without hardware."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "modules/scripts/lib"))

import meshback  # noqa: E402
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
    backend = meshback.SimBackend(clock=clock)
    app = meshui.App(backend, clock())
    clock.t += 1
    app.poll(clock.t)
    return app, clock


def type_text(app, clock, text):
    for ch in text:
        app.key(ch, clock.t)


def screen(app, clock, cols=40, rows=16):
    plain, _ = preview.emulate(app.render(cols, rows, clock.t), cols, rows)
    return plain


def test_wrap_words_and_hard_splits():
    assert meshui.wrap("hello brave new world", 11) == ["hello brave", "new world"]
    assert meshui.wrap("abcdefghij", 4) == ["abcd", "efgh", "ij"]
    assert meshui.wrap("a\nb", 10) == ["a", "b"]
    assert meshui.wrap("", 10) == [""]
    assert meshui.wrap("x  y", 10) == ["x y"]


def test_sanitize_strips_escapes_and_non_ascii():
    assert meshui.sanitize("a\x1b[31mb") == "a?[31mb"
    assert meshui.sanitize("café\tx") == "caf? x"


def test_age_strings():
    assert [meshui.age_str(s) for s in (None, 3, 30, 300, 7200, 200000)] == \
        ["--", "now", "30s", "5m", "2h", "2d"]


@pytest.mark.parametrize("cols,rows", SIZES)
@pytest.mark.parametrize("scene", ["chat", "nodes", "chats", "menu", "info", "help", "scrolled"])
def test_every_screen_fits_the_grid(scene, cols, rows):
    # emulate() raises if a write overflows the row or leaves the screen
    plain, _ = preview.emulate(preview.scene(scene, cols, rows), cols, rows)
    assert len(plain) == rows and all(len(r) == cols for r in plain)


def test_history_and_dm_unread_on_start(rig):
    app, clock = rig
    assert app.order == ["ch:0", "ch:1", "dm:!0000b0b1"]
    assert app.convs["dm:!0000b0b1"].unread == 1
    assert len(app.convs["ch:0"].msgs) == 3
    assert "Bob 1" in screen(app, clock)[1]


def test_discover_opens_nodes_from_command_menu_and_shortcut(rig):
    app, clock = rig
    type_text(app, clock, "/discover")
    app.key("\r", clock.t)
    assert app.mode == "nodes" and "Demo" in app.notice[0]
    app.key("d", clock.t)
    assert app.mode == "nodes" and "Demo" in app.notice[0]
    app.mode = "menu"
    app.sel = app.menu_items().index("Discover nodes")
    app.key("\r", clock.t)
    assert app.mode == "nodes"
    for cols, rows in SIZES:
        screen(app, clock, cols, rows)


def test_discovery_controls_hidden_for_raw_mode():
    app = meshui.App(meshback.LoraBackend(FakeRadio(), "test"), T0)
    assert "Discover nodes" not in app.menu_items()
    assert "discover" not in app.commands()


def test_send_ack_and_fail_markers(rig):
    app, clock = rig
    for i in range(4):
        type_text(app, clock, "msg%d" % i)
        app.key("\r", clock.t)
    sending = screen(app, clock)
    assert any("msg3 ..." in line for line in sending)
    clock.t += 4
    app.poll(clock.t)
    done = screen(app, clock)
    assert any("msg0 ok" in line for line in done)
    assert any("msg3 !" in line for line in done)


def test_incoming_while_scrolled_keeps_view_and_counts(rig):
    app, clock = rig
    clock.t += 60
    app.poll(clock.t)  # the scripted history
    for i in range(6):
        app.conv().add(meshui.Msg("!5d09", "Sam", "filler %d" % i, clock.t))
    app.render(40, 16, clock.t)
    for _ in range(3):
        app.key("UP", clock.t)
    before = screen(app, clock)
    assert app.conv().scroll == 3
    app.backend._due.append((clock.t, ("msg", "ch:0", "!5d09", "Sam", "Fresh news", clock.t)))
    app.poll(clock.t)
    after = screen(app, clock)
    assert app.conv().newer == 1
    assert "1 new" in after[12]
    assert before[3:12] == after[3:12]
    app.key("DOWN", clock.t)
    for _ in range(5):
        app.key("DOWN", clock.t)
    app.render(40, 16, clock.t)
    assert app.conv().scroll == 0 and app.conv().newer == 0


def test_switch_chat_clears_unread(rig):
    app, clock = rig
    app.key("RIGHT", clock.t)
    app.key("RIGHT", clock.t)
    assert app.cur == "dm:!0000b0b1" and app.conv().unread == 0
    app.key("LEFT", clock.t)
    app.key("LEFT", clock.t)
    assert app.cur == "ch:0"


def test_slash_commands(rig):
    app, clock = rig
    type_text(app, clock, "/dm ali")
    app.key("\r", clock.t)
    assert app.cur == "dm:!0000a1f3" and app.conv().title == "Alice"
    type_text(app, clock, "/nodes")
    app.key("\r", clock.t)
    assert app.mode == "nodes"
    app.key("ESC", clock.t)
    type_text(app, clock, "/ch 1")
    app.key("\r", clock.t)
    assert app.cur == "ch:0"
    type_text(app, clock, "/nope")
    app.key("\r", clock.t)
    assert "Unknown command" in screen(app, clock)[15]
    type_text(app, clock, "/clear")
    app.key("\r", clock.t)
    assert app.conv().msgs == []
    type_text(app, clock, "/quit")
    assert app.key("\r", clock.t) == "quit"


def test_command_hint_while_typing(rig):
    app, clock = rig
    type_text(app, clock, "/n")
    assert "/nodes" in screen(app, clock)[15]


def test_menu_and_back_navigation(rig):
    app, clock = rig
    app.key("ESC", clock.t)
    assert app.mode == "menu"
    app.key("ESC", clock.t)
    assert app.mode == "chat"
    app.key("ESC", clock.t)
    for _ in range(len(app.menu_items()) - 1):
        app.key("DOWN", clock.t)
    assert app.key("\r", clock.t) == "quit"


def test_nodes_list_opens_dm(rig):
    app, clock = rig
    app.key("ESC", clock.t)
    app.key("\r", clock.t)               # Nodes
    app.key("DOWN", clock.t)              # Alice
    app.key("\r", clock.t)
    assert app.mode == "chat" and app.cur == "dm:!0000a1f3"


def test_input_is_capped_and_backspace_works(rig):
    app, clock = rig
    type_text(app, clock, "x" * (meshui.MAX_INPUT + 20))
    assert len(app.input) == meshui.MAX_INPUT
    app.key("\x7f", clock.t)
    assert len(app.input) == meshui.MAX_INPUT - 1


def test_incoming_escape_sequences_never_reach_the_screen(rig):
    app, clock = rig
    app.backend._due.append((clock.t, ("msg", "ch:0", "!evil", "Eve", "\x1b[2Jboom", clock.t)))
    app.poll(clock.t)
    frame = app.render(40, 16, clock.t)
    assert "\x1b[2J" not in frame


def test_long_conversation_is_trimmed(rig):
    app, clock = rig
    for i in range(meshui.MAX_MSGS + 20):
        app.conv().add(meshui.Msg("x", "X", "m%d" % i, clock.t))
    assert len(app.conv().msgs) == meshui.MAX_MSGS


def test_open_backend_falls_back_to_demo_with_a_note():
    backend, note = meshback.open_backend(None)
    assert isinstance(backend, meshback.SimBackend) and note
    backend, note = meshback.open_backend(None, demo=True)
    assert note is None


class FakeRadio:
    def __init__(self):
        self.sent = []
        self.inbox = []
        self.rx_armed = 0
        self.deinit_called = False

    def start_receive(self):
        self.rx_armed += 1

    def transmit(self, data):
        self.sent.append(data)

    def receive(self):
        return self.inbox.pop(0) if self.inbox else None

    def snr(self):
        return 7.5

    def rssi(self):
        return -90

    def deinit(self):
        self.deinit_called = True


@pytest.fixture
def lora():
    radio = FakeRadio()
    clock = Clock()
    backend = meshback.LoraBackend(radio, "me", 868.0, clock=clock)
    app = meshui.App(backend, clock())
    return app, radio, clock


def test_lora_send_uses_lorachat_wire_format_and_marks_sent(lora):
    app, radio, clock = lora
    type_text(app, clock, "hello")
    app.key("\r", clock.t)
    assert radio.sent == [b"me: hello"]
    assert radio.rx_armed >= 2                 # re-armed after transmit
    app.poll(clock.t)
    assert any("hello sent" in line for line in screen(app, clock))


def test_lora_receives_lorachat_packets_and_tracks_nodes(lora):
    app, radio, clock = lora
    radio.inbox.append(b"bob: anyone out there?")
    app.poll(clock.t)
    assert [m.text for m in app.conv().msgs] == ["anyone out there?"]
    assert [n.name for n in app.nodes] == ["bob"]
    assert app.nodes[0].snr == 7.5
    radio.inbox.append(b"\xff\xfe")            # garbage must not crash the UI
    app.poll(clock.t)
    assert len(app.conv().msgs) == 2


def test_lora_direct_messages_round_trip(lora):
    app, radio, clock = lora
    radio.inbox.append(b"bob: @me psst")
    app.poll(clock.t)
    assert app.convs["dm:bob"].msgs[0].text == "psst"
    assert app.convs["dm:bob"].unread == 1
    app.key("RIGHT", clock.t)
    type_text(app, clock, "yes?")
    app.key("\r", clock.t)
    assert radio.sent == [b"me: @bob yes?"]


def test_lora_send_failure_is_reported(lora):
    app, radio, clock = lora

    def boom(data):
        raise RuntimeError("tx timeout")

    radio.transmit = boom
    type_text(app, clock, "x")
    app.key("\r", clock.t)
    assert app.conv().msgs[-1].state == "fail"
    assert "Send failed" in screen(app, clock)[15]


def test_lora_close_releases_the_radio():
    radio = FakeRadio()
    env = type("Env", (), {"radio": radio})()
    meshback.LoraBackend(radio, "me", None, env).close()
    assert radio.deinit_called and env.radio is None
