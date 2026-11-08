# What the T-Deck firmware supports, and what's missing

Status as of 2026-11-08. **Verified** = run on a device (or measured, for RF).
**Built/tested off-device** = code exists and host tests pass, but no device
run. Nothing here claims more than that.

## Supported

| Area | Tool | Status |
|---|---|---|
| Wi-Fi inspector | `sniff` (management frames, pcap), `spectrum` (2.4 GHz band profile), `wardrive` (scan, WiGLE CSV, KML; GPS only on the Plus) | sniff verified; wardrive verified with a synthetic fix, real GPS untested |
| BLE explorer | `blescan` (company IDs, names), `blechat` | blescan verified (28 devices) |
| Sub-GHz (LoRa chip, SX1262) | `loramon` (rx/tx monitor), `lorachat`, `loracfg` | implemented; loramon not re-verified this session |
| RF lab | iqlab: `tone <40\|80> [offset_MHz]`, keyboard input | verified on a HackRF (tones at set offsets); the vtOS `iqtx` app is gated off (RAM bank) |
| Terminal | vtOS shell, SSH client with profiles, SFTP/FTP, telnet, IRC, file manager, Alt+D launcher, `neofetch` | existing apps; launcher/SSH profiles verified earlier |
| Remote access | `sshd`, `telnetd`, `ftpd`, `sftpd`, `vncd`, `webvncd` | existing, not re-tested |
| USB | `usbmsc` (SD card as a USB drive) | existing |
| AI | `agent`: OpenAI-compatible chat with list/read/grep/write file tools | host tests + on-device run with a fake server; **not yet tried with a real model**; voice not built |
| Video | `yt`: YouTube rendered on an SSH server, JPEG + MP3 over SSH (`tools/ytstream`) | server and parser tested on a Mac; **not run on the device**; real YouTube downloads blocked on the test Mac (403) |
| Boot | dual-boot loader with menu | verified |

## Missing / not supported

| Wanted | Why |
|---|---|
| Wi-Fi deauthentication detection | not written |
| I2C scanner, SPI/UART bridge tool | MicroPython can do it by hand; no app |
| USB keyboard/mouse/gamepad, macros | not written (only mass storage) |
| IR remote | no IR LED/receiver on the T-Deck |
| Sub-GHz record/replay (CC1101 style) | the radio is a LoRa SX1262, not a general sub-GHz transceiver |
| NFC/RFID | no reader hardware, no code |
| 30 fps video | `yt` is 8 fps by default; a C decoder module would be needed |
| Agent voice (AssemblyAI) | planned, not built |
| vtOS `iqtx` transmitting | RAM bank reservation breaks vtOS boot; use iqlab |
| PocketJS / Pocket Shell | shelved on purpose, code kept in `utils/pocket/` |
