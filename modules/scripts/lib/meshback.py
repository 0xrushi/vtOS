#
# Meshchat backends: the only place that knows how to talk to a mesh.
# License: MIT
#
# A backend is any object with:
#   poll()                -> [event]   non-blocking; events are tuples:
#                              ("msg", conv_key, node_id, name, text, ts[, meta])
#                              ("ack", message_id, ok)   ok: True | False | "sent"
#                                  ("sent" = left the radio, delivery unknown)
#                              ("node",)            node list or telemetry changed
#                              ("notice", text[, colour])   one-line status message
#   send(conv_key, text)  -> message_id
#   nodes()               -> [NodeInfo-like: nid name hops snr heard batt fav ignored ...]
#   channels()            -> [(conv_key, title)]   first one is the home chat
#   info()                -> {"name", "region", "preset", "batt", "link", "demo"}
#   close()
# Optional, enabling the settings screens and node actions:
#   config (mtconfig.Settings) and apply(settings) -> (ok, message)
#   stats() -> dict, traceroute(nid), request_position(nid), request_nodeinfo(nid),
#   share_position() / announce() -> (ok, message), my_position()
#
# conv_key is "ch:<n>" for channel n and "dm:<node_id>" for a direct message.
#

import time

import mtconfig
import mtmesh
import mtproto as P
import mtradio as R
from mtmesh import NodeInfo
from meshdraw import GREEN, YELLOW, RED, DIM

UNIX_OFFSET = 946684800     # MicroPython's clock counts from 2000, Meshtastic from 1970


def _nid(num):
    return P.node_id_str(num)


def _num(nid):
    return int(nid.lstrip("!"), 16)


