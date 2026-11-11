#
# Meshtastic payload crypto: AES-128/256 in CTR mode.
# License: MIT
#
# On the T-Deck MicroPython's `cryptolib` does the work. Because a wrong
# counter convention would silently produce garbage, the first call checks
# cryptolib against a small pure-Python AES and falls back to that if they
# disagree (or if cryptolib is missing, as on the host). The pure version is
# slow (milliseconds per block on the device) but correct.
#

try:
    import cryptolib
except ImportError:
    cryptolib = None

_MODE_ECB = 1          # MicroPython cryptolib modes: 1 ECB, 2 CBC, 6 CTR
_MODE_CTR = 6
_native_mode = None    # "ctr" or "ecb" once the self-test has chosen
_SBOX = None
_native_ok = None      # None = untested, True/False after the self-test


def _tables():
    """AES S-box, built once from GF(2^8) arithmetic (saves 256 literals)."""
    global _SBOX
    if _SBOX is not None:
        return _SBOX
    exp = [0] * 255
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x ^= ((x << 1) ^ 0x11B) & 0xFF if x & 0x80 else x << 1
    sbox = [0] * 256
    for a in range(256):
        inv = exp[(255 - log[a]) % 255] if a else 0
        s = inv
        for k in range(1, 5):
            s ^= ((inv << k) | (inv >> (8 - k))) & 0xFF
        sbox[a] = s ^ 0x63
    _SBOX = bytes(sbox)
    return _SBOX


def _xtime(a):
    return ((a << 1) ^ 0x11B) & 0xFF if a & 0x80 else a << 1


class _AES:
    """AES block encryption only (CTR needs nothing else)."""

    def __init__(self, key):
        n = len(key)
        if n not in (16, 24, 32):
            raise ValueError("bad AES key length")
        sbox = _tables()
        nk = n // 4
        self.rounds = nk + 6
        words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
        rcon = 1
        for i in range(nk, 4 * (self.rounds + 1)):
            t = list(words[i - 1])
            if i % nk == 0:
                t = t[1:] + t[:1]
                t = [sbox[b] for b in t]
                t[0] ^= rcon
                rcon = _xtime(rcon)
            elif nk > 6 and i % nk == 4:
                t = [sbox[b] for b in t]
            words.append([a ^ b for a, b in zip(words[i - nk], t)])
        self.rk = []
        for r in range(self.rounds + 1):
            rk = []
            for w in words[4 * r:4 * r + 4]:
                rk += w
            self.rk.append(rk)

    def encrypt_block(self, block):
        sbox = _SBOX
        s = [b ^ k for b, k in zip(block, self.rk[0])]
        for r in range(1, self.rounds + 1):
            s = [sbox[b] for b in s]
            # ShiftRows: state is column-major, index = col * 4 + row
            s = [s[((c + row) % 4) * 4 + row] for c in range(4) for row in range(4)]
            if r != self.rounds:
                out = []
                for c in range(4):
                    a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                    out.append(_xtime(a0) ^ _xtime(a1) ^ a1 ^ a2 ^ a3)
                    out.append(a0 ^ _xtime(a1) ^ _xtime(a2) ^ a2 ^ a3)
                    out.append(a0 ^ a1 ^ _xtime(a2) ^ _xtime(a3) ^ a3)
                    out.append(_xtime(a0) ^ a0 ^ a1 ^ a2 ^ _xtime(a3))
                s = out
            s = [b ^ k for b, k in zip(s, self.rk[r])]
        return bytes(s)


_INV = None


def _inv_sbox():
    global _INV
    if _INV is None:
        inv = [0] * 256
        for i, v in enumerate(_tables()):
            inv[v] = i
        _INV = bytes(inv)
    return _INV


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _decrypt_block(aes, block):
    """AES block decryption (straight inverse cipher; used by ECB only)."""
    inv = _inv_sbox()
    s = [b ^ k for b, k in zip(block, aes.rk[aes.rounds])]
    for r in range(aes.rounds - 1, -1, -1):
        # InvShiftRows then InvSubBytes
        s = [s[((c - row) % 4) * 4 + row] for c in range(4) for row in range(4)]
        s = [inv[b] for b in s]
        s = [b ^ k for b, k in zip(s, aes.rk[r])]
        if r:
            out = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                out.append(_mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9))
                out.append(_mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13))
                out.append(_mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11))
                out.append(_mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14))
            s = out
    return bytes(s)


