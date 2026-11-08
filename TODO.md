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

## Pocket Shell as a third dual-boot slot -- SHELVED (2026-11-08)

**Decision 2026-11-08: do not work on any PocketJS / Pocket Shell item below
until the user asks again.** State when shelved: the launcher, Notes and the
Wi-Fi/HTTP worker firmware all build and were flashed (image 2,854,992 B in the
3 MB `pocket` slot, 9% free); Wi-Fi and Hacker News were never tested on the
device (the SD card still held older packages). Everything is kept in
`utils/pocket/` as-is. The `pocket` slot at 0x610000 is being reused for the
agent (see "Minimal agent UI in C").


Goal: a third payload next to vtOS and iqlab: pocket-nexus/pocket-shell, a
touch launcher for PocketJS applets (QuickJS guest + Rust core, GPL-3.0
launcher, MIT runtime). Research 2026-11-08 (clones were read, nothing built
until the spike below):

- PocketJS lists ESP32-S3 as supported (software RGB565 renderer, SPI display
  via `pocket_display_esp_lcd`, touch via `pocket_input_touch`, buttons via
  `pocket_input_gpio`). Only the Tab5 (ESP32-P4) has a board program
  (`pocket-m5`); no T-Deck program exists.
- S3 needs the Xtensa Rust fork (pinned `1.97.0-nightly`, commit 8ea53bc in
  `tools/build/toolchains/esp-idf/compiler-pins.json`, built with
  `-Zbuild-std=core,alloc`) and ESP-IDF 6.0/6.1/6.2 (vtOS/loader use 5.5.1;
  payloads are separate images so mixed IDF versions are fine).
- Applets are installed from a paired Mac app (Pocket Hub, unreleased, or
  Pocket Companion). Without it the launcher lists only embedded apps.

Partition plan (keeps `/flash` at 0x920000 untouched): shrink iqlab from 4 MB
to 1 MB (image is about 0.7 MB) and use the freed space at 0x610000 for
`pocket` as ota_2, 3 MB. Loader menu gets a third entry.

- [x] Phase 1 (2026-11-08): 3-slot table (`utils/dualboot/partitions-dual.csv`, loader copy,
      `boards/LILYGO_T_DECK/partitions.csv` unaffected) and loader menu with
      BOOT cycling vtOS -> iq lab -> pocket shell, serial keys 1/2/3.
- [~] Phase 1b: partition table + 3-slot loader flashed and hashes verified 2026-11-08 (BOOT cycling skips empty slots; empty slot falls back to vtOS). Still to look at on the screen: vtOS and `/flash` must keep
      working (verify), iqlab still boots from its new 1 MB slot.
- [x] Phase 2a (2026-11-08): Xtensa toolchain via espup 0.17.1 (aarch64 Linux
      Docker, `utils/pocket/xtensa-runtime-spike.sh`) builds the PocketJS runtime
      archive for esp32s3: `libpocket_idf_runtime.a` = 3,761,500 B (static
      archive, not the linked size). Compiler reports 1.97.0-nightly commit
      c980f48, but `compiler-pins.json` pins 8ea53bc, so their receipt check
      (`verifyNativeCompiler`) would reject it: the espup branch has moved.
- [x] Phase 2b (2026-11-08): PocketJS `tests/esp-idf/examples/prebuilt` linked for
      esp32s3 on ESP-IDF 6.0.1 (Docker `espressif/idf:v6.0.1`, Xtensa Rust from
      phase 2a, Bun 1.4.2 for the pocket CLI; `utils/pocket/build-s3-example.sh`).
      Image = 1,552,240 B (0x17af70) with a 246 KB package: fits the 3 MB slot
      with about 1.4 MB spare. Flash code 1.08 MB, rodata 0.41 MB; static RAM
      only about 50 KB DIRAM (the runtime heap goes to PSRAM, example sets
      CONFIG_SPIRAM_USE_MALLOC). One example fix was needed: the example
      main.c missed the new `flags` field (-Werror). Not yet flashed or run;
      still lacks display, input, Wi-Fi and the real launcher package, so
      expect the final image to be larger than this.
- [x] Phase 3 (2026-11-08, blue card confirmed on screen): T-Deck program `utils/pocket/tdeck/` (build with
      `build.sh`, needs POCKETJS_SRC + RUST_HOME). ESP-IDF 6.0.1, 1,620,720 B
      (0x18baf0, 48% of the slot free). GPIO10 power, ST7789 via esp_lcd
      (`esp_lcd_new_panel_st7789`, SPI2 40 MHz, byte-swapped RGB565), keyboard
      I2C 0x55 + BOOT mapped to buttons (w/a/s/d or i/j/k/l = d-pad, Enter =
      cross, Backspace = circle, Space = start, BOOT = select), PocketJS smoke
      applet embedded. Flashed at 0x610000 and booted from the loader (serial
      key `3`): runs about 60 fps, 0 skipped/errored frames over 30 s.
      Fixes needed on the way: `main/idf_component.yml` override_path for
      pocket-nexus/pocket_runtime; the guest runs on the calling task, so the
      app now runs on a 384 KB PSRAM task stack (32 KB main stack overflowed).
      Input is wired but untested: the smoke applet ignores buttons.
