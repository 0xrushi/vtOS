# Trackball sensitivity: show or change the settings in /flash/.trackball
#
#   ball                 show the settings
#   ball move 1          shell: pulses before the cursor/scroll moves
#   ball gain 8          vnc: pointer speed
#   ball reset           back to the defaults

import trackcfg


def usage():
    print("Usage: ball [move|gain <n> | reset]")
    for name in sorted(trackcfg.SPECS):
        default, low, high, text = trackcfg.SPECS[name]
        print("  %-5s %d-%d (default %d)  %s" % (name, low, high, default, text))


def show():
    for name in sorted(trackcfg.SPECS):
        print("%-5s %d" % (name, trackcfg.get(name)))


def main(env, args):
    args = list(args)
    try:
        if not args:
            show()
        elif args[0] in ("-h", "--help", "help"):
            usage()
        elif args[0] == "reset" and len(args) == 1:
            trackcfg.reset()
            show()
        elif len(args) == 2:
            trackcfg.set(args[0], args[1])
            show()
        else:
            usage()
    except ValueError as err:
        print("ball: " + str(err))
    except OSError as err:
        print("ball: cannot save settings: %s" % (err,))