class SimBackend:
    """Scripted demo mesh so the UI can be used and judged without a radio."""

    demo = True

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self.t0 = self._clock()
        self._mid = 0
        self._sent = 0
        self._due = []           # (due_time, event)
        self._last_drift = self.t0
        self._base = {}
        self.config = mtconfig.Settings()
        self.config.node_num = 0xDE0DE0
        self.config.long_name = "vtOS demo"
        self.config.short_name = "DEMO"
        self.config.channels.append(mtconfig.Channel("Hike", bytes(range(16))))
        t0 = self.t0
        def node(num, name, short, hops, snr, ago, batt, lat=None, lon=None):
            n = NodeInfo(num)
            n.long_name, n.short_name, n.hops, n.snr = name, short, hops, snr
            n.heard, n.batt, n.hw, n.volt = t0 - ago, batt, P.HW_T_DECK, 3.2 + batt / 100.0
            if lat is not None:
                n.lat, n.lon, n.alt, n.pos_time = lat, lon, 12, t0 - ago
            return n
        self._nodes = [
            node(0xB0B1, "Bob", "BOB", 0, 9.0, 30, 64, 37.7790, -122.4180),
            node(0xA1F3, "Alice", "ALIC", 1, 6.5, 120, 88, 37.7850, -122.4090),
            node(0x5D09, "Sam", "SAM", 0, 2.0, 400, 41),
            node(0x7C20, "Ridge-R", "RIDG", 3, -4.0, 840, 91, 37.8100, -122.4700),
            node(0xC4BE, "Cabin", "CABN", 2, -12.5, 5400, 17),
        ]
        for n in self._nodes:
            self._base[n.num] = n.snr
        bob, alice, sam, ridge = (_nid(0xB0B1), _nid(0xA1F3), _nid(0x5D09), _nid(0x7C20))
        # (offset seconds from start, event without timestamp); negatives are history
        self._script = [
            (-420, ("msg", "ch:0", alice, "Alice", "Anyone near the ridge?", "1 hop +6dB")),
            (-380, ("msg", "ch:0", ridge, "Ridge-R", "Yes, 2 hops from you", "3 hops -4dB")),
            (-200, ("msg", "ch:0", bob, "Bob", "Weather is turning, heads up", "direct +9dB")),
            (-90, ("msg", "dm:" + bob, bob, "Bob", "Got your position, looks good", "direct +9dB")),
            (8, ("msg", "ch:0", alice, "Alice", "You on the trail yet? Send a ping when you reach the junction.", "1 hop +6dB")),
            (40, ("msg", "ch:1", sam, "Sam", "Battery swap at the cabin in an hour", "direct +2dB")),
        ]
        self._i = 0

    def channels(self):
        return [("ch:%d" % i, self.config.channel_name(i) or "Chan%d" % i)
                for i in range(len(self.config.channels))]

    def nodes(self):
        return list(self._nodes)

    def info(self):
        c = self.config
        return {"name": c.display_names()[0], "region": c.region,
                "preset": R.preset(c.preset)[2] if c.use_preset else "custom",
                "batt": 87, "link": "ok", "demo": True}

    def stats(self):
        return {"mode": "demo (no radio)", "frequency": "%.3f MHz" % self.config.frequency(),
                "packets in": 482, "packets out": 61, "duplicates": 190, "relayed": 17,
                "acked": 58, "failed": 3, "channel use": "4.2%", "airtime tx": "0.6%",
                "crypto": "n/a"}

    def apply(self, cfg):
        self.config = cfg.copy()
        return True, "Applied (demo): %s" % self.config.radio_summary()

    def my_position(self):
        return (37.7749, -122.4194)

    def share_position(self):
        return True, "Position shared (demo)"

    def announce(self):
        return True, "Name announced (demo)"

    def discover(self):
        return True, "Demo nodes only (no radio)"

    def _later(self, secs, *ev):
        self._due.append((self._clock() + secs, ev))

    def traceroute(self, nid):
        for n in self._nodes:
            if n.nid == nid:
                path = " > ".join(["You", "Ridge-R", n.name][:min(3, n.hops + 2)] if n.hops else ["You", n.name])
                self._later(2, "notice", "Route: %s" % path, GREEN)

    def request_position(self, nid):
        self._later(2, "notice", "Position updated", GREEN)

    def request_nodeinfo(self, nid):
        self._later(2, "notice", "Name updated", GREEN)

    def send(self, conv_key, text):
        self._mid += 1
        self._sent += 1
        now = self._clock()
        ok = self._sent % 4 != 0          # every 4th send fails, to show the state
        self._due.append((now + 1, ("ack", self._mid, "sent")))
        self._due.append((now + (2 if ok else 3), ("ack", self._mid, ok)))
        if ok:
            if conv_key.startswith("dm:"):
                nid = conv_key[3:]
            else:
                nid = _nid(0xA1F3)
            name = "Alice"
            for n in self._nodes:
                if n.nid == nid:
                    name = n.name
            self._due.append((now + 5, ("msg", conv_key, nid, name, "Copy that.", None, "1 hop +6dB")))
        return self._mid

    def poll(self):
        now = self._clock()
        out = []
        while self._i < len(self._script) and self.t0 + self._script[self._i][0] <= now:
            off, ev = self._script[self._i]
            self._i += 1
            out.append(ev[:5] + (self.t0 + off, ev[5]))
        keep = []
        for due, ev in self._due:
            if due <= now:
                if ev[0] == "msg":
                    ev = ev[:5] + (now,) + ev[6:]
                out.append(ev)
            else:
                keep.append((due, ev))
        self._due = keep
        if now - self._last_drift >= 5:
            self._last_drift = now
            step = int(now) // 5
            for i, n in enumerate(self._nodes):
                n.snr = self._base[n.num] + ((step + i) % 5 - 2) * 0.25
            self._nodes[0].heard = now - 2
            out.append(("node",))
        return out

    def close(self):
        pass


class _RawNode:
    """Node entry for the raw lorachat mode: only a nickname and what we heard."""

    def __init__(self, nick):
        self.nid = nick
        self.name = nick
        self.hops = 0
        self.snr = None
        self.heard = None
        self.batt = None
        self.fav = False
        self.ignored = False


