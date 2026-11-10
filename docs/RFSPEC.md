# Sub-GHz spectrum (`rfspec`)

A spectrum view for the T-Deck's LoRa chip (SX1262): a live trace with peak hold on top, a waterfall
underneath, so you can see which channels are busy, where the noise floor sits and whether an antenna
is doing anything. **Receive only**, nothing is ever transmitted.

**Status:** the sweep driver, scaling, rendering (all font grid sizes) and key handling are tested off
the device (`utils/tests/test_rfscan.py`, `test_sx126x.py`). It has **not been run on the T-Deck yet**.
First checks on hardware: that levels move when you key a known transmitter nearby, and that the
settle time (`settle_us`, 600 by default) is long enough for a stable reading after each retune.

## Run it

```
rfspec                    # the band of your Meshtastic region (US: 915 MHz, 50 MHz span)
rfspec 868 30             # centre 868 MHz, span 30 MHz
rfspec 433.9 5 62.5       # also the resolution bandwidth in kHz (62.5, 125, 250 or 500)
rfspec --demo             # a fake band, no radio needed
```

Keys: trackball left/right pan by a quarter span, up/down change the span (2, 5, 10, 20, 30, 50, 100,
200 MHz), `b` cycles the resolution bandwidth, `p` pauses, `h` peak hold on/off, `c` clears the peak
hold, `q` or a trackball click quits. The top line shows the strongest point of the latest sweep.

## What it is, and what it is not

The SX1262 only measures power inside its own channel. A "sweep" retunes it step by step and reads
the instantaneous RSSI at each step, so this is a stepped scanner, not a real analyzer:

- The channel width you pick is the resolution bandwidth, and the step is that width (or wider: a
  sweep is capped at 160 points, so a 200 MHz span steps by about 1.3 MHz and **can miss narrow
  signals between steps**).
- A sweep of 160 points takes roughly 0.2 s, so short bursts (key fobs, brief LoRa packets) are
  caught only when they happen to coincide with a step.
- Levels are the chip's own RSSI estimate, not calibrated, and the antenna and matching network are
  built for one band per T-Deck variant. Away from that band readings are real but much less sensitive.
- It uses the LoRa receiver, so the view is limited to the chip's 150-960 MHz tuning range.

## Sharing the radio

`rfspec` takes the radio exclusively, like `loramon` and `meshchat`. Quit `meshchat` first. On exit it
releases the radio so the next app reconfigures it. It reuses the TCXO voltage and RF-switch settings
`meshchat` found for the board (Settings > Advanced), and tries the usual voltages if none is saved.

## Files

`lib/rfscan.py` sweep state, scaling, rendering, demo band and radio start-up (pure logic) ·
`bin/rfspec.py` the app · `lib/sx126x.py` `SX126x.sweep()` and `rssi_inst()`.
