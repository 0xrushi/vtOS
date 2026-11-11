"""MeshCore crypto: Curve25519/Ed25519 maths and AES-ECB against reference
implementations (pycryptodome, RFC 7748)."""

import hashlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "modules/scripts/lib"))

import mccurve as C  # noqa: E402
import mtcrypto  # noqa: E402

Crypto = pytest.importorskip("Crypto")
from Crypto.Cipher import AES  # noqa: E402
from Crypto.PublicKey import ECC  # noqa: E402
from Crypto.Signature import eddsa  # noqa: E402


def test_x25519_rfc7748_vector():
    k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
    u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    assert C.x25519(k, u).hex() == \
        "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"


def test_public_key_matches_reference():
    for _ in range(3):
        seed = os.urandom(32)
        h = hashlib.sha512(seed).digest()
        priv = C.clamp(h[:32]) + h[32:]
        ref = ECC.construct(curve="Ed25519", seed=seed).public_key().export_key(format="raw")
        assert C.public_from_private(priv) == ref


def test_verify_signature():
    key = ECC.generate(curve="Ed25519")
    sig = eddsa.new(key, "rfc8032").sign(b"advert")
    pub = key.public_key().export_key(format="raw")
    assert C.verify(pub, b"advert", sig) is True
    assert C.verify(pub, b"advers", sig) is False
    assert C.verify(pub, b"advert", sig[:-1] + bytes([sig[-1] ^ 1])) is False


def test_shared_secret_is_symmetric_and_rejects_bad_key():
    a, b = C.new_private(os.urandom(64)), C.new_private(os.urandom(64))
    pa, pb = C.public_from_private(a), C.public_from_private(b)
    assert C.shared_secret(a, pb) == C.shared_secret(b, pa)
    assert C.shared_secret(a, b"\xff" * 32) is None


def test_ecb_pure_matches_pycryptodome(monkeypatch):
    monkeypatch.setattr(mtcrypto, "_ecb_ok", False)       # force the pure path
    for klen in (16, 32):
        key, data = os.urandom(klen), os.urandom(80)
        enc = AES.new(key, AES.MODE_ECB).encrypt(data)
        assert mtcrypto.ecb(key, data) == enc
        assert mtcrypto.ecb(key, enc, decrypt=True) == data


def test_ecb_needs_whole_blocks():
    with pytest.raises(ValueError):
        mtcrypto.ecb(bytes(16), b"abc")