class LoraBackend:
    """Raw-LoRa chat, wire-compatible with `lorachat`.

    Every packet is plain UTF-8 "nick: text" on the frequency saved by
    `loracfg`. There are no acks, so a successful transmit shows as "sent".
    Direct messages are a convention on top: the text is prefixed with
    "@peer " and the receiver files it under a dm tab.
    """

    demo = False

    def __init__(self, radio, nick, freq=None, env=None, clock=None):
        self._radio = radio
        self._env = env
        self.nick = nick
        self._freq = freq
        self._clock = clock or time.time
        self._mid = 0
        self._events = []
        self._nodes = {}
        radio.start_receive()

    def channels(self):
        return [("ch:0", "LoRa")]

    def nodes(self):
        return list(self._nodes.values())

    def info(self):
        region = "%.1f MHz" % self._freq if self._freq else "?"
        return {"name": self.nick, "region": region, "preset": "SF10 125k",
                "batt": None, "link": "ok", "demo": False}

    def send(self, conv_key, text):
        if conv_key.startswith("dm:"):
            text = "@%s %s" % (conv_key[3:], text)
        payload = ("%s: %s" % (self.nick, text)).encode("utf-8")
        if len(payload) > 255:
            raise ValueError("message too long")
        self._mid += 1
        try:
            self._radio.transmit(payload)
        finally:
            # transmit() leaves the chip in standby, win or lose
            self._radio.start_receive()
        self._events.append(("ack", self._mid, "sent"))
        return self._mid

    def poll(self):
        out = self._events
        self._events = []
        packet = self._radio.receive()
        if not packet:
            return out
        now = self._clock()
        try:
            snr = self._radio.snr()
        except Exception:
            snr = None
        try:
            text = packet.decode("utf-8")
        except UnicodeError:
            text = repr(packet)
        nick, sep, body = text.partition(": ")
        if not sep:
            nick, body = "?", text
        nick = nick[:16] or "?"
        node = self._nodes.get(nick)
        if node is None:
            node = self._nodes[nick] = _RawNode(nick)
        node.snr = snr
        node.heard = now
        key = "ch:0"
        prefix = "@%s " % self.nick
        if body.startswith(prefix):
            body = body[len(prefix):]
            key = "dm:" + nick
        out.append(("msg", key, nick, nick, body, now))
        out.append(("node",))
        self._radio.start_receive()
        return out

    def close(self):
        # Free the SPI handle and RadioLib objects, as lorachat does on exit.
        try:
            self._radio.deinit()
        finally:
            if self._env is not None:
                self._env.radio = None


def _read_freq():
    try:
        import json
        with open("/flash/.radio.json") as f:
            return float(json.load(f)["freq"])
    except Exception:
        return None


