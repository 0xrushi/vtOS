"""The vtOS entry point and terminal fallback for Mini Balatro."""

import sys

from balatrogame import ANTES, DECKS, JOKERS, RANKS, SUITS, TAGS, TAG_TEXT, TAROTS, Run, item_info
from bin.launcher import _read_key

RESET = "\x1b[0m"
CLEAR = "\x1b[2J\x1b[H"
GOLD = "\x1b[93m"
BLUE = "\x1b[94m"
RED = "\x1b[91m"
GREEN = "\x1b[92m"

CONTROLS = {
    "deck": ("A/D or Up/Down Change Deck", "Enter Start   Q Quit"),
    "blind": ("Enter Play Blind", "S Skip   Q Quit"),
    "play": ("A/D Move   Space/Click or 1-8 Select", "Enter Play   X Discard   I Jokers   Q"),
    "tarot": ("A/D Move   Space/Click or 1-8 Select", "Enter Use Tarot   B Back   Q Quit"),
    "cashout": ("Enter Cash Out", "Q Quit"),
    "shop": ("Up/Down Move   Enter Buy   1-6 Buy", "N Next   R Reroll   I Inventory   Q"),
    "pack": ("Up/Down Move   Enter Pick   1-3 Pick", "X Skip   I Inventory   Q Quit"),
    "inventory": ("Up/Down Move   A/D Reorder Jokers", "Enter Use   X Sell   B Back   Q Quit"),
    "won": ("R New Run", "Q Quit"),
    "lost": ("R New Run", "Q Quit"),
}


def heading(game):
    if game.phase == "deck":
        return "WHITE STAKE"
    return "ANTE %d/%d  $%d" % (game.ante + 1, len(ANTES), game.money)


def joker_names(game):
    if hidden_jokers(game):
        return "%d hidden" % len(game.jokers)
    return ", ".join(JOKERS[i][0] for i in game.jokers) or "-"


def hidden_jokers(game):
    return game.rule == "acorn" and (game.phase == "play" or
                                    game.phase == "inventory" and game.inventory_from == "play")


def menu_heading(game):
    if game.phase == "shop":
        return "Shop   Reroll $%d   Jokers %d/%d" % (game.reroll_cost, len(game.jokers), game.joker_slots)
    if game.phase == "pack":
        return game.pack_name + "   Choose one"
    return "Inventory   Jokers %d/%d   Items %d/2" % (len(game.jokers), game.joker_slots, len(game.consumables))


def menu_row(game, item, index):
    prefix = ("%d. " % (index + 1)) if game.phase != "inventory" else ""
    if item is None:
        return prefix + "Sold out"
    name, _, price = item_info(item)
    if game.phase == "shop":
        return prefix + name + " $%d" % game.cost(item)
    if game.phase == "inventory":
        if item[0] == "joker" and hidden_jokers(game):
            return "Face-down Joker"
        return name + " $%d" % price
    return prefix + name


def phase_lines(game):
    if game.phase == "deck":
        name, effect = DECKS[game.cursor]
        return ["Choose a starting deck", "%d/%d  %s" % (game.cursor + 1, len(DECKS), name), effect]
    if game.phase == "blind":
        if game.blind == 2:
            return [game.blind_name, game.boss[3], "Target %d" % game.target]
        tag = game.tags[game.blind]
        return [game.blind_name, "Target %d" % game.target,
                "Skip for " + TAGS[tag] + " Tag", TAG_TEXT[tag],
                "Boss: " + game.boss[0]]
    if game.phase == "cashout":
        reward, hands, interest, bonus = game.payout()
        return ["Blind cleared!", "Blind reward      $%d" % reward,
                "Unused hands      $%d" % hands, "Interest          $%d" % interest,
                "Other bonuses     $%d" % bonus, "Cash out          $%d" % sum(game.payout())]
    if game.phase == "won":
        return ["You cleared Ante 8!"]
    return ["Run over. Blind missed."]


def play_preview(game):
    if game.phase == "tarot":
        name, description, _ = TAROTS[game.tarot]
        return name + ": " + description
    if not game.selected:
        return game.boss[3] if game.blind == 2 else "Select up to five cards."
    name, chips, mult = game.preview()
    level = game.levels.get(name, 1)
    return "%s L%d  %d x %g = %d" % (name, level, chips, mult, int(chips * mult))


