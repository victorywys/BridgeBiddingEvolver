from pathlib import Path

from bridge_evolve.experiments import EvolutionConfig, run_evolution
from bridge_evolve.versioning import SystemRepository

SEED_PATH = Path(__file__).resolve().parent.parent / "systems" / "seed_v0"


def test_evolution_loop_runs(tmp_path):
    repo = SystemRepository.init_from(SEED_PATH, tmp_path / "repo")
    cfg = EvolutionConfig(iterations=2, deals_per_iter=20, val_deals=15)
    history = run_evolution(repo, cfg)
    assert len(history) == 2
    # HEAD must point at a valid version
    assert (repo.path_for(repo.head) / "meta.yaml").exists()
    # Reproducibility: re-load the system from HEAD; same name.
    s = repo.load()
    assert s.opening_table
