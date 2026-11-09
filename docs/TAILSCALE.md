# Tailscale on the T-Deck

vtOS uses [MicroLink](https://github.com/CamM2325/microlink) to join a Tailscale
tailnet directly from the ESP32-S3 over Wi-Fi. SSH and `yt` can then reach a
server's numeric Tailscale IPv4 address. The server still downloads YouTube
and converts it to JPEG + MP3 using `ytstream.py`.

## Setup

1. Build and install the updated native firmware. Copying Python scripts alone
   cannot add the WireGuard interface or native SSH routing.
2. Connect Wi-Fi once with `nm` or `nm connect <ssid> [password]`. vtOS saves
   the successful Wi-Fi profile and reconnects at boot.
3. Generate a Tailscale **auth key**, not an API key, in your
   [admin console](https://login.tailscale.com/admin/settings/keys). Use a
   non-ephemeral device identity. A reusable key permits re-registration after
   node-key expiry. Approve the device if your tailnet requires approval.
4. Run `tailscale up`. The first run prompts for the key without echoing it,
   then a hostname (default `vtos-tdeck`). Esc cancels entry. Joining waits up
   to 60 seconds; Ctrl-C or a long trackball press cancels and stops it.

Commands: `tailscale up`, `down`, `status`, `ip`, and `peers`. Status includes
free internal SRAM, its largest free block, and free PSRAM. Configuration and
device keys persist in NVS. **Startup is manual:** after power-on or a
MicroPython soft reset, run `tailscale up` again. `yt` never starts it implicitly.
An already-started session recovers when Wi-Fi reconnects. `tailscale down`
also disconnects SSH sockets using the VPN interface.

To replace an expired/revoked key or change the hostname:

```text
tailscale down
tailscale configure
tailscale up
```

The key stays outside shell history and firmware images. NVS uses the device's
existing storage protection; this integration does not encrypt it. Keep device
backups private. Reconfiguration preserves the existing machine identity.

## YouTube over SSH

Install `yt-dlp`, `ffmpeg`, and `ytstream.py` on a server running Tailscale and
ordinary SSH with password authentication. Tailscale SSH uses a different
authentication system; vtOS uses ordinary SSH over the Tailscale network.
Allow the T-Deck to reach the server's SSH TCP port in your tailnet grants/ACLs
and server firewall.

Add the server to `/flash/.ssh/config` (or `/sd/.ssh/config`):

```text
Host yt
    HostName 100.101.102.103
    User me
    Port 22
```

Replace the IP with your server's address. Omit `Password` to be prompted.

```text
tailscale up
ssh yt
yt big buck bunny
yt --full https://www.youtube.com/watch?v=VIDEO_ID
```

`ssh yt` is an optional connection test; disconnect with Ctrl-] before `yt`.
Tailscale profiles default to `--clear` (256×144, 8 fps, less JPEG compression)
to leave time for Wi-Fi, SSH, audio and drawing. Existing presets remain
available when chosen explicitly. The playback controls and protocol are
unchanged. The ESP32 triggers
the peer handshake and binds SSH to its VPN source address. Tailnet connections
have a 45-second total budget: up to 30 seconds for tunnel discovery, with time
reserved for TCP/SSH. LAN SSH retains 15 seconds. Direct UDP is preferred;
DERP relay is available when NAT/firewalls block a direct path. Actual FPS
depends on network and CPU load; `yt` prints measured FPS on exit.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Native support missing | Rebuild and install the full firmware, including `modtailscale`. |
| Connect Wi-Fi first | Run `nm`; the Wi-Fi network needs internet access. |
| Registration failed/timed out | Internet access, auth-key expiry and device approval. Replace the key with `tailscale configure` while down. |
| VPN disconnected | Check `tailscale status`, Wi-Fi, then run `tailscale up`. |
| Peer unavailable | Server IP, online status, device approval and tailnet access rules. |
| SSH timeout | Server SSH port, firewall, password login and direct/DERP connectivity. |
| Allocation failed | Check status memory counts; stop memory-heavy apps or reboot before joining. |
| Workers still stopping | Wait briefly and run `tailscale down` again; live resources are retained safely until network operations finish. |
| YouTube reports 403 | Follow the [server's yt-dlp setup instructions](../tools/ytstream/README.md). |

Initial support uses numeric IPv4 in `100.64.0.0/10`. MagicDNS integration,
exit nodes, subnet routing and cellular are outside this change. There are 16
active WireGuard slots and 16 cached NVS peers; both coordination buffers are
512 KB. Larger tailnets may need a restricted peer map or custom settings.

## Build and native API

Existing host, Docker and release builds fetch MicroLink and its nested
WireGuard component at commit `216da3300f0493b0860247d43f7af5ce29df63a5`.
The integration uses ESP-IDF 5.5.1, MicroPython 1.28.0, and the existing 4 MB
vtOS application partition. No auth keys are compiled into firmware.

After upgrading an existing build environment, run `make clean` before
`make build` so cached SDK settings pick up the new crypto, lwIP and stack
options. Dependency notices accompany release binaries as
`TAILSCALE_NOTICES.md`.

`modules/modtailscale/patch_microlink.py` applies idempotent source checks and
integration patches. These add worker shutdown acknowledgements, partial-start
cleanup, PSRAM stacks for network/coordination/DERP workers, and lwIP core
locking. The WireGuard manager keeps an internal stack for NVS writes.
Decrypted IP input is synchronous under the core lock, preventing queued input
from retaining a freed interface. Finalizers stop the VPN and join SSH workers
on soft reset. Cellular, HTTP configuration and zero-copy mode stay disabled.

The native API is `modtailscale.configure(auth_key, hostname)`, asynchronous
`up()`, `down()`, `status()` (dict), `ip()` (string or `None`), and `peers()`
(list of `(ip, hostname, direct_path)` tuples). Import does not start the VPN.

## Verification

```sh
python3 -m pytest utils/tests/test_tailscale.py utils/tests/test_ssh_profiles.py \
  utils/tests/test_ssh_terminal.py utils/tests/test_ssh_mic.py \
  utils/tests/test_ytproto.py utils/tests/test_ytjpeg.py
```

Host tests cover manual setup, hidden entry, cancellation, failures, native
socket preparation/binding, address boundaries, connection budgets, SSH,
streaming and JPEG decoding. Firmware compilation checks the partition size.

Tailnet acceptance needs credentials and a configured video server:

1. Join and verify approval, then connect `ssh yt`.
2. Play with audio; check pause, volume and quit.
3. Stream for ten minutes. Record measured FPS and `modtailscale.status()`
   memory counts before/after playback.
4. Repeat with direct UDP blocked to exercise DERP; record both results.
5. Reconnect Wi-Fi, repeat `up/down`, and soft-reset while connected. Verify
   manual startup and stable memory across cycles.

Host tests and compilation do not establish tailnet connectivity or sustained
playback. Report device results separately from requested FPS presets.

### Verification on 2026-11-08

- 118 targeted host tests passed, including the native transport harness and
  existing SSH, streaming, JPEG and launcher checks.
- ESP-IDF 5.5.1 / MicroPython 1.28.0 build succeeded. The latest application is
  2,838,880 bytes and fits the existing 4 MB slot with 1,355,424 bytes free.
- Installed only the vtOS app slot at `0x110000` after backing up that slot
  and boot metadata. The dual-boot loader, other apps and filesystem remain.
- Verified native module import, status/peers/ip/down commands, Wi-Fi-required
  error, and three stopped-VPN SSH attempts with GC cleanup. Free SRAM and
  PSRAM were identical before/after those three attempts (114,543 and
  8,196,224 bytes respectively).
- Soft reset returned to `down` without joining automatically. Updated the
  device's existing `yt` SSH profile to `100.96.103.69`, retaining user `bread`;
  the old config is `/flash/.ssh/config.before-tailscale`. The server's SSH
  banner was reachable from the development computer.
- The key was saved in device NVS; the T-Deck joined as `100.84.232.67`.
  It exchanged SSH data with the server over a direct WireGuard path.
  A synthetic stream drew 224 frames at 7.3 fps with no reported audio error.
- Real-video attempts at higher resolution and in Clear mode hit interrupt
  watchdog resets.
  The viewer now defaults to clearer compression at 256×144 and 8 fps over
  Tailscale. The installed native firmware suppresses WireGuard packet logs
  and caps receive batches under the network lock. The `--fluid` preset
  requests 160×90 at 15 fps. On the requested clip, the 192×108 trial averaged
  6.6 rendered fps. The 160×90 trial also averaged about 6.6 fps, with
  intervals between 4.2 and 10.2 fps. It still hit an interrupt watchdog
  reset after about 2½ minutes. DERP and ten-minute results remain open.
