"""GT911 touch input in the T-Deck's landscape orientation.

Register layout and axis mapping follow LilyGO's T-Deck UnitTest and
SensorLib's GT911 driver. Never rewrite the controller configuration.
"""


class GT911:
    def __init__(self, bus):
        self.bus = bus
        self.address = None
        self.point = None
        for address in (0x5D, 0x14):
            try:
                product = self._read(address, 0x8140, 4)
                if product[:3] == b"911":
                    self.address = address
                    break
            except OSError:
                pass
        if self.address is None:
            raise OSError("GT911 touchscreen not found")

    def _read(self, address, register, count):
        # GT911 register addresses are big endian, coordinate data little endian.
        return self.bus.readfrom_mem(address, register, count, addrsize=16)

    def read(self):
        """Return last contact, None on release; no ready frame keeps state."""
        status = self._read(self.address, 0x814E, 1)[0]
        if not status & 0x80:
            return self.point
        count = status & 15
        try:
            if count == 1:
                data = self._read(self.address, 0x814F, 8)
                raw_x = data[1] | data[2] << 8
                raw_y = data[3] | data[4] << 8
                # LilyGO: swapXY(true), mirrorXY(false, true).
                self.point = (min(319, max(0, raw_y)),
                              min(239, max(0, 239 - raw_x)))
            else:
                # Ignore multi-touch; it must not produce accidental tile taps.
                self.point = None
        finally:
            self.bus.writeto_mem(self.address, 0x814E, b"\x00", addrsize=16)
        return self.point
