"""Convention DSL: schema, parser, matcher, validators."""

from .schema import (
    Convention,
    Trigger,
    Requirements,
    Meaning,
    Followup,
    BiddingSystem,
    OpeningRule,
    Principle,
)
from .parser import load_system, dump_convention, dump_system, save_system
from .matcher import (
    AuctionContext,
    select_action,
    BidDecision,
    interpret_partner_bid,
)
from .validators import (
    ValidationIssue,
    validate_system,
    has_errors,
    format_issues,
)

__all__ = [
    "Convention",
    "Trigger",
    "Requirements",
    "Meaning",
    "Followup",
    "BiddingSystem",
    "OpeningRule",
    "Principle",
    "load_system",
    "dump_convention",
    "dump_system",
    "save_system",
    "AuctionContext",
    "select_action",
    "BidDecision",
    "interpret_partner_bid",
    "ValidationIssue",
    "validate_system",
    "has_errors",
    "format_issues",
]
