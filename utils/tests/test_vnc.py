"""The VNC client app (bin/vnc.py): drawing, input, viewport and the session
loop, against a scripted fake server and a fake display. No device needed."""

import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rfb():
    return _load("vtos_rfb_for_vnc", ROOT / "modules/scripts/lib/rfb.py")


@pytest.fixture(scope="module")
def fake():
    return _load("vtos_fake_rfb_for_vnc", HERE / "fake_rfb.py")


@pytest.fixture
def vnc(rfb, monkeypatch):
    monkeypatch.setitem(sys.modules, "rfb", rfb)     # bin/vnc.py does `import rfb`
    cfg = _load("vtos_trackcfg_for_vnc", ROOT / "modules/scripts/lib/trackcfg.py")
    cfg._values = cfg._defaults()
    monkeypatch.setitem(sys.modules, "trackcfg", cfg)
    return _load("vtos_vnc_test", ROOT / "modules/scripts/bin/vnc.py")


@pytest.fixture
def clock(vnc, monkeypatch):
    now = [1000]
    monkeypatch.setattr(vnc, "_ticks", lambda: now[0])
    return now


# --- command line ------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("pc", ("pc", 5900)),
    ("192.168.1.4", ("192.168.1.4", 5900)),
    ("pc:1", ("pc", 5901)),
    ("pc:0", ("pc", 5900)),
    ("pc:99", ("pc", 5999)),
    ("pc:100", ("pc", 100)),              # 100 and up is a port, like other viewers
    ("pc:5902", ("pc", 5902)),
    ("pc::5901", ("pc", 5901)),
    ("pc::1", ("pc", 1)),
])
def test_split_target(vnc, text, expected):
    assert vnc.split_target(text) == expected


@pytest.mark.parametrize("text", ["pc:x", "pc::", "pc::99999", "pc:70000", "pc::-1"])
def test_split_target_rejects_bad_numbers(vnc, text):
    with pytest.raises(ValueError):
        vnc.split_target(text)


def test_profiles_parse_and_skip_junk(vnc):
    text = "# my machines\n\nhome 192.168.1.4:1 sekret\nlab lab.local\nbroken\nbad host:zz pw\n"
    assert vnc.parse_profiles(text) == {
        "home": ("192.168.1.4", 5901, "sekret"),
        "lab": ("lab.local", 5900, None),
    }


def test_load_profiles_reads_first_file_that_exists(vnc, tmp_path):
    good = tmp_path / ".vnc"
    good.write_text("a h1\n")
    assert vnc.load_profiles((str(tmp_path / "missing"), str(good))) == {"a": ("h1", 5900, None)}
    assert vnc.load_profiles((str(tmp_path / "missing"),)) == {}


def test_parse_target_alias_and_overrides(vnc):
    profiles = {"home": ("10.0.0.2", 5901, "saved")}
    assert vnc.parse_target(["home"], profiles) == ("10.0.0.2", 5901, "saved")
    assert vnc.parse_target(["home", "typed"], profiles) == ("10.0.0.2", 5901, "typed")
    assert vnc.parse_target(["10.0.0.9:2", "pw"], profiles) == ("10.0.0.9", 5902, "pw")
    assert vnc.parse_target(["10.0.0.9"], profiles) == ("10.0.0.9", 5900, None)


def test_parse_target_needs_a_host(vnc):
    with pytest.raises(ValueError):
        vnc.parse_target([], {})
    with pytest.raises(ValueError):
        vnc.parse_target([":1"], {})


@pytest.mark.parametrize("host,tailnet", [
    ("100.64.0.1", True), ("100.101.102.103", True), ("100.127.255.255", True),
    ("100.63.0.1", False), ("100.128.0.1", False), ("192.168.1.4", False),
    ("pc.local", False), ("100.64.0", False), ("100.64.0.256", False),
])
def test_is_tailnet_host(vnc, host, tailnet):
    assert vnc.is_tailnet_host(host) is tailnet


# --- input translation --------------------------------------------------------

@pytest.mark.parametrize("byte,sym", [
    (ord("a"), 0x61), (ord("A"), 0x41), (ord(" "), 0x20), (ord("~"), 0x7E), (ord("$"), 0x24),
    (13, 0xFF0D), (10, 0xFF0D), (8, 0xFF08), (127, 0xFF08), (9, 0xFF09), (27, 0xFF1B),
    (0xE9, 0xE9),
    (0, None), (1, None), (0x1C, None), (0x1D, None), (0x7F + 1, None), (0x9F, None),
])
def test_keysym_for(vnc, byte, sym):
    assert vnc.keysym_for(byte) == sym


def test_accelerate_is_precise_when_slow_and_fast_when_fast(vnc):
    assert [vnc.accelerate(n) for n in (0, 1, 2, 4, 8)] == [0, 4, 12, 40, 144]
    assert vnc.accelerate(-3) == -vnc.accelerate(3)


def test_pending_accumulates_between_takes(vnc):
    p = vnc.Pending()
    p.add(1, 2, False)
    p.add(-3, 4, True)
    p.add(0, 0, True)
    assert p.take() == (-2, 6, 2)
    assert p.take() == (0, 0, 0)


# --- the mode label --------------------------------------------------------------

