"""Meshtastic wire format: checked against the official protobuf classes and
known values. Needs the `meshtastic` and `cryptography` packages (host only)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules/scripts/lib"))

import mtcrypto  # noqa: E402
import mtproto  # noqa: E402
import mtradio  # noqa: E402

pb = pytest.importorskip("meshtastic.mesh_pb2")
cfg_pb = pytest.importorskip("meshtastic.config_pb2")
chan_pb = pytest.importorskip("meshtastic.channel_pb2")
app_pb = pytest.importorskip("meshtastic.apponly_pb2")
tel_pb = pytest.importorskip("meshtastic.telemetry_pb2")


# --- AES-CTR -----------------------------------------------------------------

@pytest.mark.parametrize("klen", [16, 32])
@pytest.mark.parametrize("n", [1, 15, 16, 17, 100, 239])
def test_pure_ctr_matches_cryptography(klen, n):
    cryptography = pytest.importorskip("cryptography.hazmat.primitives.ciphers")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = bytes((i * 7 + 3) & 0xFF for i in range(klen))
    nonce = bytes(range(16))
    data = bytes((i * 13) & 0xFF for i in range(n))
    enc = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    assert mtcrypto.ctr_pure(key, nonce, data) == enc.update(data) + enc.finalize()


def test_counter_carries_across_bytes():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = bytes(range(16))
    nonce = b"\x00" * 12 + b"\x00\x00\x00\xff"        # next block carries
    data = bytes(48)
    enc = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    assert mtcrypto.ctr_pure(key, nonce, data) == enc.update(data)


# --- protobuf codec vs official classes -------------------------------------

def test_data_round_trips_with_official_class():
    ours = mtproto.encode(mtproto.DATA, {"portnum": 1, "payload": b"hello", "want_response": True,
                                         "dest": 0xDEADBEEF, "request_id": 7})
    theirs = pb.Data()
    theirs.ParseFromString(ours)
    assert (theirs.portnum, theirs.payload, theirs.want_response, theirs.dest, theirs.request_id) == \
        (1, b"hello", True, 0xDEADBEEF, 7)
    official = pb.Data(portnum=3, payload=b"\x01\x02", source=5, reply_id=9).SerializeToString()
    d = mtproto.decode(mtproto.DATA, official)
    assert (d["portnum"], d["payload"], d["source"], d["reply_id"]) == (3, b"\x01\x02", 5, 9)


def test_user_position_telemetry_routing_match_official():
    u = pb.User(id="!1234abcd", long_name="Rush", short_name="RUSH", hw_model=50, role=2,
                macaddr=b"\x01\x02\x03\x04\x05\x06")
    d = mtproto.decode(mtproto.USER, u.SerializeToString())
    assert (d["id"], d["long_name"], d["short_name"], d["hw_model"], d["role"], d["macaddr"]) == \
        ("!1234abcd", "Rush", "RUSH", 50, 2, b"\x01\x02\x03\x04\x05\x06")
    assert mtproto.encode(mtproto.USER, d) == u.SerializeToString()

    p = pb.Position(latitude_i=377749000, longitude_i=-1224194000, altitude=-12, time=1700000000,
                    sats_in_view=7, precision_bits=13, ground_speed=3)
    d = mtproto.decode(mtproto.POSITION, p.SerializeToString())
    assert (d["latitude_i"], d["longitude_i"], d["altitude"], d["time"]) == \
        (377749000, -1224194000, -12, 1700000000)
    assert mtproto.encode(mtproto.POSITION, d) == p.SerializeToString()

    t = tel_pb.Telemetry(time=5)
    t.device_metrics.battery_level = 87
    t.device_metrics.voltage = 4.125
    t.device_metrics.uptime_seconds = 999
    d = mtproto.decode(mtproto.TELEMETRY, t.SerializeToString())
    assert d["device_metrics"]["battery_level"] == 87
    assert d["device_metrics"]["voltage"] == pytest.approx(4.125)
    assert mtproto.encode(mtproto.TELEMETRY, d) == t.SerializeToString()

    r = pb.Routing(error_reason=5)
    assert mtproto.decode(mtproto.ROUTING, r.SerializeToString())["error_reason"] == 5
    assert mtproto.encode(mtproto.ROUTING, {"error_reason": 5}) == r.SerializeToString()


def test_route_discovery_packed_repeated():
    rd = pb.RouteDiscovery(route=[1, 2, 0xFFFFFF00])
    d = mtproto.decode(mtproto.ROUTE_DISCOVERY, rd.SerializeToString())
    assert d["route"] == [1, 2, 0xFFFFFF00]
    assert mtproto.encode(mtproto.ROUTE_DISCOVERY, {"route": [1, 2, 0xFFFFFF00]}) == rd.SerializeToString()


def test_channel_url_matches_official_format():
    s = chan_pb.ChannelSettings(psk=b"\x01", name="")
    cs = app_pb.ChannelSet()
    cs.settings.append(s)
    cs.settings.append(chan_pb.ChannelSettings(psk=bytes(range(16)), name="Hike", uplink_enabled=True))
    cs.lora_config.region = 1
    cs.lora_config.modem_preset = 0
    cs.lora_config.use_preset = True
    cs.lora_config.hop_limit = 3
    import base64
    official = base64.urlsafe_b64encode(cs.SerializeToString()).decode().rstrip("=")
    ours = mtproto.channel_set_url(
        [{"psk": b"\x01"}, {"psk": bytes(range(16)), "name": "Hike", "uplink_enabled": True}],
        {"region": 1, "modem_preset": 0, "use_preset": True, "hop_limit": 3})
    assert ours == "https://meshtastic.org/e/#" + official
    back = mtproto.parse_channel_url(official)
    assert back["settings"][1]["name"] == "Hike" and back["lora_config"]["region"] == 1
    assert mtproto.parse_channel_url("https://meshtastic.org/e/#" + official)["settings"][0]["psk"] == b"\x01"


def test_lora_config_matches_official():
    cfg = cfg_pb.Config.LoRaConfig(use_preset=True, modem_preset=4, region=1, hop_limit=5,
                                   tx_enabled=True, tx_power=-3, channel_num=20,
                                   sx126x_rx_boosted_gain=True)
    d = mtproto.decode(mtproto.LORA_CONFIG, cfg.SerializeToString())
    assert (d["modem_preset"], d["region"], d["hop_limit"], d["tx_power"], d["channel_num"]) == \
        (4, 1, 5, -3, 20)
    assert mtproto.encode(mtproto.LORA_CONFIG, d) == cfg.SerializeToString()


def test_decode_rejects_garbage():
    for bad in (b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff", b"\x0a\x20abc", b"\x07"):
        with pytest.raises(ValueError):
            mtproto.decode(mtproto.DATA, bad)


# --- keys, hash, header ------------------------------------------------------

def test_default_key_and_channel_hash_for_longfast():
    key = mtproto.expand_psk(b"\x01")
    assert key == mtproto.DEFAULT_PSK and len(key) == 16
    assert mtproto.channel_hash("LongFast", key) == 8       # the well-known default channel id
    assert mtproto.expand_psk(b"\x02")[-1] == 0x02
    assert mtproto.expand_psk(b"\x00") == b""
    assert mtproto.expand_psk(b"abc") == b"abc" + b"\x00" * 13
    assert mtproto.key_kind(b"\x01") == "default" and mtproto.key_kind(bytes(32)) == "AES256"


def test_header_layout_and_flags():
    raw = mtproto.pack_header(0xFFFFFFFF, 0x11223344, 0xAABBCCDD, hop_limit=3, want_ack=True,
                              hop_start=5, chan_hash=8, next_hop=0, relay=0x44)
    assert len(raw) == 16
    assert raw[:4] == b"\xff\xff\xff\xff" and raw[4:8] == bytes.fromhex("44332211")
    assert raw[8:12] == bytes.fromhex("ddccbbaa")
    assert raw[12] == 3 | 8 | (5 << 5) and raw[13] == 8 and raw[15] == 0x44
    h = mtproto.unpack_header(raw + b"payload")
    assert (h["hop_limit"], h["hop_start"], h["want_ack"], h["channel"]) == (3, 5, True, 8)


def test_seal_and_open_round_trip_and_wrong_key_fails():
    key = mtproto.expand_psk(b"\x01")
    data = mtproto.data_bytes(mtproto.PORT_TEXT, b"hi there")
    hdr = {"to": mtproto.BROADCAST, "from": 0x1234, "id": 77, "channel": 8, "hop_limit": 3}
    raw = mtproto.seal(hdr, data, key)
    h = mtproto.unpack_header(raw)
    d = mtproto.open_data(h, raw[16:], key)
    assert d["portnum"] == 1 and d["payload"] == b"hi there"
    assert raw[16:] != data                                  # actually encrypted
    bad = mtproto.expand_psk(b"\x02")
    with pytest.raises(ValueError):
        for _ in range(1):
            mtproto.open_data(h, raw[16:], bad)


def test_too_large_packet_rejected():
    with pytest.raises(ValueError):
        mtproto.seal({"to": 1, "from": 2, "id": 3}, bytes(300), b"")


# --- radio -------------------------------------------------------------------

def test_us_longfast_default_slot_is_906_875():
    us = mtradio.region("US")
    lf = mtradio.preset("LONG_FAST")
    assert mtradio.num_slots(us, lf[3]) == 104
    assert mtradio.default_slot("LongFast", 104) == 20
    assert mtradio.frequency(us, lf[3], "LongFast") == pytest.approx(906.875)


def test_eu_slots_match_docs():
    eu433 = mtradio.region("EU_433")
    eu868 = mtradio.region("EU_868")
    assert mtradio.num_slots(eu433, 250.0) == 4
    assert mtradio.frequency(eu433, 250.0, "LongFast") == pytest.approx(433.875)
    assert mtradio.num_slots(eu868, 250.0) == 1
    assert mtradio.frequency(eu868, 250.0, "LongFast") == pytest.approx(869.525)


def test_slot_override_and_offset():
    us = mtradio.region("US")
    assert mtradio.frequency(us, 250.0, "x", slot=1) == pytest.approx(902.125)
    assert mtradio.frequency(us, 250.0, "x", slot=1, offset_mhz=0.01) == pytest.approx(902.135)
    assert mtradio.frequency(us, 250.0, "x", override=915.0) == 915.0


def test_airtime_longfast_is_plausible():
    # By hand: Tsym 8.192 ms, 20.25 preamble symbols + 38 payload symbols = 477 ms
    t = mtradio.airtime_ms(32, 250.0, 11, 5)
    assert t == pytest.approx(477.2, abs=0.5)
    assert 900 < mtradio.airtime_ms(100, 250.0, 11, 5) < 1100
    assert mtradio.airtime_ms(32, 250.0, 7, 5) < 80


def test_power_is_clamped_to_region_and_hardware():
    assert mtradio.clamp_power(mtradio.region("US"), 30) == 22
    assert mtradio.clamp_power(mtradio.region("EU_433"), 20) == 10
    assert mtradio.clamp_power(mtradio.region("US"), 0) == 22


def test_native_ecb_counter_path_matches_pure(monkeypatch):
    """The cryptolib path used on the T-Deck (ECB over counter blocks), emulated with
    the pure block cipher, must equal plain CTR including counter carries."""
    class FakeCryptolib:
        class aes:
            def __init__(self, key, mode, iv=None):
                assert mode == 1
                self.cipher = mtcrypto._AES(key)

            def encrypt(self, blocks):
                return b"".join(self.cipher.encrypt_block(blocks[i:i + 16])
                                for i in range(0, len(blocks), 16))

    mtcrypto._tables()
    monkeypatch.setattr(mtcrypto, "cryptolib", FakeCryptolib)
    monkeypatch.setattr(mtcrypto, "_native_mode", "ecb")
    for klen in (16, 32):
        key = bytes(range(klen))
        nonce = b"\x00" * 12 + b"\xff\xff\xff\xfe"        # counter carries across the word
        for n in (0, 1, 16, 17, 100, 239):
            data = bytes((i * 31) & 0xFF for i in range(n))
            assert mtcrypto._ctr_native(key, nonce, data) == mtcrypto.ctr_pure(key, nonce, data)
