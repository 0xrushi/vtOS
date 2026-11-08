#!/usr/bin/env python3
"""Expose T-Deck PCM arriving on SSH stdin as a PipeWire/Pulse microphone.

Install on the SSH host at ~/.local/share/vtos/tdeck_mic_bridge.py.
No network listener, credentials, root privileges or default-device changes.
"""

import argparse
import os
import shutil
import subprocess
import sys

SINK = "vtos_tdeck"
SOURCE = "vtos_tdeck_mic"
READY = b"VTOS_MIC_READY 16000 1\n"


def pactl(*args):
    return subprocess.check_output(["pactl", *args], text=True).strip()


def device_names(kind):
    return {line.split("\t")[1] for line in pactl("list", "short", kind).splitlines()
            if "\t" in line}


def create_microphone():
    """Return IDs of modules created here, leaving existing devices alone."""
    owned = []
    try:
        if SINK not in device_names("sinks"):
            owned.append(pactl(
                "load-module", "module-null-sink", "sink_name=" + SINK,
                "rate=16000", "channels=1", "channel_map=mono",
                "sink_properties=device.description=TDeck_Audio_Transport"))
        if SOURCE not in device_names("sources"):
            owned.append(pactl(
                "load-module", "module-remap-source", "master=" + SINK + ".monitor",
                "source_name=" + SOURCE, "channels=1", "channel_map=mono", "remix=no",
                "source_properties=device.description=TDeck_Microphone"))
        return owned
    except BaseException:
        remove_microphone(owned)
        raise


def remove_microphone(owned):
    for module_id in reversed(owned):
        subprocess.run(["pactl", "unload-module", str(module_id)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def relay(source, destination):
    """Copy raw signed 16-bit little-endian mono PCM without framing it."""
    tail = b""
    while True:
        data = source.read(2048)
        if not data:
            if tail:
                raise ValueError("Truncated 16-bit microphone sample")
            return
        data = tail + data
        count = len(data) & ~1
        tail = data[count:]
        if count:
            view = memoryview(data)[:count]
            while view:
                written = destination.write(view)
                if written is None or written <= 0:
                    raise OSError("Audio playback pipe stopped")
                view = view[written:]
            destination.flush()


def run_stdio():
    if not shutil.which("pactl") or not shutil.which("pacat"):
        raise RuntimeError("Install pactl and pacat (PulseAudio client tools) on this host")
    owned = create_microphone()
    player = None
    try:
        player = subprocess.Popen([
            "pacat", "--playback", "--raw", "--device=" + SINK,
            "--format=s16le", "--rate=16000", "--channels=1",
            "--latency-msec=60", "--process-time-msec=20",
            "--client-name=vtOS", "--stream-name=TDeck_Microphone",
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        sys.stdout.buffer.write(READY)
        sys.stdout.buffer.flush()
        relay(sys.stdin.buffer, player.stdin)
        player.stdin.close()
        if player.wait(timeout=5):
            raise RuntimeError("PipeWire microphone transport failed")
    finally:
        if player is not None and player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=3)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait()
        remove_microphone(owned)


def launch_claude(args):
    if SOURCE not in device_names("sources"):
        raise RuntimeError("Connect from vtOS with 'ssh --mic' first")
    env = os.environ.copy()
    env["PULSE_SOURCE"] = SOURCE
    # The input device is supplied by the encrypted audio session. Avoid
    # remote-terminal heuristics that otherwise disable local voice capture.
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        env.pop(name, None)
    command = ["claude", "--settings", '{"voice":{"enabled":true,"mode":"tap"}}', *args]
    os.execvpe(command[0], command, env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdio", action="store_true", help="receive PCM on SSH stdin")
    parser.add_argument("--claude", action="store_true", help="launch Claude with the forwarded mic")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    try:
        if options.stdio:
            run_stdio()
        elif options.claude:
            launch_claude(options.args[1:] if options.args[:1] == ["--"] else options.args)
        else:
            parser.print_help()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print("VTOS_MIC_ERROR:", error, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
