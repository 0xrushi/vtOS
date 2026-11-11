#
# Curve25519 / Ed25519 maths for MeshCore, pure Python (no sha512 needed
# except to verify signatures).
# License: MIT
#
# MeshCore identities are Ed25519 keys (orlp's ed25519 library). The private key
# is stored as the 64-byte *expanded* key (clamped scalar + prefix), and a shared
# secret between two nodes is X25519 between our clamped scalar and the peer's
# Ed25519 public key converted to Montgomery form (u = (1 + y) / (1 - y)). That is
# what `ed25519_key_exchange` does, and why no SHA-512 is needed on the device to
# talk to repeaters. Big-int arithmetic: about a second per scalar multiplication
# on the T-Deck, so callers cache secrets.
#

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, P - 2, P)) % P
_I = pow(2, (P - 1) // 4, P)
_BY = 4 * pow(5, P - 2, P) % P


def _inv(x):
    return pow(x, P - 2, P)


def _recover_x(y, sign):
    xx = (y * y - 1) * _inv(D * y * y + 1) % P
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P:
        x = x * _I % P
    if (x * x - xx) % P:
        return None
    if x & 1 != sign:
        x = P - x
    return x


_BX = _recover_x(_BY, 0)
_B = (_BX, _BY, 1, _BX * _BY % P)          # extended coordinates (X, Y, Z, T)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % P
    b = (p[1] + p[0]) * (q[1] + q[0]) % P
    c = 2 * p[3] * q[3] * D % P
    d = 2 * p[2] * q[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _encode(p):
    zi = _inv(p[2])
    x, y = p[0] * zi % P, p[1] * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(b):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= P:
        return None
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def clamp(scalar32):
    a = bytearray(scalar32)
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return bytes(a)


def new_private(random_bytes):
    """64-byte expanded private key from 64 random bytes (as orlp's keygen leaves it)."""
    if len(random_bytes) != 64:
        raise ValueError("need 64 random bytes")
    return clamp(random_bytes[:32]) + bytes(random_bytes[32:])


def public_from_private(priv64):
    a = int.from_bytes(priv64[:32], "little")
    return _encode(_mul(a, _B))


def to_montgomery(pub32):
    """Ed25519 public key -> X25519 u coordinate, or None if not a valid point."""
    pt = _decode(pub32)
    if pt is None:
        return None
    y = pt[1]
    if y == 1:
        return None
    return ((1 + y) * _inv(1 - y) % P).to_bytes(32, "little")


def x25519(k32, u32):
    """RFC 7748 X25519 (the scalar is clamped here)."""
    k = int.from_bytes(clamp(k32), "little")
    u = int.from_bytes(u32, "little") & ((1 << 255) - 1)
    x1, x2, z2, x3, z3, swap = u, 1, 0, u, 1, 0
    a24 = 121665
    for t in range(254, -1, -1):
        bit = (k >> t) & 1
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a = (x2 + z2) % P
        aa = a * a % P
        b = (x2 - z2) % P
        bb = b * b % P
        e = (aa - bb) % P
        c = (x3 + z3) % P
        d = (x3 - z3) % P
        da = d * a % P
        cb = c * b % P
        x3 = (da + cb) ** 2 % P
        z3 = x1 * (da - cb) ** 2 % P
        x2 = aa * bb % P
        z2 = e * (aa + a24 * e) % P
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * _inv(z2) % P).to_bytes(32, "little")


def shared_secret(priv64, peer_pub32):
    """MeshCore's ed25519_key_exchange: 32-byte secret, or None for a bad peer key."""
    u = to_montgomery(peer_pub32)
    if u is None:
        return None
    return x25519(priv64[:32], u)


def verify(pub32, msg, sig64):
    """Ed25519 signature check. Needs hashlib.sha512; returns None when that is
    missing (cannot tell), else True/False."""
    try:
        from hashlib import sha512
    except ImportError:
        return None
    try:
        sha512(b"").digest()
    except Exception:
        return None
    a = _decode(pub32)
    r = _decode(sig64[:32])
    s = int.from_bytes(sig64[32:], "little")
    if a is None or r is None or s >= L:
        return False
    h = int.from_bytes(sha512(sig64[:32] + pub32 + msg).digest(), "little") % L
    lhs = _mul(s, _B)
    rhs = _add(r, _mul(h, a))
    # compare projectively: X1*Z2 == X2*Z1 and Y1*Z2 == Y2*Z1
    return ((lhs[0] * rhs[2] - rhs[0] * lhs[2]) % P == 0
            and (lhs[1] * rhs[2] - rhs[1] * lhs[2]) % P == 0)
