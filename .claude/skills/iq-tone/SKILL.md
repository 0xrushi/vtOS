---
name: iq-tone
description: Explains the T-Deck IQ tone transmitter (iqtx / iqlab) - terms (Msps, DAC, IQ, NCO, channel, backoff), how tone frequency is derived from sample rate, loop length N and cycles m, the supported frequency range, and known limits. Use when working on modules/mod_iqtx, bin/iqtx.py, the iqlab dual-boot payload, or when asked why only "tone 40/80" exist or how to emit a given frequency.
---

# IQ tone transmitter (iqtx / iqlab)

Two front ends drive the same ESP32-S3 RF hardware path:

| Front end | Where | Status |
|---|---|---|
| `iqtx` (vtOS app, Alt+D launcher) | `modules/mod_iqtx/`, `modules/scripts/bin/iqtx.py` | Playback gated off: `BANK_PROTECTED = 0` (see TODO.md) |
| iqlab (dual-boot payload) | `esp32-iq-tx` repo, `examples/sample_rates` | Tone works; console is the USB serial port (`rates>`), not the T-Deck keyboard |

Commands: `tone 40|80`, `stop`, plus `reinit` and `info` in `iqtx`.
Upstream project: https://github.com/whstudio123/esp32-iq-tx (GPL-3.0-or-later; mod_iqtx is derived from it).

## What it does

It stores a short loop of IQ samples in on-chip RF SRAM, plays them through the DAC at a fixed rate, and the radio shifts the result up to a WiFi channel. The output is one steady tone at the channel center plus a small offset. It transmits on the air: stay within local rules and keep power low.

## Terms

- **Sample**: one number the hardware outputs.
- **Msps**: mega-samples per second. 40 Msps = 40 million samples a second, one every 25 ns. `tone 40` / `tone 80` select this rate; they are **not** a frequency.
- **DAC**: digital-to-analog converter. Turns each sample number into an output voltage, once per sample.
- **IQ**: each sample has two values, I (in-phase, horizontal) and Q (quadrature, vertical), a point on a plane. A point that circles steadily is a pure tone. Rotation direction distinguishes +offset from -offset, which one number per sample cannot.
- **Loop / N**: the stored samples repeat. N is the number of samples in one loop.
- **m**: number of full turns around the circle within one loop.
- **Offset**: tone frequency relative to the channel center.
- **Channel**: WiFi channel number, 1-13, centers at 2412 + 5*(ch-1) MHz (2412..2472 MHz).
- **Mixer / upconverter** (called "channel mixer" in explanations): adds the channel center to the low-frequency tone so it lands in the 2.4 GHz band.
- **NCO**: numerically controlled oscillator, a digital sine generator driven by a frequency number. The code clears its enable bits (`IQTX_NCO1/2/3`, bit 18) during playback so only the stored samples make the signal. Why the vendor path requires this was not confirmed.
- **RF SRAM bank**: 64 KB block at `0x3fcd0000..0x3fce0000` that the radio reads directly. Holds the IQ loop.
- **Backoff**: transmit power reduction in quarter-dB steps (`IQTX_MAX_BACKOFF` 120). Higher = weaker.
- **PHY / cert-test**: the chip's radio layer; playback uses vendor cert-test entry points in `librftest.a` (`CONFIG_ESP_PHY_ENABLE_CERT_TEST=y`).

## Frequency formula

```
offset  = Fs * m / N            (Fs = 40 or 80 MHz)
RF freq = channel_center + offset
```

Constants in `mod_iqtx.c`: rates `{40000000, 80000000}`, `IQTX_MAX_SAMPLES` 16128, `IQTX_LOOP_ALIGN` 256, `IQTX_MAX_CHANNELS` 13.

Current behavior: a fixed 64-sample circle (m = 1), so offset = Fs/64 = 0.625 MHz at 40 Msps and 1.25 MHz at 80 Msps nominal.

Measured on a HackRF (2026-11-08, iqlab channel 6 = 2437 MHz, 40 Msps): requested offsets 0.5 / 1.25 / 2 / 5 / -2 MHz appeared at 2437.37 / 2438.12 / 2438.86 / 2441.86 / 2434.86 MHz. That is a constant shift of about -0.13 to -0.14 MHz from nominal for every tone, independent of offset size and sign - NOT a scale factor (the earlier 78-89% reading was a misinterpretation). The whole carrier reads about 140 kHz low (about -57 ppm at 2437 MHz); the cause (HackRF clock error, ESP32 PLL placement, or both) is not determined.

