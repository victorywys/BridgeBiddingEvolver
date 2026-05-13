"""Contract scoring, par from DDS table, IMP differential.

The trick estimator is injected so DDS / heuristic / a deterministic mock
can all be plugged in without touching scoring logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .auction import Auction, Bid, BidType
from .deal import Deal
from .deck import Suit
from .trick_estimator import (
    TrickEstimator,
    get_default_estimator,
)


# ---------------------------------------------------------------------------
# Bridge scoring (non-vulnerable by default; vulnerability is plumbed through
# but defaults to False so the bidding-system research question stays
# uncluttered).
# ---------------------------------------------------------------------------


def _trick_value(strain: Suit) -> int:
    if strain in (Suit.HEARTS, Suit.SPADES, Suit.NT):
        return 30
    return 20


def _contract_score(level: int, strain: Suit, tricks: int, doubled: int = 0,
                    vulnerable: bool = False) -> int:
    """Standard duplicate bridge score for one declarer side."""
    needed = 6 + level
    if tricks >= needed:
        tv = _trick_value(strain)
        trick_pts = level * tv + (10 if strain == Suit.NT else 0)
        if doubled == 1:
            trick_pts *= 2
        elif doubled == 2:
            trick_pts *= 4
        ot = tricks - needed
        if doubled == 0:
            ot_pts = ot * tv
        elif doubled == 1:
            ot_pts = ot * (200 if vulnerable else 100)
        else:
            ot_pts = ot * (400 if vulnerable else 200)
        bonus = (500 if vulnerable else 300) if trick_pts >= 100 else 50
        if level == 6:
            bonus += 750 if vulnerable else 500
        elif level == 7:
            bonus += 1500 if vulnerable else 1000
        insult = {0: 0, 1: 50, 2: 100}[doubled]
        return trick_pts + ot_pts + bonus + insult
    under = needed - tricks
    if doubled == 0:
        per = 100 if vulnerable else 50
        return -per * under
    if not vulnerable:
        schedule = [100, 300, 500] + [300] * 50
    else:
        schedule = [200, 500] + [300] * 50
    pen = sum(schedule[:under])
    if doubled == 2:
        pen *= 2
    return -pen


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------


@dataclass
class ContractResult:
    contract: Optional[Bid]
    declarer: Optional[str]
    doubled: int
    tricks_taken: int
    tricks_needed: int
    made: bool
    overtricks: int
    undertricks: int
    score: int            # signed for declarer's pair
    par_score: int        # NS perspective
    notes: Dict[str, object]


def score_contract(deal: Deal,
                   contract: Optional[Bid],
                   declarer: Optional[str],
                   doubled: int = 0,
                   estimator: Optional[TrickEstimator] = None,
                   vulnerable: bool = False) -> ContractResult:
    est = estimator or get_default_estimator()
    par = par_score(deal, estimator=est)
    if contract is None or declarer is None:
        return ContractResult(None, None, 0, 0, 0, False, 0, 0,
                              score=0, par_score=par,
                              notes={"passed_out": True, "estimator": est.name})
    needed = 6 + contract.level
    taken = est.estimate(deal, declarer, contract.strain)
    made = taken >= needed
    raw = _contract_score(contract.level, contract.strain, taken,
                          doubled=doubled, vulnerable=vulnerable)
    # Sign relative to NS (so benchmarks treat NS as the protagonist).
    signed = raw if declarer in ("N", "S") else -raw
    return ContractResult(
        contract=contract,
        declarer=declarer,
        doubled=doubled,
        tricks_taken=taken,
        tricks_needed=needed,
        made=made,
        overtricks=max(0, taken - needed),
        undertricks=max(0, needed - taken),
        score=signed,
        par_score=par,
        notes={"passed_out": False, "estimator": est.name, "raw_score": raw},
    )


def evaluate_auction(deal: Deal, auction: Auction,
                     estimator: Optional[TrickEstimator] = None) -> ContractResult:
    last = auction.last_contract_bid()
    if last is None:
        return score_contract(deal, None, None, estimator=estimator)
    contract, last_bidder = last
    declarer = _find_declarer(auction, last_bidder, contract.strain)
    return score_contract(deal, contract, declarer,
                          doubled=auction.doubled_state(),
                          estimator=estimator)


def _find_declarer(auction: Auction, last_bidder: str, strain: Suit) -> str:
    side = {"N", "S"} if last_bidder in ("N", "S") else {"E", "W"}
    for call in auction.calls:
        if (call.type == BidType.BID and call.bid is not None
                and call.bid.strain == strain and call.seat in side):
            return call.seat
    return last_bidder


# ---------------------------------------------------------------------------
# Par from a DDS table (NS perspective)
# ---------------------------------------------------------------------------


def par_score(deal: Deal, estimator: Optional[TrickEstimator] = None,
              vulnerable_ns: bool = False, vulnerable_ew: bool = False) -> int:
    """Best-contract par score from NS perspective.

    For each (level, strain, side), take the side's best declarer-seat
    score; report the max-of-NS minus the max-of-EW. Doubled sacrifices
    are not searched — a simplification, but the relative ordering of
    bidding systems is preserved.
    """
    est = estimator or get_default_estimator()
    tbl = est.table(deal)
    best_ns = 0
    best_ew = 0
    for level in range(1, 8):
        for strain in (Suit.CLUBS, Suit.DIAMONDS, Suit.HEARTS, Suit.SPADES, Suit.NT):
            for seats, vul, target in (
                (("N", "S"), vulnerable_ns, "NS"),
                (("E", "W"), vulnerable_ew, "EW"),
            ):
                tricks = max(tbl[(s, strain.symbol)] for s in seats)
                raw = _contract_score(level, strain, tricks,
                                      doubled=0, vulnerable=vul)
                if target == "NS" and raw > best_ns:
                    best_ns = raw
                elif target == "EW" and raw > best_ew:
                    best_ew = raw
    return best_ns - best_ew


# ---------------------------------------------------------------------------
# IMP table
# ---------------------------------------------------------------------------

_IMP_TABLE = [
    (20, 0), (50, 1), (90, 2), (130, 3), (170, 4), (220, 5), (270, 6),
    (320, 7), (370, 8), (430, 9), (500, 10), (600, 11), (750, 12),
    (900, 13), (1100, 14), (1300, 15), (1500, 16), (1750, 17),
    (2000, 18), (2250, 19), (2500, 20), (3000, 21), (3500, 22),
    (4000, 23),
]


def score_difference_to_imps(diff: int) -> int:
    """Convert a raw point difference to IMPs (signed)."""
    sign = 1 if diff >= 0 else -1
    d = abs(diff)
    imps = 24
    for upper, value in _IMP_TABLE:
        if d < upper:
            imps = value
            break
    return sign * imps


def imps_vs_par(score: int, par: int) -> int:
    return score_difference_to_imps(score - par)
