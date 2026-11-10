---
name: tdeck-serial
description: Use the Mac's USB serial console to run vtOS commands on a connected LilyGO T-Deck. Use when asked to control the T-Deck from the Mac, open mpremote, exit a running VNC client or other foreground vtOS app, or start VNC from the vtOS shell.
---

# T-Deck USB console

For a how-to request, show the command and relevant keystrokes. For a request to act, open an interactive terminal session on the Mac with a PTY and keep its stdin available.

```sh
uvx mpremote connect /dev/cu.usbmodem101 repl
```

The port above is the one observed on this Mac. If it is missing, run `uvx mpremote devs` and use the T-Deck's current USB port. If the port is busy, check for an existing serial session.

- To stop VNC or another foreground app when requested, send **Ctrl+C** once (byte `0x03`) through the serial session. Wait for the vtOS `$` prompt. The connection banner alone only confirms that the serial console is open.
- At the vtOS `$` prompt, send the requested command followed by Enter (carriage return, `\r`). If the prompt is idle but not visible after attaching, press Enter to redraw it. If you see MicroPython's `>>>` prompt, enter `sh` to return to vtOS.
- For VNC, send `vnc <host>:<display>` at the vtOS prompt and respond to `VNC password:` if prompted. Keep passwords out of skill files and status messages. A line like `vnc: <desktop name> (320x240)` confirms the remote desktop opened.
- When finished, send **Ctrl+]** (byte `0x1d`) to close mpremote and release the Mac's serial port. This leaves the current device app running.

Use `repl` for the live console. Commands such as `mpremote exec` enter raw MicroPython REPL and can reset the device.

## Claude Code Bash

If Bash cannot write to a running process's stdin, use a dedicated tmux session. Open it and inspect the pane before sending input.

```sh
tmux new-session -d -s tdeck-serial 'uvx mpremote connect /dev/cu.usbmodem101 repl'
tmux capture-pane -p -t tdeck-serial
```

To interrupt the foreground app when requested, run `tmux send-keys -t tdeck-serial C-c` and capture the pane again. At the vtOS prompt, send the requested text literally with `tmux send-keys -t tdeck-serial -l -- '<vtOS command>'`, then send `Enter` with a separate `send-keys` call. When done, run `tmux send-keys -t tdeck-serial 'C-]'` to close the console.
