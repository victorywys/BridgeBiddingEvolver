"""Agent interface + auction-running driver."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from ..env.auction import Auction, BidType, Call, parse_bid
from ..env.deal import Deal, hand_features


@dataclass
class BidResult:
    seat: str
    token: str
    rationale: str
    rule_name: str
    rule_type: str
    meaning: Dict[str, object] = field(default_factory=dict)


class Agent(Protocol):
    """Protocol for any bidder."""
    name: str

    def make_bid(self, deal: Deal, auction: Auction, seat: str) -> BidResult:
        ...


@dataclass
class AuctionRecord:
    deal: Deal
    auction: Auction
    bids: List[BidResult]
    meanings: List[Dict[str, object]]
    agents: Dict[str, str]


def run_auction(deal: Deal, agents: Dict[str, Agent],
                max_calls: int = 60) -> AuctionRecord:
    """Run a complete auction. ``agents`` maps seat -> Agent.

    Returns an :class:`AuctionRecord` containing the deal, the final
    auction, and the per-bid bookkeeping needed by the reflection
    engine.
    """
    auction = Auction(dealer=deal.dealer)
    bids: List[BidResult] = []
    meanings: List[Dict[str, object]] = []
    while not auction.is_complete() and len(auction.calls) < max_calls:
        seat = auction.next_seat()
        agent = agents[seat]
        result = agent.make_bid(deal, auction, seat)
        call = parse_bid(result.token)
        try:
            auction.apply(call)
        except ValueError:
            # illegal bid - force pass and record the failure
            call = Call(BidType.PASS)
            auction.apply(call)
            result = BidResult(
                seat=seat, token="P",
                rationale=f"Illegal bid '{result.token}' rewritten to Pass",
                rule_name="env_correction", rule_type="default",
                meaning={"Illegal": result.token},
            )
        bids.append(result)
        meanings.append(result.meaning)
    return AuctionRecord(
        deal=deal,
        auction=auction,
        bids=bids,
        meanings=meanings,
        agents={s: a.name for s, a in agents.items()},
    )
