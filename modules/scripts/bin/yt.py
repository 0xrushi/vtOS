#
# MicroPython YouTube viewer: the heavy work runs on an SSH server
# License: MIT
#
#   yt [profile] <youtube url or search words>
#
# Connects to a profile from /flash/.ssh/config (like `ssh`), runs
# tools/ytstream/ytstream.py there, and plays the stream: 320x240 JPEG frames
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
_PRIME_BYTES = 6000
_READ_CHUNK = 2048


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


def play(env, client, query):
    import st7789  # SLOW mode: the display buffer is only 10 KB
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
    last_data = time.ticks_ms()

    cmd = "stty raw -echo; exec %s --w 320 --h 240 --fps 8 %s\n" % (
        _remote_command(), _shell_quote(query))
    client.write(cmd.encode())

    try:
        while ended is None:
            ready, _, _ = select.select([client], [], [], 0)
            if ready:
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
                        frame = payload          # keep only the newest frame
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
                x = (320 - meta.get("w", 320)) // 2
                y = (240 - meta.get("h", 240)) // 2
                try:
                    tft.jpg(frame, x, y, st7789.SLOW)
                except (OSError, RuntimeError):
                    pass                          # a torn frame: skip it
                frame = None
            elif not ready:
                time.sleep_ms(5)

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
    return ended


def main(env, args):
    if not args:
        print("Usage: yt [profile] <youtube url or words>")
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
        result = play(env, session.client, query)
    finally:
        session.close()
    if result and result not in ("stopped", "end"):
        print("yt: " + result)
