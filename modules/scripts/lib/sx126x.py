#
# SX1262 LoRa driver in MicroPython, on the board's shared hardware SPI object.
# License: MIT
#
# Same interface as the native `lora` module (begin / transmit / start_receive /
# receive / snr / rssi / cad / rx_boost / get_device_errors / deinit), plus the
# options Meshtastic needs: preamble length, sync word, DIO3 TCXO supply, DIO2
# RF switch and over-current limit.
#
# The T-Deck's display, SD card and radio share one SPI bus. The display runs
# it at 80 MHz, the radio tops out far lower, so every radio transaction
# parks the bus at a radio-safe speed, sets env.sd_busy so the display timer
# skips its draw (the same protocol the SD card code uses), and restores the
# display speed afterwards.
#
# Why not the C module: on the test unit the native module's RadioLib HAL
# reported "chip not found" while this driver, on MicroPython's own SPI
# object, read the SX1262 version string correctly.
#

import time

try:
    _ticks = time.ticks_ms
    _diff = time.ticks_diff
    _sleep_ms = time.sleep_ms
except AttributeError:                      # host tests
    def _ticks():
        return int(time.time() * 1000)

    def _diff(a, b):
        return a - b

    def _sleep_ms(ms):
        time.sleep(ms / 1000.0)

# opcodes
CMD_SET_SLEEP = 0x84
CMD_SET_STANDBY = 0x80
CMD_SET_TX = 0x83
CMD_SET_RX = 0x82
CMD_SET_CAD = 0xC5
CMD_SET_PACKET_TYPE = 0x8A
CMD_SET_RF_FREQ = 0x86
CMD_SET_PA_CONFIG = 0x95
CMD_SET_TX_PARAMS = 0x8E
CMD_SET_BUFFER_BASE = 0x8F
CMD_SET_MOD_PARAMS = 0x8B
CMD_SET_PKT_PARAMS = 0x8C
CMD_SET_CAD_PARAMS = 0x88
CMD_SET_DIO_IRQ = 0x08
CMD_GET_IRQ = 0x12
CMD_CLEAR_IRQ = 0x02
CMD_SET_DIO2_RFSW = 0x9D
CMD_SET_DIO3_TCXO = 0x97
CMD_SET_REGULATOR = 0x96
CMD_CALIBRATE = 0x89
CMD_CALIBRATE_IMAGE = 0x98
CMD_GET_STATUS = 0xC0
CMD_GET_RX_BUF = 0x13
CMD_GET_PKT_STATUS = 0x14
CMD_GET_ERRORS = 0x17
CMD_GET_RSSI_INST = 0x15
CMD_CLEAR_ERRORS = 0x07
CMD_READ_BUF = 0x1E
CMD_WRITE_BUF = 0x0E
CMD_WRITE_REG = 0x0D
CMD_READ_REG = 0x1D

REG_VERSION = 0x0320
REG_SYNC_MSB = 0x0740
REG_IQ_POLARITY = 0x0736
REG_TX_MOD = 0x0889
REG_RX_GAIN = 0x08AC
REG_OCP = 0x08E7

IRQ_TX_DONE = 1 << 0
IRQ_RX_DONE = 1 << 1
IRQ_HEADER_ERR = 1 << 5
IRQ_CRC_ERR = 1 << 6
IRQ_CAD_DONE = 1 << 7
IRQ_CAD_DETECTED = 1 << 8
IRQ_TIMEOUT = 1 << 9

BW_CODES = {7.8: 0x00, 10.4: 0x08, 15.6: 0x01, 20.8: 0x09, 31.25: 0x02, 41.7: 0x0A,
            62.5: 0x03, 125.0: 0x04, 250.0: 0x05, 500.0: 0x06}
