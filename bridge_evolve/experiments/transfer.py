"""Teacher / Student transfer experiment.

This is the *critical* experiment from the research brief:

> A new agent instance with no prior interaction history should be able
> to read the generated documents, follow the conventions, and perform
> coherent bidding.

Procedure
---------

1. Teacher = agent loaded from the *teacher* system (e.g. an evolved
   version of ``seed_v0``). The teacher pair plays ``deals`` deals; we
   collect their bidding behaviour.
2. Student = a *freshly constructed* :class:`SystemAgent` loaded from
   the *same teacher system path*. The student pair plays the same
   deals.
3. The experiment succeeds if:

   * the student pair completes coherent auctions (no
     ``ILLEGAL_FORCED_PASS`` diagnoses);
   * its key metrics (avg_score, game_reach_rate) are within
     ``tolerance`` of the teacher's;
   * its auction-by-auction behaviour is reproducible
     (``consistency >= 0.99``) — this is what makes "transfer" real.

The student is constructed from scratch each deal: no memory leaks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..agent import SystemAgent
from ..benchmark import BenchmarkResult, benchmark_metrics, run_benchmark
from ..benchmark.metrics import consistency_score
from ..dsl import load_system


@dataclass
class TransferReport:
    teacher_system: str
    student_system: str
    n_deals: int
    teacher_metrics: Dict[str, object]
    student_metrics: Dict[str, object]
    consistency: float
    success: bool
    notes: List[str] = field(default_factory=list)


def run_transfer_experiment(teacher_path: str, n_deals: int = 200,
                            seed: int = 7777,
                            tolerance: float = 0.05,
                            student_path: Optional[str] = None) -> TransferReport:
    """Run the transfer experiment.

    Parameters
    ----------
    teacher_path : str
        Path to the teacher's bidding system directory.
    student_path : str, optional
        If supplied, the student loads from a *different* path — useful
        for the "send the documents to another machine" scenario.
        Defaults to the same path as the teacher (sanity check that
        construction is stateless).
    """
    student_path = student_path or teacher_path

    # 1) Teacher plays
    teacher_factory = lambda: SystemAgent(load_system(teacher_path))
    teacher_run = run_benchmark(teacher_factory, n_deals=n_deals, seed=seed)

    # 2) Student is constructed fresh on every deal from the document tree.
    student_factory = lambda: SystemAgent(load_system(student_path))
    student_run = run_benchmark(student_factory, n_deals=n_deals, seed=seed)

    # 3) Compare
    consistency = consistency_score(teacher_run.records, student_run.records)
    teacher_score = teacher_run.metrics.avg_score
    student_score = student_run.metrics.avg_score
    denom = max(1.0, abs(teacher_score) + abs(student_score))
    rel_gap = abs(teacher_score - student_score) / denom

    notes: List[str] = []
    notes.append(f"avg_score teacher={teacher_score:.2f}, student={student_score:.2f}")
    notes.append(f"consistency={consistency:.3f}")
    notes.append(f"relative_score_gap={rel_gap:.3f} (tolerance={tolerance})")

    illegal_forced = sum(
        1 for rec in student_run.records for b in rec.bids
        if b.rule_name == "env_correction"
    )
    if illegal_forced > 0:
        notes.append(f"WARN: {illegal_forced} illegal-forced-pass bids by student")

    success = (
        consistency >= 0.99
        and rel_gap <= tolerance
        and illegal_forced == 0
    )

    return TransferReport(
        teacher_system=teacher_path,
        student_system=student_path,
        n_deals=n_deals,
        teacher_metrics=teacher_run.metrics.as_dict(),
        student_metrics=student_run.metrics.as_dict(),
        consistency=consistency,
        success=success,
        notes=notes,
    )
