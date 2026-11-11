#
# Encrypted settings backup. See docs/BACKUP.md.
#
# One file holds every secret and setting listed in docs/SECRETS.md: the
# /flash dotfiles plus the Wi-Fi and Tailscale credentials kept in NVS. The
# file is AES-256-CTR encrypted with a key derived from a passphrase and
# authenticated with HMAC-SHA256, so a wrong passphrase or a damaged file is
# rejected before anything on the device is touched.
#
#   magic "VTBK1" | salt(16) | nonce(16) | mac(32) | ciphertext
#
# The ciphertext is JSON: {"v":1, "files":{name: base64}, "wifi":[ssid,pw],
# "tailscale":[auth_key, hostname]}.
#

import binascii
import hashlib
import json
import os

import mtcrypto

MAGIC = b"VTBK1"
ROOT = "/flash"
SD_DIR = "/sd/backups"
DEFAULT_NAME = "vtos-backup.vtbk"
ITERATIONS = 2000
MAX_FILE = 262144  # skip anything bigger (a runaway log, not a setting)

# Files under ROOT that make up the settings. A restore refuses any name that
# is not on this list (or in MENU_DIR), so a crafted backup cannot write
# elsewhere.
FILES = (
    ".agent", ".clip.json", ".http.json", ".mesh.json", ".mesh.json.nodes",
    ".meshcontacts.json", ".meshchat.json", ".meshcore.json", ".radio.json", ".trackball", ".favs.json", ".shellrc",
    ".vnc", ".ssh/config", ".sshd_host_key", ".yt", ".compose.mail",
    ".view.mail", "mesh-channels.txt", ".virc",
)
MENU_DIR = "menu"


class BackupError(Exception):
    pass


def _b64(data):
    return binascii.b2a_base64(data).decode().strip()


def _unb64(text):
    return binascii.a2b_base64(text)


def allowed(name):
    """True if a restored name is one this module is allowed to write."""
    if not isinstance(name, str) or name.startswith("/") or ".." in name.split("/"):
        return False
    if name in FILES:
        return True
    parts = name.split("/")
    return (len(parts) == 2 and parts[0] == MENU_DIR
            and parts[1].startswith(".") and parts[1].endswith(".json"))


def _hmac(key, data):
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + b"\x00" * (64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key) + data).digest()
    return hashlib.sha256(bytes(b ^ 0x5C for b in key) + inner).digest()


def _keys(passphrase, salt):
    dk = salt + passphrase.encode()
    for _ in range(ITERATIONS):
        dk = hashlib.sha256(dk).digest()
    return (hashlib.sha256(dk + b"enc").digest(),
            hashlib.sha256(dk + b"mac").digest())


def _same(a, b):
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a, b):
        diff |= x ^ y
    return diff == 0


def seal(payload, passphrase, salt=None, nonce=None):
    """Encrypt a payload dict; returns the file contents."""
    if not passphrase:
        raise BackupError("passphrase required")
    salt = salt or os.urandom(16)
    nonce = nonce or os.urandom(16)
    enc, mac = _keys(passphrase, salt)
    ct = mtcrypto.ctr(enc, nonce, json.dumps(payload).encode())
    head = MAGIC + salt + nonce
    return head + _hmac(mac, head + ct) + ct


def unseal(blob, passphrase):
    """Verify and decrypt a backup file; returns the payload dict."""
    n = len(MAGIC)
    if len(blob) < n + 64 or blob[:n] != MAGIC:
        raise BackupError("not a vtOS backup")
    salt = blob[n:n + 16]
    nonce = blob[n + 16:n + 32]
    tag = blob[n + 32:n + 64]
    ct = blob[n + 64:]
    enc, mac = _keys(passphrase or "", salt)
    if not _same(tag, _hmac(mac, blob[:n + 32] + ct)):
        raise BackupError("wrong passphrase or damaged file")
    try:
        payload = json.loads(mtcrypto.ctr(enc, nonce, ct).decode())
    except (ValueError, UnicodeError):
        raise BackupError("damaged file")
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise BackupError("unsupported backup version")
    return payload


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _names(root):
    names = [n for n in FILES if _exists(root + "/" + n)]
    try:
        for entry in os.listdir(root + "/" + MENU_DIR):
            name = MENU_DIR + "/" + entry
            if allowed(name):
                names.append(name)
    except OSError:
        pass
    return names


def collect(root=ROOT, wifi=None, tailscale=None):
    """Build the payload dict from the device's current settings."""
    files = {}
    for name in _names(root):
        path = root + "/" + name
        try:
            if os.stat(path)[6] > MAX_FILE:
                continue
            files[name] = _b64(_read(path))
        except OSError:
            continue
    payload = {"v": 1, "files": files}
    if wifi is not None:
        try:
            pair = wifi.load_station()
        except (OSError, ValueError):
            pair = None
        if pair:
            payload["wifi"] = [_b64(pair[0]), _b64(pair[1])]
    getter = getattr(tailscale, "get_config", None)
    if getter is not None:
        try:
            pair = getter()
        except OSError:
            pair = None
        if pair and pair[0]:
            payload["tailscale"] = [pair[0], pair[1]]
    return payload


def _mkdirs(path):
    cur = ""
    for seg in [p for p in path.split("/") if p][:-1]:
        cur += "/" + seg
        if not _exists(cur):
            try:
                os.mkdir(cur)
            except OSError:
                pass


def _write_atomic(path, data):
    _mkdirs(path)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.rename(tmp, path)


def apply(payload, root=ROOT, wifi=None, tailscale=None):
    """Write a payload back to the device. Returns a list of restored names."""
    files = payload.get("files", {})
    if not isinstance(files, dict):
        raise BackupError("damaged file")
    # Validate everything before writing anything.
    decoded = {}
    for name, text in files.items():
        if not allowed(name):
            raise BackupError("refusing unexpected file: %s" % name)
        try:
            decoded[name] = _unb64(text)
        except ValueError:
            raise BackupError("damaged file")
    restored = []
    for name, data in decoded.items():
        _write_atomic(root + "/" + name, data)
        restored.append(name)
    pair = payload.get("wifi")
    if pair and wifi is not None:
        wifi.save_station(_unb64(pair[0]), _unb64(pair[1]))
        restored.append("wifi")
    pair = payload.get("tailscale")
    if pair and tailscale is not None and hasattr(tailscale, "configure"):
        tailscale.configure(pair[0], pair[1])
        restored.append("tailscale")
    return restored


def _modules():
    try:
        import modwifi
    except ImportError:
        modwifi = None
    try:
        import modtailscale
    except ImportError:
        modtailscale = None
    return modwifi, modtailscale


def export_bytes(passphrase, root=ROOT):
    wifi, ts = _modules()
    return seal(collect(root, wifi, ts), passphrase)


def export_file(path, passphrase, root=ROOT):
    blob = export_bytes(passphrase, root)
    _write_atomic(path, blob)
    return len(blob)


def import_bytes(blob, passphrase, root=ROOT):
    payload = unseal(blob, passphrase)
    wifi, ts = _modules()
    return apply(payload, root, wifi, ts)


def import_file(path, passphrase, root=ROOT):
    return import_bytes(_read(path), passphrase, root)


def list_backups(directory=SD_DIR):
    try:
        return sorted(n for n in os.listdir(directory) if n.endswith(".vtbk"))
    except OSError:
        return []
