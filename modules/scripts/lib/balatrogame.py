"""Rules and run state for the T-Deck's poker roguelike."""

import random

RANKS = "23456789TJQKA"
SUITS = "CDHS"
HAND_SIZE = 8
MAX_SELECTED = 5
JOKER_SLOTS = 5
DECKS = (
    ("Red Deck", "+1 discard per round"),
    ("Blue Deck", "+1 hand per round"),
    ("Yellow Deck", "Start with an extra $10"),
    ("Green Deck", "$2 per spare hand, $1 per spare discard. No interest."),
    ("Black Deck", "+1 Joker slot, -1 hand per round"),
    ("Abandoned Deck", "Start with no face cards"),
    ("Checkered Deck", "Start with 26 Hearts and 26 Spades"),
)
ANTES = (300, 800, 2000, 5000, 11000, 20000, 35000, 50000)
BLIND_NAMES = ("Small Blind", "Big Blind", "Boss Blind")
BLIND_PAY = (3, 4, 5)

# Base chips, base mult, chips/level, mult/level, planet.
HANDS = {
    "High Card": (5, 1, 10, 1, "Pluto"),
    "Pair": (10, 2, 15, 1, "Mercury"),
    "Two Pair": (20, 2, 20, 1, "Uranus"),
    "Three of a Kind": (30, 3, 20, 2, "Venus"),
    "Straight": (30, 4, 30, 3, "Saturn"),
    "Flush": (35, 4, 15, 2, "Jupiter"),
    "Full House": (40, 4, 25, 2, "Earth"),
    "Four of a Kind": (60, 7, 30, 3, "Mars"),
    "Straight Flush": (100, 8, 40, 4, "Neptune"),
    "Five of a Kind": (120, 12, 35, 3, "Planet X"),
    "Flush House": (140, 14, 40, 4, "Ceres"),
    "Flush Five": (160, 16, 50, 3, "Eris"),
}
SECRET_HANDS = ("Five of a Kind", "Flush House", "Flush Five")
BASIC_HANDS = tuple(hand for hand in HANDS if hand not in SECRET_HANDS)

# Name, cost, trigger, amount, description.
JOKERS = (
    ("Joker", 2, "mult", 4, "+4 Mult"),
    ("Jolly Joker", 3, "contains", (2, 8), "+8 Mult if hand has a Pair"),
    ("Zany Joker", 4, "contains", (3, 12), "+12 Mult with Three of a Kind"),
    ("Mad Joker", 4, "twopair", 10, "+10 Mult with Two Pair"),
    ("Crazy Joker", 4, "straight", 12, "+12 Mult with a Straight"),
    ("Droll Joker", 4, "flush", 10, "+10 Mult with a Flush"),
    ("Sly Joker", 3, "pairchips", 50, "+50 Chips if hand has a Pair"),
    ("Half Joker", 5, "half", 20, "+20 Mult with 3 or fewer cards"),
    ("Banner", 5, "discards", 30, "+30 Chips per discard left"),
    ("Mystic Summit", 5, "nodiscard", 15, "+15 Mult with no discards left"),
    ("Blue Joker", 5, "deck", 2, "+2 Chips per card left in deck"),
    ("Abstract Joker", 4, "owned", 3, "+3 Mult per owned Joker"),
    ("Scary Face", 4, "facechips", 30, "+30 Chips per scoring face card"),
    ("Smiley Face", 4, "facemult", 5, "+5 Mult per scoring face card"),
    ("Even Steven", 4, "even", 4, "+4 Mult per scoring even rank"),
    ("Odd Todd", 4, "odd", 31, "+31 Chips per scoring odd rank"),
    ("Scholar", 4, "ace", 0, "+20 Chips and +4 Mult per Ace"),
    ("Greedy Joker", 5, "suit", 1, "+3 Mult per scoring Diamond"),
    ("Lusty Joker", 5, "suit", 2, "+3 Mult per scoring Heart"),
    ("Wrathful Joker", 5, "suit", 3, "+3 Mult per scoring Spade"),
    ("Gluttonous Joker", 5, "suit", 0, "+3 Mult per scoring Club"),
    ("Golden Joker", 6, "cash", 4, "+$4 at the end of each round"),
    ("Bull", 6, "moneychips", 2, "+2 Chips for each dollar"),
    ("Bootstraps", 7, "moneymult", 2, "+2 Mult for every $5"),
    ("Supernova", 5, "nova", 0, "+Mult equal to plays of this hand"),
    ("Acrobat", 6, "last", 3, "X3 Mult on the final hand"),
    ("Card Sharp", 6, "repeat", 3, "X3 Mult on a repeated hand"),
    ("Joker Stencil", 8, "stencil", 0, "X1 Mult per empty slot, plus itself"),
    ("The Duo", 8, "duo", 2, "X2 Mult if hand has a Pair"),
    ("The Trio", 8, "trio", 3, "X3 Mult with Three of a Kind"),
    ("The Order", 8, "xstraight", 3, "X3 Mult with a Straight"),
    ("The Tribe", 8, "xflush", 2, "X2 Mult with a Flush"),
    ("Constellation", 6, "planets", 0, "Gains X0.1 Mult per Planet used"),
    ("Throwback", 6, "skips", 0, "Gains X0.25 Mult per skipped Blind"),
    ("Splash", 3, "splash", 0, "Every played card scores"),
)