def test_every_mode_label_can_be_drawn(vnc):
    for text in vnc._MODE_LABELS:
        assert all(ch in vnc._GLYPHS for ch in text), text


def test_label_draws_boxed_glyphs_inside_its_reported_size(vnc, fake):
    tft = fake.FakeTft(60, 30)
    tft.fill(0)
    x, y, w, h = vnc.draw_label(tft, "KEYS", 3, 4, 0xFFFF, 0x1234)
    assert (w, h) == vnc.label_size("KEYS")
    assert tft.screen[y][x] == 0x1234                       # box background
    assert tft.screen[y + h - 1][x + w - 1] == 0x1234
    lit = {(c, r) for r in range(30) for c in range(60) if tft.screen[r][c] == 0xFFFF}
    assert lit and all(x <= c < x + w and y <= r < y + h for c, r in lit)
    outside = [(c, r) for r in range(30) for c in range(60)
               if not (x <= c < x + w and y <= r < y + h) and tft.screen[r][c] != 0]
    assert outside == []


def test_letters_are_distinguishable(vnc, fake):
    rendered = set()
    for ch in sorted(vnc._GLYPHS):
        tft = fake.FakeTft(20, 20)
        tft.fill(0)
        vnc.draw_label(tft, ch, 0, 0, 0xFFFF, 0)
        rendered.add(tuple(tuple(row) for row in tft.screen))
    assert len(rendered) == len(vnc._GLYPHS)


# --- drawing: Screen ---------------------------------------------------------------

@pytest.fixture
def pipe(rfb, fake):
    """pipe(width, height) -> (server, client) with the handshake done."""
    servers = []

    def make(width, height):
        server = fake.FakeServer(width=width, height=height)
        servers.append(server)
        client = rfb.Client(fake.SockIO(server.start()))
        client.handshake()
        return server, client

    yield make
    for s in servers:
        s.stop()


def draw(fake, vnc, pipe, remote, viewport, rect):
    """Send one Raw rectangle through the real parser into a fresh Screen."""
    rw, rh = remote
    server, client = pipe(rw, rh)
    tft = fake.FakeTft()
    screen = vnc.Screen(tft, 320, 240)
    screen.set_remote(rw, rh)
    screen.vx, screen.vy = viewport
    server.queue(fake.update(fake.raw_rect(*rect)) + bytes([2]))      # then a Bell
    sink_events = []
    screen.bell = lambda: sink_events.append("bell")
    client.read_message(screen)
    assert client.read_message(screen) == 2, "stream out of sync after the rectangle"
    assert sink_events == ["bell"]
    return tft, screen


def check_pixels(fake, tft, screen, rect):
    x, y, w, h = rect
    drawn = 0
    for ry in range(y, y + h):
        for rx in range(x, x + w):
            inside = (screen.vx <= rx < screen.vx + screen.vw and
                      screen.vy <= ry < screen.vy + screen.vh)
            sx = screen.ox + rx - screen.vx
            sy = screen.oy + ry - screen.vy
            if inside:
                assert tft.screen[sy][sx] == fake.pixel_at(rx, ry), (rx, ry)
                drawn += 1
    # nothing else on the display changed from the black fill
    lit = sum(1 for row in tft.screen for p in row if p != 0)
    zero_value_pixels = sum(1 for ry in range(y, y + h) for rx in range(x, x + w)
                            if fake.pixel_at(rx, ry) == 0 and
                            screen.vx <= rx < screen.vx + screen.vw and
                            screen.vy <= ry < screen.vy + screen.vh)
    assert lit == drawn - zero_value_pixels
    return drawn


BIG = (700, 500)
VIEW = (100, 50)           # viewport covers x 100..419, y 50..289


@pytest.mark.parametrize("name,rect,visible", [
    ("inside", (150, 80, 40, 30), 40 * 30),
    ("clipped left", (60, 80, 80, 30), 40 * 30),
    ("clipped right", (380, 80, 80, 30), 40 * 30),
    ("clipped top", (150, 30, 40, 40), 40 * 20),
    ("clipped bottom", (150, 270, 40, 40), 40 * 20),
    ("clipped at a corner", (60, 30, 80, 40), 40 * 20),
    ("fully outside", (500, 400, 30, 30), 0),
    ("covers the viewport", (50, 0, 500, 400), 320 * 240),
    ("one pixel", (100, 50, 1, 1), 1),
    ("last pixel", (419, 289, 3, 3), 1),
])
def test_raw_rect_lands_on_the_right_pixels(fake, vnc, pipe, name, rect, visible):
    tft, screen = draw(fake, vnc, pipe, BIG, VIEW, rect)
    assert check_pixels(fake, tft, screen, rect) == visible


def test_a_wide_rect_larger_than_the_chunk_buffer(fake, vnc, pipe):
    # 700 px * 2 bytes = 1400 per row; the 16 KiB chunk holds 11 rows
    rect = (0, 0, 700, 60)
    tft, screen = draw(fake, vnc, pipe, BIG, VIEW, rect)
    assert check_pixels(fake, tft, screen, rect) == 320 * 10


