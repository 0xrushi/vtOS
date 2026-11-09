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
    "clear": (256, 144, 8),
    "fluid": (160, 90, 15),
    "queued": (96, 72, 30),
    "full": (320, 240, 30),
    "smooth": (256, 144, 30),
    "fast": (176, 96, 60),
    "tiny": (128, 80, 60),
}
_LEGACY_FPS = {"clear": 5, "fluid": 10, "queued": 20, "full": 5, "smooth": 10, "fast": 20, "tiny": 20}
_DEFAULT_PRESET = "smooth"
_PRIME_BYTES = 6000
_READ_CHUNK = 4096
_DRAIN_MS = 8
_DRAIN_READS = 8
_QUEUE_FRAMES = 18
_QUEUE_PRIME_FRAMES = 12
_QUEUE_PRIME_MS = 500
_QUEUE_AUDIO_BYTES = 1024


def parse_options(args):
    """Split leading video-preset options off the arguments."""
    preset = _DEFAULT_PRESET
    rest = []
    for arg in args:
        if not rest and arg.startswith("--") and arg[2:] in PRESETS:
            preset = arg[2:]
        else:
            rest.append(arg)
    return preset, rest


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
    frames = []
    buffered = preset == "queued"
    fill = buffered and hasattr(st7789, "FILL")
    if fill:
        jpg_mode = st7789.FILL
    capacity = _QUEUE_FRAMES if buffered else 1
    prime_start = None
    next_draw = None
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
    if buffered and not fill:
        want_w = 128
    if not hasattr(st7789, "STRIP"):
        want_fps = _LEGACY_FPS[preset]
        _status(env, "rebuild firmware for the faster JPEG decoder; using legacy rate")
    fit = " --fit cover" if preset == "full" or fill else ""
    quality = {"clear": " --quality 3", "fluid": " --quality 9",
               "queued": " --quality 12"}.get(preset, "")
    frame_ms, frame_fraction = divmod(1000, want_fps)
    phase = 0
    cmd = "stty raw -echo; exec %s --w %d --h %d --fps %d%s%s %s\n" % (
        _remote_command(), want_w, want_h, want_fps, fit, quality, _shell_quote(query))

    wifi = None
    try:
        if buffered:
            try:
                import network
                sta = network.WLAN(network.STA_IF)
                if sta.isconnected():
                    previous_pm = sta.config("pm")
                    if previous_pm != sta.PM_NONE:
                        wifi = sta
                        sta.config(pm=sta.PM_NONE)
            except (ImportError, AttributeError, OSError, ValueError):
                pass
        client.write(cmd.encode())
        while ended is None or (buffered and ended == "end" and frames and not paused):
            ready = ended is None and select.select([client], [], [], 0)[0]
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
                            if len(frames) == capacity:
                                frames.pop(0)
                                skipped += 1
                            frames.append(payload)
                            if buffered and prime_start is None:
                                prime_start = last_data
                        elif kind == b"A" and env.audio is not None:
                            pipe.push(payload)
                        elif kind == b"M":
                            try:
                                meta = json.loads(payload)
                            except ValueError:
                                pass
                            _status(env, "playing: " + str(meta.get("title", "")))
                        elif kind == b"E":
                            ended = payload.decode()
                        del payload
                    if ended is not None or time.ticks_diff(time.ticks_ms(), drain_start) >= _DRAIN_MS:
                        break
                    more, _, _ = select.select([client], [], [], 0)
                    if not more:
                        break
            elif ended is None and not paused and time.ticks_diff(time.ticks_ms(), last_data) > 20000:
                ended = "no data for 20 s"

            can_draw = bool(frames) and not paused
            audio_bytes = _PRIME_BYTES
            if buffered:
                now = time.ticks_ms()
                primed = started or (prime_start is not None and (
                    ended is not None or time.ticks_diff(now, prime_start) >= _QUEUE_PRIME_MS or
                    (len(frames) >= _QUEUE_PRIME_FRAMES and
                     (env.audio is None or pipe.size >= _QUEUE_AUDIO_BYTES))))
                can_draw = can_draw and primed and (
                    next_draw is None or time.ticks_diff(now, next_draw) >= 0)
                audio_bytes = _QUEUE_AUDIO_BYTES if primed and not paused else 0
            if not playing and audio_bytes and pipe.size >= audio_bytes and env.audio is not None:
                env.audio.volume(env.volume)
                env.audio.play(pipe)
                playing = True

            if can_draw:
                if not started:
                    started = True
                    env.sd_busy = True           # stop terminal + status bar redraws
                    tft.fill(0)
                x = 0 if fill else (320 - meta.get("w", 320)) // 2
                y = 0 if fill else (240 - meta.get("h", 240)) // 2
                try:
                    t0 = time.ticks_ms()
                    if buffered:
                        if next_draw is None or time.ticks_diff(t0, next_draw) >= frame_ms:
                            next_draw = t0
                            phase = 0
                        extra_ms, phase = divmod(phase + frame_fraction, want_fps)
                        next_draw = time.ticks_add(next_draw, frame_ms + extra_ms)
                    tft.jpg(frames.pop(0), x, y, jpg_mode)
                    last_draw = time.ticks_ms()
                    if first_draw is None:
                        first_draw = last_draw
                    draw_ms += time.ticks_diff(last_draw, t0)
                    drawn += 1
                except (OSError, RuntimeError):
                    skipped += 1                  # a torn frame: skip it
            elif not ready:
                time.sleep_ms(1)

            keys, _, _ = select.select([sys.stdin], [], [], 0)
            if keys:
                ch = sys.stdin.read(1)
                if ch in ("q", "Q"):
                    ended = "stopped"
                elif ch == " ":
                    paused = not paused
                    if not paused:
                        last_data = time.ticks_ms()
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
        if wifi is not None:
            try:
                wifi.config(pm=previous_pm)
            except OSError:
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
    explicit_preset = bool(args and args[0].startswith("--") and args[0][2:] in PRESETS)
    preset, args = parse_options(list(args))
    if not args:
        print("Usage: yt [--clear|--fluid|--queued|--smooth|--fast|--tiny|--full] [profile] <url or words>")
        print("       clear: sharper 256x144 8fps (default over Tailscale)")
        print("       fluid: smaller 160x90 15fps frames for smoother playback")
        print("       queued: fills the display, 30fps target, 18-frame RAM queue")
        print("       targets: smooth 256x144 30fps (LAN default), fast 176x96 60fps,")
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
    if not explicit_preset and ssh.is_tailnet_host(profile["host"]):
        preset = "clear"
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
