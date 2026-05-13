"""Disk-mediated evolution sessions.

Each evolution iteration is a stateless function of the disk state:

```
repo/
  versions/<HEAD>/...                  current bidding system
  sessions/<id>/
    report.md       (written by evaluator; read by the proposer)
    patch.yaml      (written by the proposer; read by evaluator)
    result.md       (written by evaluator after accept/reject)
```

A *proposer* (the LLM running in this chat) reads ``report.md``, writes a
``patch.yaml`` using the documented DSL, and signals completion. The
evaluator then validates the patch, runs the IMP-match fitness check
against a fixed baseline system, snapshots a new version if the patch
wins, and writes ``result.md``.

The protocol is intentionally simple and replayable: every session is a
plain folder on disk; nothing about the loop depends on conversational
memory.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..agent import SystemAgent
from ..benchmark import run_imp_match, imps_vs_par_single
from ..benchmark.imp_match import BoardResult, MatchResult
from ..dsl import (
    BiddingSystem,
    has_errors,
    load_system,
    validate_system,
)
from ..env.deal import deal_random
from ..env.scoring import par_score, score_difference_to_imps
from ..env.trick_estimator import (
    DDSEstimator,
    TrickEstimator,
    get_default_estimator,
)
from ..reflection.analyzer import analyze_auction
from ..reflection.patch import Patch, apply_patch
from ..versioning import SystemRepository


# ---------------------------------------------------------------------------
# Session metadata
# ---------------------------------------------------------------------------


@dataclass
class SessionConfig:
    eval_boards: int = 200
    eval_seed: int = 20260101
    baseline_path: str = "systems/baseline_sayc"
    target_imps_delta: float = 0.05    # minimum improvement to accept
    top_failures: int = 8              # how many failure deals to include in report


@dataclass
class SessionState:
    id: str
    repo: str
    head_version: str
    config: SessionConfig
    created: str
    status: str = "open"               # open | proposed | evaluated | accepted | rejected
    accepted_version: Optional[str] = None

    def to_yaml(self) -> Dict[str, Any]:
        return {
            "Id": self.id,
            "Repo": self.repo,
            "HeadVersion": self.head_version,
            "Created": self.created,
            "Status": self.status,
            "AcceptedVersion": self.accepted_version,
            "Config": {
                "EvalBoards": self.config.eval_boards,
                "EvalSeed": self.config.eval_seed,
                "BaselinePath": self.config.baseline_path,
                "TargetImpsDelta": self.config.target_imps_delta,
                "TopFailures": self.config.top_failures,
            },
        }

    @classmethod
    def from_yaml(cls, data: Dict[str, Any]) -> "SessionState":
        c = data.get("Config", {}) or {}
        cfg = SessionConfig(
            eval_boards=int(c.get("EvalBoards", 200)),
            eval_seed=int(c.get("EvalSeed", 20260101)),
            baseline_path=str(c.get("BaselinePath", "systems/baseline_sayc")),
            target_imps_delta=float(c.get("TargetImpsDelta", 0.05)),
            top_failures=int(c.get("TopFailures", 8)),
        )
        return cls(
            id=data["Id"], repo=data["Repo"], head_version=data["HeadVersion"],
            config=cfg, created=data.get("Created", ""),
            status=data.get("Status", "open"),
            accepted_version=data.get("AcceptedVersion"),
        )


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _sessions_dir(repo: SystemRepository) -> Path:
    p = Path(repo.root) / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _next_session_id(repo: SystemRepository) -> str:
    existing = sorted(p.name for p in _sessions_dir(repo).iterdir() if p.is_dir())
    n = 0
    for name in existing:
        m = re.match(r"s(\d+)$", name)
        if m:
            n = max(n, int(m.group(1)))
    return f"s{n + 1:04d}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def open_session(repo: SystemRepository, config: Optional[SessionConfig] = None,
                 estimator: Optional[TrickEstimator] = None) -> Path:
    """Run a benchmark of the current HEAD vs. baseline and write a report
    for the proposer. Returns the session directory.
    """
    cfg = config or SessionConfig()
    est = estimator or get_default_estimator()
    sid = _next_session_id(repo)
    session_dir = _sessions_dir(repo) / sid
    session_dir.mkdir(parents=True, exist_ok=True)

    state = SessionState(
        id=sid, repo=str(repo.root), head_version=repo.head,
        config=cfg, created=_now(), status="open",
    )
    _save_state(session_dir, state)

    # Run the benchmark.
    current = load_system(repo.path_for(repo.head))
    baseline = load_system(cfg.baseline_path)
    match = _run_match(current, baseline, cfg, estimator=est)

    # Aggregate diagnoses on the "current" side.
    diag_counts: Dict[str, int] = {}
    for board in match.boards:
        if not board.open_auction:
            continue
        # rebuild a minimal AuctionRecord-like view for analyze_auction
        pass

    report_path = session_dir / "report.md"
    report_path.write_text(_render_report(state, current, baseline, match),
                           encoding="utf-8")

    # Persist the structured board data alongside the markdown so a
    # downstream tool can read it without re-parsing prose.
    (session_dir / "report.json").write_text(
        json.dumps(_match_to_json(match), indent=2), encoding="utf-8"
    )
    return session_dir


def evaluate_session(repo: SystemRepository, session_id: str,
                     estimator: Optional[TrickEstimator] = None) -> Path:
    """Validate ``patch.yaml`` in the session folder, score it on the same
    eval deals, accept or reject, and write ``result.md``.
    """
    est = estimator or get_default_estimator()
    session_dir = _sessions_dir(repo) / session_id
    state = _load_state(session_dir)
    cfg = state.config

    patch_path = session_dir / "patch.yaml"
    if not patch_path.exists():
        raise FileNotFoundError(f"no patch.yaml in {session_dir}")
    patch = Patch.from_yaml(patch_path.read_text(encoding="utf-8"))

    current = load_system(repo.path_for(state.head_version))
    baseline = load_system(cfg.baseline_path)

    # 1) Static validation.
    candidate = apply_patch(current, patch)
    issues = validate_system(candidate)
    accepted = False
    notes: List[str] = []
    new_version: Optional[str] = None
    baseline_match: Optional[MatchResult] = None
    candidate_match: Optional[MatchResult] = None
    if has_errors(issues):
        notes.append("REJECTED: candidate system fails validation:")
        for i in issues:
            if i.severity == "error":
                notes.append(f"  - [{i.code}] {i.target}: {i.message}")
    else:
        # 2) Fitness on the same eval deals.
        baseline_match = _run_match(current, baseline, cfg, estimator=est)
        candidate_match = _run_match(candidate, baseline, cfg, estimator=est)
        delta = candidate_match.avg_imps_for_a - baseline_match.avg_imps_for_a
        notes.append(
            f"baseline avg IMPs vs SAYC: {baseline_match.avg_imps_for_a:+.3f}"
        )
        notes.append(
            f"candidate avg IMPs vs SAYC: {candidate_match.avg_imps_for_a:+.3f}"
        )
        notes.append(f"delta: {delta:+.3f} (target: {cfg.target_imps_delta:+.3f})")
        if delta >= cfg.target_imps_delta:
            accepted = True
            new_version = _next_version(state.head_version)
            repo.commit_patch(patch, new_version=new_version,
                              parent=state.head_version,
                              metrics={"avg_imps_for_a": candidate_match.avg_imps_for_a,
                                       "vs_baseline_delta": delta,
                                       "n_boards": candidate_match.n_boards})
            state.accepted_version = new_version
            state.status = "accepted"
            notes.append(f"ACCEPTED -> {new_version}")
        else:
            state.status = "rejected"
            notes.append("REJECTED: insufficient improvement")

    # 3) Write result.
    if state.status == "open":
        state.status = "rejected"
    result_path = session_dir / "result.md"
    result_path.write_text(_render_result(state, patch, issues, notes,
                                          baseline_match, candidate_match),
                           encoding="utf-8")
    _save_state(session_dir, state)
    return result_path


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _run_match(candidate: BiddingSystem, baseline: BiddingSystem,
               cfg: SessionConfig, estimator: TrickEstimator) -> MatchResult:
    deals = [deal_random(seed=cfg.eval_seed + i, dealer="N")
             for i in range(cfg.eval_boards)]
    return run_imp_match(
        a_factory=lambda: SystemAgent(candidate),
        b_factory=lambda: SystemAgent(baseline),
        n_boards=cfg.eval_boards,
        seed=cfg.eval_seed,
        estimator=estimator,
        deals=deals,
    )


def _save_state(session_dir: Path, state: SessionState) -> None:
    (session_dir / "STATE.yaml").write_text(
        yaml.safe_dump(state.to_yaml(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _load_state(session_dir: Path) -> SessionState:
    data = yaml.safe_load((session_dir / "STATE.yaml").read_text(encoding="utf-8"))
    return SessionState.from_yaml(data)


def _next_version(parent: str) -> str:
    if parent.startswith("v") and parent[1:].isdigit():
        return f"v{int(parent[1:]) + 1}"
    return parent + "_x"


def _match_to_json(match: MatchResult) -> Dict[str, Any]:
    return {
        "summary": match.summary(),
        "boards": [
            {
                "seed": b.deal_seed,
                "open_score": b.open_score,
                "closed_score": b.closed_score,
                "imps_for_a": b.imps_for_a,
                "par_ns": b.par_ns,
                "open_contract": b.open_contract,
                "closed_contract": b.closed_contract,
                "open_auction": b.open_auction,
                "closed_auction": b.closed_auction,
                "open_meanings": b.open_meanings,
                "closed_meanings": b.closed_meanings,
            } for b in match.boards
        ],
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _render_report(state: SessionState, current: BiddingSystem,
                   baseline: BiddingSystem, match: MatchResult) -> str:
    cfg = state.config
    boards = sorted(match.boards, key=lambda b: b.imps_for_a)
    worst = boards[: cfg.top_failures]
    best = list(reversed(boards[-min(cfg.top_failures, 4):]))

    lines: List[str] = []
    lines.append(f"# Session {state.id} report")
    lines.append("")
    lines.append(f"- Created: `{state.created}`")
    lines.append(f"- Repo: `{state.repo}`")
    lines.append(f"- HEAD version: **`{state.head_version}`** ({current.name})")
    lines.append(f"- Baseline: `{cfg.baseline_path}` ({baseline.name})")
    lines.append(f"- Eval boards: {cfg.eval_boards}, eval seed: {cfg.eval_seed}")
    lines.append("")
    lines.append("## Match summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    s = match.summary()
    for k, v in s.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## Current system inventory")
    lines.append("")
    lines.append(f"- Opening rules: {len(current.opening_table)} -- "
                 + ", ".join(r.name for r in current.opening_table) or "(none)")
    lines.append(f"- Conventions: {len(current.conventions)} -- "
                 + ", ".join(c.name for c in current.conventions) or "(none)")
    lines.append(f"- Principles: {len(current.principles)}")
    lines.append("")
    lines.append("## Worst boards for current system")
    lines.append("")
    for b in worst:
        lines.append(_render_board_block(b, state, current))
    lines.append("")
    lines.append("## Best boards for current system")
    lines.append("")
    for b in best:
        lines.append(_render_board_block(b, state, current))
    lines.append("")
    lines.append("## How to propose a patch")
    lines.append("")
    lines.append(
        "Write `patch.yaml` in this folder using the Patch schema. "
        "After writing, run `python -m bridge_evolve.cli session evaluate "
        f"--repo {state.repo} --id {state.id}` to validate and score it."
    )
    lines.append("")
    lines.append("Patch operations: `add_convention`, `remove_convention`, "
                 "`modify_convention`, `add_opening`, `remove_opening`, "
                 "`add_principle`, `remove_principle`.")
    lines.append("")
    lines.append("Hard rules:")
    lines.append("- Every Convention must have non-empty Description and Meaning.")
    lines.append("- A bid's meaning must be recoverable by interpret_partner_bid "
                 "from the docs alone (validator enforces this).")
    lines.append("- A patch is accepted only if it improves avg IMPs vs the "
                 "baseline by at least the target delta on the same eval deals.")
    return "\n".join(lines)


def _render_board_block(b: BoardResult, state: SessionState,
                        current: BiddingSystem) -> str:
    deal = deal_random(seed=b.deal_seed, dealer="N")
    hands = " | ".join(f"{s}: {deal.hand(s).pretty()}" for s in "NESW")
    return (
        f"### Board seed={b.deal_seed} -- IMPs for A: {b.imps_for_a:+d}\n"
        f"\n"
        f"- Hands: {hands}\n"
        f"- Par (NS): {b.par_ns}\n"
        f"- Open room (A=NS) contract: `{b.open_contract or 'passed out'}`  "
        f"score: {b.open_score}\n"
        f"- Closed room (B=NS) contract: `{b.closed_contract or 'passed out'}`  "
        f"score: {b.closed_score}\n"
        f"- Open room auction (A=NS):  `{' '.join(b.open_auction) or '(empty)'}`\n"
        f"- Closed room auction (B=NS): `{' '.join(b.closed_auction) or '(empty)'}`\n"
    )


def _render_result(state: SessionState, patch: Patch, issues, notes,
                   baseline_match: Optional[MatchResult],
                   candidate_match: Optional[MatchResult]) -> str:
    lines = [
        f"# Session {state.id} result",
        "",
        f"- Status: **{state.status}**",
        f"- Accepted version: `{state.accepted_version or '(none)'}`",
        f"- Patch: `{patch.name}`",
        "",
        "## Notes",
        "",
    ]
    lines.extend(f"- {n}" for n in notes)
    lines.append("")
    if issues:
        lines.append("## Validation issues")
        lines.append("")
        for i in issues:
            lines.append(f"- [{i.severity}] `{i.code}` @ `{i.target}`: {i.message}")
        lines.append("")
    if candidate_match is not None and baseline_match is not None:
        lines.append("## Top boards where the patch helped")
        lines.append("")
        # Compare per-deal: candidate vs baseline IMPs (both vs SAYC). Need a join.
        b_map = {b.deal_seed: b for b in baseline_match.boards}
        diffs = []
        for cb in candidate_match.boards:
            bb = b_map.get(cb.deal_seed)
            if bb is None:
                continue
            diffs.append((cb.imps_for_a - bb.imps_for_a, cb, bb))
        diffs.sort(key=lambda t: t[0], reverse=True)
        for delta, cb, bb in diffs[:5]:
            lines.append(
                f"- seed={cb.deal_seed}: Δ{delta:+d} IMPs "
                f"(cand `{cb.open_contract}` vs base `{bb.open_contract}`)"
            )
        lines.append("")
        lines.append("## Top boards where the patch hurt")
        lines.append("")
        for delta, cb, bb in diffs[-5:]:
            lines.append(
                f"- seed={cb.deal_seed}: Δ{delta:+d} IMPs "
                f"(cand `{cb.open_contract}` vs base `{bb.open_contract}`)"
            )
    return "\n".join(lines)
