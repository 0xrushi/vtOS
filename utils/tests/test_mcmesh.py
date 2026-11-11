"""MeshCore engine, backend, UI controls and the `meshcore` command, against a
simulated repeater built from the same (from-memory) packet layouts. Proves the
logic is self-consistent; it is NOT evidence of on-air compatibility."""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "modules/scripts/lib"))
sys.path.insert(0, str(ROOT / "utils"))

import mccurve as C  # noqa: E402
import mcback  # noqa: E402
import mcmesh  # noqa: E402
import mcproto as W  # noqa: E402
import meshui  # noqa: E402
import mesh_preview as preview  # noqa: E402

T0 = 1_800_000_000.0
E = "\r"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Air:
    """Radio double: records transmits, feeds queued frames to receive()."""

    def __init__(self):
        self.sent = []
        self.inbox = []
        self.listening = False

    def transmit(self, data):
        self.sent.append(bytes(data))

    def receive(self):
        return self.inbox.pop(0) if self.inbox else None

    def start_receive(self):
        self.listening = True

    def snr(self):
        return 6.5

    def deinit(self):
        pass


class Repeater:
    """A repeater with a password, answering login and CLI commands."""

    def __init__(self, password="hunter2"):
        self.priv = C.new_private(os.urandom(64))
        self.pub = C.public_from_private(self.priv)
        self.password = password
        self.log = []

    def sign(self, msg):
        """Ed25519 signature from the expanded private key (RFC 8032 5.1.6)."""
        import hashlib
        h = lambda b: int.from_bytes(hashlib.sha512(b).digest(), "little")
        a = int.from_bytes(self.priv[:32], "little")
        r = h(self.priv[32:] + msg) % C.L
        big_r = C._encode(C._mul(r, C._B))
        k = h(big_r + self.pub + msg) % C.L
        return big_r + ((r + k * a) % C.L).to_bytes(32, "little")

    def advert(self, forged=False):
        ts = 1700000000
        app = W.build_advert_payload(self.pub, ts, bytes(64), W.ADV_REPEATER,
                                     "Hill-RPT", 37.5, -122.25)[100:]
        sig = self.sign(self.pub + ts.to_bytes(4, "little") + app)
        if forged:
            sig = bytes(64)
        return W.build_advert_payload(self.pub, ts, sig, W.ADV_REPEATER, "Hill-RPT", 37.5, -122.25)

    def link(self, forged=False):
        return "meshcore://" + W.Packet(W.PT_ADVERT, self.advert(forged)).encode().hex()

    def handle(self, raw):
        """-> list of reply frames for a packet addressed to this repeater."""
        pkt = W.decode(raw)
        if pkt.ptype == W.PT_ANON_REQ and pkt.payload[0] == self.pub[0]:
            client = pkt.payload[1:33]
            secret = C.shared_secret(self.priv, client)
            plain = W.unseal(secret, pkt.payload[33:])
            if plain is None:
                return []
            ts, pw = plain[:4], plain[4:].rstrip(b"\x00").decode()
            self.log.append(("login", pw))
            code = 0 if pw == self.password else 1
            body = ts + bytes([code, 4, 1, 3]) + bytes(8)
            return [W.build_peer(W.PT_RESPONSE, secret, client, self.pub, body).encode()]
        if pkt.ptype == W.PT_TXT and pkt.payload[0] == self.pub[0]:
            return []        # needs the client's pub: tests call reply_txt directly
        return []

    def reply_txt(self, client_pub, text, ttype=W.TXT_CLI):
        secret = C.shared_secret(self.priv, client_pub)
        plain = W.txt_plain(1700000100, text, ttype << 2)
        return W.build_peer(W.PT_TXT, secret, client_pub, self.pub, plain).encode()

    def read_txt(self, raw, client_pub):
        pkt = W.decode(raw)
        secret = C.shared_secret(self.priv, client_pub)
        plain = W.unseal(secret, pkt.payload[2:])
        return W.parse_txt(plain), plain


@pytest.fixture(scope="module")
def keys():
    return {"me": C.new_private(os.urandom(64)), "rpt": Repeater()}


@pytest.fixture
def rig(keys, tmp_path):
    clock, air = Clock(), Air()
    st = mcmesh.State()
    st.priv = keys["me"]
    st.pub = C.public_from_private(keys["me"])
    be = mcback.MeshCoreBackend(air, st, None, clock, path=str(tmp_path / "mc.json"))
    return be, air, clock, keys["rpt"]


def add_repeater(be, rpt):
    ok, msg, nid = be.import_contact(rpt.link())
    assert ok, msg
    return nid


# --- state / persistence -------------------------------------------------------

