"""Convention matching: turns (hand, history, system) into a bid + meaning.

The matcher is the *interpreter* of the bidding DSL. It is deterministic
and depends only on:

* the hand's symbolic features
* the auction token history
* the loaded :class:`BiddingSystem`

This is what guarantees that an agent's behaviour is fully reproducible
from the document tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from ..env.auction import Auction, Bid, BidType, Call, parse_bid
from ..env.deal import HandFeatures
from ..env.deck import Suit
from .schema import (
    BiddingSystem,
    Convention,
    Followup,
    Meaning,
    OpeningRule,
    Requirements,
    Trigger,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class AuctionContext:
    """Symbolic view of the auction state from a single seat's perspective."""
    seat: str
    dealer: str
    history_tokens: List[str]
    own_tokens: List[str]
    partner_tokens: List[str]
    role: str  # "Opener" / "Responder" / "Intervenor" / "Advancer" / "None"

    @property
    def is_opening(self) -> bool:
        return all(t == "P" for t in self.history_tokens)

    @property
    def partner_has_bid(self) -> bool:
        return any(t not in ("P", "X", "XX") for t in self.partner_tokens)


@dataclass
class BidDecision:
    bid: str
    rationale: str
    rule_name: str
    rule_type: str           # "opening" | "convention" | "followup" | "default"
    meaning: Dict[str, object] = field(default_factory=dict)
    fallback: bool = False
    candidates: List[str] = field(default_factory=list)


def select_action(features: HandFeatures, auction: Auction, seat: str,
                  system: BiddingSystem) -> BidDecision:
    ctx = _context(auction, seat)
    legal = {str(c) for c in auction.legal_calls()}

    # 1) opening?
    if ctx.is_opening:
        decision = _try_opening(features, system, legal)
        if decision is not None:
            return decision
        return BidDecision(
            bid="P", rationale="No opening rule matched", rule_name="default_pass",
            rule_type="default", meaning={"NoOpening": True},
        )

    # 2) followup to one of our own previous conventions?
    fu = _try_followup(features, ctx, system, legal)
    if fu is not None:
        return fu

    # 3) a fresh convention triggered by current auction pattern?
    conv = _try_convention(features, ctx, system, legal)
    if conv is not None:
        return conv

    # 4) default rules (raise with fit, NT with balanced, otherwise pass).
    return _default_action(features, ctx, system, legal)


def interpret_partner_bid(partner_token: str, prior_history: List[str],
                          system: BiddingSystem) -> Dict[str, object]:
    """Re-derive the *meaning* a partner's bid carries under the system.

    This is the symbolic communication channel: both players read the
    same documents, so the listener can recover the speaker's meaning.
    """
    full_history = prior_history + [partner_token]
    # Match against every convention with an Action == partner_token.
    for conv in sorted(system.conventions, key=lambda c: -c.priority):
        if conv.action and conv.action.upper() == partner_token.upper():
            if _pattern_matches(conv.trigger.auction_pattern, prior_history):
                return {
                    "Convention": conv.name,
                    "Meaning": dict(conv.meaning.fields),
                    "Description": conv.description,
                }
        # followup interpretations
        for fu in conv.followups:
            combined_pattern = list(conv.trigger.auction_pattern) + [conv.action or "*"] + list(fu.pattern[:-1])
            if (fu.action and fu.action.upper() == partner_token.upper()
                    and _pattern_matches(combined_pattern, prior_history)):
                return {
                    "Convention": f"{conv.name}/followup",
                    "Meaning": dict(fu.meaning.fields),
                    "Description": conv.description,
                }
        # followup interpretations where Pattern matches the partner token directly
        for fu in conv.followups:
            if not fu.action and fu.pattern and fu.pattern[-1].upper() == partner_token.upper():
                pre = list(conv.trigger.auction_pattern) + [conv.action or "*"]
                if _pattern_matches(pre + list(fu.pattern[:-1]), prior_history):
                    return {
                        "Convention": f"{conv.name}/followup",
                        "Meaning": dict(fu.meaning.fields),
                        "Description": conv.description,
                    }
    # opening table
    for rule in system.opening_table:
        if (rule.action and rule.action.upper() == partner_token.upper()
                and all(t == "P" for t in prior_history)):
            return {
                "Convention": rule.name,
                "Meaning": _opening_meaning(rule),
                "Description": rule.description,
            }
    return {"Convention": None, "Meaning": {"Natural": True}}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _context(auction: Auction, seat: str) -> AuctionContext:
    partner = {"N": "S", "S": "N", "E": "W", "W": "E"}[seat]
    history = [str(c) for c in auction.calls]
    own = [str(c) for c in auction.calls if c.seat == seat]
    par = [str(c) for c in auction.calls if c.seat == partner]
    role = _infer_role(auction, seat)
    return AuctionContext(seat=seat, dealer=auction.dealer, history_tokens=history,
                          own_tokens=own, partner_tokens=par, role=role)