def test_full_viewport_update_is_a_few_big_blits_not_one_per_row(fake, vnc, pipe):
    rect = (100, 50, 320, 240)
    tft, screen = draw(fake, vnc, pipe, BIG, VIEW, rect)
    assert check_pixels(fake, tft, screen, rect) == 320 * 240
    assert tft.blits == 2                                    # 120 rows per chunk


def test_rows_wider_than_the_default_chunk_grow_the_buffer(fake, vnc, pipe):
    rect = (0, 0, 9000, 2)                                   # 18000 bytes per row
    tft, screen = draw(fake, vnc, pipe, (9000, 20), (0, 0), rect)
    assert check_pixels(fake, tft, screen, rect) == 320 * 2


def test_desktop_smaller_than_the_display_is_centred(fake, vnc, pipe):
    rect = (0, 0, 64, 48)
    tft, screen = draw(fake, vnc, pipe, (64, 48), (0, 0), rect)
    assert (screen.ox, screen.oy) == (128, 96)
    assert tft.screen[96][128] == fake.pixel_at(0, 0)
    assert tft.screen[143][191] == fake.pixel_at(63, 47)
    assert check_pixels(fake, tft, screen, rect) == 64 * 48


def test_desktop_narrower_but_taller_is_centred_on_one_axis(fake, vnc, pipe):
    rect = (0, 100, 100, 50)
    tft, screen = draw(fake, vnc, pipe, (100, 600), (0, 90), rect)
    assert (screen.ox, screen.oy, screen.vw, screen.vh) == (110, 0, 100, 240)
    assert check_pixels(fake, tft, screen, rect) == 100 * 50


def test_zero_sized_rectangles_are_harmless(fake, vnc, pipe):
    server, client = pipe(64, 48)
    screen = vnc.Screen(fake.FakeTft(), 320, 240)
    screen.set_remote(64, 48)
    server.queue(fake.update((5, 5, 0, 10, 0, b""), (5, 5, 10, 0, 0, b"")) + bytes([2]))
    client.read_message(screen)
    assert client.read_message(screen) == 2


def test_resize_rebuilds_the_viewport_and_clears_the_display(fake, vnc, rfb, pipe):
    server, client = pipe(64, 48)
    tft = fake.FakeTft()
    screen = vnc.Screen(tft, 320, 240)
    screen.set_remote(700, 500)
    screen.vx, screen.vy = 380, 260
    tft.screen[5][5] = 0xBEEF
    server.queue(fake.update((0, 0, 400, 300, rfb.ENC_DESKTOP_SIZE, b"")))
    client.read_message(screen)
    assert screen.resized
    assert (screen.rw, screen.rh) == (400, 300)
    assert (screen.vx, screen.vy) == (80, 60)                # clamped into the smaller desktop
    assert tft.screen[5][5] == 0


def test_server_cut_text_goes_to_the_callback(fake, vnc, pipe):
    server, client = pipe(64, 48)
    screen = vnc.Screen(fake.FakeTft(), 320, 240)
    got = []
    screen.on_cut = got.append
    server.queue(bytes([3, 0, 0, 0, 0, 0, 0, 2]) + b"hi")
    client.read_message(screen)
    assert got == ["hi"]


# --- viewport following ---------------------------------------------------------------

def make_screen(vnc, fake, w, h):
    screen = vnc.Screen(fake.FakeTft(), 320, 240)
    screen.set_remote(w, h)
    return screen


def test_viewport_stays_put_while_the_pointer_is_inside(vnc, fake):
    screen = make_screen(vnc, fake, 700, 500)
    assert screen.follow(160, 120) is False
    assert screen.follow(24, 24) is False                    # right on the margin
    assert screen.follow(295, 215) is False
    assert screen.region() == (0, 0, 320, 240)


def test_viewport_scrolls_to_keep_the_pointer_visible_and_clamps(vnc, fake):
    screen = make_screen(vnc, fake, 700, 500)
    assert screen.follow(300, 100) is True
    assert screen.region() == (5, 0, 320, 240)
    assert screen.follow(699, 499) is True
    assert screen.region() == (380, 260, 320, 240)           # clamped to the far corner
    assert screen.follow(0, 0) is True
    assert screen.region() == (0, 0, 320, 240)
    assert screen.follow(0, 0) is False


def test_small_desktops_never_scroll(vnc, fake):
    screen = make_screen(vnc, fake, 64, 48)
    assert screen.follow(63, 47) is False
    assert screen.region() == (0, 0, 64, 48)


def test_restore_blacks_out_the_overlay_and_returns_what_to_refetch(vnc, fake):
    screen = make_screen(vnc, fake, 700, 500)
    screen.vx, screen.vy = 100, 50
    assert screen.restore(0, 0, 30, 14) == (100, 50, 30, 14)
    small = make_screen(vnc, fake, 64, 48)                   # picture is at 128,96
    assert small.restore(0, 0, 30, 14) is None               # overlay was all margin
    assert small.restore(120, 90, 20, 20) == (0, 0, 12, 14)  # clipped to the picture


# --- the session ---------------------------------------------------------------------

class FakeKvm:
    def __init__(self):
        self.queue = bytearray()
        self.on_read = None

    def feed(self, *data):
        for d in data:
            self.queue += bytes([d]) if isinstance(d, int) else d

    def readinto(self, buf):
        if self.on_read:
            self.on_read()
        if not self.queue:
            return None
        buf[0] = self.queue.pop(0)
        return 1


