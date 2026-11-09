"""Settings, channels, node screens and the Meshtastic backend, driven by keys."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "modules/scripts/lib"))
sys.path.insert(0, str(ROOT / "utils"))

import meshback  # noqa: E402
import meshset  # noqa: E402
import meshui  # noqa: E402
import mtconfig  # noqa: E402
import mtmesh  # noqa: E402
import mtproto as P  # noqa: E402
import mtradio as R  # noqa: E402
import mesh_preview as preview  # noqa: E402

T0 = 1_800_000_000.0
D, U, E, L, Rt = "DOWN", "UP", "\r", "LEFT", "RIGHT"


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
    return app, backend, clock


def keys(app, clock, *ks):
    for k in ks:
        app.key(k, clock.t)


def typed(app, clock, text):
    for ch in text:
        app.key(ch, clock.t)


def screen(app, clock, cols=40, rows=16):
    plain, _ = preview.emulate(app.render(cols, rows, clock.t), cols, rows)
    return plain


def open_settings(app, clock):
    keys(app, clock, "ESC", D, D, D, E)       # menu > Settings
    assert app.mode == "settings"


def open_section(app, clock, name):
    open_settings(app, clock)
    for _ in range(meshset.SEC_NAMES.index(name)):
        keys(app, clock, D)
    keys(app, clock, E)


# --- settings flow -----------------------------------------------------------

def test_settings_list_shows_summaries(rig):
    app, backend, clock = rig
    open_settings(app, clock)
    text = "\n".join(screen(app, clock))
    assert "Radio" in text and "US  LongFast  slot 20" in text and "2 channels" in text


def test_radio_edit_changes_live_summary_and_marks_unapplied(rig):
    app, backend, clock = rig
    open_section(app, clock, "Radio")
    assert app.mode == "form"
    keys(app, clock, Rt)                          # region: US -> next
    assert app.cfg_edit.region == "EU_433"
    top = "\n".join(screen(app, clock)[1:5])
    assert "unapplied" in top and "433" in top
    keys(app, clock, L)
    assert app.cfg_edit.region == "US"


def test_preset_toggle_swaps_visible_fields(rig):
    app, backend, clock = rig
    open_section(app, clock, "Radio")
    before = "\n".join(screen(app, clock))
    assert "Modem preset" in before and "Bandwidth" not in before
    keys(app, clock, D, Rt)                       # Use preset -> off
    after = "\n".join(screen(app, clock))
    assert "Bandwidth" in after and "Spread factor" in after and "Modem preset" not in after


def test_number_fields_step_clamp_and_accept_typed_values(rig):
    app, backend, clock = rig
    open_section(app, clock, "Radio")
    # Max hops row: region, use preset, preset, slot, hops
    keys(app, clock, D, D, D, D)
    assert app._fields()[app.form_sel]["key"] == "hop_limit"
    for _ in range(10):
        keys(app, clock, Rt)
    assert app.cfg_edit.hop_limit == 7
    keys(app, clock, E)
    assert app.prompt is not None
    typed(app, clock, "\x7f5")
    keys(app, clock, E)
    assert app.cfg_edit.hop_limit == 5
    keys(app, clock, E)
    typed(app, clock, "\x7f9")
    keys(app, clock, E)
    assert app.cfg_edit.hop_limit == 5 and "Use 1 to 7" in screen(app, clock)[15]


def test_region_change_clamps_power_to_legal_limit(rig):
    app, backend, clock = rig
    app.cfg_edit = None
    open_section(app, clock, "Radio")
    app.cfg_edit.tx_power = 22
    keys(app, clock, Rt)                          # -> EU_433, limit 10 dBm
    assert app.cfg_edit.region == "EU_433" and app.cfg_edit.tx_power == 10


def test_apply_pushes_settings_to_backend(rig):
    app, backend, clock = rig
    open_section(app, clock, "Radio")
    keys(app, clock, D, D, D, D, Rt, "ESC")       # hops 3 -> 4, back to list
    assert app.mode == "settings" and app.cfg_dirty()
    keys(app, clock, D, D, D, D, D, D)            # to "Apply changes" (skips the gap)
    keys(app, clock, E)
    assert backend.config.hop_limit == 4 and not app.cfg_dirty()
    assert "Applied" in screen(app, clock)[15]


def test_leaving_with_unapplied_changes_asks_first(rig):
    app, backend, clock = rig
    open_section(app, clock, "Device")
    keys(app, clock, E)                           # long name prompt
    typed(app, clock, "!!")
    keys(app, clock, E, "ESC")
    assert app.cfg_dirty() and app.mode == "settings"
    keys(app, clock, "ESC")
    assert app.dialog is not None
    keys(app, clock, D, E)                        # Discard
    assert app.mode == "chat" and backend.config.long_name == "vtOS demo"
    open_section(app, clock, "Device")
    keys(app, clock, E)
    typed(app, clock, "?")
    keys(app, clock, E, "ESC", "ESC")
    keys(app, clock, E)                           # Apply
    assert app.mode == "chat" and backend.config.long_name.endswith("?")


def test_bad_settings_are_refused_with_a_reason(rig):
    app, backend, clock = rig
    open_section(app, clock, "Radio")
    app.cfg_edit.region = "NOPE"
    assert app.apply_settings(clock.t) is False
    assert "region" in screen(app, clock)[15].lower()


def test_reset_to_defaults_needs_confirmation(rig):
    app, backend, clock = rig
    open_section(app, clock, "Advanced")
    app.cfg_edit.dio2 = False
    keys(app, clock, D, D, D)                     # Reset row
    keys(app, clock, E)
    assert app.dialog and app.cfg_edit.dio2 is False
    keys(app, clock, E)
    assert app.cfg_edit.dio2 is True


# --- channels ----------------------------------------------------------------

def test_channel_list_add_delete_and_primary_protected(rig):
    app, backend, clock = rig
    keys(app, clock, "ESC", D, D, E)              # Channels
    assert app.mode == "chlist"
    text = "\n".join(screen(app, clock))
    assert "LongFast" in text and "default" in text and "AES128" in text
    keys(app, clock, "a")
    assert len(app.cfg_edit.channels) == 3 and app.ch_idx == 2
    keys(app, clock, "d")
    keys(app, clock, E)                           # Delete
    assert len(app.cfg_edit.channels) == 2
    keys(app, clock, U, "d")
    assert app.dialog is None and "primary" in screen(app, clock)[15].lower()


def test_channel_key_choices(rig):
    app, backend, clock = rig
    keys(app, clock, "ESC", D, D, E, D, E)        # Channels > Hike > form
    assert app.form_kind == "channel"
    keys(app, clock, D, E)                        # key dialog
    keys(app, clock, D, D, D, E)                  # new random 256-bit
    assert len(app.cfg_edit.channels[1].psk) == 32
    keys(app, clock, E, E)                        # default key
    assert app.cfg_edit.channels[1].psk == b"\x01"
    keys(app, clock, E, D, D, D, D, E)            # type a key
    typed(app, clock, "00112233445566778899aabbccddeeff")
    keys(app, clock, E)
    assert app.cfg_edit.channels[1].psk == bytes.fromhex("00112233445566778899aabbccddeeff")
    keys(app, clock, E, D, E)                     # none
    assert app.cfg_edit.channels[1].psk == b"\x00"


def test_parse_key_accepts_hex_base64_and_keywords():
    assert meshset.parse_key("default") == b"\x01"
    assert meshset.parse_key("AQ==") == b"\x01"
    assert meshset.parse_key("none") == b"\x00"
    assert meshset.parse_key("11" * 16) == bytes([0x11]) * 16
    import base64
    assert meshset.parse_key(base64.b64encode(bytes(range(32))).decode()) == bytes(range(32))
    with pytest.raises(ValueError):
        meshset.parse_key("zz")
    with pytest.raises(ValueError):
        meshset.parse_key("abcd" * 5)


def test_share_and_import_round_trip(rig):
    app, backend, clock = rig
    keys(app, clock, "ESC", D, D, E, "s")
    assert app.mode == "page" and app.page[0] == "Share"
    url = backend.config.share_url()
    flat = "".join(ln[0][2] for ln in app.page[1] if ln[0][2].startswith(("https", "AAEC")) or True)
    assert "meshtastic.org/e/#" in flat
    keys(app, clock, "ESC")
    assert app.mode == "chlist"
    other = mtconfig.Settings()
    other.channels = [mtconfig.Channel("Mine", bytes(range(32)))]
    other.region = "ANZ"
    other.preset = "MEDIUM_FAST"
    link = other.share_url()
    keys(app, clock, "i")
    typed(app, clock, link)
    keys(app, clock, E)
    assert app.dialog and "1 channel" in " ".join(app.dialog["lines"])
    keys(app, clock, E)
    assert app.cfg_edit.channels[0].name == "Mine" and app.cfg_edit.region == "ANZ"
    assert app.cfg_edit.preset == "MEDIUM_FAST"
    assert url.startswith("https://meshtastic.org/e/#")


def test_import_rejects_garbage(rig):
    app, backend, clock = rig
    keys(app, clock, "ESC", D, D, E, "i")
    typed(app, clock, "https://example.com/not-a-link")
    keys(app, clock, E)
    assert app.dialog is None and "valid channel link" in screen(app, clock)[15]


def test_apply_resyncs_chat_tabs_with_new_channels(rig):
    app, backend, clock = rig
    keys(app, clock, "ESC", D, D, E, "a")
    keys(app, clock, "ESC", "ESC")                # back to chat? settings list first
    assert app.mode == "settings"
    keys(app, clock, D, D, D, D, D, D, E)         # Apply changes
    assert len([k for k in app.order if k.startswith("ch:")]) == 3


# --- nodes -------------------------------------------------------------------

def test_node_detail_and_actions(rig):
    app, backend, clock = rig
    keys(app, clock, "ESC", E, "i")
    assert app.mode == "node"
    text = "\n".join(screen(app, clock))
    assert "Bob" in text and "!0000b0b1" in text and "From you" in text and "direct" in text
    keys(app, clock, "f")
    assert app.node_view.fav
    keys(app, clock, "t")
    clock.t += 3
    app.poll(clock.t)
    assert "Route" in screen(app, clock)[15]
    keys(app, clock, "ESC")
    assert app.mode == "nodes"
    assert app.nodes[0].fav                     # favourites sort first
    assert "*Bob" in "\n".join(screen(app, clock)).replace(" ", "")


def test_distance_and_bearing():
    km, brg = meshset.distance_bearing(37.7749, -122.4194, 37.8044, -122.2712)
    assert 13.0 < km < 14.5 and 70 < brg < 80
    km, brg = meshset.distance_bearing(0, 0, 0, 0)
    assert km == 0


def test_slash_commands_for_new_screens(rig):
    app, backend, clock = rig
    for cmd, mode in (("/settings", "settings"), ("/channels", "chlist"), ("/stats", "page")):
        typed(app, clock, cmd)
        keys(app, clock, E)
        assert app.mode == mode, cmd
        app.cfg_edit = None
        app.mode = "chat"
    typed(app, clock, "/node ali")
    keys(app, clock, E)
    assert app.mode == "node" and app.node_view.name == "Alice"
    app.mode = "chat"
    typed(app, clock, "/pos")
    keys(app, clock, E)
    assert "Position shared" in screen(app, clock)[15]


def test_incoming_message_shows_hops_and_signal(rig):
    app, backend, clock = rig
    assert any("1 hop +6dB" in ln or "3 hops -4dB" in ln for ln in screen(app, clock))


@pytest.mark.parametrize("cols,rows", [(40, 16), (53, 19), (64, 29), (32, 12)])
@pytest.mark.parametrize("scene", ["settings", "radio", "device", "position", "advanced", "channels",
                                   "channel", "keydialog", "prompt", "node", "stats", "share",
                                   "info", "help", "nodes"])
def test_new_screens_fit_every_grid(scene, cols, rows):
    plain, _ = preview.emulate(preview.scene(scene, cols, rows), cols, rows)
    assert len(plain) == rows


# --- settings model ----------------------------------------------------------

def test_settings_round_trip_and_defaults():
    s = mtconfig.Settings()
    s.region = "ANZ"
    s.channels.append(mtconfig.Channel("Hike", bytes(range(32))))
    back = mtconfig.Settings.from_dict(s.to_dict())
    assert back.region == "ANZ" and back.channels[1].psk == bytes(range(32))
    assert mtconfig.Settings().problems() == []


def test_problems_catch_unusable_settings():
    s = mtconfig.Settings()
    s.region = "NOPE"
    assert "region" in s.problems()[0].lower()
    s = mtconfig.Settings()
    s.channels[0] = mtconfig.Channel("Same", b"\x01")
    s.channels.append(mtconfig.Channel("Same", b"\x01"))
    assert any("clash" in p for p in s.problems())
    s = mtconfig.Settings()
    s.channels[0].psk = b"abc"
    assert any("wrong length" in p for p in s.problems())


def test_radio_args_match_meshtastic_defaults():
    s = mtconfig.Settings()
    a = s.radio_args()
    assert a["freq"] == pytest.approx(906.875) and a["bw"] == 250.0 and a["sf"] == 11
    assert a["cr"] == 5 and a["sync_word"] == 0x2B and a["preamble"] == 16
    assert a["power"] == 17
    s.use_preset = False
    s.bandwidth, s.sf, s.cr = 125.0, 9, 6
    assert s.radio_args()["bw"] == 125.0


def test_name_warning_only_for_unverified_presets():
    s = mtconfig.Settings()
    assert s.name_warning() == ""
    s.preset = "MEDIUM_FAST"
    assert "unverified" in s.name_warning()
    s.slot = 10
    assert s.name_warning() == ""


# --- Meshtastic backend --------------------------------------------------------

class FakeLora:
    def __init__(self, fail_tcxo=()):
        self.begun = []
        self.fail_tcxo = fail_tcxo
        self.inbox = []
        self.sent = []
        self.rx_boosted = None
        self.deinit_called = False

    def begin(self, **kw):
        if "tcxo" in kw and kw["tcxo"] in self.fail_tcxo:
            raise RuntimeError("RadioLib begin failed with code -706")
        self.begun.append(kw)

    def get_device_errors(self):
        return 0

    def rx_boost(self, on):
        self.rx_boosted = on

    def start_receive(self):
        pass

    def transmit(self, raw):
        self.sent.append(bytes(raw))

    def receive(self):
        return self.inbox.pop(0) if self.inbox else None

    def snr(self):
        return 4.0

    def rssi(self):
        return -90.0

    def deinit(self):
        self.deinit_called = True


class OldLora(FakeLora):
    def begin(self, **kw):
        if "preamble" in kw or "tcxo" in kw:
            raise TypeError("unexpected keyword argument")
        self.begun.append(kw)


def make_backend(tmp_path, radio=None, **kw):
    radio = radio or FakeLora()
    cfg = mtconfig.Settings()
    cfg.node_num = 0x1111
    cfg.nodeinfo_interval = 0
    cfg.telem_interval = 0
    clock = Clock()
    ok, msg, limited = meshback.configure_radio(radio, cfg)
    assert ok, msg
    backend = meshback.MeshtasticBackend(radio, cfg, 0x1111, None, clock=clock,
                                         configure=meshback.configure_radio,
                                         node_path=str(tmp_path / "nodes.json"),
                                         cfg_path=str(tmp_path / "cfg.json"),
                                         limited=limited, **kw)
    return backend, radio, clock


def test_configure_radio_uses_meshtastic_parameters():
    radio = FakeLora()
    cfg = mtconfig.Settings()
    ok, msg, limited = meshback.configure_radio(radio, cfg)
    kw = radio.begun[0]
    assert ok and limited == ""
    assert kw["sync_word"] == 0x2B and kw["preamble"] == 16 and kw["bw"] == 250.0
    assert kw["freq"] == pytest.approx(906.875) and kw["dio2_rf_switch"] is True
    assert kw["current_limit"] == 140 and radio.rx_boosted is False


def test_configure_radio_probes_tcxo_and_remembers():
    radio = FakeLora(fail_tcxo=(0.0, 1.8))
    cfg = mtconfig.Settings()
    ok, msg, _ = meshback.configure_radio(radio, cfg)
    assert ok and cfg.tcxo_found == 2.8
    radio2 = FakeLora(fail_tcxo=(0.0, 1.8))
    ok, _, _ = meshback.configure_radio(radio2, cfg)
    assert ok and [b["tcxo"] for b in radio2.begun] == [2.8]       # no re-probing


def test_configure_radio_reports_old_firmware():
    ok, msg, limited = meshback.configure_radio(OldLora(), mtconfig.Settings())
    assert ok and "rebuild" in limited


def test_configure_radio_fails_cleanly_when_nothing_works():
    ok, msg, _ = meshback.configure_radio(FakeLora(fail_tcxo=(0.0, 1.8, 2.8, 2.4, 3.3)), mtconfig.Settings())
    assert not ok and "-706" in msg


def test_backend_sends_encrypted_text_visible_to_a_second_node(tmp_path):
    backend, radio, clock = make_backend(tmp_path)
    mid = backend.send("ch:0", "hello mesh")
    evs = backend.poll()
    assert ("ack", mid, "sent") in evs
    raw = radio.sent[0]
    assert b"hello mesh" not in raw                      # encrypted on the air
    other_radio = FakeLora()
    other_cfg = mtconfig.Settings()
    other = mtmesh.Mesh(other_radio, other_cfg, 0x2222, clock)
    other_radio.inbox.append(raw)
    ev = other.poll()
    assert [e for e in ev if e[0] == "text"][0][4] == "hello mesh"


def test_backend_translates_incoming_text_and_dm(tmp_path):
    backend, radio, clock = make_backend(tmp_path)
    sender_radio = FakeLora()
    sender = mtmesh.Mesh(sender_radio, mtconfig.Settings(), 0x2222, clock)
    sender.cfg.long_name = "Zed"
    sender.send_nodeinfo()
    sender.send_text(P.BROADCAST, "to all", want_ack=False)
    sender.send_text(0x1111, "just you", want_ack=False)
    sender.poll()
    sender.poll()
    sender.poll()
    for raw in sender_radio.sent:
        radio.inbox.append(raw)
    msgs = [e for e in backend.poll() if e[0] == "msg"]
    keys_ = {m[1]: m for m in msgs}
    assert keys_["ch:0"][3:5] == ("Zed", "to all") or keys_["ch:0"][4] == "to all"
    assert keys_["dm:!00002222"][4] == "just you"
    assert "direct" in keys_["ch:0"][6] and "+4dB" in keys_["ch:0"][6]


def test_backend_apply_persists_and_retunes(tmp_path):
    backend, radio, clock = make_backend(tmp_path)
    new = backend.config.copy()
    new.region = "ANZ"
    new.hop_limit = 5
    ok, msg = backend.apply(new)
    assert ok and radio.begun[-1]["freq"] == pytest.approx(new.frequency())
    assert backend.mesh.cfg.hop_limit == 5
    assert (tmp_path / "cfg.json").exists()
    bad = new.copy()
    bad.region = "NOPE"
    ok, msg = backend.apply(bad)
    assert not ok and backend.config.region == "ANZ"


def test_backend_node_persistence_and_close(tmp_path):
    class Env:
        pass
    env = Env()
    backend, radio, clock = make_backend(tmp_path)
    backend.env = env
    env.radio = radio
    n = backend.mesh.node(0x3333)
    n.long_name = "Remembered"
    backend.close()
    assert radio.deinit_called and env.radio is None
    again, _, _ = make_backend(tmp_path)
    assert again.mesh.nodes[0x3333].long_name == "Remembered"


def test_ui_drives_real_backend_end_to_end(tmp_path):
    backend, radio, clock = make_backend(tmp_path)
    app = meshui.App(backend, clock())
    typed(app, clock, "ping")
    app.key("\r", clock.t)
    app.poll(clock.t)
    assert len(radio.sent) == 1                  # went out on the first pass
    app.poll(clock.t)
    assert "ping sent" in "\n".join(screen(app, clock))
    # bring up settings against the real backend
    keys(app, clock, "ESC", D, D, D, E)
    assert app.mode == "settings"
    assert "US  LongFast  slot 20" in "\n".join(screen(app, clock))


def test_numeric_prompt_replaces_the_old_value_on_first_key(rig):
    app, backend, clock = rig
    open_section(app, clock, "Position")
    keys(app, clock, Rt, Rt, D, D, E)             # mode fixed, to latitude, edit
    assert app.prompt["value"] == "0.0000"
    typed(app, clock, "37.7749")
    keys(app, clock, E)
    assert app.cfg_edit.fixed_lat == pytest.approx(37.7749)
    keys(app, clock, E)
    keys(app, clock, "\x7f")                      # backspace clears all while fresh
    assert app.prompt["value"] == ""
    typed(app, clock, "1")
    keys(app, clock, "\x7f")                      # now it edits normally
    assert app.prompt["value"] == ""
