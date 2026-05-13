"""Tests for the DSL validators (readability, symbol consistency, invertibility)."""

from __future__ import annotations

import copy

from bridge_evolve.dsl import (
    BiddingSystem,
    Convention,
    Meaning,
    Requirements,
    Trigger,
    has_errors,
    load_system,
    validate_system,
)


def test_baseline_sayc_validates_cleanly():
    s = load_system("systems/baseline_sayc")
    issues = validate_system(s)
    # Warnings are OK, errors are not.
    assert not has_errors(issues), [
        (i.code, i.target, i.message) for i in issues if i.severity == "error"
    ]


def test_primitives_v0_validates_cleanly():
    s = load_system("systems/primitives_v0")
    issues = validate_system(s)
    assert not has_errors(issues), [
        (i.code, i.target, i.message) for i in issues if i.severity == "error"
    ]


def test_validator_flags_empty_meaning():
    s = load_system("systems/baseline_sayc")
    s = copy.deepcopy(s)
    bad = Convention(
        name="Bad_NoMeaning",
        description="",
        priority=99,
        trigger=Trigger(auction_pattern=["1NT"], seat="responder"),
        requirements=Requirements(constraints={"HCP": ">=10"}),
        meaning=Meaning(fields={}),
        action="2C",
    )
    s.replace_convention(bad)
    issues = validate_system(s)
    codes = {(i.code, i.target) for i in issues}
    assert ("readability/no_meaning", "Bad_NoMeaning") in codes


def test_validator_flags_symbol_inconsistency():
    """Two conventions with the same trigger+action but contradictory
    Meaning fields should raise a consistency error."""
    s = load_system("systems/primitives_v0")
    s = copy.deepcopy(s)
    a = Convention(
        name="ConvA",
        description="bid 2C over 1NT",
        priority=80,
        trigger=Trigger(auction_pattern=["1NT"], seat="responder"),
        requirements=Requirements(constraints={"HCP": ">=8"}),
        meaning=Meaning(fields={"HCP": ">=8", "PromisesMajor": True}),
        action="2C",
    )
    b = Convention(
        name="ConvB",
        description="bid 2C over 1NT (conflict)",
        priority=80,
        trigger=Trigger(auction_pattern=["1NT"], seat="responder"),
        requirements=Requirements(constraints={"HCP": ">=10"}),
        meaning=Meaning(fields={"HCP": ">=10", "PromisesMajor": False}),
        action="2C",
    )
    s.replace_convention(a)
    s.replace_convention(b)
    issues = validate_system(s)
    codes = {i.code for i in issues}
    assert "symbol/overloaded" in codes, codes