class MeshtasticBackend:
    """Real Meshtastic node on the SX1262: wraps mtmesh.Mesh for the UI."""

    demo = False

    def __init__(self, radio, cfg, me, env=None, clock=None, sensors=None, gps=None,
                 configure=None, node_path=mtconfig.PATH + ".nodes", cfg_path=mtconfig.PATH,
                 unix_offset=0, limited=""):
        self.radio = radio
        self.env = env
        self.config = cfg
        self.me = me
        self.limited = limited
        self._configure = configure
        self._node_path = node_path
        self._cfg_path = cfg_path
        self._clock = clock or time.time
        self._sensors = sensors or (lambda: {})
        self._gps = gps
        self._saved_at = self._clock()
        self._nodes_dirty = False
        self.mesh = mtmesh.Mesh(radio, cfg, me, self._clock, sensors=self._sensors,
                                gps=gps, unix_offset=unix_offset)
        self._load_nodes()

    # -- persistence --

    def _load_nodes(self):
        try:
            import json
            with open(self._node_path) as f:
                self.mesh.import_nodes(json.load(f))
        except (OSError, ValueError, ImportError):
            pass

    def save(self):
        try:
            import json
            with open(self._node_path, "w") as f:
                json.dump(self.mesh.export_nodes(), f)
            self._nodes_dirty = False
        except OSError:
            pass

    # -- UI contract --

    def channels(self):
        out = []
        for i in range(len(self.config.channels)):
            name = self.config.channel_name(i) or "Chan%d" % i
            out.append(("ch:%d" % i, name))
        return out

    def nodes(self):
        return [n for n in self.mesh.nodes.values() if n.num != self.me]

    def info(self):
        c = self.config
        s = self._sensors()
        link = "ok"
        if not c.tx_enabled:
            link = "rx only"
        if self.limited:
            link = "no preamble opt"
        return {"name": c.display_names()[0], "region": c.region,
                "preset": R.preset(c.preset)[2] if c.use_preset else "custom",
                "batt": s.get("battery"), "link": link, "demo": False}

    def stats(self):
        m = self.mesh
        c = self.config
        import mtcrypto
        st = m.stats
        out = {"frequency": "%.3f MHz" % c.frequency(), "slot": "%d of %d" % (c.effective_slot(), c.slots()),
               "power": "%d dBm" % c.power(), "packets in": st["rx"], "packets out": st["tx"],
               "duplicates": st["dup"], "relayed": st["relayed"], "acked": st["acked"],
               "failed": st["failed"], "not for us": st["undecoded"], "bad": st["bad"],
               "channel use": "%.1f%%" % m.channel_util(), "airtime tx": "%.2f%%" % m.tx_util(),
               "queued": len(m.txq), "crypto": mtcrypto.backend()}
        if self.limited:
            out["warning"] = self.limited
        return out

    def send(self, conv_key, text):
        if conv_key.startswith("dm:"):
            return self.mesh.send_text(_num(conv_key[3:]), text, 0)
        return self.mesh.send_text(P.BROADCAST, text, int(conv_key[3:]))

    def poll(self):
        out = []
        node_changed = False
        for ev in self.mesh.poll():
            kind = ev[0]
            if kind == "text":
                _, chan, to, frm, text, _pid, snr, hops, now = ev
                n = self.mesh.nodes.get(frm)
                name = n.name if n else _nid(frm)
                key = "ch:%d" % chan if to == P.BROADCAST else "dm:" + _nid(frm)
                meta = "%s%s" % (_hops(hops),
                                 (" %+.0fdB" % snr) if snr is not None else "")
                out.append(("msg", key, _nid(frm), name, text, now, meta))
            elif kind == "sent":
                out.append(("ack", ev[1], "sent"))
            elif kind == "ack":
                out.append(("ack", ev[1], ev[2]))
                if not ev[2]:
                    out.append(("notice", "No ack: %s" % P.ERROR_NAMES.get(ev[3], ev[3]), YELLOW))
            elif kind == "node":
                node_changed = True
            elif kind == "trace":
                out.append(("notice", self._trace_text(ev), GREEN))
        if node_changed:
            out.append(("node",))
            self._nodes_dirty = True
        now = self._clock()
        if self._nodes_dirty and now - self._saved_at > 600:
            self._saved_at = now
            self.save()
        return out

    def _trace_text(self, ev):
        names = []
        for num in ev[2]:
            n = self.mesh.nodes.get(num)
            names.append(n.name if n else _nid(num))
        who = self.mesh.nodes.get(ev[1])
        path = " > ".join(["You"] + names + [who.name if who else _nid(ev[1])])
        return "Route: " + path

    # -- settings --

    def apply(self, cfg):
        problems = cfg.problems()
        if problems:
            return False, problems[0]
        if self._configure is not None:
            ok, msg, limited = self._configure(self.radio, cfg)
            if not ok:
                return False, msg
            self.limited = limited
        cfg.node_num = cfg.node_num or self.me
        self.config = cfg
        self.mesh.cfg = cfg
        self.mesh.reconfigure()
        try:
            cfg.save(self._cfg_path)
        except OSError:
            pass
        if hasattr(self.radio, "start_receive"):
            self.radio.start_receive()
        return True, "Applied: %.3f MHz" % cfg.frequency()

    # -- node actions --

    def traceroute(self, nid):
        self.mesh.traceroute(_num(nid))

    def request_position(self, nid):
        self.mesh.request_position(_num(nid))

    def request_nodeinfo(self, nid):
        self.mesh.send_nodeinfo(_num(nid), want_response=True)

    def share_position(self):
        if self.mesh.send_position() is None:
            return False, "No position: set GPS or a fixed position"
        return True, "Position sent"

    def announce(self):
        self.mesh.send_nodeinfo()
        return True, "Name announced"

    def discover(self):
        try:
            self.mesh.discover()
        except ValueError as e:
            return False, str(e)
        return True, "Discovery queued; listening on %s" % self.config.channel_name(0)

    def my_position(self):
        fix = self.mesh._position()
        return (fix[0], fix[1]) if fix else None

    def close(self):
        self.save()
        try:
            self.config.save(self._cfg_path)
        except OSError:
            pass
        if self.env is not None:
            try:
                self.radio.deinit()
            finally:
                self.env.radio = None


