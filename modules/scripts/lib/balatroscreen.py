"""Pixel-art poker cards and game view for the T-Deck."""

import framebuf
import time

from balatrogame import JOKERS, RANKS, TAROTS, item_info
from bin.balatro import CONTROLS, heading, hidden_jokers, joker_names, menu_heading, menu_row, phase_lines, play_preview
from pixelui import pixel, text, visible

BG = pixel(0x102725)
BAR = pixel(0x223B3D)
FELT = pixel(0x194239)
SHADOW = pixel(0x0B2925)
PAPER = pixel(0xFFF1D5)
EDGE = pixel(0xBDAD91)
INK = pixel(0x252944)
RED = pixel(0xB92F48)
TEXT = pixel(0xFAF2DD)
DIM = pixel(0xA7C6B9)
GOLD = pixel(0xFFC66D)
MINT = pixel(0x79E0AF)
VIOLET = pixel(0x7850A0)
TEAL = pixel(0x25837D)
BLUE = pixel(0x3967A2)
PEACH = pixel(0xF4BD8D)

# Nine 9-bit rows per suit, ordered clubs, diamonds, hearts, spades.
PIPS = (
    (0x038, 0x07C, 0x07C, 0x038, 0x0FE, 0x1FF, 0x1FF, 0x0D6, 0x038),
    (0x010, 0x038, 0x07C, 0x0FE, 0x1FF, 0x0FE, 0x07C, 0x038, 0x010),
    (0x0C6, 0x1EF, 0x1FF, 0x1FF, 0x0FE, 0x07C, 0x038, 0x010, 0x000),
    (0x010, 0x038, 0x07C, 0x0FE, 0x1FF, 0x1FF, 0x0EE, 0x010, 0x038),
)

# Hat halves, face, outline, and bells. Dots leave the card face showing.
JESTER = (
    "5.............5",
    ".11....5....22.",
    "..111.121.222..",
    "..11111222222..",
    "...111122222...",
    "...111122222...",
    "....4444444....",
    "...433333334...",
    "...434333434...",
    "...433333334...",
    "....4344434....",
    "....4333334....",
    ".....44444.....",
    "...112232211...",
    "..11112321111..",
    "..5...151...5..",
)

CARD_W = 36
CARD_H = 66
HAND_X = 5
HAND_Y = 76
JOKER_W = 40
JOKER_H = 59
GAP = 3
BACK_W = 38
BACK_H = 46
DECK_X = 276
DECK_Y = 144
DEAL_MS = 20
LINE_W = 156  # 26 characters in the shared 6x12 font
LINE_H = 12


def _rounded(frame, x, y, w, h, color):
    frame.fill_rect(x + 2, y, w - 4, h, color)
    frame.fill_rect(x + 1, y + 1, w - 2, h - 2, color)
    frame.fill_rect(x, y + 2, w, h - 4, color)


def draw_card(frame, card, slot, selected, focused, debuffed=False):
    frame.fill(FELT)
    y = 1 if selected else 7
    border = GOLD if focused else MINT if selected else EDGE
    _rounded(frame, 3, y + 3, 32, 47, SHADOW)
    _rounded(frame, 1, y, 32, 47, border)
    _rounded(frame, 3, y + 2, 28, 43, PAPER)
    enhancement, card = divmod(card, 64)
    suit = card // 13
    ink = RED if suit in (1, 2) else INK
    rank = RANKS[card % 13]
    rank = "10" if rank == "T" else rank
    if enhancement:
        text(frame, " BMSG"[enhancement], 24, y + 2, BLUE)
    text(frame, rank, 5, y + 2, ink)
    text(frame, rank, 31 - len(rank) * 6, y + 33, ink)
    for row, bits in enumerate(PIPS[suit]):
        for col in range(9):
            if bits & (0x100 >> col):
                frame.fill_rect(8 + 2 * col, y + 15 + 2 * row, 2, 2, ink)
    label = "[%d]" % slot if selected else str(slot)
    text(frame, label, (CARD_W - len(label) * 6) // 2, 54,
         GOLD if focused else MINT if selected else DIM)


def draw_joker(frame, joker, focused):
    _, _, effect, _, _ = joker
    if effect in ("moneychips", "cash", "moneymult"):
        hat = (TEAL, RED)
    elif effect in ("discards", "deck", "pairchips", "facechips", "odd"):
        hat = (BLUE, TEAL)
    elif effect in ("mult", "half", "contains"):
        hat = (RED, VIOLET)
    else:
        hat = (VIOLET, TEAL)
    colors = hat + (PEACH, INK, GOLD)
    frame.fill(FELT)
    y = 0 if focused else 4
    _rounded(frame, 3, y + 2, 36, 53, SHADOW)
    _rounded(frame, 1, y, 36, 53, GOLD if focused else EDGE)
    _rounded(frame, 3, y + 2, 32, 49, PAPER)
    text(frame, "J", 5, y + 2, hat[0])
    text(frame, "J", 28, y + 40, hat[1])
    for row, pixels in enumerate(JESTER):
        for col, shade in enumerate(pixels):
            if shade != ".":
                frame.fill_rect(4 + 2 * col, y + 11 + 2 * row, 2, 2, colors[ord(shade) - 49])


