# httpd [start [port] | stop | token | new-token | auth on|off | auto on|off | status]
# HTTP file server for /flash and /sd. See docs/HTTPD.md.
import httpd


def _url(cfg, ip):
    url = "http://%s:%d/" % (ip or "<wifi-ip>", cfg["port"])
    return url + "?token=" + cfg["token"] if cfg["auth"] else url


def _tailnet_ip():
    try:
        import modtailscale
        st = modtailscale.status()
        return str(st["ip"]) if st["connected"] else None
    except (ImportError, OSError, KeyError):
        return None


def main(env, args):
    action = args[0] if args else "start"
    cfg = httpd.load_config()
    if action == "start":
        port = int(args[1]) if len(args) > 1 else None
        srv = httpd.start(env, port)
        cfg = httpd.load_config()
        print("HTTP server on port %d (background); `httpd stop` to stop" % srv.port)
        if not cfg["auth"]:
            print("No password: anyone on the network can read /flash. `httpd auth on` adds a token.")
        print(_url(cfg, httpd.get_ip()))
        ts = _tailnet_ip()
        if ts:
            print("tailnet:", _url(cfg, ts))
    elif action == "stop":
        httpd.stop(env)
        print("HTTP server stopped")
    elif action in ("token", "status"):
        state = "running" if env.httpd is not None else "stopped"
        print("httpd %s, token %s, autostart %s" % (state, "required" if cfg["auth"] else "off", "on" if cfg["autostart"] else "off"))
        print(_url(cfg, httpd.get_ip()))
    elif action == "new-token":
        cfg["token"] = httpd._new_token()
        httpd.save_config(cfg)
        if env.httpd is not None:
            env.httpd.token = cfg["token"]
        print(_url(cfg, httpd.get_ip()))
    elif action == "auth" and len(args) == 2 and args[1] in ("on", "off"):
        cfg["auth"] = args[1] == "on"
        httpd.save_config(cfg)
        if env.httpd is not None:
            env.httpd.auth = cfg["auth"]
        print("token required:", args[1])
        print(_url(cfg, httpd.get_ip()))
    elif action == "auto" and len(args) == 2 and args[1] in ("on", "off"):
        cfg["autostart"] = args[1] == "on"
        httpd.save_config(cfg)
        print("autostart", args[1])
    else:
        print("Usage: httpd [start [port] | stop | token | new-token | auth on|off | auto on|off | status]")
