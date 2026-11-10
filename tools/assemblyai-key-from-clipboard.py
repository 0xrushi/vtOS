#!/usr/bin/env python3
"""Store an AssemblyAI API key on the T-Deck, taking it from the macOS clipboard.

Copy the key (Cmd-C), quit anything running on the T-Deck so it shows the
vtOS `$` prompt (in `vnc`, long-press the trackball), close any other serial
console, then run on the Mac:

    python3 tools/assemblyai-key-from-clipboard.py

The key is read from the clipboard and written into /flash/.agent (the same
file `agent set assemblyai_key ...` uses) over USB. It is never printed, put on
a command line (so not in `ps` or shell history) or written to the Mac's disk,
and the clipboard is cleared afterwards.

Uses mpremote's library without a soft reset of the board.
Needs: pip install mpremote
"""

import glob
import re
import subprocess
import sys

from mpremote.transport_serial import SerialTransport


def main():
    key = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", key):
        sys.exit("The clipboard does not hold a 32-character AssemblyAI key. Copy the key first.")
    ports = glob.glob("/dev/cu.usbmodem*")
    if len(ports) != 1:
        sys.exit("Expected exactly one /dev/cu.usbmodem* port, found: %s" % ports)

    board = SerialTransport(ports[0], baudrate=115200)
    try:
        board.enter_raw_repl(soft_reset=False)
        code = (
            "import json\n"
            "try:\n"
            " cfg = json.loads(open('/flash/.agent').read())\n"
            "except (OSError, ValueError):\n"
            " cfg = {}\n"
            "cfg['assemblyai_key'] = %r\n"
            "open('/flash/.agent', 'w').write(json.dumps(cfg))\n"
            "print('saved, %%d chars' %% len(cfg['assemblyai_key']))\n" % key
        )
        print(board.exec(code).decode(errors="replace").strip())
        subprocess.run(["pbcopy"], input=b"")            # clear the key
        print("Clipboard cleared. Now run `vnc` on the T-Deck and press the Mic key.")
    finally:
        try:
            board.exit_raw_repl()
        finally:
            board.close()


if __name__ == "__main__":
    main()
