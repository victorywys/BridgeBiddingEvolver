"""Optional LLM-backed agent.

The LLM does **not** acquire new bidding knowledge — it still resolves
its bid via the document-driven matcher. The LLM is exposed only as an
optional explanation / candidate-ranking hook so the architecture is
ready for plugging in a real model without breaking the
"externalized knowledge" guarantee.

When no callable is supplied the agent collapses to a :class:`SystemAgent`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..dsl import BiddingSystem
from ..env.auction import Auction
from ..env.deal import Deal
from .base import BidResult
from .system_agent import SystemAgent


LLMCallable = Callable[[str], str]


class LLMAgent:
    """Wraps a :class:`SystemAgent` and optionally asks an LLM for advisory
    natural-language explanations of the chosen bid. The LLM never changes
    the chosen bid — that's the deal: knowledge lives in documents only.
    """

    def __init__(self, system: BiddingSystem | str, llm: Optional[LLMCallable] = None,
                 name: Optional[str] = None) -> None:
        self._inner = SystemAgent(system, name=name)
        self._llm = llm
        self.name = self._inner.name + ("+LLM" if llm else "")

    @property
    def system(self) -> BiddingSystem:
        return self._inner.system

    def make_bid(self, deal: Deal, auction: Auction, seat: str) -> BidResult:
        result = self._inner.make_bid(deal, auction, seat)
        if self._llm is None:
            return result
        prompt = (
            "You are a bridge bidding co-pilot. The following bid was already "
            f"selected by the system: {result.token} ({result.rationale}). "
            "Write a one-line annotation explaining the meaning. "
            f"Auction so far: {' '.join(c for c in (str(x) for x in auction.calls))}."
        )
        try:
            annotation = self._llm(prompt).strip()
        except Exception:  # pragma: no cover - LLM is optional
            annotation = ""
        result.rationale = f"{result.rationale} | LLM: {annotation}" if annotation else result.rationale
        return result
