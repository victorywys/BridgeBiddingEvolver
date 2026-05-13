"""Self-play evolution loop.

Each iteration:

1. Play ``deals_per_iter`` deals with NS = teacher pair, EW = passive.
2. Run :func:`analyze_auction` on each record.
3. Ask the reflector (heuristic or LLM-backed) for candidate patches.
4. For each patch, apply on a copy, benchmark on a held-out validation
   set, and only commit if the patch improves the configured target
   metric.
5. Commit successful patches into the :class:`SystemRepository` and
   advance HEAD.

This produces a fully versioned trace of the bidding system's evolution
on disk. Nothing is kept in memory between iterations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..agent import SystemAgent
from ..benchmark import BenchmarkResult, run_benchmark
from ..dsl import BiddingSystem
from ..reflection import (
    HeuristicReflector,
    LLMReflector,
    Patch,
    analyze_auction,
)
from ..versioning import SystemRepository


@dataclass
class EvolutionConfig:
    iterations: int = 5
    deals_per_iter: int = 100
    val_deals: int = 80
    train_seed: int = 1000
    val_seed: int = 9000
    target_metric: str = "avg_score"   # benchmark field
    higher_is_better: bool = True
    min_delta: float = 0.0


@dataclass
class EvolutionStep:
    iteration: int
    parent_version: str
    new_version: Optional[str]
    accepted_patch: Optional[str]
    proposed_patches: List[str]
    train_metrics: Dict[str, object]
    val_metrics: Optional[Dict[str, object]]
    diagnosis_counts: Dict[str, int]
    notes: str = ""


def _metric(m: BenchmarkResult, key: str) -> float:
    return float(getattr(m, key))


def run_evolution(repo: SystemRepository, config: EvolutionConfig,
                  reflector=None) -> List[EvolutionStep]:
    if reflector is None:
        reflector = HeuristicReflector()
    history: List[EvolutionStep] = []
    for it in range(config.iterations):
        parent_version = repo.head
        base_system = repo.load(parent_version)

        # 1. Self-play
        run = run_benchmark(
            lambda s=base_system: SystemAgent(s),
            n_deals=config.deals_per_iter,
            seed=config.train_seed + it,
        )
        diags = []
        for rec in run.records:
            diags.extend([(rec, analyze_auction(rec))])
        diag_counts: Dict[str, int] = {}
        for _, ds in diags:
            for d in ds:
                diag_counts[d.kind.value] = diag_counts.get(d.kind.value, 0) + 1

        # 2. Propose patches
        patches: List[Patch] = reflector.reflect(base_system, diags)

        accepted: Optional[Patch] = None
        accepted_metrics: Optional[BenchmarkResult] = None
        baseline_metrics = run.metrics
        baseline_value = _metric(baseline_metrics, config.target_metric)
        notes_parts: List[str] = [
            f"baseline {config.target_metric}={baseline_value:.2f}"
        ]

        for patch in patches:
            # apply and validate
            from ..reflection.patch import apply_patch
            candidate = apply_patch(base_system, patch)
            cand_run = run_benchmark(
                lambda s=candidate: SystemAgent(s),
                n_deals=config.val_deals,
                seed=config.val_seed + it,
            )
            cand_value = _metric(cand_run.metrics, config.target_metric)
            improved = (
                cand_value - baseline_value > config.min_delta
                if config.higher_is_better
                else baseline_value - cand_value > config.min_delta
            )
            notes_parts.append(
                f"patch '{patch.name}': {config.target_metric}={cand_value:.2f} "
                f"({'+' if improved else '-'})"
            )
            if improved:
                accepted = patch
                accepted_metrics = cand_run.metrics
                break

        # 3. Commit if accepted
        new_version: Optional[str] = None
        val_metrics_dict: Optional[Dict[str, object]] = None
        if accepted is not None:
            new_version = _next_version(parent_version)
            repo.commit_patch(accepted, new_version=new_version,
                              parent=parent_version,
                              metrics=accepted_metrics.as_dict() if accepted_metrics else None)
            val_metrics_dict = accepted_metrics.as_dict() if accepted_metrics else None
            # also persist a reflection note inside the new version
            _write_reflection_note(repo, new_version, diag_counts, accepted)

        history.append(EvolutionStep(
            iteration=it,
            parent_version=parent_version,
            new_version=new_version,
            accepted_patch=accepted.name if accepted else None,
            proposed_patches=[p.name for p in patches],
            train_metrics=baseline_metrics.as_dict(),
            val_metrics=val_metrics_dict,
            diagnosis_counts=diag_counts,
            notes="; ".join(notes_parts),
        ))
    return history


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _next_version(parent: str) -> str:
    """Generate a fresh version label from the previous one."""
    if parent.startswith("v") and parent[1:].isdigit():
        return f"v{int(parent[1:]) + 1}"
    return parent + "_x"


def _write_reflection_note(repo: SystemRepository, version: str,
                           diag_counts: Dict[str, int], patch: Patch) -> None:
    path = repo.path_for(version) / "reflections"
    path.mkdir(parents=True, exist_ok=True)
    body = [
        f"# Reflection for {version}",
        "",
        "## Diagnosis counts",
        "",
    ]
    for k, v in sorted(diag_counts.items()):
        body.append(f"- {k}: {v}")
    body.extend([
        "",
        f"## Accepted patch: {patch.name}",
        "",
        patch.rationale,
        "",
        "```yaml",
        patch.to_yaml(),
        "```",
    ])
    (path / f"{patch.name}.md").write_text("\n".join(body), encoding="utf-8")
