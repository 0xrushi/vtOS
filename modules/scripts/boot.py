import os
import sys
import machine
import micropython

import env
import hardware
import modvt
import tdeck_kvm
import trackball
import keyrepeat
import statusbar
import shell
import defaults


env = env.Environment(320, 240, "terminus_mpy_14", "siji_mpy_statusbar_12")

hardware.init_flash()
defaults.apply()
hardware.init_board()
hardware.init_spi(env)
tft = hardware.init_tft(env)
env.tft = tft   # apps that draw directly (yt) use this; set env.sd_busy to pause the terminal
kbd = hardware.init_keyboard()

# Initialize ST engine
env.term = modvt.VT(tft, env)
env.term.top_offset(env.status_height)
env.term.set_icon_font(env.icon_font)


# Combine ST & keyboard into one stream object
env.kvm = tdeck_kvm.KVM(env.term, kbd)

# Redirect to REPL
os.dupterm(env.kvm)
keyrepeat.install(env)

# Write the MicroPython prompt in red
sys.stdout.write("\x1b[38;5;210m")

# Status bar component
env.sts = statusbar.StatusBar(env)
env.sts.refresh()

import quickpanel
quickpanel.install(env, tft)

import clipd
import httpd

# The FAST loop (30ms)
def scheduled_fast(_):
    trackball.poll(env)
    clipd.tick(env)
    httpd.tick(env)
    if env.quickpanel is not None:
        env.quickpanel.poll()

    # SD transfers park the shared SPI bus at SD_BAUDRATE. Pixel apps
    # draw the area below the status bar themselves.
    if not env.sd_busy and env.app_display is None:
        env.term.draw()

def fast_loop(_):
    # Use schedule to keep the ISR (Interrupt Service Routine) light.
    # schedule() raises RuntimeError if its queue is already full (e.g.
    # mp_task is blocked in some other call and hasn't drained the
    # previous tick yet) -- letting that escape uncaught from a hard-IRQ
    # timer callback is unsafe, so just drop this tick instead.
    try:
        micropython.schedule(scheduled_fast, 0)
    except RuntimeError:
        pass

# The SLOW loop (1000ms)
def scheduled_slow(_):
    # refresh() itself checks env.sd_busy before touching the display --
    # see statusbar.py.
    env.sts.refresh()

def slow_loop(_):
    # Update the status bar string (ANSI parsing happens here)
    try:
        micropython.schedule(scheduled_slow, 0)
    except RuntimeError:
        pass

# 30ms = ~33 FPS.
draw_timer = machine.Timer(0)
draw_timer.init(
    period=30,
    mode=machine.Timer.PERIODIC,
    callback=fast_loop)

statusbar_timer = machine.Timer(1)
statusbar_timer.init(
    period=1000,
    mode=machine.Timer.PERIODIC,
    callback=slow_loop)

# Choose your cursor:

# Beam Cursor
sys.stdout.write("\x1b[ 6 q")

# Underline Cursor
#sys.stdout.write("\x1b[ 4 q")

# Block Cursor (default)
#sys.stdout.write("\x1b[ 2 q")

class Cmd:
    def __init__(self, func):
        self.func = func
    def __repr__(self):
        self.func()
        return ""

env.shell = shell.Shell(env)

import wificonfig
wificonfig.start_at_boot()

# Phone clipboard share: binds lazily once Wi-Fi is up (see clipd.poll()).
try:
    if clipd.load_config()["autostart"]:
        clipd.start(env)
except Exception:
    pass

# HTTP file server (httpd): same lazy bind as clipd.
try:
    if httpd.load_config()["autostart"]:
        httpd.start(env)
except Exception:
    pass

# quick way to return to the shell from MicroPython
# just type `sh` into MicroPython to return to our shell
sh = Cmd(env.shell.run)

# Inject / "run" our shell (we do it this way so we get the MicroPython banner / shell first)
env.kvm.inject("sh\r")
