#
# Meshtastic node settings: model, persistence, channel sharing, and the form
# schema the settings TUI renders.
# License: MIT
#

import json

import mtproto as P
import mtradio as R

PATH = "/flash/.mesh.json"
MAX_CHANNELS = 8

INTERVALS_POS = ((0, "off"), (300, "5 min"), (900, "15 min"), (1800, "30 min"),
                 (3600, "1 hour"), (7200, "2 hours"), (21600, "6 hours"))
INTERVALS_TELEM = ((0, "off"), (900, "15 min"), (1800, "30 min"), (3600, "1 hour"),
                   (10800, "3 hours"))
INTERVALS_NODEINFO = ((0, "off"), (3600, "1 hour"), (10800, "3 hours"),
                      (21600, "6 hours"), (86400, "24 hours"))
BANDWIDTHS = ((62.5, "62.5 kHz"), (125.0, "125 kHz"), (250.0, "250 kHz"), (500.0, "500 kHz"))
TCXO_CHOICES = ((None, "auto-detect"), (0.0, "none (crystal)"), (1.8, "1.8 V"),
                (2.4, "2.4 V"), (2.8, "2.8 V"), (3.3, "3.3 V"))
CURRENT_CHOICES = ((60, "60 mA"), (100, "100 mA"), (140, "140 mA"))
POS_MODES = (("off", "off"), ("gps", "GPS"), ("fixed", "fixed"))


class Channel:
    def __init__(self, name="", psk=b"\x01", uplink=False, downlink=False, precision=32):
        self.name = name
        self.psk = bytes(psk)
        self.uplink = uplink
        self.downlink = downlink
        self.precision = precision        # position precision in bits

    def to_dict(self):
        return {"name": self.name, "psk": self.psk.hex(), "up": self.uplink,
                "down": self.downlink, "prec": self.precision}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("name", ""), bytes.fromhex(d.get("psk", "01")),
                   d.get("up", False), d.get("down", False), d.get("prec", 32))


# name -> default. Everything the TUI edits is a plain attribute of Settings.
DEFAULTS = {
    "long_name": "", "short_name": "", "role": "CLIENT", "node_num": 0,
    "nodeinfo_interval": 10800,
    "region": "US", "use_preset": True, "preset": "LONG_FAST",
    "bandwidth": 250.0, "sf": 11, "cr": 5, "slot": 0, "freq_offset": 0.0,
    "override_freq": 0.0, "hop_limit": 3, "tx_enabled": True, "tx_power": 17,
    "rx_boost": False, "override_duty": False,
    "pos_mode": "off", "pos_interval": 900, "fixed_lat": 0.0, "fixed_lon": 0.0,
    "fixed_alt": 0, "pos_precision": 32,
    "telem_interval": 1800,
    "ack_channel": True,
    "tcxo": None, "dio2": True, "current_limit": 140,
    "tcxo_found": None,       # voltage that worked when tcxo is "auto"
}


