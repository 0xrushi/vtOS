# HTTP file server

```
httpd [start [port]]   start (default port 8080, binds once Wi-Fi is up)
httpd stop | status | token | new-token | auth on|off | auto on|off
```

`httpd start` prints `http://<ip>:8080/`. Open that in a browser to
browse `/flash` and `/sd`, upload, download and delete, or use `curl`:

```
T=<token>; IP=<device ip>     # with auth off, drop the -H Authorization lines
curl -H "Authorization: Bearer $T" "http://$IP:8080/api/list?path=/sd"
curl -H "Authorization: Bearer $T" -T local.bin "http://$IP:8080/files/sd/dir/local.bin"
curl -H "Authorization: Bearer $T" "http://$IP:8080/files/sd/dir/local.bin" -o out.bin
curl -H "Authorization: Bearer $T" -X DELETE "http://$IP:8080/files/sd/dir/local.bin"
```

| Request | Does |
|---|---|
| `GET /` | web page |
| `GET /api/list?path=/sd` | JSON `[{"n":name,"d":isdir,"s":size}]`; `/` lists the roots |
| `GET /files/<flash\|sd>/<path>` | download |
| `PUT /files/<flash\|sd>/<path>` | upload (Content-Length required; parent folders are created) |
| `DELETE /files/<flash\|sd>/<path>` | delete a file |
| `GET /backup` | encrypted settings backup, passphrase in `X-Passphrase` ([BACKUP.md](BACKUP.md)) |

## Security

- **No password by default**: anyone on the network can read and delete everything on `/flash` and `/sd`, including the secrets in [SECRETS.md](SECRETS.md). `httpd auth on` makes every request need the token (header or `?token=`; the URL printed then includes it). `/backup` always needs its passphrase.
- The token lives in
  `/flash/.http.json` (see [SECRETS.md](SECRETS.md)); `httpd new-token` rotates it.
- Plain HTTP: use it on a trusted LAN. The server can read **every** file on
  `/flash`, so with auth on treat the token like a password. It binds `0.0.0.0`, so it should also answer on the tailnet address once
  `tailscale up` is running (`httpd start` prints that URL too). **Untested:**
  the Tailscale code so far only makes outbound connections, so check that the
  MicroLink tunnel really delivers inbound TCP to a listening socket.
- Only `/flash` and `/sd` are reachable; `..` and anything else is refused.
- Uploads go to `<name>.part` and are renamed when complete, so an aborted
  upload never leaves a half-written file under the real name.

## How it works

No hardware timer: `boot.py` calls `httpd.tick(env)` every 30 ms, which moves
data for at most 12 ms (2 KB chunks) so the UI stays responsive. One
connection at a time; others wait in the listen backlog. Idle connections are
dropped after 15 s.

## Status

Host tests only (`utils/tests/test_httpd.py`): routing, auth, path safety,
streamed upload and download over a socket pair, aborted upload cleanup.
**Not yet run on the T-Deck**; real throughput, RAM use and SD/display
interaction are unmeasured.
