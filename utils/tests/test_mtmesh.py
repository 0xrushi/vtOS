"""Mesh engine behaviour on a simulated air: delivery, acks, relaying, retries."""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules/scripts/lib"))

import mtconfig  # noqa: E402
import mtmesh  # noqa: E402
import mtproto as P  # noqa: E402

T0 = 1_800_000_000.0


class Air:
    """Instant-delivery radio medium with a hearing matrix and a loss switch."""

    def __init__(self):
        self.radios = {}
        self.links = set()      # (src, dst) pairs that can hear each other
        self.drop = lambda src, dst, raw: False
        self.log = []

    def radio(self, name):
        r = FakeRadio(self, name)
        self.radios[name] = r
        return r

    def hears(self, a, b):
        self.links.add((a, b))
        self.links.add((b, a))


class FakeRadio:
    def __init__(self, air, name):
        self.air = air
        self.name = name
        self.inbox = []
        self.busy = False
        self.sent = 0

    def transmit(self, raw):
        self.sent += 1
        self.air.log.append((self.name, bytes(raw)))
        for other, r in self.air.radios.items():
            if (self.name, other) in self.air.links and not self.air.drop(self.name, other, raw):
                r.inbox.append(bytes(raw))

    def receive(self):
        return self.inbox.pop(0) if self.inbox else None

    def start_receive(self):
        pass

    def snr(self):
        return 5.0

    def rssi(self):
        return -80.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def make_node(air, clock, name, num, **cfg):
    s = mtconfig.Settings()
    s.long_name = name
    s.short_name = name[:4]
    s.nodeinfo_interval = 0
    s.telem_interval = 0
    for k, v in cfg.items():
        setattr(s, k, v)
    return mtmesh.Mesh(air.radio(name), s, num, clock, random.Random(num))


def run(nodes, clock, seconds, step=0.2):
    events = {n: [] for n in nodes}
    end = clock.t + seconds
    while clock.t < end:
        for name, m in nodes.items():
            events[name] += m.poll()
        clock.t += step
    return events


def kinds(evs, kind):
    return [e for e in evs if e[0] == kind]


@pytest.fixture
def pair():
    air, clock = Air(), Clock()
    a = make_node(air, clock, "alice", 0xA1)
    b = make_node(air, clock, "bob", 0xB2)
    air.hears("alice", "bob")
    return air, clock, {"alice": a, "bob": b}


def test_text_delivery_and_direct_ack(pair):
    air, clock, nodes = pair
    pid = nodes["alice"].send_text(0xB2, "hello bob")
    ev = run(nodes, clock, 5)
    text = kinds(ev["bob"], "text")[0]
    assert text[3] == 0xA1 and text[4] == "hello bob" and text[1] == 0 and text[2] == 0xB2
    assert ("sent", pid) in ev["alice"]
    assert ("ack", pid, True, P.ERR_NONE) in ev["alice"]
    assert nodes["alice"].pending == {}


def test_broadcast_text_reaches_everyone_and_is_not_dm(pair):
    air, clock, nodes = pair
    nodes["alice"].send_text(P.BROADCAST, "all hands", want_ack=False)
    ev = run(nodes, clock, 5)
    t = kinds(ev["bob"], "text")[0]
    assert t[2] == P.BROADCAST and t[4] == "all hands"


def test_node_info_and_position_populate_node_db(pair):
    air, clock, nodes = pair
    nodes["alice"].cfg.pos_mode = "fixed"
    nodes["alice"].cfg.fixed_lat = 37.7749
    nodes["alice"].cfg.fixed_lon = -122.4194
    nodes["alice"].cfg.fixed_alt = 15
    nodes["alice"].send_nodeinfo()
    nodes["alice"].send_position()
    run(nodes, clock, 5)
    n = nodes["bob"].nodes[0xA1]
    assert n.long_name == "alice" and n.short_name == "alic" and n.hw == P.HW_T_DECK
    assert n.lat == pytest.approx(37.7749, abs=1e-6) and n.lon == pytest.approx(-122.4194, abs=1e-6)
    assert n.alt == 15 and n.snr == 5.0 and n.hops == 0


def test_telemetry_updates_battery(pair):
    air, clock, nodes = pair
    nodes["alice"].sensors = lambda: {"battery": 73, "voltage": 3.95}
    nodes["alice"].send_telemetry()
    run(nodes, clock, 5)
    n = nodes["bob"].nodes[0xA1]
    assert n.batt == 73 and n.volt == pytest.approx(3.95, abs=0.01) and n.uptime is not None


def test_position_precision_blurs_coordinates(pair):
    air, clock, nodes = pair
    a = nodes["alice"]
    a.cfg.pos_mode = "fixed"
    a.cfg.fixed_lat, a.cfg.fixed_lon = 37.7749, -122.4194
    a.cfg.pos_precision = 13
    a.send_position()
    run(nodes, clock, 5)
    n = nodes["bob"].nodes[0xA1]
    assert n.lat != 37.7749 and abs(n.lat - 37.7749) < 0.05
    assert abs(n.lon + 122.4194) < 0.05