class Rig:
    pass


@pytest.fixture
def rig(vnc, rfb, fake, clock):
    made = []

    def make(width=64, height=48, **server_args):
        r = Rig()
        r.server = fake.FakeServer(width=width, height=height, **server_args)
        made.append(r.server)
        r.sock = r.server.start()
        r.client = rfb.Client(fake.SockIO(r.sock))
        r.client.handshake()
        r.client.set_pixel_format()
        r.client.set_encodings([rfb.ENC_RAW, rfb.ENC_DESKTOP_SIZE])
        r.tft = fake.FakeTft()
        r.kvm = FakeKvm()
        r.env = SimpleNamespace(tft=r.tft, kvm=r.kvm, pointer=None, clipboard="")
        r.screen = vnc.Screen(r.tft, 320, 240)
        r.screen.set_remote(r.client.width, r.client.height)
        r.session = vnc.Session(r.env, r.sock, r.client, r.screen)
        r.fake = fake
        r.vnc = vnc

        def pump(until, timeout=5.0):
            for _ in range(int(timeout / 0.02)):
                if until():
                    return True
                r.session.step(0.02)
            return until()

        def events(kind=None):
            with r.server.lock:
                evs = list(r.server.events)
            return [e for e in evs if kind is None or e[0] == kind]

        r.pump = pump
        r.events = events
        return r

    yield make
    for s in made:
        s.stop()


def test_first_request_then_incremental_requests_after_every_update(rig):
    r = rig(64, 48)
    r.session.start()
    assert r.pump(lambda: r.tft.blits > 0)
    assert r.tft.screen[96][128] == r.fake.pixel_at(0, 0)
    assert r.pump(lambda: ("fbur", 1, 0, 0, 64, 48) in r.events())
    fburs = r.events("fbur")
    assert fburs[0] == ("fbur", 0, 0, 0, 64, 48)             # full picture first
    assert fburs[1] == ("fbur", 1, 0, 0, 64, 48)             # then only changes

    r.server.change(10, 10, 5, 4)                            # the held request is answered
    assert r.pump(lambda: len(r.events("fbur")) >= 3)
    assert r.tft.screen[96 + 12][128 + 12] == r.fake.pixel_at(12, 12)


def test_nothing_is_requested_without_an_update_to_answer(rig):
    r = rig(64, 48)
    r.session.start()
    r.pump(lambda: len(r.events("fbur")) >= 2)
    n = len(r.events("fbur"))
    for _ in range(10):
        r.session.step(0.02)
    assert len(r.events("fbur")) == n                        # no polling storm while idle


def test_typing_sends_press_and_release(rig):
    r = rig()
    r.kvm.feed(b"aB", 13, 8, 127, 9, 27, 0xE9)
    r.kvm.feed(1, 0x1D, 0x9F)                                # control bytes: ignored
    r.session.step(0.01)
    assert r.pump(lambda: len(r.events("key")) >= 18)
    keys = [(down, sym) for _, down, sym in r.events("key")]
    expected = []
    for sym in (0x61, 0x42, 0xFF0D, 0xFF08, 0xFF08, 0xFF09, 0xFF1B, 0xE9):
        pair = [(1, sym), (0, sym)]
        if sym == 0x42:                       # a capital is typed holding Shift
            pair = [(1, 0xFFE1)] + pair + [(0, 0xFFE1)]
        expected += pair
    assert keys == expected


def test_a_burst_of_typing_is_taken_over_several_passes_without_loss(rig):
    r = rig()
    text = b"hello world, this is a long paste!"
    r.kvm.feed(text)
    r.pump(lambda: len(r.events("key")) >= 2 * len(text))
    typed = bytes(sym for _, down, sym in r.events("key") if down)
    assert typed == text


def test_mouse_mode_moves_with_acceleration_and_clicks_left(rig):
    r = rig()
    assert (r.session.px, r.session.py) == (32, 24)
    r.env.pointer = r.session.pending.add
    r.session.pending.add(1, 0, False)
    r.session.step(0.01)
    r.session.pending.add(4, -2, False)                      # 40 px right, 12 px up
    r.session.step(0.01)
    r.session.pending.add(0, 0, True)
    r.session.step(0.01)
    assert r.pump(lambda: len(r.events("pointer")) >= 4)
    assert r.events("pointer") == [
        ("pointer", 0, 36, 24), ("pointer", 0, 63, 12),
        ("pointer", 1, 63, 12), ("pointer", 0, 63, 12)]


def test_pointer_stays_inside_the_desktop(rig):
    r = rig(64, 48)
    r.session.pending.add(100, 100, False)
    r.session.step(0.01)
    r.session.pending.add(-300, -300, False)
    r.session.step(0.01)
    assert r.pump(lambda: len(r.events("pointer")) >= 2)
    assert r.events("pointer") == [("pointer", 0, 63, 47), ("pointer", 0, 0, 0)]