class Settings:
    def __init__(self, **kw):
        for k, v in DEFAULTS.items():
            setattr(self, k, kw.get(k, v))
        self.channels = [Channel()]

    # -- derived values --

    def reg(self):
        return R.region(self.region)

    def modem(self):
        """(bandwidth kHz, SF, CR denominator) for the active preset or manual values."""
        if self.use_preset:
            p = R.preset(self.preset)
            return p[3], p[4], p[5]
        return self.bandwidth, self.sf, self.cr

    def channel_name(self, i):
        """Name used for hashing: a blank primary takes the preset's default name."""
        name = self.channels[i].name
        if name == "" and i == 0:
            return R.preset(self.preset)[2]
        return name

    def key(self, i):
        return P.expand_psk(self.channels[i].psk)

    def chan_hash(self, i):
        return P.channel_hash(self.channel_name(i), self.key(i))

    def slots(self):
        reg = self.reg()
        return R.num_slots(reg, self.modem()[0]) if reg else 1

    def frequency(self):
        reg = self.reg()
        if reg is None:
            return 0.0
        return R.frequency(reg, self.modem()[0], self.channel_name(0), self.slot,
                           self.freq_offset, self.override_freq)

    def effective_slot(self):
        if self.override_freq:
            return 0
        if 0 < self.slot <= self.slots():
            return self.slot
        return R.default_slot(self.channel_name(0), self.slots())

    def power(self):
        return R.clamp_power(self.reg(), self.tx_power)

    def radio_args(self):
        bw, sf, cr = self.modem()
        return {"freq": self.frequency(), "bw": bw, "sf": sf, "cr": cr,
                "sync_word": R.SYNC_WORD, "power": self.power(),
                "preamble": R.PREAMBLE}

    def radio_summary(self):
        reg = self.reg()
        if reg is None:
            return "Pick a region first"
        bw, sf, cr = self.modem()
        slot = self.effective_slot()
        where = "freq %.3f" % self.override_freq if not slot else "slot %d/%d" % (slot, self.slots())
        return "%s  %.3f MHz  %g kHz SF%d CR4/%d  %d dBm" % (
            where, self.frequency(), bw, sf, cr, self.power())

    def name_warning(self):
        """Non-empty when the slot depends on a preset name that is unverified."""
        if self.use_preset and self.slot == 0 and self.channels[0].name == "" \
                and self.preset not in R.NAME_VERIFIED:
            return "slot auto-pick unverified for this preset; set the slot by hand"
        return ""

    def airtime_text(self):
        bw, sf, cr = self.modem()
        return "~%d ms for 32 bytes" % R.airtime_ms(48, bw, sf, cr)

    def short_default(self):
        num = self.node_num & 0xFFFF
        return "%04x" % num

    def display_names(self):
        long_name = self.long_name or "vtOS %s" % self.short_default()
        short = (self.short_name or self.short_default())[:4]
        return long_name, short

    def problems(self):
        """Reasons these settings can't be applied (empty list when fine)."""
        out = []
        if self.reg() is None:
            out.append("Pick a region first")
        if not self.use_preset and not self.override_freq:
            reg = self.reg()
            if reg and R.num_slots(reg, self.bandwidth) < 1:
                out.append("Bandwidth doesn't fit the region")
        if self.override_freq and not (137.0 <= self.override_freq <= 960.0):
            out.append("Override must be 137-960 MHz")
        if not self.channels:
            out.append("Need at least one channel")
        for i, c in enumerate(self.channels):
            if len(c.psk) not in (0, 1, 16, 32):
                out.append("Channel %d key is the wrong length" % i)
        seen = set()
        for i in range(len(self.channels)):
            h = self.chan_hash(i)
            if h in seen:
                out.append("Channels %d clash; rename one" % i)
            seen.add(h)
        return out

    # -- persistence --

    def to_dict(self):
        d = {k: getattr(self, k) for k in DEFAULTS}
        d["channels"] = [c.to_dict() for c in self.channels]
        return d

    @classmethod
    def from_dict(cls, d):
        s = cls()
        for k in DEFAULTS:
            if k in d:
                setattr(s, k, d[k])
        chans = [Channel.from_dict(c) for c in d.get("channels", [])][:MAX_CHANNELS]
        if chans:
            s.channels = chans
        return s

    def copy(self):
        return Settings.from_dict(self.to_dict())

    def save(self, path=PATH):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f)

    @classmethod
    def load(cls, path=PATH):
        try:
            with open(path) as f:
                return cls.from_dict(json.load(f))
        except (OSError, ValueError):
            return cls()

    # -- channels and sharing --

    def add_channel(self, name="", psk=None):
        if len(self.channels) >= MAX_CHANNELS:
            raise ValueError("max %d channels" % MAX_CHANNELS)
        self.channels.append(Channel(name, psk if psk is not None else random_key(16)))
        return len(self.channels) - 1

    def delete_channel(self, i):
        if i == 0:
            raise ValueError("primary channel can't be deleted")
        del self.channels[i]

    def lora_dict(self):
        reg = self.reg()
        p = R.preset(self.preset)
        return {"use_preset": self.use_preset, "modem_preset": p[1],
                "bandwidth": int(self.bandwidth), "spread_factor": self.sf,
                "coding_rate": self.cr, "region": reg[1] if reg else 0,
                "hop_limit": self.hop_limit, "tx_enabled": self.tx_enabled,
                "channel_num": self.slot, "frequency_offset": self.freq_offset,
                "override_frequency": self.override_freq,
                "sx126x_rx_boosted_gain": self.rx_boost}

    def share_url(self, include_secondary=True):
        chans = self.channels if include_secondary else self.channels[:1]
        settings = []
        for c in chans:
            s = {"psk": c.psk, "name": c.name, "uplink_enabled": c.uplink,
                 "downlink_enabled": c.downlink}
            settings.append(s)
        return P.channel_set_url(settings, self.lora_dict())

    def import_url(self, url):
        """Replace channels and LoRa settings from a meshtastic.org/e/# URL."""
        cs = P.parse_channel_url(url)
        chans = []
        for s in cs["settings"][:MAX_CHANNELS]:
            chans.append(Channel(s["name"], s["psk"], s["uplink_enabled"],
                                 s["downlink_enabled"],
                                 s["module_settings"]["position_precision"] or 32
                                 if s["module_settings"] else 32))
        self.channels = chans
        lc = cs["lora_config"]
        if lc:
            reg = R.region_by_enum(lc["region"])
            if reg:
                self.region = reg[0]
            self.use_preset = lc["use_preset"] or not lc["bandwidth"]
            self.preset = R.preset_by_enum(lc["modem_preset"])[0]
            if lc["bandwidth"]:
                self.bandwidth = float(lc["bandwidth"])
                self.sf = lc["spread_factor"] or self.sf
                self.cr = lc["coding_rate"] or self.cr
            self.hop_limit = min(7, max(1, lc["hop_limit"] or 3))
            self.slot = lc["channel_num"]
            self.freq_offset = lc["frequency_offset"]
            self.override_freq = lc["override_frequency"]
        return len(chans)


