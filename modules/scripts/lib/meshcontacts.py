"""Persistent MeshChat address book and its keyboard UI. License: MIT."""
import json
import os

from meshdraw import (BG, BG_SEL, FG, DIM, ACCENT, GREEN, YELLOW, RED,
                      S, paint, sanitize, wrap)

PATH = "/flash/.meshcontacts.json"
MAX_CONTACTS = 200
FIELDS = (("name", "Name", 40), ("nid", "Node ID", 64),
          ("email", "Email (optional)", 254), ("note", "Note (optional)", 240))
ACTIONS = ("Message", "Save changes", "View details", "Delete contact")


def protocol(backend):
    if getattr(backend, "meshcore", False):
        return "meshcore"
    if getattr(backend, "demo", False):
        return "demo"
    return "meshtastic" if hasattr(backend, "config") else "raw"


def clean(row):
    if not isinstance(row, dict):
        raise ValueError("Invalid contact")
    p = row.get("protocol", "meshtastic")
    if p not in ("meshtastic", "meshcore", "raw", "demo"):
        raise ValueError("Invalid contact mode")
    out = {"protocol": p}
    for key, label, limit in FIELDS:
        val = row.get(key, "")
        if not isinstance(val, str) or len(val) > limit:
            raise ValueError("%s is too long or invalid" % label)
        if key != "note" and any(ord(c) < 32 or ord(c) == 127 for c in val):
            raise ValueError("%s contains invalid characters" % label)
        out[key] = val.strip()
    if not out["name"]:
        raise ValueError("Enter a contact name")
    nid = out["nid"]
    if p != "raw":
        nid = nid.lower().lstrip("!")
        size = 64 if p == "meshcore" else 8
        if len(nid) != size or any(c not in "0123456789abcdef" for c in nid):
            raise ValueError("Node ID needs %d hex digits" % size)
        if p != "meshcore":
            if int(nid, 16) in (0, 0xffffffff):
                raise ValueError("Use an individual node ID")
            nid = "!" + nid
    elif not nid or any(ord(c) < 33 or ord(c) > 126 for c in nid):
        raise ValueError("Enter the raw LoRa nickname")
    out["nid"] = nid
    return out


class Store:
    def __init__(self, path=PATH):
        self.path = path
        self.rows = []
        self.error = None
        try:
            with open(path) as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unknown contacts format")
            rows = data.get("contacts")
            if not isinstance(rows, list) or len(rows) > MAX_CONTACTS:
                raise ValueError("Invalid contacts list")
            result = [clean(r) for r in rows]
            keys = [(r["protocol"], r["nid"]) for r in result]
            if len(set(keys)) != len(keys):
                raise ValueError("Duplicate contact node IDs")
            self.rows = result
        except OSError as e:
            if e.args and e.args[0] != 2:
                self.error = "Cannot read contacts"
        except (ValueError, TypeError):
            self.error = "Contacts file is damaged"

    def find(self, mode, nid):
        for r in self.rows:
            if r["protocol"] == mode and r["nid"] == nid:
                return r
        return None

    def _save(self, rows):
        if self.error:
            raise ValueError(self.error)
        temp = self.path + ".tmp"
        try:
            with open(temp, "w") as f:
                json.dump({"version": 1, "contacts": rows}, f)
            if hasattr(os, "sync"):
                os.sync()
            # Check the complete write before replacing the saved address book.
            with open(temp) as f:
                if json.load(f) != {"version": 1, "contacts": rows}:
                    raise ValueError("Contacts write was incomplete")
            os.rename(temp, self.path)
        except (OSError, ValueError):
            try:
                os.remove(temp)
            except OSError:
                pass
            raise
        self.rows = rows
        if hasattr(os, "sync"):
            os.sync()

    def put(self, row, original=None):
        row = clean(row)
        key = (row["protocol"], row["nid"])
        if any((r["protocol"], r["nid"]) == key and
               (r["protocol"], r["nid"]) != original for r in self.rows):
            raise ValueError("That node is already a contact")
        rows = [r for r in self.rows if (r["protocol"], r["nid"]) != original]
        if len(rows) >= MAX_CONTACTS:
            raise ValueError("Contacts list is full")
        rows.append(row)
        self._save(rows)
        return row

    def delete(self, key):
        self._save([r for r in self.rows if (r["protocol"], r["nid"]) != key])