def test_mic_key_cycles_modes_and_shows_a_label(rig, vnc):
    r = rig()
    seen = []
    for _ in range(5):
        r.kvm.feed(vnc.MIC_KEY)
        r.session.step(0.01)
        seen.append(r.session.mode)
    assert seen == [1, 2, 3, 0, 1]
    boxes = [rect for rect in r.tft.rects if rect[:2] == (0, 0) and rect[4] != 0]   # 0 = erasing the old label
    assert [b[4] for b in boxes] == [vnc._MODE_COLORS[m] for m in (1, 2, 3, 0, 1)]
    assert r.events("key") == []                             # the Mic key is not sent on


def test_right_mode_clicks_the_right_button(rig, vnc):
    r = rig()
    r.session.set_mode(vnc.MODE_RIGHT)
    r.session.pending.add(0, 0, True)
    r.session.step(0.01)
    assert r.pump(lambda: len(r.events("pointer")) >= 2)
    assert r.events("pointer") == [("pointer", 4, 32, 24), ("pointer", 0, 32, 24)]


def test_scroll_mode_turns_the_ball_into_the_wheel(rig, vnc):
    r = rig()
    r.session.set_mode(vnc.MODE_SCROLL)
    for dx, dy in ((0, 2), (0, -1), (1, 0), (-1, 0), (0, 50)):
        r.session.pending.add(dx, dy, False)
        r.session.step(0.01)
    r.session.pending.add(0, 0, True)
    r.session.step(0.01)
    assert r.pump(lambda: len(r.events("pointer")) >= 2 * (2 + 1 + 1 + 1 + 4 + 1))
    presses = [b for _, b, x, y in r.events("pointer") if b]
    assert presses == [16, 16, 8, 64, 32, 16, 16, 16, 16, 1]
    assert {(x, y) for _, b, x, y in r.events("pointer")} == {(32, 24)}   # wheel never moves the pointer


def test_keys_mode_sends_arrows_with_a_dead_zone_and_click_is_escape(rig, vnc):
    r = rig()
    r.session.set_mode(vnc.MODE_KEYS)
    for dx, dy in ((1, 1), (-1, -1), (3, 0), (0, -7), (-2, 2)):
        r.session.pending.add(dx, dy, False)
        r.session.step(0.01)
    r.session.pending.add(0, 0, True)
    r.session.step(0.01)
    assert r.pump(lambda: len(r.events("key")) >= 2 * 7)
    downs = [sym for _, down, sym in r.events("key") if down]
    assert downs == [0xFF53, 0xFF52, 0xFF52, 0xFF52, 0xFF51, 0xFF54, 0xFF1B]
    assert r.events("pointer") == []


def test_big_desktop_pans_and_asks_for_the_new_region_once_per_interval(rig, clock):
    r = rig(700, 500)
    r.session.start()
    r.pump(lambda: r.tft.blits > 0)
    r.session.px = 290
    clock[0] += 500
    r.session.pending.add(4, 0, False)                       # 40 px right: crosses the margin
    r.session.step(0.01)
    assert r.screen.region() == (35, 0, 320, 240)
    assert r.pump(lambda: ("fbur", 0, 35, 0, 320, 240) in r.events())
    # more movement inside the same 120 ms window must not spam full refreshes
    n = len(r.events("fbur"))
    r.session.pending.add(4, 0, False)
    r.session.step(0.01)
    r.session.step(0.01)
    assert len([e for e in r.events("fbur") if e[1] == 0]) == len([e for e in r.events("fbur")[:n] if e[1] == 0])
    clock[0] += 200
    r.session.step(0.01)
    assert r.pump(lambda: ("fbur", 0, 75, 0, 320, 240) in r.events())


def test_a_stale_update_after_a_pan_still_draws_correctly(rig, clock):
    r = rig(700, 500)
    r.session.start()
    r.pump(lambda: r.tft.blits > 0)
    r.screen.vx = 40                                         # the viewport moved under an in-flight update
    r.server.queue(r.fake.update(r.fake.raw_rect(0, 0, 100, 10)))
    blits = r.tft.blits
    assert r.pump(lambda: r.tft.blits > blits)
    assert r.tft.screen[0][0] == r.fake.pixel_at(40, 0)      # only the part that is now in view
    assert r.tft.screen[0][59] == r.fake.pixel_at(99, 0)
    assert r.tft.screen[0][60] != r.fake.pixel_at(100, 0)


def test_server_resize_triggers_a_full_refresh_of_the_new_size(rig, rfb):
    r = rig(64, 48)
    r.session.start()
    r.pump(lambda: len(r.events("fbur")) >= 2)
    r.server.queue(r.fake.update((0, 0, 400, 300, rfb.ENC_DESKTOP_SIZE, b"")))
    assert r.pump(lambda: ("fbur", 0, 0, 0, 320, 240) in r.events())
    assert (r.session.px, r.session.py) == (32, 24)
    r.session.px, r.session.py = 399, 299
    r.server.queue(r.fake.update((0, 0, 100, 100, rfb.ENC_DESKTOP_SIZE, b"")))
    assert r.pump(lambda: (r.session.px, r.session.py) == (99, 99))


