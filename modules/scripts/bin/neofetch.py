#
# MicroPython neofetch-style system summary
# License: MIT
#
# A small handheld logo on the left, system details on the right.
#

import sys

ACCENT = 85
LABEL = 110
TEXT = 252
DIM = 244

LOGO = [
    "  .---------.",
    "  |.-------.|",
    "  ||  vt   ||",
    "  ||  OS   ||",
    "  |'-------'|",
    "  | q w e r |",
    "  | a s d f |",
    "  '---------'",
]
LOGO_WIDTH = 14


def human(n):
    """Byte count -> short text: 512B, 31K, 3.2M, 1.5G."""
    if n < 1024:
        return "%dB" % n
    if n < 1024 * 1024:
        return "%dK" % (n // 1024)
    if n < 1024 * 1024 * 1024:
        tenths = (n * 10) // (1024 * 1024)
        return "%d.%dM" % (tenths // 10, tenths % 10)
    tenths = (n * 10) // (1024 * 1024 * 1024)
    return "%d.%dG" % (tenths // 10, tenths % 10)


def uptime_text(seconds):
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "%dd %dh %dm" % (d, h, m)
    if h:
        return "%dh %dm %ds" % (h, m, s)
    return "%dm %ds" % (m, s)


def usage_text(used, total):
    if total <= 0:
        return "n/a"
    return "%s/%s %d%%" % (human(used), human(total), (used * 100) // total)


def _attempt(func, default=None):
    try:
        return func()
    except Exception:
        return default


def _disk(path):
    import os
    st = os.statvfs(path)
    total = st[1] * st[2]
    free = st[0] * st[3]
    return usage_text(total - free, total)


def collect(env):
    """[(label, value)] for everything that can be read on this device."""
    import gc
    import os
    info = []

    uname = _attempt(os.uname)
    machine_name = "T-Deck"
    if uname is not None:
        machine_name = uname[4].split(" with ")[0].replace("LilyGo ", "LilyGO ")
        info.append(("OS", "vtOS mpy-%s" % uname[2]))
    info.append(("Host", machine_name))

    sts = getattr(env, "sts", None)
    start = getattr(sts, "start_ticks", None)
    if start is not None:
        def up():
            import time
            return uptime_text(time.ticks_diff(time.ticks_ms(), start) // 1000)
        uptime = _attempt(up)
        if uptime:
            info.append(("Uptime", uptime))

    def cpu():
        import machine
        text = "S3 %dMHz" % (machine.freq() // 1000000)
        temp = _attempt(lambda: __import__("esp32").mcu_temperature())
        if temp is not None:
            text += " %dC" % round(temp)
        return text
    info.append(("CPU", _attempt(cpu, "ESP32-S3")))

    def heap():
        gc.collect()
        used, free = gc.mem_alloc(), gc.mem_free()
        return usage_text(used, used + free)
    info.append(("Heap", _attempt(heap, "n/a")))

    def psram():
        import esp32
        total = used_p = 0
        for region in esp32.idf_heap_info(esp32.HEAP_SPIRAM):
            total += region[0]
            used_p += region[0] - region[1]
        return usage_text(used_p, total) if total else None
    ps = _attempt(psram)
    if ps:
        info.append(("PSRAM", ps))

    info.append(("Flash", _attempt(lambda: _disk("/flash"), "n/a")))
    sd = _attempt(lambda: _disk("/sd"))
    if sd and getattr(env, "sd", None) is not None:
        info.append(("SD", sd))

    def battery():
        pin = env.sts.bat_pin
        pct = ((pin.read_u16() * 660) // 65535) - 320
        return "%d%%" % max(0, min(100, pct))
    bat = _attempt(battery)
    if bat:
        info.append(("Batt", bat))

    def network():
        import network as net
        wlan = net.WLAN(net.STA_IF)
        if not wlan.isconnected():
            return "offline"
        return wlan.ifconfig()[0]
    info.append(("Net", _attempt(network, "n/a")))

    def ssid():
        import network as net
        wlan = net.WLAN(net.STA_IF)
        return wlan.config("essid") if wlan.isconnected() else None
    name = _attempt(ssid)
    if name:
        info.append(("SSID", name))

    info.append(("Term", "%dx%d chars" % (env.cols, env.rows)))
    shell = getattr(env, "shell", None)
    if shell is not None:
        info.append(("Cmds", "%d apps" % len(shell.apps)))
    return info


def _pad(text, width):
    # MicroPython's str has no ljust().
    return text + " " * (width - len(text))


def _clip(text, width):
    return text if len(text) <= width else text[:max(width - 1, 0)] + "~"


def render(info, cols, title="vtos@tdeck"):
    """Full neofetch screen as a list of terminal lines (ANSI colours)."""
    show_logo = cols >= 36
    gap = LOGO_WIDTH if show_logo else 0
    width = cols - gap - 1
    label_w = max([len(k) for k, _ in info] + [4]) + 1

    right = []
    right.append("\x1b[1;38;5;%dm%s\x1b[0m" % (ACCENT, _clip(title, width)))
    right.append("\x1b[38;5;%dm%s\x1b[0m" % (DIM, "-" * min(len(title), width)))
    for label, value in info:
        value = _clip(value, width - label_w)
        right.append("\x1b[1;38;5;%dm%s\x1b[0;38;5;%dm%s\x1b[0m" % (
            LABEL, _pad(label, label_w), TEXT, value))
    right.append("")
    swatches = "".join("\x1b[48;5;%dm  " % c for c in (0, 1, 2, 3, 4, 5, 6, 7))
    right.append(swatches + "\x1b[0m")

    lines = []
    for i in range(max(len(right), len(LOGO) if show_logo else 0)):
        left = ""
        if show_logo:
            art = LOGO[i] if i < len(LOGO) else ""
            left = "\x1b[38;5;%dm%s\x1b[0m" % (ACCENT, _pad(art, LOGO_WIDTH))
        lines.append(left + (right[i] if i < len(right) else ""))
    return lines


def main(env, args):
    lines = render(collect(env), env.cols)
    sys.stdout.write("\r\n".join(lines) + "\r\n")
