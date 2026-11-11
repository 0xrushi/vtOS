#
# MeshCore wire format: packets, encrypted payloads, adverts, contact links.
# License: MIT
#
# Written from MeshCore's public firmware sources and docs as remembered, NOT
# checked against a real MeshCore node: every layout below is marked in the tests
# as "from memory" and has to be confirmed on the air (docs/MESHCORE.md).
#
# Packet:   header(1)  [transport codes(4) if route is 0 or 3]  path_len(1)
#           path(path_len & 63 bytes of 1-byte hashes)  payload
#   header: bits 0-1 route (0 transport flood, 1 flood, 2 direct, 3 transport
#           direct), bits 2-5 payload type, bits 6-7 version
# Encrypted payloads (REQ, RESPONSE, TXT, PATH):  dest_hash(1) src_hash(1) mac(2) cipher
#   cipher = AES-128-ECB over the zero-padded plaintext, key = shared secret[:16],
#   mac = HMAC-SHA256(key = 32-byte shared secret, cipher)[:2]
# Group text:  channel_hash(1) mac(2) cipher, channel secret 16 bytes (zero-padded
#   to 32 for the HMAC), channel_hash = sha256(secret)[0]
# ANON_REQ:    dest_hash(1) sender_pub(32) mac(2) cipher     (used to log in)
#

import hashlib
import mtcrypto

ROUTE_TRANSPORT_FLOOD, ROUTE_FLOOD, ROUTE_DIRECT, ROUTE_TRANSPORT_DIRECT = 0, 1, 2, 3
PT_REQ, PT_RESPONSE, PT_TXT, PT_ACK, PT_ADVERT, PT_GRP_TXT, PT_GRP_DATA, PT_ANON_REQ, \
    PT_PATH, PT_TRACE = 0, 1, 2, 3, 4, 5, 6, 7, 8, 9

TXT_PLAIN, TXT_CLI = 0, 1
ADV_NONE, ADV_CHAT, ADV_REPEATER, ADV_ROOM, ADV_SENSOR = 0, 1, 2, 3, 4
TYPE_NAMES = {ADV_NONE: "node", ADV_CHAT: "chat", ADV_REPEATER: "repeater",
              ADV_ROOM: "room", ADV_SENSOR: "sensor"}
MAX_PAYLOAD = 184
MAX_PATH = 64
MAC_LEN = 2

# The shared "Public" channel. Base64 "izOH6cXN6mrJ5e26oRXNcg==" in the apps.
PUBLIC_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")


class ProtoError(ValueError):
    pass


def text_of(raw):
    """UTF-8 text; bytes that are not valid become '?' (MicroPython's decode()
    takes no errors= argument)."""
    try:
        return bytes(raw).decode()
    except UnicodeError:
        return "".join(chr(b) if 32 <= b < 127 else "?" for b in raw)


# --- helpers -----------------------------------------------------------------