def _infer_role(auction: Auction, seat: str) -> str:
    partner = {"N": "S", "S": "N", "E": "W", "W": "E"}[seat]
    first_bidder = None
    for c in auction.calls:
        if c.type == BidType.BID:
            first_bidder = c.seat
            break
    if first_bidder is None:
        return "Opener" if seat == auction.dealer else "None"
    if first_bidder == seat:
        return "Opener"
    if first_bidder == partner:
        return "Responder"
    return "Intervenor" if seat != partner else "Advancer"


def _try_opening(features: HandFeatures, system: BiddingSystem,
                 legal: set) -> Optional[BidDecision]:
    for rule in system.opening_table:
        if _check_requirements(features, rule.requirements):
            if rule.action and rule.action.upper() in legal:
                return BidDecision(
                    bid=rule.action.upper(),
                    rationale=f"Opening rule '{rule.name}' matched: {rule.description}",
                    rule_name=rule.name,
                    rule_type="opening",
                    meaning=_opening_meaning(rule),
                )
    return None


def _opening_meaning(rule: OpeningRule) -> Dict[str, object]:
    return {"Opening": rule.action, **{k: v for k, v in rule.requirements.constraints.items()}}


def _try_convention(features: HandFeatures, ctx: AuctionContext,
                    system: BiddingSystem, legal: set) -> Optional[BidDecision]:
    candidates: List[Tuple[Convention, str]] = []
    for conv in system.conventions:
        if conv.trigger.seat and conv.trigger.seat != ctx.role and conv.trigger.seat != "Any":
            continue
        if not _pattern_matches(conv.trigger.auction_pattern, ctx.history_tokens):
            continue
        if not _check_requirements(features, conv.requirements):
            continue
        if not conv.action:
            continue
        if conv.action.upper() not in legal:
            continue
        candidates.append((conv, conv.action.upper()))

    if not candidates:
        return None
    conv, bid = candidates[0]  # already sorted by priority
    return BidDecision(
        bid=bid,
        rationale=f"Convention '{conv.name}' triggered",
        rule_name=conv.name,
        rule_type="convention",
        meaning=dict(conv.meaning.fields),
        candidates=[c[0].name for c in candidates],
    )


def _try_followup(features: HandFeatures, ctx: AuctionContext,
                  system: BiddingSystem, legal: set) -> Optional[BidDecision]:
    # We are in a followup if our own previous bid was a convention call.
    if not ctx.own_tokens:
        return None
    own_last = ctx.own_tokens[-1]
    for conv in system.conventions:
        if not conv.action or conv.action.upper() != own_last.upper():
            continue
        if not conv.followups:
            continue
        # build the position after our own bid
        # find the convention's "anchor" position in history
        idx = _find_last_occurrence(ctx.history_tokens, conv.action.upper())
        if idx is None:
            continue
        # tokens between own bid and current turn (i.e. partner's reply)
        between = ctx.history_tokens[idx + 1:]
        for fu in conv.followups:
            if not fu.pattern:
                continue
            if _pattern_matches(fu.pattern, between):
                if not _check_requirements(features, fu.requirements):
                    continue
                if fu.action and fu.action.upper() in legal:
                    return BidDecision(
                        bid=fu.action.upper(),
                        rationale=f"Followup of {conv.name} after {' '.join(fu.pattern)}",
                        rule_name=f"{conv.name}/followup",
                        rule_type="followup",
                        meaning=dict(fu.meaning.fields),
                    )
    return None


