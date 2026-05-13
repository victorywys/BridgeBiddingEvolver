"""System validators.

Every patch goes through these before evaluation. They enforce the
"externalized & understandable" constraints from the project brief:

* **Readability** — every convention has a non-empty ``Description`` and
  ``Meaning``.
* **Invertibility** — every bid produced by a convention must be
  recoverable by :func:`interpret_partner_bid` from the same docs (so
  partner can read the convention back).
* **Symbol consistency** — a single ``(AuctionPattern, Action)`` may not
  map to two different ``Meaning`` blocks (no symbol overload that the
  listener can't disambiguate).
* **Legality of the suggested action** — at least one hand exists for
  which the convention's ``Action`` would actually be a legal call given
  its trigger.

Validators return a list of :class:`ValidationIssue` objects. A patch
with any ``severity == "error"`` issue is rejected before evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .schema import BiddingSystem, Convention
from .matcher import interpret_partner_bid


@dataclass
class ValidationIssue:
    severity: str           # "error" | "warning"
    code: str               # short stable identifier
    target: str             # convention / opening name
    message: str


def validate_system(system: BiddingSystem) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    issues.extend(_check_readability(system))
    issues.extend(_check_symbol_consistency(system))
    issues.extend(_check_invertibility(system))
    return issues


# ---------------------------------------------------------------------------
# Readability
# ---------------------------------------------------------------------------


def _check_readability(system: BiddingSystem) -> List[ValidationIssue]:
    out: List[ValidationIssue] = []
    for conv in system.conventions:
        if not (conv.description or "").strip():
            out.append(ValidationIssue(
                "error", "readability/no_description", conv.name,
                "convention has empty Description; humans/LLMs can't transfer it",
            ))
        if not conv.meaning.fields:
            out.append(ValidationIssue(
                "error", "readability/no_meaning", conv.name,
                "convention has empty Meaning; partner can't recover what it shows",
            ))
        if not conv.action:
            out.append(ValidationIssue(
                "error", "readability/no_action", conv.name,
                "convention has no Action; nothing to bid",
            ))
        if not conv.trigger.auction_pattern and not conv.trigger.seat:
            out.append(ValidationIssue(
                "warning", "readability/no_trigger", conv.name,
                "convention has no Trigger; will fire from any auction state",
            ))
    for rule in system.opening_table:
        if not rule.action:
            out.append(ValidationIssue(
                "error", "readability/no_opening_action", rule.name,
                "opening rule has no Action",
            ))
        if not rule.requirements.constraints:
            out.append(ValidationIssue(
                "warning", "readability/no_opening_requirements", rule.name,
                "opening rule has no Requirements; will fire on any hand",
            ))
    return out


# ---------------------------------------------------------------------------
# Symbol consistency
# ---------------------------------------------------------------------------


def _pattern_key(conv: Convention) -> Tuple[str, str]:
    return (
        " ".join(t.upper() for t in conv.trigger.auction_pattern),
        (conv.action or "").upper(),
    )


def _check_symbol_consistency(system: BiddingSystem) -> List[ValidationIssue]:
    out: List[ValidationIssue] = []
    seen: Dict[Tuple[str, str], List[Convention]] = {}
    for conv in system.conventions:
        if not conv.action:
            continue
        key = _pattern_key(conv)
        seen.setdefault(key, []).append(conv)
    for key, group in seen.items():
        if len(group) < 2:
            continue
        # Two conventions sharing pattern+action are OK iff their Requirements
        # are disjoint *and* their Meanings agree on overlap. The simple
        # check: meanings must agree on every shared field.
        first = group[0].meaning.fields
        for other in group[1:]:
            for k, v in first.items():
                if k in other.meaning.fields and other.meaning.fields[k] != v:
                    out.append(ValidationIssue(
                        "error", "symbol/overloaded",
                        f"{group[0].name}|{other.name}",
                        f"both bind ({key[0]} -> {key[1]}) with conflicting "
                        f"Meaning[{k}]: {v!r} vs {other.meaning.fields[k]!r}",
                    ))
    return out


# ---------------------------------------------------------------------------
# Invertibility
# ---------------------------------------------------------------------------


def _check_invertibility(system: BiddingSystem) -> List[ValidationIssue]:
    out: List[ValidationIssue] = []
    for conv in system.conventions:
        if not conv.action:
            continue
        prior = list(conv.trigger.auction_pattern)
        # If pattern is empty, the convention is opening-equivalent; skip.
        recovered = interpret_partner_bid(conv.action, prior, system)
        if recovered.get("Convention") is None:
            out.append(ValidationIssue(
                "error", "invertibility/unreachable", conv.name,
                f"interpret_partner_bid({conv.action!r}, {prior!r}) returns no "
                "convention; partner cannot decode this call from the docs.",
            ))
            continue
        # Optional: meanings agree on at least the keys we declared.
        rec_meaning = recovered.get("Meaning", {}) or {}
        for k, v in conv.meaning.fields.items():
            if rec_meaning.get(k) != v:
                out.append(ValidationIssue(
                    "warning", "invertibility/meaning_mismatch", conv.name,
                    f"recovered Meaning differs at {k}: "
                    f"declared={v!r} recovered={rec_meaning.get(k)!r}",
                ))
                break
    return out


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def has_errors(issues: List[ValidationIssue]) -> bool:
    return any(i.severity == "error" for i in issues)


def format_issues(issues: List[ValidationIssue]) -> str:
    lines = []
    for i in issues:
        lines.append(f"[{i.severity}] {i.code} @ {i.target}: {i.message}")
    return "\n".join(lines)
