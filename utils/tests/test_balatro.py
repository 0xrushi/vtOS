"""Test scoring and the full run flow without a T-Deck."""

from pathlib import Path

import pytest


@pytest.fixture
def game(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "modules" / "scripts"
    monkeypatch.syspath_prepend(str(scripts / "lib"))
    monkeypatch.syspath_prepend(str(scripts))
    import balatrogame

    return balatrogame


def cards(game, names):
    return [game.RANKS.index(name[0]) + 13 * game.SUITS.index(name[1])
            for name in names.split()]


@pytest.mark.parametrize("hand, expected, scored", [
    ("AS KC 9D 7S 3C", "High Card", [0]),
    ("AS AH KC 9D 7S", "Pair", [0, 1]),
    ("AS AH KC KD 7S", "Two Pair", [0, 1, 2, 3]),
    ("AS 2H 3C 4D 5S", "Straight", list(range(5))),
    ("AS AH AC KD KS", "Full House", list(range(5))),
    ("AS 2S 3S 4S 5S", "Straight Flush", list(range(5))),
    ("AS AH AC AD AH", "Five of a Kind", list(range(5))),
    ("AS AS AS KS KS", "Flush House", list(range(5))),
    ("AS AS AS AS AS", "Flush Five", list(range(5))),
])
def test_classification(game, hand, expected, scored):
    assert game.classify(cards(game, hand))[:2] == (expected, scored)


def test_scoring_planets_card_modifiers_and_joker_order(game):
    run = game.Run()
    run.start_blind()
    run.cards = cards(game, "AS AH KC")
    run.cards[2] += 64 * game.STEEL
    run.selected = {0, 1}
    run.jokers = [0, 28]  # Joker and The Duo
    run.use_planet("Pair")
    assert run.preview() == ("Pair", 47, 17)
    run.jokers.reverse()
    assert run.preview() == ("Pair", 47, 13)


def test_boss_debuff_and_rejected_hand(game):
    run = game.Run()
    run.blind = 2
    run.boss = next(boss for boss in game.BOSSES if boss[2] == "H")
    run.start_blind()
    run.cards = cards(game, "AS AH KC")
    run.selected = {0, 1}
    assert run.preview() == ("Pair", 21, 2)
    run.boss = next(boss for boss in game.BOSSES if boss[2] == "psychic")
    assert run.preview() == ("Pair", 0, 0)
    run.submit()
    assert run.score == 0 and run.hands == 3


def test_refill_marks_only_new_slots_even_with_duplicate_cards(game):
    run = game.Run(6)
    run.start_blind()
    ace, four, seven = cards(game, "AS 4H 7H")
    run.cards = [ace, four]
    run.deck = [ace, seven]
    run.refill()
    assert run.cards == [ace, ace, seven, four]
    assert run.dealt == (1, 2)
    run.refill()
    assert run.dealt == ()


def test_full_ante_progression_and_payout(game):
    run = game.Run()
    for ante, base in enumerate(game.ANTES):
        for blind, target in enumerate((base, base * 3 // 2, base * 2)):
            if blind == 2:
                run.boss = ("Test boss", 1, "", "")
            assert (run.phase, run.ante, run.blind, run.target) == ("blind", ante, blind, target)
            run.start_blind()
            assert (run.hands, run.discards, len(run.cards)) == (4, 4, 8)
            run.score = target - 1
            run.selected = {0}
            run.submit()
            assert run.phase == "cashout"
            expected = run.money + game.BLIND_PAY[blind] + 3 + min(5, run.money // 5)
            run.cash_out()
            assert run.money == expected
            if (ante, blind) != (7, 2):
                assert run.phase == "shop"
                assert len(run.shop) == 5
                run.advance()
    assert run.phase == "won"


def test_shop_restock_and_inventory(game):
    run = game.Run()
    run.start_blind()
    run.cash_out()
    run.money = 50
    pack, second_pack, voucher = run.shop[2:]
    run.reroll()
    assert (run.money, run.reroll_cost) == (45, 6)
    assert run.shop[2:] == [pack, second_pack, voucher]
    run.shop[0] = ("joker", 0, 2)
    run.buy()
    assert run.jokers == [0] and run.shop[0] is None and run.money == 43
    run.shop[1] = ("planet", "Pair", 3)
    run.cursor = 1
    run.buy()
    run.open_inventory()
    run.cursor = 1
    run.use_consumable()
    assert run.consumables == [] and run.levels == {"Pair": 2}
    run.sell()
    assert not run.jokers and run.money == 41
    run.close_inventory()
    run.cursor = 2
    run.shop[2] = ("pack", 0, 4)
    run.buy()
    assert run.phase == "pack" and len(run.pack) == 2
    run.pick_pack()
    assert run.phase == "shop" and len(run.jokers) == 1 and run.shop[2] is None


def test_tarot_upgrades_the_held_card_and_master_deck(game):
    run = game.Run()
    run.start_blind()
    original = run.cards[0]
    run.selected = {0}
    run.consumables.append(("tarot", 2))
    run.open_inventory()
    run.use_consumable()
    assert run.phase == "tarot" and run.selected == {0}
    run.apply_tarot()
    assert run.phase == "inventory" and not run.consumables
    run.close_inventory()
    assert run.phase == "play" and run.cards[0] == original + 64 * game.BONUS
    assert run.cards[0] in run.master and original not in run.master


def test_skip_moves_to_next_blind_and_applies_tag(game):
    run = game.Run()
    run.tags = [0, 4]  # Investment and Economy
    run.skip()
    assert (run.blind, run.skip_count, run.investments) == (1, 1, 1)
    run.skip()
    assert (run.blind, run.skip_count, run.money) == (2, 2, 8)
    run.skip()
    assert (run.blind, run.skip_count) == (2, 2)
    run.start_blind()
    assert run.payout()[3] == 25


def test_terminal_input_flow(game):
    from bin.balatro import handle_key, render

    run = game.Run()
    run.phase = "deck"
    handle_key(run, "d")
    run = handle_key(run, "\r")
    assert run.deck_type == 1
    handle_key(run, "\r")
    assert run.phase == "play" and run.hands == 5
    handle_key(run, "1")
    handle_key(run, "x")
    assert run.discards == 2
    handle_key(run, "i")
    assert run.phase == "inventory"
    assert render(run, 40).count("\x1b[K") == 14
    handle_key(run, "b")
    assert run.phase == "play"