def _card_rows(game):
    tops, faces, bases, slots = [], [], [], []
    for index, card in enumerate(game.cards):
        card %= 64
        selected = index in game.selected
        focused = index == game.cursor
        suit = SUITS[card // 13]
        edge = GREEN if selected else GOLD if focused else BLUE
        face = RED if suit in "DH" else "\x1b[97m"
        tops.append(edge + "+--+" + RESET)
        faces.append(edge + "|" + face + RANKS[card % 13] + suit + edge + "|" + RESET)
        bases.append(edge + "+--+" + RESET)
        slot = " [%d]" if selected else "  %d "
        slots.append(("\x1b[7m" if focused else edge) + slot % (index + 1) + RESET)
    return [" ".join(row) for row in (tops, faces, bases, slots)]


def render(game, cols):
    title = " MINI BALATRO         " + heading(game)
    deck = "   Deck %d/%d" % (len(game.deck), len(game.master)) if game.phase == "play" else ""
    lines = [
        "\x1b[30;103m" + title[:39] + RESET,
        "%s  %d/%d" % (game.blind_name, game.score, game.target),
        "Hands %d    Discards %d%s" % (game.hands, game.discards, deck),
    ]
    if game.phase in ("play", "tarot"):
        lines.append(("Jokers " + joker_names(game))[:39])
        lines.extend(_card_rows(game))
        lines.append(play_preview(game))
    elif game.phase in ("shop", "pack", "inventory"):
        lines.append(menu_heading(game))
        items = game.items()
        first = max(0, game.cursor - 6)
        for index in range(first, min(first + 7, len(items))):
            item = items[index]
            color = GOLD if game.cursor == index else ""
            row = ("> " if color else "  ") + menu_row(game, item, index)
            lines.append(color + row[:39] + RESET)
        if len(lines) < 11 and items and items[game.cursor] and not hidden_jokers(game):
            lines.append(item_info(items[game.cursor])[1])
    else:
        lines.extend(phase_lines(game))
    lines.extend([""] * (11 - len(lines)))
    lines.extend((game.message,) + CONTROLS[game.phase])

    col = (cols - 39) // 2 + 1
    return "".join("\x1b[%d;%dH%s\x1b[K" % (row, col, line[:39] if "\x1b" not in line else line)
                   for row, line in enumerate(lines, 1))


def handle_key(game, key):
    enter = key in ("\r", "\n")
    phase = game.phase
    if phase == "deck":
        if key in ("a", "left", "up", "w"):
            game.cursor = (game.cursor - 1) % len(DECKS)
        elif key in ("d", "right", "down", "s"):
            game.cursor = (game.cursor + 1) % len(DECKS)
        elif enter or key == "esc":
            return Run(game.cursor)
    elif phase in ("play", "tarot"):
        count = len(game.cards)
        if key in ("a", "left"):
            game.cursor = (game.cursor - 1) % count
        elif key in ("d", "right"):
            game.cursor = (game.cursor + 1) % count
        elif key in (" ", "esc"):
            game.toggle(game.cursor)
        elif len(key) == 1 and "1" <= key <= str(count):
            game.toggle(int(key) - 1)
        elif phase == "tarot":
            if enter:
                game.apply_tarot()
            elif key == "b":
                game.cancel_tarot()
        elif key == "i":
            game.open_inventory()
        elif enter or key == "x":
            game.submit(discard=key == "x")
    elif phase == "blind":
        if enter or key == "esc":
            game.start_blind()
        elif key == "s":
            game.skip()
    elif phase == "cashout":
        if enter or key == "esc":
            game.cash_out()
    elif phase in ("shop", "pack", "inventory"):
        count = len(game.items())
        if count and key in ("up", "w"):
            game.cursor = (game.cursor - 1) % count
        elif count and key in ("down", "s"):
            game.cursor = (game.cursor + 1) % count
        elif phase == "inventory":
            if key in ("b", "i", "esc"):
                game.close_inventory()
            elif key == "x":
                game.sell()
            elif key in ("a", "left"):
                game.reorder(-1)
            elif key in ("d", "right"):
                game.reorder(1)
            elif enter:
                game.use_consumable()
        elif key == "i":
            game.open_inventory()
        elif phase == "shop" and key == "n":
            game.advance()
        elif phase == "shop" and key == "r":
            game.reroll()
        elif phase == "pack" and key in ("b", "x"):
            game.close_pack()
        elif enter or key == "esc" or len(key) == 1 and "1" <= key <= str(count):
            if len(key) == 1 and key.isdigit():
                game.cursor = int(key) - 1
            if phase == "shop":
                game.buy()
            else:
                game.pick_pack()
    elif key == "r":
        game = Run()
        game.phase = "deck"
    return game


def main(env, args):
    screen = None
    if (getattr(env, "tft", None) is not None and hasattr(env, "app_display")
            and env.screen_width >= 320 and env.screen_height - env.status_height >= 221):
        try:
            import select
            import time
            from balatroscreen import Screen
            from pixelui import restore_terminal, visible
            poller = select.poll()
            poller.register(sys.stdin, select.POLLIN)
            screen = Screen(env)
        except (ImportError, MemoryError):
            pass
    if screen is None and (env.cols < 40 or env.rows < 14):
        print("Mini Balatro needs a 40x14 terminal.")
        return

    game = Run()
    game.phase = "deck"
    out = sys.stdout
    redraw = True
    try:
        out.write(CLEAR + "\x1b[?25l")
        if screen is not None:
            env.app_display = screen
        while True:
            if screen is None:
                out.write(render(game, env.cols))
            else:
                if visible(env):
                    if redraw or screen.dirty:
                        screen.draw(game, time.ticks_ms())
                        redraw = False
                    elif screen.animating:
                        screen.step_deal(game, time.ticks_ms())
                if not poller.poll(20 if screen.animating else 80):
                    continue
            key = _read_key().lower()
            redraw = True
            if key in ("q", ""):
                return
            if screen is not None:
                screen.finish_deal()
            game = handle_key(game, key)
    except KeyboardInterrupt:
        pass
    finally:
        out.write(RESET + CLEAR + "\x1b[?25h")
        if screen is not None:
            restore_terminal(env)
