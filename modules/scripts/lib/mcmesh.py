#
# MeshCore client engine: identity, contacts, channels, send/receive, acks,
# repeater login. Hardware-free: talks to a `radio` (transmit/receive/
# start_receive/snr) and a clock, like mtmesh.py, so it runs under pytest.
# License: MIT
#
# This is a *client*: it never relays other people's packets. Everything is sent
# as flood routing (empty path), which any repeater in range forwards; replies
# from repeaters arrive the same way, so no path learning is needed.
#
# NOT verified on the air against real MeshCore firmware. See docs/MESHCORE.md.
#

import json

import mccurve
import mcproto as W

PATH = "/flash/.meshcore.json"
MAX_CONTACTS = 50
MAX_HEARD = 24
SEEN_MAX = 200
ACK_TIMEOUT = 30.0
LOGIN_TIMEOUT = 25.0
MAX_TEXT = 150           # leaves room for the 184-byte payload after header/MAC/padding

# LoRa defaults: MeshCore's US/Canada community setting. Frequency, bandwidth, SF
# and CR MUST match the repeater. Sync word 0x12 is RadioLib's "private" word that
# MeshCore uses; the preamble length is from memory (UNVERIFIED).
DEFAULT_RADIO = {"freq": 910.525, "bw": 62.5, "sf": 7, "cr": 5, "power": 20, "preamble": 16}


def _hexs(b):
    return "".join("%02x" % x for x in b)


class Contact:
    """A known MeshCore node. nid/name/hops/snr/heard/batt/fav/ignored feed the UI."""

    def __init__(self, pub, name="", kind=0):
        self.pub = bytes(pub)
        self.name = name or _hexs(pub[:3])
        self.kind = kind
        self.hops = None
        self.snr = None
        self.heard = None
        self.batt = None
        self.fav = False
        self.ignored = False
        self.lat = None
        self.lon = None
        self.pos_time = None
        self.logged_in = False
        self.admin = False
        self.secret = None          # cached ECDH secret (never saved)

    @property
    def nid(self):
        return _hexs(self.pub[:6])

    @property
    def kind_name(self):
        return W.TYPE_NAMES.get(self.kind, "node")

    @property
    def needs_login(self):
        return self.kind in (W.ADV_REPEATER, W.ADV_ROOM)

    def to_dict(self):
        return {"pub": _hexs(self.pub), "name": self.name, "kind": self.kind,
                "lat": self.lat, "lon": self.lon, "fav": self.fav, "ignored": self.ignored}


class Channel:
    def __init__(self, name, secret):
        self.name = name
        self.secret = bytes(secret)

    @property
    def hash(self):
        return W.channel_hash(self.secret)


