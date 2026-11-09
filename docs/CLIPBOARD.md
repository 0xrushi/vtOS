# Phone clipboard share

Share text from an Android phone to the T-Deck. The phone's Share sheet gets a
**Send to T-Deck** entry; the text lands in the device clipboard, and a
clipboard icon in the top status bar shows the service is running.

## On the T-Deck

```
nm                 # join Wi-Fi (or `tailscale up`)
clip start         # listen on port 8787
clip pair          # 6-digit code to pair the phone app (2 min, 3 tries)
clip token         # prints the address and the long token (manual setup)
clip auto on       # optional: start at every boot
```

| Command | Effect |
| :--- | :--- |
| `clip` | Print the clipboard |
| `clip start [port]` / `clip stop` | Start / stop the service |
| `clip pair` | Open a 2-minute pairing window and show a 6-digit code |
| `clip token [new]` | Show the address and token; `new` regenerates it |
| `clip paste` | Type the clipboard at the cursor |
| `clip clear` | Empty the clipboard |
| `clip auto on\|off` | Start the service at boot |

**Alt+V** pastes from anywhere (shell, vi, apps). Pasting is typed in at about
250 characters per second and strips control characters, so a clip cannot
carry escape sequences. An app that is not reading the keyboard while a paste
is in flight can lose characters.

Status bar icon: grey = off or waiting for Wi-Fi, green = listening, yellow for
three seconds after a clip arrives (a toast also appears). The icon is dropped
on fonts so wide that the bar has no room for it.

Config and token live in `/flash/.clip.json`.

## On the phone

Build and install `tools/clipshare-android` (see its README). Run `clip pair`
on the T-Deck, open **T-Deck Clip**, type the 6-digit code and press **Find
T-Deck & pair**: the app finds the device by UDP broadcast on the LAN and
fetches the token itself. Off the LAN (Tailscale), type the address first and
pair the same way, or enter address and token by hand. After that, in any app:
Share → **Send to T-Deck**.

## Protocol

HTTP/1.1, one request per connection, `Authorization: Bearer <token>` on every
request.

| Request | Result |
| :--- | :--- |
| UDP `VTOS?` to the port | `{"name":"vtos","port":8787,"pairing":false}` (discovery; default port only) |
| `POST /pair` (body = code, no auth) | `200 {"token":...}`; `403` when no pairing window; `401` wrong code, window closes after 3 wrong tries |
| `GET /ping` | `200 {"name":"vtos","clip":<chars>}` |
| `POST /clip` (UTF-8 body, `Content-Length` required, max 4096 bytes) | `200 {"ok":true,"len":<chars>}` |

Errors: `400` malformed or non-UTF-8, `401` bad token, `404`, `405`, `408`
(request not finished in 3 s), `413` over 4096 bytes, `431` headers too large.

```
curl -H "Authorization: Bearer $TOKEN" -d 'hello' http://<ip>:8787/clip
```

## Security

The transport is plain HTTP and the token is the only protection, so anyone on
the same network who can sniff traffic can read the token. On a shared Wi-Fi
use the Tailscale address (`tailscale up`) instead of the LAN address. Run
`clip token new` to rotate the token.

## Implementation notes

`sys/clipd.py` has no hardware timer of its own (Timer 3 is the single slot
telnetd/vncd/webvncd share); `boot.py` calls `clipd.tick(env)` from its 30 ms
loop and the server does a zero-timeout `select`. It binds lazily once Wi-Fi is
up, so autostart works before the connection is made. The clipboard icon is a
hand-drawn glyph (U+E0C1) appended to `fonts/siji_mpy_statusbar_12.py`.
