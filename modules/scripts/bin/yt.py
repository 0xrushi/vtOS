#
# MicroPython YouTube viewer: the heavy work runs on an SSH server
# License: MIT
#
#   yt [profile] <youtube url or search words>
#
# Connects to a profile from /flash/.ssh/config (like `ssh`), runs
# tools/ytstream/ytstream.py there, and plays the stream: small JPEG frames
# drawn straight to the display, MP3 audio through the I2S player.
# The remote command can be changed in /flash/.yt (one line, default
# "python3 ~/ytstream.py").
#
# Keys: q or long trackball press = quit, space = pause, + / - = volume.
#

import sys
import time
import select
import json

from ytproto import Demux, AudioPipe

_DEFAULT_REMOTE = "python3 ~/ytstream.py"

# Requested stream rates, not guaranteed display rates. STRIP batches a whole
# JPEG MCU row into one SPI transfer; the decoder also uses canonical Huffman
# ranges and direct RGB565 output. Smaller frames leave more time for SSH/audio.
PRESETS = {
    "full": (320, 240, 30),
    "smooth": (256, 144, 30),
    "fast": (176, 96, 60),
    "tiny": (128, 80, 60),
}
_LEGACY_FPS = {"full": 5, "smooth": 10, "fast": 20, "tiny": 20}
_DEFAULT_PRESET = "smooth"


def parse_options(args):
    """Split leading --smooth/--fast/--tiny/--full off the arguments."""
    preset = _DEFAULT_PRESET
    rest = []
    for arg in args:
        if not rest and arg.startswith("--") and arg[2:] in PRESETS:
            preset = arg[2:]
        else:
            rest.append(arg)
    return preset, rest
_PRIME_BYTES = 6000
_READ_CHUNK = 4096
_DRAIN_MS = 8
_DRAIN_READS = 8


def _remote_command():
    try:
        with open("/flash/.yt", "r") as f:
            line = f.readline().strip()
            if line:
                return line
    except OSError:
        pass
    return _DEFAULT_REMOTE


def _shell_quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def _pick_profile(ssh, args):
    profiles = ssh.load_profiles()
    if not profiles:
        return None, args
    by_alias = {p["alias"]: p for p in profiles}
    if args and args[0] in by_alias:
        return by_alias[args[0]], args[1:]
    if "yt" in by_alias:
        return by_alias["yt"], args
    return profiles[0], args


def _status(env, text):
    sys.stdout.write("\r\x1b[K" + text + "\r\n")


