# TODO

## RF tools port (modwifi + sniff/blescan/wardrive)

Passive WiFi/BLE/GPS tooling ported from the brute32 recon side (C module +
frozen apps; no frame-injection/credential-capture surface). Flash a
`dev`-branch build (~v1.28.0, 2026-11-08 or later) before testing.

### esp32-iq-tx (raw IQ TX, https://github.com/whstudio123/esp32-iq-tx)

- [ ] **Most important for vtOS development .**
      This section records the decision, not an implementation plan for it
      in this repository. Known constraints to be aware of:
      - `esp32-iq-tx` is pinned to ESP-IDF 5.3.5 with an exact PHY-library
        SHA-256 hash (see its README); vtOS builds on 5.5.1, so all of its
        PHY register paths need re-verification per toolchain the same way
        brute32's `libnet80211.a` pinning works.
      - While active, playback owns the RF exclusively -- WiFi/BLE are
        unloaded, so it can only exist as an app-level lab mode.
      - It is channel-granular inside the 2.4GHz WiFi band, not 'any
        frequency' (the SX1262 LoRa radio is the only genuinely tunable-
        frequency radio on the T-Deck).
- [x] On-device validation 2026-11-08 (T-Deck Plus, IDF 5.5.1, new libphy
      af64b3d0...): `sample_rates` example flashed, `tone 40`/`tone 80`
      both ESP_OK; HackRF One saw a clean single-bin tone on air
      (+487 kHz / +1.11 MHz offsets, ~78-89% of nominal -- physical DAC
      clock runs below the register's nominal label, matching the
      README's rate-matrix caveat), spike vanished on `stop`. Port
      open question: TX power is not stable across stop/start cycles
      -- same-gain captures showed tone 80 strong after boot and
      invisible minutes later, while tone 40 was strong again after
      further cycles (per-rate PA state, not monotonic degradation).
      The S3 backend lacks the TX power-control-field capture/restore
      that the S31 backend has (0x20100408 mask 0x00fe0000 on S31).
      mod_iqtx needs a controlled power test at fixed RX gain, then
      either field restore or full re-init per session.
- [x] vtOS port 2026-11-08: `modules/mod_iqtx/` C module (S3 backend,
      GPL-3.0-or-later from whstudio123/esp32-iq-tx, provenance in the
      file header; rest of vtOS stays MIT) + `bin/iqtx.py` TUI lab app
      (`tone 40|80`, `stop`, `reinit`, `info`). Board sdkconfig gains
      CONFIG_ESP_PHY_ENABLE_CERT_TEST=y (links librftest.a). Registered
      as shell `iqtx` + Alt+D launcher entry.
      On-device verified: import, caps, exclusive-RF init (module
      performs the esp_wifi stop/deinit takeover itself; deinit
      re-creates the stopped driver so wlan.active(True) restores
      cleanly), load gate, deinit.
      PLAYBACK IS GATED OFF (iqtx.BANK_PROTECTED=0): the upstream
      SOC_RESERVE_MEMORY_REGION(0x3fcd0000..0x3fce0000) link-time
      reservation KILLS BOOT on this board -- bisected 2026-11-08
      (no-module + same sdkconfig boots; module-without-reservation
      boots; reservation = dark boot, silent ROM fallback). The RF bank
      currently sits inside the MicroPython heap, so load/start raise
      until fixed. Next port step: carve the bank out with a linker
      fragment on the dram segments (neither static data nor
      heap_caps may see it), flip IQTX_BANK_PROTECTED to 1, retest
      boot, then HackRF-verify tone through the module like the
      example run.
      `reinit` remains the workaround for the stop/start power drift.
- [x] Unrelated decision unchanged: spectrum.py (2.4GHz band profiler, RX
      only) + loramon.py (tunable LoRa RX/monitor + conforming TX) --
      implemented 2026-11-08 (bin/spectrum.py, bin/loramon.py; shell
      `spectrum` / `loramon`).

### Sweep progress

- [x] `modwifi` C module compiles clean on IDF 5.5.1; device-validated:
      mgmt capture, ring drain, channel lock, hop, clean stop
- [x] `lib/pcap.py`: writer byte-validated (`pcap` global header, v2.4,
      LINKTYPE_IEEE802_11=105); scapy parses a live 1461-frame capture
- [x] `bin/sniff.py`: end-to-end on device (BLE dropped, stats, pcap saved)
- [x] `bin/blescan.py`: 28 devices fingerprinted (company IDs, names)
- [x] `bin/wardrive.py`: vanilla T-Deck exits with the designed
      "GPS requires the T-Deck Plus" error; scan->WiGLE CSV->KML
      validated with synthetic fix (34 APs)
- [ ] Wardrive on real T-Deck Plus GPS (needs a Plus unit; GPS lives on
      GPIO 43/44 -- known UART REPL conflict, addressed by the board def)
- [ ] `blescan` from the shell requires the rebuilt firmware: builds
      tagged 2026-11-08 freeze a pre-fix `bin.blescan` whose IRQ handler
      unpacks a 6-tuple; passive scans on some builds deliver a 5-tuple
      and the exception resets the chip. Rebuild & reflash, don't run
      the shell command on older builds (the `/flash/blescan.py` copy
      is the fixed one).

## After flashing firmware built from `main`/`dev` on the T-Deck Plus

The T-Deck Plus currently runs release v0.1.14 plus on-device overrides in the
flash root (installed over USB with mpremote). Firmware built from this branch
contains all of those changes natively, so the overrides must not come back.

### Before flashing

- [ ] Back up anything you want to keep from `/flash` (`make flash` runs
      `erase_flash`, which wipes the whole filesystem, not just the app):
      `.shellrc`, `.favs.json`, `.ssh/config`, `menu/`, and any books/notes.
      e.g. `mpremote connect <port> cp -r : ./flash-backup/`
- [ ] Build from this branch with the updated board definition
      (`boards/LILYGO_T_DECK/mpconfigboard.h` disables the UART0 console that
      conflicts with the Plus's GPS pins): `make build`
- [ ] Flash: `make flash` (device in bootloader mode, correct `PORT`)

### After flashing

- [ ] Do NOT restore the old `main.py`. Its patches (UART mask, `ssh_terminal`,
      `shell_ext`, `statusbar_ext`, `keyrepeat` install) are now built in and
      would override the new firmware. Start with no `main.py`, or only your
      own startup code.
- [ ] Do NOT restore these stale override files (the firmware has the real
      versions): `keyrepeat.py`, `trackball.py`, `ssh_terminal.py`,
      `shell_ext.py`, `launcher.py`, `statusbar_ext.py`, `main.py.before-*`,
      `ssh_terminal.before-trackball.py`.
- [ ] Restore your backed-up user data: `.shellrc`, `.favs.json`,
      `.ssh/config` (SSH profiles), `menu/`.
- [ ] Connect Wi-Fi again if it isn't in `.shellrc`: `nm connect "SSID" "pass"`.
- [ ] Verify on the device:
  - [ ] Typing works with no workaround (UART fix is in the firmware).
  - [ ] Hold Backspace: repeats after ~400 ms, stops on release.
  - [ ] Alt+D opens the launcher; Esc / Alt+D / long-press closes it.
  - [ ] `ssh` with a `.ssh/config` shows the profile picker.
  - [ ] SSH: Enter submits in Claude/Codex; trackball up/down scrolls output.
  - [ ] Top bar shows the dark, colored style (battery/Wi-Fi colors change).
- [ ] If anything above misbehaves, check `git status` on the firmware source:
      the build only includes what is committed or present in the working tree
      when `make build` ran.
