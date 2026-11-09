# Meshtastic on the T-Deck (`meshchat`)

`meshchat` is a Meshtastic node written in MicroPython on the T-Deck's SX1262:
it uses the same radio settings, packet format and AES encryption as the
official firmware, so it can talk to Meshtastic nodes on the same preset,
frequency slot and channel key. It has a chat UI, node list, channel manager
and a settings TUI. There is no official Meshtastic library for MicroPython, so
the protocol layer is written from the public documentation and checked against
the generated protobuf classes of the official `meshtastic` Python package.

**Status:** all logic is tested off-device (`utils/tests/test_mt*.py`,
`test_meshset.py`). It has **not been run on the air** against a real Meshtastic
node yet. Do that first (see "First bring-up").

## Run it

```
meshchat              # real node (first run: region US, LongFast, default channel)
meshchat Rush         # also sets the long name on first use
meshchat --demo       # scripted fake mesh, no radio
meshchat --raw        # old lorachat-style "nick: text" over LoRa (no Meshtastic)
```

Keys: trackball left/right switch chats, up/down scroll, click opens the menu
or goes back, long press quits. Type to compose, Enter sends. `/help` lists the
slash commands (`/nodes /dm /node /trace /pos /announce /fav /settings
/channels /stats /info /clear /quit`).

## What it does

- **Channels and direct messages**: up to 8 channels, each with its own name and
  key (none, the public default key, 128-bit or 256-bit AES, or your own). DMs
  use a per-node tab. Messages show hops and SNR.
- **Reliability**: delivery marks (`...` queued, `sent` on air, `ok` acked, `!`
  failed). Direct messages wait for a routing ack; channel messages count a
  neighbour relaying them as the ack. Up to 3 retransmissions.
- **Relaying**: managed flooding with a hop limit, duplicate suppression, and
  SNR-weighted back-off (strong signals wait longer so distant nodes relay
  first). Roles Router/Repeater relay with priority, Client Mute never relays.
- **Node database**: names, role, hops, SNR, battery/voltage, uptime, position,
  distance and bearing from you, favourites, ignore. Saved across runs.
- **Announcements**: node info, position (GPS on the Plus, or a fixed position,
  with precision blurring) and device telemetry on configurable intervals.
- **Traceroute, position and name requests** from a node's detail screen.
- **Channel links**: export/import `https://meshtastic.org/e/#...` links (the
  same format the phone apps use). The export also writes
  `/flash/mesh-channels.txt`. Treat links as secrets, they contain the key.
- **Duty-cycle and power limits**: regional power cap, EU 10% duty cycle
  enforced, optional override for regions without one.

## Settings TUI (Menu > Settings)

Radio (region, preset or manual bandwidth/SF/CR, frequency slot, hop limit,
transmit on/off, TX power, RX boost, offset/override frequency), Channels,
Device (names, role, announce interval, channel-message confirmation), Position,
Telemetry, Advanced (TCXO, RF switch, PA current limit). Changes are made on a
copy and only take effect on **Apply changes**; leaving with unapplied edits
asks first. A line at the top of the Radio section always shows the resulting
slot, frequency, modem and power. Region defaults to **US** (902-928 MHz,
LongFast = slot 20 = 906.875 MHz).

## Not implemented (honest list)

- **Public-key direct messages.** Current Meshtastic firmware encrypts DMs with
  per-node keys when both sides know each other's key. This node sends DMs with
  the channel key (still accepted by the firmware) but cannot read PKI-encrypted
  DMs from modern apps.
- Admin messages / remote configuration, MQTT, store-and-forward, canned
  messages, waypoints on a map, detection-sensor/other modules, neighbour info.
- Intermediate relays are not recorded in traceroutes we forward.
- Slot auto-pick for presets other than LongFast relies on the preset's default
  channel name; those names are from the firmware as remembered, not from the
  docs. The Radio screen warns, and setting the slot by hand avoids the issue.

## First bring-up checklist

1. Rebuild and flash vtOS (`make build`, then write only the vtOS slot; see the
   dual-boot notes in the `iq-tone` skill) so `meshchat` and its libraries are
   built in.
2. `meshchat`, then Menu > Stats: `crypto` should read `cryptolib-ecb` (the
   T-Deck's cryptolib has no CTR mode, so CTR is built on its ECB).
3. Start a stock Meshtastic node (US, LongFast, default key) near the T-Deck.
   Within about 15 s the T-Deck's name announcement should appear on it, and its
   node should appear in Menu > Nodes.
4. Send a channel message each way, then a DM. Watch for `ok` marks.
5. If the radio won't start, Settings > Advanced > Radio TCXO can pin a voltage
   (auto-detect tries 0, 1.8, 2.8, 2.4, 3.3 V and remembers the one that works).

## Radio driver

`lib/sx126x.py` drives the SX1262 from MicroPython on the board's shared SPI
object, with the Meshtastic options (16-symbol preamble, sync word 0x2B, DIO3
TCXO, DIO2 RF switch, over-current limit, CAD, RX boost). The native `lora`
module is only a fallback: on the test unit its RadioLib HAL reported "chip not
found" (error -2) while the Python driver read the chip version string and ran
fine. Every radio transaction slows the shared bus to 8 MHz and sets
`env.sd_busy` so the display skips its draw, then restores the display speed.
With TCXO set to auto-detect, the first start tries 0, 1.8, 2.8, 2.4 and 3.3 V;
the test unit needed **1.8 V**, which is then remembered.

## Verified on the T-Deck so far

Protocol, crypto and UI paths run under MicroPython on the device and match the
host results (a sealed packet is byte-identical to the host's). The radio comes
up (no device errors), transmits (TX done) and retunes between regions. **Not
yet verified:** receiving a real Meshtastic packet. In a 60 s listen on 906.875
MHz nothing was heard, which may just be a quiet area or an antenna/band issue
(the unit's LoRa band is unconfirmed).

## Files

`lib/mtcrypto.py` AES-CTR (cryptolib with a pure-Python fallback, self-tested) ·
`lib/mtproto.py` protobuf subset, header, keys, channel links · `lib/mtradio.py`
presets, regions, slots, airtime · `lib/mtconfig.py` settings and form schema ·
`lib/mtmesh.py` routing engine · `lib/sx126x.py` radio driver · `lib/meshback.py` backends · `lib/meshdraw.py`,
`lib/meshui.py`, `lib/meshset.py` UI · `bin/meshchat.py` entry point ·
`utils/mesh_preview.py` prints any screen at any size on the host.
