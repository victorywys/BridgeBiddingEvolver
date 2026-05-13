"""Auction mechanics: legal calls, history tracking, last contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from .deck import Suit
from .deal import SEATS, SEAT_INDEX


class BidType(Enum):
    PASS = "P"
    DOUBLE = "X"
    REDOUBLE = "XX"
    BID = "B"


@dataclass(frozen=True)
class Bid:
    """A contract bid: level (1..7) and strain.

    For non-contract calls (pass/double) use ``Call`` instead.
    """
    level: int
    strain: Suit

    def __post_init__(self) -> None:
        if not 1 <= self.level <= 7:
            raise ValueError(f"bad level {self.level}")

    def rank(self) -> int:
        return self.level * 5 + int(self.strain)

    def __str__(self) -> str:
        return f"{self.level}{self.strain.symbol}"


@dataclass(frozen=True)
class Call:
    type: BidType
    bid: Optional[Bid] = None
    seat: str = ""

    def __str__(self) -> str:
        if self.type == BidType.BID and self.bid is not None:
            return str(self.bid)
        return self.type.value


def parse_bid(token: str) -> Call:
    t = token.strip().upper()
    if t in ("P", "PASS"):
        return Call(BidType.PASS)
    if t == "X" or t == "DBL":
        return Call(BidType.DOUBLE)
    if t == "XX" or t == "RDBL":
        return Call(BidType.REDOUBLE)
    if len(t) >= 2 and t[0].isdigit():
        level = int(t[0])
        strain = Suit.from_symbol(t[1:])
        return Call(BidType.BID, Bid(level, strain))
    raise ValueError(f"cannot parse bid: {token!r}")


def format_bid(call: Call) -> str:
    return str(call)


@dataclass
class Auction:
    dealer: str = "N"
    calls: List[Call] = field(default_factory=list)

    # ----- core state -----
    def next_seat(self) -> str:
        idx = (SEAT_INDEX[self.dealer] + len(self.calls)) % 4
        return SEATS[idx]

    def last_contract_bid(self) -> Optional[Tuple[Bid, str]]:
        for c in reversed(self.calls):
            if c.type == BidType.BID and c.bid is not None:
                return c.bid, c.seat
        return None

    def doubled_state(self) -> int:
        """0 = undoubled, 1 = doubled, 2 = redoubled (relative to last bid)."""
        state = 0
        for c in reversed(self.calls):
            if c.type == BidType.BID:
                return state
            if c.type == BidType.DOUBLE:
                state = 1
            elif c.type == BidType.REDOUBLE:
                state = 2
        return state

    def is_complete(self) -> bool:
        if len(self.calls) < 4:
            return False
        # Three passes after at least one bid, or four passes overall.
        if all(c.type == BidType.PASS for c in self.calls):
            return True
        if (
            len(self.calls) >= 4
            and all(c.type == BidType.PASS for c in self.calls[-3:])
            and any(c.type == BidType.BID for c in self.calls)
        ):
            return True
        return False

    # ----- legality -----
    def legal_calls(self) -> List[Call]:
        calls: List[Call] = [Call(BidType.PASS)]
        last = self.last_contract_bid()
        # bids
        for level in range(1, 8):
            for strain in (Suit.CLUBS, Suit.DIAMONDS, Suit.HEARTS, Suit.SPADES, Suit.NT):
                bid = Bid(level, strain)
                if last is None or bid.rank() > last[0].rank():
                    calls.append(Call(BidType.BID, bid))
        # double / redouble
        last_non_pass = next((c for c in reversed(self.calls) if c.type != BidType.PASS), None)
        if last_non_pass is not None and last_non_pass.type == BidType.BID:
            # double opponent's bid (only if it was made by an opponent of next_seat).
            opener_seat = last_non_pass.seat
            opp = _are_opponents(self.next_seat(), opener_seat)
            if opp:
                calls.append(Call(BidType.DOUBLE))
        if last_non_pass is not None and last_non_pass.type == BidType.DOUBLE:
            opener_seat = last_non_pass.seat
            opp = _are_opponents(self.next_seat(), opener_seat)
            if not opp:
                calls.append(Call(BidType.REDOUBLE))
        return calls

    def apply(self, call: Call) -> None:
        if call not in self.legal_calls() and not _call_in(call, self.legal_calls()):
            raise ValueError(f"illegal call: {call}")
        seated = Call(call.type, call.bid, self.next_seat())
        self.calls.append(seated)

    # ----- views -----
    def history_tokens(self) -> List[str]:
        return [str(c) for c in self.calls]

    def for_partner_pair(self, seat: str) -> List[Call]:
        """Return all calls (seated) so an agent can analyse them."""
        return list(self.calls)


def _are_opponents(a: str, b: str) -> bool:
    return (SEAT_INDEX[a] - SEAT_INDEX[b]) % 2 == 1


def _call_in(call: Call, calls: List[Call]) -> bool:
    """Equality ignoring the ``seat`` field."""
    for c in calls:
        if c.type == call.type and c.bid == call.bid:
            return True
    return False