def test_state_roundtrip_and_find(tmp_path, keys):
    st = mcmesh.State()
    st.ensure_identity(os.urandom)
    assert len(st.priv) == 64 and len(st.pub) == 32
    c, created = st.add_contact(keys["rpt"].pub, "Hill-RPT", W.ADV_REPEATER)
    assert created and st.add_contact(keys["rpt"].pub, "Hill2")[1] is False
    path = str(tmp_path / "s.json")
    st.save(path)
    back = mcmesh.State.load(path)
    assert back.priv == st.priv and back.pub == st.pub
    assert back.contacts[0].name == "Hill2" and back.contacts[0].kind == W.ADV_REPEATER
    assert back.find("hill") is back.contacts[0] and back.find(c.nid) is back.contacts[0]
    assert back.channels[0].name == "Public"


def test_load_missing_or_corrupt(tmp_path):
    assert mcmesh.State.load(str(tmp_path / "none")).contacts == []
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert mcmesh.State.load(str(bad)).name == "vtOS"


def test_own_key_and_limits(keys):
    st = mcmesh.State()
    st.ensure_identity(os.urandom)
    with pytest.raises(ValueError):
        st.add_contact(st.pub)
    for i in range(mcmesh.MAX_CONTACTS):
        st.add_contact(bytes([i + 1]) * 32)
    with pytest.raises(ValueError):
        st.add_contact(b"\xee" * 32)


def test_radio_validation_and_args():
    st = mcmesh.State()
    assert st.problems() == ""
    assert st.radio_args()["sync_word"] == 0x12
    ok, msg = mcmesh.set_radio(st, "915.0 250 10 5 17")
    assert ok and st.radio["sf"] == 10 and st.radio["power"] == 17
    assert not mcmesh.set_radio(st, "915 123 7 5")[0] and st.radio["bw"] == 250.0
    assert not mcmesh.set_radio(st, "abc 62.5 7 5")[0]
    assert not mcmesh.set_radio(st, "")[0]


# --- import --------------------------------------------------------------------

def test_import_adds_repeater_contact(rig):
    be, _, _, rpt = rig
    ok, msg, nid = be.import_contact(rpt.link())
    assert ok and "Hill-RPT" in msg and "repeater" in msg
    c = be.nodes()[0]
    assert c.needs_login and c.lat == 37.5
    assert be.import_contact(rpt.link())[1].startswith("Updated")
    assert len(be.nodes()) == 1


def test_import_rejects_junk_forged_and_saves(rig, tmp_path):
    be, _, _, rpt = rig
    assert not be.import_contact("hello")[0]
    ok, msg, _ = be.import_contact(rpt.link(forged=True))
    assert not ok and "signature" in msg and be.nodes() == []
    add_repeater(be, rpt)
    assert json.loads((tmp_path / "mc.json").read_text())["contacts"][0]["name"] == "Hill-RPT"


def test_import_from_heard_advert(rig):
    be, air, clock, rpt = rig
    air.inbox.append(W.Packet(W.PT_ADVERT, rpt.advert()).encode())
    be.poll()
    assert be.nodes() == [] and be.heard()[0][0] == "Hill-RPT"
    ok, msg, nid = be.import_contact("hill")
    assert ok and be.nodes()[0].name == "Hill-RPT" and be.heard() == []


# --- login and CLI ---------------------------------------------------------------

def test_login_success_then_cli_and_reply(rig):
    be, air, clock, rpt = rig
    nid = add_repeater(be, rpt)
    assert be.login(nid, "hunter2")[0]
    sent = air.sent.pop()
    assert W.decode(sent).ptype == W.PT_ANON_REQ
    air.inbox += rpt.handle(sent)
    ev = be.poll()
    assert any(e[0] == "notice" and "Logged in" in e[1] and "admin" in e[1] for e in ev)
    assert be.nodes()[0].logged_in

    mid = be.send("dm:" + nid, "ver")
    (ts, ttype, _, text), _ = rpt.read_txt(air.sent[-1], be.state.pub)
    assert (ttype, text) == (W.TXT_CLI, "ver")
    air.inbox.append(rpt.reply_txt(be.state.pub, "MeshCore v1.2 repeater"))
    ev = be.poll()
    msgs = [e for e in ev if e[0] == "msg"]
    assert msgs[0][1] == "dm:" + nid and msgs[0][4] == "MeshCore v1.2 repeater"
    assert msgs[0][6].startswith("reply")
    assert ("ack", mid, True) in ev          # the reply counted as the ack


def test_login_wrong_password_and_timeout(rig):
    be, air, clock, rpt = rig
    nid = add_repeater(be, rpt)
    be.login(nid, "nope")
    air.inbox += rpt.handle(air.sent.pop())
    assert any("refused" in e[1] for e in be.poll() if e[0] == "notice")
    assert not be.nodes()[0].logged_in
    be.login(nid, "x")
    clock.t += mcmesh.LOGIN_TIMEOUT + 1
    assert any("No login reply" in e[1] for e in be.poll() if e[0] == "notice")


