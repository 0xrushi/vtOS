#
# Meshchat -- sober Meshtastic chat UI: state, input handling and rendering.
# License: MIT
#
# Pure logic with no hardware imports, so it runs on the host under pytest
# (see utils/tests/test_meshui.py) as well as on the T-Deck. The app in
# bin/meshchat.py only reads keys, polls the clock and writes frames.
#
# Everything is laid out from (cols, rows), so a font change via `fc`
# (40x16 Terminus 14 up to 64x29 Spleen 8) just reflows. The font has ASCII
# plus the box-drawing set only: no arrows, bullets or block glyphs.
#
# A backend (lib/meshback.py) supplies events and takes outgoing text:
#   poll() -> [("msg", conv_key, node_id, name, text, ts),
#              ("ack", message_id, ok), ("node",)]
#   send(conv_key, text) -> message_id
#   nodes() -> [Node], channels() -> [(conv_key, title)], info() -> dict
#

import time

# Palette (xterm-256): quiet greys, one soft-blue accent, colour only for state.
from meshdraw import (BG, BG_BAR, BG_TAB, BG_SEL, RULE, FG, DIM, ACCENT, GREEN, YELLOW, RED,
                      SENDER_COLORS, S, seg_len, clip, paint, sanitize, wrap, age_str,
                      clock_ok, stamp, name_color, snr_color, hops_str, ljust, rjust)
import meshradar
import meshset

MAX_INPUT = 200   # Meshtastic text payloads top out around 228 bytes
MAX_MSGS = 100    # per conversation, RAM only
GROUP_GAP = 300   # seconds of silence before a sender header repeats
NOTICE_SECS = 4

COMMANDS = ("nodes", "radar", "chats", "ch", "dm", "node", "trace", "pos", "announce", "fav",
            "settings", "channels", "info", "stats", "clear", "help", "quit")
MENU_ITEMS = ("Nodes", "Radar", "Chats", "Channels", "Settings", "Info", "Stats", "Help", "Quit")

HELP_LINES = (
    ("Enter", "send"),
    ("Left/Right", "switch chat"),
    ("Up/Down", "scroll"),
    ("Click", "menu / back"),
    ("Hold click", "quit"),
    ("/nodes", "who is on the mesh"),
    ("/radar", "nodes around you"),
    ("/chats", "all conversations"),
    ("/ch N", "go to chat N"),
    ("/dm name", "direct message"),
    ("/node name", "node details"),
    ("/trace name", "traceroute"),
    ("/pos", "share position now"),
    ("/announce", "share your name now"),
    ("/fav name", "toggle favourite"),
    ("/settings", "radio, channels, device"),
    ("/channels", "channels and keys"),
    ("/stats", "radio and mesh counters"),
    ("/info", "this node"),
    ("/clear", "clear this chat"),
    ("/quit", "leave"),
)


# --- model -------------------------------------------------------------------

class Msg:
    def __init__(self, sid, name, text, ts, mine=False, state="", kind="msg", meta=""):
        self.meta = meta
        self.sid = sid
        self.name = name
        self.text = text
        self.ts = ts
        self.mine = mine
        self.state = state    # "" | "sending" | "sent" | "ok" | "fail"  (own messages)
        self.kind = kind      # "msg" | "sys"


class Conv:
    def __init__(self, key, title, kind="ch"):
        self.key = key
        self.title = title
        self.kind = kind      # "ch" channel | "dm" direct
        self.msgs = []
        self.unread = 0
        self.scroll = 0       # display lines hidden below the view
        self.newer = 0        # messages that arrived while scrolled up
        self.version = 0
        self._ck = None
        self._lines = []
        self._total = 0

    def add(self, msg):
        self.msgs.append(msg)
        if len(self.msgs) > MAX_MSGS:
            del self.msgs[0]
        self.version += 1


