# Where the T-Deck keeps secrets and settings, and what a flash resets

Keep this file current: when an app starts storing a key, password or setting
on the device, add a row here. No secret values belong in this file, only
where they live and how to put them back.

Status as of 2026-11-10. "Checked" means I read the code that writes it.
Nothing here was verified by dumping a real device.

## Where things live

Two stores on the device, plus the SD card.

- **NVS** (flash partition `nvs`, 0x9000, 16 KB): written by native modules.
  Not encrypted.
- **`/flash`** (FAT partition `vfs`, 0x920000): ordinary files, readable over
  USB (`mpremote cp`) and by anyone with the board. Not encrypted.
- **`/sd`** (the SD card, if present): survives any flash, goes with the card.

### Secrets

| Secret | Where | Set with | Notes |
|---|---|---|---|
| Wi-Fi network and password | NVS, namespace `vtos_wifi`, key `station` | `nm connect <ssid> [password]` (saved on success) | Reconnects at boot. Lost by `erase_flash` |
| Tailscale auth key and hostname | NVS, namespace `vtos_tailscale` (`auth_key`, `hostname`) | `tailscale configure` / first `tailscale up`; or `tools/tailscale-join-from-clipboard.py` | Device identity keys (MicroLink) also in NVS. Losing NVS makes the device register as a new node: delete the old one in the Tailscale admin console. Startup is manual: run `tailscale up` after every boot ([TAILSCALE.md](TAILSCALE.md)) |
| AssemblyAI key (voice typing, `agent` voice) | `/flash/.agent`, JSON key `assemblyai_key` | `agent set assemblyai_key <key>`, or `tools/assemblyai-key-from-clipboard.py` | Used by `vnc` (Mic key) |
| LLM API key, base URL, model for `agent` | `/flash/.agent` (`api_key`, `api_base`, `model`) | `agent set <name> <value>` | Same file as above |
| SSH server (`sshd`) host key | `/flash/.sshd_host_key` | generated on first `sshd` | Lost = new host key, clients warn about a changed key |
| SSH client profiles (host, user, **password**) | `/flash/.ssh/config`, `/sd/.ssh/config` | edit the file (OpenSSH-style, `Password` line) | Passwords in plain text |
| VNC profiles (alias, host, password) | `/flash/.vnc`, `/sd/.vnc` | edit the file ([VNC.md](VNC.md)) | Passwords in plain text |
| Phone-clipboard pairing token | `/flash/.clip.json` | `clip pair` / `clip token new` | Lost = pair the phone again |
| Meshtastic channel keys (PSKs), node name, region | `/flash/.mesh.json`, `/flash/mesh-channels.txt` | `meshchat` Settings | PSK = who can read your mesh channel |
| Mail drafts / viewed mail | `/flash/.compose.mail`, `/flash/.view.mail` | `smtp`, `pop3` | Message text; passwords for `pop3`/`smtp` are typed on the command line each time and not stored |

### Settings (not secret, but you will want them back)

| Setting | Where | Set with |
|---|---|---|
| Trackball sensitivity (`move`, `gain`) | `/flash/.trackball` | `ball move <n>`, `ball gain <n>` |
| LoRa frequency / power | `/flash/.radio.json` | `loracfg` |
| Meshchat state | `/flash/.meshchat.json` | `meshchat` |
| `yt` remote command (one line; may name your server) | `/flash/.yt` | edit the file |
| Favourites, shell start-up, welcome text | `/flash/.favs.json`, `/flash/.shellrc` (also `/sd/.shellrc`), `/flash/WELCOME.md` | by hand |
| `agent` work folder | `/flash/agent/` | the `agent` tools |

## What survives which flash

| Action | NVS (Wi-Fi, Tailscale) | `/flash` files | Loader and other slots |
|---|---|---|---|
| Write only the vtOS slot: `esptool write-flash 0x110000 micropython.bin` (what I normally do) | kept | kept | kept |
| Rewrite the loader pieces only (bootloader 0x0, table 0x8000, otadata 0xd000, loader 0x10000; "Case B" in the iq-tone skill) | kept | kept | kept |
| **`make flash`** (`erase_flash`, then `firmware.bin` at 0x0) | **wiped** | **wiped** | **wiped** (dual boot is gone too) |
| Fresh dual-boot install from the single-image layout ("Case A", `utils/dualboot/README.md`) | wiped | reformatted | rewritten |
| Any change to the `vfs` offset or size in the partition table | kept | **lost** | n/a |
| Soft reset / `machine.reset()` / power cycle | kept | kept | kept |
| `tailscale down` | kept | kept | n/a (the VPN just stops) |

So a normal firmware update keeps every secret. Only a full erase or a layout
change loses them, and then the order to restore is:

1. `nm connect <ssid> <password>` (Wi-Fi).
2. `tailscale up` (it asks for the auth key if NVS was wiped), or
   `tools/tailscale-join-from-clipboard.py`; remove the old device from the
   Tailscale admin console.
3. `tools/assemblyai-key-from-clipboard.py` and `agent set api_key ...`.
4. Re-create `.ssh/config`, `.vnc`, then `ball`, `loracfg`, `clip pair`.

## Backups and the Mac side

- A full flash dump (`esptool read-flash`, Step 0 in the iq-tone skill) contains
  all of the above in plain form: Wi-Fi password, Tailscale key, API keys.
  `flash-backup/` is git-ignored; keep it private and delete old dumps.
- `mpremote cp -r :/flash/. flash-backup/...` copies the files the same way.
- The Mac-side helper scripts read a key from the clipboard, send it over USB
  and clear the clipboard; they never print it or write it to disk.
- Keys pasted into a chat or typed on a command line end up in that
  history or in the shell history. Rotate any key that was shown that way.
- Test-server credentials in the repo (`tools/vnctest`, VNC password `secret`)
  are throwaway test values, not secrets.

## Not stored on the device

`pop3`/`smtp` passwords, SSH passwords typed at a prompt, and the VNC password
typed at `VNC password:` are only kept if you put them in a profile file above.