def test_send_to_repeater_requires_login(rig):
    be, _, _, rpt = rig
    nid = add_repeater(be, rpt)
    with pytest.raises(ValueError):
        be.send("dm:" + nid, "ver")


def test_login_rules(rig):
    be, _, _, rpt = rig
    nid = add_repeater(be, rpt)
    assert not be.login(nid, "x" * 16)[0]
    assert not be.login("zzzz", "x")[0]
    be.state.add_contact(b"\x42" * 32, "Bob", W.ADV_CHAT)
    assert not be.login(be.state.find("Bob").nid, "x")[0]


def test_timestamps_never_go_backwards(rig):
    be, air, clock, rpt = rig
    nid = add_repeater(be, rpt)
    be.state.last_ts = 2_000_000_000          # an earlier run had a "later" clock
    be.login(nid, "a")
    be.login(nid, "b")
    t = [int.from_bytes(W.unseal(be.mesh.secret_for(be.nodes()[0]), W.decode(p).payload[33:])[:4],
                        "little") for p in air.sent]
    assert t[0] > 2_000_000_000 and t[1] == t[0] + 1


# --- chat with a normal contact ----------------------------------------------------

def test_dm_ack_roundtrip_and_timeout(rig):
    be, air, clock, _ = rig
    peer = Repeater()
    c, _ = be.state.add_contact(peer.pub, "Bob", W.ADV_CHAT)
    mid = be.send("dm:" + c.nid, "hello bob")
    assert ("ack", mid, "sent") in be.poll()
    (_, ttype, _, text), plain = peer.read_txt(air.sent[-1], be.state.pub)
    assert (ttype, text) == (W.TXT_PLAIN, "hello bob")
    air.inbox.append(W.Packet(W.PT_ACK, W.expected_ack(plain, be.state.pub)).encode())
    assert ("ack", mid, True) in be.poll()
    mid2 = be.send("dm:" + c.nid, "again")
    be.poll()
    clock.t += mcmesh.ACK_TIMEOUT + 1
    ev = be.poll()
    assert ("ack", mid2, False) in ev and any(e[0] == "notice" for e in ev)


def test_incoming_dm_is_acked_and_deduped(rig):
    be, air, clock, _ = rig
    peer = Repeater()
    c, _ = be.state.add_contact(peer.pub, "Bob", W.ADV_CHAT)
    frame = peer.reply_txt(be.state.pub, "yo", W.TXT_PLAIN)
    air.inbox += [frame, frame]
    ev = be.poll() + be.poll()
    assert [e[4] for e in ev if e[0] == "msg"] == ["yo"]
    ack = W.decode(air.sent[-1])
    assert ack.ptype == W.PT_ACK
    plain = W.txt_plain(1700000100, "yo", 0)
    assert ack.payload == W.expected_ack(plain, peer.pub)
    assert be.mesh.stats["dup"] == 1


def test_unknown_sender_and_wrong_recipient_ignored(rig):
    be, air, _, _ = rig
    stranger = Repeater()
    air.inbox.append(stranger.reply_txt(be.state.pub, "who am i", W.TXT_PLAIN))
    assert [e for e in be.poll() if e[0] == "msg"] == []
    assert be.mesh.stats["undecoded"] == 1
    other = bytes([be.state.pub[0] ^ 1]) + b"\x00" * 31
    pkt = W.build_peer(W.PT_TXT, bytes(32), other, stranger.pub, b"x" * 5)
    air.inbox.append(pkt.encode())
    assert be.poll() == []


def test_group_text_both_ways(rig):
    be, air, _, _ = rig
    be.send("ch:0", "anyone?")
    pkt = W.decode(air.sent[-1])
    assert pkt.ptype == W.PT_GRP_TXT and pkt.payload[0] == 0x11
    body = W.unseal(W.PUBLIC_SECRET, pkt.payload[1:])[5:].rstrip(b"\x00")
    assert body == b"vtOS: anyone?"
    air.inbox.append(pkt.encode())                         # our own echo is ignored
    assert [e for e in be.poll() if e[0] == "msg"] == []
    other = W.build_group(W.PUBLIC_SECRET, W.group_plain(1700000000, "Ann", "hi all"))
    air.inbox.append(other.encode())
    msg = [e for e in be.poll() if e[0] == "msg"][0]
    assert msg[1] == "ch:0" and msg[3] == "Ann" and msg[4] == "hi all"
    assert "dB" in msg[6]


def test_message_length_limit(rig):
    be, _, _, _ = rig
    with pytest.raises(ValueError):
        be.send("ch:0", "x" * (mcmesh.MAX_TEXT + 1))


def test_garbage_frames_are_counted(rig):
    be, air, _, _ = rig
    air.inbox += [b"\x00", os.urandom(40)]
    be.poll()
    be.poll()
    assert be.mesh.stats["bad"] >= 1


