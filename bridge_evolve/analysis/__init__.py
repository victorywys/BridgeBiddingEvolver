"""Analysis utilities: information theory, statistics, and example
generation. These layers are *purely descriptive* — they never affect
how the matcher selects a bid.

Submodules
----------

* :mod:`information` — entropy / bits-per-call metrics for auctions and
  rule probabilities.
* :mod:`statistics`  — paired bootstrap CIs over IMP deltas.
* :mod:`examples`    — typical-hand generation for opening rules.
"""

from .information import (
    HandPool,
    RuleStats,
    AuctionInfo,
    rule_probability,
    auction_information,
)
from .statistics import BootstrapResult, paired_bootstrap
from .examples import TypicalHand, typical_hands_for_rule

__all__ = [
    "HandPool",
    "RuleStats",
    "AuctionInfo",
    "rule_probability",
    "auction_information",
    "BootstrapResult",
    "paired_bootstrap",
    "TypicalHand",
    "typical_hands_for_rule",
]