def ctr_pure(key, nonce, data):
    """AES-CTR with a 128-bit big-endian counter, pure Python."""
    aes = _AES(key)
    counter = int.from_bytes(nonce, "big")
    out = bytearray(len(data))
    for off in range(0, len(data), 16):
        ks = aes.encrypt_block(counter.to_bytes(16, "big"))
        chunk = data[off:off + 16]
        for i in range(len(chunk)):
            out[off + i] = chunk[i] ^ ks[i]
        counter = (counter + 1) & ((1 << 128) - 1)
    return bytes(out)


def _ctr_native(key, nonce, data):
    """CTR from cryptolib: native CTR mode when this build has it, otherwise
    native ECB over the counter blocks (the usual MicroPython build lacks CTR)."""
    if _native_mode == "ctr":
        return cryptolib.aes(key, _MODE_CTR, nonce).encrypt(data)
    nblocks = (len(data) + 15) // 16
    counter = int.from_bytes(nonce, "big")
    blocks = b"".join(((counter + i) & ((1 << 128) - 1)).to_bytes(16, "big")
                      for i in range(nblocks))
    ks = cryptolib.aes(key, _MODE_ECB).encrypt(blocks)[:len(data)]
    n = len(data)
    if n == 0:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(n, "big")


def _selftest():
    """True when a cryptolib path agrees with the pure implementation."""
    global _native_mode
    if cryptolib is None:
        return False
    sample = bytes(range(70))
    for mode in ("ctr", "ecb"):
        _native_mode = mode
        try:
            for key in (bytes(range(16)), bytes(range(32))):
                nonce = bytes(range(100, 108)) + b"\x00" * 4 + b"\xff\xff\xff\xfe"
                if _ctr_native(key, nonce, sample) != ctr_pure(key, nonce, sample):
                    raise ValueError("mismatch")
            return True
        except Exception:
            continue
    _native_mode = None
    return False


def ctr(key, nonce, data):
    """Encrypt or decrypt (same operation) data with AES-CTR."""
    global _native_ok
    if _native_ok is None:
        _native_ok = _selftest()
    if _native_ok:
        return _ctr_native(key, nonce, data)
    return ctr_pure(key, nonce, data)


def backend():
    """Which implementation is in use: "cryptolib-ctr", "cryptolib-ecb" or "pure"."""
    global _native_ok
    if _native_ok is None:
        _native_ok = _selftest()
    return ("cryptolib-" + _native_mode) if _native_ok else "pure"


# --- AES-ECB (MeshCore) ------------------------------------------------------
# MeshCore encrypts with plain AES-128-ECB over zero-padded 16-byte blocks. Same
# policy as CTR above: use cryptolib only after it agrees with the pure code.

_ecb_ok = None


def _ecb_pure(key, data, decrypt):
    aes = _AES(key)
    out = []
    for off in range(0, len(data), 16):
        blk = data[off:off + 16]
        out.append(_decrypt_block(aes, blk) if decrypt else aes.encrypt_block(blk))
    return b"".join(out)


def _ecb_selftest():
    if cryptolib is None:
        return False
    sample = bytes(range(48))
    try:
        key = bytes(range(16))
        enc = cryptolib.aes(key, _MODE_ECB).encrypt(sample)
        return (enc == _ecb_pure(key, sample, False)
                and cryptolib.aes(key, _MODE_ECB).decrypt(enc) == sample)
    except Exception:
        return False


def ecb(key, data, decrypt=False):
    """AES-ECB of whole 16-byte blocks (len(data) must be a multiple of 16)."""
    global _ecb_ok
    if len(data) % 16:
        raise ValueError("ECB needs whole blocks")
    if _ecb_ok is None:
        _ecb_ok = _ecb_selftest()
    if _ecb_ok:
        c = cryptolib.aes(key, _MODE_ECB)
        return c.decrypt(data) if decrypt else c.encrypt(data)
    return _ecb_pure(key, data, decrypt)