def draw_back(frame, remaining=None, total=None):
    frame.fill(FELT)
    if remaining is None:
        x, y, w, h = 4, 2, 30, 42
    else:
        x, y, w, h = 4, 0, 27, 29
        for offset in range(min(2, remaining - 1), 0, -1):
            _rounded(frame, x + 2 * offset, y + 2 * offset, w, h, EDGE)
        label = "%d/%d" % (remaining, total)
        text(frame, label, max(0, (BACK_W - 6 * len(label)) // 2), 34, DIM)
    if remaining == 0:
        _rounded(frame, x, y, w, h, EDGE)
        _rounded(frame, x + 1, y + 1, w - 2, h - 2, FELT)
        return
    _rounded(frame, x, y, w, h, PAPER)
    _rounded(frame, x + 2, y + 2, w - 4, h - 4, RED)
    frame.rect(x + 4, y + 4, w - 8, h - 8, INK)
    center_x = x + w // 2
    center_y = y + h // 2
    for dy in range(-5, 6):
        half = 5 - abs(dy)
        frame.hline(center_x - half, center_y + dy, 2 * half + 1, GOLD)
    frame.pixel(center_x, center_y, RED)


class Screen:
    def __init__(self, env):
        self.env = env
        self.x = (env.screen_width - 320) // 2
        self.top = env.status_height
        self.height = env.screen_height - self.top
        self.dirty = True
        self.phase = None
        self.last_dealt = None
        self.pending = ()
        self.flight = None
        # Cards and text are sent in turn using the same 4,752-byte scratch buffer.
        self.buf = bytearray(CARD_W * CARD_H * 2)
        self.card = framebuf.FrameBuffer(self.buf, CARD_W, CARD_H, framebuf.RGB565)
        self.joker = framebuf.FrameBuffer(self.buf, JOKER_W, JOKER_H, framebuf.RGB565)
        self.line = framebuf.FrameBuffer(self.buf, LINE_W, LINE_H, framebuf.RGB565)
        self.back = framebuf.FrameBuffer(self.buf, BACK_W, BACK_H, framebuf.RGB565)

    @property
    def animating(self):
        return bool(self.pending)

    def finish_deal(self):
        if self.animating:
            self.pending = ()
            self.flight = None
            self.dirty = True

    def _draw_deck(self, game):
        draw_back(self.back, len(game.deck) + len(self.pending), len(game.master))
        self._send(DECK_X, DECK_Y, BACK_W, BACK_H)

    def _draw_slot(self, game, index):
        draw_card(self.card, game.cards[index], index + 1, index in game.selected,
                  index == game.cursor, game.phase == "play" and game.debuffed(game.cards[index]))
        self._send(HAND_X + index * (CARD_W + GAP), HAND_Y, CARD_W, CARD_H)

    def _play_caption(self, game):
        self._line(play_preview(game)[:45], 155, GOLD, FELT)
        self._line(game.message[:45], 174, DIM, FELT)

    def _erase_flight(self, game):
        if self.flight is None:
            return
        x, y = self.flight
        self._fill(x, y, BACK_W, BACK_H, FELT)
        if y < HAND_Y + CARD_H and y + BACK_H > HAND_Y:
            for index in range(len(game.cards)):
                left = HAND_X + index * (CARD_W + GAP)
                if index not in self.pending and left < x + BACK_W and left + CARD_W > x:
                    self._draw_slot(game, index)
        if x + BACK_W > DECK_X and y + BACK_H > DECK_Y:
            self._draw_deck(game)
        self.flight = None

    def step_deal(self, game, now):
        if time.ticks_diff(now, self.frame_at) < DEAL_MS:
            return
        self.frame_at = now
        self._erase_flight(game)
        index = self.pending[0]
        self.flight_step += 1
        if self.flight_step == 4:
            self.pending = self.pending[1:]
            self.flight_step = 0
            self._draw_slot(game, index)
            if not self.pending:
                self._play_caption(game)
            self._draw_deck(game)
        else:
            end_x = HAND_X + index * (CARD_W + GAP)
            end_y = HAND_Y + 8
            x = DECK_X + (end_x - DECK_X) * self.flight_step // 4
            y = DECK_Y + (end_y - DECK_Y) * self.flight_step // 4
            draw_back(self.back)
            self._send(x, y, BACK_W, BACK_H)
            self.flight = x, y

    def _send(self, x, y, w, h):
        if visible(self.env):
            self.env.tft.blit_buffer(self.buf, self.x + x, self.top + y, w, h)
        else:
            self.dirty = True

    def _fill(self, x, y, w, h, color):
        if visible(self.env):
            # The direct fill takes RGB565, while the scratch buffer is byte-swapped.
            self.env.tft.fill_rect(self.x + x, self.top + y, w, h,
                                   ((color & 255) << 8) | (color >> 8))
        else:
            self.dirty = True

    def _line(self, label, y, ink=TEXT, bg=BG):
        self._half_line(0, label[:26], y, ink, bg)
        self._half_line(1, label[26:52], y, ink, bg)

    def _half_line(self, index, label, y, ink, bg, centered=False):
        self.line.fill(bg)
        x = (LINE_W - 6 * len(label)) // 2 if centered else 0
        text(self.line, label, x, 0, ink)
        self._send(4 + index * LINE_W, y, LINE_W, LINE_H)

    def _draw_menu(self, game):
        self._line(menu_heading(game), 74, MINT, FELT)
        items = game.items()
        for index in range(8):
            item = items[index] if index < len(items) else None
            label = menu_row(game, item, index) if index < len(items) else ""
            focused = index == game.cursor and index < len(items)
            prefix = "> " if focused else "  "
            self._half_line(0, (prefix + label)[:26], 86 + index * 12,
                            GOLD if focused else TEXT, FELT)
        self._fill(160, 89, 156, 100, FELT)
        if not items or items[game.cursor] is None:
            return
        kind, key, _ = item = items[game.cursor]
        if kind == "joker" and hidden_jokers(game):
            draw_back(self.back)
            self._send(218, 89, BACK_W, BACK_H)
            return
        name, description, _ = item_info(item)
        if kind == "joker":
            draw_joker(self.joker, JOKERS[key], True)
            self._send(218, 89, JOKER_W, JOKER_H)
        elif kind == "card":
            draw_card(self.card, key, 1, False, True)
            self._send(220, 88, CARD_W, CARD_H)
        self._half_line(1, name[:26], 148, GOLD, FELT, centered=True)
        lines = [""]
        for word in description.split():
            if len(lines[-1]) + len(word) + 1 > 26:
                lines.append(word)
            else:
                lines[-1] = (lines[-1] + " " + word).strip()
        for index, label in enumerate(lines[:2]):
            self._half_line(1, label, 158 + 12 * index, TEXT, FELT, centered=True)

    def draw(self, game, now=None):
        full = self.dirty or self.phase != game.phase
        self.dirty = False
        self.phase = game.phase
        phase = game.phase
        menu = phase in ("shop", "pack", "inventory")
        self.pending = ()
        self.flight = None
        if phase == "play" and game.dealt is not self.last_dealt:
            self.last_dealt = game.dealt
            if now is not None and game.dealt:
                self.pending = tuple(reversed(game.dealt))
                self.frame_at = now
                self.flight_step = 0
        if full:
            self._fill(0, 0, 320, self.height, BG)
            self._fill(0, 0, 320, 25, BAR)
            self._fill(0, 71, 320, 122 if menu or phase == "play" else 78, FELT)
            first, second = CONTROLS[phase]
            self._line(first, 194, DIM)
            self._line(second, 209, DIM)

        self._line(" %-29s%s" % ("MINI BALATRO", heading(game)), 6, GOLD, BAR)
        self._line("%s   %d/%d" % (game.blind_name, game.score, game.target) if phase != "deck" else "", 27)
        self._fill(4, 42, 312, 4, BAR)
        progress = min(312, 312 * game.score // game.target)
        if progress:
            self._fill(4, 42, progress, 4, MINT)
        self._line("Hands %d   Discards %d   Jokers %s" %
                   (game.hands, game.discards, joker_names(game)) if phase != "deck" else "", 53, DIM)

        if phase in ("play", "tarot"):
            if self.pending and not full:
                self._fill(0, HAND_Y, 320, 193 - HAND_Y, FELT)
            if len(game.cards) < 8:
                left = HAND_X + len(game.cards) * (CARD_W + GAP)
                self._fill(left, HAND_Y, 320 - left, CARD_H, FELT)
            for index in range(len(game.cards)):
                if index not in self.pending:
                    self._draw_slot(game, index)
            if phase == "tarot":
                name, description, _ = TAROTS[game.tarot]
                self._line(name, 151, GOLD)
                self._line(description, 164, TEXT)
            else:
                if not self.pending:
                    self._play_caption(game)
                self._draw_deck(game)
        elif menu:
            self._draw_menu(game)
        elif full or phase == "deck":
            for index, label in enumerate(phase_lines(game)):
                y = 76 + index * 15
                self._line(label, y, MINT if index == 0 else TEXT, FELT if y + 12 <= 149 else BG)

        if phase == "tarot":
            self._line(game.message, 179, DIM)
        elif menu:
            self._line(game.message, 182, DIM)
        elif phase != "play":
            self._line(game.message, 175, DIM)