class State:
    """Everything saved in /flash/.meshcore.json. Holds the PRIVATE KEY."""

    def __init__(self):
        self.name = "vtOS"
        self.priv = b""
        self.pub = b""
        self.radio = dict(DEFAULT_RADIO)
        self.contacts = []          # Contact
        self.channels = [Channel("Public", W.PUBLIC_SECRET)]
        self.last_ts = 0            # last timestamp used: never go backwards (replay checks)
        self.tcxo = None
        self.tcxo_found = None
        self.dio2 = True
        self.current_limit = 140
        self.rx_boost = True

    def ensure_identity(self, rand):
        """rand(n) -> n random bytes. Creates the identity on first use."""
        if len(self.priv) != 64 or len(self.pub) != 32:
            self.priv = mccurve.new_private(rand(64))
            self.pub = mccurve.public_from_private(self.priv)

    def radio_args(self):
        r = self.radio
        return {"freq": r["freq"], "bw": r["bw"], "sf": r["sf"], "cr": r["cr"],
                "sync_word": 0x12, "power": r["power"], "preamble": r["preamble"]}

    def frequency(self):
        return self.radio["freq"]

    def problems(self):
        r = self.radio
        if not 400.0 <= r["freq"] <= 1000.0:
            return "frequency must be 400-1000 MHz"
        if r["bw"] not in (7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125.0, 250.0, 500.0):
            return "bandwidth not supported"
        if not 5 <= r["sf"] <= 12:
            return "SF must be 5-12"
        if not 5 <= r["cr"] <= 8:
            return "CR must be 5-8 (4/5..4/8)"
        if not 2 <= r["power"] <= 22:
            return "power must be 2-22 dBm"
        return ""

    def to_dict(self):
        return {"name": self.name, "priv": _hexs(self.priv), "pub": _hexs(self.pub),
                "radio": self.radio, "last_ts": self.last_ts,
                "contacts": [c.to_dict() for c in self.contacts],
                "channels": [{"name": c.name, "secret": _hexs(c.secret)} for c in self.channels],
                "tcxo": self.tcxo, "tcxo_found": self.tcxo_found}

    @classmethod
    def from_dict(cls, d):
        s = cls()
        s.name = str(d.get("name") or s.name)[:16]
        try:
            s.priv = bytes.fromhex(d.get("priv", ""))
            s.pub = bytes.fromhex(d.get("pub", ""))
        except ValueError:
            s.priv = s.pub = b""
        s.radio.update({k: v for k, v in d.get("radio", {}).items() if k in DEFAULT_RADIO})
        s.last_ts = int(d.get("last_ts", 0))
        s.tcxo = d.get("tcxo")
        s.tcxo_found = d.get("tcxo_found")
        for cd in d.get("contacts", [])[:MAX_CONTACTS]:
            try:
                c = Contact(bytes.fromhex(cd["pub"]), cd.get("name", ""), cd.get("kind", 0))
            except (ValueError, KeyError):
                continue
            if len(c.pub) != 32:
                continue
            c.lat, c.lon = cd.get("lat"), cd.get("lon")
            c.fav, c.ignored = bool(cd.get("fav")), bool(cd.get("ignored"))
            s.contacts.append(c)
        chans = []
        for ch in d.get("channels", []):
            try:
                chans.append(Channel(ch["name"], bytes.fromhex(ch["secret"])))
            except (ValueError, KeyError):
                continue
        if chans:
            s.channels = chans
        return s

    @classmethod
    def load(cls, path=PATH):
        try:
            with open(path) as f:
                return cls.from_dict(json.load(f))
        except (OSError, ValueError):
            return cls()

    def save(self, path=PATH):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f)

    def find(self, key):
        """Contact by nid prefix, public-key hex prefix, or name (exact, prefix, substring)."""
        q = key.strip().lower()
        if not q:
            return None
        for c in self.contacts:
            if c.nid == q or _hexs(c.pub) == q:
                return c
        for test in (lambda c: c.name.lower() == q, lambda c: c.name.lower().startswith(q),
                     lambda c: q in c.name.lower()):
            for c in self.contacts:
                if test(c):
                    return c
        if len(q) >= 4:
            for c in self.contacts:
                if _hexs(c.pub).startswith(q):
                    return c
        return None

    def add_contact(self, pub, name="", kind=0, lat=None, lon=None):
        """-> (contact, created). Updates name/type/position of an existing one."""
        if bytes(pub) == self.pub:
            raise ValueError("that is your own key")
        for c in self.contacts:
            if c.pub == bytes(pub):
                if name:
                    c.name = name
                if kind:
                    c.kind = kind
                if lat is not None:
                    c.lat, c.lon = lat, lon
                return c, False
        if len(self.contacts) >= MAX_CONTACTS:
            raise ValueError("contact list full (%d)" % MAX_CONTACTS)
        c = Contact(pub, name, kind)
        c.lat, c.lon = lat, lon
        self.contacts.append(c)
        return c, True


