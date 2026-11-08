# IQ tone on the T-Deck (iqlab)

A tone generator that uses the ESP32-S3's Wi-Fi radio hardware. You pick a
sample rate and a frequency offset; the chip transmits one steady carrier at
that offset from a Wi-Fi channel. It is a lab tool: check it with a receiver
such as a HackRF, keep power low, and only transmit where the rules allow it.

It runs from the **iqlab** payload of the dual-boot loader (see
[utils/dualboot/README.md](../utils/dualboot/README.md)). The vtOS `iqtx` app
cannot transmit yet.

## Use it

1. Press RST, tap BOOT in the 3-second menu to highlight **iq lab**, and let
   it boot (or hold BOOT for 1 second).
2. The screen shows the channel (default 6, 2437 MHz), the TX state, and an
   input line at the bottom. Type on the T-Deck keyboard, or use the USB
   serial console (`rates>` prompt, 115200 baud).
3. Commands:

| Command | What it does |
|---|---|
| `tone 40` or `tone 80` | transmit at 40 or 80 Msps with the default offset (rate / 64) |
| `tone 40 1.25` | transmit with a +1.25 MHz offset from the channel centre |
| `tone 40 -2` | negative offsets work too (the other side of the carrier) |
| `stop` | stop transmitting |

The screen then shows the offset it actually got, the loop length N and the
turn count m (see below). RST returns to the loader menu.

While iqlab transmits, Wi-Fi and Bluetooth are off: the radio belongs to the
tone.

## What the words mean

- **Sample**: one number the hardware outputs. **Msps** is mega-samples per
  second; 40 Msps means 40 million numbers every second.
- **DAC** (digital-to-analog converter): turns each number into an output
  voltage. Played in a loop, the numbers make a wave.
- **IQ**: each sample is two numbers, I (horizontal) and Q (vertical), a point
  on a plane. A point that goes around a circle at a steady speed is a pure
  tone. Going clockwise or counterclockwise tells a +offset from a -offset,
  which one number per sample could not.
- **Loop, N and m**: the samples are stored as a loop of N points that goes
  around the circle m times. Then the loop repeats forever.
- **Channel**: a Wi-Fi channel number, 1-13. Its centre is
  2412 + 5 x (channel - 1) MHz, so channel 6 is 2437 MHz.
- **Mixer** (upconverter): adds the channel centre to the low-frequency tone so
  it lands in the 2.4 GHz band.
- **NCO**: a digital oscillator that can make a sine from a frequency number.
  The code switches it off during playback, so the stored samples alone make
  the signal.
- **RF SRAM bank**: 64 KB of on-chip memory the radio reads directly. It holds
  the loop.
- **Backoff**: a vendor value used when the radio is prepared (default 60,
  quarter-dB steps). It is *not* a calibrated output power; check the real level
  with a receiver.

```
 sample memory ---> DAC ---> mixer ---> antenna
 (N numbers,      (40 or    (adds the
  m turns)        80 Msps)   channel)
   content          rate     channel
```

## The frequency formula

    offset = Fs x m / N        (Fs = 40 or 80 MHz)
    transmitted = channel centre + offset

`tone <Msps> <offset_MHz>` picks the pair (N, m) that lands closest to what
you asked for:

- N: a multiple of 256, up to 16128 samples.
- m: a whole number of turns with |m| < N/2 (below the Nyquist limit).

Example: `tone 40 1.25` asks for 1.25 / 40 = 1/32 of a turn per sample, which is
exactly m = 8 in a loop of N = 256. A request like 1.3 MHz has no exact pair, so
it uses the nearest and shows the offset it really got.

## Range and limits

| | 40 Msps | 80 Msps |
|---|---|---|
| Smallest step (N = 16128, m = 1) | about 2.5 kHz | about 5 kHz |
| Largest offset, in theory (m = N/2) | 20 MHz | 40 MHz |

- Near the largest offset the circle collapses to two points and the analog
  filtering weakens the tone first. The usable range is smaller; I have not
  measured where.
- The carrier can only sit on Wi-Fi channels 1-13 (2412-2472 MHz), plus the
  offset: roughly 2392-2492 MHz. The chip's transmitter cannot go to other
  frequencies such as 2000 MHz.
- Only 40 and 80 Msps are accepted on the ESP32-S3.
- Power is not stable across stop/start cycles: a tone that was strong can be
  weak after a restart, and the other rate can be strong again. The vtOS
  `iqtx` app has a `reinit` command for this; iqlab does not yet.

## Measured on a real receiver

Tested on channel 6 (2437 MHz) at 40 Msps with a HackRF. The plot and logs are
in `utils/hackrf/`; `plot_tone_log.py` redraws them:

| Requested offset | Seen at | Seen offset |
|---|---|---|
| +0.5 MHz | 2437.37 MHz | +0.37 |
| +1.25 MHz | 2438.12 MHz | +1.12 |
| +2 MHz | 2438.86 MHz | +1.86 |
| +5 MHz | 2441.86 MHz | +4.86 |
| -2 MHz | 2434.86 MHz | -2.14 |

Every tone came out about 0.13-0.14 MHz low, whatever its size or sign. That is
a constant shift of the whole carrier (about -57 ppm at 2437 MHz), not a scale
error. I do not know whether it comes from the HackRF's clock, from how the
ESP32 places its channel frequency, or both. A second receiver would settle it.
The screen shows the offset the plan reached, not the corrected one; expect
the receiver to read about 0.14 MHz lower.

To watch it yourself (receive only):

    hackrf_sweep -f 2434:2442 -w 10000 -l 24 -g 30

The tone shows as one narrow spike. Keep the HackRF a short distance away: a
tone this close can overload it.

## Rules and safety

You are transmitting a radio signal on a shared band. Rules differ by country;
keep power low, use it indoors or in a shielded setup, and do not interfere with
other people's networks. You are responsible for staying within the law.

## Where the code is

- iqlab firmware: patched upstream project, see `utils/iqlab/` and
  [whstudio123/esp32-iq-tx](https://github.com/whstudio123/esp32-iq-tx)
  (GPL-3.0-or-later).
- vtOS `iqtx` app: `modules/mod_iqtx/` and `modules/scripts/bin/iqtx.py`. Its
  playback is disabled because reserving the RF RAM bank at link time stops
  vtOS from booting.