def _default_action(features: HandFeatures, ctx: AuctionContext,
                    system: BiddingSystem, legal: set) -> BidDecision:
    """Meaning-driven fallback.

    This branch carries *no* implicit knowledge of bridge ("natural longest
    suit", "raise with a fit", etc.) so that the evolved system is forced
    to encode every behaviour in explicit rules. The only thing we do here
    is honour an explicit forcing obligation from partner's last bid:

    * If partner's most recent positive call carries ``Forcing >= 1`` in
      its documented meaning, we must keep the auction alive — bid the
      cheapest legal positive call. This shows up as a ``NO_RULE_FIRED``
      diagnosis so the reflection layer can fix it with a real rule.

    * Otherwise: Pass.
    """
    partner_last = _partner_last_positive(ctx)
    if partner_last is not None:
        prior = ctx.history_tokens[: ctx.history_tokens.index(partner_last)] \
            if partner_last in ctx.history_tokens else []
        meaning = interpret_partner_bid(partner_last, prior, system).get("Meaning", {})
        try:
            forcing = int(meaning.get("Forcing", 0) or 0)
        except (TypeError, ValueError):
            forcing = 0
        if forcing >= 1:
            for tok in sorted(legal, key=_bid_rank):
                if tok in ("P", "X", "XX"):
                    continue
                return BidDecision(
                    bid=tok,
                    rationale="Forced by partner; no rule matched -- cheapest legal call.",
                    rule_name="forced_no_rule",
                    rule_type="default",
                    meaning={"Forced": True, "Reason": "missing_rule"},
                    fallback=True,
                )

    return BidDecision(
        bid="P",
        rationale="No rule matched; pass.",
        rule_name="default_pass",
        rule_type="default",
        meaning={},
        fallback=True,
    )


def _partner_last_positive(ctx: AuctionContext) -> Optional[str]:
    for tok in reversed(ctx.partner_tokens):
        if tok not in ("P", "X", "XX"):
            return tok
    return None


def _bid_rank(token: str) -> int:
    if token == "P":
        return 0
    if token in ("X", "XX"):
        return 1
    try:
        call = parse_bid(token)
        if call.type == BidType.BID and call.bid is not None:
            return 100 + call.bid.rank()
    except Exception:
        pass
    return 9999


# ---------------------------------------------------------------------------
# Pattern + constraint helpers
# ---------------------------------------------------------------------------


def _pattern_matches(pattern: Iterable[str], history: List[str]) -> bool:
    """Match a pattern (with ``*`` wildcards) anchored to the *end* of history.

    Examples:
        pattern ["1NT"]              matches history ["1NT"] or ["P","1NT"]
        pattern ["1NT","*","2C"]     matches ["1NT","P","2C"], ["1NT","X","2C"]
        pattern ["*","1NT"]          matches anything ending in 1NT
    """
    pat = [p.upper() for p in pattern]
    hist = [h.upper() for h in history]
    if not pat:
        return True
    if len(pat) > len(hist):
        return False
    # anchor to end
    tail = hist[-len(pat):]
    for p, h in zip(pat, tail):
        if p == "*":
            continue
        if p == h:
            continue
        return False
    return True


def _check_requirements(features: HandFeatures, req: Requirements) -> bool:
    for key, raw in req.constraints.items():
        if raw == "any" or raw is None:
            continue
        if not _check_single(features, key, raw):
            return False
    return True


