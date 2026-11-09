#
# Meshtastic mesh engine: receive, decrypt, dedup, flood rebroadcast, acks and
# retransmission, node database, periodic announcements.
# License: MIT
#
# Hardware-free: it talks to a `radio` object (transmit/receive/start_receive/
# snr/rssi, optionally cad) and a clock, so utils/tests/test_mtmesh.py can run
# several nodes against a simulated air. poll() never blocks except inside
# radio.transmit().
#
# Behaviour follows the public Meshtastic design: managed flooding with a
# hop limit, SNR-weighted rebroadcast delay (nodes that heard it loudest wait
# longest), duplicate suppression by (sender, packet id), want_ack with up to
# three retransmissions, implicit acks from hearing a neighbour relay your
# broadcast, ROUTING_APP acks for direct messages.
#

import time

import mtproto as P
import mtradio as R

MAX_RETX = 3
SEEN_TTL = 600.0
SEEN_MAX = 256
MAX_NODES = 100
REBROADCAST_ROLES = ("CLIENT", "ROUTER", "ROUTER_CLIENT", "REPEATER", "CLIENT_HIDDEN")
NODEINFO_REPLY_GAP = 60.0
FIRST_ANNOUNCE = 15.0
CAD_TRIES = 6


def _text(raw):
    """UTF-8 text, or '?' for bytes that aren't (works without errors='replace')."""
    try:
        return bytes(raw).decode()
    except UnicodeError:
        return "".join(chr(b) if 32 <= b < 127 else "?" for b in raw)


class NodeInfo:
    """One entry in the node database. nid/name/batt feed the UI directly."""

    def __init__(self, num):
        self.num = num
        self.long_name = ""
        self.short_name = ""
        self.hw = 0
        self.role = 0
        self.snr = None
        self.rssi = None
        self.hops = None
        self.heard = None
        self.lat = None
        self.lon = None
        self.alt = None
        self.pos_time = None
        self.batt = None
        self.volt = None
        self.chutil = None
        self.airutil = None
        self.uptime = None
        self.fav = False
        self.ignored = False
        self.via_mqtt = False

    @property
    def nid(self):
        return P.node_id_str(self.num)

    @property
    def name(self):
        return self.long_name or self.short_name or self.nid

    def to_dict(self):
        keys = ("long_name", "short_name", "hw", "role", "snr", "heard", "lat", "lon",
                "alt", "pos_time", "batt", "volt", "fav", "ignored", "hops")
        d = {k: getattr(self, k) for k in keys}
        d["num"] = self.num
        return d

    @classmethod
    def from_dict(cls, d):
        n = cls(d["num"])
        for k, v in d.items():
            if k != "num":
                setattr(n, k, v)
        return n


class _Tx:
    def __init__(self, raw, due, pid, own=False, key=None):
        self.raw = raw
        self.due = due
        self.pid = pid
        self.own = own          # a packet we originated (reports "sent")
        self.key = key          # (from, id) for rebroadcasts, so they can be cancelled
        self.cad = 0


