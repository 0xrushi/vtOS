#
# Meshtastic wire format: protobuf subset, packet header, channel keys.
# License: MIT
#
# A small schema-driven protobuf codec (no generated code, no dependencies)
# plus the 16-byte over-the-air header and AES-CTR packet sealing. Field
# numbers match meshtastic/protobufs; utils/tests/test_mtproto.py checks them
# against the official generated classes from the `meshtastic` Python package.
#
# Schema entry: name -> (field_number, type) or (field_number, type, True) for
# repeated. Types: u varint | i int32/64 | s zigzag | b bool | f32 fixed32 |
# sf32 signed fixed32 | flt float | y bytes | t string | (schema,) nested.
#

import struct

import mtcrypto

# --- ports -------------------------------------------------------------------

PORT_TEXT = 1
PORT_REMOTE_HW = 2
PORT_POSITION = 3
PORT_NODEINFO = 4
PORT_ROUTING = 5
PORT_ADMIN = 6
PORT_WAYPOINT = 8
PORT_REPLY = 32
PORT_RANGE_TEST = 66
PORT_TELEMETRY = 67
PORT_TRACEROUTE = 70
PORT_NEIGHBORINFO = 71

BROADCAST = 0xFFFFFFFF
HW_T_DECK = 50
HW_PRIVATE = 255

# Routing.Error
ERR_NONE = 0
ERR_NO_ROUTE = 1
ERR_GOT_NAK = 2
ERR_TIMEOUT = 3
ERR_NO_INTERFACE = 4
ERR_MAX_RETRANSMIT = 5
ERR_NO_CHANNEL = 6
ERR_TOO_LARGE = 7
ERR_NO_RESPONSE = 8
ERR_DUTY_CYCLE_LIMIT = 9
ERR_BAD_REQUEST = 32
ERR_NOT_AUTHORIZED = 33

ERROR_NAMES = {
    0: "ok", 1: "no route", 2: "nak", 3: "timeout", 4: "no interface",
    5: "no ack", 6: "no channel", 7: "too large", 8: "no response",
    9: "duty cycle", 32: "bad request", 33: "not authorized",
}

# Config.DeviceConfig.Role
ROLES = (
    ("CLIENT", 0), ("CLIENT_MUTE", 1), ("ROUTER", 2), ("ROUTER_CLIENT", 3),
    ("REPEATER", 4), ("TRACKER", 5), ("SENSOR", 6), ("CLIENT_HIDDEN", 8),
    ("LOST_AND_FOUND", 9),
)

# --- schemas -----------------------------------------------------------------