class Mesh:
    def __init__(self, radio, state, clock, unix_offset=0):
        self.radio = radio
        self.st = state
        self.clock = clock
        self.offset = unix_offset
        self.events = []
        self.seen = {}               # packet hash -> time
        self.heard = {}              # pub hex -> Contact heard in adverts, not added
        self.acks = {}               # 4-byte ack -> (mid, deadline, contact, cli)
        self.logins = {}             # contact pub -> deadline
        self._mid = 0
        self.stats = {"rx": 0, "tx": 0, "dup": 0, "bad": 0, "undecoded": 0, "acked": 0,
                      "failed": 0}

    # -- helpers --

    @property
    def me(self):
        return self.st.pub

    def now(self):
        return self.clock()

    def _ts(self):
        t = max(int(self.clock()) + self.offset, self.st.last_ts + 1)
        self.st.last_ts = t
        return t

    def secret_for(self, c):
        if c.secret is None:
            c.secret = mccurve.shared_secret(self.st.priv, c.pub)
            if c.secret is None:
                raise ValueError("%s has an invalid public key" % c.name)
        return c.secret

    def _transmit(self, pkt):
        self.seen[pkt.hash()] = self.now()
        self.radio.transmit(pkt.encode())
        self.stats["tx"] += 1
        if hasattr(self.radio, "start_receive"):
            self.radio.start_receive()

    def _emit(self, *ev):
        self.events.append(ev)

    # -- sending --

    def send_text(self, conv_key, text):
        """conv_key "ch:<n>" or "dm:<nid>". Returns a message id."""
        if len(text.encode("utf-8")) > MAX_TEXT:
            raise ValueError("message too long (max %d bytes)" % MAX_TEXT)
        self._mid += 1
        mid = self._mid
        if conv_key.startswith("ch:"):
            ch = self.st.channels[int(conv_key[3:])]
            plain = W.group_plain(self._ts(), self.st.name, text)
            self._transmit(W.build_group(ch.secret, plain))
            self._emit("sent", mid)
            return mid
        c = self.st.find(conv_key[3:])
        if c is None:
            raise ValueError("unknown contact")
        cli = c.kind == W.ADV_REPEATER and c.logged_in
        if c.needs_login and not c.logged_in:
            raise ValueError("log in first: /login <password>")
        flags = (W.TXT_CLI << 2) if cli else 0
        plain = W.txt_plain(self._ts(), text, flags)
        pkt = W.build_peer(W.PT_TXT, self.secret_for(c), c.pub, self.me, plain)
        self.acks[W.expected_ack(plain, self.me)] = (mid, self.now() + ACK_TIMEOUT, c, cli)
        self._transmit(pkt)
        self._emit("sent", mid)
        return mid

    def login(self, c, password):
        """Send a login request (ANON_REQ) to a repeater or room server."""
        if len(password.encode("utf-8")) > 15:
            raise ValueError("password too long (max 15 bytes)")
        plain = self._ts().to_bytes(4, "little") + password.encode("utf-8")
        self._transmit(W.build_anon_req(self.secret_for(c), c.pub, self.me, plain))
        self.logins[c.pub] = self.now() + LOGIN_TIMEOUT

    # -- receiving --

    def poll(self):
        raw = self.radio.receive()
        if raw:
            snr = None
            try:
                snr = self.radio.snr()
            except Exception:
                pass
            self._rx(bytes(raw), snr)
        self._expire()
        out, self.events = self.events, []
        return out

    def _expire(self):
        now = self.now()
        for k in [k for k, v in self.acks.items() if v[1] <= now]:
            mid = self.acks.pop(k)[0]
            self.stats["failed"] += 1
            self._emit("ack", mid, False, "no ack")
        for pub in [p for p, t in self.logins.items() if t <= now]:
            del self.logins[pub]
            c = self.st.find(_hexs(pub))
            self._emit("login", c.nid if c else "", None, False)
        if len(self.seen) > SEEN_MAX:
            for h in sorted(self.seen, key=self.seen.get)[:SEEN_MAX // 4]:
                del self.seen[h]

    def _rx(self, raw, snr):
        try:
            pkt = W.decode(raw)
        except W.ProtoError:
            self.stats["bad"] += 1
            return
        h = pkt.hash()
        if h in self.seen:
            self.stats["dup"] += 1
            return
        self.seen[h] = self.now()
        self.stats["rx"] += 1
        hops = len(pkt.path) if pkt.route in (W.ROUTE_FLOOD, W.ROUTE_TRANSPORT_FLOOD) else 0
        t = pkt.ptype
        if t == W.PT_ADVERT:
            self._on_advert(pkt, snr, hops)
        elif t in (W.PT_TXT, W.PT_RESPONSE):
            self._on_peer(pkt, snr, hops)
        elif t == W.PT_ACK:
            self._on_ack(pkt)
        elif t == W.PT_GRP_TXT:
            self._on_group(pkt, snr, hops)

    def _on_advert(self, pkt, snr, hops):
        try:
            adv = W.parse_advert_payload(pkt.payload)
        except W.ProtoError:
            self.stats["bad"] += 1
            return
        if adv.pub == self.me:
            return
        c = next((x for x in self.st.contacts if x.pub == adv.pub), None)
        known = c is not None
        if not known:
            c = self.heard.get(_hexs(adv.pub))
            if c is None:
                if len(self.heard) >= MAX_HEARD:
                    del self.heard[next(iter(self.heard))]
                c = self.heard[_hexs(adv.pub)] = Contact(adv.pub, adv.name, adv.kind)
        if adv.name:
            c.name = adv.name
        c.kind = adv.kind or c.kind
        if adv.lat is not None:
            c.lat, c.lon, c.pos_time = adv.lat, adv.lon, self.now()
        c.heard, c.snr, c.hops = self.now(), snr, hops
        self._emit("node")

    def _find_sender(self, src_hash, blob):
        for c in self.st.contacts:
            if c.pub[0] != src_hash:
                continue
            try:
                plain = W.unseal(self.secret_for(c), blob)
            except ValueError:
                continue
            if plain is not None:
                return c, plain
        return None, None

    def _on_peer(self, pkt, snr, hops):
        parts = W.parse_peer(pkt)
        if parts is None or parts[0] != self.me[0]:
            return
        c, plain = self._find_sender(parts[1], parts[2])
        if c is None:
            self.stats["undecoded"] += 1
            return
        c.heard, c.snr, c.hops = self.now(), snr, hops
        self._emit("node")
        if pkt.ptype == W.PT_RESPONSE:
            self._on_response(c, plain)
            return
        parsed = W.parse_txt(plain)
        if parsed is None:
            return
        ts, ttype, attempt, text = parsed
        if ttype == W.TXT_CLI:
            for k, v in list(self.acks.items()):         # a reply stands in for the ack
                if v[2] is c and v[3]:
                    del self.acks[k]
                    self.stats["acked"] += 1
                    self._emit("ack", v[0], True)
                    break
        else:
            ack = W.Packet(W.PT_ACK, W.expected_ack(plain, c.pub))
            try:
                self._transmit(ack)
            except RuntimeError:
                pass
        self._emit("text", "dm:" + c.nid, c, c.name, text, snr, hops, ttype)

    def _on_response(self, c, plain):
        if c.pub in self.logins:
            res = W.parse_login_response(plain)
            if res is None:
                return
            del self.logins[c.pub]
            c.logged_in, c.admin = res[0], res[0] and res[1]
            self._emit("login", c.nid, res[0], c.admin)
        else:
            self._emit("response", c.nid, len(plain))

    def _on_ack(self, pkt):
        hit = self.acks.pop(pkt.payload[:4], None)
        if hit:
            self.stats["acked"] += 1
            self._emit("ack", hit[0], True)

    def _on_group(self, pkt, snr, hops):
        p = pkt.payload
        if len(p) < 1 + W.MAC_LEN + 16:
            return
        for i, ch in enumerate(self.st.channels):
            if ch.hash != p[0]:
                continue
            plain = W.unseal(ch.secret, p[1:])
            if plain is None:
                continue
            if len(plain) < 6:
                return
            body = W.text_of(plain[5:].rstrip(b"\x00"))
            sender, sep, text = body.partition(": ")
            if not sep:
                sender, text = "?", body
            if sender == self.st.name:
                return                                   # our own message echoed back
            self._emit("text", "ch:%d" % i, None, sender, text, snr, hops, 0)
            return
        self.stats["undecoded"] += 1

    # -- contacts --

    def adopt(self, c):
        """Move a heard node into the contact list."""
        self.heard.pop(_hexs(c.pub), None)
        got, _ = self.st.add_contact(c.pub, c.name, c.kind, c.lat, c.lon)
        return got


# --- shared by the UI backend and the `meshcore` shell command ----------------

def import_link(state, text):
    """Add/update a contact from a pasted link. -> (ok, message, contact)."""
    text = text.strip()
    if not text:
        return False, "Paste a meshcore:// link", None
    try:
        adv, sig_ok = W.parse_contact_link(text)
    except W.ProtoError as e:
        return False, "Bad link: %s" % e, None
    if sig_ok is False:
        return False, "Link signature is invalid: not importing", None
    try:
        c, created = state.add_contact(adv.pub, adv.name, adv.kind, adv.lat, adv.lon)
    except ValueError as e:
        return False, str(e), None
    note = "" if sig_ok else " (signature not checked)"
    return True, "%s %s (%s)%s" % ("Added" if created else "Updated", c.name, c.kind_name, note), c


def set_radio(state, text):
    """Parse "freq bw sf cr [power]" into state.radio. -> (ok, message)."""
    parts = text.replace(",", " ").split()
    r = state.radio
    if len(parts) not in (4, 5):
        return False, "Now %.3f BW%g SF%d CR%d %ddBm. Use: freq bw sf cr [power]" % (
            r["freq"], r["bw"], r["sf"], r["cr"], r["power"])
    old = dict(r)
    try:
        r["freq"], r["bw"] = float(parts[0]), float(parts[1])
        r["sf"], r["cr"] = int(parts[2]), int(parts[3])
        if len(parts) == 5:
            r["power"] = int(parts[4])
    except ValueError:
        r.update(old)
        return False, "Numbers only: freq bw sf cr [power]"
    bad = state.problems()
    if bad:
        r.update(old)
        return False, bad
    return True, "Radio: %.3f MHz BW%g SF%d CR4/%d %d dBm" % (
        r["freq"], r["bw"], r["sf"], r["cr"], r["power"])


def share_link(state):
    return "meshcore://contact/add?name=%s&public_key=%s&type=1" % (
        state.name.replace(" ", "%20"), _hexs(state.pub))


USAGE = ("meshcore id | list | import <link> | rm <name> | radio [freq bw sf cr [power]] | "
         "name <name> | channel <name> <32 hex>")


def run_cli(state, args, rand):
    """The `meshcore` shell command. Returns (lines, changed)."""
    if not args:
        return [USAGE], False
    cmd, rest = args[0].lower(), " ".join(args[1:])
    if cmd == "id":
        state.ensure_identity(rand)
        return ["name  " + state.name, "key   " + _hexs(state.pub), "link  " + share_link(state)], True
    if cmd == "list":
        if not state.contacts:
            return ["No contacts. meshcore import <link>"], False
        return ["%-12s %-8s %s" % (c.nid, c.kind_name, c.name) for c in state.contacts], False
    if cmd == "import":
        ok, msg, _ = import_link(state, rest)
        return [msg], ok
    if cmd == "rm" and rest:
        c = state.find(rest)
        if c is None:
            return ["No contact '%s'" % rest], False
        state.contacts.remove(c)
        return ["Removed " + c.name], True
    if cmd == "radio":
        if not rest:
            return [set_radio(state, "")[1]], False
        ok, msg = set_radio(state, rest)
        return [msg], ok
    if cmd == "name" and rest:
        state.name = rest[:16]
        return ["Name: " + state.name], True
    if cmd == "channel" and len(args) == 3:
        try:
            secret = bytes.fromhex(args[2])
        except ValueError:
            secret = b""
        if len(secret) != 16:
            return ["Channel secret is 32 hex digits (16 bytes)"], False
        if len(state.channels) >= 8:
            return ["At most 8 channels"], False
        state.channels.append(Channel(args[1][:16], secret))
        return ["Added channel %s (hash %02x)" % (args[1][:16], W.channel_hash(secret))], True
    return [USAGE], False
