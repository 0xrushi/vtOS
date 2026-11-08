# ytstream: watch YouTube on the T-Deck, rendered on your SSH server

The T-Deck cannot decode YouTube video. This splits the work: your server runs
`yt-dlp` and `ffmpeg`, shrinks the video to 320x240 JPEG frames (8 fps,
about 6 KB each, roughly 50 KB/s) plus 22 kHz mono MP3 audio (40 kbps), and
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
(`yt <profile> <words>` picks another profile). Keys: space = pause,
`+` / `-` = volume, `q` or a long trackball press = quit.
To run the script from another path, put the command on one line in
`/flash/.yt`, for example `python3 /opt/yt/ytstream.py`.

The app is in the firmware (`bin/yt.py`, parser in `lib/ytproto.py`), so the
firmware must be rebuilt; it also needs `env.tft` from the updated `boot.py`.

## Protocol

The server prints marker `ff 59 54 53 54 52 4d 31`, then packets
`[type:1][length:4 little-endian][payload]`: `M` JSON (title, size, fps), `V` a
baseline JPEG, `A` MP3 bytes, `E` the end or an error message. ffmpeg runs in
real time (`-re`), so the packets arrive at playback speed and the client needs
no clock; it keeps only the newest frame. Client to server: `q` quit, `p`
pause. The remote shell is switched to raw mode with `stty raw -echo` before
the script starts so the bytes pass unchanged.

## Limits

Frame rate is about 8 fps and the picture is small; audio has no lip-sync
correction beyond the real-time pacing. The T-Deck side is untested on the
device (no device was reachable when it was written); the server and the
parser are covered by `utils/tests/test_ytproto.py`.
