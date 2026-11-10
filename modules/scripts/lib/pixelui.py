"""Shared colors and text for the T-Deck's pixel views."""

from fonts import terminus_mpy_12 as small, terminus_mpy_14 as large
from meshdraw import sanitize


def pixel(rgb):
    # framebuf stores RGB565 little-endian, while the ST7789 takes MSB first.
    value = ((rgb >> 8) & 0xF800) | ((rgb >> 5) & 0x07E0) | ((rgb >> 3) & 0x001F)
    return ((value & 255) << 8) | (value >> 8)


BG = pixel(0x18181B)
PLOT = pixel(0x121215)
GRID = pixel(0x303037)
EDGE = pixel(0x484853)
TEXT = pixel(0xF4F4F5)
DIM = pixel(0xA6A6B1)
ACCENT = pixel(0xBD93F9)
AMBER = pixel(0xFFDD78)
CHIP = pixel(0x35303F)
HEADER = tuple(pixel(rgb) for rgb in (0x212125, 0x242428, 0x27272C, 0x292930))


def text(frame, label, x, y, color=DIM, font=small):
    glyphs = font.BOLD if font is large else font.REGULAR
    dot = frame.pixel
    for ch in label:
        offset = (ord(ch) - font.FIRST) * font.HEIGHT
        for row in range(font.HEIGHT):
            bits = glyphs[offset + row]
            for col in range(font.WIDTH):
                if not bits & (0x80 >> col):
                    dot(x + col, y + row, color)
        x += font.WIDTH


def single_line(value):
    return sanitize(value).replace("\n", " ").replace("\t", " ")


def visible(env):
    panel = env.quickpanel
    return not env.sd_busy and (panel is None or not panel.opened)


def restore_terminal(env):
    env.app_display = None
    panel = env.quickpanel
    if panel is None or not panel.opened:
        env.term.top_offset(env.status_height)  # redraw the area covered by the app
