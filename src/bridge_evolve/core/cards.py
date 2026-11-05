"""Card, hand, and deal validation for bridge."""

from __future__ import annotations

from dataclasses import dataclass

from bridge_evolve.core.types import Seat

SUITS = ("C", "D", "H", "S")
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A")


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str

    def __post_init__(self) -> None:
        if self.rank not in RANKS:
            raise ValueError(f"invalid rank: {self.rank!r}")
        if self.suit not in SUITS:
            raise ValueError(f"invalid suit: {self.suit!r}")


@dataclass(frozen=True)
class Hand:
    cards: tuple[Card, ...]

    def __post_init__(self) -> None:
        if len(self.cards) != 13:
            raise ValueError(f"bridge hand must have 13 cards, got {len(self.cards)}")
        if len(set(self.cards)) != 13:
            raise ValueError("bridge hand contains duplicate cards")


@dataclass(frozen=True)
class Deal:
    north: Hand
    east: Hand
    south: Hand
    west: Hand

    def __post_init__(self) -> None:
        all_cards = self.north.cards + self.east.cards + self.south.cards + self.west.cards
        if len(all_cards) != 52:
            raise ValueError(f"bridge deal must have 52 cards, got {len(all_cards)}")
        if len(set(all_cards)) != 52:
            raise ValueError("bridge deal contains duplicate cards across hands")

    def hand(self, seat: Seat) -> Hand:
        if seat == Seat.NORTH:
            return self.north
        if seat == Seat.EAST:
            return self.east
        if seat == Seat.SOUTH:
            return self.south
        return self.west


def full_deck() -> tuple[Card, ...]:
    return tuple(Card(rank=rank, suit=suit) for suit in SUITS for rank in RANKS)

