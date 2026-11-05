"""Legal bridge auction transitions and final contract detection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from bridge_evolve.core.types import Partnership, Seat, Strain


class CallKind(str, Enum):
    PASS = "P"
    DOUBLE = "X"
    REDOUBLE = "XX"
    BID = "BID"


@dataclass(frozen=True)
class Call:
    kind: CallKind
    level: int | None = None
    strain: Strain | None = None

    def __post_init__(self) -> None:
        if self.kind == CallKind.BID:
            if self.level is None or self.strain is None:
                raise ValueError("bid call requires both level and strain")
            if not 1 <= self.level <= 7:
                raise ValueError(f"bid level must be in 1..7, got {self.level}")
            return
        if self.level is not None or self.strain is not None:
            raise ValueError("pass/double/redouble cannot include level or strain")

    @classmethod
    def pass_call(cls) -> "Call":
        return cls(kind=CallKind.PASS)

    @classmethod
    def double(cls) -> "Call":
        return cls(kind=CallKind.DOUBLE)

    @classmethod
    def redouble(cls) -> "Call":
        return cls(kind=CallKind.REDOUBLE)

    @classmethod
    def bid(cls, level: int, strain: Strain) -> "Call":
        return cls(kind=CallKind.BID, level=level, strain=strain)

    def ordering_index(self) -> int:
        if self.kind != CallKind.BID or self.level is None or self.strain is None:
            raise ValueError("ordering_index is only available for bid calls")
        return (self.level - 1) * 5 + self.strain.rank

    def __str__(self) -> str:
        if self.kind == CallKind.BID:
            return f"{self.level}{self.strain.value}"
        return self.kind.value


ALL_BID_CALLS = tuple(
    Call.bid(level, strain)
    for level in range(1, 8)
    for strain in (Strain.CLUBS, Strain.DIAMONDS, Strain.HEARTS, Strain.SPADES, Strain.NOTRUMP)
)


@dataclass(frozen=True)
class BidRecord:
    seat: Seat
    call: Call


@dataclass(frozen=True)
class AuctionState:
    dealer: Seat
    next_seat: Seat
    calls: tuple[Call, ...] = ()
    last_bid: BidRecord | None = None
    doubling: int = 1
    consecutive_passes: int = 0
    any_non_pass: bool = False
    ended: bool = False


@dataclass(frozen=True)
class Contract:
    level: int
    strain: Strain
    declarer: Seat
    doubling: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.level <= 7:
            raise ValueError(f"contract level must be in 1..7, got {self.level}")
        if self.doubling not in (1, 2, 4):
            raise ValueError(f"contract doubling must be 1, 2, or 4, got {self.doubling}")


@dataclass(frozen=True)
class PassedOut:
    pass


PASS = Call.pass_call()
DOUBLE = Call.double()
REDOUBLE = Call.redouble()


def initial_auction(dealer: Seat) -> AuctionState:
    return AuctionState(dealer=dealer, next_seat=dealer)


def legal_calls(auction: AuctionState) -> tuple[Call, ...]:
    if auction.ended:
        return ()

    actions: list[Call] = [PASS]
    seat = auction.next_seat

    if auction.last_bid is not None and seat.partnership != auction.last_bid.seat.partnership:
        if auction.doubling == 1:
            actions.append(DOUBLE)

    if auction.last_bid is not None and seat.partnership == auction.last_bid.seat.partnership:
        if auction.doubling == 2:
            actions.append(REDOUBLE)

    min_index = -1
    if auction.last_bid is not None:
        min_index = auction.last_bid.call.ordering_index()

    for bid in ALL_BID_CALLS:
        if bid.ordering_index() > min_index:
            actions.append(bid)

    return tuple(actions)


def apply_call(auction: AuctionState, call: Call) -> AuctionState:
    if auction.ended:
        raise ValueError("auction has already ended")
    if call not in legal_calls(auction):
        raise ValueError(f"illegal call {call} for current auction state")

    current_seat = auction.next_seat
    next_seat = current_seat.next()
    calls = auction.calls + (call,)

    if call.kind == CallKind.PASS:
        passes = auction.consecutive_passes + 1
        ended = (not auction.any_non_pass and passes == 4) or (auction.any_non_pass and passes == 3)
        return AuctionState(
            dealer=auction.dealer,
            next_seat=next_seat if not ended else current_seat,
            calls=calls,
            last_bid=auction.last_bid,
            doubling=auction.doubling,
            consecutive_passes=passes,
            any_non_pass=auction.any_non_pass,
            ended=ended,
        )

    if call.kind == CallKind.BID:
        return AuctionState(
            dealer=auction.dealer,
            next_seat=next_seat,
            calls=calls,
            last_bid=BidRecord(seat=current_seat, call=call),
            doubling=1,
            consecutive_passes=0,
            any_non_pass=True,
            ended=False,
        )

    new_doubling = 2 if call.kind == CallKind.DOUBLE else 4
    return AuctionState(
        dealer=auction.dealer,
        next_seat=next_seat,
        calls=calls,
        last_bid=auction.last_bid,
        doubling=new_doubling,
        consecutive_passes=0,
        any_non_pass=True,
        ended=False,
    )


def final_contract(auction: AuctionState) -> Contract | PassedOut:
    if not auction.ended:
        raise ValueError("final contract is undefined before the auction ends")
    if auction.last_bid is None:
        return PassedOut()

    winning_side = auction.last_bid.seat.partnership
    winning_strain = auction.last_bid.call.strain

    declarer = _find_declarer(
        dealer=auction.dealer,
        calls=auction.calls,
        side=winning_side,
        strain=winning_strain,
    )
    if declarer is None:
        raise RuntimeError("internal error: no declarer found for final contract")

    return Contract(
        level=auction.last_bid.call.level or 1,
        strain=winning_strain or Strain.CLUBS,
        declarer=declarer,
        doubling=auction.doubling,
    )


def _find_declarer(
    dealer: Seat,
    calls: tuple[Call, ...],
    side: Partnership,
    strain: Strain | None,
) -> Seat | None:
    seat = dealer
    for call in calls:
        if call.kind == CallKind.BID and call.strain == strain and seat.partnership == side:
            return seat
        seat = seat.next()
    return None

