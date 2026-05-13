from pathlib import Path

from bridge_evolve.agent import SystemAgent
from bridge_evolve.benchmark import run_benchmark
from bridge_evolve.dsl import load_system

SEED_PATH = Path(__file__).resolve().parent.parent / "systems" / "seed_v0"


def test_seed_system_loads():
    sys = load_system(SEED_PATH)
    assert sys.opening_table, "should have opening rules"
    names = [c.name for c in sys.conventions]
    assert "Stayman" in names


def test_benchmark_seed_runs():
    factory = lambda: SystemAgent(load_system(SEED_PATH))
    run = run_benchmark(factory, n_deals=20, seed=123)
    m = run.metrics.as_dict()
    assert m["n_deals"] == 20
    # We should be opening at least some hands.
    assert m["passout_rate"] < 1.0