def build_lines(conv, width, now, wall):
    """Conversation -> display lines [(right_aligned, [segs])]."""
    lines = []
    prev = None
    prev_ts = 0
    body = max(width - 4, 8)
    for m in conv.msgs:
        if m.kind == "sys":
            lines.append((False, [S(DIM, " " + m.text)]))
            prev = None
            continue
        color = ACCENT if m.mine else name_color(m.sid)
        if m.sid != prev or m.ts - prev_ts > GROUP_GAP:
            who = "You" if m.mine else m.name
            head = [S(color, who, bold=True), S(DIM, "  " + stamp(m.ts, now, wall))]
            if m.meta and not m.mine:
                head.append(S(DIM, "  " + m.meta))
            if not m.mine:
                head.insert(0, S(FG, " "))
            lines.append((m.mine, head))
        prev = m.sid
        prev_ts = m.ts
        if m.mine:
            mark, mcol = {"sending": ("...", YELLOW), "sent": ("sent", DIM),
                          "ok": ("ok", GREEN), "fail": ("!", RED)}.get(m.state, ("", DIM))
            parts = wrap(m.text, body - 5)
            for i, part in enumerate(parts):
                segs = [S(ACCENT, part)]
                if i == len(parts) - 1 and mark:
                    segs.append(S(mcol, " " + mark))
                lines.append((True, segs))
        else:
            for part in wrap(m.text, body):
                lines.append((False, [S(FG, " " + part)]))
    return lines


class Node:
    """Duck-typed by the UI; backends may use their own class."""

    def __init__(self, nid, name, hops=None, snr=None, heard=None, batt=None):
        self.nid = nid
        self.name = name
        self.hops = hops
        self.snr = snr
        self.heard = heard
        self.batt = batt


# --- application -------------------------------------------------------------