class Mesh:
    def __init__(self, radio, cfg, me, clock=None, rng=None, sensors=None, gps=None, unix_offset=0):
        self.radio = radio
        self.cfg = cfg
        self.me = me
        self.unix_offset = unix_offset      # added to clock() for on-air timestamps
        self.clock = clock or time.time
        if rng is None:
            import random as rng
        self.rng = rng
        self.sensors = sensors or (lambda: {})
        self.gps = gps or (lambda: None)
        self.nodes = {}
        self.seen = {}
        self.txq = []
        self.rebroadcasts = {}
        self.pending = {}
        self.air = []             # (time, ms, is_tx) over the last hour
        self.stats = {"rx": 0, "tx": 0, "dup": 0, "bad": 0, "undecoded": 0,
                      "relayed": 0, "acked": 0, "failed": 0}
        self.started = self.clock()
        self._next = {"nodeinfo": self.started + FIRST_ANNOUNCE if cfg.nodeinfo_interval else None,
                      "position": None, "telemetry": None}
        self._reply_gap = {}
        self.events = []
        self.reconfigure()

    # -- configuration --

    def reconfigure(self):
        bw, sf, cr = self.cfg.modem()
        self._bw, self._sf, self._cr = bw, sf, cr
        now = self.clock()
        if self.cfg.pos_mode != "off" and self.cfg.pos_interval:
            self._next["position"] = self._next["position"] or now + 45.0
        else:
            self._next["position"] = None
        if self.cfg.telem_interval:
            self._next["telemetry"] = self._next["telemetry"] or now + 60.0
        else:
            self._next["telemetry"] = None

    def _airtime(self, nbytes):
        return R.airtime_ms(nbytes, self._bw, self._sf, self._cr)

    def _rnd(self, n):
        return self.rng.getrandbits(16) % max(1, n)

    def _new_id(self):
        pid = self.rng.getrandbits(32)
        return pid or 1

    # -- node database --

    def node(self, num):
        n = self.nodes.get(num)
        if n is None:
            if len(self.nodes) >= MAX_NODES:
                victims = [k for k, v in self.nodes.items() if not v.fav]
                if victims:
                    victims.sort(key=lambda k: self.nodes[k].heard or 0)
                    del self.nodes[victims[0]]
            n = self.nodes[num] = NodeInfo(num)
        return n

    def export_nodes(self):
        return [n.to_dict() for n in self.nodes.values()]

    def import_nodes(self, rows):
        for d in rows:
            try:
                self.nodes[d["num"]] = NodeInfo.from_dict(d)
            except (KeyError, TypeError):
                pass

    # -- main loop --

    def poll(self):
        """Do one pass of work. Returns a list of events (see _emit)."""
        now = self.clock()
        for _ in range(4):
            raw = self.radio.receive()
            if not raw:
                break
            snr = self._call(self.radio.snr)
            rssi = self._call(self.radio.rssi)
            self._rx(bytes(raw), snr, rssi, now)
            self.radio.start_receive()
        self._announce(now)
        self._retransmit(now)
        self._pump(now)
        out = self.events
        self.events = []
        return out

    @staticmethod
    def _call(fn):
        try:
            return fn()
        except Exception:
            return None

    def _emit(self, *ev):
        self.events.append(ev)

    # -- receive --

    def _rx(self, raw, snr, rssi, now):
        self.stats["rx"] += 1
        self._log_air(now, self._airtime(len(raw)), False)
        try:
            h = P.unpack_header(raw)
        except ValueError:
            self.stats["bad"] += 1
            return
        if h["from"] == self.me:
            self._implicit_ack(h)
            return
        key = (h["from"], h["id"])
        if len(self.seen) > SEEN_MAX:
            for k in [k for k, t in self.seen.items() if now - t > SEEN_TTL]:
                del self.seen[k]
            while len(self.seen) > SEEN_MAX:
                del self.seen[next(iter(self.seen))]
        if key in self.seen:
            self.stats["dup"] += 1
            self._cancel_rebroadcast(key)
            return
        self.seen[key] = now

        data = None
        chan = None
        body = raw[P.HDR_LEN:]
        for i in range(len(self.cfg.channels)):
            if self.cfg.chan_hash(i) != h["channel"]:
                continue
            try:
                data = P.open_data(h, body, self.cfg.key(i))
                chan = i
                break
            except ValueError:
                continue
        if data is None:
            self.stats["undecoded"] += 1
        else:
            self._deliver(h, data, chan, snr, rssi, now)
        self._maybe_rebroadcast(h, raw, snr, now, key)

    def _implicit_ack(self, h):
        """We heard our own packet come back: a neighbour relayed our broadcast."""
        p = self.pending.get(h["id"])
        if p and p["bcast"]:
            del self.pending[h["id"]]
            self.stats["acked"] += 1
            self._emit("ack", h["id"], True, P.ERR_NONE)

    def _deliver(self, h, data, chan, snr, rssi, now):
        frm = h["from"]
        n = self.node(frm)
        n.heard = now
        n.snr = snr
        n.rssi = rssi
        n.hops = (h["hop_start"] - h["hop_limit"]) if h["hop_start"] else None
        n.via_mqtt = h["via_mqtt"]
        port = data["portnum"]
        to_me = h["to"] == self.me
        bcast = h["to"] == P.BROADCAST
        if not (to_me or bcast):
            if port == P.PORT_ROUTING:      # acks for others still teach us nothing
                return
            self._emit("node", frm)
            return
        payload = data["payload"]
        try:
            if port == P.PORT_TEXT:
                self._on_text(h, data, chan, snr, now, to_me)
            elif port == P.PORT_NODEINFO:
                self._on_nodeinfo(h, n, payload, data, now)
            elif port == P.PORT_POSITION:
                self._on_position(h, n, payload, data, now)
            elif port == P.PORT_TELEMETRY:
                self._on_telemetry(n, payload)
            elif port == P.PORT_ROUTING:
                self._on_routing(data, frm)
            elif port == P.PORT_TRACEROUTE:
                self._on_traceroute(h, data, frm, chan)
            else:
                self._emit("other", frm, port)
        except (ValueError, UnicodeError):
            self.stats["bad"] += 1
        self._emit("node", frm)

    def _on_text(self, h, data, chan, snr, now, to_me):
        text = _text(data["payload"])
        n = self.nodes[h["from"]]
        if n.ignored:
            return
        hops = n.hops
        self._emit("text", chan, h["to"], h["from"], text, h["id"], snr, hops, now)
        if to_me and h["want_ack"]:
            self._send_ack(h, chan)

    def _on_nodeinfo(self, h, n, payload, data, now):
        u = P.decode(P.USER, payload)
        n.long_name = u["long_name"]
        n.short_name = u["short_name"]
        n.hw = u["hw_model"]
        n.role = u["role"]
        if data["want_response"] and self._reply_ok(h["from"], now):
            self.send_nodeinfo(h["from"], channel=0)

    def _on_position(self, h, n, payload, data, now):
        p = P.decode(P.POSITION, payload)
        if p["latitude_i"] or p["longitude_i"]:
            n.lat = p["latitude_i"] / 1e7
            n.lon = p["longitude_i"] / 1e7
            n.alt = p["altitude"] or p["altitude_hae"]
            n.pos_time = now
        if data["want_response"] and h["to"] == self.me and self._reply_ok(h["from"], now):
            self.send_position(h["from"])

    def _on_telemetry(self, n, payload):
        t = P.decode(P.TELEMETRY, payload)
        dm = t["device_metrics"]
        if dm:
            n.batt = dm["battery_level"]
            n.volt = dm["voltage"]
            n.chutil = dm["channel_utilization"]
            n.airutil = dm["air_util_tx"]
            n.uptime = dm["uptime_seconds"]

    def _on_routing(self, data, frm):
        rid = data["request_id"]
        err = P.decode(P.ROUTING, data["payload"])["error_reason"]
        p = self.pending.pop(rid, None)
        if p is None:
            return
        if err == P.ERR_NONE:
            self.stats["acked"] += 1
        else:
            self.stats["failed"] += 1
        self._emit("ack", rid, err == P.ERR_NONE, err)

    def _on_traceroute(self, h, data, frm, chan):
        rd = P.decode(P.ROUTE_DISCOVERY, data["payload"])
        if data["request_id"]:
            self.pending.pop(data["request_id"], None)
            self._emit("trace", frm, rd["route"], rd["route_back"])
        elif h["to"] == self.me and data["want_response"]:
            reply = P.encode(P.ROUTE_DISCOVERY, {"route": rd["route"]})
            self._send_data(frm, P.PORT_TRACEROUTE, reply, chan, request_id=h["id"])

    def _reply_ok(self, num, now):
        last = self._reply_gap.get(num, -1e9)
        if now - last < NODEINFO_REPLY_GAP:
            return False
        self._reply_gap[num] = now
        return True

    # -- rebroadcast (flooding) --

    def _maybe_rebroadcast(self, h, raw, snr, now, key):
        if not self.cfg.tx_enabled or self.cfg.role not in REBROADCAST_ROLES:
            return
        if h["to"] == self.me or h["hop_limit"] <= 0 or h["via_mqtt"]:
            return
        hdr = dict(h)
        hdr["hop_limit"] = h["hop_limit"] - 1
        hdr["relay"] = self.me & 0xFF
        hdr["hop_start"] = h["hop_start"] or h["hop_limit"]
        out = P.pack_header(hdr["to"], hdr["from"], hdr["id"], hdr["hop_limit"],
                            hdr["want_ack"], hdr["via_mqtt"], hdr["hop_start"],
                            hdr["channel"], hdr["next_hop"], hdr["relay"]) + raw[P.HDR_LEN:]
        item = _Tx(out, now + self._rebroadcast_delay(snr), h["id"], False, key)
        self.rebroadcasts[key] = item
        self.txq.append(item)

    def _rebroadcast_delay(self, snr):
        """Louder signals wait longer, so distant nodes get to relay first."""
        slot = max(0.010, 2.5 * (1 << self._sf) / self._bw / 1000.0)
        if self.cfg.role in ("ROUTER", "ROUTER_CLIENT", "REPEATER"):
            cw = 2
        else:
            s = -20.0 if snr is None else max(-20.0, min(10.0, snr))
            cw = 3 + int((s + 20.0) / 30.0 * 5.0 + 0.5)       # 3 (weak) .. 8 (strong)
        return self._rnd(1 << cw) * slot

    def _cancel_rebroadcast(self, key):
        item = self.rebroadcasts.pop(key, None)
        if item is not None and item in self.txq:
            self.txq.remove(item)

    # -- sending --

    def send_text(self, to, text, channel=0, want_ack=None):
        if want_ack is None:
            want_ack = (to != P.BROADCAST) or self.cfg.ack_channel
        return self._send_data(to, P.PORT_TEXT, text.encode("utf-8"), channel,
                               want_ack=want_ack)

    def send_nodeinfo(self, to=P.BROADCAST, channel=0, want_response=False):
        long_name, short = self.cfg.display_names()
        role = dict(P.ROLES).get(self.cfg.role, 0)
        user = P.encode(P.USER, {"id": P.node_id_str(self.me), "long_name": long_name,
                                 "short_name": short, "hw_model": P.HW_T_DECK,
                                 "role": role})
        return self._send_data(to, P.PORT_NODEINFO, user, channel,
                               want_response=want_response)

    def send_position(self, to=P.BROADCAST, channel=0):
        fix = self._position()
        if fix is None:
            return None
        bits = self.cfg.pos_precision
        lat = self._fuzz(int(fix[0] * 1e7), bits)
        lon = self._fuzz(int(fix[1] * 1e7), bits)
        pos = P.encode(P.POSITION, {"latitude_i": lat, "longitude_i": lon,
                                    "altitude": int(fix[2] or 0), "time": int(fix[3] or 0),
                                    "precision_bits": bits if bits < 32 else 0,
                                    "sats_in_view": fix[4] or 0})
        return self._send_data(to, P.PORT_POSITION, pos, channel)

    @staticmethod
    def _fuzz(v, bits):
        """Drop low bits so the shared position is only as exact as configured."""
        if bits >= 32:
            return v
        u = v & 0xFFFFFFFF
        u &= (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
        u += 1 << (31 - bits)
        return u - (1 << 32) if u >= 1 << 31 else u

    def _position(self):
        if self.cfg.pos_mode == "fixed":
            return (self.cfg.fixed_lat, self.cfg.fixed_lon, self.cfg.fixed_alt,
                    int(self.clock()) + self.unix_offset, 0)
        if self.cfg.pos_mode == "gps":
            return self.gps()
        return None

    def send_telemetry(self, channel=0):
        s = self.sensors()
        dm = {"battery_level": int(s.get("battery", 0)), "voltage": float(s.get("voltage", 0.0)),
              "channel_utilization": self.channel_util(), "air_util_tx": self.tx_util(),
              "uptime_seconds": int(self.clock() - self.started)}
        t = P.encode(P.TELEMETRY, {"time": int(self.clock()) + self.unix_offset, "device_metrics": dm})
        return self._send_data(P.BROADCAST, P.PORT_TELEMETRY, t, channel)

    def request_position(self, to, channel=0):
        return self._send_data(to, P.PORT_POSITION, b"", channel, want_response=True)

    def traceroute(self, to, channel=0):
        pid = self._send_data(to, P.PORT_TRACEROUTE, P.encode(P.ROUTE_DISCOVERY, {}),
                              channel, want_response=True, want_ack=False)
        return pid

    def _send_ack(self, h, chan):
        routing = P.encode(P.ROUTING, {"error_reason": P.ERR_NONE})
        hops = (h["hop_start"] - h["hop_limit"]) if h["hop_start"] else 0
        limit = min(7, max(hops + 1, 1)) if h["hop_start"] else self.cfg.hop_limit
        self._send_data(h["from"], P.PORT_ROUTING, routing, chan,
                        request_id=h["id"], hop_limit=limit)

    def _send_data(self, to, port, payload, channel, want_ack=False, want_response=False,
                   request_id=0, hop_limit=None):
        if channel >= len(self.cfg.channels):
            channel = 0
        pid = self._new_id()
        limit = self.cfg.hop_limit if hop_limit is None else hop_limit
        hdr = {"to": to, "from": self.me, "id": pid, "hop_limit": limit,
               "hop_start": limit, "want_ack": want_ack,
               "channel": self.cfg.chan_hash(channel), "relay": self.me & 0xFF}
        raw = P.seal(hdr, P.data_bytes(port, payload, want_response, request_id=request_id),
                     self.cfg.key(channel))
        now = self.clock()
        self.seen[(self.me, pid)] = now
        self.txq.append(_Tx(raw, now, pid, True))
        if want_ack:
            self.pending[pid] = {"raw": raw, "tries": 0, "next": None,
                                 "bcast": to == P.BROADCAST}
        return pid

    # -- transmit pump --

    def _pump(self, now):
        if not self.txq:
            return
        item = min(self.txq, key=lambda t: t.due)
        if item.due > now:
            return
        if not self.cfg.tx_enabled:
            self.txq.remove(item)
            self._fail(item, P.ERR_NO_INTERFACE)
            return
        cad = getattr(self.radio, "cad", None)
        if cad is not None and item.cad < CAD_TRIES:
            busy = self._call(cad)
            self.radio.start_receive()
            if busy:
                item.cad += 1
                item.due = now + 0.05 + self._rnd(400) / 1000.0
                return
        air = self._airtime(len(item.raw))
        if not self._duty_ok(now, air):
            self.txq.remove(item)
            self._fail(item, P.ERR_DUTY_CYCLE_LIMIT)
            return
        self.txq.remove(item)
        if item.key is not None:
            self.rebroadcasts.pop(item.key, None)
            self.stats["relayed"] += 1
        try:
            self.radio.transmit(item.raw)
        except Exception:
            self.radio.start_receive()
            self._fail(item, P.ERR_NO_INTERFACE)
            return
        self.radio.start_receive()
        self.stats["tx"] += 1
        self._log_air(now, air, True)
        if item.own:
            p = self.pending.get(item.pid)
            if p is not None:
                p["next"] = now + self._retry_wait(air)
            self._emit("sent", item.pid)

    def _fail(self, item, err):
        if item.own:
            self.pending.pop(item.pid, None)
            self.stats["failed"] += 1
            self._emit("ack", item.pid, False, err)

    def _retry_wait(self, air_ms):
        return 6.0 + 4.0 * air_ms / 1000.0 + self._rnd(2000) / 1000.0

    def _retransmit(self, now):
        for pid in list(self.pending):
            p = self.pending[pid]
            if p["next"] is None or now < p["next"]:
                continue
            if p["tries"] >= MAX_RETX:
                del self.pending[pid]
                self.stats["failed"] += 1
                self._emit("ack", pid, False, P.ERR_MAX_RETRANSMIT)
                continue
            p["tries"] += 1
            p["next"] = None
            self.txq.append(_Tx(p["raw"], now, pid, True))

    # -- periodic announcements --

    def _announce(self, now):
        if not self.cfg.tx_enabled:
            return
        nxt = self._next
        if nxt["nodeinfo"] is not None and now >= nxt["nodeinfo"]:
            iv = self.cfg.nodeinfo_interval
            nxt["nodeinfo"] = now + iv if iv else None
            self.send_nodeinfo(want_response=False)
        if nxt["position"] is not None and now >= nxt["position"]:
            nxt["position"] = now + self.cfg.pos_interval
            self.send_position()
        if nxt["telemetry"] is not None and now >= nxt["telemetry"]:
            nxt["telemetry"] = now + self.cfg.telem_interval
            self.send_telemetry()

    # -- airtime accounting --

    def _log_air(self, now, ms, is_tx):
        self.air.append((now, ms, is_tx))
        cutoff = now - 3600.0
        while self.air and self.air[0][0] < cutoff:
            self.air.pop(0)

    def channel_util(self):
        now = self.clock()
        ms = sum(a[1] for a in self.air if now - a[0] <= 60.0)
        return min(100.0, ms / 600.0)

    def tx_util(self):
        now = self.clock()
        ms = sum(a[1] for a in self.air if a[2] and now - a[0] <= 3600.0)
        return min(100.0, ms / 36000.0)

    def _duty_ok(self, now, air_ms):
        reg = self.cfg.reg()
        if reg is None or reg[5] >= 100 or self.cfg.override_duty:
            return True
        used = sum(a[1] for a in self.air if a[2] and now - a[0] <= 3600.0)
        return used + air_ms <= reg[5] * 36000.0
