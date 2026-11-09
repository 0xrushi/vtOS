# Manual Tailscale connection management. Credentials are prompted, never args.
import sys
import time
import network

_CONNECT_MS = 60000


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
        elif ch and 32 <= ord(ch) <= 126 and len(chars) < 255:
            chars.append(ch)
            if not hidden:
                sys.stdout.write(ch)


def _configure(vpn):
    key = _read_line("Tailscale auth key (hidden; Esc cancels): ", True)
    if key is None:
        return False
    name = _read_line("Hostname [vtos-tdeck]: ")
    if name is None:
        return False
    vpn.configure(key, name.strip() or "vtos-tdeck")
    return True


def main(env, args):
    action = args[0] if args else "status"
    if len(args) > 1 or action not in ("up", "down", "status", "ip", "peers", "configure"):
        print("Usage: tailscale [up|down|status|ip|peers|configure]")
        return
    try:
        import modtailscale as vpn
    except ImportError:
        print("tailscale: rebuild firmware with native MicroLink support")
        return
    try:
        if action == "up":
            if not network.WLAN(network.STA_IF).isconnected():
                print("tailscale: connect Wi-Fi with nm first")
                return
            if not vpn.status()["configured"] and not _configure(vpn):
                return
            started = False
            connected = False
            try:
                started = True  # also clean up a partially failed native start
                vpn.up()
                print("Joining Tailscale (up to 60 s; long press/Ctrl-C cancels)...")
                begin = time.ticks_ms()
                while time.ticks_diff(time.ticks_ms(), begin) < _CONNECT_MS:
                    state = vpn.status()
                    if state["connected"]:
                        connected = True
                        print("Tailscale: " + state["ip"])
                        return
                    if state["state"] == "error":
                        raise OSError("registration failed; check auth key and device approval")
                    # WIFI_WAIT is also MicroLink's normal initial state,
                    # before its asynchronous coordinator gets scheduled.
                    if state["state"] in ("down", "stopping") or not network.WLAN(network.STA_IF).isconnected():
                        raise OSError("Wi-Fi or VPN disconnected while joining")
                    time.sleep_ms(100)
                raise OSError("registration timed out; check internet, auth key and device approval")
            except KeyboardInterrupt:
                print("tailscale: cancelled")
            finally:
                if started and not connected:
                    vpn.down()
        elif action == "down":
            vpn.down()
            print("Tailscale disconnected")
        elif action == "configure":
            # Explicit recovery path for an expired/revoked saved auth key.
            if vpn.status()["state"] != "down":
                print("tailscale: run tailscale down before reconfiguring")
                return
            if _configure(vpn):
                print("Tailscale configuration saved; run tailscale up to connect")
        elif action == "ip":
            print(vpn.ip() or "Tailscale is down; run tailscale up")
        elif action == "peers":
            peers = vpn.peers()
            for ip, name, direct in peers:
                print("%s  %s  %s" % (ip, "direct" if direct else "DERP/discovering", name))
            if not peers:
                print("No peers available; check tailscale status and tailnet access rules")
        else:
            state = vpn.status()
            print("Tailscale: %s%s" % (state["state"], " " + state["ip"] if state["ip"] else ""))
            print("Configured: %s; startup: manual" % ("yes" if state["configured"] else "no"))
            print("Free SRAM: %d; largest: %d; free PSRAM: %d" % (
                state["internal_free"], state["internal_largest"], state["psram_free"]))
    except (OSError, ValueError) as err:
        print("tailscale: " + str(err))
    except KeyboardInterrupt:
        print("tailscale: cancelled")