def random_key(nbytes):
    import os
    return os.urandom(nbytes)


# --- form schema for the settings TUI -----------------------------------------
# Each field: key, label, kind (enum/int/float/bool/text/info/action), extras,
# help. `when` hides a field. The UI edits attributes on a Settings copy.

def _opts(pairs):
    return list(pairs)


def _region_opts():
    return [(r[0], r[0]) for r in R.REGIONS]


def _preset_opts():
    return [(p[0], p[2]) for p in R.PRESETS]


def nice(name):
    """CLIENT_MUTE -> "Client Mute" (MicroPython has no str.title)."""
    return " ".join(w[:1] + w[1:].lower() for w in name.split("_"))


ROLE_OPTS = [(name, nice(name)) for name, _ in P.ROLES]

SECTIONS = (
    ("Radio", (
        {"key": "radio_summary", "kind": "info", "label": "Now using"},
        {"key": "region", "label": "Region", "kind": "enum", "options": _region_opts,
         "help": "Sets frequency band, power cap and duty cycle. Must be legal where you are."},
        {"key": "use_preset", "label": "Use preset", "kind": "bool",
         "help": "Off lets you pick bandwidth, spreading factor and coding rate by hand."},
        {"key": "preset", "label": "Modem preset", "kind": "enum", "options": _preset_opts,
         "when": lambda c: c.use_preset,
         "help": "LongFast is the default everyone starts on. Nodes must share the preset."},
        {"key": "bandwidth", "label": "Bandwidth", "kind": "enum", "options": lambda: list(BANDWIDTHS),
         "when": lambda c: not c.use_preset, "help": "Wider is faster, narrower reaches further."},
        {"key": "sf", "label": "Spread factor", "kind": "int", "range": (7, 12, 1),
         "when": lambda c: not c.use_preset, "help": "Each step up roughly doubles range and halves speed."},
        {"key": "cr", "label": "Coding rate 4/", "kind": "int", "range": (5, 8, 1),
         "when": lambda c: not c.use_preset, "help": "Higher means more error correction."},
        {"key": "slot", "label": "Frequency slot", "kind": "int", "range": (0, "slots", 1),
         "help": "0 picks a slot from the primary channel name. Set the same slot on every node to be sure."},
        {"key": "hop_limit", "label": "Max hops", "kind": "int", "range": (1, 7, 1),
         "help": "How many relays your messages may pass through. 3 is the default; more floods the mesh."},
        {"key": "tx_enabled", "label": "Transmit", "kind": "bool",
         "help": "Off makes this a receive-only node."},
        {"key": "tx_power", "label": "TX power dBm", "kind": "int", "range": (1, 22, 1),
         "help": "Capped by region and by the radio (22 dBm). Lower saves battery."},
        {"key": "rx_boost", "label": "RX boost gain", "kind": "bool",
         "help": "A few dB more sensitivity for slightly more receive current."},
        {"key": "freq_offset", "label": "Freq offset MHz", "kind": "float", "range": (-0.1, 0.1, 0.001),
         "help": "Corrects a crystal that is slightly off. Leave at 0 unless you know you need it."},
        {"key": "override_freq", "label": "Override MHz", "kind": "float", "range": (0.0, 960.0, 0.025),
         "help": "0 uses the region and slot. Anything else forces that exact frequency: you own the legality."},
        {"key": "override_duty", "label": "Ignore duty cycle", "kind": "bool",
         "help": "Only for regions without a duty-cycle rule. Illegal in the EU."},
    )),
    ("Device", (
        {"key": "long_name", "label": "Long name", "kind": "text", "max": 39,
         "help": "Shown to other nodes in their node list."},
        {"key": "short_name", "label": "Short name", "kind": "text", "max": 4,
         "help": "Up to 4 characters."},
        {"key": "role", "label": "Role", "kind": "enum", "options": lambda: ROLE_OPTS,
         "help": "Client rebroadcasts; Client Mute stays quiet; Router/Repeater relay with priority."},
        {"key": "nodeinfo_interval", "label": "Announce name", "kind": "enum",
         "options": lambda: list(INTERVALS_NODEINFO), "help": "How often your name is broadcast."},
        {"key": "ack_channel", "label": "Confirm channel msgs", "kind": "bool",
         "help": "Retry channel messages until a relay is heard. Costs airtime."},
    )),
    ("Position", (
        {"key": "pos_mode", "label": "Position", "kind": "enum", "options": lambda: list(POS_MODES),
         "help": "GPS needs a T-Deck Plus. Fixed shares the coordinates you type."},
        {"key": "pos_interval", "label": "Share every", "kind": "enum",
         "options": lambda: list(INTERVALS_POS), "when": lambda c: c.pos_mode != "off",
         "help": "How often your position goes out."},
        {"key": "fixed_lat", "label": "Latitude", "kind": "float", "range": (-90.0, 90.0, 0.0001),
         "when": lambda c: c.pos_mode == "fixed", "help": "Decimal degrees, e.g. 37.7749."},
        {"key": "fixed_lon", "label": "Longitude", "kind": "float", "range": (-180.0, 180.0, 0.0001),
         "when": lambda c: c.pos_mode == "fixed", "help": "Decimal degrees, e.g. -122.4194."},
        {"key": "fixed_alt", "label": "Altitude m", "kind": "int", "range": (-500, 9000, 1),
         "when": lambda c: c.pos_mode == "fixed", "help": "Metres above sea level."},
        {"key": "pos_precision", "label": "Precision bits", "kind": "int", "range": (10, 32, 1),
         "when": lambda c: c.pos_mode != "off",
         "help": "32 is exact; 13 is roughly 1 km; 10 is about 5 km. Lower hides your exact spot."},
    )),
    ("Telemetry", (
        {"key": "telem_interval", "label": "Device metrics", "kind": "enum",
         "options": lambda: list(INTERVALS_TELEM),
         "help": "Battery, voltage and channel use, broadcast on this schedule."},
    )),
    ("Advanced", (
        {"key": "tcxo", "label": "Radio TCXO", "kind": "enum", "options": lambda: list(TCXO_CHOICES),
         "help": "Auto tries the usual voltages when the radio starts, then remembers what worked."},
        {"key": "dio2", "label": "DIO2 RF switch", "kind": "bool",
         "help": "Lets the radio chip drive the antenna switch. On for the T-Deck."},
        {"key": "current_limit", "label": "PA current limit", "kind": "enum",
         "options": lambda: list(CURRENT_CHOICES), "help": "Needs 140 mA for full 22 dBm output."},
        {"key": "reset", "label": "Reset all", "kind": "action",
         "help": "Restores every setting, keeps nothing. Channels return to the default LongFast key."},
    )),
)


def field_visible(field, cfg):
    when = field.get("when")
    return when is None or when(cfg)


def options_of(field, cfg=None):
    opts = field["options"]
    return opts() if callable(opts) else opts
