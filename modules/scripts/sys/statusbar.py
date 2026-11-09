#
# MicroPython StatusBar
# Copyright (c) 2026 8bitmcu
# License: MIT
#

import time
import network
import gc
import hardware

# Siji characters
UPTIME = chr(0xE015).encode()
MEM    = chr(0xE020).encode()
WIFI   = chr(0xE048).encode()
BT     = chr(0xE00B).encode()
SPKR   = chr(0xE152).encode()
BAT    = chr(0xE033).encode()
CLIP   = chr(0xE0C1).encode()  # hand-drawn clipboard, see fonts/siji_mpy_statusbar_12.py

# Palette (xterm-256), shared with the launcher: a quiet dark bar, soft
# per-item accent colours, and traffic-light state colours. The dynamic
# colours are all 3 digits so they can be swapped in place in the buffer.
BAR_BG  = 235
TEXT    = 252
DIM     = 244
C_UP    = 110   # uptime icon: soft blue
C_MEM   = 179   # memory icon: amber
GREEN   = b"114"
YELLOW  = b"221"
RED     = b"203"
GREY    = b"244"


def _fg(code):
    return b"\033[38;5;" + (code if isinstance(code, bytes) else str(code).encode()) + b"m"


def _battery_color(pct):
    if pct >= 50:
        return GREEN
    if pct >= 20:
        return YELLOW
    return RED


