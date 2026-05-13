"""Command-line entrypoint.

Subcommands:

* ``benchmark``      — run a batch of deals with a given system and print metrics.
* ``play``           — play one deal verbosely and print the auction + meanings.
* ``evolve``         — run the self-evolution loop (heuristic reflector).
* ``transfer``       — run the teacher/student transfer experiment.
* ``inspect``        — print structural information about a system or repo.
* ``match``          — IMP match between two systems (DDS-scored).
* ``precompute-dds`` — fill the DDS cache for a deal seed range.
* ``session open``   — start a new evolution session against HEAD; writes
                       ``report.md`` for the proposer.
* ``session evaluate`` — validate the proposer's ``patch.yaml``, score it,
                       accept or reject.
* ``session show``   — print a session's STATE and paths.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional

from .agent import SystemAgent
from .benchmark import run_benchmark, run_imp_match
from .dsl import load_system, validate_system, format_issues, has_errors
from .env.deal import deal_random
from .env.trick_estimator import (
    DDSEstimator,
    HeuristicEstimator,
    get_default_estimator,
    set_default_estimator,
)
from .experiments import (
    EvolutionConfig,
    SessionConfig,
    evaluate_session,
    open_session,
    run_evolution,
    run_transfer_experiment,
)
from .versioning import SystemRepository, diff_systems


def _select_estimator(args: argparse.Namespace):
    if getattr(args, "estimator", "auto") == "heuristic":
        est = HeuristicEstimator()
        set_default_estimator(est)
        return est
    return get_default_estimator(getattr(args, "dds_cache", None))


def _cmd_benchmark(args: argparse.Namespace) -> int:
    _select_estimator(args)
    factory = lambda: SystemAgent(load_system(args.system))
    run = run_benchmark(factory, n_deals=args.deals, seed=args.seed)
    print(json.dumps({"system": args.system, "metrics": run.metrics.as_dict()}, indent=2))
    return 0


def _cmd_play(args: argparse.Namespace) -> int:
    _select_estimator(args)
    system = load_system(args.system)
    agents: Dict[str, SystemAgent] = {s: SystemAgent(system) for s in "NESW"}
    deal = deal_random(seed=args.seed)
    from .agent.base import run_auction
    rec = run_auction(deal, agents)
    print(f"# Deal seed={args.seed}")
    for s in "NESW":
        print(f"  {s}: {deal.hand(s).pretty()}")
    print()
    print("# Auction")
    for b in rec.bids:
        print(f"  {b.seat} {b.token:>3} [{b.rule_type}/{b.rule_name}] {b.rationale}")
    from .env.scoring import evaluate_auction
    res = evaluate_auction(deal, rec.auction)
    print()
    print("# Result")
    if res.contract is None:
        print("  passed out")
    else:
        print(f"  contract={res.contract} declarer={res.declarer} "
              f"tricks={res.tricks_taken}/{res.tricks_needed} made={res.made} score={res.score} par={res.par_score}")
    return 0


def _cmd_evolve(args: argparse.Namespace) -> int:
    _select_estimator(args)
    repo_path = Path(args.repo or f"repos/{Path(args.system).name}")
    if not (repo_path / "HEAD").exists():
        repo = SystemRepository.init_from(args.system, repo_path)
    else:
        repo = SystemRepository(repo_path)
    cfg = EvolutionConfig(
        iterations=args.iterations,
        deals_per_iter=args.deals,
        val_deals=args.val_deals,
    )
    history = run_evolution(repo, cfg)
    summary = [
        {
            "iteration": h.iteration,
            "parent": h.parent_version,
            "new": h.new_version,
            "patch": h.accepted_patch,
            "proposed": h.proposed_patches,
            "train": h.train_metrics,
            "val": h.val_metrics,
            "diagnoses": h.diagnosis_counts,
            "notes": h.notes,
        } for h in history
    ]
    print(json.dumps({"head": repo.head, "history": summary}, indent=2))
    return 0


def _cmd_transfer(args: argparse.Namespace) -> int:
    _select_estimator(args)
    report = run_transfer_experiment(args.teacher, n_deals=args.deals,
                                     seed=args.seed,
                                     student_path=args.student or None,
                                     tolerance=args.tolerance)
    print(json.dumps({
        "teacher": report.teacher_system,
        "student": report.student_system,
        "n_deals": report.n_deals,
        "consistency": report.consistency,
        "success": report.success,
        "teacher_metrics": report.teacher_metrics,
        "student_metrics": report.student_metrics,
        "notes": report.notes,
    }, indent=2))
    return 0 if report.success else 1


def _cmd_inspect(args: argparse.Namespace) -> int:
    s = load_system(args.system)
    out = {
        "name": s.name,
        "version": s.version,
        "openings": [r.name for r in s.opening_table],
        "conventions": [c.name for c in s.conventions],
        "principles": [p.name for p in s.principles],
        "validation": [
            {"severity": i.severity, "code": i.code, "target": i.target,
             "message": i.message}
            for i in validate_system(s)
        ],
    }
    if args.compare:
        b = load_system(args.compare)
        out["diff"] = diff_systems(s, b)
    print(json.dumps(out, indent=2))
    return 0


def _cmd_match(args: argparse.Namespace) -> int:
    est = _select_estimator(args)
    a = load_system(args.a)
    b = load_system(args.b)
    m = run_imp_match(
        a_factory=lambda: SystemAgent(a),
        b_factory=lambda: SystemAgent(b),
        n_boards=args.boards,
        seed=args.seed,
        estimator=est,
    )
    out = m.summary()
    if isinstance(est, DDSEstimator):
        est.save()
    print(json.dumps(out, indent=2))
    return 0


def _cmd_precompute_dds(args: argparse.Namespace) -> int:
    est = get_default_estimator(args.dds_cache)
    if not isinstance(est, DDSEstimator):
        print("DDS is unavailable; cannot precompute.", file=sys.stderr)
        return 2
    n = args.count
    deals = [deal_random(seed=args.start + i, dealer="N") for i in range(n)]
    import time
    t0 = time.perf_counter()
    for i, d in enumerate(deals, 1):
        est.table(d)
        if i % 50 == 0:
            print(f"  {i}/{n} cached ({(time.perf_counter()-t0)/i*1000:.1f} ms/deal)")
    est.save()
    print(f"DDS cache saved with {est.size} entries at {args.dds_cache}")
    return 0


def _cmd_session_open(args: argparse.Namespace) -> int:
    est = _select_estimator(args)
    repo_path = Path(args.repo)
    if not (repo_path / "HEAD").exists():
        if not args.init_from:
            print(f"repo {repo_path} not initialized; pass --init-from <system>",
                  file=sys.stderr)
            return 2
        repo = SystemRepository.init_from(args.init_from, repo_path)
    else:
        repo = SystemRepository(repo_path)
    cfg = SessionConfig(
        eval_boards=args.boards,
        eval_seed=args.seed,
        baseline_path=args.baseline,
        target_imps_delta=args.target_delta,
        top_failures=args.top_failures,
    )
    session_dir = open_session(repo, cfg, estimator=est)
    if isinstance(est, DDSEstimator):
        est.save()
    print(json.dumps({
        "session_dir": str(session_dir),
        "report": str(session_dir / "report.md"),
        "patch_target": str(session_dir / "patch.yaml"),
    }, indent=2))
    return 0


def _cmd_session_evaluate(args: argparse.Namespace) -> int:
    est = _select_estimator(args)
    repo = SystemRepository(args.repo)
    result_path = evaluate_session(repo, args.id, estimator=est)
    if isinstance(est, DDSEstimator):
        est.save()
    print(json.dumps({"result": str(result_path), "head": repo.head}, indent=2))
    return 0


def _cmd_session_show(args: argparse.Namespace) -> int:
    repo = SystemRepository(args.repo)
    session_dir = Path(repo.root) / "sessions" / args.id
    if not session_dir.exists():
        print(f"no such session: {session_dir}", file=sys.stderr)
        return 2
    files = sorted(p.name for p in session_dir.iterdir())
    print(json.dumps({"dir": str(session_dir), "files": files}, indent=2))
    return 0


def _add_estimator_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--estimator", choices=("auto", "heuristic", "dds"),
                   default="auto")
    p.add_argument("--dds-cache", dest="dds_cache", default=None,
                   help="path to the DDS pickle cache (defaults to "
                        "$BRIDGE_EVOLVE_DDS_CACHE or ~/.cache/...)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser("bridge_evolve")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("benchmark")
    pb.add_argument("--system", required=True)
    pb.add_argument("--deals", type=int, default=200)
    pb.add_argument("--seed", type=int, default=1234)
    _add_estimator_flags(pb)
    pb.set_defaults(fn=_cmd_benchmark)

    pp = sub.add_parser("play")
    pp.add_argument("--system", required=True)
    pp.add_argument("--seed", type=int, default=42)
    _add_estimator_flags(pp)
    pp.set_defaults(fn=_cmd_play)

    pe = sub.add_parser("evolve")
    pe.add_argument("--system", required=True)
    pe.add_argument("--repo")
    pe.add_argument("--iterations", type=int, default=5)
    pe.add_argument("--deals", type=int, default=100)
    pe.add_argument("--val-deals", dest="val_deals", type=int, default=80)
    _add_estimator_flags(pe)
    pe.set_defaults(fn=_cmd_evolve)

    pt = sub.add_parser("transfer")
    pt.add_argument("--teacher", required=True)
    pt.add_argument("--student", default=None)
    pt.add_argument("--deals", type=int, default=200)
    pt.add_argument("--seed", type=int, default=7777)
    pt.add_argument("--tolerance", type=float, default=0.05)
    _add_estimator_flags(pt)
    pt.set_defaults(fn=_cmd_transfer)

    pi = sub.add_parser("inspect")
    pi.add_argument("--system", required=True)
    pi.add_argument("--compare", default=None)
    pi.set_defaults(fn=_cmd_inspect)

    pm = sub.add_parser("match")
    pm.add_argument("--a", required=True, help="system A (candidate)")
    pm.add_argument("--b", required=True, help="system B (baseline)")
    pm.add_argument("--boards", type=int, default=200)
    pm.add_argument("--seed", type=int, default=20260101)
    _add_estimator_flags(pm)
    pm.set_defaults(fn=_cmd_match)

    pc = sub.add_parser("precompute-dds")
    pc.add_argument("--start", type=int, default=20260101)
    pc.add_argument("--count", type=int, default=400)
    pc.add_argument("--dds-cache", dest="dds_cache", default=None)
    pc.set_defaults(fn=_cmd_precompute_dds)

    # session subgroup
    ps = sub.add_parser("session")
    ps_sub = ps.add_subparsers(dest="session_cmd", required=True)

    pso = ps_sub.add_parser("open")
    pso.add_argument("--repo", required=True)
    pso.add_argument("--init-from", dest="init_from", default=None,
                     help="initialize repo from this system path if it doesn't exist")
    pso.add_argument("--boards", type=int, default=200)
    pso.add_argument("--seed", type=int, default=20260101)
    pso.add_argument("--baseline", default="systems/baseline_sayc")
    pso.add_argument("--target-delta", dest="target_delta", type=float, default=0.05)
    pso.add_argument("--top-failures", dest="top_failures", type=int, default=8)
    _add_estimator_flags(pso)
    pso.set_defaults(fn=_cmd_session_open)

    pse = ps_sub.add_parser("evaluate")
    pse.add_argument("--repo", required=True)
    pse.add_argument("--id", required=True)
    _add_estimator_flags(pse)
    pse.set_defaults(fn=_cmd_session_evaluate)

    pss = ps_sub.add_parser("show")
    pss.add_argument("--repo", required=True)
    pss.add_argument("--id", required=True)
    pss.set_defaults(fn=_cmd_session_show)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
