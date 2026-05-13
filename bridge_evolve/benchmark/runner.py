"""High-level benchmark runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from ..agent.base import Agent, AuctionRecord, run_auction
from ..env.deal import Deal, deal_random
from .metrics import BenchmarkResult, benchmark_metrics, consistency_score


@dataclass
class BenchmarkRun:
    records: List[AuctionRecord]
    metrics: BenchmarkResult
    extra: Dict[str, object] = field(default_factory=dict)


def _make_deals(n: int, seed: int) -> List[Deal]:
    return [deal_random(seed=seed + i, dealer="N") for i in range(n)]


def run_benchmark(agent_factory: Callable[[], Agent],
                  partner_factory: Optional[Callable[[], Agent]] = None,
                  n_deals: int = 200, seed: int = 1234,
                  opponent_factory: Optional[Callable[[], Agent]] = None) -> BenchmarkRun:
    """Run ``n_deals`` auctions with the same NS pair against a fixed EW.

    Both NS seats share the same system (same factory). EW defaults to a
    passive pass-everything agent if no factory is provided, isolating
    the partnership signal.
    """
    if partner_factory is None:
        partner_factory = agent_factory
    if opponent_factory is None:
        opponent_factory = lambda: _PassAgent("OppPass")

    deals = _make_deals(n_deals, seed)
    records: List[AuctionRecord] = []
    for d in deals:
        agents: Dict[str, Agent] = {
            "N": agent_factory(),
            "S": partner_factory(),
            "E": opponent_factory(),
            "W": opponent_factory(),
        }
        rec = run_auction(d, agents)
        records.append(rec)
    metrics = benchmark_metrics(records)
    return BenchmarkRun(records=records, metrics=metrics)


def run_pair_comparison(a_factory: Callable[[], Agent],
                        b_factory: Callable[[], Agent],
                        n_deals: int = 100, seed: int = 4321) -> Dict[str, object]:
    """Compare two partnerships on the same set of deals."""
    a_run = run_benchmark(a_factory, n_deals=n_deals, seed=seed)
    b_run = run_benchmark(b_factory, n_deals=n_deals, seed=seed)
    return {
        "a": a_run.metrics.as_dict(),
        "b": b_run.metrics.as_dict(),
        "score_diff": a_run.metrics.avg_score - b_run.metrics.avg_score,
    }


# ---------------------------------------------------------------------------
# Tiny built-in opponent
# ---------------------------------------------------------------------------


class _PassAgent:
    def __init__(self, name: str) -> None:
        self.name = name

    def make_bid(self, deal, auction, seat):
        from .runner import _make_pass_result  # late import to avoid cycle
        return _make_pass_result(seat)


def _make_pass_result(seat: str):
    from ..agent.base import BidResult
    return BidResult(seat=seat, token="P", rationale="Passive opponent",
                     rule_name="opp_pass", rule_type="default", meaning={"Pass": True})
