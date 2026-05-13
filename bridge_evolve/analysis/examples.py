"""Generate *typical hands* that satisfy a rule's requirements.

This is purely descriptive — the matcher never reads ``examples.yaml``.
The point is two-fold:

1. **Human comprehension.** A YAML rule like ``HCP: ">=12", Hearts: ">=5",
   Spades: "<=4"`` is correct but tells you very little about the *kind*
   of hand it actually classifies. Three sampled hands say it instantly.

2. **Proposer cueing.** When the LLM proposer reads the session report,
   each opening rule is followed by three concrete hands; this anchors
   the symbolic spec to a small "exemplar set" and makes pattern-level
   reasoning ("this rule should not fire on a 4-3-3-3 hand") much
   easier.

We pick examples by sampling from the hand pool and choosing three:

* the *minimum*-HCP match (the boundary case),
* the *median*-HCP match (the prototype),
* the *maximum*-HCP match (the upper boundary).

If the rule has fewer than three matches we report what we have.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..dsl.matcher import _check_requirements
from ..dsl.schema import Convention, OpeningRule
from ..env.deal import Hand, HandFeatures, SUITS, deal_random, hand_features
from ..env.deck import Suit


@dataclass(frozen=True)
class TypicalHand:
    pretty: str          # e.g. "S:AKxxx H:Qxx D:xx C:Kxx"
    hcp: int
    shape: str
    label: str           # "min" | "median" | "max"

    def as_dict(self) -> dict:
        return {
            "pretty": self.pretty,
            "hcp": self.hcp,
            "shape": self.shape,
            "label": self.label,
        }


def _hand_to_pretty(hand: Hand) -> str:
    bs = hand.by_suit()
    order = [Suit.SPADES, Suit.HEARTS, Suit.DIAMONDS, Suit.CLUBS]
    return " ".join(
        f"{s.symbol}:" + ("".join(c.rank for c in bs[s]) or "-")
        for s in order
    )


def _rule_requirements(rule):
    if isinstance(rule, (OpeningRule, Convention)):
        return rule.requirements
    raise TypeError(f"unknown rule type: {type(rule).__name__}")


def typical_hands_for_rule(rule, *, n: int = 3, pool_size: int = 2500,
                           seed: int = 4242) -> List[TypicalHand]:
    """Return up to ``n`` representative hands that satisfy ``rule``.

    Notes
    -----
    A fresh hand pool is sampled per call because we want the *full*
    Hand object (cards, not just features) so we can pretty-print suits.
    """
    req = _rule_requirements(rule)
    rng = random.Random(seed)
    matches: List[Tuple[HandFeatures, Hand]] = []
    for _ in range(pool_size):
        d = deal_random(seed=rng.randrange(1 << 31), dealer="N")
        h = d.hand("N")
        f = hand_features(h)
        if _check_requirements(f, req):
            matches.append((f, h))
    if not matches:
        return []
    matches.sort(key=lambda fh: fh[0].hcp)
    out: List[TypicalHand] = []
    if n == 1 or len(matches) == 1:
        idxs = [len(matches) // 2]
        labels = ["median"]
    elif n == 2:
        idxs = [0, len(matches) - 1]
        labels = ["min", "max"]
    else:
        # canonical 3: min / median / max
        idxs = [0, len(matches) // 2, len(matches) - 1]
        labels = ["min", "median", "max"]
        # if caller asked for >3, add evenly spaced extras
        if n > 3:
            extra = n - 3
            for k in range(extra):
                pos = int(round((k + 1) * len(matches) / (extra + 1)))
                pos = min(max(pos, 0), len(matches) - 1)
                if pos not in idxs:
                    idxs.append(pos)
                    labels.append(f"q{k+1}")
    seen = set()
    for idx, label in zip(idxs, labels):
        if idx in seen:
            continue
        seen.add(idx)
        f, h = matches[idx]
        out.append(TypicalHand(
            pretty=_hand_to_pretty(h),
            hcp=f.hcp,
            shape=f.shape,
            label=label,
        ))
    out.sort(key=lambda t: t.hcp)
    return out
