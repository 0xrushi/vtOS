"""Render the actual T-Deck Balatro pixel view on a host: python utils/balatro_preview.py OUT.png."""
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


class FrameBuffer:
    def __init__(self, buf, width, height, fmt):
        self.buf, self.width, self.height = buf, width, height

    def pixel(self, x, y, color=None):
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        offset = 2 * (y * self.width + x)
        if color is None:
            return struct.unpack_from('<H', self.buf, offset)[0]
        struct.pack_into('<H', self.buf, offset, color)

    def fill(self, color):
        self.fill_rect(0, 0, self.width, self.height, color)

    def fill_rect(self, x, y, w, h, color):
        for row in range(max(0, y), min(self.height, y + h)):
            for col in range(max(0, x), min(self.width, x + w)):
                self.pixel(col, row, color)

    def hline(self, x, y, w, color):
        self.fill_rect(x, y, w, 1, color)

    def line(self, x, y, x2, y2, color):
        dx, dy = abs(x2 - x), -abs(y2 - y)
        sx, sy = (1 if x < x2 else -1), (1 if y < y2 else -1)
        err = dx + dy
        while True:
            self.pixel(x, y, color)
            if (x, y) == (x2, y2):
                break
            e = err * 2
            if e >= dy:
                err += dy
                x += sx
            if e <= dx:
                err += dx
                y += sy

    def rect(self, x, y, w, h, color):
        self.hline(x, y, w, color)
        self.hline(x, y + h - 1, w, color)
        self.fill_rect(x, y, 1, h, color)
        self.fill_rect(x + w - 1, y, 1, h, color)


class Display:
    def __init__(self):
        self.pixels = [0] * (320 * 240)

    def fill_rect(self, x, y, w, h, color):
        assert 0 <= x <= x + w <= 320 and 0 <= y <= y + h <= 240, (x, y, w, h)
        for row in range(y, y + h):
            self.pixels[row * 320 + x:row * 320 + x + w] = [color] * w

    def blit_buffer(self, buf, x, y, w, h):
        assert 0 <= x <= x + w <= 320 and 0 <= y <= y + h <= 240, (x, y, w, h)
        colors = struct.unpack('>' + 'H' * (w * h), bytes(buf[:w * h * 2]))
        for row in range(h):
            self.pixels[(y + row) * 320 + x:(y + row) * 320 + x + w] = colors[row * w:(row + 1) * w]

    def save(self, path):
        from PIL import Image
        im = Image.new('RGB', (320, 240))
        im.putdata([(((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63,
                     (c & 31) * 255 // 31) for c in self.pixels])
        im.save(path)
        im.resize((960, 720), Image.Resampling.NEAREST).save(str(path).replace('.png', '-3x.png'))


def setup():
    sys.path.insert(0, str(ROOT / 'modules/scripts'))
    sys.path.insert(0, str(ROOT / 'modules/scripts/lib'))
    sys.modules['framebuf'] = SimpleNamespace(FrameBuffer=FrameBuffer, RGB565=1)
    from balatroscreen import Screen
    from balatrogame import Run
    env = SimpleNamespace(screen_width=320, screen_height=240, status_height=19,
                          quickpanel=None, sd_busy=False, tft=Display())
    return Screen(env), Run(), env


def example():
    screen, run, env = setup()
    run.start_blind()
    run.cards = [51, 50, 49, 48, 47, 12, 24, 26]
    run.jokers = [0, 1, 2, 3, 4]
    run.selected = {0, 1, 2, 3, 4}
    run.cursor = 2
    run.money = 34
    run.consumables = [('tarot', 0)]
    screen.draw(run)
    return screen, run, env


if __name__ == '__main__':
    screen, run, env = example()
    env.tft.save(sys.argv[1])