class ContactsMixin:
    def contacts_init(self, path=None):
        self.address_book = Store(PATH if path is None else path)
        self.contact_edit = None
        self.contact_original = None
        self.contact_sel = 0

    def contact_rows(self):
        rows = [r for r in self.address_book.rows if r["protocol"] == protocol(self.backend)]
        return sorted(rows, key=lambda r: (r["name"].lower(), r["nid"]))

    def contacts_open(self, now):
        self.mode = "contacts"
        self.sel = 0
        if self.address_book.error:
            self.notify(self.address_book.error, RED, now)

    def contact_alias(self, nid, default):
        row = self.address_book.find(protocol(self.backend), str(nid))
        return sanitize(row["name"]) if row else default

    def contact_open(self, row=None, node=None):
        if node is not None:
            nid = str(node.nid)
            row = self.address_book.find(protocol(self.backend), nid)
            if row is None:
                row = {"protocol": protocol(self.backend), "nid": nid, "name": node.name,
                       "email": "", "note": ""}
        self.contact_edit = dict(row) if row else {"protocol": protocol(self.backend),
                                                  "name": "", "nid": "", "email": "", "note": ""}
        saved = self.address_book.find(self.contact_edit["protocol"], self.contact_edit["nid"])
        if saved:
            self.contact_edit = dict(saved)
        self.contact_original = dict(saved) if saved else None
        self.contact_sel = 0
        self.mode = "contact"

    def contact_save(self, now):
        original = self.contact_original
        key = (original["protocol"], original["nid"]) if original else None
        try:
            row = self.address_book.put(self.contact_edit, key)
        except (OSError, ValueError) as e:
            self.notify("Could not save: %s" % e, RED, now)
            return False
        self.contact_edit = dict(row)
        self.contact_original = dict(row)
        conv = self.convs.get("dm:" + row["nid"])
        if conv:
            conv.title = row["name"]
        self.notify("Contact saved", GREEN, now)
        return True

    def contact_message(self, row, now):
        if getattr(self.backend, "meshcore", False):
            if not any(str(n.nid) == row["nid"] for n in self.nodes):
                self.notify("Import this MeshCore contact first", YELLOW, now)
                return
        conv = self._add_conv("dm:" + row["nid"], row["name"], "dm")
        conv.title = row["name"]
        self._goto_conv(conv.key)

    def contact_leave(self, now):
        if self.contact_edit == self.contact_original or not any(
                self.contact_edit[k] for k, _, _ in FIELDS):
            self.contacts_open(now)
            return
        def choice(i):
            if i == 0:
                if self.contact_save(now):
                    self.contacts_open(now)
            elif i == 1:
                self.contacts_open(now)
        self.ask("Unsaved contact", ["Save your changes?"],
                 ["Save", "Discard", "Keep editing"], choice)

    def key_contacts(self, k, now):
        rows = self.contact_rows()
        if k == "ESC":
            self.mode = "chat"
        elif k == "a":
            self.contact_open()
        elif rows:
            self.sel = min(self.sel, len(rows) - 1)
            if k == "UP":
                self.sel = (self.sel - 1) % len(rows)
            elif k == "DOWN":
                self.sel = (self.sel + 1) % len(rows)
            elif k in ("\r", "\n"):
                self.contact_open(rows[self.sel])
            elif k == "m":
                self.contact_message(rows[self.sel], now)

    def key_contact(self, k, now):
        if k == "ESC":
            self.contact_leave(now)
        elif k == "UP":
            self.contact_sel = (self.contact_sel - 1) % (len(FIELDS) + len(ACTIONS))
        elif k == "DOWN":
            self.contact_sel = (self.contact_sel + 1) % (len(FIELDS) + len(ACTIONS))
        elif k in ("\r", "\n"):
            i = self.contact_sel
            if i < len(FIELDS):
                key, label, limit = FIELDS[i]
                def edited(value):
                    self.contact_edit[key] = value
                self.ask_text(label, self.contact_edit[key], edited, limit, fresh=True)
            else:
                action = ACTIONS[i - len(FIELDS)]
                if action == "Save changes":
                    self.contact_save(now)
                elif action == "Message":
                    if self.contact_edit == self.contact_original or self.contact_save(now):
                        self.contact_message(self.contact_edit, now)
                elif action == "View details":
                    lines = []
                    for key, label, _ in FIELDS:
                        lines.append([S(ACCENT, label)])
                        lines.extend([[S(FG, t)] for t in wrap(sanitize(self.contact_edit[key]) or "--", 28)])
                    self.page = ("Contact details", lines)
                    self.page_scroll = 0
                    self.page_back = "contact"
                    self.mode = "page"
                elif action == "Delete contact":
                    if not self.contact_original:
                        self.notify("This contact is not saved yet", DIM, now)
                        return
                    key = (self.contact_original["protocol"], self.contact_original["nid"])
                    def delete(i):
                        if i == 0:
                            try:
                                self.address_book.delete(key)
                            except (OSError, ValueError) as e:
                                self.notify("Could not delete: %s" % e, RED, now)
                                return
                            self.contacts_open(now)
                            self.notify("Contact deleted", GREEN, now)
                    self.ask("Delete contact", [sanitize(self.contact_original["name"])],
                             ["Delete", "Cancel"], delete)

    def render_contacts(self, out, cols, rows):
        items = self.contact_rows()
        self._subheader(out, cols, "Contacts (%d)" % len(items))
        h = max(1, rows - 4)
        self.sel = min(self.sel, max(0, len(items) - 1))
        first = max(0, self.sel - h + 1)
        for i in range(h):
            idx = first + i
            segs = []
            if idx < len(items):
                row = items[idx]
                segs = [S(ACCENT, "> " if idx == self.sel else "  "),
                        S(FG, sanitize(row["name"])), S(DIM, "  " + sanitize(row["nid"]))]
            elif not items and i == 0:
                segs = [S(DIM, " No contacts yet. Press a to add.")]
            out.append(paint(4 + i, segs, BG_SEL if idx == self.sel and items else BG, cols))
        out.append(paint(rows, [S(DIM, " a add  Enter edit  m message")], BG, cols))

    def render_contact(self, out, cols, rows):
        self._subheader(out, cols, "Contact", "local address book")
        values = [label + ": " + (self.contact_edit[key] or "--") for key, label, _ in FIELDS]
        values += list(ACTIONS)
        if self.contact_edit != self.contact_original:
            values[5] += " *"
        h = max(1, rows - 4)
        first = max(0, self.contact_sel - h + 1)
        for i in range(h):
            idx = first + i
            selected = idx == self.contact_sel
            segs = [S(ACCENT, "> " if selected else "  "), S(FG, sanitize(values[idx]).replace("\n", " "))] if idx < len(values) else []
            out.append(paint(4 + i, segs, BG_SEL if selected else BG, cols))
        out.append(paint(rows, [S(DIM, " Enter edit/select  click back")], BG, cols))
