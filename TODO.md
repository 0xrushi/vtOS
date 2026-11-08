# TODO

## After flashing firmware built from `main`/`dev` on the T-Deck Plus

The T-Deck Plus currently runs release v0.1.14 plus on-device overrides in the
flash root (installed over USB with mpremote). Firmware built from this branch
contains all of those changes natively, so the overrides must not come back.

### Before flashing

- [ ] Back up anything you want to keep from `/flash` (`make flash` runs
      `erase_flash`, which wipes the whole filesystem, not just the app):
      `.shellrc`, `.favs.json`, `.ssh/config`, `menu/`, and any books/notes.
      e.g. `mpremote connect <port> cp -r : ./flash-backup/`
- [ ] Build from this branch with the updated board definition
      (`boards/LILYGO_T_DECK/mpconfigboard.h` disables the UART0 console that
      conflicts with the Plus's GPS pins): `make build`
- [ ] Flash: `make flash` (device in bootloader mode, correct `PORT`)

### After flashing

- [ ] Do NOT restore the old `main.py`. Its patches (UART mask, `ssh_terminal`,
      `shell_ext`, `statusbar_ext`, `keyrepeat` install) are now built in and
      would override the new firmware. Start with no `main.py`, or only your
      own startup code.
- [ ] Do NOT restore these stale override files (the firmware has the real
      versions): `keyrepeat.py`, `trackball.py`, `ssh_terminal.py`,
      `shell_ext.py`, `launcher.py`, `statusbar_ext.py`, `main.py.before-*`,
      `ssh_terminal.before-trackball.py`.
- [ ] Restore your backed-up user data: `.shellrc`, `.favs.json`,
      `.ssh/config` (SSH profiles), `menu/`.
- [ ] Connect Wi-Fi again if it isn't in `.shellrc`: `nm connect "SSID" "pass"`.
- [ ] Verify on the device:
  - [ ] Typing works with no workaround (UART fix is in the firmware).
  - [ ] Hold Backspace: repeats after ~400 ms, stops on release.
  - [ ] Alt+D opens the launcher; Esc / Alt+D / long-press closes it.
  - [ ] `ssh` with a `.ssh/config` shows the profile picker.
  - [ ] SSH: Enter submits in Claude/Codex; trackball up/down scrolls output.
  - [ ] Top bar shows the dark, colored style (battery/Wi-Fi colors change).
- [ ] If anything above misbehaves, check `git status` on the firmware source:
      the build only includes what is committed or present in the working tree
      when `make build` ran.
