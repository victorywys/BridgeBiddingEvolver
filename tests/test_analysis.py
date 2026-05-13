"""Tests for the analysis layer: information theory + bootstrap + examples."""

from __future__ import annotations

import math

import pytest

from bridge_evolve.analysis import (
    AuctionInfo,
    HandPool,
    paired_bootstrap,
    rule_probability,
    auction_information,
    typical_hands_for_rule,
)
from bridge_evolve.analysis.information import system_rule_stats
from bridge_evolve.dsl import load_system


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_empty_input():
    r = paired_bootstrap([], n_resamples=100)
    assert r.n == 0
    assert r.mean == 0.0


def test_bootstrap_constant_input():
    r = paired_bootstrap([3.0] * 50, n_resamples=200, seed=1)
    assert r.mean == 3.0
    # Constant sample has zero variance so CI collapses to the point estimate.
    assert r.ci_low == pytest.approx(3.0)
    assert r.ci_high == pytest.approx(3.0)
    assert r.p_value_one_sided == 0.0


def test_bootstrap_ci_contains_mean():
    # symmetric noise around +1
    deltas = [1.0 + (i - 50) / 50.0 for i in range(101)]
    r = paired_bootstrap(deltas, n_resamples=500, seed=42, level=0.95)
    assert r.ci_low <= r.mean <= r.ci_high
    # Strong positive signal: lower CI bound should be > 0.
    assert r.ci_low > 0


def test_bootstrap_detects_zero_signal():
    deltas = [(i % 5) - 2.0 for i in range(100)]   # mean = 0
    r = paired_bootstrap(deltas, n_resamples=500, seed=7)
    assert r.ci_low < 0 < r.ci_high
    assert 0.2 < r.p_value_one_sided < 0.8


# ---------------------------------------------------------------------------
# Information
# ---------------------------------------------------------------------------


def test_hand_pool_is_deterministic():
    p1 = HandPool.build(size=400, seed=99)
    p2 = HandPool.build(size=400, seed=99)
    assert len(p1.features) == 400 == len(p2.features)
    # Same seed => same first-hand HCP across calls.
    assert p1.features[0].hcp == p2.features[0].hcp
    assert p1.features[-1].shape == p2.features[-1].shape


def test_rule_probability_for_baseline_1nt():
    s = load_system("systems/baseline_sayc")
    pool = HandPool.build(size=2000, seed=12345)
    nt_rule = next((r for r in s.opening_table if "1NT" in r.name or r.action == "1NT"), None)
    assert nt_rule is not None
    rs = rule_probability(nt_rule, pool)
    # 15-17 balanced ≈ 3-5% of hands. Cushion the test range to be robust
    # to Monte-Carlo noise at N=2000.
    assert 0.01 < rs.probability < 0.10
    assert math.isfinite(rs.bits)
    # bits should match -log2(p) within rounding.
    assert rs.bits == pytest.approx(-math.log2(rs.probability), abs=1e-6)


def test_system_rule_stats_covers_everything():
    s = load_system("systems/baseline_sayc")
    pool = HandPool.build(size=500, seed=1)
    stats = system_rule_stats(s, pool)
    names = {r.name for r in stats}
    for r in s.opening_table:
        assert r.name in names
    for c in s.conventions:
        assert c.name in names


def test_auction_information_empty_auction():
    s = load_system("systems/primitives_v0")
    pool = HandPool.build(size=200, seed=2)
    info = auction_information([], s, pool)
    assert info.total_bits == 0.0
    assert info.positive_calls == 0


def test_auction_information_pass_is_zero_bits():
    s = load_system("systems/primitives_v0")
    pool = HandPool.build(size=200, seed=3)
    steps = [
        {"seat": "N", "token": "P", "rule_type": "pass", "rule_name": "pass"},
        {"seat": "E", "token": "P", "rule_type": "pass", "rule_name": "pass"},
    ]
    info = auction_information(steps, s, pool)
    assert info.total_bits == 0.0
    assert info.positive_calls == 0


# ---------------------------------------------------------------------------
# Typical hand examples
# ---------------------------------------------------------------------------


def test_typical_hands_for_opening():
    s = load_system("systems/baseline_sayc")
    rule = next(r for r in s.opening_table if r.action == "1S")
    hands = typical_hands_for_rule(rule, n=3, pool_size=600, seed=11)
    assert 1 <= len(hands) <= 3
    for t in hands:
        assert "S:" in t.pretty
        assert 12 <= t.hcp <= 25
        assert t.label in ("min", "median", "max", "q1", "q2", "q3")


def test_typical_hands_unsatisfiable_rule_returns_empty():
    from bridge_evolve.dsl.schema import OpeningRule, Requirements
    impossible = OpeningRule(
        name="Impossible",
        priority=99,
        description="impossible to satisfy",
        requirements=Requirements(constraints={"HCP": ">=999"}),
        action="7NT",
    )
    hands = typical_hands_for_rule(impossible, n=3, pool_size=200, seed=1)
    assert hands == []