TCXO_CODES = ((1.6, 0), (1.7, 1), (1.8, 2), (2.2, 3), (2.4, 4), (2.7, 5), (3.0, 6), (3.3, 7))
CAD_DET_PEAK = {5: 18, 6: 19, 7: 22, 8: 22, 9: 23, 10: 24, 11: 25, 12: 28}

RADIO_BAUD = 8000000
TX_TIMEOUT_MS = 12000


class SX126x:
    def __init__(self, spi, cs, rst, busy, dio1, env=None, baud=RADIO_BAUD, restore_baud=80000000):
        self.spi = spi
        self.cs = cs
        self.rst = rst
        self.busy = busy
        self.dio1 = dio1
        self.env = env
        self.baud = baud
        self.restore_baud = restore_baud
        self._depth = 0
        self._mode = "idle"
        self._rssi = 0.0
        self._snr = 0.0
        self._bw = 125.0
        self._sf = 7
        self._preamble = 8
        self._cr = 5
        self.alive = True

    # -- bus handling --

    def _lock(self):
        if self._depth == 0:
            if self.env is not None:
                self.env.sd_busy = True
            self.spi.init(baudrate=self.baud)
        self._depth += 1

    def _unlock(self):
        self._depth -= 1
        if self._depth == 0:
            self.spi.init(baudrate=self.restore_baud)
            if self.env is not None:
                self.env.sd_busy = False

    def _wait_busy(self, timeout_ms=200):
        t = _ticks()
        while self.busy.value():
            if _diff(_ticks(), t) > timeout_ms:
                raise RuntimeError("SX126x busy timeout")

    def _xfer(self, tx, skip):
        """Send tx, return the bytes received after the first `skip`."""
        self._lock()
        try:
            self._wait_busy()
            rx = bytearray(len(tx))
            self.cs.value(0)
            try:
                self.spi.write_readinto(tx, rx)
            finally:
                self.cs.value(1)
            return bytes(rx[skip:])
        finally:
            self._unlock()

    def _write(self, opcode, data=b""):
        self._xfer(bytes([opcode]) + bytes(data), 0)

    def _read(self, opcode, n):
        """Commands that answer with a status byte first: returns the n bytes after it."""
        return self._xfer(bytes([opcode, 0]) + bytes(n), 2)

    def _write_reg(self, addr, data):
        self._xfer(bytes([CMD_WRITE_REG, addr >> 8, addr & 0xFF]) + bytes(data), 0)

    def _read_reg(self, addr, n=1):
        return self._xfer(bytes([CMD_READ_REG, addr >> 8, addr & 0xFF, 0]) + bytes(n), 4)

    def _update_reg(self, addr, mask, value):
        cur = self._read_reg(addr)[0]
        self._write_reg(addr, bytes([(cur & ~mask & 0xFF) | (value & mask)]))

    # -- setup --

    def probe(self):
        """True when an SX126x answers on the bus."""
        self._lock()
        try:
            self._reset()
            return self._read_reg(REG_VERSION, 16).startswith(b"SX126")
        finally:
            self._unlock()

    def _reset(self):
        self.rst.value(0)
        _sleep_ms(10)
        self.rst.value(1)
        _sleep_ms(20)
        self._wait_busy(500)

    def begin(self, freq, bw, sf, cr, sync_word=0x12, power=10, preamble=8, tcxo=None,
              dio2_rf_switch=False, current_limit=0):
        self._lock()
        try:
            self._mode = "idle"
            self._reset()
            if not self._read_reg(REG_VERSION, 16).startswith(b"SX126"):
                raise RuntimeError("SX126x not found")
            self._write(CMD_SET_STANDBY, b"\x00")
            if tcxo:
                code = 7
                for volts, c in TCXO_CODES:
                    if tcxo <= volts + 0.01:
                        code = c
                        break
                self._write(CMD_SET_DIO3_TCXO, bytes([code, 0x00, 0x01, 0x40]))   # 5 ms start-up
                self._write(CMD_CLEAR_ERRORS, b"\x00\x00")
            self._write(CMD_CALIBRATE, b"\x7F")
            self._write(CMD_SET_REGULATOR, b"\x01")                               # DC-DC
            if dio2_rf_switch:
                self._write(CMD_SET_DIO2_RFSW, b"\x01")
            self._write(CMD_SET_PACKET_TYPE, b"\x01")                             # LoRa
            self._set_frequency(freq)
            self._write(CMD_SET_PA_CONFIG, bytes([0x04, 0x07, 0x00, 0x01]))
            self._set_power(power)
            if current_limit:
                self._write_reg(REG_OCP, bytes([int(current_limit / 2.5)]))
            self._write(CMD_SET_BUFFER_BASE, b"\x00\x00")
            if bw not in BW_CODES:
                raise RuntimeError("unsupported bandwidth %s" % bw)
            self._bw, self._sf, self._cr, self._preamble = bw, sf, cr, preamble
            tsym = (1 << sf) / bw                                                # ms
            ldro = 1 if tsym > 16.0 else 0
            self._write(CMD_SET_MOD_PARAMS, bytes([sf, BW_CODES[bw], cr - 4, ldro]))
            self._write_reg(REG_SYNC_MSB, bytes([(sync_word & 0xF0) | 0x04,
                                                 ((sync_word & 0x0F) << 4) | 0x04]))
            self._update_reg(REG_TX_MOD, 0x04, 0x00 if bw == 500.0 else 0x04)     # BW500 TX fix
            self._set_packet_params(255)
            self._write(CMD_SET_DIO_IRQ, bytes([0x03, 0xFF, 0x02, 0x63, 0, 0, 0, 0]))
            self._mode = "standby"
            errors = self.get_device_errors()
            if errors:
                raise RuntimeError("SX126x device errors 0x%04X" % errors)
        finally:
            self._unlock()

    def _set_frequency(self, mhz):
        if mhz < 446.0:
            img = (0x6B, 0x6F)
        elif mhz < 734.0:
            img = (0x75, 0x81)
        elif mhz < 828.0:
            img = (0xC1, 0xC5)
        elif mhz < 877.0:
            img = (0xD7, 0xDB)
        else:
            img = (0xE1, 0xE9)
        self._write(CMD_CALIBRATE_IMAGE, bytes(img))
        frf = int(mhz * (1 << 25) / 32.0)
        self._write(CMD_SET_RF_FREQ, frf.to_bytes(4, "big"))

    def _set_power(self, dbm):
        dbm = max(-9, min(22, int(dbm)))
        self._write(CMD_SET_TX_PARAMS, bytes([dbm & 0xFF, 0x04]))

    def _set_packet_params(self, length):
        pre = self._preamble
        self._write(CMD_SET_PKT_PARAMS, bytes([pre >> 8, pre & 0xFF, 0x00, length, 0x01, 0x00]))
        self._update_reg(REG_IQ_POLARITY, 0x04, 0x04)            # standard (non-inverted) IQ

    def _standby(self):
        if self._mode != "standby":
            self._write(CMD_SET_STANDBY, b"\x00")
            self._mode = "standby"

    # -- status --

    def get_status(self):
        return self._xfer(bytes([CMD_GET_STATUS, 0]), 1)[0]

    def get_device_errors(self):
        d = self._read(CMD_GET_ERRORS, 2)
        return (d[0] << 8) | d[1]

    def _irq(self):
        d = self._read(CMD_GET_IRQ, 2)
        return (d[0] << 8) | d[1]

    def _clear_irq(self, mask=0xFFFF):
        self._write(CMD_CLEAR_IRQ, bytes([mask >> 8, mask & 0xFF]))

    # -- receive / transmit --

    def start_receive(self):
        if self._mode == "rx":
            return
        self._lock()
        try:
            self._standby()
            self._set_packet_params(255)
            self._write(CMD_SET_DIO_IRQ, bytes([0x03, 0xFF, 0x02, 0x63, 0, 0, 0, 0]))
            self._clear_irq()
            self._write(CMD_SET_RX, b"\xFF\xFF\xFF")               # continuous
            self._mode = "rx"
        finally:
            self._unlock()

    def receive(self):
        """The next received payload, or None. Keeps listening afterwards."""
        if self._mode != "rx" or not self.dio1.value():
            return None
        self._lock()
        try:
            irq = self._irq()
            self._clear_irq()
            if not irq & IRQ_RX_DONE:
                return None
            if irq & (IRQ_CRC_ERR | IRQ_HEADER_ERR):
                return None
            length, start = self._read(CMD_GET_RX_BUF, 2)
            if length == 0:
                return None
            data = self._xfer(bytes([CMD_READ_BUF, start, 0]) + bytes(length), 3)
            st = self._read(CMD_GET_PKT_STATUS, 3)
            self._rssi = -st[0] / 2.0
            snr = st[1] - 256 if st[1] > 127 else st[1]
            self._snr = snr / 4.0
            return data
        finally:
            self._unlock()

    def transmit(self, data):
        data = bytes(data)
        if not data or len(data) > 255:
            raise RuntimeError("Transmit failed: bad length")
        # No lock held while waiting: each command takes the bus on its own, so the
        # display keeps updating during the (up to ~second-long) transmission.
        try:
            self._mode = "idle"
            self._write(CMD_SET_STANDBY, b"\x00")
            self._set_packet_params(len(data))
            self._write(CMD_WRITE_BUF, b"\x00" + data)
            self._write(CMD_SET_DIO_IRQ, bytes([0x02, 0x01, 0x02, 0x01, 0, 0, 0, 0]))
            self._clear_irq()
            self._write(CMD_SET_TX, b"\x00\x00\x00")           # no radio timeout; we watch the clock
            t = _ticks()
            while True:
                if self.dio1.value():
                    irq = self._irq()
                    if irq & IRQ_TX_DONE:
                        break
                    if irq & IRQ_TIMEOUT:
                        raise RuntimeError("Transmit failed: radio timeout")
                if _diff(_ticks(), t) > TX_TIMEOUT_MS:
                    raise RuntimeError("Transmit failed: no TX done")
                _sleep_ms(2)
            self._clear_irq()
        finally:
            self._write(CMD_SET_STANDBY, b"\x00")
            self._mode = "standby"

    def cad(self):
        """Channel activity detection (listen before talk). True = someone is on air."""
        self._standby()
        peak = CAD_DET_PEAK.get(self._sf, 25)
        self._write(CMD_SET_CAD_PARAMS, bytes([0x02, peak, 10, 0x00, 0, 0, 0]))
        self._write(CMD_SET_DIO_IRQ, bytes([0x01, 0x80, 0x01, 0x80, 0, 0, 0, 0]))
        self._clear_irq()
        self._write(CMD_SET_CAD, b"")
        self._mode = "idle"
        t = _ticks()
        irq = 0
        while _diff(_ticks(), t) < 300:
            irq = self._irq()
            if irq & IRQ_CAD_DONE:
                break
            _sleep_ms(1)
        self._clear_irq()
        self._write(CMD_SET_STANDBY, b"\x00")
        self._mode = "standby"
        return bool(irq & IRQ_CAD_DETECTED)

    def rx_boost(self, on):
        self._write_reg(REG_RX_GAIN, b"\x96" if on else b"\x94")

    def rssi(self):
        return self._rssi

    def rssi_inst(self):
        """Current channel power in dBm (valid while receiving)."""
        return -self._read(CMD_GET_RSSI_INST, 1)[0] / 2.0

    def snr(self):
        return self._snr

    def deinit(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self._write(CMD_SET_SLEEP, b"\x04")
        except Exception:
            pass
        self._mode = "sleep"
