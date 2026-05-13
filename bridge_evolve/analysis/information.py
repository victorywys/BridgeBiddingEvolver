"""Information-theoretic analysis of bidding systems.

The fundamental quantity is a *rule's marginal probability*:

    P(R) := Pr[a random 13-card hand satisfies R's Requirements]

Estimated by Monte-Carlo sampling from the same uniform-deal RNG the
evaluator uses. From that single number two useful metrics follow.

* :func:`rule_probability` returns ``P(R)`` and the *self-information*
  ``-log2 P(R)`` — the bits a listener gains, in expectation, on hearing
  the speaker's hand satisfies ``R`` (and nothing else).

* :func:`auction_information` decomposes an auction into per-bid
  information contributions. For each positive call we credit the
  *conditional* bits gained: ``-log2 P(bidder's hand still consistent |
  prior constraints on bidder)``. Pass / X / XX contribute 0 bits at this
  layer (they convey strategic information that this metric ignores by
  design — we want a clean, system-agnostic lower bound).

Two design points worth understanding:

1. **No deal observation.** The metric is intrinsic to the system, not to
   the run: it asks "how much can a listener learn from the system's
   bidding vocabulary?", not "how much did this auction reveal about
   *this* hand?". This matches the user's request to compare
   bidding-system quality independently of luck-of-the-draw.

2. **Lower bound, not exact bits.** We treat each rule as a hard
   constraint; we do not model the negative inference "the speaker did
   not take any higher-priority alternative call". The reported bits
   therefore *underestimate* true information. This is fine — a system
   that dominates on a lower bound dominates on the true measure.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..dsl.matcher import _check_requirements
from ..dsl.schema import BiddingSystem, Convention, OpeningRule, Requirements
from ..env.deal import HandFeatures, deal_random, hand_features


# ---------------------------------------------------------------------------
# Hand pool
# ---------------------------------------------------------------------------


@dataclass
class HandPool:
    """A frozen sample of random hands used to estimate rule probabilities.

    The pool fixes the Monte-Carlo noise of all downstream metrics:
    invoking ``rule_probability`` twice with the same pool produces the
    same answer to many decimal places, which keeps the session evaluator
    deterministic.
    """
    features: Tuple[HandFeatures, ...]
    seed: int
    size: int

    @classmethod
    def build(cls, size: int = 5000, seed: int = 13_576) -> "HandPool":
        rng = random.Random(seed)
        feats: List[HandFeatures] = []
        for _ in range(size):
            d = deal_random(seed=rng.randrange(1 << 31), dealer="N")
            # Just take North; the deck is shuffled uniformly so any seat
            # gives an i.i.d. sample of 13-card hands.
            feats.append(hand_features(d.hand("N")))
        return cls(features=tuple(feats), seed=seed, size=size)


# ---------------------------------------------------------------------------
# Rule probability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleStats:
    name: str
    bid: str
    probability: float
    bits: float                 # -log2(probability), float('inf') if P=0
    matches: int
    pool_size: int

    def as_dict(self) -> dict:
        b = self.bits
        if not math.isfinite(b):
            b_out = None
        else:
            b_out = round(b, 3)
        return {
            "name": self.name,
            "bid": self.bid,
            "probability": round(self.probability, 5),
            "bits": b_out,
            "matches": self.matches,
            "pool_size": self.pool_size,
        }


def _rule_requirements(rule) -> Tuple[str, Requirements]:
    """Extract (bid_token, Requirements) from an OpeningRule or Convention."""
    if isinstance(rule, OpeningRule):
        return (rule.action or "", rule.requirements)
    if isinstance(rule, Convention):
        return (rule.action or "", rule.requirements)
    raise TypeError(f"unknown rule type: {type(rule).__name__}")


def rule_probability(rule, pool: HandPool) -> RuleStats:
    """Estimate ``P(hand satisfies rule.requirements)`` from ``pool``."""
    bid, req = _rule_requirements(rule)
    matches = sum(1 for f in pool.features if _check_requirements(f, req))
    p = matches / pool.size if pool.size else 0.0
    bits = (-math.log2(p)) if p > 0 else float("inf")
    return RuleStats(
        name=rule.name,
        bid=bid,
        probability=p,
        bits=bits,
        matches=matches,
        pool_size=pool.size,
    )


def system_rule_stats(system: BiddingSystem, pool: HandPool) -> List[RuleStats]:
    """Compute rule probabilities for all openings + conventions."""
    out: List[RuleStats] = []
    for r in system.opening_table:
        out.append(rule_probability(r, pool))
    for c in system.conventions:
        out.append(rule_probability(c, pool))
    return out


# ---------------------------------------------------------------------------
# Auction information
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    seat: str
    token: str
    rule_type: str          # "opening" | "convention" | "default" | "pass"
    rule_name: str
    bits: float

    def as_dict(self) -> dict:
        b = self.bits
        if not math.isfinite(b):
            b_out = None
        else:
            b_out = round(b, 3)
        return {
            "seat": self.seat, "token": self.token,
            "rule_type": self.rule_type, "rule_name": self.rule_name,
            "bits": b_out,
        }


@dataclass(frozen=True)
class AuctionInfo:
    total_bits: float
    positive_calls: int
    bits_per_call: float
    steps: Tuple[_Step, ...]

    def as_dict(self) -> dict:
        return {
            "total_bits": round(self.total_bits, 2),
            "positive_calls": self.positive_calls,
            "bits_per_call": round(self.bits_per_call, 2),
            "steps": [s.as_dict() for s in self.steps],
        }


def auction_information(steps: Sequence,
                        system: BiddingSystem,
                        pool: HandPool) -> AuctionInfo:
    """Score an auction's information content using the *system*'s vocabulary.

    Parameters
    ----------
    steps
        Sequence of ``BidRecord``-like objects with attributes
        ``seat``, ``token``, ``rule_type``, ``rule_name``. (Anything
        emitted by :func:`bridge_evolve.agent.run_auction`.)
    system
        The bidding system whose rules supply the requirements.
    pool
        Pre-built hand pool used to estimate marginal probabilities.

    Returns
    -------
    AuctionInfo
        Total information, count of positive calls, and a per-step
        breakdown. Calls that the matcher resolved via the default
        branch (``rule_type='default'`` or ``rule_name='forced_no_rule'``)
        contribute 0 bits — they convey no system-defined meaning.
    """
    rule_idx: Dict[str, object] = {}
    for r in system.opening_table:
        rule_idx[r.name] = r
    for c in system.conventions:
        rule_idx[c.name] = c

    # Per-seat conditional bits: each seat accumulates the constraints
    # implied by ITS OWN previous calls. We sample the pool once and
    # filter incrementally.
    per_seat_pool: Dict[str, List[HandFeatures]] = {
        s: list(pool.features) for s in ("N", "E", "S", "W")
    }
    total_pool = pool.size
    total_bits = 0.0
    positive = 0
    out_steps: List[_Step] = []

    for step in steps:
        if isinstance(step, dict):
            seat = step.get("seat", "?")
            token = step.get("token", "")
            rt = step.get("rule_type", "")
            rn = step.get("rule_name", "")
        else:
            seat = step.seat
            token = step.token
            rt = step.rule_type
            rn = step.rule_name
        if token in ("P", "PASS", "X", "XX"):
            out_steps.append(_Step(seat, token, "pass", rn, 0.0))
            continue
        rule = rule_idx.get(rn)
        if rule is None or rt in ("default",):
            out_steps.append(_Step(seat, token, rt, rn, 0.0))
            positive += 1
            continue
        _bid, req = _rule_requirements(rule)
        before = per_seat_pool[seat]
        after = [f for f in before if _check_requirements(f, req)]
        if not before or not after:
            bits = 0.0
        else:
            # Bits gained = -log2(P(after | before))
            p = len(after) / len(before)
            bits = -math.log2(p) if p > 0 else 0.0
        per_seat_pool[seat] = after
        total_bits += bits
        positive += 1
        out_steps.append(_Step(seat, token, rt, rn, bits))

    bpc = total_bits / positive if positive else 0.0
    return AuctionInfo(total_bits=total_bits, positive_calls=positive,
                       bits_per_call=bpc, steps=tuple(out_steps))