def test_label_is_removed_after_a_moment_and_the_picture_underneath_refetched(rig, clock):
    r = rig(700, 500)
    r.session.start()
    r.pump(lambda: r.tft.blits > 0)
    label = r.vnc.label_size("MOUSE")
    assert r.session.toast is not None
    clock[0] += 500
    r.session.step(0.01)
    assert r.session.toast is not None                       # still showing
    clock[0] += 1000
    r.session.step(0.01)
    assert r.session.toast is None
    assert r.pump(lambda: ("fbur", 0, 0, 0, label[0], label[1]) in r.events())


def test_label_over_the_margin_of_a_small_desktop_needs_no_refetch(rig, clock):
    r = rig(64, 48)
    r.session.start()
    r.pump(lambda: len(r.events("fbur")) >= 2)
    before = len(r.events("fbur"))
    clock[0] += 2000
    r.session.step(0.01)
    assert r.session.toast is None
    assert len(r.events("fbur")) == before
    assert r.tft.screen[2][2] == 0                           # label area black again


def test_run_installs_the_trackball_hook_and_removes_it_on_disconnect(rig):
    r = rig(64, 48)
    seen = []
    r.kvm.on_read = lambda: seen.append(r.env.pointer)
    threading.Timer(0.3, r.server.stop).start()
    reason = r.session.run()
    assert reason == "connection closed"
    assert seen and all(p is not None for p in seen)
    assert r.env.pointer is None


def test_run_ends_quietly_on_a_long_press(rig):
    r = rig(64, 48)

    def interrupt():
        raise KeyboardInterrupt
    r.kvm.on_read = interrupt
    assert r.session.run() == "closed"
    assert r.env.pointer is None


def test_run_reports_protocol_errors(rig):
    r = rig(64, 48)
    r.server.queue(bytes([99]))
    assert r.session.run() == "unknown server message 99"


def test_run_reports_socket_errors(rig):
    r = rig(64, 48)

    def explode(*a, **k):
        raise OSError(104, "reset")
    r.client.write = explode
    assert r.session.run().startswith("connection lost")


# --- main() ------------------------------------------------------------------------

@pytest.fixture
def app(vnc, fake, monkeypatch):
    env = SimpleNamespace(tft=fake.FakeTft(), kvm=FakeKvm(), pointer=None, clipboard="",
                          screen_width=320, screen_height=240, sd_busy=False)
    monkeypatch.setattr(vnc, "load_profiles", lambda: {})
    monkeypatch.setattr(vnc, "_wifi_no_sleep", lambda: (lambda: None))
    return SimpleNamespace(env=env, vnc=vnc, fake=fake, monkeypatch=monkeypatch)


def test_main_prints_usage(app, capsys):
    app.vnc.main(app.env, [])
    assert "Usage: vnc [-z] <host>" in capsys.readouterr().out


def test_main_needs_a_display(app, capsys):
    app.env.tft = None
    app.vnc.main(app.env, ["pc"])
    assert "does not expose the display" in capsys.readouterr().out


def test_main_reports_a_bad_target(app, capsys):
    app.vnc.main(app.env, ["pc:zz"])
    assert "bad display in pc:zz" in capsys.readouterr().out


def test_main_reports_connection_failure(app, capsys):
    def refuse(host, port):
        assert (host, port) == ("pc", 5901)
        raise OSError(111, "ECONNREFUSED")
    app.monkeypatch.setattr(app.vnc, "connect", refuse)
    app.vnc.main(app.env, ["pc:1"])
    out = capsys.readouterr().out
    assert "connecting to pc:5901" in out and "cannot connect" in out and "ECONNREFUSED" in out
    assert app.env.sd_busy is False


def serve(app, **kwargs):
    server = app.fake.FakeServer(**kwargs)
    app.monkeypatch.setattr(app.vnc, "connect",
                            lambda host, port: app.fake.SockIO(server.start()))
    return server


def test_main_reports_a_wrong_password_without_taking_over_the_display(app, capsys):
    server = serve(app, security=(2,), password="right")
    app.vnc.main(app.env, ["pc", "wrong"])
    out = capsys.readouterr().out
    assert "vnc: authentication failed" in out
    assert app.env.sd_busy is False
    assert app.env.tft.blits == 0
    server.stop()


def test_main_prompts_for_the_password_only_when_the_server_asks(app, capsys):
    server = serve(app, security=(2,), password="pw")
    prompts = []
    app.monkeypatch.setattr(app.vnc, "_read_password", lambda p: prompts.append(p) or "pw")
    threading.Timer(0.4, server.stop).start()
    app.vnc.main(app.env, ["pc"])
    assert prompts == ["VNC password: "]
    assert server.auth_ok is True
    assert "connection closed" in capsys.readouterr().out


def test_main_cancelled_password_prompt(app, capsys):
    server = serve(app, security=(2,), password="pw")
    app.monkeypatch.setattr(app.vnc, "_read_password", lambda p: None)
    app.vnc.main(app.env, ["pc"])
    assert "password required" in capsys.readouterr().out
    server.stop()


