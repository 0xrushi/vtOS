#!/usr/bin/env python3
"""ytstream -- render a YouTube video on this machine and stream it to a T-Deck.

Run it on the SSH server (needs python3, ffmpeg and yt-dlp on PATH):

    ytstream.py [options] URL-or-search words

The T-Deck `yt` app starts it over SSH. Output on stdout, binary:

    8-byte marker  ff 'Y' 'T' 'S' 'T' 'R' 'M' '1'
    then packets   [type:1][length:4 little-endian][payload]
        'M'  JSON  {"title":..., "w":..., "h":..., "fps":...}   (once)
        'V'  one baseline JPEG frame
        'A'  MP3 bytes (any split)
        'E'  UTF-8 text, end of stream or an error

ffmpeg runs in real time (-re), so packets arrive at playback speed and the
client needs no clock. Input on stdin (single bytes): 'q' quit, 'p' pause or
resume. The terminal should be raw (`stty raw -echo`) so bytes pass unchanged.
"""
import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading

MARKER = b"\xffYTSTRM1"
FORMAT = "18/b[height<=360][protocol^=http]/b[height<=360]/b"
# Extra yt-dlp options for your server, e.g. cookies or a player client:
#   YTSTREAM_YTDLP_ARGS="--cookies /home/me/cookies.txt"
EXTRA = shlex.split(os.environ.get("YTSTREAM_YTDLP_ARGS", ""))
MAX_FRAME = 60 * 1024

out_lock = threading.Lock()
stdout = sys.stdout.buffer


def send(kind, payload):
    packet = kind + len(payload).to_bytes(4, "little") + payload
    with out_lock:
        stdout.write(packet)
        stdout.flush()


def fail(message):
    send(b"E", message.encode("utf-8", "replace")[:300])
    sys.exit(1)


def target_of(query):
    return query if "://" in query else "ytsearch1:" + query


def resolve_title(query):
    """Return the video title (also proves yt-dlp can find the video)."""
    cmd = ["yt-dlp", "-q", "--no-warnings", "--no-playlist"] + EXTRA + [
        "-f", FORMAT, "--print", "title", target_of(query)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as err:
        fail("yt-dlp: %s" % err)
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    if result.returncode != 0 or not lines:
        fail("yt-dlp could not find that video")
    return lines[0]


def split_jpegs(stream, emit):
    """Cut an MJPEG byte stream into frames (EOI = ff d9)."""
    buf = bytearray()
    while True:
        chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            end = buf.find(b"\xff\xd9")
            if end < 0:
                break
            frame = bytes(buf[:end + 2])
            del buf[:end + 2]
            if len(frame) <= MAX_FRAME:
                emit(frame)


def pump_audio(fd):
    with os.fdopen(fd, "rb", buffering=0) as stream:
        while True:
            chunk = stream.read(2048)
            if not chunk:
                break
            send(b"A", chunk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="+")
    ap.add_argument("--w", type=int, default=320)
    ap.add_argument("--h", type=int, default=240)
    ap.add_argument("--fps", type=int, default=8)
    ap.add_argument("--fit", choices=("contain", "cover"), default="contain",
                    help="contain: letterbox; cover: crop to fill the display")
    ap.add_argument("--quality", type=int, default=9, help="ffmpeg mjpeg -q:v, 2 best .. 31 worst")
    ap.add_argument("--abr", default="40k", help="audio bitrate (mono 22.05 kHz MP3)")
    ap.add_argument("--test", action="store_true", help="synthetic source, no network")
    ap.add_argument("--src", help="local media file instead of a YouTube URL")
    ap.add_argument("--seconds", type=int, default=0, help="stop after N seconds (0 = whole video)")
    args = ap.parse_args()
    query = " ".join(args.query)

    with out_lock:
        stdout.write(MARKER)
        stdout.flush()

    downloader = None
    if args.test:
        title = "test pattern"
        inputs = ["-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=%d" % args.fps,
                  "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050"]
    elif args.src:
        title = os.path.basename(args.src)
        inputs = ["-re", "-i", args.src]
    else:
        title = resolve_title(query)
        # yt-dlp downloads (it carries the headers YouTube checks) and pipes
        # the media into ffmpeg, which paces it in real time.
        downloader = subprocess.Popen(
            ["yt-dlp", "-q", "--no-warnings", "--no-playlist"] + EXTRA + [
                "-f", FORMAT, "-o", "-", target_of(query)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        inputs = ["-re", "-i", "pipe:0"]
    limit = ["-t", str(args.seconds)] if args.seconds else []

    send(b"M", json.dumps({"title": title[:60], "w": args.w, "h": args.h,
                           "fps": args.fps}).encode())

    # Drop unneeded source frames before scaling; one encoder thread avoids
    # frame-thread buffering at the small resolutions used by the device.
    if args.fit == "cover":
        vf = ("fps=%d,scale=%d:%d:force_original_aspect_ratio=increase,"
              "crop=%d:%d,setsar=1,format=yuvj420p"
              % (args.fps, args.w, args.h, args.w, args.h))
    else:
        vf = ("fps=%d,scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuvj420p"
              % (args.fps, args.w, args.h, args.w, args.h))
    r, w = os.pipe()
    cmd = (["ffmpeg", "-loglevel", "error"] + (["-nostdin"] if downloader is None else []) + inputs +
           ["-map", "0:v:0", "-vf", vf, "-c:v", "mjpeg", "-threads:v", "1", "-q:v", str(args.quality)] + limit +
           ["-flush_packets", "1", "-f", "image2pipe", "pipe:1",
            "-map", "0:a:0" if not args.test else "1:a:0", "-ac", "1", "-ar", "22050",
            "-c:a", "libmp3lame", "-b:a", args.abr] + limit + ["-f", "mp3", "pipe:%d" % w])
    try:
        ff = subprocess.Popen(cmd, stdin=downloader.stdout if downloader else None,
                              stdout=subprocess.PIPE, pass_fds=(w,))
    except OSError as err:
        fail("ffmpeg: %s" % err)
    os.close(w)

    threading.Thread(target=pump_audio, args=(r,), daemon=True).start()

    def control():
        paused = False
        while True:
            key = sys.stdin.buffer.read(1)
            if not key or key == b"q":
                ff.terminate()
                return
            if key == b"p":
                paused = not paused
                ff.send_signal(signal.SIGSTOP if paused else signal.SIGCONT)

    threading.Thread(target=control, daemon=True).start()

    split_jpegs(ff.stdout, lambda frame: send(b"V", frame))
    ff.wait()
    if downloader:
        downloader.terminate()
    send(b"E", b"end")
    # The stdin reader thread is blocked in read(); skip interpreter shutdown.
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
