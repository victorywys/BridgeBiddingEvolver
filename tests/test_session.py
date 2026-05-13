"""End-to-end smoke test for the proposer/evaluator session protocol."""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from bridge_evolve.env.trick_estimator import HeuristicEstimator
from bridge_evolve.experiments import (
    SessionConfig,
    evaluate_session,
    open_session,
)
from bridge_evolve.versioning import SystemRepository


@pytest.fixture
def repo(tmp_path: Path) -> SystemRepository:
    return SystemRepository.init_from("systems/primitives_v0", tmp_path / "repo")


def test_session_open_writes_report(repo: SystemRepository):
    cfg = SessionConfig(eval_boards=4, eval_seed=2026, top_failures=2)
    est = HeuristicEstimator()
    session_dir = open_session(repo, cfg, estimator=est)

    assert (session_dir / "STATE.yaml").is_file()
    assert (session_dir / "report.md").is_file()
    assert (session_dir / "report.json").is_file()

    text = (session_dir / "report.md").read_text(encoding="utf-8")
    assert "report" in text.lower()
    assert "primitives_v0" in text
    assert "baseline_sayc" in text


def test_session_evaluate_accepts_good_patch(repo: SystemRepository):
    cfg = SessionConfig(eval_boards=8, eval_seed=2026,
                        target_imps_delta=-1000.0)  # accept anything
    est = HeuristicEstimator()
    session_dir = open_session(repo, cfg, estimator=est)
    patch = textwrap.dedent("""\
        Patch:
          Name: trivial_principle_only
          Rationale: trivial no-op patch for the test
          ParentVersion: v0
          Operations:
            - Op: add_principle
              Target: TestPrinciple
              Payload:
                Text: this is a test principle
        """)
    (session_dir / "patch.yaml").write_text(patch, encoding="utf-8")
    result_path = evaluate_session(repo, session_dir.name, estimator=est)
    assert result_path.is_file()
    text = result_path.read_text(encoding="utf-8")
    # With target_delta hugely negative, any patch is accepted.
    assert "accepted" in text.lower()
    # HEAD should have moved.
    assert repo.head != "v0"


def test_session_evaluate_rejects_invalid_patch(repo: SystemRepository):
    cfg = SessionConfig(eval_boards=4, eval_seed=2026)
    est = HeuristicEstimator()
    session_dir = open_session(repo, cfg, estimator=est)
    # A convention with an empty Meaning must fail readability validation.
    patch = textwrap.dedent("""\
        Patch:
          Name: bad_no_meaning
          Rationale: deliberately invalid
          ParentVersion: v0
          Operations:
            - Op: add_convention
              Target: BadConv
              Payload:
                Name: BadConv
                Description: ""
                Priority: 50
                Trigger:
                  AuctionPattern: ["1C"]
                  Seat: responder
                Requirements:
                  HCP: ">=6"
                Meaning: {}
                Action:
                  Bid: 1D
        """)
    (session_dir / "patch.yaml").write_text(patch, encoding="utf-8")
    result_path = evaluate_session(repo, session_dir.name, estimator=est)
    text = result_path.read_text(encoding="utf-8")
    assert "rejected" in text.lower()
    # HEAD must not have moved.
    assert repo.head == "v0"