def test_main_full_session_draws_and_cleans_up(app, capsys):
    server = serve(app, width=64, height=48, name="desk")
    states = []
    app.env.kvm.on_read = lambda: states.append((app.env.sd_busy, app.env.pointer is not None))
    threading.Timer(0.4, server.stop).start()
    app.vnc.main(app.env, ["pc"])
    out = capsys.readouterr().out
    assert "vnc: desk (64x48)" in out
    assert states and all(s == (True, True) for s in states)     # display owned, trackball taken
    assert app.env.sd_busy is False and app.env.pointer is None  # and given back
    assert app.env.tft.screen[96][128] == app.fake.pixel_at(0, 0)
    assert "\x1b[2J" in out                                      # terminal repaint requested
    kinds = [e[0] for e in server.events]
    assert kinds[:2] == ["pixfmt", "encodings"]
    assert server.events[1] == ("encodings", [0, -223])


def test_main_server_clipboard_lands_in_the_vtos_clipboard(app):
    server = serve(app)
    server.queue(bytes([3, 0, 0, 0, 0, 0, 0, 5]) + b"clip!")
    threading.Timer(0.4, server.stop).start()
    app.vnc.main(app.env, ["pc"])
    assert app.env.clipboard == "clip!"


def test_pending_keeps_input_that_arrives_while_it_is_being_taken(vnc):
    # The 30 ms system tick can run add() between any two statements of take().
    # Fire it right after take() has read the clicks counter (its last read).
    class Racy(vnc.Pending):
        armed = False

        def __getattribute__(self, name):
            value = object.__getattribute__(self, name)
            if name == "clicks" and Racy.armed:
                Racy.armed = False
                vnc.Pending.add(self, 1, 1, True)
            return value

    p = Racy()
    p.add(5, 6, True)
    Racy.armed = True                                  # from here on: only inside take()
    assert p.take() == (5, 6, 1)
    assert Racy.armed is False, "the simulated tick never fired"
    assert p.take() == (1, 1, 1)                       # the mid-take input was not lost


# --- over Tailscale ------------------------------------------------------------------

class FakeSock:
    def __init__(self, log):
        self.log = log

    def bind(self, addr):
        self.log.append(("bind", addr))

    def settimeout(self, seconds):
        self.log.append(("settimeout", seconds))

    def connect(self, addr):
        self.log.append(("connect", addr))

    def setsockopt(self, *a):
        self.log.append(("setsockopt",) + a)

    def close(self):
        self.log.append(("close",))


@pytest.fixture
def net(vnc, monkeypatch):
    """Fake socket module and fake modtailscale, with a shared call log."""
    log = []
    sock_mod = SimpleNamespace(
        getaddrinfo=lambda host, port: [(2, 1, 0, "", (host, port))],
        socket=lambda: FakeSock(log))
    monkeypatch.setattr(vnc, "socket", sock_mod)
    ts = SimpleNamespace(result="100.100.100.1", waits=[])

    def wait_peer(host, ms):
        ts.waits.append((host, ms))
        log.append(("wait_peer", host))
        if isinstance(ts.result, Exception):
            raise ts.result
        return ts.result
    ts.wait_peer = wait_peer
    monkeypatch.setitem(sys.modules, "modtailscale", ts)
    return SimpleNamespace(log=log, ts=ts)


def test_tailnet_connect_waits_for_the_tunnel_then_binds_before_connecting(vnc, net):
    vnc.connect("100.96.103.69", 5901)
    kinds = [e[0] for e in net.log]
    assert kinds[:3] == ["wait_peer", "bind", "settimeout"]
    assert ("bind", ("100.100.100.1", 0)) in net.log          # our own tailnet address, any port
    assert net.log.index(("bind", ("100.100.100.1", 0))) < net.log.index(("connect", ("100.96.103.69", 5901)))
    assert net.ts.waits == [("100.96.103.69", 30000)]
    assert ("settimeout", 15) in net.log and ("settimeout", 10) in net.log


def test_lan_connect_never_touches_tailscale(vnc, net):
    vnc.connect("192.168.1.157", 5901)
    assert net.ts.waits == []
    assert not any(e[0] == "bind" for e in net.log)
    assert ("connect", ("192.168.1.157", 5901)) in net.log


@pytest.mark.parametrize("code,words", [
    (-2010, "run `tailscale up`"),
    (-2011, "peer unavailable"),
    (-2012, "timed out"),
    (-2013, "cannot bind"),
])
def test_tailscale_errors_become_readable_messages(vnc, net, code, words):
    net.ts.result = OSError(code)
    with pytest.raises(OSError, match=words):
        vnc.connect("100.96.103.69", 5901)
    assert not any(e[0] in ("bind", "connect") for e in net.log)   # no socket was even opened


def test_an_unknown_tailscale_error_is_passed_through(vnc, net):
    net.ts.result = OSError(-1, "weird")
    with pytest.raises(OSError, match="weird"):
        vnc.connect("100.96.103.69", 5901)


def test_firmware_without_tailscale_says_so(vnc, net, monkeypatch):
    monkeypatch.setitem(sys.modules, "modtailscale", None)     # makes `import modtailscale` fail
    with pytest.raises(OSError, match="no Tailscale support"):
        vnc.connect("100.96.103.69", 5901)


def test_failed_connect_closes_the_socket(vnc, net, monkeypatch):
    def boom(self, addr):
        raise OSError(113, "EHOSTUNREACH")
    monkeypatch.setattr(FakeSock, "connect", boom)
    with pytest.raises(OSError, match="EHOSTUNREACH"):
        vnc.connect("100.96.103.69", 5901)
    assert ("close",) in net.log