- [x] Phase 4 (2026-11-08): the real Pocket Shell launcher runs on the T-Deck; home screen and touch confirmed by eye.
      `utils/pocket/tdeck-shell/compile.sh` compiles `shells/3ds/shell` in
      Docker (never install untrusted deps on the host) against the T-Deck
      host contract `utils/pocket/tdeck/pocket.host.json` (id tdeck-home:
      400x240 primary kept off screen + 320x240 auxiliary = the panel;
      capabilities input.buttons, input.touch.auxiliary, display.auxiliary,
      text.glyphs.baked). Package 1,642,056 B; image 3,020,960 B, only 4%
      (125 KB) free in the 3 MB slot, so there is no room for embedded applets
      without growing the slot. Firmware: off-screen primary display (PSRAM)
      + panel as AUXILIARY role, GT911 touch (I2C 0x5D, shared bus with the
      keyboard) as contacts. On device: boots in about 5 s, 30 fps, 0
      errors, 4.7 MB PSRAM and 361 KB internal RAM free. The CPU1 idle
      watchdog fired during boot (added CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1=n,
      not rebuilt yet). Touch reported working with the default mapping
      (TOUCH_SWAP=1, no flips) although two serial captures logged no
      `touch raw` lines, so touch logging was not independently confirmed.
      Keyboard buttons still untested.
- [~] Phase 5 (2026-11-08): applet install path. Findings so far:
      * Applets compile for the T-Deck host contract: Notes built against
        tdeck-home + `data.fs` is admitted (`applet: rules pass`), 627,944 B.
        Needs: Notes = data.fs; Hacker News = net.http (Wi-Fi + worker);
        Camera/Photos = camera.photo (no T-Deck camera). Notes is the first
        feasible applet.
      * The Tab5 firmware's `pocket_system` component
        (pocket-m5/programs/m5stack-tab5/components/pocket_system, MIT, about
        3,400 lines) holds the app store, launch/switch, data.fs, Wi-Fi/SNTP,
        Pocket Link and URL/QR installs. It is P4-only (esp_wifi_remote,
        REQUIRED_IDF_TARGETS esp32p4, shared sources from
        third_party/pocketjs/engine/host-services). Packages may be kept as
        files under the store root (`packages_partition = NULL`).
      * The firmware also needs the Tab5's app loop (take_switch, rebuild the
        runtime per app) and an op provider for appTable/appLaunch.
      * Size problem: the image is 3,020,960 B of 3,145,728 B. pocket_system
        will not fit the 3 MB slot. Options: (a) move the launcher package
        (1.64 MB) to the SD card and load it at boot; (b) grow the slot by
        shrinking vfs, which reformats vtOS `/flash` (back up first).
      Plan: port pocket_system to S3 with Wi-Fi off, store on an SD card
      (esp_vfs_fat sdspi on the shared SPI2 bus, CS 39), add the app loop and
      ops, then drop `notes.pocket` on the card and launch it from the home
      screen. Later: Wi-Fi + net.http worker for Hacker News.

## Minimal agent as a vtOS app (2026-11-08, supersedes the standalone C agent)

**Decision 2026-11-08:** the agent is a normal vtOS app, `modules/scripts/bin/agent.py`
(shell command `agent`), not a separate firmware slot. It reuses vtOS Wi-Fi,
keyboard, filesystem and `requests`. Verified: 28 host tests (fake model
server, `utils/tests/test_agent.py`) and a run on the device under MicroPython
(tools, SSE streaming parser, tool loop; app heap about 11 KB after import).
NOT yet verified against a real model API. Settings in `/flash/.agent`
(`agent set api_key ...`), files in `/flash/agent`. Until the firmware is
rebuilt, run it from the MicroPython prompt: `exit`, then
`import agentapp; agentapp.main(env, [])` (file copied to `/flash/agentapp.py`).
Still open: AssemblyAI voice (record with `env.rec`, upload + transcribe over
the REST API), freeze into a firmware build, real-API test, `/mem` style RAM
number on a live request. The standalone C version in `utils/agent/` is kept
only as a reference; the loader's third slot "agent" and the `agentfs`
partition are now unused.

### (older notes) Minimal agent UI in C, standalone slot

Goal: an opencode / Claude Code style terminal agent on the T-Deck, in C, small:
- UI: text console on the ST7789, keyboard in, trackball scroll.
- Brain: any OpenAI-compatible `/chat/completions` endpoint (base URL, key and
  model from a config file; streaming responses).
