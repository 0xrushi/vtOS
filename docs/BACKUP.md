# Settings backup and restore

`settings export` writes **one encrypted file** holding everything that
[SECRETS.md](SECRETS.md) lists: the `/flash` dotfiles (`.agent`, `.mesh.json`,
`.vnc`, `.ssh/config`, `.http.json`, ...), the saved Wi-Fi network and the
Tailscale auth key and hostname (both live in NVS, which a `make flash` wipes).

```
settings                  menu (also: Settings button in the touch Quick Settings panel)
settings export [file]    default /sd/backups/vtos-backup.vtbk
settings import <file>    no file = pick from /sd/backups
```

You are asked for a passphrase (twice on export). Nothing is written back
unless the passphrase and file check out. After an import, reboot.

The web server can also hand the file to your Mac: open `httpd`'s page and
press *Download backup*, or
`curl -H "Authorization: Bearer $TOKEN" -H "X-Passphrase: ..." http://<ip>:8080/backup -o b.vtbk`.

## Format

`VTBK1 | salt(16) | nonce(16) | HMAC-SHA256(32) | AES-256-CTR(JSON)`.
Keys come from the passphrase by 2000 rounds of SHA-256 with the salt, then
split into an encryption and a MAC key. The MAC is checked first, so a wrong
passphrase or a damaged file is rejected before anything is touched.
(`lib/backup.py`; the AES comes from `lib/mtcrypto.py`.)

## What is not saved

- MicroLink peer cache and node identity (regenerated on `tailscale up`).
- Brightness, volume, font choice and the Wi-Fi/BT/GPS/LoRa tile states are not
  persisted anywhere, so there is nothing to back up.
- `/flash/agent/` (the agent's work folder) and files over 256 KB.
- A restore only writes the names on the whitelist in `backup.FILES` (plus
  `menu/.*.json`); a crafted file cannot write anywhere else.

## Before reflashing

`make flash` erases NVS and `/flash`. Export first, flash, then import. The
backup is on the SD card, which every flash leaves alone.

## Status

Host tests only (`utils/tests/test_backup.py`). Backing up the Tailscale key
needs the firmware that adds `modtailscale.get_config()`; on older firmware the
Tailscale entry is simply skipped. **Not yet run on the T-Deck.**
