"""Touch quick settings, drawn over a live terminal without changing its grid."""

import io
import time
import hardware


class TerminalOverlay(io.IOBase):
    def __init__(self, terminal, panel):
        self.terminal = terminal
        self.panel = panel
        self.bars = {}

    def __getattr__(self, name):
        return getattr(self.terminal, name)

    def write(self, data):
        return self.terminal.write(data)

    def ioctl(self, request, arg):
        return self.terminal.ioctl(request, arg)

    def draw(self):
        if not self.panel.opened:
            self.terminal.draw()

    def repaint_bars(self):
        if not self.panel.opened:
            self.terminal.repaint_bars()

    def top_bar(self, data):
        self._bar("top_bar", data)

    def bottom_bar(self, data):
        self._bar("bottom_bar", data)

    def _bar(self, name, data):
        if self.panel.opened:
            self.bars[name] = bytes(data) if not isinstance(data, str) else data
        else:
            getattr(self.terminal, name)(data)

    def restore(self):
        self.terminal.top_offset(self.panel.env.status_height)  # dirty all rows
        self.terminal.top_bar_invalidate()
        self.terminal.bottom_bar_invalidate()
        for name, data in self.bars.items():
            getattr(self.terminal, name)(data)
        self.bars.clear()
        self.terminal.draw()
        self.terminal.repaint_bars()


def clamp(value, low=0, high=100):
    return min(high, max(low, value))


