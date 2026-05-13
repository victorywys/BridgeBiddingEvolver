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
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..agent import SystemAgent
from ..analysis import (
    AuctionInfo,
    BootstrapResult,
    HandPool,
    RuleStats,
    auction_information,
    paired_bootstrap,
    typical_hands_for_rule,
)
from ..analysis.information import system_rule_stats
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
    eval_boards: int = 500
    eval_seed: int = 20260101
    baseline_path: str = "systems/baseline_sayc"
    # Acceptance: mean delta must clear ``target_imps_delta`` AND the
    # paired-bootstrap lower CI bound must be > ``min_ci_low``.
    target_imps_delta: float = 0.25
    min_ci_low: float = 0.0
    bootstrap_resamples: int = 2000
    bootstrap_level: float = 0.95
    bootstrap_seed: int = 17
    # Analysis layers
    info_pool_size: int = 4000
    info_pool_seed: int = 13_576
    typical_hands_per_rule: int = 3
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
                "MinCiLow": self.config.min_ci_low,
                "BootstrapResamples": self.config.bootstrap_resamples,
                "BootstrapLevel": self.config.bootstrap_level,
                "BootstrapSeed": self.config.bootstrap_seed,
                "InfoPoolSize": self.config.info_pool_size,
                "InfoPoolSeed": self.config.info_pool_seed,
                "TypicalHandsPerRule": self.config.typical_hands_per_rule,
                "TopFailures": self.config.top_failures,
            },
        }

    @classmethod
    def from_yaml(cls, data: Dict[str, Any]) -> "SessionState":
        c = data.get("Config", {}) or {}
        cfg = SessionConfig(
            eval_boards=int(c.get("EvalBoards", 500)),
            eval_seed=int(c.get("EvalSeed", 20260101)),
            baseline_path=str(c.get("BaselinePath", "systems/baseline_sayc")),
            target_imps_delta=float(c.get("TargetImpsDelta", 0.25)),
            min_ci_low=float(c.get("MinCiLow", 0.0)),
            bootstrap_resamples=int(c.get("BootstrapResamples", 2000)),
            bootstrap_level=float(c.get("BootstrapLevel", 0.95)),
            bootstrap_seed=int(c.get("BootstrapSeed", 17)),
            info_pool_size=int(c.get("InfoPoolSize", 4000)),
            info_pool_seed=int(c.get("InfoPoolSeed", 13_576)),
            typical_hands_per_rule=int(c.get("TypicalHandsPerRule", 3)),
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

    # Load systems + build a shared hand pool for information analysis.
    current = load_system(repo.path_for(repo.head))
    baseline = load_system(cfg.baseline_path)
    pool = HandPool.build(size=cfg.info_pool_size, seed=cfg.info_pool_seed)

    # Run the benchmark.
    match = _run_match(current, baseline, cfg, estimator=est)

    # Per-rule probabilities and per-board auction information.
    rule_stats = system_rule_stats(current, pool)
    board_info = _board_information(match, current, pool)

    # Typical hands per opening rule.
    typical = _typical_hands_per_rule(current, cfg)

    report_path = session_dir / "report.md"
    report_path.write_text(
        _render_report(state, current, baseline, match, rule_stats, board_info,
                       typical),
        encoding="utf-8",
    )

    (session_dir / "report.json").write_text(
        json.dumps(_match_to_json(match, rule_stats, board_info, typical),
                   indent=2),
        encoding="utf-8",
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
    pool = HandPool.build(size=cfg.info_pool_size, seed=cfg.info_pool_seed)

    # 1) Static validation.
    candidate = apply_patch(current, patch)
    issues = validate_system(candidate)
    notes: List[str] = []
    new_version: Optional[str] = None
    baseline_match: Optional[MatchResult] = None
    candidate_match: Optional[MatchResult] = None
    boot: Optional[BootstrapResult] = None
    cand_avg_bits: Optional[float] = None
    base_avg_bits: Optional[float] = None
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

        # Paired bootstrap over per-board deltas (same deal seed pairs).
        b_map = {b.deal_seed: b.imps_for_a for b in baseline_match.boards}
        deltas = [c.imps_for_a - b_map[c.deal_seed] for c in candidate_match.boards
                  if c.deal_seed in b_map]
        boot = paired_bootstrap(deltas,
                                n_resamples=cfg.bootstrap_resamples,
                                level=cfg.bootstrap_level,
                                seed=cfg.bootstrap_seed)

        # Information density: average bits/positive-call per side over
        # the open-room (where the system in question sits NS).
        cand_info = _board_information(candidate_match, candidate, pool)
        base_info = _board_information(baseline_match, current, pool)
        cand_avg_bits = _avg_bits_per_call(cand_info)
        base_avg_bits = _avg_bits_per_call(base_info)

        notes.append(
            f"baseline avg IMPs vs SAYC: {baseline_match.avg_imps_for_a:+.3f}"
        )
        notes.append(
            f"candidate avg IMPs vs SAYC: {candidate_match.avg_imps_for_a:+.3f}"
        )
        notes.append(
            f"paired delta: {boot.mean:+.3f} IMPs/board "
            f"[{int(boot.level*100)}% CI {boot.ci_low:+.3f}, {boot.ci_high:+.3f}] "
            f"p={boot.p_value_one_sided:.3f}, n={boot.n}"
        )
        notes.append(
            f"acceptance gates: mean ≥ {cfg.target_imps_delta:+.3f} "
            f"AND CI_low > {cfg.min_ci_low:+.3f}"
        )
        notes.append(
            f"info density: candidate {cand_avg_bits:.2f} bits/call "
            f"vs current {base_avg_bits:.2f} bits/call"
        )

        accept_mean = delta >= cfg.target_imps_delta
        accept_ci = boot.ci_low > cfg.min_ci_low
        if accept_mean and accept_ci:
            new_version = _next_version(state.head_version)
            repo.commit_patch(patch, new_version=new_version,
                              parent=state.head_version,
                              metrics={
                                  "avg_imps_for_a": candidate_match.avg_imps_for_a,
                                  "vs_baseline_delta": delta,
                                  "ci_low": boot.ci_low,
                                  "ci_high": boot.ci_high,
                                  "p_value_one_sided": boot.p_value_one_sided,
                                  "n_boards": candidate_match.n_boards,
                                  "info_bits_per_call": cand_avg_bits,
                              })
            state.accepted_version = new_version
            state.status = "accepted"
            notes.append(f"ACCEPTED -> {new_version}")
        else:
            state.status = "rejected"
            why = []
            if not accept_mean:
                why.append(f"mean delta {delta:+.3f} < target {cfg.target_imps_delta:+.3f}")
            if not accept_ci:
                why.append(f"CI lower bound {boot.ci_low:+.3f} ≤ {cfg.min_ci_low:+.3f}")
            notes.append("REJECTED: " + "; ".join(why))

    # 3) Write result.
    if state.status == "open":
        state.status = "rejected"
    result_path = session_dir / "result.md"
    result_path.write_text(
        _render_result(state, patch, issues, notes,
                       baseline_match, candidate_match, boot,
                       cand_avg_bits, base_avg_bits),
        encoding="utf-8",
    )
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


def _match_to_json(match: MatchResult,
                   rule_stats: Optional[List[RuleStats]] = None,
                   board_info: Optional[Dict[int, AuctionInfo]] = None,
                   typical: Optional[Dict[str, List[Any]]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
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
                "open_bids": b.open_bids,
                "closed_bids": b.closed_bids,
                "open_info": (board_info.get(b.deal_seed).as_dict()
                              if board_info and b.deal_seed in board_info
                              else None),
            } for b in match.boards
        ],
    }
    if rule_stats is not None:
        out["rule_stats"] = [r.as_dict() for r in rule_stats]
    if typical is not None:
        out["typical_hands"] = {
            k: [t.as_dict() for t in v] for k, v in typical.items()
        }
    return out


def _board_information(match: MatchResult, system: BiddingSystem,
                       pool: HandPool) -> Dict[int, AuctionInfo]:
    """Compute auction information for each board's *open room* auction
    (where the supplied system sat NS)."""
    out: Dict[int, AuctionInfo] = {}
    for b in match.boards:
        out[b.deal_seed] = auction_information(b.open_bids, system, pool)
    return out


def _avg_bits_per_call(board_info: Dict[int, AuctionInfo]) -> float:
    """Average bits per positive call aggregated over all boards.

    Computed as total_bits / total_positive_calls rather than mean of
    means so passout boards (0 calls) don't get weighted equally with
    long auctions.
    """
    total_bits = 0.0
    total_calls = 0
    for info in board_info.values():
        total_bits += info.total_bits
        total_calls += info.positive_calls
    return total_bits / total_calls if total_calls else 0.0


def _typical_hands_per_rule(system: BiddingSystem, cfg: SessionConfig
                            ) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {}
    for r in system.opening_table:
        out[r.name] = typical_hands_for_rule(
            r, n=cfg.typical_hands_per_rule,
            pool_size=2500, seed=cfg.info_pool_seed,
        )
    for c in system.conventions:
        out[c.name] = typical_hands_for_rule(
            c, n=cfg.typical_hands_per_rule,
            pool_size=2500, seed=cfg.info_pool_seed,
        )
    return out


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def _render_report(state: SessionState, current: BiddingSystem,
                   baseline: BiddingSystem, match: MatchResult,
                   rule_stats: List[RuleStats],
                   board_info: Dict[int, AuctionInfo],
                   typical: Dict[str, List[Any]]) -> str:
    cfg = state.config
    boards = sorted(match.boards, key=lambda b: b.imps_for_a)
    worst = boards[: cfg.top_failures]
    best = list(reversed(boards[-min(cfg.top_failures, 4):]))

    avg_bits = _avg_bits_per_call(board_info)
    total_bits = sum(i.total_bits for i in board_info.values())
    total_calls = sum(i.positive_calls for i in board_info.values())

    lines: List[str] = []
    lines.append(f"# Session {state.id} report")
    lines.append("")
    lines.append(f"- Created: `{state.created}`")
    lines.append(f"- Repo: `{state.repo}`")
    lines.append(f"- HEAD version: **`{state.head_version}`** ({current.name})")
    lines.append(f"- Baseline: `{cfg.baseline_path}` ({baseline.name})")
    lines.append(f"- Eval boards: {cfg.eval_boards}, eval seed: {cfg.eval_seed}")
    lines.append(f"- Hand pool: {cfg.info_pool_size} hands @ seed {cfg.info_pool_seed}")
    lines.append("")
    lines.append("## Match summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    s = match.summary()
    for k, v in s.items():
        lines.append(f"| {k} | {v} |")
    lines.append(f"| avg_bits_per_call (open room) | {avg_bits:.2f} |")
    lines.append(f"| total_bits / positive_calls | {total_bits:.0f} / {total_calls} |")
    lines.append("")

    lines.append("## System inventory + rule probabilities")
    lines.append("")
    lines.append(
        "*Probability* = fraction of random 13-card hands matching the rule's "
        "Requirements; *bits* = self-information `-log2(p)` (a higher number "
        "means a rarer, more informative call)."
    )
    lines.append("")
    lines.append("### Opening table")
    lines.append("")
    lines.append("| Rule | Bid | P(hand) | bits | matches/pool |")
    lines.append("|---|---|---:|---:|---|")
    opening_names = {r.name for r in current.opening_table}
    for rs in rule_stats:
        if rs.name not in opening_names:
            continue
        bits = "∞" if not math.isfinite(rs.bits) else f"{rs.bits:.2f}"
        lines.append(f"| `{rs.name}` | `{rs.bid}` | {rs.probability:.4f} | "
                     f"{bits} | {rs.matches}/{rs.pool_size} |")
    lines.append("")
    if current.conventions:
        lines.append("### Conventions")
        lines.append("")
        lines.append("| Rule | Bid | P(hand) | bits | matches/pool |")
        lines.append("|---|---|---:|---:|---|")
        conv_names = {c.name for c in current.conventions}
        for rs in rule_stats:
            if rs.name not in conv_names:
                continue
            bits = "∞" if not math.isfinite(rs.bits) else f"{rs.bits:.2f}"
            lines.append(f"| `{rs.name}` | `{rs.bid}` | {rs.probability:.4f} | "
                         f"{bits} | {rs.matches}/{rs.pool_size} |")
        lines.append("")
    lines.append(f"- Principles: {len(current.principles)} "
                 f"({', '.join(p.name for p in current.principles) or '(none)'})")
    lines.append("")

    lines.append("## Typical hands per opening rule")
    lines.append("")
    lines.append(
        "Sampled by rejection from a fresh pool of 2,500 hands; the listed "
        "examples are the min-HCP, median-HCP and max-HCP matches "
        "(these are descriptive, not normative — the matcher never reads them)."
    )
    lines.append("")
    for r in current.opening_table:
        lines.append(f"### `{r.name}` -- bids `{r.action}`")
        lines.append("")
        examples = typical.get(r.name, [])
        if not examples:
            lines.append("  *(no hands in pool match these requirements)*")
        else:
            for t in examples:
                lines.append(f"- **{t.label}** ({t.hcp} HCP, shape {t.shape}): `{t.pretty}`")
        lines.append("")

    lines.append("## Worst boards for current system")
    lines.append("")
    for b in worst:
        lines.append(_render_board_block(b, state, current, board_info))
    lines.append("")
    lines.append("## Best boards for current system")
    lines.append("")
    for b in best:
        lines.append(_render_board_block(b, state, current, board_info))
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
    lines.append(
        f"- Acceptance: mean ΔIMPs ≥ {cfg.target_imps_delta:+.3f} **and** "
        f"the {int(cfg.bootstrap_level*100)}%-CI lower bound on paired "
        f"per-board deltas exceeds {cfg.min_ci_low:+.3f}."
    )
    return "\n".join(lines)


def _render_board_block(b: BoardResult, state: SessionState,
                        current: BiddingSystem,
                        board_info: Dict[int, AuctionInfo]) -> str:
    deal = deal_random(seed=b.deal_seed, dealer="N")
    hands = " | ".join(f"{s}: {deal.hand(s).pretty()}" for s in "NESW")
    info = board_info.get(b.deal_seed)
    info_line = ""
    if info is not None and info.positive_calls > 0:
        per_call = " ".join(
            f"`{step.token}`({step.bits:.1f}b)" for step in info.steps
            if step.token not in ("P", "PASS", "X", "XX") and math.isfinite(step.bits)
        )
        info_line = (
            f"- Open-room info: {info.total_bits:.1f} bits across "
            f"{info.positive_calls} positive call(s); per-call: {per_call}\n"
        )
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
        + info_line
    )


def _render_result(state: SessionState, patch: Patch, issues, notes,
                   baseline_match: Optional[MatchResult],
                   candidate_match: Optional[MatchResult],
                   boot: Optional[BootstrapResult],
                   cand_bits: Optional[float],
                   base_bits: Optional[float]) -> str:
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
    if boot is not None:
        lines.append("## Statistical summary")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        for k, v in boot.as_dict().items():
            lines.append(f"| {k} | {v} |")
        if cand_bits is not None and base_bits is not None:
            lines.append(f"| candidate bits_per_call | {cand_bits:.3f} |")
            lines.append(f"| current bits_per_call | {base_bits:.3f} |")
            lines.append(f"| Δ bits_per_call | {cand_bits - base_bits:+.3f} |")
        lines.append("")
    if candidate_match is not None and baseline_match is not None:
        lines.append("## Top boards where the patch helped")
        lines.append("")
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