def _hops(h):
    if h is None:
        return "?"
    if h == 0:
        return "direct"
    return "%d hop%s" % (h, "" if h == 1 else "s")


def configure_radio(radio, cfg):
    """Tune the SX1262 for Meshtastic. Returns (ok, message, limited_warning).

    Tries the usual TCXO voltages when the setting is "auto", and remembers the
    one that works. Old firmware without the preamble option falls back to the
    legacy call and reports it, because such a radio can't hear Meshtastic.
    """
    args = cfg.radio_args()
    if cfg.tcxo is not None:
        cands = [cfg.tcxo]
    elif cfg.tcxo_found is not None:
        cands = [cfg.tcxo_found]
    else:
        cands = [0.0, 1.8, 2.8, 2.4, 3.3]
    last = "radio did not start"
    for t in cands:
        try:
            radio.begin(tcxo=t, dio2_rf_switch=cfg.dio2, current_limit=cfg.current_limit, **args)
        except TypeError:
            try:
                radio.begin(freq=args["freq"], bw=args["bw"], sf=args["sf"], cr=args["cr"],
                            sync_word=args["sync_word"], power=args["power"])
            except RuntimeError as e:
                return False, str(e), ""
            return True, "ok", "firmware lacks preamble option: rebuild to hear Meshtastic"
        except RuntimeError as e:
            last = str(e)
            continue
        try:
            errs = radio.get_device_errors()
        except Exception:
            errs = 0
        if errs:
            last = "radio error 0x%04X" % errs
            continue
        if cfg.tcxo is None:
            cfg.tcxo_found = t
        if hasattr(radio, "rx_boost"):
            try:
                radio.rx_boost(cfg.rx_boost)
            except RuntimeError:
                pass
        return True, "ok", ""
    return False, last, ""


def _node_number():
    try:
        import network
        mac = network.WLAN(network.STA_IF).config("mac")
        return int.from_bytes(mac[2:6], "big") or 0xFFFF0001
    except Exception:
        import random
        return random.getrandbits(32) | 0x10


def _device_sensors(env):
    state = {}

    def read():
        try:
            if "pin" not in state:
                import hardware
                pin = getattr(getattr(env, "sts", None), "bat_pin", None) or hardware.init_bat()
                state["pin"] = pin
                state["hw"] = hardware
            pct = state["hw"].bat_value(state["pin"])
            return {"battery": pct, "voltage": 3.2 + pct / 100.0}
        except Exception:
            return {}
    return read