DATA = {
    "portnum": (1, "u"), "payload": (2, "y"), "want_response": (3, "b"),
    "dest": (4, "f32"), "source": (5, "f32"), "request_id": (6, "f32"),
    "reply_id": (7, "f32"), "emoji": (8, "f32"), "bitfield": (9, "u"),
}
USER = {
    "id": (1, "t"), "long_name": (2, "t"), "short_name": (3, "t"),
    "macaddr": (4, "y"), "hw_model": (5, "u"), "is_licensed": (6, "b"),
    "role": (7, "u"), "public_key": (8, "y"),
}
POSITION = {
    "latitude_i": (1, "sf32"), "longitude_i": (2, "sf32"), "altitude": (3, "i"),
    "time": (4, "f32"), "location_source": (5, "u"), "altitude_source": (6, "u"),
    "timestamp": (7, "f32"), "timestamp_millis_adjust": (8, "i"),
    "altitude_hae": (9, "s"), "altitude_geoidal_separation": (10, "s"),
    "PDOP": (11, "u"), "HDOP": (12, "u"), "VDOP": (13, "u"),
    "gps_accuracy": (14, "u"), "ground_speed": (15, "u"),
    "ground_track": (16, "u"), "fix_quality": (17, "u"), "fix_type": (18, "u"),
    "sats_in_view": (19, "u"), "sensor_id": (20, "u"), "next_update": (21, "u"),
    "seq_number": (22, "u"), "precision_bits": (23, "u"),
}
DEVICE_METRICS = {
    "battery_level": (1, "u"), "voltage": (2, "flt"),
    "channel_utilization": (3, "flt"), "air_util_tx": (4, "flt"),
    "uptime_seconds": (5, "u"),
}
TELEMETRY = {
    "time": (1, "f32"), "device_metrics": (2, (DEVICE_METRICS,)),
}
ROUTE_DISCOVERY = {
    "route": (1, "f32", True), "snr_towards": (2, "i", True),
    "route_back": (3, "f32", True), "snr_back": (4, "i", True),
}
ROUTING = {
    "route_request": (1, (ROUTE_DISCOVERY,)),
    "route_reply": (2, (ROUTE_DISCOVERY,)),
    "error_reason": (3, "u"),
}
WAYPOINT = {
    "id": (1, "u"), "latitude_i": (2, "sf32"), "longitude_i": (3, "sf32"),
    "expire": (4, "u"), "locked_to": (5, "u"), "name": (6, "t"),
    "description": (7, "t"), "icon": (8, "f32"),
}
MODULE_SETTINGS = {"position_precision": (1, "u"), "is_client_muted": (2, "b")}
CHANNEL_SETTINGS = {
    "channel_num": (1, "u"), "psk": (2, "y"), "name": (3, "t"), "id": (4, "f32"),
    "uplink_enabled": (5, "b"), "downlink_enabled": (6, "b"),
    "module_settings": (7, (MODULE_SETTINGS,)),
}
LORA_CONFIG = {
    "use_preset": (1, "b"), "modem_preset": (2, "u"), "bandwidth": (3, "u"),
    "spread_factor": (4, "u"), "coding_rate": (5, "u"),
    "frequency_offset": (6, "flt"), "region": (7, "u"), "hop_limit": (8, "u"),
    "tx_enabled": (9, "b"), "tx_power": (10, "i"), "channel_num": (11, "u"),
    "override_duty_cycle": (12, "b"), "sx126x_rx_boosted_gain": (13, "b"),
    "override_frequency": (14, "flt"),
}
CHANNEL_SET = {
    "settings": (1, (CHANNEL_SETTINGS,), True),
    "lora_config": (2, (LORA_CONFIG,)),
}


# --- varints and fields ------------------------------------------------------

def put_varint(n):
    n &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def get_varint(buf, i):
    n = 0
    shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("bad varint")
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7


def _zigzag(n, bits=32):
    return ((n << 1) ^ (n >> (bits - 1))) & ((1 << bits) - 1)


def _unzigzag(n):
    return (n >> 1) ^ -(n & 1)


def _scalar(num, typ, v):
    """Encode one non-nested value; returns bytes ("" for proto3 defaults)."""
    if typ in ("u", "b"):
        return put_varint((num << 3) | 0) + put_varint(int(v)) if v else b""
    if typ == "i":
        return put_varint((num << 3) | 0) + put_varint(v) if v else b""
    if typ == "s":
        return put_varint((num << 3) | 0) + put_varint(_zigzag(v)) if v else b""
    if typ == "f32":
        return put_varint((num << 3) | 5) + struct.pack("<I", v & 0xFFFFFFFF) if v else b""
    if typ == "sf32":
        return put_varint((num << 3) | 5) + struct.pack("<i", v) if v else b""
    if typ == "flt":
        return put_varint((num << 3) | 5) + struct.pack("<f", v) if v else b""
    if typ == "y":
        v = bytes(v)
        return put_varint((num << 3) | 2) + put_varint(len(v)) + v if v else b""
    if typ == "t":
        v = v.encode()
        return put_varint((num << 3) | 2) + put_varint(len(v)) + v if v else b""
    raise ValueError("bad type")


def encode(schema, d):
    """dict -> protobuf bytes. Missing/zero/empty fields are omitted."""
    out = []
    for name in sorted(schema, key=lambda k: schema[k][0]):
        ent = schema[name]
        v = d.get(name)
        if v is None:
            continue
        num, typ = ent[0], ent[1]
        repeated = len(ent) > 2
        if isinstance(typ, tuple):
            for item in (v if repeated else [v]):
                body = encode(typ[0], item)
                out.append(put_varint((num << 3) | 2) + put_varint(len(body)) + body)
        elif repeated:
            if not v:
                continue
            if typ in ("f32", "sf32", "flt"):
                body = b"".join(_scalar(1, typ, x)[1:] if x else b"\x00\x00\x00\x00" for x in v)
            else:
                body = b"".join(put_varint(_zigzag(x) if typ == "s" else x) for x in v)
            out.append(put_varint((num << 3) | 2) + put_varint(len(body)) + body)
        else:
            out.append(_scalar(num, typ, v))
    return b"".join(out)


