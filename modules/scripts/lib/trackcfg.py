"""Trackball sensitivity settings, kept in /flash/.trackball (one name=value
per line). The values live in this module, so changing one with the `ball`
command takes effect at once for the shell and for `vnc`."""

PATH = "/flash/.trackball"

# name: (default, lowest, highest, what it does)
SPECS = {
    "move": (2, 1, 8, "pulses before the shell moves the cursor or scrolls (lower = more sensitive)"),
    "gain": (4, 1, 16, "vnc pointer pixels per pulse (higher = faster pointer)"),
}

_values = None


def _defaults():
    return {name: spec[0] for name, spec in SPECS.items()}


def load(path=None):
    """(Re)read the file; unknown names, bad numbers and out-of-range values
    are ignored, so a damaged file can never break the trackball."""
    global _values
    values = _defaults()
    try:
        with open(path or PATH) as f:
            text = f.read()
    except OSError:
        text = ""
    for line in text.split("\n"):
        name, sep, raw = line.partition("=")
        name = name.strip()
        if sep and name in SPECS:
            try:
                number = int(raw.strip())
            except ValueError:
                continue
            if SPECS[name][1] <= number <= SPECS[name][2]:
                values[name] = number
    _values = values
    return values


def get(name):
    if _values is None:
        load()
    return _values[name]


def check(name, number):
    """Return the value as an int, or raise ValueError saying what is wrong."""
    if name not in SPECS:
        raise ValueError("unknown setting '%s' (use: %s)" % (name, ", ".join(sorted(SPECS))))
    try:
        number = int(number)
    except ValueError:
        raise ValueError("%s must be a whole number" % name)
    if not SPECS[name][1] <= number <= SPECS[name][2]:
        raise ValueError("%s must be %d to %d" % (name, SPECS[name][1], SPECS[name][2]))
    return number


def save(path=None):
    with open(path or PATH, "w") as f:
        for name in sorted(SPECS):
            f.write("%s=%d\n" % (name, get(name)))


def set(name, number, path=None):
    number = check(name, number)
    if _values is None:
        load(path)
    _values[name] = number
    save(path)
    return number


def reset(path=None):
    global _values
    _values = _defaults()
    save(path)
