"""MeshCore packet codec, sealed payloads, adverts and contact links.

All layouts are written from memory of the MeshCore sources (see lib/mcproto.py);
these tests prove the code is self-consistent, NOT that it matches real nodes."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "modules/scripts/lib"))

import mccurve as C  # noqa: E402
import mcproto as W  # noqa: E402

pytest.importorskip("Crypto")
from Crypto.PublicKey import ECC  # noqa: E402
from Crypto.Signature import eddsa  # noqa: E402


def test_public_channel_hash_is_0x11():
    assert W.channel_hash(W.PUBLIC_SECRET) == 0x11


def test_hmac_matches_stdlib():
    import hmac
    import hashlib
    for klen in (16, 32, 80):
        key = os.urandom(klen)
        assert W.hmac_sha256(key, b"data") == hmac.new(key, b"data", hashlib.sha256).digest()


def test_packet_roundtrip_flood_and_transport():
    p = W.Packet(W.PT_TXT, b"x" * 30, W.ROUTE_FLOOD, path=b"\x01\x02")
    d = W.decode(p.encode())
    assert (d.ptype, d.route, d.path, d.payload) == (W.PT_TXT, W.ROUTE_FLOOD, b"\x01\x02", b"x" * 30)
    t = W.Packet(W.PT_ADVERT, b"y" * 10, W.ROUTE_TRANSPORT_FLOOD, transport=b"\xaa\xbb\xcc\xdd")
    d = W.decode(t.encode())
    assert d.transport == b"\xaa\xbb\xcc\xdd" and d.payload == b"y" * 10


def test_decode_rejects_garbage():
    for raw in (b"", b"\x05", b"\x05\x40" + b"x" * 5, b"\x05\x00"):
        with pytest.raises(W.ProtoError):
            W.decode(raw)


def test_seal_unseal_and_tamper():
    secret = os.urandom(32)
    blob = W.seal(secret, b"hello there")
    assert W.unseal(secret, blob).rstrip(b"\x00") == b"hello there"
    bad = bytearray(blob)
    bad[-1] ^= 1
    assert W.unseal(secret, bytes(bad)) is None
    assert W.unseal(os.urandom(32), blob) is None


def test_group_secret_is_padded_for_mac():
    pkt = W.build_group(W.PUBLIC_SECRET, W.group_plain(1700000000, "Bob", "hi"))
    assert pkt.payload[0] == 0x11
    plain = W.unseal(W.PUBLIC_SECRET, pkt.payload[1:])
    assert plain[5:].rstrip(b"\x00") == b"Bob: hi"


def test_txt_plain_roundtrip_and_ack():
    plain = W.txt_plain(1700000000, "ping", W.TXT_CLI << 2)
    ts, ttype, attempt, text = W.parse_txt(plain + bytes(7))
    assert (ts, ttype, attempt, text) == (1700000000, W.TXT_CLI, 0, "ping")
    pub = os.urandom(32)
    assert W.expected_ack(plain, pub) == W.expected_ack(plain + b"\x00\x00", pub)
    assert len(W.expected_ack(plain, pub)) == 4


def test_login_response_parse():
    ok = (1).to_bytes(4, "little") + bytes([0, 5, 1, 3]) + bytes(8)
    assert W.parse_login_response(ok) == (True, True)
    assert W.parse_login_response(bytes(4) + b"\x01" + bytes(11))[0] is False
    assert W.parse_login_response(b"ab") is None


def _signed_advert(name="Hill Repeater", kind=W.ADV_REPEATER, lat=37.5, lon=-122.25):
    key = ECC.generate(curve="Ed25519")
    pub = key.public_key().export_key(format="raw")
    ts = 1700000000
    app = W.build_advert_payload(pub, ts, bytes(64), kind, name, lat, lon)[100:]
    sig = eddsa.new(key, "rfc8032").sign(pub + ts.to_bytes(4, "little") + app)
    return pub, W.build_advert_payload(pub, ts, sig, kind, name, lat, lon)


def test_advert_parse_fields():
    pub, payload = _signed_advert()
    a = W.parse_advert_payload(payload)
    assert (a.pub, a.kind, a.name) == (pub, W.ADV_REPEATER, "Hill Repeater")
    assert (a.lat, a.lon) == (37.5, -122.25)
    assert C.verify(a.pub, a.signed_message(), a.sig) is True


def test_advert_without_location_or_name():
    payload = W.build_advert_payload(bytes(32), 5, bytes(64), W.ADV_CHAT, "")
    a = W.parse_advert_payload(payload)
    assert a.name == "" and a.lat is None and a.kind == W.ADV_CHAT


def test_link_with_advert_packet_verifies():
    pub, payload = _signed_advert()
    link = "meshcore://" + W.Packet(W.PT_ADVERT, payload).encode().hex()
    adv, sig_ok = W.parse_contact_link(link)
    assert adv.pub == pub and adv.name == "Hill Repeater" and sig_ok is True
    adv2, _ = W.parse_contact_link(link[len("meshcore://"):])       # bare hex
    assert adv2.pub == pub


def test_link_with_bad_signature_is_flagged():
    _, payload = _signed_advert()
    bad = bytearray(payload)
    bad[40] ^= 0xFF
    link = "meshcore://" + W.Packet(W.PT_ADVERT, bytes(bad)).encode().hex()
    assert W.parse_contact_link(link)[1] is False


def test_link_query_form_and_bare_key():
    pub = os.urandom(32).hex()
    adv, sig = W.parse_contact_link(
        "meshcore://contact/add?name=Ridge%20Rpt&public_key=" + pub + "&type=2")
    assert adv.pub.hex() == pub and adv.name == "Ridge Rpt" and adv.kind == W.ADV_REPEATER
    assert sig is None
    adv, _ = W.parse_contact_link(pub)
    assert adv.pub.hex() == pub and adv.kind == W.ADV_NONE


@pytest.mark.parametrize("text", ["", "meshcore://", "meshcore://zz", "meshcore://abc",
                                  "meshcore://contact/add?name=x",
                                  "meshcore://contact/add?public_key=abcd",
                                  "meshcore://" + W.Packet(W.PT_TXT, b"x" * 20).encode().hex()])
def test_link_rejects_bad_input(text):
    with pytest.raises(W.ProtoError):
        W.parse_contact_link(text)