def _fields(buf):
    """Split a message into [(field, wiretype, value)]; raises on malformed."""
    i = 0
    n = len(buf)
    out = []
    while i < n:
        key, i = get_varint(buf, i)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = get_varint(buf, i)
        elif wt == 1:
            v = buf[i:i + 8]
            i += 8
        elif wt == 2:
            ln, i = get_varint(buf, i)
            v = buf[i:i + ln]
            if len(v) != ln:
                raise ValueError("truncated")
            i += ln
        elif wt == 5:
            v = buf[i:i + 4]
            i += 4
        else:
            raise ValueError("wire type %d" % wt)
        if i > n:
            raise ValueError("truncated")
        out.append((num, wt, v))
    return out


def _default(typ, repeated):
    if repeated:
        return []
    if isinstance(typ, tuple):
        return None
    return {"b": False, "y": b"", "t": "", "flt": 0.0}.get(typ, 0)


def _unpack_one(typ, v, wt):
    if typ in ("u", "b"):
        if wt != 0:
            raise ValueError("type")
        return bool(v) if typ == "b" else v
    if typ == "i":
        if wt != 0:
            raise ValueError("type")
        return v - (1 << 64) if v >= 1 << 63 else v
    if typ == "s":
        return _unzigzag(v)
    if typ == "f32":
        return struct.unpack("<I", v)[0]
    if typ == "sf32":
        return struct.unpack("<i", v)[0]
    if typ == "flt":
        return struct.unpack("<f", v)[0]
    if typ == "y":
        return bytes(v)
    if typ == "t":
        return bytes(v).decode()
    raise ValueError("bad type")


def decode(schema, buf):
    """protobuf bytes -> dict with every schema field present (defaults filled)."""
    by_num = {}
    for name, ent in schema.items():
        by_num[ent[0]] = (name, ent[1], len(ent) > 2)
    out = {}
    for name, ent in schema.items():
        out[name] = _default(ent[1], len(ent) > 2)
    for num, wt, v in _fields(bytes(buf)):
        if num not in by_num:
            continue
        name, typ, repeated = by_num[num]
        if isinstance(typ, tuple):
            item = decode(typ[0], v)
            if repeated:
                out[name].append(item)
            else:
                out[name] = item
        elif repeated:
            if wt == 2 and typ != "y" and typ != "t":     # packed
                if typ in ("f32", "sf32", "flt"):
                    for k in range(0, len(v) - len(v) % 4, 4):
                        out[name].append(_unpack_one(typ, v[k:k + 4], 5))
                else:
                    j = 0
                    while j < len(v):
                        x, j = get_varint(v, j)
                        out[name].append(_unpack_one(typ, x, 0))
            else:
                out[name].append(_unpack_one(typ, v, wt))
        else:
            out[name] = _unpack_one(typ, v, wt)
    return out


# --- channel keys ------------------------------------------------------------

DEFAULT_PSK = bytes([0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59,
                     0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01])


def expand_psk(psk):
    """The key actually used: b"" (no encryption), 16 or 32 bytes.

    A one-byte PSK is an index into the well-known default key family: 0 means
    none, 1 is the default key, n adds (n - 1) to its last byte.
    """
    psk = bytes(psk)
    if len(psk) == 0:
        return b""
    if len(psk) == 1:
        if psk[0] == 0:
            return b""
        return DEFAULT_PSK[:-1] + bytes([(DEFAULT_PSK[-1] + psk[0] - 1) & 0xFF])
    if len(psk) < 16:
        return psk + b"\x00" * (16 - len(psk))
    if len(psk) < 32 and len(psk) != 16:
        return psk + b"\x00" * (32 - len(psk))
    return psk[:32]


def channel_hash(name, key):
    """One-byte channel id carried in the header: xor of name and key bytes."""
    h = 0
    for b in name.encode():
        h ^= b
    for b in key:
        h ^= b
    return h


def key_kind(psk):
    """Human label for the key a PSK expands to."""
    key = expand_psk(psk)
    if not key:
        return "none"
    if key == DEFAULT_PSK:
        return "default"
    return "AES%d" % (len(key) * 8)