# Name, minimum ante, rule, description.
BOSSES = (
    ("The Hook", 1, "hook", "Discards 2 random cards after each hand"),
    ("The Club", 1, "C", "Clubs are debuffed"),
    ("The Window", 1, "D", "Diamonds are debuffed"),
    ("The Head", 1, "H", "Hearts are debuffed"),
    ("The Goad", 1, "S", "Spades are debuffed"),
    ("The Psychic", 1, "psychic", "Must play 5 cards"),
    ("The Wall", 2, "wall", "Extra large blind"),
    ("The Water", 2, "water", "Start with zero discards"),
    ("The Manacle", 1, "manacle", "Hand size reduced by 1"),
    ("The Eye", 3, "eye", "Each poker hand type can score once"),
    ("The Mouth", 2, "mouth", "Only the first hand type can score"),
    ("The Arm", 2, "arm", "Played hand loses a level"),
    ("The Needle", 2, "needle", "Play only one hand"),
    ("The Plant", 4, "plant", "Face cards are debuffed"),
    ("The Flint", 1, "flint", "Base Chips and Mult are halved"),
    ("The Tooth", 3, "tooth", "Lose $1 per played card"),
    ("The Ox", 6, "ox", "Most played hand sets money to $0"),
)
FINISHERS = (
    ("Violet Vessel", 8, "vessel", "Extra large blind"),
    ("Cerulean Bell", 8, "bell", "Forces one card to be selected"),
    ("Crimson Heart", 8, "heart", "Disables one Joker each hand"),
    ("Amber Acorn", 8, "acorn", "Shuffles and hides your Jokers"),
    ("Verdant Leaf", 8, "leaf", "Cards debuffed until you sell a Joker"),
)

TAGS = ("Investment", "Handy", "Garbage", "Speed", "Economy", "Orbital")
TAG_TEXT = (
    "$25 after the Boss Blind", "$1 per hand played so far",
    "$1 per discard used so far", "$5 per skipped Blind",
    "Double money, up to $40", "Level a poker hand 3 times",
)
VOUCHERS = (
    ("Grabber", "+1 hand each round"),
    ("Wasteful", "+1 discard each round"),
    ("Overstock", "+1 card in future shops"),
    ("Clearance Sale", "Cards and packs cost 25% less"),
    ("Seed Money", "Raise the interest cap to $10"),
)
PACKS = ("Buffoon Pack", "Celestial Pack", "Arcana Pack", "Standard Pack")
TAROTS = (
    ("The Fool", "Create the last Planet or Tarot used", 0),
    ("The Empress", "Give 1 or 2 cards +4 Mult", 2),
    ("The Hierophant", "Give 1 or 2 cards +30 Chips", 2),
    ("Strength", "Raise the rank of 1 or 2 cards", 2),
    ("The Hanged Man", "Destroy 1 or 2 cards", 2),
    ("Death", "Turn the left card into the right card", 2),
    ("Temperance", "Cash equal to Joker sell value", 0),
    ("The Hermit", "Double money, up to $20", 0),
    ("The Star", "Turn up to 3 cards into Diamonds", 3),
    ("The Moon", "Turn up to 3 cards into Clubs", 3),
    ("The Sun", "Turn up to 3 cards into Hearts", 3),
    ("The World", "Turn up to 3 cards into Spades", 3),
    ("The Chariot", "One held card gives X1.5 Mult", 1),
    ("Justice", "One card gives X2 Mult, may break", 1),
)
BONUS, MULT, STEEL, GLASS = 1, 2, 3, 4


