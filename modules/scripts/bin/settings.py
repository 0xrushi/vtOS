# Settings: encrypted backup/restore and the network services.
#
#   settings                  menu
#   settings export [file]    write an encrypted backup (default /sd/backups/...)
#   settings import <file>    restore one
#
# See docs/BACKUP.md. The passphrase is always prompted, never an argument.
import sys

from bin import launcher
import backup

TOP = [
    ("export", "back up settings + secrets to the SD card"),
    ("import", "restore settings from a backup"),
    ("web", "http file server: start / stop"),
    ("remote", "vnc server (Mac remote control): start / stop"),
]


def _read_line(prompt, hidden=False):
    sys.stdout.write(prompt)
    chars = []
    while True:
        ch = sys.stdin.read(1)
        if ch in ("\r", "\n"):
            sys.stdout.write("\r\n")
            return "".join(chars)
        if ch in ("\x1b", "\x03"):
            sys.stdout.write("\r\n")
            return None
        if ch in ("\x08", "\x7f"):
            if chars:
                chars.pop()
                if not hidden:
                    sys.stdout.write("\b \b")
        elif ch and 32 <= ord(ch) <= 126 and len(chars) < 128:
            chars.append(ch)
            sys.stdout.write("*" if hidden else ch)


def _passphrase(confirm):
    first = _read_line("Passphrase (Esc cancels): ", True)
    if not first:
        return None
    if confirm and _read_line("Again: ", True) != first:
        print("settings: passphrases differ")
        return None
    return first


def _sd_ready(env):
    if getattr(env, "sd", None) is None:
        print("settings: no SD card; use `settings export /flash/x.vtbk`")
        return False
    return True


def do_export(env, path=None):
    if path is None:
        if not _sd_ready(env):
            return
        path = backup.SD_DIR + "/" + backup.DEFAULT_NAME
    pw = _passphrase(True)
    if pw is None:
        return
    try:
        size = backup.export_file(path, pw)
    except (OSError, backup.BackupError) as e:
        print("settings: export failed:", e)
        return
    print("Saved %d bytes to %s" % (size, path))


def do_import(env, path=None):
    if path is None:
        if not _sd_ready(env):
            return
        names = backup.list_backups()
        if not names:
            print("settings: no backups in", backup.SD_DIR)
            return
        choice = launcher.pick(env, [(n, "") for n in names], "restore")
        if not choice:
            return
        path = backup.SD_DIR + "/" + choice
    pw = _passphrase(False)
    if pw is None:
        return
    try:
        restored = backup.import_file(path, pw)
    except (OSError, backup.BackupError) as e:
        print("settings: import failed:", e)
        return
    print("Restored %d items: %s" % (len(restored), ", ".join(restored)))
    print("Reboot to apply (Ctrl-D).")


def _toggle(env, service):
    choice = launcher.pick(env, [("start", ""), ("stop", "")], service)
    if choice == "start":
        env.shell.execute(service)
    elif choice == "stop":
        env.shell.execute(service + " stop")


def main(env, args):
    if args:
        cmd = args[0]
        path = args[1] if len(args) > 1 else None
        if cmd == "export":
            do_export(env, path)
        elif cmd == "import" and path:
            do_import(env, path)
        else:
            print("Usage: settings [export [file] | import <file>]")
        return
    choice = launcher.pick(env, TOP, "settings")
    if choice == "export":
        do_export(env)
    elif choice == "import":
        do_import(env)
    elif choice == "web":
        _toggle(env, "httpd")
    elif choice == "remote":
        _toggle(env, "vncd")