def _check_single(features: HandFeatures, key: str, raw: object) -> bool:
    f = features
    k = key
    # Booleans
    if isinstance(raw, bool):
        return _hand_bool(f, k) == raw
    if isinstance(raw, (int, float)):
        return _hand_number(f, k) == raw
    if isinstance(raw, str):
        return _check_string(f, k, raw)
    if isinstance(raw, list):
        return _hand_value(f, k) in raw
    return True


def _hand_bool(f: HandFeatures, key: str) -> bool:
    return {
        "Balanced": f.balanced,
        "SemiBalanced": f.semi_balanced,
        "Has5CardMajor": f.has_5cm,
        "FourCardMajor": bool(f.four_card_majors),
        "FiveCardMajor": bool(f.five_card_majors),
        "Has6CardSuit": f.has_6c_suit,
        "Void": bool(f.void_suits),
        "Singleton": bool(f.singleton_suits),
    }.get(key, False)


def _hand_number(f: HandFeatures, key: str) -> int:
    if key == "HCP":
        return f.hcp
    if key in ("Spades", "Hearts", "Diamonds", "Clubs"):
        return f.length[key[0]]
    if key.startswith("Length."):
        return f.length.get(key.split(".", 1)[1], 0)
    if key == "QuickTricks":
        return int(round(f.quick_tricks))
    return 0


def _hand_value(f: HandFeatures, key: str) -> object:
    if key == "Shape":
        return f.shape
    if key == "LongestSuit":
        return f.longest_suit
    if key == "FourCardMajors":
        return list(f.four_card_majors)
    if key == "FiveCardMajors":
        return list(f.five_card_majors)
    return None


_NUM_RE = re.compile(r"^\s*(>=|<=|>|<|=)?\s*(-?\d+)\s*$")
_RANGE_RE = re.compile(r"^\s*(-?\d+)\s*(?:\.\.|-)\s*(-?\d+)\s*$")


def _check_string(f: HandFeatures, key: str, raw: str) -> bool:
    raw = raw.strip()
    # numeric checks
    n = _hand_number(f, key)
    m = _RANGE_RE.match(raw)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return lo <= n <= hi
    m = _NUM_RE.match(raw)
    if m:
        op = m.group(1) or "="
        v = int(m.group(2))
        return _cmp(n, op, v)
    # categorical
    val = _hand_value(f, key)
    if val is None:
        return False
    if isinstance(val, list):
        return raw in val
    return str(val) == raw


def _cmp(lhs: int, op: str, rhs: int) -> bool:
    return {
        ">=": lhs >= rhs,
        "<=": lhs <= rhs,
        ">": lhs > rhs,
        "<": lhs < rhs,
        "=": lhs == rhs,
    }[op]


# ---------------------------------------------------------------------------
# Tiny seat/history helpers
# ---------------------------------------------------------------------------


def _partner_of_seat(seat: str) -> str:
    return {"N": "S", "S": "N", "E": "W", "W": "E"}[seat]


def _seat_at(ctx: AuctionContext, idx: int) -> str:
    order = ["N", "E", "S", "W"]
    start = order.index(ctx.dealer)
    return order[(start + idx) % 4]


def _find_last_occurrence(history: List[str], token: str) -> Optional[int]:
    for i in range(len(history) - 1, -1, -1):
        if history[i].upper() == token.upper():
            return i
    return None


def _last_natural_suit_bid_by(ctx: AuctionContext, seat: str) -> Optional[Bid]:
    order = ["N", "E", "S", "W"]
    start = order.index(ctx.dealer)
    for i in range(len(ctx.history_tokens) - 1, -1, -1):
        if order[(start + i) % 4] != seat:
            continue
        tok = ctx.history_tokens[i]
        if tok in ("P", "X", "XX"):
            continue
        try:
            call = parse_bid(tok)
        except Exception:
            continue
        if call.type == BidType.BID and call.bid is not None:
            return call.bid
    return None
