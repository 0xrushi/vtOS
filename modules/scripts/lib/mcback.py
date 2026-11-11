#
# MeshCore backend for meshchat: wraps mcmesh.Mesh in the UI contract described
# at the top of meshback.py, plus the extra calls the MeshCore controls use:
#   import_contact(text) -> (ok, message, nid)   contact link / key / heard name
#   login(nid, password) -> (ok, message)         repeater / room login
#   set_radio(text)      -> (ok, message)         "freq bw sf cr [power]"
#   identity()           -> {"name", "pub", "link"}
#   heard()              -> [(name, kind_name, age_secs, hops)]
#   forget(nid)          -> (ok, message)
# License: MIT
#

import mcmesh
import mcproto as W
from meshdraw import GREEN, YELLOW, RED, DIM


def _hops(h):
    if h is None:
        return "?"
    return "direct" if h == 0 else "%d hop%s" % (h, "" if h == 1 else "s")


class MeshCoreBackend:
    demo = False
    meshcore = True

    def __init__(self, radio, state, env=None, clock=None, configure=None, path=mcmesh.PATH,
                 unix_offset=0):
        import time
        self.radio = radio
        self.env = env
        self.state = state
        self._configure = configure
        self._path = path
        self._clock = clock or time.time
        self.mesh = mcmesh.Mesh(radio, state, self._clock, unix_offset)

    # -- UI contract --

    def channels(self):
        return [("ch:%d" % i, c.name) for i, c in enumerate(self.state.channels)]

    def nodes(self):
        return list(self.state.contacts)

    def info(self):
        r = self.state.radio
        return {"name": self.state.name, "region": "%.3f MHz" % r["freq"],
                "preset": "MeshCore SF%d/%gk" % (r["sf"], r["bw"]), "batt": None,
                "link": "ok", "demo": False}

    def stats(self):
        m = self.mesh
        st = m.stats
        import mtcrypto
        r = self.state.radio
        return {"mode": "MeshCore (host-tested, not on-air verified)",
                "frequency": "%.3f MHz" % r["freq"],
                "modem": "BW %g  SF%d  CR4/%d" % (r["bw"], r["sf"], r["cr"]),
                "power": "%d dBm" % r["power"], "packets in": st["rx"], "packets out": st["tx"],
                "duplicates": st["dup"], "acked": st["acked"], "failed": st["failed"],
                "not for us": st["undecoded"], "bad": st["bad"], "contacts": len(self.state.contacts),
                "heard adverts": len(m.heard), "crypto": mtcrypto.backend()}

    def send(self, conv_key, text):
        return self.mesh.send_text(conv_key, text)

    def poll(self):
        out = []
        changed = False
        for ev in self.mesh.poll():
            kind = ev[0]
            if kind == "text":
                _, key, c, name, text, snr, hops, ttype = ev
                meta = _hops(hops) + ((" %+.0fdB" % snr) if snr is not None else "")
                nid = c.nid if c is not None else name
                if ttype == W.TXT_CLI:
                    meta = "reply " + meta
                out.append(("msg", key, nid, name, text, self._clock(), meta))
            elif kind == "sent":
                out.append(("ack", ev[1], "sent"))
            elif kind == "ack":
                out.append(("ack", ev[1], ev[2]))
                if not ev[2]:
                    out.append(("notice", "No ack from the node (%s)" % ev[3], YELLOW))
            elif kind == "node":
                changed = True
            elif kind == "login":
                out.append(("notice",) + self._login_text(ev))
            elif kind == "response":
                out.append(("notice", "Response from %s (%d bytes)" % (self._name(ev[1]), ev[2]), DIM))
        if changed:
            out.append(("node",))
        return out

    def _name(self, nid):
        c = self.state.find(nid)
        return c.name if c else nid

    def _login_text(self, ev):
        who = self._name(ev[1])
        if ev[2] is None:
            return "No login reply from %s" % who, YELLOW
        if not ev[2]:
            return "%s refused the login" % who, RED
        return "Logged in to %s%s" % (who, " (admin)" if ev[3] else ""), GREEN

    # -- MeshCore controls --

    def _save(self):
        try:
            self.state.save(self._path)
        except OSError:
            pass

    def import_contact(self, text):
        text = text.strip()
        ok, msg, c = mcmesh.import_link(self.state, text)
        if not ok:
            h = self._heard_by_name(text) if text else None
            if h is None:
                return False, msg, None
            c = self.mesh.adopt(h)
            msg, ok = "Added %s (%s) from adverts" % (c.name, c.kind_name), True
        else:
            self.mesh.heard.pop(mcmesh._hexs(c.pub), None)
        self._save()
        return ok, msg, c.nid

    def _heard_by_name(self, name):
        q = name.lower()
        for c in self.mesh.heard.values():
            if c.name.lower() == q:
                return c
        for c in self.mesh.heard.values():
            if c.name.lower().startswith(q):
                return c
        return None

    def login(self, nid, password):
        c = self.state.find(nid)
        if c is None:
            return False, "Unknown contact"
        if not c.needs_login:
            return False, "%s is not a repeater or room" % c.name
        try:
            self.mesh.login(c, password)
        except (ValueError, RuntimeError) as e:
            return False, str(e)
        return True, "Login sent to %s, waiting for reply" % c.name

    def set_radio(self, text):
        r = self.state.radio
        old = dict(r)
        ok, msg = mcmesh.set_radio(self.state, text)
        if not ok or not text.strip():
            return ok and bool(text.strip()), msg
        if self._configure is not None:
            ok2, msg2, _ = self._configure(self.radio, self.state)
            if not ok2:
                r.update(old)
                return False, msg2
        if hasattr(self.radio, "start_receive"):
            self.radio.start_receive()
        self._save()
        return True, msg

    def identity(self):
        return {"name": self.state.name, "pub": mcmesh._hexs(self.state.pub),
                "link": mcmesh.share_link(self.state)}

    def heard(self):
        now = self._clock()
        return [(c.name, c.kind_name, None if c.heard is None else now - c.heard, c.hops)
                for c in self.mesh.heard.values()]

    def forget(self, nid):
        c = self.state.find(nid)
        if c is None:
            return False, "Unknown contact"
        self.state.contacts.remove(c)
        self._save()
        return True, "Removed %s" % c.name

    def close(self):
        self._save()
        if self.env is not None:
            try:
                self.radio.deinit()
            finally:
                self.env.radio = None
