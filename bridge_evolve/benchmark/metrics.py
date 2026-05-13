"""Benchmark metric definitions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from ..agent.base import AuctionRecord
from ..env.scoring import evaluate_auction
from ..reflection.analyzer import analyze_auction


@dataclass
class BenchmarkResult:
    n_deals: int
    avg_score: float
    contract_make_rate: float
    passout_rate: float
    game_reach_rate: float
    avg_undertricks: float
    ambiguity_rate: float
    default_rule_rate: float
    diagnosis_counts: Dict[str, int] = field(default_factory=dict)
    consistency: float = 1.0  # 1 = perfectly reproducible

    def as_dict(self) -> Dict[str, object]:
        return {
            "n_deals": self.n_deals,
            "avg_score": round(self.avg_score, 2),
            "contract_make_rate": round(self.contract_make_rate, 3),
            "passout_rate": round(self.passout_rate, 3),
            "game_reach_rate": round(self.game_reach_rate, 3),
            "avg_undertricks": round(self.avg_undertricks, 3),
            "ambiguity_rate": round(self.ambiguity_rate, 3),
            "default_rule_rate": round(self.default_rule_rate, 3),
            "diagnosis_counts": dict(self.diagnosis_counts),
            "consistency": round(self.consistency, 3),
        }


def benchmark_metrics(records: Sequence[AuctionRecord]) -> BenchmarkResult:
    n = len(records)
    if n == 0:
        return BenchmarkResult(0, 0, 0, 0, 0, 0, 0, 0, {}, 1.0)
    scores: List[int] = []
    makes = 0
    passouts = 0
    games = 0
    undertricks = 0
    ambiguous = 0
    defaults = 0
    bid_total = 0
    diag_counter: Counter = Counter()
    for rec in records:
        res = evaluate_auction(rec.deal, rec.auction)
        scores.append(res.score)
        if res.notes.get("passed_out"):
            passouts += 1
        else:
            if res.made:
                makes += 1
            else:
                undertricks += res.undertricks
            if res.contract is not None and res.contract.level >= 3:
                games += 1
        for b in rec.bids:
            bid_total += 1
            if not b.meaning and b.token != "P":
                ambiguous += 1
            if b.rule_type == "default" and b.token != "P":
                defaults += 1
        for d in analyze_auction(rec):
            diag_counter[d.kind.value] += 1
    return BenchmarkResult(
        n_deals=n,
        avg_score=sum(scores) / n,
        contract_make_rate=makes / max(1, n - passouts),
        passout_rate=passouts / n,
        game_reach_rate=games / n,
        avg_undertricks=undertricks / n,
        ambiguity_rate=ambiguous / max(1, bid_total),
        default_rule_rate=defaults / max(1, bid_total),
        diagnosis_counts=dict(diag_counter),
    )


def consistency_score(records_a: Sequence[AuctionRecord],
                      records_b: Sequence[AuctionRecord]) -> float:
    """Fraction of deals where both agent batches produced identical auctions.

    Used to verify that two independent agent instances loaded from the
    same system documents really do behave identically (the transfer
    guarantee).
    """
    n = min(len(records_a), len(records_b))
    if n == 0:
        return 1.0
    matches = 0
    for a, b in zip(records_a, records_b):
        if [c for c in a.auction.calls] == [c for c in b.auction.calls]:
            matches += 1
    return matches / n
