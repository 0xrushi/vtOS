"""SX126x driver against a small simulated chip: command sequences and data flow."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules/scripts/lib"))

import sx126x  # noqa: E402


class Pin:
    def __init__(self, v=0):
        self.v = v

    def value(self, x=None):
        if x is None:
            return self.v
        self.v = x


class Env:
    sd_busy = False


class FakeChip:
    """Just enough SX1262 to exercise the driver; also plays the shared-bus SPI."""

    def __init__(self, version=b"SX1261 V2D 2D02\x00"):
        self.log = []
        self.regs = {sx126x.REG_VERSION: version.ljust(16, b"\x00"),
                     sx126x.REG_TX_MOD: b"\x00", sx126x.REG_IQ_POLARITY: b"\x00"}
        self.irq = 0
        self.rx_packet = None
        self.errors = 0
        self.baud = 80000000
        self.bauds = []
        self.dio1 = Pin()
        self.busy_seen = None
        self.written = None
        self.env = None

    def init(self, baudrate=None):
        self.baud = baudrate
        self.bauds.append(baudrate)

    def write_readinto(self, tx, rx):
        op = tx[0]
        assert self.baud == sx126x.RADIO_BAUD, "radio transaction at display speed"
        if self.env is not None:
            assert self.env.sd_busy, "display not paused during radio transaction"
        out = bytearray(len(tx))
        if op == sx126x.CMD_READ_REG:
            addr = (tx[1] << 8) | tx[2]
            data = self.regs.get(addr, bytes(len(tx)))
            for i in range(len(tx) - 4):
                out[4 + i] = data[i] if i < len(data) else 0
            self.log.append(("readreg", addr))
        elif op == sx126x.CMD_WRITE_REG:
            addr = (tx[1] << 8) | tx[2]
            self.regs[addr] = bytes(tx[3:])
            self.log.append(("writereg", addr, bytes(tx[3:])))
        elif op == sx126x.CMD_GET_IRQ:
            out[2], out[3] = self.irq >> 8, self.irq & 0xFF
            self.log.append(("getirq",))
        elif op == sx126x.CMD_CLEAR_IRQ:
            self.irq &= ~((tx[1] << 8) | tx[2])
            self.dio1.v = 1 if self.irq else 0
            self.log.append(("clearirq",))
        elif op == sx126x.CMD_GET_ERRORS:
            out[2], out[3] = self.errors >> 8, self.errors & 0xFF
        elif op == sx126x.CMD_GET_RX_BUF:
            out[2], out[3] = len(self.rx_packet), 0
        elif op == sx126x.CMD_READ_BUF:
            for i, b in enumerate(self.rx_packet):
                out[3 + i] = b
        elif op == sx126x.CMD_GET_PKT_STATUS:
            out[2], out[3], out[4] = 2 * 87, 0xF6, 2 * 85            # -87 dBm, SNR -2.5 dB
        elif op == sx126x.CMD_GET_RSSI_INST:
            out[2] = 2 * 101
        elif op == sx126x.CMD_WRITE_BUF:
            self.written = bytes(tx[2:])
            self.log.append(("writebuf", self.written))
        elif op == sx126x.CMD_SET_TX:
            self.irq |= sx126x.IRQ_TX_DONE
            self.dio1.v = 1
            self.log.append(("settx",))
        elif op == sx126x.CMD_SET_CAD:
            self.irq |= sx126x.IRQ_CAD_DONE | (sx126x.IRQ_CAD_DETECTED if getattr(self, "busy_air", False) else 0)
            self.log.append(("setcad",))
        else:
            self.log.append(("cmd", op, bytes(tx[1:])))
        rx[:] = out

    def deliver(self, payload, irq=sx126x.IRQ_RX_DONE):
        self.rx_packet = payload
        self.irq |= irq
        self.dio1.v = 1

    def cmds(self, op):
        return [e[2] for e in self.log if e[0] == "cmd" and e[1] == op]


@pytest.fixture
def radio():
    chip = FakeChip()
    env = Env()
    chip.env = env
    r = sx126x.SX126x(chip, Pin(1), Pin(1), Pin(0), chip.dio1, env=env)
    return r, chip, env


def begin_meshtastic(r, **kw):
    args = dict(freq=906.875, bw=250.0, sf=11, cr=5, sync_word=0x2B, power=17, preamble=16,
                tcxo=1.8, dio2_rf_switch=True, current_limit=140)
    args.update(kw)
    r.begin(**args)


def test_probe_accepts_sx1262_and_rejects_nothing_there(radio):
    r, chip, _ = radio
    assert r.probe()
    chip.regs[sx126x.REG_VERSION] = b"\xff" * 16
    assert not r.probe()


def test_begin_programs_meshtastic_longfast(radio):
    r, chip, env = radio
    begin_meshtastic(r)
    assert chip.cmds(sx126x.CMD_SET_PACKET_TYPE) == [b"\x01"]
    frf = int(906.875 * (1 << 25) / 32.0).to_bytes(4, "big")
    assert chip.cmds(sx126x.CMD_SET_RF_FREQ) == [frf]
    assert chip.cmds(sx126x.CMD_SET_MOD_PARAMS) == [bytes([11, 0x05, 1, 0])]      # SF11 BW250 CR4/5, no LDRO
    assert chip.cmds(sx126x.CMD_SET_DIO3_TCXO) == [bytes([2, 0, 1, 0x40])]          # 1.8 V
    assert chip.cmds(sx126x.CMD_SET_DIO2_RFSW) == [b"\x01"]
    assert chip.cmds(sx126x.CMD_CALIBRATE_IMAGE) == [bytes([0xE1, 0xE9])]
    assert chip.cmds(sx126x.CMD_SET_TX_PARAMS)[0][0] == 17
    assert chip.regs[sx126x.REG_SYNC_MSB] == bytes([0x24, 0xB4])                    # 0x2B -> 0x24B4
    assert chip.regs[sx126x.REG_OCP] == bytes([56])                                 # 140 mA
    pkt = chip.cmds(sx126x.CMD_SET_PKT_PARAMS)[-1]
    assert pkt[:2] == b"\x00\x10" and pkt[2] == 0 and pkt[4] == 1                   # preamble 16, explicit header, CRC on
    assert chip.regs[sx126x.REG_IQ_POLARITY][0] & 0x04
    assert not env.sd_busy and chip.baud == 80000000                                 # display speed restored


def test_slow_modes_enable_low_data_rate_optimisation(radio):
    r, chip, _ = radio
    begin_meshtastic(r, bw=125.0, sf=12, cr=8)
    assert chip.cmds(sx126x.CMD_SET_MOD_PARAMS)[-1] == bytes([12, 0x04, 4, 1])


def test_begin_fails_cleanly_without_a_chip_or_with_errors(radio):
    r, chip, _ = radio
    chip.regs[sx126x.REG_VERSION] = b"\x00" * 16
    with pytest.raises(RuntimeError):
        begin_meshtastic(r)
    chip.regs[sx126x.REG_VERSION] = b"SX1261 V2D 2D02\x00"
    chip.errors = 0x0020
    with pytest.raises(RuntimeError, match="errors"):
        begin_meshtastic(r)


def test_transmit_writes_buffer_and_waits_for_tx_done(radio):
    r, chip, env = radio
    begin_meshtastic(r)
    r.transmit(b"\x01\x02\x03hello")
    assert chip.written == b"\x01\x02\x03hello"
    assert chip.cmds(sx126x.CMD_SET_PKT_PARAMS)[-1][3] == 8                       # payload length
    assert ("settx",) in chip.log and not env.sd_busy


def test_transmit_rejects_bad_lengths(radio):
    r, chip, _ = radio
    begin_meshtastic(r)
    for bad in (b"", bytes(256)):
        with pytest.raises(RuntimeError):
            r.transmit(bad)


def test_transmit_times_out_if_radio_never_finishes(radio, monkeypatch):
    r, chip, _ = radio
    begin_meshtastic(r)
    monkeypatch.setattr(sx126x, "TX_TIMEOUT_MS", 30)
    chip.write_readinto_orig = chip.write_readinto

    def no_txdone(tx, rx):
        chip.write_readinto_orig(tx, rx)
        if tx[0] == sx126x.CMD_SET_TX:
            chip.irq = 0
            chip.dio1.v = 0
    chip.write_readinto = no_txdone
    with pytest.raises(RuntimeError, match="no TX done"):
        r.transmit(b"x")
    assert r._mode == "standby"


def test_receive_returns_payload_with_rssi_and_snr(radio):
    r, chip, _ = radio
    begin_meshtastic(r)
    r.start_receive()
    assert r.receive() is None
    chip.deliver(b"\xde\xad\xbe\xef")
    assert r.receive() == b"\xde\xad\xbe\xef"
    assert r.rssi() == -87.0 and r.snr() == -2.5
    assert r.receive() is None                         # cleared, still listening
    assert r._mode == "rx"


def test_receive_drops_crc_errors(radio):
    r, chip, _ = radio
    begin_meshtastic(r)
    r.start_receive()
    chip.deliver(b"bad", irq=sx126x.IRQ_RX_DONE | sx126x.IRQ_CRC_ERR)
    assert r.receive() is None


def test_start_receive_is_idempotent_and_rearms_after_tx(radio):
    r, chip, _ = radio
    begin_meshtastic(r)
    r.start_receive()
    n = len(chip.cmds(sx126x.CMD_SET_RX))
    r.start_receive()
    assert len(chip.cmds(sx126x.CMD_SET_RX)) == n
    r.transmit(b"x")
    r.start_receive()
    assert len(chip.cmds(sx126x.CMD_SET_RX)) == n + 1


def test_cad_reports_activity(radio):
    r, chip, _ = radio
    begin_meshtastic(r)
    assert r.cad() is False
    chip.busy_air = True
    assert r.cad() is True
    assert chip.cmds(sx126x.CMD_SET_CAD_PARAMS)[-1][1] == 25                      # SF11 detection peak


def test_rx_boost_and_noise_floor(radio):
    r, chip, _ = radio
    begin_meshtastic(r)
    r.rx_boost(True)
    assert chip.regs[sx126x.REG_RX_GAIN] == b"\x96"
    r.rx_boost(False)
    assert chip.regs[sx126x.REG_RX_GAIN] == b"\x94"
    assert r.rssi_inst() == -101.0


def test_busy_stuck_high_raises(radio, monkeypatch):
    r, chip, _ = radio
    r.busy = Pin(1)
    monkeypatch.setattr(sx126x, "_ticks", iter(range(0, 10000, 100)).__next__)
    with pytest.raises(RuntimeError, match="busy"):
        r._write(sx126x.CMD_SET_STANDBY, b"\x00")
    assert chip.baud == 80000000                          # bus restored even on failure


def test_works_with_the_meshtastic_stack(radio):
    import meshback
    import mtconfig
    r, chip, _ = radio
    cfg = mtconfig.Settings()
    ok, msg, limited = meshback.configure_radio(r, cfg)
    assert ok and limited == "" and cfg.tcxo_found == 0.0
