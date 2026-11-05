"""Strict complete-deal PBN conversion: clockwise seats and S.H.D.C suits."""

from __future__ import annotations

from bridge_evolve.core.cards import RANKS, Card, Deal, Hand
from bridge_evolve.core.types import Seat
from bridge_evolve.oracle.table import DECLARERS

PBN_SUITS = ("S", "H", "D", "C")


def deal_to_pbn(deal: Deal, first: Seat = Seat.NORTH) -> str:
    if not isinstance(deal, Deal) or not isinstance(first, Seat):
        raise TypeError("deal_to_pbn requires a Deal and a Seat")
    # The core dataclasses accept list-valued cards; revalidate before native code.
    deal = Deal(*(Hand(tuple(deal.hand(seat).cards)) for seat in DECLARERS))
    start = DECLARERS.index(first)
    seats = DECLARERS[start:] + DECLARERS[:start]
    hands = []
    for seat in seats:
        cards = set(deal.hand(seat).cards)
        hands.append(
            ".".join(
                "".join(rank for rank in reversed(RANKS) if Card(rank, suit) in cards)
                for suit in PBN_SUITS
            )
        )
    return f"{first.value}:" + " ".join(hands)


def deal_from_pbn(pbn: str) -> Deal:
    if not isinstance(pbn, str):
        raise TypeError("PBN must be a string")
    if len(pbn) < 3 or pbn[1] != ":":
        raise ValueError("PBN must begin with N:, E:, S:, or W:")
    try:
        first = Seat(pbn[0])
    except ValueError as exc:
        raise ValueError("PBN must begin with N:, E:, S:, or W:") from exc
    raw_hands = pbn[2:].split()
    if len(raw_hands) != 4:
        raise ValueError("PBN requires exactly four complete hands")
    hands: dict[Seat, Hand] = {}
    seat = first
    for raw_hand in raw_hands:
        suits = raw_hand.split(".")
        if len(suits) != 4:
            raise ValueError("PBN hands require four S.H.D.C suit fields")
        cards = []
        for suit, ranks in zip(PBN_SUITS, suits):
            if ranks == "-":
                ranks = ""
            cards.extend(Card(rank, suit) for rank in ranks)
        hands[seat] = Hand(tuple(cards))
        seat = seat.next()
    return Deal(*(hands[seat] for seat in DECLARERS))
