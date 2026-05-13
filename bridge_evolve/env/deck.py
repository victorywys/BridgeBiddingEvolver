"""Card / suit / rank primitives.

Suits are ordered by bridge ranking: Clubs < Diamonds < Hearts < Spades < NT.
Only the four real suits are valid for cards; NT is a contract-only "suit".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Tuple


class Suit(IntEnum):
    CLUBS = 0
    DIAMONDS = 1
    HEARTS = 2
    SPADES = 3
    NT = 4  # contract strain only

    @property
    def symbol(self) -> str:
        return {0: "C", 1: "D", 2: "H", 3: "S", 4: "NT"}[int(self)]

    @property
    def is_major(self) -> bool:
        return self in (Suit.HEARTS, Suit.SPADES)

    @property
    def is_minor(self) -> bool:
        return self in (Suit.CLUBS, Suit.DIAMONDS)

    @classmethod
    def from_symbol(cls, s: str) -> "Suit":
        s = s.upper()
        return {"C": cls.CLUBS, "D": cls.DIAMONDS, "H": cls.HEARTS,
                "S": cls.SPADES, "NT": cls.NT, "N": cls.NT}[s]


SUITS: Tuple[Suit, ...] = (Suit.CLUBS, Suit.DIAMONDS, Suit.HEARTS, Suit.SPADES)
RANKS: Tuple[str, ...] = ("2", "3", "4", "5", "6", "7", "8", "9",
                          "T", "J", "Q", "K", "A")
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}  # 2..14
HCP_VALUE = {"A": 4, "K": 3, "Q": 2, "J": 1}


@dataclass(frozen=True)
class Card:
    suit: Suit
    rank: str  # one of RANKS

    def __post_init__(self) -> None:
        if self.suit == Suit.NT:
            raise ValueError("NT is not a valid card suit")
        if self.rank not in RANK_VALUE:
            raise ValueError(f"bad rank: {self.rank}")

    @property
    def hcp(self) -> int:
        return HCP_VALUE.get(self.rank, 0)

    def __str__(self) -> str:
        return f"{self.suit.symbol}{self.rank}"
