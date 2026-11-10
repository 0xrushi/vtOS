# VNC client (`vnc`)

`vnc` shows a remote desktop on the T-Deck's 320x240 display and sends the
keyboard and trackball back to it. It speaks plain RFB (the VNC protocol), so
any server that offers **VNC password** or **no** authentication works.

```
vnc 192.168.1.4            # display :0, port 5900, asks for the password
vnc 192.168.1.4:1 hunter2  # display :1 (port 5901), password on the command line
vnc 192.168.1.4::5905      # an explicit port
vnc home                   # an alias, see below
```

`host:N` means display N (port 5900+N) for N below 100, and a port otherwise,
as in other VNC viewers. The password is only asked for when the server wants
one, and Esc at the prompt cancels.

## Controls

| Input | Does |
|---|---|
| Trackball | moves the mouse pointer (rolling fast moves further) |
| Trackball click | left click |
| **Touch: tap** | moves the pointer to that spot and left-clicks (sets the cursor in a text field) |
| **Touch: swipe** | scrolls like a phone: drag up to scroll down, drag sideways to scroll sideways; the wheel acts where the finger began |
| **Alt+M** | changes what the trackball does (below); a small label shows the new mode |
| **Mic key** | voice typing: press to record (red `REC`), press again to transcribe (`WAIT`) and type the words into the remote window (below) |
| Keyboard | types into the remote machine: letters, digits, symbols, Enter, Backspace |
| Hold the click | quit |

Alt+M modes, in order:

| Label | Trackball | Click |
|---|---|---|
| `MOUSE` | moves the pointer | left button |
| `RIGHT` | moves the pointer | right button |
| `SCROLL` | up/down/left/right = mouse wheel | left button |
| `KEYS` | arrow keys | Escape |

The keyboard has no Ctrl or Esc key and does not report Alt to apps, and the
click is press-and-release only. So there is no dragging and no key chords, and
Escape lives in `KEYS` mode.

Touch works in every trackball mode. One finger only; a touch held still for more
than 0.6 s does nothing, and a tap on the black border around a small desktop
lands on the nearest edge of the picture. If the touch controller fails, touch
is switched off for the session and the rest keeps working.

### Voice typing

One-time setup, in the vtOS shell (the key stays on the device and is never
printed): `agent set assemblyai_key <your AssemblyAI API key>`.

In `vnc`, press the Mic key and speak (up to 15 s), then press it again. The clip
is sent to AssemblyAI over Wi-Fi (not through the VNC connection), and the
transcript is typed into the remote machine as key presses, so the remote window
needs a text field in focus. It is push-to-talk, not live: expect a few seconds
of `WAIT`. A tap under 0.4 s is ignored. Labels: `NO KEY` = key not set, `ERR` =
no Wi-Fi, a rejected key or a failed transcription (nothing is typed). Only
ASCII/Latin-1 characters are typed. Audio is uploaded to AssemblyAI; leave the
Mic key alone if that is not acceptable.

## Setting up the server

The T-Deck screen is 320x240. If the remote desktop is bigger, `vnc` shows a
320x240 window of it and scrolls that window when the pointer nears an edge
(each scroll refetches the window, so it is slower than a small desktop). For
the best result make the remote desktop small:

```sh
# a throwaway 320x240 desktop with TigerVNC
Xvnc :1 -geometry 320x240 -depth 24 -rfbport 5901 \
     -SecurityTypes VncAuth -PasswordFile ~/.vnc/passwd
```

A desktop smaller than the screen is centred. If the server's desktop changes
size while you are connected, `vnc` follows (tested against the fake server only).