class StatusBar:

    def __init__(self, env):
        self.env = env  # needed for env.audio.volume() -- audio is optional/lazy
        self.start_ticks = time.ticks_ms()
        self.wlan = network.WLAN(network.STA_IF)
        self.bat_pin = hardware.init_bat()

        self.style = _fg(TEXT) + ("\033[48;5;%dm" % BAR_BG).encode()
        self.clear = b"\033[0m"

        # Notification overlay state -- see notify().
        self._notify_text = None
        self._notify_until = 0
        # Dark text on a soft blue (#87afd7): clearly distinct from the
        # bar, readable, and not an alarming red for routine events such
        # as "sshd: connected".
        self._notify_style = _fg(BAR_BG) + b"\033[48;5;110m"

        # Initialize the layout and calculate offsets dynamically via the new method
        self.update_width(env.cols)

    def update_width(self, width):
        """Regenerates the layout template and recalculates internal offsets dynamically."""
        self.width = width

        # Build left/right incrementally instead of writing out a fixed
        # byte string and reverse-engineering offsets from it -- with
        # mixed narrow ASCII (1 byte, 1 column) and wide icons (3 bytes,
        # 2 columns), that arithmetic doesn't reduce to a single len()
        # anywhere, so it's tracked explicitly as each piece is appended.
        segments = []
        offsets = {}
        cols = 0

        def text(data):
            nonlocal cols
            segments.append(data)
            cols += len(data)

        def icon(data):
            nonlocal cols
            segments.append(data)
            cols += 2  # WIDE glyph: 2 terminal columns regardless of UTF-8 byte length

        def field(name, placeholder):
            offsets[name] = sum(len(s) for s in segments)
            text(placeholder)

        def color(code):
            # Zero-width: colours take bytes, not terminal columns.
            segments.append(_fg(code))

        def cfield(name, default):
            # A colour whose 3-digit code is rewritten in place on refresh.
            segments.append(b"\033[38;5;")
            offsets[name] = sum(len(s) for s in segments)
            segments.append(default + b"m")

        # Left: uptime, mem, wifi
        color(C_UP)
        icon(UPTIME)
        color(TEXT)
        field('up_min', b'00')   # minutes, 2 digits (was 1 -- wrapped at 10m before)
        color(DIM)
        text(b'm')
        color(TEXT)
        field('up_sec', b'00')   # seconds, 2 digits
        text(b' ')
        color(C_MEM)
        icon(MEM)
        color(TEXT)
        field('mem', b'0.0M')  # digit/./digit/unit below 10, digit/digit/unit at 10+ (no decimal then)
        text(b' ')
        cfield('wifi_c', GREY)
        icon(WIFI)
        field('wifi', b'OFF')

        left = b''.join(segments)
        left_cols = cols

        # Right: clipboard service (only when there is room), bluetooth
        # (ON when the BLE mesh transport is active -- see env.ble /
        # hardware.init_ble), speaker volume, battery
        for with_clip in (True, False):
            segments = []
            cols = 0
            if with_clip:
                cfield('clip_c', GREY)
                icon(CLIP)
                text(b' ')
            cfield('ble_c', GREY)
            icon(BT)
            field('ble', b'OFF')
            text(b' ')
            cfield('vol_c', GREY)
            icon(SPKR)
            field('vol', b'  0')
            color(DIM)
            text(b'% ')
            cfield('bat_c', GREEN)
            icon(BAT)
            field('bat', b'  0')
            color(DIM)
            text(b'%')
            if left_cols + cols <= width:
                break
            offsets.pop('clip_c', None)

        right = b''.join(segments)
        right_cols = cols

        # Build the middle padding dynamically to fill up to the new target width
        pad_len = width - left_cols - right_cols
        if pad_len < 0:
            pad_len = 0
        mid_pad = b' ' * pad_len

        # Reallocate the bytearray buffer matching the new dimensions
        self.buffer = bytearray(self.style + left + mid_pad + right + self.clear + b"\x00")

        # Absolute offsets into self.buffer for each dynamic field
        s_len = len(self.style)
        right_base = s_len + len(left) + pad_len
        self.off_up_min = s_len + offsets['up_min']
        self.off_up_sec = s_len + offsets['up_sec']
        self.off_mem    = s_len + offsets['mem']
        self.off_wifi   = s_len + offsets['wifi']
        self.off_ble    = right_base + offsets['ble']
        self.off_vol    = right_base + offsets['vol']
        self.off_bat    = right_base + offsets['bat']
        self.off_wifi_c = s_len + offsets['wifi_c']
        self.off_ble_c  = right_base + offsets['ble_c']
        self.off_clip_c = right_base + offsets['clip_c'] if 'clip_c' in offsets else None
        self.off_vol_c  = right_base + offsets['vol_c']
        self.off_bat_c  = right_base + offsets['bat_c']

    def _write_at(self, offset, data):
        """Helper to overwrite a slice of the buffer"""
        self.buffer[offset : offset + len(data)] = data

    def notify(self, text):
        """Takes over the status bar with `text` (on a red background)
        for 5 seconds, then reverts to the normal display. Renders
        immediately -- refresh() only runs on the 1Hz status bar timer,
        which would otherwise mean up to a 1s delay before a notification
        actually appeared. The normal 1Hz tick then handles reverting
        once the 5s window is up. (If the SD card happens to be mid
        transfer right now, refresh() defers to it and this notification
        just shows up on that next 1Hz tick instead -- still recorded
        below either way.)"""
        if not text:
            return
        panel = getattr(self.env, "quickpanel", None)
        if panel is not None:
            panel.notify(text)
        self._notify_text = text
        self._notify_until = time.ticks_add(time.ticks_ms(), 5000)
        self.refresh()

    def _render_notification(self):
        text = self._notify_text
        if isinstance(text, str):
            text = text.encode()

        # Fixed-width row like the normal buffer -- draw_bar_ansi()
        # expects exactly self.width columns. Assumes plain ASCII (no
        # wide icon glyphs), so byte length == column count here.
        if len(text) >= self.width:
            text = text[:self.width]
        else:
            pad = self.width - len(text)
            left_pad = pad // 2
            text = b' ' * left_pad + text + b' ' * (pad - left_pad)

        self.env.term.top_bar(self._notify_style + text + self.clear + b"\x00")

    def refresh(self):
        # top_bar() (below and in _render_notification()) writes straight
        # to the display over the SPI bus shared with the SD card
        # (top_bar() -> draw_bar_ansi() -> xdrawline() in fb.c), bypassing
        # env.term.draw()'s buffered path entirely. Skip this refresh
        # while a SD transfer has the bus parked at SD_BAUDRATE, rather
        # than asserting TFT_CS while SDCARD_CS may still be held low
        # mid-transaction. notify() below still records the pending
        # notification either way -- it just won't render immediately,
        # and shows up on the next refresh() (1Hz tick at worst) instead.
        if self.env.sd_busy:
            return

        if self._notify_text is not None:
            if time.ticks_diff(self._notify_until, time.ticks_ms()) > 0:
                self._render_notification()
                return
            self._notify_text = None
            # Falls through to redraw the normal status bar immediately
            # below, rather than waiting for the next 1Hz tick.

        # Gather raw data (Integer math only to avoid float allocations)
        total_sec = time.ticks_diff(time.ticks_ms(), self.start_ticks) // 1000
        mins = (total_sec // 60) % 100  # clamp to the 2-digit field, wraps at 100m now (was 10m)
        secs = total_sec % 60

        # Scaled integer math for battery percentage
        pct = hardware.bat_value(self.bat_pin)

        # 1. Memory Calculation
        mem_free = gc.mem_free()

        if mem_free >= 1048576: # 1024 * 1024
            mem_scaled = (mem_free * 10) // 1048576
            val = mem_scaled // 10
            dec = mem_scaled % 10
            if val > 99:
                val = 99  # clamp -- 2-digit field, and no MicroPython heap on this board gets near 100M
            unit = 77                 # ASCII 'M'

            if val < 10:
                # Single digit: keep the decimal, no leading blank needed
                self.buffer[self.off_mem]     = 48 + val
                self.buffer[self.off_mem + 1] = 46
                self.buffer[self.off_mem + 2] = 48 + dec
                self.buffer[self.off_mem + 3] = unit
            else:
                # Two digits: both preserved, decimal dropped to fit the
                # field (never blanks/truncates an actual digit)
                self.buffer[self.off_mem]     = 48 + (val // 10)
                self.buffer[self.off_mem + 1] = 48 + (val % 10)
                self.buffer[self.off_mem + 2] = unit
                self.buffer[self.off_mem + 3] = 32
        else:
            mem_k = mem_free // 1024
            mh = (mem_k // 100) % 10
            mt = (mem_k // 10) % 10
            mu = mem_k % 10

            # Always zero-padded -- no leading blanks, so the value hugs
            # the icon instead of leaving a gap
            self.buffer[self.off_mem]     = 48 + mh
            self.buffer[self.off_mem + 1] = 48 + mt
            self.buffer[self.off_mem + 2] = 48 + mu
            self.buffer[self.off_mem + 3] = 75 # ASCII 'K'

        # Uptime MMmSS -- both minute digits now dynamic (was 1 digit, wrapped at 10m)
        self.buffer[self.off_up_min]     = 48 + (mins // 10)
        self.buffer[self.off_up_min + 1] = 48 + (mins % 10)
        self.buffer[self.off_up_sec]     = 48 + (secs // 10)
        self.buffer[self.off_up_sec + 1] = 48 + (secs % 10)

        # Battery percentage, right-aligned in a 3-digit field (leading
        # zeros blanked, same style as the old code)
        bh = (pct // 100) % 10
        bt = (pct // 10) % 10
        bu = pct % 10

        self._write_at(self.off_bat_c, _battery_color(pct))
        self.buffer[self.off_bat]     = 48 + bh if bh > 0 else 32
        self.buffer[self.off_bat + 1] = 48 + bt if (bt > 0 or bh > 0) else 32
        self.buffer[self.off_bat + 2] = 48 + bu

        # Speaker volume, same right-aligned 3-digit style. env.audio is
        # lazy/optional (only created once `play` runs -- see shell.py's
        # _app(audio=True)), so this shows blank until then rather than
        # crashing on a None.
        if self.env.audio is not None:
            vol = self.env.audio.volume()
            self._write_at(self.off_vol_c, GREEN)
            vh = (vol // 100) % 10
            vt = (vol // 10) % 10
            vu = vol % 10
            self.buffer[self.off_vol]     = 48 + vh if vh > 0 else 32
            self.buffer[self.off_vol + 1] = 48 + vt if (vt > 0 or vh > 0) else 32
            self.buffer[self.off_vol + 2] = 48 + vu
        else:
            self._write_at(self.off_vol_c, GREY)
            self.buffer[self.off_vol : self.off_vol + 3] = b'   '

        # WiFi Status
        if self.wlan.isconnected():
            self._write_at(self.off_wifi_c, GREEN)
            self.buffer[self.off_wifi : self.off_wifi + 3] = b"ON "
        else:
            self._write_at(self.off_wifi_c, GREY)
            self.buffer[self.off_wifi : self.off_wifi + 3] = b"OFF"

        # BLE Mesh Status -- env.ble is lazy (see hardware.init_ble),
        # None until blechat has actually been run this boot.
        if self.env.ble is not None:
            self._write_at(self.off_ble_c, GREEN)
            self.buffer[self.off_ble : self.off_ble + 3] = b"ON "
        else:
            self._write_at(self.off_ble_c, GREY)
            self.buffer[self.off_ble : self.off_ble + 3] = b"OFF"

        # Clipboard service (clipd): grey = off, green = listening,
        # yellow for a few seconds after a clip arrives.
        if self.off_clip_c is not None:
            clipd = getattr(self.env, 'clipd', None)
            flash = getattr(self.env, 'clip_flash_until', None)
            if clipd is None or not clipd.listening:
                self._write_at(self.off_clip_c, GREY)
            elif flash is not None and time.ticks_diff(flash, time.ticks_ms()) > 0:
                self._write_at(self.off_clip_c, YELLOW)
            else:
                self._write_at(self.off_clip_c, GREEN)

        # Push to Terminal
        self.env.term.top_bar(self.buffer)
