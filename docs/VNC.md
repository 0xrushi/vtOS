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
| **Mic key** | changes what the trackball does (below); a small label shows the new mode |
| Keyboard | types into the remote machine: letters, digits, symbols, Enter, Backspace |
| Hold the click | quit |

Mic-key modes, in order:

| Label | Trackball | Click |
|---|---|---|
| `MOUSE` | moves the pointer | left button |
| `RIGHT` | moves the pointer | right button |
| `SCROLL` | up/down/left/right = mouse wheel | left button |
| `KEYS` | arrow keys | Escape |

The keyboard has no Ctrl or Esc key and does not report Alt to apps, and the
click is press-and-release only. So there is no dragging and no key chords, and
Escape lives in `KEYS` mode.

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
`modtailscale.wait_peer` (added with this feature). **Status: the first attempt on a
T-Deck failed to connect and the cause is not yet known; treat this as unverified.**

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
  order. A Raw rectangle is read from the socket in chunks of up to 16 KB and
  handed straight to the display driver: no conversion, no framebuffer copy in RAM.
- Only Raw rectangles and desktop-resize are requested, so servers send
  uncompressed pixels. A full 320x240 frame is 150 KB; after the first frame
  servers only send what changed.
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
checked by hand on the device: rolling the trackball, the Mic-key modes and
label, and the physical keyboard.

Not measured: behaviour with servers other than TigerVNC, and frame rate with a
busy desktop. Not supported: Hextile/ZRLE/Tight compression, copy-rect, TLS/VeNCrypt/
ARD security, clipboard from the T-Deck to the server (text the server copies
is stored in the vtOS clipboard, `env.clipboard`, which Alt+V pastes; that path
is tested only with the fake server), cursor shape (servers draw the cursor into
the picture), VNC over Tailscale by name (numeric addresses work).