- Tools (pi-style minimal: tiny system prompt, flat tool set, plain loop, no
  planning/sub-agents): `list_files`, `read_file`, `grep_file` (case-insensitive,
  one file or all), `write_file`, sandboxed to one folder on the SD card. No
  shell, no network tools, no deletes.
- Voice: push-to-talk, AssemblyAI streaming speech-to-text with the user's API
  key; the transcript goes into the input line (editable) before sending.
- Memory budget: **under 200 KB of RAM** for the agent. Assumed meaning (user
  to confirm): the agent's own static + heap peak, internal SRAM and PSRAM
  together, measured with `heap_caps` at the busiest moment (a TLS reply with a
  tool call), and also report the whole-system number including the Wi-Fi
  driver, which cannot be shrunk. Hard rule: no full-screen framebuffer (320x240
  RGB565 is 150 KB by itself); draw text straight to the panel in small strips.

Design decisions:
- Slot: reuse the `pocket` partition (ota_2 @ 0x610000, 3 MB), rename the loader
  entry to "agent". Plain ESP-IDF 5.5.1 app in `utils/agent/` (same toolchain as
  the loader and iqlab), linked for 0x610000 with a build-time partition table.
- **No SD card (user decision 2026-11-08).** Config and notes live in a new
  internal SPIFFS partition `agentfs` (0x790000, 1.5 MB, carved from the agent
  slot, which is now 1.5 MB at 0x610000; `/flash` and the other slots are
  untouched). Config is set from the T-Deck keyboard with `/set KEY VALUE`
  (`wifi_ssid`, `wifi_pass`, `api_base`, `api_key`, `model`, `assemblyai_key`),
  shown masked by `/config`, never in the repo or chat. Needs the new partition
  table flashed once (0x8000).
- Sandbox: tools only touch the flat `/data/` partition; reject `..`, absolute paths and
  names longer than 31 bytes (SPIFFS); `write_file` asks for confirmation on the screen
  (path + byte count) before writing; file size cap about 8 KB per call.
- JSON: tiny hand-written writer and streaming scanner, no cJSON; history kept
  in a ring of about 12 KB and trimmed oldest-first; tool loop capped at 8 steps.
- Network: Wi-Fi station, SNTP for TLS dates, `esp_http_client` or raw mbedtls
  for streaming POST; TLS buffers in PSRAM (counted in the budget). One TLS
  session at a time: close the voice WebSocket before the chat request.
- Voice path: ES7210 codec (I2C 0x40) + I2S RX at 16 kHz mono, as in
  `modules/tdeck_i2s/audiorecorder.c` (I2C SDA 18/SCL 8, I2S MCLK 48, LRCK 21,
  SCK 47, DIN 14). Stream 50 ms PCM frames to
  `wss://streaming.assemblyai.com/v3/ws?sample_rate=16000&encoding=pcm_s16le`
  (`Authorization` header); read `Turn` messages for the transcript. No audio is
  ever buffered whole (5 s would be 160 KB).

Phases:
- [x] A0 (code, built 2026-11-08): loader menu entry "pocket shell" -> "agent"; `utils/agent/` skeleton,
      build-time partition table, flash script; boots to a blank console.
- [x] A1 (code, built, not yet run on the device): text console (8x8 font straight to the panel, no framebuffer),
      keyboard line editor, trackball scroll, SD mount, `read_file` /
      `write_file` tools with the sandbox, tried through `/read` and `/write`
      slash commands (no network yet). Measure RAM.
- [~] A2 (code written and built 2026-11-08, untested): config file, Wi-Fi + SNTP, streaming chat completion (text only),
      render tokens as they arrive. Measure RAM at the busiest moment.
- [~] A3 (code written and built 2026-11-08, untested): tool calling loop (streamed tool-call deltas, confirm on write).
- [ ] A4: AssemblyAI push-to-talk (ES7210 + I2S + WebSocket), transcript into
      the input line.
- [ ] A5: RAM audit against the 200 KB budget; trim; history/scroll polish;
      README for the agent.

## YouTube via SSH server (`yt`, 2026-11-08)

- [x] Server `tools/ytstream/ytstream.py`: yt-dlp -> ffmpeg -> 320x240 MJPEG +
      MP3 packets on stdout; tested with a synthetic source and a real 720p file
      (`utils/tests/test_ytproto.py`).
- [x] Client `modules/scripts/bin/yt.py` + `lib/ytproto.py`; `env.tft` exposed
      in `boot.py`/`env.py`; registered as shell `yt`. NOT run on a device yet.
- [ ] Real YouTube from this Mac fails: yt-dlp gets 403 / "format not
      available" for every client tried (yt-dlp 2026.03.17). Needs cookies or a
      JS runtime on the server (`YTSTREAM_YTDLP_ARGS`). Not solved.
- [ ] Rebuild vtOS firmware, add an SSH `Host yt` profile, test on the device:
      JPEG draw speed (SLOW mode), audio underruns while decoding, pause.
