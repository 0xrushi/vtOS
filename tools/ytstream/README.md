# ytstream: watch YouTube on the T-Deck, rendered on your SSH server

The T-Deck cannot decode YouTube video. This splits the work: your server runs
`yt-dlp` and `ffmpeg`, shrinks the video to small JPEG frames (256x144 with a
30 fps target by default) plus 22 kHz mono MP3 audio (40 kbps), and
sends them over SSH. The T-Deck draws the newest frame and plays the audio.

## Server setup

1. Install `python3`, `ffmpeg` (with libmp3lame) and `yt-dlp` and keep yt-dlp
   up to date (`yt-dlp -U`).
2. Copy the script: `scp tools/ytstream/ytstream.py you@server:~/`
3. If YouTube refuses the download (403, "format not available"), give yt-dlp
   what it needs through an environment variable, for example cookies:
   `export YTSTREAM_YTDLP_ARGS="--cookies /home/you/cookies.txt"`
   (set it in the server account's profile). Newer yt-dlp builds may also need
   a JavaScript runtime such as `deno` or `node`.

Try it from the server shell. These need no network:
`python3 ytstream.py --test --seconds 3 x | python3 check_stream.py` and
`python3 ytstream.py --src some-video.mp4 x | python3 check_stream.py`.

## On the T-Deck

Add the server to `/flash/.ssh/config` (same file the `ssh` command uses):

    Host yt
      HostName my-server.example.com
      User me
      Password secret     # optional; asked at the prompt if missing

Then: `yt big buck bunny` or `yt https://www.youtube.com/watch?v=...`
(`yt <profile> <words>` picks another profile). Pick a size before the words:

| Option | Picture | Target rate |
| --- | --- | --- |
| `--smooth` (default) | 256x144 | 30 fps |
| `--full` | 320x240 (cropped to fill) | 30 fps |
| `--fast` | 176x96 | 60 fps |
| `--tiny` | 128x80 | 60 fps |

These are requested stream rates. Actual display rate depends on video
complexity, Wi-Fi, SSH and audio load; it is printed on exit along with draw
time and skipped frames. A 30 fps stream of 3 KB JPEGs needs about 90 KB/s
before audio and SSH overhead; 60 fps doubles the video bandwidth.
`--full` fills the entire display without stretching: widescreen video is
cropped at the sides. Other presets show the complete picture. On legacy
firmware, full-screen playback requests 5 fps to leave time for audio.
Keys: space = pause,
`+` / `-` = volume, `q` or a long trackball press = quit.
To run the script from another path, put the command on one line in
`/flash/.yt`, for example `python3 /opt/yt/ytstream.py`.

Rebuild the firmware for both the app (`bin/yt.py`, `lib/ytproto.py`) and the
native JPEG/display optimizations. Copy the updated `ytstream.py` to the SSH
server too. Updating only Python files leaves the old decoder in place; the
app detects this and uses conservative legacy rates. It also needs `env.tft`
from the updated `boot.py`.

## Direct LAN server (no SSH login)

`lan_server.py` keeps the TCP bridge used for live device playback in the
codebase. On your Mac or Linux server, run:

```sh
python3 tools/ytstream/lan_server.py --bind YOUR_SERVER_LAN_IP --port 8765 \
  --w 320 --h 240 --fps 5 --fit cover \
  'https://www.youtube.com/watch?v=-84Etqh78Xc'
```

At the T-Deck's MicroPython prompt, using the existing boot environment:

```python
import socket
from bin import yt
client = socket.socket()
client.connect(('YOUR_SERVER_LAN_IP', 8765))
try:
    yt.play(env, client, 'LAN video', 'full')
finally:
    client.close()
```

If the viewer was installed manually as `/flash/ytapp.py` on older firmware,
use `import ytapp as yt` instead. The server arguments select the video,
dimensions and FPS; match them to the client preset. The example uses the
5 fps legacy full-screen setting; rebuilt firmware can request a higher rate.
The connection is unencrypted, accepts one client, and exits when playback
ends. Bind to your private LAN address. Quit/pause/volume keys work as usual.

## Rendering algorithm

The `st7789.STRIP` JPEG mode assembles a full MCU row in the existing 10 KB
display buffer and transfers it in one SPI operation. At 320 pixels wide,
4:2:0 video uses 20 times fewer display windows and pixel transfers than
`SLOW` mode. It does not allocate a full-screen framebuffer.

The decoder checks canonical Huffman code ranges instead of scanning every
symbol of each code length. For unscaled video, it converts YCbCr directly
to RGB565 and reuses chroma calculations for pairs of pixels, avoiding the
intermediate RGB888 image. Existing scaled decoding remains available.

The client drains a bounded burst of incoming packets before drawing only
the latest complete frame, while retaining audio packets. This reduces
backlog without letting a busy stream monopolize the keyboard loop. The
server filters frame rate before scaling and avoids encoder frame-thread
buffering.

## Protocol

The server prints marker `ff 59 54 53 54 52 4d 31`, then packets
`[type:1][length:4 little-endian][payload]`: `M` JSON (title, size, fps), `V` a
baseline JPEG, `A` MP3 bytes, `E` the end or an error message. ffmpeg runs in
real time (`-re`), so the packets arrive at playback speed and the client needs
no clock; it keeps only the newest frame. Client to server: `q` quit, `p`
pause. The remote shell is switched to raw mode with `stty raw -echo` before
the script starts so the bytes pass unchanged.

## Limits

30/60 fps are targets, not measured device guarantees. The optimized firmware
has been compiled, but end-to-end playback with the new decoder still needs
an on-device measurement. YouTube and 320x240 cropped playback have been
verified over the LAN bridge using the legacy decoder. Audio has no lip-sync
correction beyond real-time pacing. A 60 fps target can repeat source frames
when a video is only 30 fps.

`utils/tests/test_ytproto.py` checks 8/30/60 fps server streams, protocol
parsing and client frame dropping. `utils/tests/test_ytjpeg.py` compiles the
native decoder and checks pixel output against the original implementation,
as well as strip placement, borders and transfer counts. A host CPU-only
benchmark can be built with the command in `bench_decode.c`; its timing
excludes the device, SPI and network.