The server must offer **VNC authentication** (a password, first 8 characters
count) or no authentication. Only TigerVNC has been tried. Others using plain
"VNC password" mode (x11vnc, TightVNC, UltraVNC, RealVNC Server's VNC-password
setting, macOS Screen Sharing's "VNC viewers may control screen with password")
should work but are untested here. A server that offers only user-name, TLS or
VeNCrypt authentication is refused with `unsupported security types [...]`,
naming the types it offered.

**Security:** VNC password authentication is weak and nothing is encrypted by
VNC itself: pixels, keystrokes and the password exchange cross the network in
the clear. Use it on a network you trust. (Over Tailscale the tunnel itself is
encrypted.)

## Over Tailscale

`vnc` also reaches a numeric Tailscale address (100.64.0.0/10), for example your
computer's tailnet IP:

```
tailscale up                  # once after each boot: joins the tailnet (see docs/TAILSCALE.md)
vnc 100.96.103.69:1 secret
```

It waits (up to 30 s) for the tunnel to that machine, then connects through it.
Only numeric addresses work, not MagicDNS names, and the server must accept
connections on its tailnet address (Docker's published ports do). If the tunnel
is not up, `vnc` says to run `tailscale up`. This needs a firmware with
`modtailscale.wait_peer` (added with this feature). **Status: works on a T-Deck
(confirmed by hand after joining the tailnet); speed over the tunnel is not measured.**

## Trying it without a real server

`tools/vnctest` is an Ubuntu image running TigerVNC at 320x240 with a small LXDE
desktop: a panel along the bottom (menu, terminal, Firefox and file-manager
launchers, a window list, clock), every window maximised above the panel, and
Firefox with its interface scaled down so it fits (`FIREFOX_SCALE`, default 0.6,
raise it for bigger text).

```sh
docker build -t vtos-vnctest tools/vnctest
docker run --rm --shm-size=512m -p 5901:5901 vtos-vnctest   # password: secret
# on the T-Deck, with your Mac's Wi-Fi address (ipconfig getifaddr en0):
#   vnc 192.168.1.157:1 secret
```

`--shm-size` stops Firefox's tabs from crashing. Tapping the panel launchers
works with the trackball click (MOUSE mode).

## Aliases

`/flash/.vnc` (or `/sd/.vnc`), one per line; the password is optional:

```
# alias  host[:display | ::port]  [password]
home     192.168.1.4:1            hunter2
lab      lab.local
```

Passwords stored here are plain text on the device, like SSH profiles.

## How it works (and why it can be fast enough)

- The pixel format is requested as RGB565, big-endian: the display's own byte
  order. A Raw rectangle is read from the socket in chunks of up to 75 KB (a full frame is 2 blits) and
  handed straight to the display driver: no conversion, no framebuffer copy in RAM.
- Raw and desktop-resize are requested by default (fastest on the T-Deck). `vnc -z ...` also asks for Hextile; it cuts the bytes sent about 6.5x but the Python decode costs more than the bytes save on a fast link, so it was slower to draw. Hextile sends 16x16
  tiles as solid fills and small sub-rectangles, so flat areas such as window
  backgrounds cost a few bytes; the client composes each row of tiles in a 10 KB band buffer and blits it once (per-sub-rectangle fills were about 6,900 SPI transfers per frame and made it slow), so it needs
  no framebuffer. One full 320x240 desktop frame from the test server was
  153,664 bytes as Raw and 23,459 bytes as Hextile (6.5x less; photos compress
  far less). After the first frame servers only send what changed. A server
  that does not offer Hextile falls back to Raw.
- Wi-Fi power save is switched off while connected (and restored after) to keep
  the pointer responsive.
- The trackball is taken over through `env.pointer` (see `lib/trackball.py`),
  which receives raw movement instead of arrow keys, and is released when `vnc` ends.

## Status

Checked off-device:

- 158 host tests (`utils/tests/test_rfb.py`, `test_vnc.py`, `test_trackball.py`)
  against a scripted fake server and fake display, including every clipping case
  of the drawing code, checked pixel by pixel.
- The same code run under MicroPython 1.28 (Unix port) against a real TigerVNC
  800x600 server: password authentication, RGB565 colours (pure red, green and
  blue come out as exactly `F800`, `07E0`, `001F`), held incremental requests,
  pointer position, key events, and a scrolling viewport.

Run on a T-Deck (2026-11-09, over Wi-Fi to the `tools/vnctest` server): connect
0.2 s, VNC authentication 0.16 s (the pure-Python DES takes 93 ms), first full
320x240 frame (150 KB) in 274 ms (about 550 KB/s, roughly 3-4 full frames a
second; small updates take 3-15 ms), typed text and a full-screen redraw both
arrived, and free heap was unchanged afterwards.

The picture and colours look correct on the panel (confirmed by eye). Not yet
checked by hand on the device: rolling the trackball, the Alt+M modes, voice typing and
label, and the physical keyboard.

Not measured: behaviour with servers other than TigerVNC, and frame rate with a
busy desktop. Not supported: ZRLE/Tight (zlib) compression, copy-rect (needs a framebuffer copy), TLS/VeNCrypt/
ARD security, clipboard from the T-Deck to the server (text the server copies
is stored in the vtOS clipboard, `env.clipboard`, which Alt+V pastes; that path
is tested only with the fake server), cursor shape (servers draw the cursor into
the picture), VNC over Tailscale by name (numeric addresses work).

# VNC server (`vncd`): control the T-Deck from your Mac

```
vncd [port]     start (default 5900); `vncd stop` to stop
open vnc://<t-deck ip>:5900        # on the Mac (or TigerVNC / any RFB viewer)
```

Also reachable from `settings` > remote. No password: use it on a trusted LAN.

## Full screen vs terminal only

- **Full screen** (firmware with the ST7789 shadow framebuffer): the viewer
  shows exactly what is on the LCD, including pixel apps (`balatro`, `rfspec`,
  meshchat radar, the Quick Settings panel). `vncd` prints "full screen".
- **Terminal only**: older firmware has no way to read the panel back, so only
  the terminal text and bar are sent, as before. `vncd` prints "terminal only".

The driver cannot read pixels back over SPI, so `st7789.c` keeps a live copy
and an update snapshot in PSRAM (300 KB total). `write_spi()` follows the window
set by `set_window()` and stores the
RAMWR bytes (big-endian RGB565, as on the wire) and flags rows whose pixels change.
It is off until `vncd` calls `tft.shadow(True)` and freed on `vncd stop`.
Methods: `shadow(on)`, `shadow_dirty(buf)`, `shadow_row(y, buf)`,
`shadow_row_rgb888(y, buf, big_endian)`, `shadow_snapshot(flags)`,
`shadow_pixels(y, rows, buf, format, hextile)`, `shadow_invalidate()`.
The server offers big-endian RGB565 as its native pixel format. The driver
converts the viewer's true-color format directly and uses Hextile when the
viewer offers it. Full-screen updates are sent as one RFB message in small
bands from a stable snapshot. Changes during transmission stay pending for the
next update. Hardware scrolling
(VSCSAD) is not tracked.

For RealVNC Viewer, set the saved T-Deck connection's **Picture quality** to
**High**. Automatic quality initially selects 64 colors, making MeshChat's dark
panels black. The server refreshes all pixels when the viewer changes format.

## Input

- **Keyboard**: printable keys, arrows, Home/End/PgUp/PgDn/Insert/Delete, and
  Ctrl+letter (so Ctrl-C works), injected through `env.kvm`.
- **Mouse**: the left button acts as a finger on the touch screen
  (`GT911.remote`), so taps and drags work in the Quick Settings panel and in
  apps that read touch. The trackball and keys of apps that ignore touch are not
  mapped from the mouse.
- Timer 3 is still used (150 ms terminal, 30 ms full screen), so `vncd`,
  `webvncd` and `telnetd` cannot run together.

## Status

The host tests (`utils/tests/test_vncd.py`) cover streaming across partial sends,
snapshot handling, Hextile negotiation, pixel formats, and input mapping. The
native encoder passed 240 round-trip cases across six pixel formats, and the
snapshot implementation was checked for isolation during redraws.

On November 10, 2026, a live MeshChat demo frame using 32-bit pixels transferred
17,043 bytes with Hextile in 0.65 seconds. Its raw transfer took about 1.9 seconds
and 307,200 bytes. Chat and animated radar captures were checked. The user also
confirmed correct MeshChat UI and colors in RealVNC Viewer after selecting High
picture quality. The final snapshot firmware was then flashed and booted, and
a real MeshChat frame using little-endian RGB565 transferred 7,752 bytes in
0.66 seconds. Sustained frame rate is unmeasured.
