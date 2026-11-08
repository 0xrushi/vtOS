# Dual boot: vtOS and the iqlab RF lab on one T-Deck

One 16 MB flash holds a tiny **loader** and separate **payload** firmwares. Every
reset shows a 3-second menu; pick the payload, it boots, and the next reset
comes back to the menu.

## Why it exists

- **iqlab** plays raw IQ samples through the radio hardware (an arbitrary tone
  next to a WiFi channel, checked with a HackRF). That needs a 64 KB RF SRAM
  block and the radio to itself. MicroPython's heap normally owns that RAM:
  reserving it at link time stopped vtOS from booting (see TODO.md, "PLAYBACK IS
  GATED OFF"), so the vtOS `iqtx` app cannot transmit. A separate firmware has
  no such conflict.
- **While transmitting, WiFi and BLE are off.** The radio belongs to the tone.
- **Safe experiments.** A payload that is never marked valid rolls back to the
  loader on the next reset, so a bad build can't lock you out.
- **Different toolchains.** vtOS (MicroPython) and iqlab (plain ESP-IDF) are
  built separately; they only have to agree on partition offsets.

## Flash layout (`partitions-dual.csv`)

| Slot | Offset | Size | What |
|---|---|---|---|
| loader (factory) | 0x10000 | 1 MB | menu + switching logic (this folder, `loader/`) |
| vtos (ota_0) | 0x110000 | 4 MB | vtOS MicroPython firmware |
| iqlab (ota_1) | 0x510000 | 1 MB | esp32-iq-tx lab payload |
| ota_2 | 0x610000 | 1.5 MB | free (a third menu entry; named "agent", unused) |
| agentfs | 0x790000 | 1.5 MB | unused data partition |
| vfs | 0x920000 | 6.9 MB | vtOS `/flash` filesystem |

Apps are position dependent, so each payload is built with a build-time table
that places it at its own offset (`boards/LILYGO_T_DECK/partitions.csv` for
vtOS, `partitions-iqlab.csv` for iqlab). The table flashed at 0x8000 is the dual
one above.

## First-time setup

**Back up `/flash` first** if you have anything on it
(`mpremote connect PORT cp -r : ./flash-backup/`). Moving from the old
single-image layout reformats `/flash`; updating a slot later does not touch it.

1. Build the loader:
   ```bash
   cd utils/dualboot/loader
   docker run --rm -v "$PWD":/work -w /work espressif/idf:v5.5.1 idf.py build
   ```
2. Build vtOS (needs `make init` once):
   ```bash
   make build        # build_output/micropython.bin
   ```
3. Build iqlab (GPL-3.0 upstream, patched; the patch and overlay are in `utils/iqlab/`):
   ```bash
   git clone https://github.com/whstudio123/esp32-iq-tx.git && cd esp32-iq-tx
   git checkout cf88cb6
   git apply ../utils/iqlab/esp32-iq-tx-local.patch
   cp -R ../utils/iqlab/overlay/examples/. examples/
   D='-DIQ_TX_EXAMPLE=sample_rates -DSDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.defaults.esp32s3;sdkconfig.dualboot -DSDKCONFIG=/work/.build-iqlab/sdkconfig'
   docker run --rm -v "$PWD":/work -w /work/examples/ofdm_tx espressif/idf:v5.5.1 idf.py -B /work/.build-iqlab $D set-target esp32s3
   docker run --rm -v "$PWD":/work -w /work/examples/ofdm_tx espressif/idf:v5.5.1 idf.py -B /work/.build-iqlab $D build
   ```
4. Put the T-Deck in download mode (see below), then flash everything in one go
   (use your own paths and port):
   ```bash
   L=utils/dualboot/loader/build
   esptool --chip esp32s3 -p PORT -b 460800 --before no_reset --after hard_reset write_flash \
     --flash_mode dio --flash_size 16MB --flash_freq 80m \
     0x0      $L/bootloader/bootloader.bin \
     0x8000   $L/partition_table/partition-table.bin \
     0xd000   $L/ota_data_initial.bin \
     0x10000  $L/dualboot_loader.bin \
     0x110000 build_output/micropython.bin \
     0x510000 esp32-iq-tx/.build-iqlab/ofdm_tx.bin
   ```

> **Do not use `make flash` once dual boot is installed.** It runs
> `erase_flash` and writes the old single-image `firmware.bin` at 0x0, which
> wipes the loader, both payloads and `/flash`.

## Day to day

- **Pick a payload:** press RST. In the 3-second menu, tap BOOT to move the
  highlight, hold BOOT for 1 second to boot at once, or let the timer boot the
  highlighted (last used) one. Over the USB serial console (115200) the keys
  `1` (vtOS), `2` (iq lab) choose too.
- **Update one slot** (device in download mode):
  `esptool ... write_flash 0x110000 build_output/micropython.bin` for vtOS, or
  `... write_flash 0x510000 .../ofdm_tx.bin` for iqlab. The other slot and
  `/flash` are untouched. `.../write_flash 0x10000 dualboot_loader.bin` updates
  the loader.
- **Every reset returns to the menu.** That is deliberate (payloads are never
  confirmed valid); vtOS keeps running as long as you don't reset.

## Getting into download mode

- From vtOS: leave the shell with `exit`, then at the `>>>` prompt run
  `import machine; machine.bootloader()`.
- From the loader or iqlab (they use the USB serial/JTAG console): esptool can
  reset the chip itself, `--before default_reset`.
- Otherwise: unplug USB, hold BOOT, plug in, keep holding about 3 seconds, wait
  a few seconds for the port, then use `--before no_reset`. The port name can
  change after replugging (`/dev/cu.usbmodem...`).

## Recovery

The loader is the factory app, so a broken payload only breaks that slot:
re-flash the slot, or flash just the loader and table again. Only
`erase_flash` (or `make flash`) removes the loader.

## Loader behavior

Boot picks the last used slot; an empty slot is dimmed, skipped when cycling
and falls back to vtOS. The loader draws the screen (power pin, ST7789 init)
itself and logs to the USB serial console.