# --- packet ------------------------------------------------------------------

HDR_LEN = 16
MAX_PACKET = 255
_HDR = "<IIIBBBB"


def pack_header(to, frm, pid, hop_limit=3, want_ack=False, via_mqtt=False,
                hop_start=3, chan_hash=0, next_hop=0, relay=0):
    flags = (hop_limit & 7) | (8 if want_ack else 0) | (16 if via_mqtt else 0) \
        | ((hop_start & 7) << 5)
    return struct.pack(_HDR, to, frm, pid, flags, chan_hash, next_hop, relay)


def unpack_header(raw):
    if len(raw) < HDR_LEN:
        raise ValueError("short packet")
    to, frm, pid, flags, chan, nh, relay = struct.unpack(_HDR, raw[:HDR_LEN])
    return {
        "to": to, "from": frm, "id": pid, "hop_limit": flags & 7,
        "want_ack": bool(flags & 8), "via_mqtt": bool(flags & 16),
        "hop_start": (flags >> 5) & 7, "channel": chan, "next_hop": nh,
        "relay": relay,
    }


def nonce(pid, frm):
    """CTR nonce: 64-bit packet id, 32-bit sender, 32-bit zero counter."""
    return struct.pack("<QII", pid, frm, 0)


def crypt(key, frm, pid, data):
    if not key:
        return bytes(data)
    return mtcrypto.ctr(key, nonce(pid, frm), data)


def seal(hdr, data_bytes, key):
    """Header dict + plaintext Data protobuf -> bytes ready for the radio."""
    raw = pack_header(hdr["to"], hdr["from"], hdr["id"], hdr.get("hop_limit", 3),
                      hdr.get("want_ack", False), hdr.get("via_mqtt", False),
                      hdr.get("hop_start", hdr.get("hop_limit", 3)),
                      hdr.get("channel", 0), hdr.get("next_hop", 0),
                      hdr.get("relay", 0))
    body = crypt(key, hdr["from"], hdr["id"], data_bytes)
    if len(raw) + len(body) > MAX_PACKET:
        raise ValueError("packet too large")
    return raw + body


def open_data(hdr, body, key):
    """Decrypt body with key and parse it as Data; raises ValueError if it isn't."""
    d = decode(DATA, crypt(key, hdr["from"], hdr["id"], body))
    if d["portnum"] == 0 and not d["payload"]:
        raise ValueError("empty")
    if d["portnum"] > 1023:
        raise ValueError("implausible port")
    return d


# --- convenience encoders ----------------------------------------------------

def data_bytes(portnum, payload=b"", want_response=False, dest=0, source=0,
               request_id=0, reply_id=0):
    return encode(DATA, {"portnum": portnum, "payload": payload,
                         "want_response": want_response, "dest": dest,
                         "source": source, "request_id": request_id,
                         "reply_id": reply_id})


def node_id_str(num):
    return "!%08x" % num


# --- channel sharing URL -----------------------------------------------------

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
URL_PREFIX = "https://meshtastic.org/e/#"


def b64url_encode(data):
    out = []
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        n = int.from_bytes(chunk + b"\x00" * (3 - len(chunk)), "big")
        sym = [_B64[(n >> s) & 63] for s in (18, 12, 6, 0)]
        out.append("".join(sym[:len(chunk) + 1]))
    return "".join(out)


def b64url_decode(text):
    text = text.strip().replace("+", "-").replace("/", "_").rstrip("=")
    acc = 0
    bits = 0
    out = bytearray()
    for ch in text:
        v = _B64.find(ch)
        if v < 0:
            raise ValueError("bad base64")
        acc = (acc << 6) | v
        bits += 6
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
            acc &= (1 << bits) - 1
    return bytes(out)


def channel_set_url(settings, lora):
    return URL_PREFIX + b64url_encode(encode(CHANNEL_SET, {"settings": settings,
                                                          "lora_config": lora}))


def parse_channel_url(url):
    """Returns the decoded ChannelSet dict; ValueError on a bad URL."""
    frag = url.strip().split("#")[-1]
    cs = decode(CHANNEL_SET, b64url_decode(frag))
    if not cs["settings"]:
        raise ValueError("no channels in URL")
    return cs
