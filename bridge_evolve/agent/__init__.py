"""Bidding agents.

The default :class:`SystemAgent` is fully document-driven: given a hand,
auction, and a path to a :class:`BiddingSystem`, it returns a bid using
only the matcher. There is no hidden state.

An optional :class:`LLMAgent` is provided as a hook for plugging in a
real model; by default it falls back to the deterministic agent so the
codebase runs without network access.
"""

from .base import Agent, BidResult, run_auction
from .system_agent import SystemAgent
from .llm_agent import LLMAgent

__all__ = ["Agent", "BidResult", "run_auction", "SystemAgent", "LLMAgent"]
