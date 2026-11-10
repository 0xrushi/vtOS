#
# Meshchat settings screens: section list, form editor, channel manager, node
# detail, dialogs and text prompts. Mixed into meshui.App.
# License: MIT
#
# All screens edit a *copy* of the backend's Settings (cfg_edit). Nothing
# touches the radio until "Apply changes", which hands the copy to
# backend.apply() and reports what happened.
#

import math

import mtconfig as C
import mtproto as P
import mtradio as R
from meshdraw import (BG, BG_BAR, BG_TAB, BG_SEL, RULE, FG, DIM, ACCENT, GREEN, YELLOW, RED,
                      S, seg_len, paint, wrap, age_str, snr_color, hops_str, ljust, rjust)

SEC_NAMES = ("Radio", "Channels", "Device", "Position", "Telemetry", "Advanced")

CH_FIELDS = (
    {"key": "name", "label": "Name", "kind": "text", "max": 11,
     "help": "Up to 11 characters. The primary channel's blank name means the preset name."},
    {"key": "psk", "label": "Encryption key", "kind": "key",
     "help": "Enter to pick default, none, a new random key, or paste your own (hex or base64)."},
    {"key": "uplink", "label": "Uplink", "kind": "bool", "help": "Allow gateways to forward this channel to MQTT."},
    {"key": "downlink", "label": "Downlink", "kind": "bool", "help": "Accept MQTT traffic for this channel."},
    {"key": "precision", "label": "Position bits", "kind": "int", "range": (10, 32, 1),
     "help": "How exactly your position is shared on this channel. 32 is exact."},
    {"key": "share", "label": "Share channel", "kind": "action",
     "help": "Shows the meshtastic.org link for this channel only."},
)


