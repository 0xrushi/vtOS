> **2026-11-08 update:** the PocketJS / Pocket Shell work (utils/pocket/) is
> **shelved** -- do not resume it unless the user asks. The third dual-boot slot
> (`pocket`, ota_2 @ 0x610000) now targets a minimal C agent UI; see TODO.md,
> "Minimal agent UI in C".

# Session summary — dual-boot + GUI loader + iqlab display (2026-11-08)

Working state: vtOS dual-boot is flashed and verified on the T-Deck Plus.
Remaining issue: the iqlab payload's display build fails to link.

## What works (on device, verified)

- **Dual-boot core**: `utils/dualboot/partitions-dual.csv` layout
  (loader=factory @0x10000, vtos=ota_0 @0x110000, iqlab=ota_1
  @0x510000, vfs @0x920000, 16 MB). Flashed; first boot lands in the
  loader, loader sets ota_0, vtOS boots, REPL verified
  (`MicroPython v1.28.0 on 2026-11-08`).
- **Rollback cycle**: `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`
  (MicroPython default; correct 5.5 name -- NOT
  `CONFIG_BOOT_ROLLBACK_ENABLE` which silently no-ops). Payloads never
  call `esp_ota_mark_app_valid`, so every reset rolls back to the
  loader. NVS `dualboot/target` written/read correctly.
- **vtOS payload**: rebuilt against the build-time table
  (`boards/LILYGO_T_DECK/partitions.csv`, vtos typed factory @0x110000
  so IDF links it at the ota_0 offset). App = 2,714,288 B, boots.
- **/flash**: reformatted clean at the new vfs offset, user files
  restored (menu/* json configs, .virc, WELCOME.md, sniff-526.pcap).
  `validate.pcap` and `menu/.loracfg.json` lost (skipped per dj's call).
- **Loader GUI source**: `utils/dualboot/loader/` builds clean
  (display_min.c ST7789 driver + font8x8 + menu/BOOT-tap/hold/serial
  logic, 266 KB). **Not yet flashed to the device** -- v2 (no-GUI,
  serial-only) is what's currently in the loader slot.
- **Display driver**: display_min.c/h written once, shared by loader
  and iqlab; same init sequence as vtOS's st7789 module (INVON, MADCTL
  0x60, RGB565, SPI2 @40 MHz, CS=12/DC=11/BL=42, SCK=40/MOSI=41).

## The open issue: iqlab display build -- REQUIRES does not propagate

`esp32-iq-tx` example project (`examples/ofdm_tx/`), sample_rates
branch. Goal: compile `display_min.c` into the example and draw a
status page (title, ch/backoff, live TX state on `tone`/`stop`).

### Evidence

- `main.c` + `ui_draw()` patches applied; SRCS/INCLUDE_DIRS from the
  patched `idf_component_register` DO take effect (display_min.c
  compiles, `-I examples/sample_rates` present).
- `REQUIRES esp_driver_gpio esp_driver_spi` in the same call does NOT:
  - compile line lacks `-I .../esp_driver_gpio/include` (works fine in
    the loader project with identical REQUIRES names)
  - link line lacks esp_driver_spi in the order-repeat segments after
    main -> `spi_bus_initialize/spi_bus_add_device/spi_device_transmit`
    undefined (symbols ARE in libesp_driver_spi.a; nm confirms)
- `PRIV_REQUIRES`, legacy `driver` component, and top-level
  `set(COMPONENTS main esp_driver_gpio esp_driver_spi ...)` all
  failed to fix it. COMPONENTS does add the libs to the build (they
  appear once, too early in link order).
- Configure emits no "component dropped" warnings. Container sees the
  patched CMakeLists content. Fresh build dir every attempt.
- Workaround that fixed the COMPILE half: explicit INCLUDE_DIRS
  `"$ENV{IDF_PATH}/components/esp_driver_gpio/include"` (+ spi).
  Link still fails.

### Root cause (FIXED 2026-11-08)

ESP-IDF evaluates each component CMakeLists once in an early-expansion
process (`cmake -P`) that does not receive `-DIQ_TX_EXAMPLE`. That pass
always took the first (`ofdm_tx`) branch, so REQUIRES was collected from
`firmware console freertos` only, while SRCS/INCLUDE_DIRS (re-evaluated in
the real pass with the variable defined) came from the `sample_rates`
branch. Hence compile-time includes worked but dependency/link order did not.

Fix (`/tmp/esp32-iq-tx/examples/ofdm_tx/main/CMakeLists.txt`): every branch
lists the same REQUIRES (`firmware console freertos esp_driver_gpio
esp_driver_spi`); the hand-written INCLUDE_DIRS and
`target_link_libraries` workarounds are removed. Clean build in
`espressif/idf:v5.5.1` links: `ofdm_tx.bin` 0xaedb0 B, fits ota_1.

### Loader/iqlab display bring-up (2026-11-08, fixed, loader verified on device)

Four bugs in `display_min.c` (shared by loader and iqlab) kept the panel blank:
1. GPIO10 (peripheral power) never driven high -> now set in `display_init`
   (with SD/LoRa CS held high on the shared bus).
2. No `RAMWR` (0x2C) after CASET/RASET, so pixel data was discarded -> added
   to `set_window()`. This was the actual blank-screen cause.
3. RGB565 sent little-endian -> `__builtin_bswap16` on pixel buffers.
4. Loader called `usb_serial_jtag_read_bytes()` without installing the
   driver (LoadProhibited panic) -> `usb_serial_jtag_driver_install()`.
The loader prints `display_init` result on serial and runs a red/green/blue
self-test at start.

### Next actions

1. (Loader GUI done.) Still to verify: BOOT tap/hold + serial toggle, iqlab display page and `tone 40`. Original item: flash loader GUI build to the loader slot (dance; 0x10000 single-
   region write), verify menu + BOOT tap/hold + serial toggle
   end-to-end, then flash the iqlab image (`.build-fix/ofdm_tx.bin` @
   0x510000) and verify the display and `tone 40`.

## Operational notes from today (keep)

- **macOS flash**: Docker `--device` passthrough does not work;
  flash host-side (esptool venv at /tmp/esptool-venv). After BOOT+RST,
  wait ~3 s before esptool (node-enumeration race, errno 6 vs "no
  serial data" = stale/fresh port churn). Direct single-shot esptool
  works; retry-loops reliably miss.
- **Build cache**: any change to the module set or partition layout
  needs `make clean` (volume build dir caches sdkconfig, module
  tables, qstr defs). sdkconfig.board edits need the generated
  `build-*/sdkconfig` deleted to take effect.
- **Bootloader entry**: vtOS runs TinyUSB CDC -- esptool cannot reset
  it into download mode; BOOT+RST dance required for every flash.
- The loader logs `held` (BOOT-hold ms) to NVS for post-mortems;
  `esp32.NVS('dualboot')` readable from either payload.
- **Flashing**: unplug USB, hold BOOT, replug, hold ~3 s, then wait for the
  port to settle (a few seconds) before esptool with `--before no_reset`.
  RST+BOOT timing often fails (strapping 0x27 = BOOT not held).