def test_main_announces_and_reports_a_tailnet_failure(app, net, capsys):
    net.ts.result = OSError(-2010)
    app.vnc.main(app.env, ["100.96.103.69:1", "secret"])
    out = capsys.readouterr().out
    assert "connecting to 100.96.103.69:5901 over Tailscale" in out
    assert "cannot connect: Tailscale is down" in out
    assert app.env.sd_busy is False


# --- Hextile ---------------------------------------------------------------------

def hextile_payload(x, y, w, h, truth):
    """Encode a rectangle as Hextile, varying tile types; truth[(px, py)] gets
    the colour every pixel must end up with."""
    import struct
    out = bytearray()
    n = 0
    bg_state = 0
    for ty in range(y, y + h, 16):
        th = min(16, y + h - ty)
        for tx in range(x, x + w, 16):
            tw = min(16, x + w - tx)
            kind = n % 4
            n += 1
            fg, other = 0xF800, 0x07E0
            if kind in (1, 2):
                bg_state = 0x1234 + n          # only these tiles specify a background
            bg = bg_state                      # the others reuse the previous one
            if kind == 0:                                   # raw tile
                out.append(1)
                for py in range(ty, ty + th):
                    for px in range(tx, tx + tw):
                        out += struct.pack(">H", 0x4000 + px + 3 * py)
                        truth[(px, py)] = 0x4000 + px + 3 * py
                continue
            for py in range(ty, ty + th):
                for px in range(tx, tx + tw):
                    truth[(px, py)] = bg
            if kind == 1:                                   # solid background only
                out += bytes([2]) + struct.pack(">H", bg)
            elif kind == 2:                                 # bg + fg, one subrect
                out += bytes([2 | 4 | 8]) + struct.pack(">HH", bg, fg)
                out += bytes([1, (0 << 4) | 0, ((tw - 1) << 4) | (th - 1)])
                for py in range(ty, ty + th):
                    for px in range(tx, tx + tw):
                        truth[(px, py)] = fg
            else:                                           # coloured subrects, bg reused
                out += bytes([8 | 16]) + bytes([2])
                out += struct.pack(">H", other) + bytes([0x00, 0x00])        # 1x1 at 0,0
                out += struct.pack(">H", fg) + bytes([(tw - 1) << 4 | (th - 1), 0x00])
                truth[(tx, ty)] = other
                truth[(tx + tw - 1, ty + th - 1)] = fg
    return bytes(out)


@pytest.mark.parametrize("rect,viewport", [
    ((0, 0, 40, 37), (0, 0)),                    # partial tiles on both edges
    ((100, 50, 320, 240), (100, 50)),            # exactly the viewport
    ((60, 30, 80, 40), (100, 50)),               # clipped at a corner
    ((500, 400, 30, 30), (100, 50)),             # outside
    ((0, 0, 1, 1), (0, 0)),
])
def test_hextile_rect_lands_on_the_right_pixels(fake, vnc, rfb, pipe, rect, viewport):
    x, y, w, h = rect
    server, client = pipe(700, 500)
    tft = fake.FakeTft()
    screen = vnc.Screen(tft, 320, 240)
    screen.set_remote(700, 500)
    screen.vx, screen.vy = viewport
    truth = {}
    server.queue(fake.update((x, y, w, h, rfb.ENC_HEXTILE,
                              hextile_payload(x, y, w, h, truth))) + bytes([2]))
    screen.bell = lambda: None
    client.read_message(screen)
    assert client.read_message(screen) == 2, "stream out of sync after the rectangle"
    for (px, py), colour in truth.items():
        inside = (screen.vx <= px < screen.vx + screen.vw and
                  screen.vy <= py < screen.vy + screen.vh)
        got = tft.screen[screen.oy + py - screen.vy][screen.ox + px - screen.vx] if inside else None
        if inside:
            assert got == colour, (px, py)
    lit = sum(1 for row in tft.screen for p in row if p not in (0, None))
    visible = sum(1 for (px, py) in truth if screen.vx <= px < screen.vx + screen.vw
                  and screen.vy <= py < screen.vy + screen.vh)
    assert lit == visible


def test_hextile_is_discarded_by_a_sink_that_ignores_it(fake, rfb, pipe):
    server, client = pipe(64, 48)
    truth = {}
    server.queue(fake.update((0, 0, 40, 37, rfb.ENC_HEXTILE,
                              hextile_payload(0, 0, 40, 37, truth))) + bytes([2]))
    client.read_message(rfb.Sink())
    assert client.read_message(rfb.Sink()) == 2


@pytest.mark.parametrize("args,expected", [(["-z", "pc"], [5, 0, -223]), (["pc"], [0, -223])])
def test_dash_z_asks_for_hextile_and_default_does_not(app, args, expected):
    server = serve(app, width=64, height=48)
    threading.Timer(0.4, server.stop).start()
    app.vnc.main(app.env, args)
    assert [e for e in server.events if e[0] == "encodings"][0][1] == expected


def test_pointer_gain_follows_the_ball_setting(vnc):
    import trackcfg
    trackcfg._values["gain"] = 8
    assert [vnc.accelerate(n) for n in (1, 2, 4)] == [8, 24, 80]