def test_relay_through_middle_node_with_hop_decrement():
    air, clock = Air(), Clock()
    nodes = {"alice": make_node(air, clock, "alice", 0xA1),
             "relay": make_node(air, clock, "relay", 0xC3),
             "bob": make_node(air, clock, "bob", 0xB2)}
    air.hears("alice", "relay")
    air.hears("relay", "bob")                       # alice and bob can't hear each other
    pid = nodes["alice"].send_text(0xB2, "via relay")
    ev = run(nodes, clock, 30)
    assert kinds(ev["bob"], "text")[0][4] == "via relay"
    assert ("ack", pid, True, P.ERR_NONE) in ev["alice"]       # ack came back through the relay
    relayed = [raw for who, raw in air.log if who == "relay"]
    first = P.unpack_header(relayed[0])
    assert first["hop_limit"] == 2 and first["hop_start"] == 3 and first["relay"] == 0xC3
    assert nodes["bob"].nodes[0xA1].hops == 1


def test_duplicates_are_suppressed_and_rebroadcast_cancelled():
    air, clock = Air(), Clock()
    nodes = {n: make_node(air, clock, n, 0x10 + i) for i, n in enumerate("abcd")}
    for x in "abcd":
        for y in "abcd":
            if x < y:
                air.hears(x, y)                         # everyone hears everyone
    pid = nodes["a"].send_text(P.BROADCAST, "storm", want_ack=False)
    ev = run(nodes, clock, 30)
    for n in "bcd":
        assert len(kinds(ev[n], "text")) == 1            # delivered once, not once per relay
    sent = [1 for _, raw in air.log if P.unpack_header(raw)["id"] == pid]
    assert len(sent) <= 3                                # original + at most a collision's worth


def test_strong_signal_waits_longer_to_rebroadcast():
    air, clock = Air(), Clock()
    m = make_node(air, clock, "r", 0x10)
    weak = max(m._rebroadcast_delay(-18.0) for _ in range(200))
    strong = max(m._rebroadcast_delay(9.0) for _ in range(200))
    assert strong > weak * 4


def test_router_role_relays_faster_than_client():
    air, clock = Air(), Clock()
    client = make_node(air, clock, "c", 1)
    router = make_node(air, clock, "r", 2, role="ROUTER")
    assert max(router._rebroadcast_delay(9.0) for _ in range(200)) < \
        max(client._rebroadcast_delay(9.0) for _ in range(200))


def test_client_mute_does_not_rebroadcast():
    air, clock = Air(), Clock()
    nodes = {"alice": make_node(air, clock, "alice", 1),
             "mute": make_node(air, clock, "mute", 2, role="CLIENT_MUTE"),
             "bob": make_node(air, clock, "bob", 3)}
    air.hears("alice", "mute")
    air.hears("mute", "bob")
    nodes["alice"].send_text(P.BROADCAST, "psst", want_ack=False)
    ev = run(nodes, clock, 20)
    assert kinds(ev["mute"], "text") and not kinds(ev["bob"], "text")


def test_zero_hop_limit_is_not_relayed():
    air, clock = Air(), Clock()
    nodes = {"alice": make_node(air, clock, "alice", 1, hop_limit=1),
             "relay": make_node(air, clock, "relay", 2),
             "far": make_node(air, clock, "far", 3),
             "farther": make_node(air, clock, "farther", 4)}
    air.hears("alice", "relay")
    air.hears("relay", "far")
    air.hears("far", "farther")
    nodes["alice"].send_text(P.BROADCAST, "short leash", want_ack=False)
    ev = run(nodes, clock, 40)
    assert kinds(ev["far"], "text")                      # hop 1 reaches the relay's neighbour
    assert not kinds(ev["farther"], "text")              # hop_limit exhausted


def test_lost_packet_is_retransmitted_then_acked(pair):
    air, clock, nodes = pair
    drops = {"n": 0}

    def drop(src, dst, raw):
        if src == "alice" and drops["n"] < 2:
            drops["n"] += 1
            return True
        return False

    air.drop = drop
    pid = nodes["alice"].send_text(0xB2, "persistent")
    ev = run(nodes, clock, 60)
    assert ("ack", pid, True, P.ERR_NONE) in ev["alice"]
    assert nodes["alice"].radio.sent == 3                # 1 try + 2 retries
    assert len(kinds(ev["bob"], "text")) == 1


def test_gives_up_after_three_retransmissions(pair):
    air, clock, nodes = pair
    air.drop = lambda s, d, r: s == "alice"
    pid = nodes["alice"].send_text(0xB2, "into the void")
    ev = run(nodes, clock, 120)
    assert ("ack", pid, False, P.ERR_MAX_RETRANSMIT) in ev["alice"]
    assert nodes["alice"].radio.sent == 1 + mtmesh.MAX_RETX


