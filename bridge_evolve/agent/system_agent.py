"""Document-driven bidding agent.

A :class:`SystemAgent` is stateless: every call to :meth:`make_bid`
re-derives the bid from the hand's symbolic features, the auction
history tokens and the loaded :class:`BiddingSystem`. Two instances
constructed from the same system path are guaranteed to behave
identically — this is what enables the transfer experiment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..dsl import BiddingSystem, load_system, select_action
from ..env.auction import Auction
from ..env.deal import Deal, hand_features
from .base import BidResult


class SystemAgent:
    name: str

    def __init__(self, system: BiddingSystem | str, name: Optional[str] = None) -> None:
        if isinstance(system, str):
            self._system = load_system(system)
        else:
            self._system = system
        self.name = name or f"SystemAgent[{self._system.name}@{self._system.version}]"

    @property
    def system(self) -> BiddingSystem:
        return self._system

    def make_bid(self, deal: Deal, auction: Auction, seat: str) -> BidResult:
        features = hand_features(deal.hand(seat))
        decision = select_action(features, auction, seat, self._system)
        return BidResult(
            seat=seat,
            token=decision.bid,
            rationale=decision.rationale,
            rule_name=decision.rule_name,
            rule_type=decision.rule_type,
            meaning=decision.meaning,
        )