def sha256(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def hmac_sha256(key, data):
    if len(key) > 64:
        key = sha256(key)
    key = bytes(key) + bytes(64 - len(key))
    inner = sha256(bytes(b ^ 0x36 for b in key), data)
    return sha256(bytes(b ^ 0x5C for b in key), inner)


def channel_hash(secret):
    return sha256(secret)[0]


def _pad(data):
    return bytes(data) + bytes((-len(data)) % 16)


def seal(secret, plain):
    """-> mac(2) + cipher. `secret` is the 32-byte shared secret (or a 16-byte
    channel secret; it is zero-padded to 32 for the MAC)."""
    cipher = mtcrypto.ecb(secret[:16], _pad(plain))
    return hmac_sha256(_pad32(secret), cipher)[:MAC_LEN] + cipher


def _pad32(secret):
    return bytes(secret) + bytes(max(0, 32 - len(secret)))


def unseal(secret, blob):
    """mac+cipher -> zero-padded plaintext, or None when the MAC does not match."""
    if len(blob) < MAC_LEN + 16 or (len(blob) - MAC_LEN) % 16:
        return None
    mac, cipher = blob[:MAC_LEN], blob[MAC_LEN:]
    want = hmac_sha256(_pad32(secret), cipher)[:MAC_LEN]
    diff = 0
    for a, b in zip(mac, want):
        diff |= a ^ b
    if diff:
        return None
    return mtcrypto.ecb(secret[:16], cipher, decrypt=True)


# --- packets -----------------------------------------------------------------

class Packet:
    def __init__(self, ptype, payload, route=ROUTE_FLOOD, path=b"", transport=None, ver=0):
        self.ptype = ptype
        self.payload = bytes(payload)
        self.route = route
        self.path = bytes(path)
        self.transport = transport      # 4 bytes or None
        self.ver = ver

    def hash(self):
        """8-byte duplicate-detection id (type + payload, as the firmware does)."""
        return sha256(bytes([self.ptype]), self.payload)[:8]

    def encode(self):
        if len(self.payload) > MAX_PAYLOAD:
            raise ProtoError("payload too long")
        out = bytearray([(self.ver << 6) | (self.ptype << 2) | self.route])
        if self.route in (ROUTE_TRANSPORT_FLOOD, ROUTE_TRANSPORT_DIRECT):
            out += (self.transport or bytes(4))[:4]
        out.append(len(self.path))
        out += self.path
        out += self.payload
        return bytes(out)


def decode(raw):
    if len(raw) < 3:
        raise ProtoError("short packet")
    h = raw[0]
    route, ptype, ver = h & 3, (h >> 2) & 15, h >> 6
    i = 1
    transport = None
    if route in (ROUTE_TRANSPORT_FLOOD, ROUTE_TRANSPORT_DIRECT):
        transport = raw[1:5]
        i = 5
    if i >= len(raw):
        raise ProtoError("short packet")
    plen = raw[i]
    i += 1
    if plen > MAX_PATH or i + plen > len(raw):
        raise ProtoError("bad path length")
    path = raw[i:i + plen]
    payload = raw[i + plen:]
    if not payload:
        raise ProtoError("empty payload")
    return Packet(ptype, payload, route, path, transport, ver)


# --- encrypted payload builders / parsers ------------------------------------

def build_peer(ptype, secret, dest_pub, src_pub, plain, route=ROUTE_FLOOD):
    """REQ / RESPONSE / TXT / PATH to a known peer."""
    return Packet(ptype, bytes([dest_pub[0], src_pub[0]]) + seal(secret, plain), route)


def parse_peer(pkt):
    """-> (dest_hash, src_hash, mac+cipher) or None."""
    p = pkt.payload
    if len(p) < 2 + MAC_LEN + 16:
        return None
    return p[0], p[1], p[2:]


def build_anon_req(secret, dest_pub, src_pub, plain, route=ROUTE_FLOOD):
    return Packet(PT_ANON_REQ, bytes([dest_pub[0]]) + bytes(src_pub) + seal(secret, plain), route)


def build_group(secret, plain, route=ROUTE_FLOOD):
    return Packet(PT_GRP_TXT, bytes([channel_hash(secret)]) + seal(secret, plain), route)


def txt_plain(ts, text, flags=0):
    """TXT_MSG plaintext: timestamp(4 LE) flags(1: txt_type << 2 | attempt) text."""
    return ts.to_bytes(4, "little") + bytes([flags]) + text.encode("utf-8")


def parse_txt(plain):
    """-> (timestamp, txt_type, attempt, text) from decrypted TXT_MSG plaintext."""
    if len(plain) < 5:
        return None
    ts = int.from_bytes(plain[:4], "little")
    flags = plain[4]
    body = plain[5:].rstrip(b"\x00")
    return ts, flags >> 2, flags & 3, text_of(body)


def group_plain(ts, sender, text):
    """GRP_TXT plaintext: timestamp(4) txt_type(1, 0) "sender: text"."""
    return ts.to_bytes(4, "little") + b"\x00" + ("%s: %s" % (sender, text)).encode("utf-8")


def expected_ack(plain_txt, sender_pub):
    """4-byte ACK the receiver sends back: sha256(ts+flags+text + sender pubkey)."""
    body = plain_txt.rstrip(b"\x00")
    return sha256(body, sender_pub)[:4]


def parse_login_response(plain):
    """Decrypted RESPONSE to a login -> (ok, is_admin) or None. Layout: tag(4)
    code(1, 0 = ok) keepalive(1) is_admin(1) permissions(1) ..."""
    plain = plain.rstrip(b"\x00") if len(plain) > 9 else plain
    if len(plain) < 5:
        return None
    ok = plain[4] == 0
    admin = bool(plain[6]) if len(plain) > 6 else False
    return ok, admin


# --- adverts and contact links ----------------------------------------------

class Advert:
    def __init__(self, pub, ts, sig, kind, name, lat=None, lon=None):
        self.pub = bytes(pub)
        self.ts = ts
        self.sig = bytes(sig)
        self.kind = kind
        self.name = name
        self.lat = lat
        self.lon = lon
        self.appdata = b""

    def signed_message(self):
        return self.pub + self.ts.to_bytes(4, "little") + self.appdata


def parse_advert_payload(payload):
    """ADVERT payload: pubkey(32) timestamp(4) signature(64) appdata
    appdata: flags(1: low nibble type, 0x10 location, 0x20/0x40 feature words,
    0x80 name) [lat(4) lon(4), signed microdegrees LE] [feat1(2)] [feat2(2)] name"""
    if len(payload) < 100:
        raise ProtoError("advert too short")
    pub, ts = payload[:32], int.from_bytes(payload[32:36], "little")
    sig, app = payload[36:100], payload[100:]
    if not app:
        raise ProtoError("advert has no app data")
    flags = app[0]
    i = 1
    lat = lon = None
    if flags & 0x10:
        if len(app) < i + 8:
            raise ProtoError("advert location truncated")
        lat = _s32(app[i:i + 4]) / 1e6
        lon = _s32(app[i + 4:i + 8]) / 1e6
        i += 8
    if flags & 0x20:
        i += 2
    if flags & 0x40:
        i += 2
    name = ""
    if flags & 0x80:
        name = text_of(app[i:].split(b"\x00")[0])
    adv = Advert(pub, ts, sig, flags & 0x0F, name, lat, lon)
    adv.appdata = bytes(app)
    return adv


def _s32(b):
    v = int.from_bytes(b, "little")
    return v - (1 << 32) if v & 0x80000000 else v


def build_advert_payload(pub, ts, sig, kind, name, lat=None, lon=None):
    """Inverse of parse (tests and the link exporter)."""
    flags = kind & 0x0F
    app = bytearray()
    if lat is not None and lon is not None:
        flags |= 0x10
    if name:
        flags |= 0x80
    app.append(flags)
    if flags & 0x10:
        app += (int(round(lat * 1e6)) & 0xFFFFFFFF).to_bytes(4, "little")
        app += (int(round(lon * 1e6)) & 0xFFFFFFFF).to_bytes(4, "little")
    if name:
        app += name.encode("utf-8")
    return bytes(pub) + ts.to_bytes(4, "little") + bytes(sig) + bytes(app)


def _hex(text):
    t = text.strip()
    if len(t) % 2:
        raise ProtoError("odd-length hex")
    try:
        return bytes.fromhex(t)
    except ValueError:
        raise ProtoError("not hex")


def _unquote(s):
    out = bytearray()
    i = 0
    s = s.replace("+", " ")
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s):
            try:
                out.append(int(s[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out += s[i].encode("utf-8")
        i += 1
    return text_of(out)


def parse_contact_link(text):
    """A pasted MeshCore contact -> Advert-like Contact fields, or ProtoError.

    Accepted (AMBIGUOUS: the real app's formats are not documented in this repo,
    see docs/MESHCORE.md):
      meshcore://<hex>                      the advert packet, as "Share contact" produces
      meshcore://contact/add?name=N&public_key=<64 hex>[&type=2]
      <hex>                                 bare advert packet hex
      <64 hex>                              bare public key
    Returns (Advert, signature_ok) where signature_ok is True/False/None
    (None = a bare key or no SHA-512 to check with)."""
    t = text.strip().strip("<>\"'")
    low = t.lower()
    if low.startswith("meshcore://"):
        body = t[len("meshcore://"):]
        if body.lower().startswith("contact/add") or body.lower().startswith("contact?"):
            return _from_query(body)
        t = body
    t = t.split("#")[0].strip()
    raw = _hex(t)
    if len(raw) == 32:
        return Advert(raw, 0, bytes(64), ADV_NONE, ""), None
    pkt = decode(raw)
    if pkt.ptype != PT_ADVERT:
        raise ProtoError("link is not a contact advert")
    adv = parse_advert_payload(pkt.payload)
    import mccurve
    return adv, mccurve.verify(adv.pub, adv.signed_message(), adv.sig)


def _from_query(body):
    q = body.split("?", 1)[1] if "?" in body else ""
    args = {}
    for part in q.split("&"):
        k, _, v = part.partition("=")
        args[k.lower()] = _unquote(v)
    key = args.get("public_key") or args.get("key") or args.get("pubkey")
    if not key:
        raise ProtoError("link has no public_key")
    pub = _hex(key)
    if len(pub) != 32:
        raise ProtoError("public key must be 32 bytes")
    kind = args.get("type", "0")
    kind = int(kind) if kind.isdigit() else {"chat": 1, "repeater": 2, "room": 3,
                                              "sensor": 4}.get(kind.lower(), 0)
    return Advert(pub, 0, bytes(64), kind, args.get("name", "")[:32]), None