def test_set_radio_applies_and_rolls_back(rig):
    be, air, _, _ = rig
    calls = []
    be._configure = lambda radio, st: (calls.append(dict(st.radio)) or (True, "ok", ""))
    assert be.set_radio("910.525 62.5 7 5")[0] and calls
    be._configure = lambda radio, st: (False, "radio did not start", "")
    assert not be.set_radio("868 125 9 5")[0]
    assert be.state.radio["freq"] == 910.525


# --- UI controls ---------------------------------------------------------------

def typed(app, text):
    for ch in text:
        app.key(ch, T0)
    app.key(E, T0)


def screen(app, clock, cols=40, rows=16):
    return "\n".join(preview.emulate(app.render(cols, rows, clock.t), cols, rows)[0])


def test_ui_import_login_send_reply(rig):
    be, air, clock, rpt = rig
    app = meshui.App(be, clock())
    app.key("/", T0)
    app.key("i", T0)
    assert "/import" in screen(app, clock)             # command hint
    app.key("\x7f", T0)
    app.key("\x7f", T0)
    typed(app, "/import " + rpt.link())
    assert app.cur.startswith("dm:") and app.conv().title == "Hill-RPT"
    assert any("Added" in n for n in [app.notice[0]])
    typed(app, "hello")                                     # not logged in: refused
    assert app.conv().msgs[-1].state == "fail" and "log in" in app.notice[0]
    typed(app, "/login")                                    # password prompt, masked
    assert app.prompt is not None
    for ch in "hunter2":
        app.key(ch, T0)
    assert "*******" in screen(app, clock) and "hunter2" not in screen(app, clock)
    app.key(E, T0)
    air.inbox += rpt.handle(air.sent[-1])
    app.poll(clock())
    assert "Logged in" in app.notice[0]
    typed(app, "ver")
    air.inbox.append(rpt.reply_txt(be.state.pub, "v1.2"))
    app.poll(clock())
    assert app.conv().msgs[-1].text == "v1.2" and app.conv().msgs[-2].state in ("sent", "ok")


def test_ui_menu_and_node_screen_for_meshcore(rig):
    be, air, clock, rpt = rig
    add_repeater(be, rpt)
    app = meshui.App(be, clock())
    assert "Import contact" in app.menu_items() and app.menu_items()[-1] == "Quit"
    assert meshui.MENU_ITEMS[-1] == "Quit" and "Import contact" not in meshui.MENU_ITEMS
    app.open_node(app.nodes[0])
    txt = screen(app, clock)
    assert "repeater" in txt and "l login" in txt
    app.key("l", T0)
    assert app.prompt and app.prompt["mask"]


def test_ui_id_heard_radio_forget_and_help(rig):
    be, air, clock, rpt = rig
    nid = add_repeater(be, rpt)
    app = meshui.App(be, clock())
    typed(app, "/id")
    assert "meshcore://contact/add" in screen(app, clock).replace("\n", "")
    app.key("ESC", T0)
    typed(app, "/heard")
    assert "No adverts" in screen(app, clock)
    app.key("ESC", T0)
    typed(app, "/radio 1 2")
    assert app.notice[1] == meshui.RED
    typed(app, "/help")
    assert "/import" in "".join(str(l) for l in app.page[1])
    app.key("ESC", T0)
    typed(app, "/forget hill")
    assert be.nodes() == []


def test_commands_hidden_without_meshcore(rig):
    import meshback
    app = meshui.App(meshback.SimBackend(clock=Clock()), T0)
    assert app.commands() == meshui.COMMANDS + ("contacts", "contact", "discover")
    typed(app, "/import x")
    assert "Unknown command" in app.notice[0]


# --- shell command ---------------------------------------------------------------

def test_cli(keys):
    st = mcmesh.State()
    rpt = keys["rpt"]
    lines, changed = mcmesh.run_cli(st, ["id"], os.urandom)
    assert changed and lines[1].split()[1] == st.pub.hex()
    lines, changed = mcmesh.run_cli(st, ["import", rpt.link()], os.urandom)
    assert changed and "Hill-RPT" in lines[0]
    assert "repeater" in mcmesh.run_cli(st, ["list"], os.urandom)[0][0]
    assert mcmesh.run_cli(st, ["radio", "915", "250", "10", "5"], os.urandom)[1]
    assert mcmesh.run_cli(st, ["channel", "Mine", "00" * 16], os.urandom)[1]
    assert not mcmesh.run_cli(st, ["channel", "Mine", "00"], os.urandom)[1]
    assert mcmesh.run_cli(st, ["rm", "hill"], os.urandom)[1] and st.contacts == []
    assert not mcmesh.run_cli(st, [], os.urandom)[1]
