#
# Meshtastic radio parameters: modem presets, regions, frequency slots, airtime.
# License: MIT
#
# Sources: meshtastic.org/docs/overview/radio-settings and
# /docs/configuration/radio/lora (docs 2.8) for presets, region limits and the
# US LongFast = slot 20 = 906.875 MHz worked example. The slot hash (djb2 of
# the primary channel name modulo the slot count) is checked against that
# example and against the EU slot numbers in the same docs in the tests.
#

SYNC_WORD = 0x2B
PREAMBLE = 16

# (name, protobuf enum, default channel name, bandwidth kHz, SF, CR denominator)
# Default channel names other than LongFast are from the firmware as remembered,
# not from the docs: they only matter when slot auto-pick is used, so the UI
# marks them and lets the user set the slot by hand.
PRESETS = (
    ("LONG_FAST", 0, "LongFast", 250.0, 11, 5),
    ("LONG_MODERATE", 7, "LongMod", 125.0, 11, 8),
    ("MEDIUM_FAST", 4, "MediumFast", 250.0, 9, 5),
    ("MEDIUM_SLOW", 3, "MediumSlow", 250.0, 10, 5),
    ("SHORT_FAST", 6, "ShortFast", 250.0, 7, 5),
    ("SHORT_SLOW", 5, "ShortSlow", 250.0, 8, 5),
    ("SHORT_TURBO", 8, "ShortTurbo", 500.0, 7, 5),
    ("LONG_SLOW", 1, "LongSlow", 125.0, 12, 8),
    ("VERY_LONG_SLOW", 2, "VLongSlow", 62.5, 12, 8),
)
NAME_VERIFIED = ("LONG_FAST",)

# (name, enum, start MHz, end MHz, power limit dBm, duty cycle %)
REGIONS = (
    ("US", 1, 902.0, 928.0, 30, 100),
    ("EU_433", 2, 433.0, 434.0, 10, 10),
    ("EU_868", 3, 869.4, 869.65, 27, 10),
    ("CN", 4, 470.0, 510.0, 19, 100),
    ("JP", 5, 920.5, 923.5, 13, 100),
    ("ANZ", 6, 915.0, 928.0, 30, 100),
    ("KR", 7, 920.0, 923.0, 23, 100),
    ("TW", 8, 920.0, 925.0, 27, 100),
    ("RU", 9, 868.7, 869.2, 20, 100),
    ("IN", 10, 865.0, 867.0, 30, 100),
    ("NZ_865", 11, 864.0, 868.0, 36, 100),
    ("TH", 12, 920.0, 925.0, 27, 10),
    ("UA_433", 14, 433.0, 434.7, 10, 10),
    ("MY_433", 16, 433.0, 435.0, 20, 100),
    ("MY_919", 17, 919.0, 924.0, 27, 100),
    ("SG_923", 18, 917.0, 925.0, 20, 100),
)

HW_MAX_POWER = 22      # SX1262 PA ceiling on the T-Deck


def preset(name):
    for p in PRESETS:
        if p[0] == name:
            return p
    return PRESETS[0]


def preset_by_enum(num):
    for p in PRESETS:
        if p[1] == num:
            return p
    return PRESETS[0]


def region(name):
    for r in REGIONS:
        if r[0] == name:
            return r
    return None


def region_by_enum(num):
    for r in REGIONS:
        if r[1] == num:
            return r
    return None


def djb2(text):
    h = 5381
    for b in text.encode():
        h = ((h << 5) + h + b) & 0xFFFFFFFF
    return h


def num_slots(reg, bw_khz):
    """How many channel slots of this bandwidth fit in the region."""
    return max(1, int((reg[3] - reg[2]) * 1000.0 / bw_khz + 1e-6))


def default_slot(name, slots):
    """1-based slot picked from the primary channel name when none is set."""
    return djb2(name) % slots + 1


def frequency(reg, bw_khz, channel_name, slot=0, offset_mhz=0.0, override=0.0):
    """Centre frequency in MHz. slot 0 = pick from the channel name hash."""
    if override:
        return override + offset_mhz
    n = num_slots(reg, bw_khz)
    if slot <= 0 or slot > n:
        slot = default_slot(channel_name, n)
    return reg[2] + bw_khz / 2000.0 + (slot - 1) * bw_khz / 1000.0 + offset_mhz


def airtime_ms(nbytes, bw_khz, sf, cr_den, preamble=PREAMBLE):
    """Time on air of one explicit-header, CRC-on LoRa frame (Semtech formula)."""
    tsym = (1 << sf) / bw_khz                   # ms  (2^SF / BW)
    de = 1 if tsym > 16.0 else 0
    num = 8 * nbytes - 4 * sf + 28 + 16
    den = 4 * (sf - 2 * de)
    blocks = max(0, -(-num // den))
    payload_syms = 8 + blocks * cr_den
    return (preamble + 4.25 + payload_syms) * tsym


def clamp_power(reg, tx_power):
    """Legal transmit power: user setting limited by region and hardware."""
    limit = min(reg[4] if reg else HW_MAX_POWER, HW_MAX_POWER)
    if tx_power <= 0:
        return limit
    return min(tx_power, limit)
