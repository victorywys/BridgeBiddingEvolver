"""Deal generation + hand feature extraction.

A ``Deal`` is a partition of the 52-card deck into four hands keyed by
seat (N, E, S, W). ``HandFeatures`` exposes the symbolic features the
bidding DSL is allowed to reference, so conventions never depend on raw
card identities.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .deck import Card, RANKS, SUITS, Suit

SEATS: Tuple[str, ...] = ("N", "E", "S", "W")
SEAT_INDEX = {s: i for i, s in enumerate(SEATS)}


@dataclass(frozen=True)
class Hand:
    cards: Tuple[Card, ...]

    def by_suit(self) -> Dict[Suit, List[Card]]:
        out: Dict[Suit, List[Card]] = {s: [] for s in SUITS}
        for c in self.cards:
            out[c.suit].append(c)
        for s in SUITS:
            out[s].sort(key=lambda c: -RANKS.index(c.rank))
        return out

    def pretty(self) -> str:
        bs = self.by_suit()
        order = [Suit.SPADES, Suit.HEARTS, Suit.DIAMONDS, Suit.CLUBS]
        return " ".join(
            f"{s.symbol}:" + ("".join(c.rank for c in bs[s]) or "-")
            for s in order
        )


@dataclass(frozen=True)
class Deal:
    hands: Dict[str, Hand]  # seat -> hand
    dealer: str = "N"
    seed: int = 0

    def hand(self, seat: str) -> Hand:
        return self.hands[seat]


def deal_random(seed: int | None = None, dealer: str = "N") -> Deal:
    rng = random.Random(seed)
    deck = [Card(s, r) for s in SUITS for r in RANKS]
    rng.shuffle(deck)
    hands = {seat: Hand(tuple(deck[i * 13:(i + 1) * 13])) for i, seat in enumerate(SEATS)}
    return Deal(hands=hands, dealer=dealer, seed=seed if seed is not None else -1)


# ---------------------------------------------------------------------------
# Symbolic features exposed to conventions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandFeatures:
    hcp: int
    length: Dict[str, int]            # "C"/"D"/"H"/"S" -> length
    longest_suit: str                  # symbol
    distribution: Tuple[int, ...]      # sorted descending
    shape: str                         # e.g. "5-3-3-2"
    balanced: bool
    semi_balanced: bool
    has_5cm: bool                      # 5+ card major
    has_5cM: bool = field(default=False)  # alias kept for readability
    has_6c_suit: bool = False
    four_card_majors: Tuple[str, ...] = ()
    five_card_majors: Tuple[str, ...] = ()
    void_suits: Tuple[str, ...] = ()
    singleton_suits: Tuple[str, ...] = ()
    quick_tricks: float = 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "HCP": self.hcp,
            "Length": dict(self.length),
            "LongestSuit": self.longest_suit,
            "Distribution": list(self.distribution),
            "Shape": self.shape,
            "Balanced": self.balanced,
            "SemiBalanced": self.semi_balanced,
            "Has5CardMajor": self.has_5cm,
            "Has6CardSuit": self.has_6c_suit,
            "FourCardMajors": list(self.four_card_majors),
            "FiveCardMajors": list(self.five_card_majors),
            "VoidSuits": list(self.void_suits),
            "SingletonSuits": list(self.singleton_suits),
            "QuickTricks": self.quick_tricks,
        }


def hand_features(hand: Hand) -> HandFeatures:
    by = hand.by_suit()
    length = {s.symbol: len(by[s]) for s in SUITS}
    hcp = sum(c.hcp for c in hand.cards)
    dist = tuple(sorted(length.values(), reverse=True))
    shape = "-".join(str(x) for x in dist)
    balanced = dist in ((4, 3, 3, 3), (4, 4, 3, 2), (5, 3, 3, 2))
    semi_bal = balanced or dist in ((5, 4, 2, 2), (6, 3, 2, 2))
    majors_4 = tuple(s.symbol for s in (Suit.HEARTS, Suit.SPADES) if length[s.symbol] >= 4)
    majors_5 = tuple(s.symbol for s in (Suit.HEARTS, Suit.SPADES) if length[s.symbol] >= 5)
    longest = max(length, key=lambda k: (length[k], "CDHS".index(k)))
    voids = tuple(s for s, n in length.items() if n == 0)
    singletons = tuple(s for s, n in length.items() if n == 1)
    # Crude quick-trick count.
    qt = 0.0
    for s in SUITS:
        ranks_in = {c.rank for c in by[s]}
        if {"A", "K"} <= ranks_in:
            qt += 2
        elif "A" in ranks_in and "Q" in ranks_in:
            qt += 1.5
        elif "A" in ranks_in:
            qt += 1
        elif {"K", "Q"} <= ranks_in:
            qt += 1
        elif "K" in ranks_in and len(by[s]) >= 2:
            qt += 0.5
    return HandFeatures(
        hcp=hcp,
        length=length,
        longest_suit=longest,
        distribution=dist,
        shape=shape,
        balanced=balanced,
        semi_balanced=semi_bal,
        has_5cm=bool(majors_5),
        has_5cM=bool(majors_5),
        has_6c_suit=any(n >= 6 for n in length.values()),
        four_card_majors=majors_4,
        five_card_majors=majors_5,
        void_suits=voids,
        singleton_suits=singletons,
        quick_tricks=qt,
    )
