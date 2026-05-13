"""Head-to-head IMP match between two bidding systems.

Each deal is played twice (`team-of-four` simulation):

* Open Room: ``system_a`` sits N-S, ``system_b`` sits E-W.
* Closed Room: ``system_b`` sits N-S, ``system_a`` sits E-W
  (same deal, rotated, so neither side gets a card advantage).

The IMP differential for ``system_a`` is

    imps = imp(score_a_NS_open - score_a_NS_closed_as_EW)

i.e. how many points each pair earned when bidding the same cards. This
is the standard duplicate teams comparison and is the right fitness
signal for "is system A better than system B at expressing this hand?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from ..agent.base import Agent, AuctionRecord, run_auction
from ..env.deal import Deal, deal_random
from ..env.scoring import (
    evaluate_auction,
    par_score,
    score_difference_to_imps,
)
from ..env.trick_estimator import TrickEstimator, get_default_estimator


@dataclass
class BoardResult:
    deal_seed: int
    open_score: int          # NS perspective, open room (A is NS)
    closed_score: int        # NS perspective, closed room (B is NS)
    imps_for_a: int          # signed, positive = A wins the board
    par_ns: int
    open_contract: Optional[str]
    closed_contract: Optional[str]
    open_auction: List[str]
    closed_auction: List[str]
    open_meanings: List[Dict]
    closed_meanings: List[Dict]
    # Per-call metadata sufficient for downstream analysis (information
    # theory, replay, rule attribution). Each entry is a dict with keys
    # ``seat``, ``token``, ``rule_type``, ``rule_name``.
    open_bids: List[Dict] = field(default_factory=list)
    closed_bids: List[Dict] = field(default_factory=list)


@dataclass
class MatchResult:
    system_a: str
    system_b: str
    n_boards: int
    total_imps_for_a: int
    avg_imps_for_a: float
    avg_imps_vs_par_a: float
    avg_imps_vs_par_b: float
    boards: List[BoardResult] = field(default_factory=list)

    def summary(self) -> Dict[str, object]:
        return {
            "system_a": self.system_a,
            "system_b": self.system_b,
            "n_boards": self.n_boards,
            "total_imps_for_a": self.total_imps_for_a,
            "avg_imps_for_a": round(self.avg_imps_for_a, 3),
            "avg_imps_vs_par_a": round(self.avg_imps_vs_par_a, 3),
            "avg_imps_vs_par_b": round(self.avg_imps_vs_par_b, 3),
        }


def run_imp_match(a_factory: Callable[[], Agent],
                  b_factory: Callable[[], Agent],
                  n_boards: int = 200,
                  seed: int = 1234,
                  estimator: Optional[TrickEstimator] = None,
                  deals: Optional[Sequence[Deal]] = None) -> MatchResult:
    """Run a teams-of-four IMP match. Returns the per-board record."""
    est = estimator or get_default_estimator()
    if deals is None:
        deals = [deal_random(seed=seed + i, dealer="N") for i in range(n_boards)]
    boards: List[BoardResult] = []
    total = 0
    imps_par_a = 0
    imps_par_b = 0
    for d in deals:
        # Open room: A is NS, B is EW
        agents_open: Dict[str, Agent] = {
            "N": a_factory(), "S": a_factory(),
            "E": b_factory(), "W": b_factory(),
        }
        open_rec = run_auction(d, agents_open)
        open_res = evaluate_auction(d, open_rec.auction, estimator=est)
        # Closed room: B is NS, A is EW (same deal, no rotation; NS-perspective
        # scores are simply assigned to B in closed room).
        agents_closed: Dict[str, Agent] = {
            "N": b_factory(), "S": b_factory(),
            "E": a_factory(), "W": a_factory(),
        }
        closed_rec = run_auction(d, agents_closed)
        closed_res = evaluate_auction(d, closed_rec.auction, estimator=est)
        # IMPs for A: A's NS score in open room minus B's NS score in closed.
        diff = open_res.score - closed_res.score
        imps = score_difference_to_imps(diff)
        par = par_score(d, estimator=est)
        total += imps
        imps_par_a += score_difference_to_imps(open_res.score - par)
        imps_par_b += score_difference_to_imps(closed_res.score - par)
        boards.append(BoardResult(
            deal_seed=d.seed,
            open_score=open_res.score,
            closed_score=closed_res.score,
            imps_for_a=imps,
            par_ns=par,
            open_contract=str(open_res.contract) if open_res.contract else None,
            closed_contract=str(closed_res.contract) if closed_res.contract else None,
            open_auction=[str(c) for c in open_rec.auction.calls],
            closed_auction=[str(c) for c in closed_rec.auction.calls],
            open_meanings=[b.meaning for b in open_rec.bids],
            closed_meanings=[b.meaning for b in closed_rec.bids],
            open_bids=[{"seat": b.seat, "token": b.token,
                        "rule_type": b.rule_type, "rule_name": b.rule_name}
                       for b in open_rec.bids],
            closed_bids=[{"seat": b.seat, "token": b.token,
                          "rule_type": b.rule_type, "rule_name": b.rule_name}
                         for b in closed_rec.bids],
        ))
    n = max(1, len(boards))
    return MatchResult(
        system_a=getattr(a_factory(), "name", "A"),
        system_b=getattr(b_factory(), "name", "B"),
        n_boards=len(boards),
        total_imps_for_a=total,
        avg_imps_for_a=total / n,
        avg_imps_vs_par_a=imps_par_a / n,
        avg_imps_vs_par_b=imps_par_b / n,
        boards=boards,
    )


def imps_vs_par_single(a_factory: Callable[[], Agent],
                       n_boards: int = 200, seed: int = 1234,
                       estimator: Optional[TrickEstimator] = None,
                       opponent: Optional[Callable[[], Agent]] = None) -> float:
    """Average IMPs vs. par for a single system (against a passive opponent
    unless one is supplied). Useful as a quick fitness signal."""
    est = estimator or get_default_estimator()
    from ..benchmark.runner import _PassAgent
    if opponent is None:
        opponent = lambda: _PassAgent("OppPass")
    deals = [deal_random(seed=seed + i, dealer="N") for i in range(n_boards)]
    total = 0
    for d in deals:
        agents = {
            "N": a_factory(), "S": a_factory(),
            "E": opponent(), "W": opponent(),
        }
        rec = run_auction(d, agents)
        res = evaluate_auction(d, rec.auction, estimator=est)
        par = par_score(d, estimator=est)
        total += score_difference_to_imps(res.score - par)
    return total / max(1, n_boards)