def rank(card):
    return (card % 64) % 13 + 2


def suit(card):
    return (card % 64) // 13


def sample(pool, count):
    pool = list(pool)
    return [pool.pop(random.randrange(len(pool))) for _ in range(min(count, len(pool)))]


def classify(cards):
    ranks = [rank(card) for card in cards]
    counts = {}
    for value in ranks:
        counts[value] = counts.get(value, 0) + 1
    groups = sorted(counts.values(), reverse=True)
    biggest = groups[0]
    straight = flush = False
    if len(cards) == 5:
        distinct = sorted(counts)
        straight = len(distinct) == 5 and (
            distinct[-1] - distinct[0] == 4 or distinct == [2, 3, 4, 5, 14]
        )
        flush = all(suit(card) == suit(cards[0]) for card in cards)
    if flush and biggest == 5:
        name = "Flush Five"
    elif flush and groups == [3, 2]:
        name = "Flush House"
    elif biggest == 5:
        name = "Five of a Kind"
    elif flush and straight:
        name = "Straight Flush"
    elif biggest == 4:
        name = "Four of a Kind"
    elif groups == [3, 2]:
        name = "Full House"
    elif flush:
        name = "Flush"
    elif straight:
        name = "Straight"
    elif biggest == 3:
        name = "Three of a Kind"
    elif groups[:2] == [2, 2]:
        name = "Two Pair"
    elif biggest == 2:
        name = "Pair"
    else:
        name = "High Card"

    if name == "High Card":
        scoring = [ranks.index(max(ranks))]
    elif name in ("Pair", "Two Pair", "Three of a Kind", "Four of a Kind"):
        scoring = [i for i, value in enumerate(ranks) if counts[value] > 1]
    else:
        scoring = list(range(len(cards)))
    return name, scoring, biggest, len(groups) > 1 and groups[1] >= 2, straight, flush