def test_broadcast_gets_implicit_ack_when_a_neighbour_relays():
    air, clock = Air(), Clock()
    nodes = {"alice": make_node(air, clock, "alice", 1), "relay": make_node(air, clock, "relay", 2)}
    air.hears("alice", "relay")
    pid = nodes["alice"].send_text(P.BROADCAST, "anyone?")
    ev = run(nodes, clock, 30)
    assert ("ack", pid, True, P.ERR_NONE) in ev["alice"]
    assert nodes["alice"].radio.sent == 1                # no retry needed


def test_wrong_channel_key_is_not_delivered_but_counted():
    air, clock = Air(), Clock()
    a = make_node(air, clock, "alice", 1)
    b = make_node(air, clock, "bob", 2)
    b.cfg.channels[0].psk = bytes(range(16))
    air.hears("alice", "bob")
    a.send_text(P.BROADCAST, "secret", want_ack=False)
    ev = run({"alice": a, "bob": b}, clock, 10)
    assert not kinds(ev["bob"], "text") and b.stats["undecoded"] == 1


def test_secondary_channel_message_tagged_with_channel_index():
    air, clock = Air(), Clock()
    nodes = {"alice": make_node(air, clock, "alice", 1), "bob": make_node(air, clock, "bob", 2)}
    key = bytes(range(32))
    for n in nodes.values():
        n.cfg.channels.append(mtconfig.Channel("Hike", key))
    air.hears("alice", "bob")
    nodes["alice"].send_text(P.BROADCAST, "on the hike channel", channel=1, want_ack=False)
    ev = run(nodes, clock, 10)
    t = kinds(ev["bob"], "text")[0]
    assert t[1] == 1 and t[4] == "on the hike channel"


def test_tx_disabled_never_transmits_and_reports_failure(pair):
    air, clock, nodes = pair
    nodes["alice"].cfg.tx_enabled = False
    pid = nodes["alice"].send_text(0xB2, "nope")
    ev = run(nodes, clock, 5)
    assert nodes["alice"].radio.sent == 0
    assert ("ack", pid, False, P.ERR_NO_INTERFACE) in ev["alice"]


def test_eu_duty_cycle_limit_blocks_excess_airtime():
    air, clock = Air(), Clock()
    a = make_node(air, clock, "alice", 1, region="EU_868")
    a.cfg.channels[0].name = ""
    air.hears("alice", "alice")
    fails = 0
    for i in range(400):
        a.send_text(P.BROADCAST, "x" * 200, want_ack=False)
        for ev in run({"a": a}, clock, 1)["a"]:
            if ev[0] == "ack" and ev[2] is False and ev[3] == P.ERR_DUTY_CYCLE_LIMIT:
                fails += 1
    assert fails > 0 and a.tx_util() <= 10.5


def test_ignored_node_text_is_dropped(pair):
    air, clock, nodes = pair
    nodes["bob"].node(0xA1).ignored = True
    nodes["alice"].send_text(P.BROADCAST, "spam", want_ack=False)
    ev = run(nodes, clock, 5)
    assert not kinds(ev["bob"], "text")


def test_nodeinfo_request_gets_a_reply_once(pair):
    air, clock, nodes = pair
    nodes["alice"].send_nodeinfo(0xB2, want_response=True)
    run(nodes, clock, 10)
    assert nodes["alice"].nodes[0xB2].long_name == "bob"
    nodes["alice"].send_nodeinfo(0xB2, want_response=True)
    before = nodes["bob"].radio.sent
    run(nodes, clock, 10)
    assert nodes["bob"].radio.sent == before             # rate limited


def test_traceroute_gets_a_reply(pair):
    air, clock, nodes = pair
    nodes["alice"].traceroute(0xB2)
    ev = run(nodes, clock, 10)
    assert kinds(ev["alice"], "trace")


def test_periodic_nodeinfo_announcement():
    air, clock = Air(), Clock()
    a = make_node(air, clock, "alice", 1, nodeinfo_interval=3600)
    b = make_node(air, clock, "bob", 2)
    air.hears("alice", "bob")
    run({"alice": a, "bob": b}, clock, 30)
    assert b.nodes[1].long_name == "alice"


def test_node_database_round_trip(pair):
    air, clock, nodes = pair
    nodes["alice"].send_nodeinfo()
    run(nodes, clock, 5)
    rows = nodes["bob"].export_nodes()
    other = make_node(Air(), Clock(), "carol", 3)
    other.import_nodes(rows)
    assert other.nodes[0xA1].long_name == "alice"


def test_garbage_and_foreign_packets_do_not_crash(pair):
    air, clock, nodes = pair
    r = nodes["bob"].radio
    r.inbox += [b"", b"\x00" * 5, bytes(range(40)), b"\xff" * 255]
    run(nodes, clock, 2)
    assert nodes["bob"].stats["rx"] >= 3