class QuickPanel:
    BG = 0x1084
    TILE = 0x2108
    ON = 0x2B51
    FG = 0xEF7D
    DIM = 0x9CF3
    ACCENT = 0x6E7B

    def __init__(self, env, display, touch):
        import network
        import bluetooth
        from fonts import terminus_mpy_12
        self.env, self.display, self.touch = env, display, touch
        self.font = terminus_mpy_12
        self.wlan = network.WLAN(network.STA_IF)
        self.ble = bluetooth.BLE()
        self.opened = False
        self.dirty = True
        self.start = self.last = None
        self.opening = False
        self.slider = None
        self.message = "Swipe up to close"
        self.notifications = []
        self.notice_index = -1
        self.confirm_wifi = None
        self.last_refresh = time.ticks_ms()
        self.last_error = None
        self.gps_buffer = b""
        self.gps_status = "Reader off"

    def notify(self, text):
        self.notifications.append(str(text).replace("\n", " ")[:48])
        self.notifications = self.notifications[-5:]
        self.notice_index = -1
        self.dirty = True

    def open(self):
        self.opened = True
        self.dirty = True

    def close(self):
        self.opened = False
        self.slider = None
        self.env.term.restore()

    def contact(self, point):
        if point is None:
            if self.start is not None and self.opened and not self.opening:
                x, y = self.last
                dx, dy = x - self.start[0], y - self.start[1]
                if dy < -42:
                    self.close()
                elif abs(dx) < 12 and abs(dy) < 12 and self.slider is None:
                    self.tap(x, y)
            self.start = self.last = None
            self.opening = False
            self.slider = None
            return
        x, y = point
        if self.start is None:
            self.start = point
            if self.opened:
                self.slider = "brightness" if 108 <= y <= 145 else (
                    "volume" if 150 <= y <= 187 else None)
        self.last = point
        if not self.opened:
            if self.start[1] <= 24 and y - self.start[1] >= 35:
                self.opening = True
                self.open()
        elif self.slider is not None and not self.opening:
            value = clamp((x - 24) * 100 // 272)
            if self.slider == "brightness":
                # Keep the lowest step lit so touch can always recover it.
                self.env.brightness = hardware.set_brightness(max(1, (value * 16 + 50) // 100))
            else:
                self.env.volume = value
                if self.env.audio is not None:
                    self.env.audio.volume(value)
            self.dirty = True

    def tap(self, x, y):
        if x >= 276 and y < 38 or y >= 224:
            self.close()
            return
        if 190 <= y < 211 and self.notifications:
            self.notice_index = (self.notice_index - 1) % len(self.notifications)
            self.dirty = True
            return
        if not 44 <= y <= 101:
            return
        index = (x - 12) // 78
        if x < 12 or not 0 <= index < 4 or (x - 12) % 78 >= 62:
            return
        self.message = "Swipe up to close"
        try:
            if index == 0:
                if self.wlan.active():
                    now = time.ticks_ms()
                    if self.wlan.isconnected() and (self.confirm_wifi is None or
                            time.ticks_diff(now, self.confirm_wifi) > 4000):
                        self.confirm_wifi = now
                        self.message = "Tap Wi-Fi again to disconnect"
                    else:
                        self.wlan.active(False)
                        self.confirm_wifi = None
                else:
                    self.wlan.active(True)
                    try:
                        self.wlan.connect()  # use credentials in RAM/NVS, no blocking wait
                        self.message = "Wi-Fi connecting..."
                    except OSError:
                        self.message = "Use nm to select a Wi-Fi network"
            elif index == 1:
                if self.env.ble is not None:
                    self.message = "Bluetooth is in use by BLE mesh"
                else:
                    self.ble.active(not self.ble.active())
            elif index == 2:
                if getattr(self.env, "gps", None) is not None:
                    self.env.gps.deinit()
                    self.env.gps = None
                    self.gps_buffer = b""
                    self.gps_status = "Reader off"
                else:
                    import machine
                    self.env.gps = machine.UART(1, baudrate=9600,
                        tx=hardware.GPS_TX, rx=hardware.GPS_RX, timeout=0, rxbuf=1024)
                    self.gps_status = "Waiting for fix"
                self.message = "GPS reader; receiver power unchanged"
            elif index == 3:
                if getattr(self.env, "app_uses_radio", False):
                    self.message = "LoRa is in use by the current app"
                elif getattr(self.env, "radio", None) is not None:
                    self.env.radio.deinit()
                    self.env.radio = None
                else:
                    import os
                    try:
                        os.stat("/flash/.radio.json")
                    except OSError:
                        raise OSError("Set LoRa frequency with loracfg first")
                    hardware.init_radio(self.env)
                    if getattr(self.env, "radio", None) is None:
                        raise OSError("LoRa could not start; check loracfg")
        except (OSError, ValueError, RuntimeError) as error:
            self.message = str(error)[:48]
            self.notify(self.message)
        self.dirty = True

    def poll_gps(self):
        gps = getattr(self.env, "gps", None)
        if gps is None or not gps.any():
            return
        self.gps_buffer = (self.gps_buffer + (gps.read(128) or b""))[-512:]
        while b"\n" in self.gps_buffer:
            line, self.gps_buffer = self.gps_buffer.split(b"\n", 1)
            fields = line.split(b",")
            if fields[0] in (b"$GNGGA", b"$GPGGA") and len(fields) > 7:
                self.gps_status = ("Fix / " if fields[6] not in (b"", b"0") else "No fix / ") + fields[7].decode("ascii", "replace") + " sats"

    def poll(self):
        if self.env.sd_busy:
            return
        try:
            self.contact(self.touch.read())
            self.poll_gps()
            now = time.ticks_ms()
            if time.ticks_diff(now, self.last_refresh) >= 1000:
                self.last_refresh = now
                self.dirty = True
            if self.opened and self.dirty:
                self.draw()
                self.dirty = False
            self.last_error = None
        except Exception as error:
            # An unplugged/failing controller must not break keyboard or SSH.
            self.last_error = str(error)
            self.start = self.last = None
            self.slider = None
            if self.opened:
                self.close()

    def text(self, text, x, y, fg=None, bg=None):
        fg = self.FG if fg is None else fg
        bg = self.BG if bg is None else bg
        text = str(text)[:max(0, (320 - x) // 6)]
        if not text:
            return
        pixels = bytearray(len(text) * 6 * 12 * 2)
        for row in range(12):
            for i, char in enumerate(text):
                code = ord(char) if 32 <= ord(char) <= 126 else 63
                bits = self.font.REGULAR[(code - 32) * 12 + row]
                for col in range(6):
                    color = bg if bits & (0x80 >> col) else fg
                    offset = (row * len(text) * 6 + i * 6 + col) * 2
                    pixels[offset] = color >> 8
                    pixels[offset + 1] = color & 255
        self.display.blit_buffer(pixels, x, y, len(text) * 6, 12)

    def icon(self, kind, x, y, color):
        """Small outlined symbols, independent of the terminal's font."""
        d = self.display

        def path(points):
            for a, b in zip(points, points[1:]):
                d.line(x + a[0], y + a[1], x + b[0], y + b[1], color)
                d.line(x + a[0], y + a[1] + 1, x + b[0], y + b[1] + 1, color)

        if kind == 0:  # Wi-Fi waves
            path(((-11, -3), (-7, -6), (0, -8), (7, -6), (11, -3)))
            path(((-7, 1), (-4, -1), (0, -2), (4, -1), (7, 1)))
            path(((-3, 5), (0, 3), (3, 5)))
            d.fill_circle(x, y + 8, 1, color)
        elif kind == 1:  # Bluetooth rune
            path(((0, -10), (0, 10), (7, 4), (-6, -5)))
            path(((-6, 5), (7, -4), (0, -10)))
        elif kind == 2:  # GPS location pin
            d.circle(x, y - 3, 6, color)
            d.circle(x, y - 3, 5, color)
            d.fill_circle(x, y - 3, 2, color)
            path(((-5, 1), (0, 10), (5, 1)))
        else:  # LoRa antenna with radiating waves
            path(((0, -2), (0, 9)))
            path(((-5, 10), (5, 10)))
            d.fill_circle(x, y - 4, 2, color)
            for side in (-1, 1):
                path(((side * 5, -8), (side * 7, -4), (side * 5, 0)))
                path(((side * 9, -11), (side * 12, -4), (side * 9, 3)))

    def draw(self):
        d = self.display
        d.fill_rect(0, 0, 320, 240, self.BG)
        self.text("Quick settings", 14, 12)
        self.text("X", 292, 12, self.DIM)
        states = (self.wlan.active(), self.ble.active(),
                  getattr(self.env, "gps", None) is not None,
                  getattr(self.env, "radio", None) is not None)
        for i, label in enumerate(("Wi-Fi", "BT", "GPS RX", "LoRa")):
            x = 12 + i * 78
            color = self.ON if states[i] else self.TILE
            d.fill_rect(x, 44, 62, 57, color)
            self.icon(i, x + 31, 58, self.FG if states[i] else self.DIM)
            self.text(label, x + (62 - len(label) * 6)//2, 73, bg=color)
            state = "On" if states[i] else "Off"
            self.text(state, x + (62 - len(state) * 6)//2, 87, bg=color)
        brightness = getattr(self.env, "brightness", 16) * 100 // 16
        volume = self.env.audio.volume() if self.env.audio is not None else self.env.volume
        for title, value, y in (("Brightness", brightness, 111), ("Volume", volume, 153)):
            self.text(title, 14, y)
            self.text("%3d%%" % value, 278, y, self.DIM)
            d.fill_rect(24, y + 22, 272, 5, self.TILE)
            width = max(1, 272 * value // 100)
            d.fill_rect(24, y + 22, width, 5, self.ACCENT)
            d.fill_rect(clamp(24 + width - 3, 21, 293), y + 18, 6, 13, self.FG)
        notice = self.notifications[self.notice_index] if self.notifications else self.gps_status
        self.text(notice[:48], 14, 196, self.DIM)
        self.text(self.message[:48], 14, 213, self.DIM)
        d.fill_rect(135, 233, 50, 3, self.DIM)



def install(env, display):
    if getattr(env, "quickpanel", None) is not None:
        return env.quickpanel
    try:
        import machine
        from touchscreen import GT911
        bus = machine.I2C(0, sda=machine.Pin(hardware.I2C_SDA),
                          scl=machine.Pin(hardware.I2C_SCL), freq=400000, timeout=10000)
        panel = QuickPanel(env, display, GT911(bus))
        if not hasattr(hardware, "set_brightness"):
            from backlight import set_brightness
            hardware.set_brightness = set_brightness
        env.brightness = hardware.set_brightness(getattr(env, "brightness", 16))
    except (OSError, ValueError) as error:
        env.sts.notify("Touch unavailable: " + str(error))
        return None
    env.term = TerminalOverlay(env.term, panel)
    env.quickpanel = panel
    return panel
