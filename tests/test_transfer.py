from pathlib import Path

from bridge_evolve.experiments import run_transfer_experiment

SEED_PATH = Path(__file__).resolve().parent.parent / "systems" / "seed_v0"


def test_transfer_is_reproducible():
    report = run_transfer_experiment(str(SEED_PATH), n_deals=20, seed=99)
    # Two fresh agents constructed from the same documents must produce
    # identical auctions: this is the externalized-knowledge guarantee.
    assert report.consistency == 1.0
    assert report.teacher_metrics["avg_score"] == report.student_metrics["avg_score"]