def play(env, client, query, preset=_DEFAULT_PRESET):
    import st7789
    jpg_mode = getattr(st7789, "STRIP", st7789.SLOW)
    tft = env.tft
    demux = Demux()
    pipe = AudioPipe()
    inbuf = bytearray(_READ_CHUNK)
    view = memoryview(inbuf)
    frame = None
    meta = {"w": 320, "h": 240}
    started = False   # first frame seen: the display is ours
    playing = False
    paused = False
    ended = None
    drawn = 0
    skipped = 0
    draw_ms = 0
    first_draw = None
    last_draw = None
    last_data = time.ticks_ms()

    want_w, want_h, want_fps = PRESETS[preset]
    if not hasattr(st7789, "STRIP"):
        want_fps = _LEGACY_FPS[preset]
        _status(env, "rebuild firmware for the faster JPEG decoder; using legacy rate")
    fit = " --fit cover" if preset == "full" else ""
    cmd = "stty raw -echo; exec %s --w %d --h %d --fps %d%s %s\n" % (
        _remote_command(), want_w, want_h, want_fps, fit, _shell_quote(query))
    client.write(cmd.encode())

    try:
        while ended is None:
            ready, _, _ = select.select([client], [], [], 0)
            if ready:
                # Drain a bounded burst before drawing. Reading one small chunk
                # per draw used to leave stale video AND audio waiting in SSH.
                drain_start = time.ticks_ms()
                for _ in range(_DRAIN_READS):
                    try:
                        n = client.readinto(inbuf)
                    except OSError:
                        n = 0
                    if not n:
                        ended = "connection closed"
                        break
                    last_data = time.ticks_ms()
                    for kind, payload in demux.feed(view[:n]):
                        if kind == b"V":
                            if frame is not None:
                                skipped += 1
                            frame = payload      # keep only the newest frame
                        elif kind == b"A":
                            pipe.push(payload)
                        elif kind == b"M":
                            try:
                                meta = json.loads(payload)
                            except ValueError:
                                pass
                            _status(env, "playing: " + str(meta.get("title", "")))
                        elif kind == b"E":
                            ended = payload.decode()
                    if ended is not None or time.ticks_diff(time.ticks_ms(), drain_start) >= _DRAIN_MS:
                        break
                    more, _, _ = select.select([client], [], [], 0)
                    if not more:
                        break
            elif time.ticks_diff(time.ticks_ms(), last_data) > 20000:
                ended = "no data for 20 s"

            if not playing and pipe.size >= _PRIME_BYTES and env.audio is not None:
                env.audio.volume(env.volume)
                env.audio.play(pipe)
                playing = True

            if frame is not None and not paused:
                if not started:
                    started = True
                    env.sd_busy = True           # stop terminal + status bar redraws
                    tft.fill(0)
                x = (320 - meta.get("w", 320)) // 2
                y = (240 - meta.get("h", 240)) // 2
                try:
                    t0 = time.ticks_ms()
                    tft.jpg(frame, x, y, jpg_mode)
                    last_draw = time.ticks_ms()
                    if first_draw is None:
                        first_draw = last_draw
                    draw_ms += time.ticks_diff(last_draw, t0)
                    drawn += 1
                except (OSError, RuntimeError):
                    skipped += 1                  # a torn frame: skip it
                frame = None
            elif not ready:
                time.sleep_ms(1)

            keys, _, _ = select.select([sys.stdin], [], [], 0)
            if keys:
                ch = sys.stdin.read(1)
                if ch in ("q", "Q"):
                    ended = "stopped"
                elif ch == " ":
                    paused = not paused
                    client.write(b"p")
                    if playing:
                        if paused:
                            env.audio.pause()
                        else:
                            env.audio.resume()
                elif ch in ("+", "="):
                    env.volume = min(100, env.volume + 10)
                    env.audio.volume(env.volume)
                elif ch == "-":
                    env.volume = max(0, env.volume - 10)
                    env.audio.volume(env.volume)
    except KeyboardInterrupt:
        ended = "stopped"
    except Exception as err:
        # Show what went wrong once the terminal is back (see finally).
        ended = "error: %s: %s" % (type(err).__name__, err)
    finally:
        try:
            client.write(b"q")
        except OSError:
            pass
        if playing:
            try:
                env.audio.stop()
            except Exception:
                pass
        env.sd_busy = False
        sys.stdout.write("\x1b[2J\x1b[H")
    elapsed = time.ticks_diff(last_draw, first_draw) if first_draw is not None else 0
    # Count frame intervals, so a one-frame clip cannot report an inflated FPS.
    fps10 = (drawn - 1) * 10000 // elapsed if drawn > 1 and elapsed > 0 else 0
    sys.stdout.write("yt: %d frames drawn, %d skipped, %d.%d fps, avg %d ms to draw (%s)\r\n" % (
        drawn, skipped, fps10 // 10, fps10 % 10, draw_ms // drawn if drawn else 0, preset))
    return ended


def main(env, args):
    preset, args = parse_options(list(args))
    if not args:
        print("Usage: yt [--smooth|--fast|--tiny|--full] [profile] <url or words>")
        print("       targets: smooth 256x144 30fps (default), fast 176x96 60fps,")
        print("       tiny 128x80 60fps, full 320x240 30fps (cropped to fill)")
        print("       needs an SSH profile + tools/ytstream/ytstream.py there")
        return
    if getattr(env, "tft", None) is None:
        print("yt: this firmware does not expose the display (env.tft)")
        return
    from bin import ssh
    profile, rest = _pick_profile(ssh, list(args))
    if profile is None or not rest:
        print("yt: need an SSH profile in /flash/.ssh/config and a video")
        return
    password = profile["password"]
    if password is None:
        password = ssh._read_password("%s password: " % ssh.describe(profile))
        if password is None:
            return
    query = " ".join(rest)
    try:
        session = ssh.SSHSession(profile["host"], profile["port"], profile["user"], password)
    except OSError as e:
        print("yt: " + str(e))
        return
    result = None
    try:
        result = play(env, session.client, query, preset)
    finally:
        session.close()
    if result and result not in ("stopped", "end"):
        print("yt: " + result)
