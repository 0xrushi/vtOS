# What the T-Deck firmware supports, and what's missing

Status as of 2026-11-08. **Verified** = run on a device (or measured, for RF).
**Built/tested off-device** = code exists and host tests pass, but no device
run. Nothing here claims more than that.

## Supported

| Area | Tool | Status |
|---|---|---|
| Wi-Fi inspector | `sniff` (management frames, pcap), `spectrum` (2.4 GHz band profile), `wardrive` (scan, WiGLE CSV, KML; needs the Plus's GPS) | sniff verified; wardrive verified with a synthetic fix, **not yet run against the Plus's real GPS** (the GPS shares the UART0 pins that the board config now frees) |
| BLE explorer | `blescan` (company IDs, names), `blechat` | blescan verified (28 devices) |
| Sub-GHz (LoRa chip, SX1262) | `loramon` (rx/tx monitor), `lorachat`, `loracfg` | implemented; loramon not re-verified this session |
| Meshtastic | `meshchat`: Meshtastic-compatible node and chat UI, settings TUI ([details](docs/MESHTASTIC.md)) | wire format, crypto, routing and UI tested off-device (protobufs checked against the official `meshtastic` classes, 230 host tests); radio driver, TX and retuning verified on the T-Deck; **no real Meshtastic packet received yet** (nothing heard in a 60 s listen; unit's LoRa band unconfirmed) |
| RF lab | iqlab: `tone <40\|80> [offset_MHz]`, keyboard input ([explained here](docs/IQTONE.md)) | verified on a HackRF (tones at set offsets); the vtOS `iqtx` app is gated off (RAM bank) |
| Terminal | vtOS shell, SSH client with profiles, SFTP/FTP, telnet, IRC, file manager, Alt+D launcher, `neofetch` | existing apps; launcher/SSH profiles verified earlier |
| Phone clipboard share | `clip` + `tools/clipshare-android` | host tests + curl against the server; shared from a Pixel 8 to the T-Deck and pasted on hardware. Alt+V repeat bug found there fixed and re-verified. Pairing is Wi-Fi LAN broadcast only; off the LAN, enter the address by hand |
| Remote access | `sshd`, `telnetd`, `ftpd`, `sftpd`, `vncd`, `webvncd` | existing, not re-tested |
| VNC client | `vnc <host>[:display] [password]`: remote desktop on the display, trackball as mouse, keyboard as keys ([details](docs/VNC.md)) | 2026-11-09: host tests against a fake server and display, plus the same code under MicroPython 1.28 (Unix port) against a real TigerVNC server (password auth, RGB565 colours, incremental updates, pointer, keys, scrolling view). Flashed to the T-Deck and run over Wi-Fi against the Docker test server (`tools/vnctest`): connect 0.2 s, auth 0.16 s, first full 320x240 frame 274 ms, typed text and a full-screen redraw arrived; panel colours confirmed correct by eye; **trackball, Mic-key modes and the physical keyboard not yet checked**. VNC-password/no-auth only (no TLS or user-name); Raw encoding only; over Tailscale (numeric IP) uses the new `modtailscale.wait_peer`: device joined the tailnet, but the **first `vnc` attempt over Tailscale failed to connect; cause not yet found** |
| Tailscale | `tailscale`: manual MicroLink VPN; SSH/`yt` to numeric tailnet IPv4 | device registered and exchanged SSH data over a direct tunnel; synthetic playback measured 7.3 fps; real-video stability, relay playback and long-run measurements still need device checks; [setup](docs/TAILSCALE.md) |
| USB | `usbmsc` (SD card as a USB drive) | existing |
| AI | `agent`: OpenAI-compatible chat with list/read/grep/write file tools | host tests + on-device run with a fake server; **not yet tried with a real model**; voice not built |
| Video | `yt`: YouTube rendered on an SSH server, JPEG + MP3 over SSH (`tools/ytstream`) | server and parser tested on a Mac; **not run on the device**; real YouTube downloads blocked on the test Mac (403) |
| Boot | dual-boot loader with menu | verified |

## Not built yet (the hardware could do it)

| Wanted | Notes |
|---|---|
| Wi-Fi deauthentication detection | management frames are already captured by `sniff`; needs a detector |
| I2C scanner, SPI/UART bridge tool | MicroPython can do it by hand; no app yet |
| USB keyboard/mouse/gamepad, macros | the S3's USB can act as HID; only mass storage exists. The one USB-C port is shared with the serial console |
| Agent voice (AssemblyAI) | planned: record with the mic, send to AssemblyAI's cloud API |
| Faster `yt` (30 fps) | 8 fps today; a C JPEG decoder module on the second core might reach it, unmeasured |
| vtOS `iqtx` transmitting | blocked by a RAM-reservation boot bug, not by hardware; use iqlab |

## Not possible on this hardware (missing part or hard limit)

Strictly ruled out by the board or the chip, not by missing code. These are for
the T-Deck Plus in use here (GPS present); a plain T-Deck has no GPS receiver,
so GPS wardriving is also impossible there. Items marked
(unchecked) are from what I know of the T-Deck and the ESP32-S3; I have not
verified them against the schematic.

| Wanted | Why it can't work |
|---|---|
| IR remote (learn or send) | no IR LED or IR receiver on the board (unchecked) |
| NFC / RFID | no NFC reader chip or antenna on the board (unchecked); would need an external module such as a PN532 |
| Sub-GHz capture and replay of garage/car-key style remotes | the radio is a LoRa chip (SX1262); it does LoRa and FSK only, not the OOK/ASK that most simple remotes use. A CC1101-style module would be needed |
| Any frequency outside the unit's one LoRa band | the SX1262's antenna matching is built for one band per T-Deck variant (433/868/915 MHz class); I have not checked which band your unit is |
| Transmitting at an arbitrary RF frequency, e.g. 2000 MHz | the S3's only transmitter is the 2.4 GHz Wi-Fi radio: channels 1-13 plus about +/-20 MHz from the IQ offset trick |
| 5 GHz or 6 GHz Wi-Fi scan or sniff | the ESP32-S3 radio is 2.4 GHz only |
| Bluetooth Classic (A2DP audio, classic device scan) | the ESP32-S3 has BLE only, no BR/EDR |
| Running a speech or language model on the device | 8 MB PSRAM and a 240 MHz dual core are far too small; voice and chat have to go to a cloud API |
