#
# clip: phone-to-device clipboard. See docs/CLIPBOARD.md.
#

import clipd

USAGE = """usage: clip              show the clipboard
       clip pair         show a 6-digit code to pair the phone app
       clip start [port] start the phone share service
       clip stop         stop it
       clip token [new]  show the pairing token (new = regenerate)
       clip paste        type the clipboard at the cursor
       clip clear        empty the clipboard
       clip auto on|off  start the service at boot"""


def _address(cfg):
    ip = clipd.get_ip() or "<no wifi>"
    return "%s:%d" % (ip, cfg["port"])


def main(env, args):
    cmd = args[0] if args else ""

    if cmd == "":
        text = env.clipboard
        if text:
            print(text)
        else:
            print("(clipboard empty)")
        return

    if cmd == "start":
        port = None
        if len(args) > 1:
            try:
                port = int(args[1])
            except ValueError:
                print("clip: bad port")
                return
        if env.clipd is not None and port is not None:
            clipd.stop(env)
        was_running = env.clipd is not None
        clipd.start(env, port)
        cfg = clipd.load_config()
        print("clip: %s on %s" % ("already running" if was_running else "listening", _address(cfg)))
        print("pair the phone app with: clip token")
        return

    if cmd == "stop":
        if env.clipd is None:
            print("clip: not running")
        else:
            clipd.stop(env)
            print("clip: stopped")
        return

    if cmd == "pair":
        import os
        if env.clipd is None:
            clipd.start(env)
        cfg = clipd.load_config()
        n = int.from_bytes(os.urandom(4), "big") % 1000000
        code = "%06d" % n
        env.clipd.start_pairing(code)
        print("pairing code: %s" % code)
        print("valid %d s, %d tries. In the app: Find T-Deck." % (clipd.PAIR_MS // 1000, clipd.PAIR_TRIES))
        print("address: %s" % _address(cfg))
        return

    if cmd == "token":
        cfg = clipd.load_config()
        if len(args) > 1 and args[1] == "new":
            cfg["token"] = clipd._new_token()
            clipd.save_config(cfg)
            if env.clipd is not None:
                env.clipd.token = cfg["token"]
            print("clip: new token generated (re-pair the phone)")
        print("address: %s" % _address(cfg))
        print("token:   %s" % cfg["token"])
        return

    if cmd == "paste":
        if not env.clipboard:
            print("clip: clipboard empty")
            return
        clipd.paste(env)
        return

    if cmd == "clear":
        env.clipboard = ""
        env.paste_buf = ""
        return

    if cmd == "auto" and len(args) > 1 and args[1] in ("on", "off"):
        cfg = clipd.load_config()
        cfg["autostart"] = args[1] == "on"
        clipd.save_config(cfg)
        print("clip: autostart %s" % args[1])
        return

    print(USAGE)
