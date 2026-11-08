#!/usr/bin/env python3
"""Serve one ytstream session over TCP instead of SSH.

python3 tools/ytstream/lan_server.py --bind YOUR_LAN_IP --port 8765 \
    --w 320 --h 240 --fps 5 --fit cover 'https://www.youtube.com/watch?v=...'

All options except --bind and --port are forwarded to the adjacent ytstream.py.
The client's initial remote-command line is discarded, never executed. The
server's arguments choose the video, resolution and rate; subsequent q/p control
bytes go directly to the streamer. This is a single-client, unencrypted stream.
"""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys


def read_request(conn):
    """Consume just the initial command, leaving subsequent controls in TCP."""
    for _ in range(4096):
        byte = conn.recv(1)
        if not byte:
            raise ConnectionError("client closed before sending its command")
        if byte == b"\n":
            return
    raise ValueError("client command exceeds 4096 bytes")


def serve(bind, port, stream_args):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((bind, port))
        listener.listen(1)
        host, actual_port = listener.getsockname()
        print("Listening on %s:%d" % (host, actual_port), flush=True)
        conn, address = listener.accept()
    with conn:
        print("Connected: %s:%d" % address, flush=True)
        conn.settimeout(15)
        read_request(conn)
        conn.settimeout(None)
        script = Path(__file__).with_name("ytstream.py")
        child = subprocess.Popen([sys.executable, str(script), *stream_args],
                                 stdin=conn, stdout=conn, start_new_session=True)
        try:
            return child.wait()
        finally:
            # Stop ffmpeg and yt-dlp too if this wrapper is interrupted.
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument("--bind", default="127.0.0.1", help="IP address to listen on")
    parser.add_argument("--port", type=int, default=8765, help="TCP port (default: 8765)")
    args, stream_args = parser.parse_known_args()
    if not stream_args:
        parser.error("supply a video URL or ytstream options such as --test x")
    try:
        return serve(args.bind, args.port, stream_args)
    except (OSError, ValueError) as err:
        print("lan_server: %s" % err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
