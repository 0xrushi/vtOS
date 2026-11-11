# MeshCore on the T-Deck (`meshchat --meshcore`)

A MeshCore **client** for the T-Deck's SX1262, added next to the Meshtastic node
in `meshchat`. It can import contacts from a contact link, chat on channels,
message contacts, and **log in to a repeater and send it remote commands** from
the repeater's chat tab. It never relays other people's traffic.

**Status: host tests only.** Every wire layout here was written from memory of
the MeshCore sources and has **not** been checked against a real MeshCore node,
repeater or app. The tests (`utils/tests/test_mc*.py`) prove the code agrees with
itself, with pycryptodome (AES, Ed25519) and RFC 7748 (X25519), and with a
simulated repeater built from the same layouts. Nothing has been run on the
device or on the air. Treat the first contact with a real repeater as bring-up.

## Use

```
meshcore import <link>      add a contact (meshcore://... link, advert hex, or 64-hex key)
meshcore id                 show your name, public key and a share link (creates the key on first use)
meshcore list | rm <name>
meshcore radio 910.525 62.5 7 5 [power]     freq MHz, bandwidth kHz, SF, CR (5 = 4/5)
meshcore name <name>        your node name (max 16)
meshcore channel <name> <32 hex>            add a private channel (16-byte secret)
meshchat --meshcore         the chat UI
```

In the chat UI (`/help` lists these):

| Control | What it does |
|---|---|
| `/import <link>` or Menu > Import contact | add a contact; with no argument opens a paste prompt (Alt+V). A heard advert can be added by name: `/import Hill` |
| Nodes list / `/dm name` | open a contact's chat |
| `/login [password]` or `l` on a repeater's detail screen | log in; without an argument a masked password prompt opens (empty = guest) |
| text in a logged-in **repeater** chat | sent as a remote CLI command; the reply shows in the same tab tagged `reply` |
| `/heard` | adverts heard from nodes you have not added |
| `/id` | your key and share link |
| `/radio freq bw sf cr [power]` | retune now and save; must match the repeater |
| `/forget name` | delete a contact |

Messages to normal contacts show `...` / `sent` / `ok` (ACK received) / `!` (no
ACK in 30 s, no retry yet). Channel messages only ever show `sent`. Replies from
a repeater count as the ACK for your CLI command. The Info and Stats pages show
the modem settings and counters.

## What is implemented

- Packet codec (flood/direct/transport routes, path), duplicate suppression.
- AES-128-ECB + 2-byte HMAC-SHA256 sealing; ECDH from Ed25519 keys (pure Python
  Curve25519/Ed25519 in `lib/mccurve.py`, about a second per new contact, cached
  in RAM). `mtcrypto.ecb()` reuses the cryptolib-with-self-test approach of the
  Meshtastic CTR code, with a pure-Python fallback.
- Text to/from contacts (with ACK), group text on the Public channel and your
  own channels, repeater/room login (ANON_REQ), repeater CLI.
- Contact links, adverts (name, type, position), signature check where SHA-512
  exists.

## Not implemented / assumptions to verify

- **Contact link format is a guess.** The repo has no MeshCore sample. Accepted:
  `meshcore://<hex of an advert packet>`, `meshcore://contact/add?name=..&public_key=<64 hex>&type=N`,
  bare advert hex, bare 64-hex key. If your app produces something else, send an
  example and `mcproto.parse_contact_link` gets a branch.
- Radio defaults (910.525 MHz, BW 62.5 kHz, SF7, CR 4/5, sync word 0x12,
  preamble 16) are the common US community setting / library defaults from memory.
  Frequency, BW, SF and CR **must equal the repeater's**; set them with
  `meshcore radio` or `/radio`. If frames are never heard, the preamble length or
  sync word is the first suspect (`mcmesh.DEFAULT_RADIO`, `State.radio_args`).
- Anything on the wire that may differ in the real firmware: header/path-length
  byte (newer firmware packs a hash-size field into the top two bits of
  `path_len`; this code treats path bytes as 1 byte per hop), advert app-data
  layout, ACK = first 4 bytes of SHA-256(plaintext + sender key), the login
  RESPONSE layout, `txt_type` 1 = CLI, and the 150-byte text limit.
- Always flood-routed; PATH returns and direct routing are ignored (works, but
  noisier). No retries, no CAD / listen-before-talk, no duty-cycle limit: **mind
  your region's rules** before transmitting. No adverts are sent by us, so the
  mesh does not learn this node's name (repeaters still accept the login because
  the ANON_REQ carries the key).
- Heard adverts are shown unverified (no signature check on the device: SHA-512
  may be missing, and verifying is slow). Imported links are verified when
  possible; a link with a bad signature is rejected. A heard advert you add by
  name is trusted by name only: check the key with `meshcore list`.
- No room-server post history sync, no telemetry/status requests, no
  per-contact path storage, no contact-sharing export other than `/id`.
- On-device speed of the pure-Python Ed25519/X25519 (`cryptolib` has no
  big-integer help) and the AES-ECB self-test are unmeasured.

## Files

`lib/mcproto.py` codec, sealing, adverts, links · `lib/mccurve.py` curve maths ·
`lib/mcmesh.py` state, engine, `meshcore` command logic · `lib/mcback.py` the
meshchat backend · `bin/meshcore.py` shell command · `lib/mtcrypto.py` `ecb()`.
Settings, identity key (secret!), contacts and channels live in
`/flash/.meshcore.json` ([SECRETS.md](SECRETS.md)); it is included in
`settings export`. Repeater passwords are never stored.

## Bring-up checklist

1. `make build`, flash the vtOS slot. `meshcore id` and `meshcore import <link>`.
2. `meshcore radio ...` to match the repeater, then `meshchat --meshcore`; Menu >
   Stats should show your modem settings and `crypto cryptolib-ecb`.
3. Open the repeater, `/login <password>`; expect "Logged in". If nothing comes
   back, compare radio settings first, then packet layouts above.
4. Send `ver` in the repeater chat and read the reply.
