#!/usr/bin/env python3
"""Join the T-Deck to Tailscale with an auth key taken from the macOS clipboard.

Typing a 60-character key on the T-Deck keyboard is painful. Copy the key
(Cmd-C), then run this from a terminal on the Mac with the T-Deck plugged in:

    python3 tools/tailscale-join-from-clipboard.py [hostname]

The key is read from the clipboard and sent over USB to modtailscale.configure();
it is never printed, put on a command line (so not in `ps` or shell history) or
written to disk, and the clipboard is cleared once the device has joined.

Uses mpremote's library in "resume" mode: no soft reset of the board, only an
interrupt of whatever is running (e.g. a stuck `tailscale up`).
Needs: pip install mpremote
"""

import glob
import subprocess
import sys
import time

from mpremote.transport_serial import SerialTransport

JOIN_WAIT_S = 70


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "vtos-tdeck"
    key = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    if not key.startswith("tskey-auth-"):
        sys.exit("The clipboard does not hold a tskey-auth- key. Copy the key first.")
    ports = glob.glob("/dev/cu.usbmodem*")
    if len(ports) != 1:
        sys.exit("Expected exactly one /dev/cu.usbmodem* port, found: %s" % ports)

    board = SerialTransport(ports[0], baudrate=115200)
    try:
        board.enter_raw_repl(soft_reset=False)

        def run(code):
            return board.exec(code).decode(errors="replace").strip()

        # Stop any half-started tunnel (a stuck `tailscale up`), then store the key.
        print(run("import modtailscale as m\n"
                  "try:\n m.down()\nexcept OSError as e:\n print('down:', e)"))
        print(run("m.configure(%r, %r)\nm.up()\nprint('joining...')" % (key, name)))

        deadline = time.time() + JOIN_WAIT_S
        while time.time() < deadline:
            status = run("s = m.status()\nprint(s['state'], s['ip'])")
            print(status)
            if status.startswith("connected"):
                subprocess.run(["pbcopy"], input=b"")          # clear the key
                print("Joined. Clipboard cleared. Now run on the T-Deck: "
                      "vnc <tailnet ip>:1 <password>")
                return
            time.sleep(3)
        print("Not connected after %d s; the key may be expired or single-use "
              "and already used (try a fresh reusable key)." % JOIN_WAIT_S)
    finally:
        try:
            board.exit_raw_repl()
        finally:
            board.close()


if __name__ == "__main__":
    main()