def _device_gps(cfg):
    state = {}

    def read():
        if cfg.pos_mode != "gps":
            return None
        try:
            if "gps" not in state:
                from bin import wardrive
                state["gps"] = wardrive.Gps()
            g = state["gps"]
            g.poll(10)
            if g.lat is None or not g.fix:
                return None
            return (g.lat, g.lon, g.alt or 0, int(time.time()) + UNIX_OFFSET, g.sats)
        except Exception:
            return None
    return read


def _make_radio(env):
    """The SX1262: the Python driver on the shared SPI bus when the chip answers
    there, otherwise the native `lora` module."""
    import hardware as hw
    spi = getattr(hw, "spi", None)
    if spi is not None:
        try:
            import machine
            import sx126x
            r = sx126x.SX126x(spi, machine.Pin(hw.RADIO_CS, machine.Pin.OUT, value=1),
                              machine.Pin(hw.RADIO_RST, machine.Pin.OUT, value=1),
                              machine.Pin(hw.RADIO_BUSY, machine.Pin.IN),
                              machine.Pin(hw.RADIO_DIO1, machine.Pin.IN),
                              env=env, restore_baud=hw.DISPLAY_BAUDRATE)
            if r.probe():
                return r
        except Exception:
            pass
    import lora
    return lora.LoRa(cs=hw.RADIO_CS, dio1=hw.RADIO_DIO1, rst=hw.RADIO_RST, busy=hw.RADIO_BUSY,
                     sck=hw.SPI_SCK, miso=hw.SPI_MISO, mosi=hw.SPI_MOSI)


def _raw_begin(radio):
    """lorachat-compatible radio settings from /flash/.radio.json."""
    freq, power = 915.0, 10
    try:
        import json
        with open("/flash/.radio.json") as f:
            cfg = json.load(f)
        freq, power = float(cfg["freq"]), int(cfg["pwr"])
    except Exception:
        pass
    radio.begin(freq=freq, bw=125.0, sf=10, cr=6, sync_word=0x12, power=power)
    return freq


def open_meshcore(env, radio, nick):
    import os
    import mcback
    import mcmesh
    state = mcmesh.State.load()
    if nick and nick != "vtOS":
        state.name = nick[:16]
    state.ensure_identity(os.urandom)
    ok, msg, _ = configure_radio(radio, state)
    if not ok:
        raise RuntimeError(msg)
    radio.start_receive()
    backend = mcback.MeshCoreBackend(radio, state, env, configure=configure_radio,
                                     unix_offset=UNIX_OFFSET if time.gmtime(0)[0] == 2000 else 0)
    try:
        state.save()
    except OSError:
        pass
    return backend


def open_backend(env, demo=False, nick="vtOS", raw=False, meshcore=False):
    """Returns (backend, note). note is a human string when demo is a fallback."""
    if demo:
        return SimBackend(), None
    try:
        radio = getattr(env, "radio", None)
        if radio is None:
            radio = _make_radio(env)
            env.radio = radio
        if meshcore:
            return open_meshcore(env, radio, nick), None
        if raw:
            freq = _raw_begin(radio)
            return LoraBackend(radio, nick, freq, env), None
        cfg = mtconfig.Settings.load()
        if not cfg.node_num:
            cfg.node_num = _node_number()
        if nick and not cfg.long_name and nick != "vtOS":
            cfg.long_name = nick
        ok, msg, limited = configure_radio(radio, cfg)
        if not ok:
            raise RuntimeError(msg)
        radio.start_receive()
        backend = MeshtasticBackend(radio, cfg, cfg.node_num, env, sensors=_device_sensors(env),
                                    gps=_device_gps(cfg), configure=configure_radio,
                                    unix_offset=UNIX_OFFSET if time.gmtime(0)[0] == 2000 else 0,
                                    limited=limited)
        try:
            cfg.save()
        except OSError:
            pass
        return backend, limited or None
    except Exception as e:
        try:
            if getattr(env, "radio", None) is not None:
                env.radio.deinit()
                env.radio = None
        except Exception:
            pass
        return SimBackend(), "Demo mode: %s" % e