class App(meshset.SettingsMixin, meshradar.RadarMixin):
    def __init__(self, backend, now=None):
        self.backend = backend
        self.convs = {}
        self.order = []
        self.pending = {}
        self.nodes = []
        self.input = ""
        self.mode = "chat"        # chat | menu | nodes | chats | page
        self.sel = 0              # selection in menu / lists
        self.page = None          # (title, [segs lines])
        self.page_scroll = 0
        self.notice = None        # (text, colour, expires)
        self.dirty = True
        self.info = {}
        self._info_at = None
        self.page_back = None
        self.set_init()
        self.radar_init()
        for key, title in backend.channels():
            self._add_conv(key, title, "ch")
        self.cur = self.order[0] if self.order else None
        self._refresh(now if now is not None else time.time())

    # -- model plumbing --

    def _add_conv(self, key, title, kind):
        conv = self.convs.get(key)
        if conv is None:
            conv = Conv(key, title, kind)
            self.convs[key] = conv
            self.order.append(key)
        return conv

    def _refresh(self, now):
        self.info = self.backend.info()
        self._info_at = now
        nodes = list(self.backend.nodes())
        nodes.sort(key=lambda n: (not getattr(n, "fav", False), -(n.heard or 0)))
        self.nodes = nodes

    def conv(self):
        return self.convs[self.cur]

    def commands(self):
        return COMMANDS + ("discover",) if hasattr(self.backend, "discover") else COMMANDS

    def menu_items(self):
        items = MENU_ITEMS
        if hasattr(self.backend, "discover"):
            items = items[:-1] + ("Discover nodes",) + items[-1:]
        return items

    def notify(self, text, color=DIM, now=None):
        if now is None:
            now = time.time()
        self.notice = (text, color, now + NOTICE_SECS)
        self.dirty = True

    def poll(self, now):
        """Drain backend events. Returns True when the screen needs a redraw."""
        for ev in self.backend.poll():
            kind = ev[0]
            if kind == "msg":
                self._on_msg(ev)
            elif kind == "ack":
                hit = self.pending.get(ev[1])
                if hit:
                    if ev[2] == "sent":
                        hit[1].state = "sent"
                    else:
                        hit[1].state = "ok" if ev[2] else "fail"
                        del self.pending[ev[1]]
                    hit[0].version += 1
                if len(self.pending) > 60:
                    del self.pending[next(iter(self.pending))]
            elif kind == "node":
                self._refresh(now)
            elif kind == "notice":
                self.notify(ev[1], ev[2] if len(ev) > 2 else DIM, now)
            self.dirty = True
        if self._info_at is None or now - self._info_at >= 10:
            self._refresh(now)
            self.dirty = True
        if self.notice and now >= self.notice[2]:
            self.notice = None
            self.dirty = True
        dirty = self.dirty
        self.dirty = False
        return dirty

    def sync_channels(self):
        """Re-read the backend's channel list after settings changed."""
        chans = self.backend.channels()
        keys = [k for k, _ in chans]
        for k, title in chans:
            self._add_conv(k, title, "ch").title = title
        for k in list(self.order):
            if k.startswith("ch:") and k not in keys:
                del self.convs[k]
                self.order.remove(k)
        if self.cur not in self.convs:
            self.cur = self.order[0]

    def _on_msg(self, ev):
        key, nid, name, text, ts = ev[1:6]
        meta = ev[6] if len(ev) > 6 else ""
        title = name if key.startswith("dm:") else key
        conv = self._add_conv(key, title, "dm" if key.startswith("dm:") else "ch")
        conv.add(Msg(nid, name, sanitize(text), ts, meta=meta))
        if key == self.cur and self.mode == "chat":
            if conv.scroll > 0:
                conv.newer += 1
        else:
            conv.unread += 1

    # -- input --

    def key(self, k, now):
        """Handle one key. Returns "quit" to leave the app, else None."""
        self.dirty = True
        if self.prompt:
            return self.key_prompt(k, now)
        if self.dialog:
            return self.key_dialog(k, now)
        if self.mode == "menu":
            return self._key_menu(k, now)
        if self.mode in ("nodes", "chats"):
            return self._key_list(k, now)
        if self.mode == "page":
            return self._key_page(k)
        if self.mode == "settings":
            return self.key_settings(k, now)
        if self.mode == "form":
            return self.key_form(k, now)
        if self.mode == "chlist":
            return self.key_chlist(k, now)
        if self.mode == "node":
            return self.key_node(k, now)
        if self.mode == "radar":
            return self.key_radar(k, now)
        return self._key_chat(k, now)

    def _key_chat(self, k, now):
        conv = self.conv()
        if k == "ESC":
            self.mode = "menu"
            self.sel = 0
        elif k == "LEFT":
            self._switch(-1)
        elif k == "RIGHT":
            self._switch(1)
        elif k == "UP":
            conv.scroll = min(conv.scroll + 1, max(0, conv._total - 1))
        elif k == "DOWN":
            conv.scroll = max(0, conv.scroll - 1)
            if conv.scroll == 0:
                conv.newer = 0
        elif k in ("\r", "\n"):
            return self._submit(now)
        elif k in ("\x08", "\x7f"):
            self.input = self.input[:-1]
        elif len(k) == 1 and 32 <= ord(k) <= 126 and len(self.input) < MAX_INPUT:
            self.input += k
        return None

    def _switch(self, delta):
        i = self.order.index(self.cur)
        self.cur = self.order[(i + delta) % len(self.order)]
        self.conv().unread = 0

    def _goto_conv(self, key):
        self.cur = key
        self.conv().unread = 0
        self.mode = "chat"

    def _submit(self, now):
        text = self.input.strip()
        if not text:
            return None
        self.input = ""
        if text.startswith("/"):
            return self._command(text, now)
        conv = self.conv()
        msg = Msg("me", "You", sanitize(text), now, mine=True, state="sending")
        try:
            mid = self.backend.send(conv.key, msg.text)
            self.pending[mid] = (conv, msg)
        except Exception as e:
            msg.state = "fail"
            self.notify("Send failed: %s" % e, RED, now)
        conv.add(msg)
        conv.scroll = 0
        conv.newer = 0
        return None

    def _command(self, text, now):
        parts = text[1:].split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("quit", "q", "exit"):
            return "quit"
        if cmd == "nodes":
            self._open_list("nodes")
        elif cmd == "discover":
            self.discover(now)
        elif cmd == "radar":
            self.radar_open()
        elif cmd == "chats":
            self._open_list("chats")
        elif cmd == "info":
            self._open_page("info", now)
        elif cmd == "help":
            self._open_page("help", now)
        elif cmd == "clear":
            conv = self.conv()
            conv.msgs = []
            conv.version += 1
            conv.scroll = 0
            conv.newer = 0
            self.notify("Chat cleared", DIM, now)
        elif cmd == "ch":
            if arg.isdigit() and 1 <= int(arg) <= len(self.order):
                self._goto_conv(self.order[int(arg) - 1])
            else:
                self.notify("Usage: /ch 1-%d" % len(self.order), RED, now)
        elif cmd == "dm":
            if not arg:
                self._open_list("nodes")
            else:
                node = self.find_node(arg)
                if node is None:
                    self.notify("No node '%s'" % arg[:16], RED, now)
                else:
                    self._open_dm(node)
        elif cmd in ("node", "trace", "fav"):
            node = self.find_node(arg) if arg else None
            if node is None:
                self.notify("No node '%s'" % arg[:16] if arg else "Usage: /%s name" % cmd, RED, now)
            elif cmd == "node":
                self.open_node(node)
            elif cmd == "trace":
                self._node_action("traceroute", node, now, "Traceroute sent")
            else:
                node.fav = not node.fav
                self.notify("Favourite" if node.fav else "Removed from favourites", DIM, now)
        elif cmd in ("pos", "announce"):
            fn = getattr(self.backend, "share_position" if cmd == "pos" else "announce", None)
            if fn is None:
                self.notify("Not available in this mode", YELLOW, now)
            else:
                ok, msg = fn()
                self.notify(msg, GREEN if ok else RED, now)
        elif cmd == "settings":
            self.set_open(now)
        elif cmd == "channels":
            self.set_open(now, "chlist")
        elif cmd == "stats":
            self._open_page("stats", now)
        else:
            self.notify("Unknown command: /%s" % cmd[:16], RED, now)
        return None

    def discover(self, now):
        fn = getattr(self.backend, "discover", None)
        if fn is None:
            self.notify("Discovery unavailable in this mode", YELLOW, now)
            return
        ok, msg = fn()
        self._open_list("nodes")
        self.notify(msg, GREEN if ok else YELLOW, now)

    def find_node(self, name):
        q = name.lower()
        for test in (lambda n: n.lower() == q,
                     lambda n: n.lower().startswith(q),
                     lambda n: q in n.lower()):
            for node in self.nodes:
                if test(node.name):
                    return node
        return None

    def _open_dm(self, node):
        conv = self._add_conv("dm:" + str(node.nid), node.name, "dm")
        self._goto_conv(conv.key)

    def _open_list(self, which):
        self.mode = which
        self.sel = 0

    def _open_page(self, which, now):
        self.mode = "page"
        self.page_scroll = 0
        if which == "help":
            extra = (("/discover", "ask nearby nodes for names"),) if hasattr(self.backend, "discover") else ()
            lines = [[S(ACCENT, "%-11s" % k, bold=True), S(FG, v)]
                     for k, v in HELP_LINES + extra]
            self.page = ("Help", lines)
            return
        if which == "stats":
            self._stats_page()
            return
        info = self.info
        batt = info.get("batt")
        rows = [("Name", info.get("name", "?")),
                ("Region", info.get("region", "?")),
                ("Preset", info.get("preset", "?")),
                ("Battery", "%d%%" % batt if batt is not None else "--"),
                ("Nodes", str(len(self.nodes))),
                ("Link", "demo" if info.get("demo") else info.get("link", "ok"))]
        lines = [[S(DIM, "%-9s" % k), S(FG, v)] for k, v in rows]
        lines.append([S(FG, "")])
        for i, key in enumerate(self.order):
            conv = self.convs[key]
            lines.append([S(DIM, "%-9s" % ("Chat %d" % (i + 1) if i == 0 else "")),
                          S(FG, "%d  %s" % (i + 1, conv.title))])
        self.page = ("Info", lines)

    def _stats_page(self):
        st = self.backend.stats() if hasattr(self.backend, "stats") else {}
        lines = []
        for k, v in st.items():
            lines.append([S(DIM, "%-13s" % k), S(FG, str(v))])
        if not lines:
            lines = [[S(DIM, "No counters in this mode")]]
        self.page = ("Stats", lines)

    def _key_menu(self, k, now):
        items = self.menu_items()
        n = len(items)
        if k == "ESC":
            self.mode = "chat"
        elif k == "UP":
            self.sel = (self.sel - 1) % n
        elif k == "DOWN":
            self.sel = (self.sel + 1) % n
        elif k in ("\r", "\n"):
            item = items[self.sel]
            if item == "Quit":
                return "quit"
            if item == "Nodes":
                self._open_list("nodes")
            elif item == "Discover nodes":
                self.discover(now)
            elif item == "Radar":
                self.radar_open()
            elif item == "Chats":
                self._open_list("chats")
            elif item == "Channels":
                self.set_open(now, "chlist")
            elif item == "Settings":
                self.set_open(now)
            elif item == "Info":
                self._open_page("info", now)
            elif item == "Stats":
                self._open_page("stats", now)
            elif item == "Help":
                self._open_page("help", now)
        return None

    def _key_list(self, k, now):
        items = self.nodes if self.mode == "nodes" else self.order
        n = len(items)
        if k == "ESC":
            self.mode = "chat"
        elif k == "UP" and n:
            self.sel = (self.sel - 1) % n
        elif k == "DOWN" and n:
            self.sel = (self.sel + 1) % n
        elif self.mode == "nodes" and k == "d":
            self.discover(now)
        elif k in ("\r", "\n") and n:
            if self.mode == "nodes":
                self._open_dm(items[self.sel])
            else:
                self._goto_conv(items[self.sel])
        elif self.mode == "nodes" and n and k in ("i", "RIGHT"):
            self.open_node(items[self.sel])
        elif self.mode == "nodes" and n and k == "f":
            node = items[self.sel]
            node.fav = not node.fav
            self.nodes.sort(key=lambda x: (not x.fav, -(x.heard or 0)))
        elif self.mode == "nodes" and n and k == "x":
            items[self.sel].ignored = not items[self.sel].ignored
        return None

    def _key_page(self, k):
        if k in ("ESC", "\r", "\n"):
            back = self.page_back
            self.page_back = None
            self.mode = back or "chat"
        elif k == "UP":
            self.page_scroll = max(0, self.page_scroll - 1)
        elif k == "DOWN":
            self.page_scroll += 1
        return None

    # -- rendering --

    def render(self, cols, rows, now):
        """The whole screen as one escape-sequence string."""
        wall = clock_ok(now)
        out = ["\x1b[?25l"]
        out.append(paint(1, self._header(cols), BG_BAR, cols))
        if self.mode in ("nodes", "chats"):
            self._render_list(out, cols, rows, now)
        elif self.mode == "page":
            self._render_page(out, cols, rows)
        elif self.mode == "settings":
            self.render_settings(out, cols, rows)
        elif self.mode == "form":
            self.render_form(out, cols, rows, now)
        elif self.mode == "chlist":
            self.render_chlist(out, cols, rows)
        elif self.mode == "node":
            self.render_node(out, cols, rows, now)
        elif self.mode == "radar":
            self.render_radar(out, cols, rows, now)
        else:
            self._render_chat(out, cols, rows, now, wall)
            if self.mode == "menu":
                self._render_menu(out, cols, rows)
        self.render_overlays(out, cols, rows, now)
        return "".join(out)

    def _rule(self, row, cols):
        return paint(row, [S(RULE, "─" * cols)], BG, cols)

    def _header(self, cols):
        info = self.info
        left = [S(ACCENT, " Meshchat", bold=True)]
        if info.get("preset"):
            left.append(S(DIM, "  " + info["preset"]))
        if info.get("demo"):
            left.append(S(YELLOW, "  demo"))
        count = len(self.nodes)
        noun = "node" if count == 1 else "nodes"
        batt = info.get("batt")
        variants = []
        link = info.get("link", "ok")
        if link != "ok":
            variants.append([S(RED, link + " ")])
        else:
            if batt is not None:
                bc = RED if batt < 20 else (YELLOW if batt < 40 else FG)
                variants.append([S(FG, "%d %s" % (count, noun)), S(FG, "  "),
                                 S(bc, "%d%%" % batt), S(FG, " ")])
            variants.append([S(FG, "%d %s " % (count, noun))])
        variants.append([])
        right = []
        for cand in variants:
            if seg_len(left) + seg_len(cand) + 1 <= cols:
                right = cand
                break
        gap = max(0, cols - seg_len(left) - seg_len(right))
        return left + [S(FG, " " * gap)] + right

    def _tabs(self, cols):
        items = []
        for key in self.order:
            conv = self.convs[key]
            if key == self.cur:
                items.append([S(BG, " " + conv.title[:14] + " ", bg=ACCENT, bold=True)])
            else:
                col = FG if conv.unread else DIM
                segs = [S(col, " " + conv.title[:14])]
                if conv.unread:
                    segs.append(S(YELLOW, " %d" % min(conv.unread, 99)))
                segs.append(S(col, " "))
                items.append(segs)
        widths = [seg_len(s) for s in items]
        active = self.order.index(self.cur)
        avail = cols - 2
        start = 0
        while start < active and sum(widths[start:active + 1]) > avail:
            start += 1
        segs = [S(DIM, "<" if start > 0 else " ")]
        used = 0
        end = start
        while end < len(items) and used + widths[end] <= avail:
            segs += items[end]
            used += widths[end]
            end += 1
        segs.append(S(FG, " " * (avail - used)))
        segs.append(S(DIM, ">" if end < len(items) else " "))
        return segs

    def _hint(self, now):
        if self.notice and now < self.notice[2]:
            return [S(self.notice[1], " " + self.notice[0])]
        if self.input.startswith("/") and " " not in self.input:
            word = self.input[1:]
            hits = ["/" + c for c in self.commands() if c.startswith(word)]
            return [S(ACCENT, " " + (" ".join(hits) if hits else "no such command"))]
        return [S(DIM, " click menu  < > chats  /help")]

    def _render_chat(self, out, cols, rows, now, wall):
        conv = self.conv()
        h = max(1, rows - 6)
        out.append(paint(2, self._tabs(cols), BG_TAB, cols))
        out.append(self._rule(3, cols))

        lines = self._lines(conv, cols, now, wall)
        total = len(lines)
        conv.scroll = min(conv.scroll, max(0, total - h))
        if conv.scroll == 0:
            conv.newer = 0
        end = total - conv.scroll
        visible = lines[max(0, end - h):end]
        rows_out = [None] * (h - len(visible)) + visible
        if conv.scroll > 0:
            note = "-- %d new, down to read --" % conv.newer if conv.newer else "-- scrolled, down for newest --"
            rows_out[-1] = (False, [S(DIM, " " + note)])
        for i in range(h):
            row = 4 + i
            item = rows_out[i]
            if item is None:
                if not conv.msgs and i == h // 2:
                    out.append(paint(row, [S(DIM, " No messages yet. Type below, Enter to send.")], BG, cols))
                else:
                    out.append(paint(row, [], BG, cols))
            else:
                out.append(paint(row, item[1], BG, cols, right=item[0]))

        out.append(self._rule(4 + h, cols))
        out.append(self._composer(5 + h, cols))
        out.append(paint(6 + h, self._hint(now), BG, cols))

    def _lines(self, conv, cols, now, wall):
        key = (cols, conv.version, 0 if wall else int(now // 60))
        if conv._ck != key:
            if conv._ck is None or conv._ck[0] != cols:
                conv.scroll = 0
                conv._total = 0
            conv._lines = build_lines(conv, cols, now, wall)
            if conv.scroll > 0:
                conv.scroll += len(conv._lines) - conv._total
                conv.scroll = max(conv.scroll, 0)
            conv._total = len(conv._lines)
            conv._ck = key
        return conv._lines

    def _composer(self, row, cols):
        extra = []
        if len(self.input) >= MAX_INPUT - 40:
            extra = [S(YELLOW if len(self.input) < MAX_INPUT else RED,
                       " %d " % (MAX_INPUT - len(self.input)))]
        room = max(1, cols - 4 - seg_len(extra))
        shown = self.input[-room:]
        segs = [S(ACCENT, " > ", bold=True), S(FG, shown), S(ACCENT, "_")]
        gap = max(0, cols - seg_len(segs) - seg_len(extra))
        return paint(row, segs + [S(FG, " " * gap)] + extra, BG_BAR, cols)

    def _render_menu(self, out, cols, rows):
        items = self.menu_items()
        w = min(cols - 2, 18)
        left = (cols - w) // 2 + 1
        top = max(1, (rows - (len(items) + 2)) // 2)
        inner = w - 2
        title = "─ Menu "
        out.append(paint(top, [S(DIM, "┌" + title + "─" * (inner - len(title)) + "┐")],
                         BG_BAR, cols, left_col=left, width=w))
        for i, item in enumerate(items):
            sel = i == self.sel
            body = ("> " if sel else "  ") + item
            out.append(paint(top + 1 + i,
                             [S(DIM, "│"),
                              S(ACCENT if sel else FG, " " + ljust(body, inner - 1), bold=sel),
                              S(DIM, "│")],
                             BG_BAR, cols, left_col=left, width=w))
        out.append(paint(top + 1 + len(items),
                         [S(DIM, "└" + "─" * inner + "┘")],
                         BG_BAR, cols, left_col=left, width=w))

    def _subheader(self, out, cols, left, right=""):
        segs = [S(ACCENT, " " + left, bold=True)]
        gap = cols - seg_len(segs) - len(right) - 1
        if gap >= 1 and right:
            segs += [S(FG, " " * gap), S(DIM, right + " ")]
        out.append(paint(2, segs, BG_TAB, cols))
        out.append(self._rule(3, cols))

    def _render_list(self, out, cols, rows, now):
        nodes = self.mode == "nodes"
        items = self.nodes if nodes else self.order
        if nodes:
            self._subheader(out, cols, "Nodes (%d)" % len(items), "by last heard")
        else:
            self._subheader(out, cols, "Chats (%d)" % len(items))
        h = max(1, rows - 4)
        self.sel = min(self.sel, max(0, len(items) - 1))
        top = 0
        if self.sel >= h:
            top = self.sel - h + 1
        for i in range(h):
            idx = top + i
            row = 4 + i
            if idx >= len(items):
                msg = []
                if i == 0 and not items:
                    msg = [S(DIM, " Listening for nodes..." if nodes and hasattr(self.backend, "discover")
                             else " Nothing here yet.")]
                out.append(paint(row, msg, BG, cols))
                continue
            sel = idx == self.sel
            segs = self._node_row(items[idx], sel, cols, now) if nodes \
                else self._chat_row(self.convs[items[idx]], sel, cols)
            out.append(paint(row, segs, BG_SEL if sel else BG, cols))
        hint = " Enter dm  i info  f fav  x ignore" if nodes else " Enter open  click back"
        if nodes and hasattr(self.backend, "discover"):
            hint = " Enter dm  i info  d discover"
        out.append(paint(rows, [S(DIM, hint)], BG, cols))

    def _node_row(self, n, sel, cols, now):
        wide = cols >= 53
        fixed = 1 + 2 + 1 + 6 + 1 + 8 + 1 + 4 + 1
        if wide:
            fixed += 9 + 1 + 4 + 1
        namew = max(6, cols - fixed)
        age = None if n.heard is None else now - n.heard
        snr = "--" if n.snr is None else "%+.1f dB" % n.snr
        flag = "*" if getattr(n, "fav", False) else ("x" if getattr(n, "ignored", False) else " ")
        namew -= 1
        segs = [S(FG, " "), S(ACCENT, "> " if sel else "  "), S(YELLOW if flag == "*" else DIM, flag),
                S(DIM if getattr(n, "ignored", False) else FG, ljust(n.name[:namew], namew), bold=sel), S(FG, " ")]
        if wide:
            segs += [S(DIM, ljust(str(n.nid)[:9], 9)), S(FG, " ")]
        segs += [S(DIM, ljust(hops_str(n.hops), 6)), S(FG, " "),
                 S(snr_color(n.snr), rjust(snr, 8)), S(FG, " ")]
        if wide:
            segs += [S(DIM, rjust("%d%%" % n.batt if n.batt is not None else "--", 4)), S(FG, " ")]
        segs.append(S(DIM, rjust(age_str(age), 4)))
        return segs

    def _chat_row(self, conv, sel, cols):
        namew = min(14, max(6, cols // 3))
        badge = (" (%d)" % min(conv.unread, 99)) if conv.unread else ""
        room = cols - 3 - namew - 1 - len(badge) - 1
        preview = ""
        if conv.msgs:
            last = conv.msgs[-1]
            who = "You" if last.mine else last.name
            preview = "%s: %s" % (who, last.text.replace("\n", " "))
        segs = [S(FG, " "), S(ACCENT, "> " if sel else "  "),
                S(FG if (sel or conv.unread) else DIM, ljust(conv.title[:namew], namew), bold=sel),
                S(FG, " "), S(DIM, ljust(preview[:max(room, 0)], max(room, 0)))]
        if badge:
            segs.append(S(YELLOW, badge))
        return segs

    def _render_page(self, out, cols, rows):
        title, lines = self.page
        self._subheader(out, cols, title)
        h = max(1, rows - 4)
        self.page_scroll = min(self.page_scroll, max(0, len(lines) - h))
        for i in range(h):
            idx = self.page_scroll + i
            segs = [S(FG, " ")] + lines[idx] if idx < len(lines) else []
            out.append(paint(4 + i, segs, BG, cols))
        hint = " click back" if len(lines) <= h else " up/down scroll  click back"
        out.append(paint(rows, [S(DIM, hint)], BG, cols))