## Why only 40 and 80

The S3 backend accepts only these two native DAC rates (`rom_dac_rate_set`). The S31 variant of upstream lists 16/32/40/80. The reason is probably fixed clock dividers in hardware, not confirmed.

## `tone <Msps> [offset_MHz]` command (implemented in iqlab; not in vtOS `iqtx`)

1. Target ratio r = offset / Fs.
2. For N in 256, 512, ... 16128 (multiples of 256) and integer m in 1..N/2, pick the pair minimizing |Fs*m/N - offset|; on ties prefer smaller N.
3. Print the actual offset and the resulting RF frequency.

Example: `tone 40 1.25` -> r = 1/32 -> N = 256, m = 8, exact. Negative offsets use negative m (circle reversed). Offset omitted = Fs/64 (the old fixed tone). |offset| must be < Fs/2. Source: `/tmp/esp32-iq-tx/examples/sample_rates/main.c` (planner `plan_tone`, waveform `fill_tone`). Commands can be typed on the T-Deck keyboard (I2C 0x55) or the USB serial console.

## Range

| | 40 Msps | 80 Msps |
|---|---|---|
| Smallest step (N=16128, m=1) | about 2.5 kHz | about 5 kHz |
| Theoretical max offset (m=N/2, Nyquist) | 20 MHz | 40 MHz |

At Nyquist the circle degenerates to two points, and analog filtering attenuates it first, so the usable ceiling is lower (not measured). Absolute coverage is about 2392-2492 MHz at ±20 MHz. 2000 MHz or any other band is unreachable: the synthesizer only does WiFi channels 1-13. The SX1262 LoRa radio is the only freely tunable radio, but it sends LoRa modulation, not a raw tone.

## Which one to use: iqlab (dual-boot) or `iqtx` (vtOS app)

Today (2026-11-08) use **iqlab**. The `iqtx` app cannot transmit: `BANK_PROTECTED` is 0, so `tone` is refused to protect the MicroPython heap. Its `init`/`info`/capability calls work, which is all it does.

| | iqlab (dual-boot payload) | `iqtx` (vtOS app) |
|---|---|---|
| Works today | Yes: tones, offsets, keyboard input, HackRF-verified | No: playback gated off |
| Switching cost | RST, tap BOOT in the 3 s loader window, reboot | None, run from the shell or Alt+D |
| RAM / RF ownership | Owns all RAM and the radio, no WiFi/BLE stack | Shares RAM with MicroPython; RF bank must be carved out of the heap |
| Scripting | Console commands only (`tone`, `stop`) | Python: sequences, sweeps, combine with LoRa/sniff/GPS data |
| Risk | Flashing loader/payload slots; a bad image rolls back to the loader | A bad vtOS image (the reservation fix) can fail to boot and fall back to the loader |
| Frequency control | `tone <Msps> [offset_MHz]` | Fixed Fs/64 tone only until ported |

Use **iqlab** for lab tests, calibration against a HackRF, and anything that needs a reliable tone now.

Use **`iqtx`** only once the linker-fragment fix lands and `BANK_PROTECTED` is 1. Its real value is automation from Python: stepping offsets, sweeping channels, timing bursts, logging with other vtOS tools, and not rebooting between experiments. Until then it is not useful for transmitting, but the module and app are kept as the base for that port.

## Known limits and open items

- Power drifts between stop/start cycles (S3 lacks the TX power-control-field restore); `reinit` is the workaround.
- vtOS `iqtx`: the link-time reservation `SOC_RESERVE_MEMORY_REGION(0x3fcd0000..0x3fce0000)` kills boot on this board (bisected 2026-11-08), so playback refuses with "RF BANK UNPROTECTED". Planned fix per TODO.md: carve the bank with a linker fragment, then set `IQTX_BANK_PROTECTED` to 1.
- iqlab keyboard input and the offset parameter were added 2026-11-08 and built, not yet verified on device at the time of writing.
- Unverified: negative offsets (reverse circle), whether hardware requires N to be a multiple of 256, and why NCO must be off.
- While running, the app owns the RF: WiFi and BLE are taken down (`iqtx` restores WiFi on quit).

## Flashing / dual-boot notes

Loader at 0x10000, vtOS at 0x110000, iqlab at 0x510000. See `summary.md` for the working flash procedure (unplug USB, hold BOOT, replug, wait for port to settle, `--before no_reset`) and the display bring-up fixes (GPIO10 power, RAMWR, byte order).