def item_info(item):
    kind, key, price = item
    if kind == "joker":
        name, _, _, _, description = JOKERS[key]
    elif kind == "planet":
        name, description = HANDS[key][4], "Level up " + key
    elif kind == "tarot":
        name, description, _ = TAROTS[key]
    elif kind == "voucher":
        name, description = VOUCHERS[key]
    elif kind == "pack":
        name, description = PACKS[key], "Choose 1 of %d cards" % (2 if key == 0 else 3)
    else:
        enhancement = ("", "Bonus ", "Mult ", "Steel ", "Glass ")[key // 64]
        name = enhancement + RANKS[rank(key) - 2] + SUITS[suit(key)]
        description = "Add this card to your deck"
    return name, description, price


class Run:
    def __init__(self, deck=0):
        self.deck_type = deck
        self.joker_slots = JOKER_SLOTS + (deck == 4)
        self.ante = 0
        self.blind = 0
        self.money = 14 if deck == 2 else 4
        self.jokers = []
        self.consumables = []
        self.vouchers = set()
        self.levels = {}
        self.times_played = {}
        self.discard_count = self.skip_count = 0
        self.investments = 0
        self.planets_used = {}
        self.last_consumable = None
        self.master = list(range(52))
        self.dealt = ()
        if deck == 5:
            self.master = [card for card in self.master if not 11 <= rank(card) <= 13]
        elif deck == 6:
            self.master = [26 + card % 13 + 13 * (suit(card) in (0, 3)) for card in self.master]
        self.used_bosses = set()
        self.round = 0
        self.new_ante()
        self.choose_blind()

    @property
    def rule(self):
        return self.boss[2] if self.blind == 2 else ""

    @property
    def blind_name(self):
        return self.boss[0] if self.blind == 2 else BLIND_NAMES[self.blind]

    @property
    def target(self):
        scale = {"wall": 4, "vessel": 6, "needle": 1}.get(self.rule, 2)
        return ANTES[self.ante] * (2, 3, scale * 2)[self.blind] // 2

    def new_ante(self):
        if self.ante == len(ANTES) - 1:
            pool = FINISHERS
        else:
            pool = [boss for boss in BOSSES if boss[1] <= self.ante + 1 and boss not in self.used_bosses]
        self.boss = pool[random.randrange(len(pool))]
        self.used_bosses.add(self.boss)
        self.tags = sample(range(len(TAGS)), 2)
        available = [i for i in range(len(VOUCHERS)) if i not in self.vouchers]
        self.voucher = available[random.randrange(len(available))] if available else None

    def choose_blind(self):
        self.phase = "blind"
        self.score = 0
        self.hands = self.discards = 0
        self.message = ""
        self.cursor = 0

    def advance(self):
        self.blind += 1
        if self.blind == 3:
            self.blind = 0
            self.ante += 1
            self.new_ante()
        self.choose_blind()

    def skip(self):
        if self.blind == 2:
            self.message = "Boss Blinds cannot be skipped."
            return
        tag = self.tags[self.blind]
        self.skip_count += 1
        if tag == 0:
            self.investments += 1
        elif tag == 5:
            hand = BASIC_HANDS[random.randrange(len(BASIC_HANDS))]
            self.levels[hand] = self.levels.get(hand, 1) + 3
        else:
            self.money += (0, sum(self.times_played.values()), self.discard_count,
                           self.skip_count * 5, min(40, self.money))[tag]
        self.advance()
        self.message = TAGS[tag] + " Tag collected."

    def start_blind(self):
        self.phase = "play"
        self.round += 1
        hands = 4 + (0 in self.vouchers) + (self.deck_type == 1) - (self.deck_type == 4)
        discards = 3 + (1 in self.vouchers) + (self.deck_type == 0)
        self.hands = 1 if self.rule == "needle" else hands
        self.discards = 0 if self.rule == "water" else discards
        self.deck = list(self.master)
        self.cards = []
        self.selected = set()
        self.cursor = 0
        self.first_hand = None
        self.round_plays = {}
        self.leaf_sold = False
        self.disabled = self.last_disabled = None
        self.ox_hand = max(self.times_played, key=self.times_played.get) if self.times_played else "High Card"
        if self.rule == "acorn":
            self.jokers = sample(self.jokers, len(self.jokers))
        self.message = ""
        self.refill()

    def refill(self, rotate_joker=True):
        size = HAND_SIZE - (self.rule == "manacle")
        held = len(self.cards)
        while len(self.cards) < size and self.deck:
            self.cards.append(self.deck.pop(random.randrange(len(self.deck))))
        order = sorted(range(len(self.cards)),
                       key=lambda i: (rank(self.cards[i]), suit(self.cards[i]), -i), reverse=True)
        self.cards = [self.cards[i] for i in order]
        self.dealt = tuple(slot for slot, i in enumerate(order) if i >= held)
        self.selected.clear()
        self.forced = None
        if self.rule == "bell" and self.cards:
            self.forced = random.randrange(len(self.cards))
            self.selected.add(self.forced)
        if self.rule == "heart" and self.jokers and rotate_joker:
            options = [joker for joker in self.jokers if joker != getattr(self, "last_disabled", None)]
            options = options or self.jokers
            self.disabled = options[random.randrange(len(options))]
            self.last_disabled = self.disabled
        self.cursor = min(self.cursor, max(0, len(self.cards) - 1))

    def toggle(self, index):
        self.cursor = index
        if index == getattr(self, "forced", None):
            self.message = "Cerulean Bell selected this card."
            return
        if index in self.selected:
            self.selected.remove(index)
        elif len(self.selected) < self.selection_limit:
            self.selected.add(index)
        else:
            self.message = "Select up to %d cards." % self.selection_limit
            return
        self.message = ""

    @property
    def selection_limit(self):
        return TAROTS[self.tarot][2] if self.phase == "tarot" else MAX_SELECTED

    def debuffed(self, card):
        rule = self.rule
        return (rule == SUITS[suit(card)] or rule == "plant" and 11 <= rank(card) <= 13
                or rule == "leaf" and not self.leaf_sold)

    def preview(self):
        cards = [self.cards[i] for i in sorted(self.selected)]
        name, scoring, biggest, two_pair, straight, flush = classify(cards)
        rule = self.rule
        if (rule == "psychic" and len(cards) != 5 or rule == "eye" and name in self.round_plays
                or rule == "mouth" and self.first_hand and name != self.first_hand):
            return name, 0, 0
        level = max(1, self.levels.get(name, 1) - (rule == "arm"))
        chips, mult, add_chips, add_mult, _ = HANDS[name]
        chips += (level - 1) * add_chips
        mult += (level - 1) * add_mult
        if rule == "flint":
            chips, mult = (chips + 1) // 2, (mult + 1) // 2
        active = [i for i in self.jokers if i != self.disabled]
        if any(JOKERS[i][2] == "splash" for i in active):
            scoring = range(len(cards))
        scored = [cards[i] for i in scoring if not self.debuffed(cards[i])]
        faces = sum(11 <= rank(card) <= 13 for card in scored)
        aces = sum(rank(card) == 14 for card in scored)
        odds = sum(rank(card) in (3, 5, 7, 9, 14) for card in scored)
        # Per-card effects happen before the ordered Joker row.
        for card in scored:
            value = rank(card)
            enhancement = card // 64
            chips += 11 if value == 14 else min(10, value)
            if enhancement == BONUS:
                chips += 30
            elif enhancement == MULT:
                mult += 4
            elif enhancement == GLASS:
                mult *= 2
            for i in active:
                _, _, effect, amount, _ = JOKERS[i]
                if effect == "suit" and suit(card) == amount:
                    mult += 3
                elif effect == "facemult" and 11 <= value <= 13:
                    mult += amount
                elif effect == "even" and value in (2, 4, 6, 8, 10):
                    mult += amount
                elif effect == "ace" and value == 14:
                    mult += 4
        for i, card in enumerate(self.cards):
            if i not in self.selected and card // 64 == STEEL and not self.debuffed(card):
                mult *= 1.5
        money = max(0, self.money - (len(cards) if rule == "tooth" else 0))
        if rule == "ox" and name == self.ox_hand:
            money = 0
        for i in active:
            _, _, effect, amount, _ = JOKERS[i]
            if effect == "mult":
                mult += amount
            elif effect == "contains" and biggest >= amount[0]:
                mult += amount[1]
            elif (effect == "twopair" and two_pair or effect == "straight" and straight
                  or effect == "flush" and flush or effect == "half" and len(cards) <= 3
                  or effect == "nodiscard" and not self.discards):
                mult += amount
            elif effect == "pairchips" and biggest >= 2:
                chips += amount
            elif effect == "discards":
                chips += amount * self.discards
            elif effect == "deck":
                chips += amount * len(self.deck)
            elif effect == "owned":
                mult += amount * len(self.jokers)
            elif effect == "facechips":
                chips += amount * faces
            elif effect == "odd":
                chips += amount * odds
            elif effect == "ace":
                chips += 20 * aces
            elif effect == "moneychips":
                chips += amount * money
            elif effect == "moneymult":
                mult += amount * (money // 5)
            elif effect == "nova":
                mult += self.times_played.get(name, 0) + 1
            elif (effect == "last" and self.hands == 1 or effect == "repeat" and name in self.round_plays
                  or effect == "duo" and biggest >= 2 or effect == "trio" and biggest >= 3
                  or effect == "xstraight" and straight or effect == "xflush" and flush):
                mult *= amount
            elif effect == "stencil":
                mult *= self.joker_slots - len(self.jokers) + 1
            elif effect == "planets":
                mult *= 1 + self.planets_used.get(i, 0) / 10
            elif effect == "skips":
                mult *= 1 + self.skip_count / 4
        return name, chips, mult

    def submit(self, discard=False):
        if not self.selected:
            self.message = "Select some cards first."
            return
        if discard:
            if not self.discards:
                self.message = "No discards left."
                return
            self.discards -= 1
            self.discard_count += 1
            self.message = "Discarded %d." % len(self.selected)
        else:
            name, chips, mult = self.preview()
            points = int(chips * mult)
            self.hands -= 1
            self.score += points
            self.times_played[name] = self.times_played.get(name, 0) + 1
            self.round_plays[name] = self.round_plays.get(name, 0) + 1
            self.first_hand = self.first_hand or name
            self.message = "%s scored +%d." % (name, points) if points else "Boss blocked that hand."
            if self.rule == "arm":
                self.levels[name] = max(1, self.levels.get(name, 1) - 1)
            elif self.rule == "tooth":
                self.money = max(0, self.money - len(self.selected))
            elif self.rule == "ox" and name == self.ox_hand:
                self.money = 0
            played = [self.cards[i] for i in sorted(self.selected)]
            _, scoring, _, _, _, _ = classify(played)
            if any(JOKERS[i][2] == "splash" for i in self.jokers if i != self.disabled):
                scoring = range(len(played))
            for i in scoring:
                card = played[i]
                if points and card // 64 == GLASS and not self.debuffed(card) and random.randrange(4) == 0:
                    self.master.remove(card)
            if self.score >= self.target:
                self.phase = "cashout"
                return
            if not self.hands:
                self.phase = "lost"
                return

        self.cards = [card for i, card in enumerate(self.cards) if i not in self.selected]
        if not discard and self.rule == "hook":
            for _ in range(min(2, len(self.cards))):
                self.cards.pop(random.randrange(len(self.cards)))
        self.refill(rotate_joker=not discard)
        if not self.cards:
            self.phase = "lost"

    def payout(self):
        interest = min(10 if 4 in self.vouchers else 5, max(0, self.money) // 5)
        bonus = sum(JOKERS[i][3] for i in self.jokers if JOKERS[i][2] == "cash" and i != self.disabled)
        if self.blind == 2:
            bonus += self.investments * 25
        if self.deck_type == 3:
            return BLIND_PAY[self.blind], self.hands * 2, 0, bonus + self.discards
        return BLIND_PAY[self.blind], self.hands, interest, bonus

    def cash_out(self):
        self.money += sum(self.payout())
        if self.blind == 2:
            self.investments = 0
        if self.blind == 2 and self.ante == len(ANTES) - 1:
            self.phase = "won"
        else:
            self.phase = "shop"
            self.cursor = 0
            self.message = ""
            self.reroll_cost = 5
            self.shop_size = 2 + (2 in self.vouchers)
            self.shop = []
            self.roll_shop()
            packs = sample(range(len(PACKS)), 2)
            if self.round == 1 and 0 not in packs:
                packs[0] = 0
            self.shop.extend(("pack", i, 4) for i in packs)
            self.shop.append(("voucher", self.voucher, 10) if self.voucher is not None else None)

    def planet_pool(self):
        return [hand for hand in HANDS if hand in BASIC_HANDS or hand in self.times_played]

    def roll_shop(self):
        offers = []
        joker_pool = [i for i in range(len(JOKERS)) if i not in self.jokers]
        tarot_pool = [i for i in range(len(TAROTS)) if ("tarot", i) not in self.consumables]
        planet_pool = [hand for hand in self.planet_pool() if ("planet", hand) not in self.consumables]
        for _ in range(self.shop_size):
            roll = random.randrange(28)
            if roll < 20 or not (tarot_pool if roll < 24 else planet_pool):
                key = joker_pool.pop(random.randrange(len(joker_pool)))
                offers.append(("joker", key, JOKERS[key][1]))
            elif roll < 24:
                offers.append(("tarot", tarot_pool.pop(random.randrange(len(tarot_pool))), 3))
            else:
                offers.append(("planet", planet_pool.pop(random.randrange(len(planet_pool))), 3))
        self.shop[:self.shop_size] = offers

    def cost(self, item):
        return max(1, item[2] * 3 // 4) if 3 in self.vouchers and item[0] != "voucher" else item[2]

    def reroll(self):
        if self.money < self.reroll_cost:
            self.message = "Need $%d to reroll." % self.reroll_cost
            return
        self.money -= self.reroll_cost
        self.reroll_cost += 1
        self.roll_shop()
        self.message = "Shop rerolled."

    def buy(self):
        item = self.shop[self.cursor]
        if item is None:
            self.message = "Nothing left in this slot."
            return
        kind, key, _ = item
        price = self.cost(item)
        if self.money < price:
            self.message = "Need $%d to buy this." % price
        elif kind == "joker" and len(self.jokers) == self.joker_slots:
            self.message = "Joker slots full. Sell one with I."
        elif kind in ("planet", "tarot") and len(self.consumables) == 2:
            self.message = "Consumable slots full. Use one with I."
        else:
            self.money -= price
            self.shop[self.cursor] = None
            if kind == "joker":
                self.jokers.append(key)
                self.planets_used[key] = 0
            elif kind == "pack":
                self.open_pack(key)
                return
            elif kind == "voucher":
                self.vouchers.add(key)
                self.voucher = None
            else:
                self.consumables.append((kind, key))
            self.message = "Bought %s." % item_info(item)[0]

    def open_pack(self, pack):
        self.shop_cursor = self.cursor
        self.phase, self.cursor = "pack", 0
        self.pack_name = PACKS[pack]
        if pack == 0:
            kind = "joker"
            pool = [i for i in range(len(JOKERS)) if i not in self.jokers]
        elif pack == 1:
            kind, pool = "planet", self.planet_pool()
        elif pack == 2:
            kind, pool = "tarot", range(len(TAROTS))
        else:
            kind, pool = "card", range(52)
        choices = sample(pool, 2 if pack == 0 else 3)
        if kind == "card":
            choices = [card + 64 * random.randrange(1, 5) if random.randrange(5) < 2 else card
                       for card in choices]
        self.pack = [(kind, i, 0) for i in choices]
        self.message = ""

    def close_pack(self):
        self.phase, self.cursor = "shop", self.shop_cursor

    def pick_pack(self):
        kind, key, _ = self.pack[self.cursor]
        if kind == "joker":
            if len(self.jokers) == self.joker_slots:
                self.message = "Joker slots full. Sell one with I."
                return
            self.jokers.append(key)
            self.planets_used[key] = 0
        elif kind == "planet":
            self.use_planet(key)
        elif kind == "tarot":
            self.open_tarot(key)
            return
        else:
            self.master.append(key)
        self.close_pack()

    def open_inventory(self):
        self.inventory_from, self.inventory_cursor = self.phase, self.cursor
        self.phase, self.cursor = "inventory", 0
        self.message = ""

    def close_inventory(self):
        self.phase, self.cursor = self.inventory_from, self.inventory_cursor
        self.message = ""

    def inventory(self):
        items = [("joker", i, max(1, self.cost(("joker", i, JOKERS[i][1])) // 2)) for i in self.jokers]
        items.extend((kind, key, 1) for kind, key in self.consumables)
        return items

    def sell(self):
        items = self.inventory()
        if not items:
            return
        kind, key, value = items[self.cursor]
        if kind == "joker":
            self.jokers.remove(key)
            self.leaf_sold = True
        else:
            self.consumables.remove((kind, key))
        self.money += value
        self.cursor = min(self.cursor, max(0, len(items) - 2))
        self.message = "Sold for $%d." % value

    def reorder(self, delta):
        target = self.cursor + delta
        if 0 <= self.cursor < len(self.jokers) and 0 <= target < len(self.jokers):
            self.jokers[self.cursor], self.jokers[target] = self.jokers[target], self.jokers[self.cursor]
            self.cursor = target

    def use_consumable(self):
        index = self.cursor - len(self.jokers)
        if not 0 <= index < len(self.consumables):
            return
        kind, key = self.consumables[index]
        if kind == "planet":
            self.use_planet(key)
            self.finish_consumable()
        else:
            self.open_tarot(key)

    def use_planet(self, hand):
        self.levels[hand] = self.levels.get(hand, 1) + 1
        for i in self.jokers:
            self.planets_used[i] = self.planets_used.get(i, 0) + 1
        self.last_consumable = ("planet", hand)
        self.message = "%s is level %d." % (hand, self.levels[hand])

    def open_tarot(self, key):
        self.tarot_from = self.phase
        self.tarot_cursor = self.cursor
        self.tarot = key
        if key == 0:
            if self.last_consumable is None:
                self.message = "Use a Planet or Tarot first."
                return
            if len(self.consumables) == 2 and self.phase == "pack":
                self.message = "No space for a consumable."
                return
            self.consumables.append(self.last_consumable)
            self.finish_consumable()
            return
        if key in (6, 7):
            self.money += min(50, sum(item[2] for item in self.inventory() if item[0] == "joker")) if key == 6 else min(20, max(0, self.money))
            self.last_consumable = ("tarot", key)
            self.finish_consumable()
            return
        self.tarot_live = self.phase == "inventory" and self.inventory_from == "play"
        if self.tarot_live:
            remaining = list(enumerate(self.master))
            self.tarot_indices = []
            for held in self.cards:
                position = next(i for i, (_, card) in enumerate(remaining) if card == held)
                self.tarot_indices.append(remaining.pop(position)[0])
        else:
            self.tarot_indices = sample(range(len(self.master)), HAND_SIZE)
        self.saved_cards = getattr(self, "cards", [])
        self.saved_selection = getattr(self, "selected", set())
        self.saved_forced = getattr(self, "forced", None)
        self.cards = [self.master[i] for i in self.tarot_indices]
        self.selected = set(self.saved_selection) if self.tarot_live and len(self.saved_selection) <= TAROTS[key][2] else set()
        self.forced = None
        self.phase, self.cursor = "tarot", 0
        self.message = ""

    def cancel_tarot(self):
        self.cards, self.selected, self.forced = self.saved_cards, self.saved_selection, self.saved_forced
        self.phase, self.cursor = self.tarot_from, self.tarot_cursor

    def apply_tarot(self):
        key = self.tarot
        selected = sorted(self.selected)
        if not selected or key == 5 and len(selected) != 2:
            self.message = "Select exactly two cards." if key == 5 else "Select a card first."
            return
        indices = [self.tarot_indices[i] for i in selected]
        if key == 4 and len(self.master) - len(indices) < HAND_SIZE:
            self.message = "Keep at least eight cards."
            return
        if key == 4:
            for i in sorted(indices, reverse=True):
                self.master.pop(i)
        elif key == 5:
            self.master[indices[0]] = self.master[indices[1]]
        else:
            for i in indices:
                card = self.master[i]
                if key in (1, 2, 12, 13):
                    self.master[i] = card % 64 + {1: MULT, 2: BONUS, 12: STEEL, 13: GLASS}[key] * 64
                elif key == 3:
                    self.master[i] = suit(card) * 13 + (rank(card) - 1) % 13 + card // 64 * 64
                else:
                    self.master[i] = {8: 1, 9: 0, 10: 2, 11: 3}[key] * 13 + rank(card) - 2 + card // 64 * 64
        self.last_consumable = ("tarot", key)
        self.cancel_tarot()
        if self.tarot_live:
            if key == 4:
                self.cards = [card for i, card in enumerate(self.cards) if i not in selected]
                self.refill(rotate_joker=False)
            else:
                self.cards = [self.master[i] for i in self.tarot_indices]
        self.finish_consumable()
        self.message = TAROTS[key][0] + " used."

    def finish_consumable(self):
        if self.phase == "pack":
            self.close_pack()
        elif self.phase == "inventory":
            self.consumables.pop(self.cursor - len(self.jokers))
            self.cursor = min(self.cursor, max(0, len(self.inventory()) - 1))

    def items(self):
        if self.phase == "shop":
            return self.shop
        if self.phase == "pack":
            return self.pack
        return self.inventory()