def _compass(deg):
    names = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return names[int((deg + 22.5) // 45) % 8]


def distance_bearing(lat1, lon1, lat2, lon2):
    """(km, degrees) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return km, (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def parse_key(text):
    """User-typed key -> psk bytes (1, 16 or 32 bytes). ValueError if unusable."""
    t = text.strip()
    low = t.lower()
    if low in ("default", "aq==", "aq"):
        return b"\x01"
    if low in ("none", "off", ""):
        return b"\x00"
    try:
        if len(t) in (32, 64):
            return bytes.fromhex(t)
    except ValueError:
        pass
    try:
        raw = P.b64url_decode(t)
    except ValueError:
        raise ValueError("not a hex or base64 key")
    if len(raw) not in (16, 32):
        raise ValueError("key must be 16 or 32 bytes")
    return raw


def _decimals(step):
    s = repr(step)
    return len(s.split(".")[1]) if "." in s else 0


class SettingsMixin:
    def set_init(self):
        self.cfg_edit = None
        self.cfg_base = None
        self.cfg_orig = None
        self.set_sel = 0
        self.sec_idx = 0
        self.form_sel = 0
        self.form_top = 0
        self.ch_idx = 0
        self.node_view = None
        self.dialog = None
        self.prompt = None
        self.form_kind = "section"      # "section" | "channel"

    # -- opening / state --

    def settings_available(self):
        return getattr(self.backend, "config", None) is not None

    def set_open(self, now, to="settings"):
        if not self.settings_available():
            self.notify("Settings aren't available in this mode", YELLOW, now)
            return
        if self.cfg_edit is None:
            self.cfg_orig = self.backend.config
            self.cfg_edit = self.cfg_orig.copy()
            self.cfg_base = self.cfg_orig.to_dict()
        self.mode = to
        self.set_sel = 0
        self.ch_idx = 0

    def cfg_dirty(self):
        return self.cfg_edit is not None and self.cfg_edit.to_dict() != self.cfg_base

    def set_leave(self, now):
        """Back out of settings; asks first when there are unapplied edits."""
        if not self.cfg_dirty():
            self.cfg_edit = None
            self.mode = "chat"
            return
        self.ask("Unapplied changes", ["Apply them to the radio?"],
                 ["Apply", "Discard", "Keep editing"], lambda i: self._leave_choice(i, now))

    def _leave_choice(self, i, now):
        if i == 0:
            if self.apply_settings(now):
                self.cfg_edit = None
                self.mode = "chat"
        elif i == 1:
            self.cfg_edit = None
            self.mode = "chat"

    def apply_settings(self, now):
        cfg = self.cfg_edit
        problems = cfg.problems()
        if problems:
            self.notify(problems[0], RED, now)
            return False
        ok, msg = self.backend.apply(cfg)
        if ok:
            self.cfg_orig = self.backend.config
            self.cfg_edit = self.backend.config.copy()
            self.cfg_base = self.cfg_edit.to_dict()
            self.sync_channels()
            self.notify(msg, GREEN, now)
        else:
            self.notify(msg, RED, now)
        return ok

    # -- dialog and prompt --

    def ask(self, title, lines, opts, cb):
        self.dialog = {"title": title, "lines": lines, "opts": opts, "sel": 0, "cb": cb}

    def ask_text(self, title, value, cb, maxlen=60, hint="Enter ok   click cancel", fresh=False):
        """Text prompt. With fresh=True the first key typed replaces the old value."""
        self.prompt = {"title": title, "value": value, "cb": cb, "max": maxlen, "hint": hint,
                       "fresh": fresh}

    def key_dialog(self, k, now):
        d = self.dialog
        n = len(d["opts"])
        if k == "UP":
            d["sel"] = (d["sel"] - 1) % n
        elif k == "DOWN":
            d["sel"] = (d["sel"] + 1) % n
        elif k in ("\r", "\n", "ESC"):
            idx = d["sel"] if k != "ESC" else None
            self.dialog = None
            if idx is not None:
                d["cb"](idx)
        return None

    def key_prompt(self, k, now):
        p = self.prompt
        if k == "ESC":
            self.prompt = None
        elif k in ("\r", "\n"):
            self.prompt = None
            p["cb"](p["value"].strip())
        elif k in ("\x08", "\x7f"):
            p["value"] = "" if p.get("fresh") else p["value"][:-1]
            p["fresh"] = False
        elif len(k) == 1 and 32 <= ord(k) <= 126:
            if p.get("fresh"):
                p["value"] = ""
                p["fresh"] = False
            if len(p["value"]) < p["max"]:
                p["value"] += k
        return None

    # -- settings main --

    def _main_items(self):
        items = [("sec", n) for n in SEC_NAMES]
        items.append(("gap", ""))
        items.append(("apply", "Apply changes"))
        items.append(("discard", "Discard changes"))
        return items

    def _summary(self, name):
        c = self.cfg_edit
        if name == "Radio":
            if c.reg() is None:
                return "not set"
            return "%s  %s  slot %d" % (c.region, R.preset(c.preset)[2] if c.use_preset else "custom",
                                       c.effective_slot())
        if name == "Channels":
            return "%d channel%s" % (len(c.channels), "" if len(c.channels) == 1 else "s")
        if name == "Device":
            return "%s  %s" % (c.display_names()[0][:14], C.nice(c.role))
        if name == "Position":
            return c.pos_mode if c.pos_mode == "off" else "%s  %s" % (c.pos_mode, self._label(C.INTERVALS_POS, c.pos_interval))
        if name == "Telemetry":
            return self._label(C.INTERVALS_TELEM, c.telem_interval)
        if name == "Advanced":
            return "TCXO %s" % ("auto" if c.tcxo is None else ("%gV" % c.tcxo if c.tcxo else "none"))
        return ""

    @staticmethod
    def _label(opts, value):
        for v, lab in opts:
            if v == value:
                return lab
        return str(value)

    def key_settings(self, k, now):
        items = self._main_items()
        n = len(items)
        if k == "ESC":
            self.set_leave(now)
        elif k in ("UP", "DOWN"):
            step = -1 if k == "UP" else 1
            i = (self.set_sel + step) % n
            while items[i][0] == "gap":
                i = (i + step) % n
            self.set_sel = i
        elif k in ("\r", "\n"):
            kind, name = items[self.set_sel]
            if kind == "sec":
                if name == "Channels":
                    self.mode = "chlist"
                    self.ch_idx = 0
                else:
                    self.sec_idx = SEC_NAMES.index(name)
                    self.form_kind = "section"
                    self.form_sel = 0
                    self.form_top = 0
                    self.mode = "form"
                    self._form_first()
            elif kind == "apply":
                self.apply_settings(now)
            elif kind == "discard":
                if self.cfg_dirty():
                    self.cfg_edit = self.cfg_orig.copy()
                    self.cfg_base = self.cfg_orig.to_dict()
                    self.notify("Changes discarded", DIM, now)
        return None

    def render_settings(self, out, cols, rows):
        dirty = self.cfg_dirty()
        self._subheader2(out, cols, "Settings", "* unapplied" if dirty else "applied",
                         YELLOW if dirty else DIM)
        items = self._main_items()
        h = rows - 4
        for i in range(h):
            row = 4 + i
            if i >= len(items):
                out.append(paint(row, [], BG, cols))
                continue
            kind, name = items[i]
            sel = i == self.set_sel
            bg = BG_SEL if sel else BG
            if kind == "gap":
                out.append(paint(row, [], BG, cols))
                continue
            if kind == "sec":
                summ = self._summary(name)
                labw = 11
                segs = [S(FG, " "), S(ACCENT, "> " if sel else "  "),
                        S(FG, ljust(name, labw), bold=sel)]
                room = cols - 3 - labw - 1
                segs.append(S(DIM, rjust(summ[:room], room) + " "))
            else:
                active = kind == "apply" and dirty
                segs = [S(FG, " "), S(ACCENT, "> " if sel else "  "),
                        S(GREEN if active else (FG if sel else DIM), name, bold=sel or active)]
            out.append(paint(row, segs, bg, cols))
        out.append(paint(rows, [S(DIM, " Enter open   click back")], BG, cols))

    def _subheader2(self, out, cols, left, right="", rcolor=DIM):
        segs = [S(ACCENT, " " + left, bold=True)]
        gap = cols - seg_len(segs) - len(right) - 1
        if right and gap >= 1:
            segs += [S(FG, " " * gap), S(rcolor, right + " ")]
        out.append(paint(2, segs, BG_TAB, cols))
        out.append(paint(3, [S(RULE, "─" * cols)], BG, cols))

    # -- form engine --

    def _fields(self):
        if self.form_kind == "channel":
            return list(CH_FIELDS)
        name = SEC_NAMES[self.sec_idx]
        for sec, fields in C.SECTIONS:
            if sec == name:
                return [f for f in fields if C.field_visible(f, self.cfg_edit)]
        return []

    def _target(self):
        return self.cfg_edit.channels[self.ch_idx] if self.form_kind == "channel" else self.cfg_edit

    def _base_target(self):
        base = C.Settings.from_dict(self.cfg_base)
        if self.form_kind == "channel":
            return base.channels[self.ch_idx] if self.ch_idx < len(base.channels) else None
        return base

    def _form_first(self):
        fields = self._fields()
        for i, f in enumerate(fields):
            if f["kind"] != "info":
                self.form_sel = i
                return

    def _value(self, f, obj=None):
        obj = obj or self._target()
        if f["kind"] == "info":
            return getattr(obj, f["key"])()
        if f["kind"] == "action":
            return ""
        if f["kind"] == "key":
            return "%s %s" % (P.key_kind(obj.psk), obj.psk.hex()[:4] if len(obj.psk) > 1 else "")
        return getattr(obj, f["key"])

    def _show(self, f, v):
        kind = f["kind"]
        if kind == "bool":
            return "on" if v else "off", GREEN if v else DIM
        if kind == "enum":
            return self._label(C.options_of(f), v), FG
        if kind == "float":
            return "%.*f" % (_decimals(f["range"][2]), v), FG
        if kind == "text":
            return (v, FG) if v else ("(not set)", DIM)
        if kind == "int" and f["key"] == "slot":
            return ("auto" if not v else str(v)), FG
        if kind == "int" and f["key"] == "tx_power":
            return "%d dBm" % v, FG
        return str(v), FG

    def key_form(self, k, now):
        fields = self._fields()
        sel = [i for i, f in enumerate(fields) if f["kind"] != "info"]
        if not sel:
            self.mode = "settings" if self.form_kind == "section" else "chlist"
            return None
        if self.form_sel not in sel:
            self.form_sel = sel[0]
        pos = sel.index(self.form_sel)
        f = fields[self.form_sel]
        obj = self._target()
        if k == "ESC":
            self.mode = "settings" if self.form_kind == "section" else "chlist"
        elif k == "UP":
            self.form_sel = sel[(pos - 1) % len(sel)]
        elif k == "DOWN":
            self.form_sel = sel[(pos + 1) % len(sel)]
        elif k in ("LEFT", "RIGHT"):
            self._change(f, obj, -1 if k == "LEFT" else 1, now)
        elif k in ("\r", "\n"):
            self._activate(f, obj, now)
        return None

    def _change(self, f, obj, delta, now):
        kind = f["kind"]
        key = f["key"]
        v = getattr(obj, key, None)
        if kind == "bool":
            setattr(obj, key, not v)
        elif kind == "enum":
            opts = C.options_of(f)
            vals = [o[0] for o in opts]
            i = vals.index(v) if v in vals else 0
            setattr(obj, key, vals[(i + delta) % len(vals)])
            if key == "region" and self.form_kind == "section":
                self._clamp_power()
        elif kind in ("int", "float"):
            lo, hi, step = f["range"]
            if hi == "slots":
                hi = self.cfg_edit.slots()
            nv = v + delta * step
            nv = max(lo, min(hi, nv))
            if kind == "float":
                nv = round(nv, _decimals(step) + 1)
            setattr(obj, key, nv)
        self._after_edit(f)

    def _clamp_power(self):
        c = self.cfg_edit
        limit = R.clamp_power(c.reg(), 0)
        if c.tx_power > limit:
            c.tx_power = limit

    def _after_edit(self, f):
        # the visible field set can change (e.g. preset <-> manual modem)
        fields = self._fields()
        if self.form_sel >= len(fields):
            self.form_sel = max(0, len(fields) - 1)

    def _activate(self, f, obj, now):
        kind = f["kind"]
        key = f["key"]
        if kind in ("bool", "enum"):
            self._change(f, obj, 1, now)
        elif kind == "text":
            self.ask_text(f["label"], getattr(obj, key), lambda t: self._set_text(f, obj, t, now),
                          f.get("max", 40))
        elif kind in ("int", "float"):
            cur = getattr(obj, key)
            txt = ("%.*f" % (_decimals(f["range"][2]), cur)) if kind == "float" else str(cur)
            self.ask_text(f["label"], txt, lambda t: self._set_number(f, obj, t, now), 12, fresh=True)
        elif kind == "key":
            self._ask_key(obj, now)
        elif kind == "action":
            if key == "reset":
                self.ask("Reset settings", ["Restore every setting to its default?"],
                         ["Reset", "Cancel"], lambda i: self._do_reset(i, now))
            elif key == "share":
                self.show_share(only=self.ch_idx)

    def _set_text(self, f, obj, text, now):
        setattr(obj, f["key"], text[:f.get("max", 40)])
        self.dirty = True

    def _set_number(self, f, obj, text, now):
        try:
            v = float(text) if f["kind"] == "float" else int(float(text))
        except ValueError:
            self.notify("Not a number", RED, now)
            return
        lo, hi, _ = f["range"]
        if hi == "slots":
            hi = self.cfg_edit.slots()
        if v < lo or v > hi:
            self.notify("Use %g to %g" % (lo, hi), RED, now)
            return
        setattr(obj, f["key"], v)
        self.dirty = True

    def _do_reset(self, i, now):
        if i == 0:
            keep = self.cfg_edit.node_num
            fresh = C.Settings()
            fresh.node_num = keep
            self.cfg_edit = fresh
            self.notify("Defaults restored, not applied yet", YELLOW, now)

    def _ask_key(self, ch, now):
        opts = ["Default key (public)", "No encryption", "New random 128-bit",
                "New random 256-bit", "Type or paste a key"]
        self.ask("Encryption key", ["Everyone on the channel needs the same key."], opts,
                 lambda i: self._key_choice(ch, i, now))

    def _key_choice(self, ch, i, now):
        if i == 0:
            ch.psk = b"\x01"
        elif i == 1:
            ch.psk = b"\x00"
        elif i == 2:
            ch.psk = C.random_key(16)
        elif i == 3:
            ch.psk = C.random_key(32)
        elif i == 4:
            self.ask_text("Key (hex or base64)", "", lambda t: self._key_typed(ch, t, now), 96)

    def _key_typed(self, ch, text, now):
        try:
            ch.psk = parse_key(text)
        except ValueError as e:
            self.notify(str(e), RED, now)

    def render_form(self, out, cols, rows, now):
        dirty = self.cfg_dirty()
        fields = self._fields()
        if self.form_kind == "channel":
            ch = self.cfg_edit.channels[self.ch_idx]
            title = "Channel %d  %s" % (self.ch_idx, ch.name or ("primary" if self.ch_idx == 0 else "(no name)"))
        else:
            title = SEC_NAMES[self.sec_idx]
        self._subheader2(out, cols, title, "* unapplied" if dirty else "", YELLOW)
        h = rows - 7
        obj = self._target()
        base = self._base_target()
        row = 4
        used = 0
        # info fields first, wrapped
        for f in fields:
            if f["kind"] == "info":
                for part in wrap(str(self._value(f, obj)), cols - 2)[:2]:
                    out.append(paint(row, [S(DIM, " " + part)], BG, cols))
                    row += 1
                    used += 1
        warn = self.cfg_edit.name_warning() if self.form_kind == "section" and SEC_NAMES[self.sec_idx] == "Radio" else ""
        if warn:
            for part in wrap(warn, cols - 2)[:1]:
                out.append(paint(row, [S(YELLOW, " " + part)], BG, cols))
                row += 1
                used += 1
        body = [f for f in fields if f["kind"] != "info"]
        visible = max(1, h - used)
        sel_pos = 0
        for i, f in enumerate(body):
            if fields.index(f) == self.form_sel:
                sel_pos = i
        top = 0
        if sel_pos >= visible:
            top = sel_pos - visible + 1
        labw = min(18, max(12, cols // 2 - 4))
        for i in range(visible):
            idx = top + i
            if idx >= len(body):
                out.append(paint(row + i, [], BG, cols))
                continue
            f = body[idx]
            sel = fields.index(f) == self.form_sel
            val, vcol = self._show(f, self._value(f, obj)) if f["kind"] not in ("action", "key") else \
                (self._value(f, obj), FG)
            if f["kind"] == "action":
                val, vcol = "", FG
            if self.form_kind == "channel" and f["key"] == "name" and not obj.name:
                val, vcol = "(%s)" % (R.preset(self.cfg_edit.preset)[2] if self.ch_idx == 0 else "none"), DIM
            changed = ""
            if base is not None and f["kind"] not in ("action",):
                try:
                    if self._value(f, obj) != self._value(f, base):
                        changed = "*"
                except Exception:
                    pass
            room = cols - 4 - labw - 1
            if sel and f["kind"] in ("enum", "int", "float", "bool"):
                val = "< %s >" % val
            if sel:
                vcol = ACCENT
            segs = [S(FG, " "), S(ACCENT, "> " if sel else "  "),
                    S(FG if sel else DIM, ljust(f["label"] + (" " + changed if changed else ""), labw)[:labw], bold=sel),
                    S(YELLOW, "") ]
            val = val[-room:] if len(val) > room else val
            segs.append(S(vcol, rjust(val, room), bold=sel))
            segs.append(S(FG, " "))
            out.append(paint(row + i, segs, BG_SEL if sel else BG, cols))
        out.append(paint(rows - 3, [S(RULE, "─" * cols)], BG, cols))
        helptext = ""
        if 0 <= self.form_sel < len(fields):
            helptext = fields[self.form_sel].get("help", "")
        hl = wrap(helptext, cols - 2)[:2]
        for i in range(2):
            out.append(paint(rows - 2 + i, [S(DIM, " " + hl[i])] if i < len(hl) else [], BG, cols))
        # hint on last row shares with help line 2: keep help to rows-2..rows-1, hint on rows
        out.append(paint(rows, [S(DIM, " < > change   Enter edit   click back")], BG, cols))

    # -- channels --

    def key_chlist(self, k, now):
        chans = self.cfg_edit.channels
        n = len(chans)
        if k == "ESC":
            self.mode = "settings"
            self.set_sel = 1
        elif k == "UP":
            self.ch_idx = (self.ch_idx - 1) % n
        elif k == "DOWN":
            self.ch_idx = (self.ch_idx + 1) % n
        elif k in ("\r", "\n"):
            self.form_kind = "channel"
            self.form_sel = 0
            self.form_top = 0
            self.mode = "form"
        elif k == "a":
            try:
                i = self.cfg_edit.add_channel("Chan%d" % len(chans))
                self.ch_idx = i
                self.notify("Added with a new random key", GREEN, now)
            except ValueError as e:
                self.notify(str(e), RED, now)
        elif k == "d":
            if self.ch_idx == 0:
                self.notify("The primary channel can't be deleted", RED, now)
            else:
                self.ask("Delete channel", ["Delete %s?" % (chans[self.ch_idx].name or "channel")],
                         ["Delete", "Cancel"], lambda i: self._del_channel(i, now))
        elif k == "s":
            self.show_share(only=None)
        elif k == "i":
            self.ask_text("Paste channel URL", "", lambda t: self._import_url(t, now), 400,
                          "Alt+V pastes   Enter ok   click cancel")
        return None

    def _del_channel(self, i, now):
        if i == 0:
            self.cfg_edit.delete_channel(self.ch_idx)
            self.ch_idx = max(0, self.ch_idx - 1)

    def _import_url(self, url, now):
        if not url:
            return
        try:
            probe = self.cfg_edit.copy()
            count = probe.import_url(url)
        except (ValueError, UnicodeError, KeyError):
            self.notify("That isn't a valid channel link", RED, now)
            return
        self.ask("Replace channels?", ["Link has %d channel%s and radio settings." %
                                       (count, "" if count == 1 else "s"),
                                       "This replaces your current ones."],
                 ["Replace", "Cancel"], lambda i: self._apply_import(i, probe, now))

    def _apply_import(self, i, probe, now):
        if i == 0:
            self.cfg_edit = probe
            self.ch_idx = 0
            self.notify("Imported. Apply to use it", YELLOW, now)

    def show_share(self, only=None):
        c = self.cfg_edit.copy()
        if only is not None:
            c.channels = [c.channels[only]]
        url = c.share_url(include_secondary=only is None)
        saved = ""
        try:
            with open("/flash/mesh-channels.txt", "w") as f:
                f.write(url + "\n")
            saved = "Saved to /flash/mesh-channels.txt"
        except OSError:
            pass
        lines = [[S(DIM, "Anyone with this link can join.")],
                 [S(YELLOW, "Keep it private: it has the key.")], [S(FG, "")]]
        for part in wrap(url, 38):
            lines.append([S(FG, part)])
        if saved:
            lines.append([S(FG, "")])
            lines.append([S(DIM, saved)])
        self.page = ("Share", lines)
        self.page_scroll = 0
        self.page_back = self.mode
        self.mode = "page"

    def render_chlist(self, out, cols, rows):
        chans = self.cfg_edit.channels
        self._subheader2(out, cols, "Channels (%d)" % len(chans), "max %d" % C.MAX_CHANNELS)
        h = rows - 4
        top = 0
        if self.ch_idx >= h:
            top = self.ch_idx - h + 1
        for i in range(h):
            idx = top + i
            row = 4 + i
            if idx >= len(chans):
                out.append(paint(row, [], BG, cols))
                continue
            ch = chans[idx]
            sel = idx == self.ch_idx
            name = ch.name or (R.preset(self.cfg_edit.preset)[2] if idx == 0 else "(no name)")
            kind = P.key_kind(ch.psk)
            kcol = YELLOW if kind in ("default", "none") else GREEN
            namew = max(6, cols - 4 - 12 - 3)
            segs = [S(FG, " "), S(ACCENT, "> " if sel else "  "),
                    S(DIM, "%d " % idx), S(FG, ljust(name[:namew], namew), bold=sel),
                    S(kcol, rjust(kind, 8)), S(FG, " ")]
            out.append(paint(row, segs, BG_SEL if sel else BG, cols))
        out.append(paint(rows, [S(DIM, " a add  d del  s share  i import")], BG, cols))

    # -- node detail --

    def open_node(self, node):
        self.node_view = node
        self.mode = "node"

    def key_node(self, k, now):
        n = self.node_view
        if k == "ESC":
            self.mode = self.node_back or "nodes"
            self.node_back = None
        elif k in ("\r", "\n", "m"):
            self._open_dm(n)
        elif k == "f":
            n.fav = not n.fav
            self.notify("Favourite" if n.fav else "Removed from favourites", DIM, now)
        elif k == "x":
            n.ignored = not n.ignored
            self.notify("Ignoring %s" % n.name if n.ignored else "Not ignoring", DIM, now)
        elif k == "t":
            self._node_action("traceroute", n, now, "Traceroute sent")
        elif k == "p":
            self._node_action("request_position", n, now, "Position requested")
        elif k == "n":
            self._node_action("request_nodeinfo", n, now, "Name requested")
        return None

    def _node_action(self, name, n, now, ok_text):
        fn = getattr(self.backend, name, None)
        if fn is None:
            self.notify("Not available here", YELLOW, now)
            return
        try:
            fn(n.nid)
            self.notify(ok_text, GREEN, now)
        except Exception as e:
            self.notify(str(e), RED, now)

    def render_node(self, out, cols, rows, now):
        n = self.node_view
        flags = ("  fav" if n.fav else "") + ("  ignored" if n.ignored else "")
        self._subheader2(out, cols, n.name[:cols - 16], flags.strip(), YELLOW)
        rows_data = [("ID", n.nid)]
        short = getattr(n, "short_name", "")
        if short:
            rows_data.append(("Short", short))
        role = getattr(n, "role", 0)
        rname = [r[0] for r in P.ROLES if r[1] == role]
        if getattr(n, "hw", 0):
            rows_data.append(("Hardware", "T-Deck" if n.hw == P.HW_T_DECK else "model %d" % n.hw))
        if rname:
            rows_data.append(("Role", C.nice(rname[0])))
        age = None if n.heard is None else now - n.heard
        rows_data.append(("Heard", age_str(age) + (" ago" if age is not None and age >= 10 else "")))
        if n.snr is not None:
            rows_data.append(("Signal", "%+.1f dB SNR" % n.snr))
        rows_data.append(("Path", hops_str(n.hops)))
        if getattr(n, "batt", None) is not None:
            v = getattr(n, "volt", None)
            rows_data.append(("Battery", "%d%%%s" % (n.batt, ("  %.2f V" % v) if v else "")))
        up = getattr(n, "uptime", None)
        if up:
            rows_data.append(("Uptime", age_str(up)))
        lat = getattr(n, "lat", None)
        if lat is not None:
            rows_data.append(("Position", "%.4f, %.4f" % (lat, n.lon)))
            mine = self.my_position()
            if mine:
                km, brg = distance_bearing(mine[0], mine[1], lat, n.lon)
                dist = "%d m" % int(km * 1000) if km < 1 else "%.1f km" % km
                rows_data.append(("From you", "%s %s" % (dist, _compass(brg))))
        h = rows - 4
        for i in range(h):
            if i < len(rows_data):
                k, v = rows_data[i]
                out.append(paint(4 + i, [S(FG, " "), S(DIM, ljust(k, 10)), S(FG, v)], BG, cols))
            else:
                out.append(paint(4 + i, [], BG, cols))
        out.append(paint(rows, [S(DIM, " Enter msg  f fav  x ign  t trace  p pos")], BG, cols))

    def my_position(self):
        c = getattr(self.backend, "config", None)
        if c is not None and c.pos_mode == "fixed" and (c.fixed_lat or c.fixed_lon):
            return c.fixed_lat, c.fixed_lon
        fn = getattr(self.backend, "my_position", None)
        return fn() if fn else None

    # -- overlays --

    def render_overlays(self, out, cols, rows, now=0):
        if self.mode != "chat" and self.notice and now < self.notice[2] and not self.prompt:
            out.append(paint(rows, [S(self.notice[1], " " + self.notice[0])], BG, cols))
        d = self.dialog
        if d:
            w = min(cols - 2, 34)
            left = (cols - w) // 2 + 1
            inner = w - 2
            lines = []
            for ln in d["lines"]:
                lines += wrap(ln, inner - 2)
            height = len(lines) + len(d["opts"]) + 3
            top = max(2, (rows - height) // 2)
            title = "─ %s " % d["title"]
            out.append(paint(top, [S(DIM, "┌" + title[:inner] + "─" * max(0, inner - len(title)) + "┐")],
                             BG_BAR, cols, left_col=left, width=w))
            r = top + 1
            for ln in lines:
                out.append(paint(r, [S(DIM, "│"), S(FG, ljust(" " + ln, inner)), S(DIM, "│")],
                                 BG_BAR, cols, left_col=left, width=w))
                r += 1
            for i, opt in enumerate(d["opts"]):
                sel = i == d["sel"]
                out.append(paint(r, [S(DIM, "│"),
                                     S(ACCENT if sel else FG, ljust(" " + ("> " if sel else "  ") + opt, inner)[:inner], bold=sel),
                                     S(DIM, "│")], BG_BAR, cols, left_col=left, width=w))
                r += 1
            out.append(paint(r, [S(DIM, "└" + "─" * inner + "┘")],
                             BG_BAR, cols, left_col=left, width=w))
        p = self.prompt
        if p:
            room = cols - 3 - 1
            shown = p["value"][-room:]
            out.append(paint(rows - 2, [S(ACCENT, " " + p["title"], bold=True)], BG_TAB, cols))
            out.append(paint(rows - 1, [S(ACCENT, " > ", bold=True), S(FG, shown), S(ACCENT, "_")], BG_BAR, cols))
            out.append(paint(rows, [S(DIM, " " + p["hint"])], BG, cols))
