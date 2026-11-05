"""Command-line entry point. Only implemented commands are registered."""

import argparse
import json
import sys
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

from bridge_evolve import __version__
from bridge_evolve.diagnostics import MINIMUM_PYTHON, EnvironmentReport, collect_diagnostics
from bridge_evolve.systems import Observation, decide, load_system, render_system
from bridge_evolve.systems.inputs import decision_data, parse_board, parse_observation, parse_prefix, run_auction, run_uncontested
from bridge_evolve.systems.language import SystemError, load_json
from bridge_evolve.systems.v2_runtime import public_context


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge-evolve",
        description="Reproducible bridge bidding protocol experiments.",
        epilog=(
            "Agent-guided protocol evolution, real DDS, paired evaluation, executable "
            "v1/v2 systems, general four-player and local 1NT auctions are available."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor", help="Inspect the runtime and real DDS status.")
    doctor.add_argument("--json", action="store_true", help="Emit a versioned JSON report.")
    doctor.add_argument(
        "--require-dds",
        action="store_true",
        help="Exit unsuccessfully unless a verified real DDS adapter is available.",
    )
    system = commands.add_parser("system", help="Validate, render, or execute a bidding system.")
    system_commands = system.add_subparsers(dest="system_command", required=True)
    validate = system_commands.add_parser("validate", help="Validate the v1/v2 IR, types, and references.")
    validate.add_argument("path", type=Path)
    classics = system_commands.add_parser("build-classics", help="Compile explicit CCBA/Precision engineering profiles.")
    classics.add_argument("--output", type=Path, required=True)
    classics.add_argument("--sources", type=Path, help="Directory containing the exact ccba.docx and prec.docx.")
    classics.add_argument("--compact", action="store_true",
                          help="Share repeated expressions and use readable compact JSON; output must not exist.")
    render = system_commands.add_parser("render", help="Generate the complete Chinese specification.")
    render.add_argument("path", type=Path)
    render.add_argument("--output", type=Path)
    decision = system_commands.add_parser("decide", help="Decide from a player-visible observation.")
    decision.add_argument("path", type=Path)
    decision.add_argument("--observation", required=True, type=Path)
    decision.add_argument("--trace", action="store_true", help="Include PRIVATE diagnostic details.")
    context = system_commands.add_parser("context", help="Reconstruct public protocol context without a decision.")
    context.add_argument("path", type=Path)
    context.add_argument("--observation", required=True, type=Path)
    browse = system_commands.add_parser("browse", help="Navigate semantic branches, dependencies, auction meanings, or a hand.")
    browse.add_argument("path", type=Path)
    browse.add_argument("--request", type=Path, help="JSON navigation request; omitted means a small catalog.")
    browse.add_argument("--compare-to", type=Path, help="Explicit baseline for semantic diff.")
    for player_command in (decision, context, browse):
        player_command.add_argument("--public-ns", type=Path, help="Pre-disclosed NS system definition.")
        player_command.add_argument("--public-ew", type=Path, help="Pre-disclosed EW system definition.")
    auction = commands.add_parser("auction", help="Run a specified board through local or general protocols.")
    auctions = auction.add_subparsers(dest="auction_command", required=True)
    run = auctions.add_parser("run", help="Run bidding only; no DDS or score.")
    run.add_argument("--mode", choices=("uncontested-1nt", "general"), default="uncontested-1nt")
    run.add_argument("--system", type=Path, help="System for the local 1NT mode.")
    run.add_argument("--ns", type=Path, help="NS system for general mode.")
    run.add_argument("--ew", type=Path, help="EW system for general mode.")
    run.add_argument("--prefix", type=Path, help="Optional JSON call-list prefix for general mode.")
    run.add_argument("--board", required=True, type=Path)
    run.add_argument("--trace", action="store_true", help="Include PRIVATE diagnostic details.")
    dataset = commands.add_parser("dataset", help="Generate reproducible, unconditional full deals.")
    datasets = dataset.add_subparsers(dest="dataset_command", required=True)
    generate = datasets.add_parser("generate")
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--count", type=int, required=True)
    generate.add_argument("--seed", type=int, required=True)
    generate.add_argument("--split", choices=("train", "validation", "test"), required=True)
    show = datasets.add_parser("show", help="Extract one verified full board for auction replay.")
    show.add_argument("--dataset", type=Path, required=True)
    show.add_argument("--index", type=int, required=True, help="Zero-based board index.")
    show.add_argument("--output", type=Path)
    oracle = commands.add_parser("oracle", help="Precompute real DDS tables.")
    oracles = oracle.add_subparsers(dest="oracle_command", required=True)
    precompute = oracles.add_parser("precompute")
    precompute.add_argument("--dataset", type=Path, required=True)
    precompute.add_argument("--cache", type=Path, required=True)
    evaluate = commands.add_parser("evaluate", help="Run a frozen two-table, real-DDS comparison.")
    evaluate.add_argument("--config", type=Path, required=True)
    records = commands.add_parser("records", help="Inspect or losslessly compact auction/audit records.")
    record_commands = records.add_subparsers(dest="records_command", required=True)
    convert = record_commands.add_parser("convert", help="Write a new compact JSONL; never overwrite the input.")
    convert.add_argument("--input", type=Path, required=True)
    convert.add_argument("--output", type=Path, required=True)
    stats = record_commands.add_parser("stats", help="Summarize records before choosing boards to inspect.")
    stats.add_argument("--input", type=Path, required=True)
    stats.add_argument("--top", type=int, default=20, help="Maximum rule/error groups; zero shows all.")
    stats.add_argument("--pretty", action="store_true")
    show_record = record_commands.add_parser("show", help="Expand one board, summary, or selected decision.")
    show_record.add_argument("--input", type=Path, required=True)
    selection = show_record.add_mutually_exclusive_group(required=True)
    selection.add_argument("--index", type=int, help="Zero-based board index.")
    selection.add_argument("--board", type=int, help="One-based board number.")
    show_record.add_argument("--view", choices=("summary", "full", "decision"), default="summary")
    show_record.add_argument("--table", type=int, choices=(1, 2), default=1)
    show_record.add_argument("--turn", type=int, help="One-based call number for decision view.")
    show_record.add_argument("--pretty", action="store_true")
    evolution = commands.add_parser("evolve", help="Run bounded protocol evolution with fresh random evaluation rounds.")
    evolution_commands = evolution.add_subparsers(dest="evolution_command", required=True)
    for name in ("init", "run"):
        command = evolution_commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--output", type=Path)
        command.add_argument("--profile", choices=("smoke", "pilot", "research"))
        command.add_argument("--source", choices=("research", "agent", "numbers", "replay"))
        command.add_argument("--model", help="Exact authenticated gateway model ID; unavailable models fail without fallback.")
        command.add_argument("--from-research", help="Copy a compatible saved research workspace into a new experiment without copying scores or resetting the old budget.")
        command.add_argument("--from-champion", help="Continue frozen published champions and their regression cases, without copying old scores or budgets.")
    evolution_commands.add_parser("models", help="List actual authenticated local gateway models.")
    for name in ("resume", "status", "cancel", "verify"):
        command = evolution_commands.add_parser(name)
        command.add_argument("--run", type=Path, required=True)
        if name == "resume":
            command.add_argument("--clear-cancel", action="store_true")
        if name == "status":
            command.add_argument("--full", action="store_true", help="Include the complete persisted generation/proposal state.")
    return parser


def _render_report(report: EnvironmentReport) -> str:
    minimum = ".".join(map(str, MINIMUM_PYTHON))
    python_status = "supported" if report.python_supported else "unsupported"
    binding_status = "detected" if report.dds.binding_detected else "not found"
    binding_version = report.dds.binding_version or "unknown"
    dds_status = "available" if report.dds.available else "unavailable"
    return "\n".join(
        (
            f"Bridge Bidding Evolution {report.package_version}",
            f"Python: {report.python_version} ({python_status}; minimum {minimum})",
            f"Executable: {report.python_executable}",
            f"Platform: {report.platform}",
            f"DDS: {dds_status}",
            f"DDS adapter: {report.dds.adapter_status}",
            f"DDS binding: {report.dds.binding_module} ({binding_status}; version {binding_version})",
            f"DDS detail: {report.dds.detail}",
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.print_help()
        return 0
    if arguments.command != "doctor":
        try:
            if arguments.command == "records":
                return _records_command(arguments)
            if arguments.command == "evolve":
                return _evolution_command(arguments)
            if arguments.command in ("dataset", "oracle", "evaluate"):
                return _research_command(arguments)
            return _system_command(arguments)
        except (OSError, ValueError, RuntimeError, RecursionError, sqlite3.Error) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    report = collect_diagnostics()
    if arguments.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print(_render_report(report))

    if not report.python_supported:
        minimum = ".".join(map(str, MINIMUM_PYTHON))
        print(f"error: Python {minimum} or newer is required.", file=sys.stderr)
        return 1
    if arguments.require_dds and not report.dds.available:
        print(f"error: DDS is required but unavailable. {report.dds.detail}", file=sys.stderr)
        return 1
    return 0


def _records_command(arguments: argparse.Namespace) -> int:
    from bridge_evolve.evaluation.record_log import (
        RecordWriter, agent_board, agent_decision, iter_records, record_statistics, select_record,
    )
    if arguments.records_command == "convert":
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        with RecordWriter(arguments.output) as writer:
            for record in iter_records(arguments.input):
                writer.write(record)
        result = {**writer.metadata(), "output": str(arguments.output),
                  "input_bytes": arguments.input.stat().st_size}
    elif arguments.records_command == "stats":
        result = record_statistics(iter_records(arguments.input, projection="index"), top=arguments.top)
    else:
        record = select_record(arguments.input, index=arguments.index, number=arguments.board,
                               table=arguments.table, expand=arguments.view != "summary")
        if arguments.view == "decision":
            if arguments.turn is None:
                raise ValueError("--view decision requires --turn.")
            result = agent_decision(record, arguments.table, arguments.turn)
        else:
            result = agent_board(record) if arguments.view == "summary" else record
    print(json.dumps(result, ensure_ascii=False, indent=2 if getattr(arguments, "pretty", False) else None,
                     separators=None if getattr(arguments, "pretty", False) else (",", ":")))
    return 0


def _evolution_command(arguments: argparse.Namespace) -> int:
    from bridge_evolve.evolution.config import ROOT
    from bridge_evolve.evolution.controller import cancel, initialize, run, status
    from bridge_evolve.evolution.inspection import overview
    action = arguments.evolution_command
    if action == "models":
        from bridge_evolve.evolution.models import available_models
        print(json.dumps({"default": "gpt-6-astra", "models": available_models(ROOT)}, ensure_ascii=False))
        return 0
    if action in ("init", "run"):
        config = load_json(arguments.config)
        if not isinstance(config, dict):
            raise ValueError("Evolution configuration must be a JSON object.")
        if arguments.profile:
            config["profile"] = arguments.profile
        if arguments.source:
            config["source"] = arguments.source
        if arguments.model:
            config["model"] = arguments.model
        if arguments.from_research:
            config["research_seed_run"] = arguments.from_research
        if arguments.from_champion:
            config["champion_seed_run"] = arguments.from_champion
        directory = initialize(config, arguments.output, ROOT)
        print(f"Evolution experiment: {directory}", file=sys.stderr, flush=True)
        if action == "run":
            run(directory)
        result = overview(directory)
    elif action == "cancel":
        result = cancel(arguments.run)
    elif action == "status":
        result = status(arguments.run) if arguments.full else overview(arguments.run)
    else:
        if action == "resume" and arguments.clear_cancel:
            (arguments.run / "cancel-requested").unlink(missing_ok=True)
        run(arguments.run, goal_only=action == "verify")
        result = overview(arguments.run)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return 0 if result["status"] in ("ready", "queued", "running", "completed", "target_met", "cancelled") else 1


def _system_command(arguments: argparse.Namespace) -> int:
    if arguments.command == "system" and arguments.system_command == "build-classics":
        from bridge_evolve.systems.classics import export_classics
        hashes = export_classics(arguments.output, sources=arguments.sources, compact=arguments.compact)
        print(json.dumps({"systems": hashes, "output": str(arguments.output),
                          "scope": "engineering translations; inspect migration.json for manual coverage differences"}))
        return 0
    if arguments.command == "auction":
        board = parse_board(load_json(arguments.board))
        if arguments.mode == "general":
            if arguments.system is not None or arguments.ns is None or arguments.ew is None:
                raise SystemError("General mode requires --ns and --ew, not --system.")
            prefix = parse_prefix(load_json(arguments.prefix)) if arguments.prefix else ()
            output = run_auction(
                load_system(arguments.ns), load_system(arguments.ew), board, prefix=prefix, trace=arguments.trace,
            )
        else:
            if arguments.system is None or arguments.ns or arguments.ew or arguments.prefix:
                raise SystemError("Local 1NT mode requires --system and does not accept --ns/--ew/--prefix.")
            output = run_uncontested(load_system(arguments.system), board, trace=arguments.trace)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    system = load_system(arguments.path)
    if arguments.system_command == "validate":
        output = {
            "valid": True, "schema_version": system.version, "operator_version": system.version,
            "system_hash": system.content_hash, "name": system.name,
            "checks": ["syntax", "types", "feature_cycles", "references", "state_reachability", "identical_rule_conflicts"],
            "limitations": "Other overlapping conditions and illegal actions are detected at runtime; not an exhaustive proof.",
        }
    elif arguments.system_command == "render":
        specification = render_system(system)
        if arguments.output is None:
            print(specification, end="")
        else:
            arguments.output.write_text(specification, encoding="utf8")
            print(json.dumps({"output": str(arguments.output), "system_hash": system.content_hash}))
        return 0
    elif arguments.system_command == "context":
        output = public_context(system, _observation(arguments))
    elif arguments.system_command == "browse":
        from bridge_evolve.systems.navigation import query_system
        request = load_json(arguments.request) if arguments.request else {}
        definitions = {side: load_system(path) for side, path in (("NS", arguments.public_ns), ("EW", arguments.public_ew))
                       if path is not None}
        output = query_system(system, request, definitions=definitions,
                              compare_to=load_system(arguments.compare_to) if arguments.compare_to else None)
    else:
        observation = _observation(arguments)
        output = decision_data(system, observation, decide(system, observation), arguments.trace)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def _observation(arguments: argparse.Namespace) -> Observation:
    observation = parse_observation(load_json(arguments.observation))
    definitions = dict(observation.public_definitions)
    for side, path in (("NS", arguments.public_ns), ("EW", arguments.public_ew)):
        if path is not None:
            definition = load_system(path)
            if side in definitions and definitions[side].content_hash != definition.content_hash:
                raise SystemError(f"Conflicting public {side} definitions in the observation and CLI.")
            definitions[side] = definition
    return replace(observation, public_definitions=tuple(definitions.items()))


def _research_command(arguments: argparse.Namespace) -> int:
    if arguments.command == "dataset":
        from bridge_evolve.evaluation.data import board_data, generate_dataset, load_dataset
        if arguments.dataset_command == "generate":
            manifest = generate_dataset(arguments.output, count=arguments.count, seed=arguments.seed, split=arguments.split)
            result = {"manifest": str(manifest), "count": arguments.count}
        else:
            _, records = load_dataset(arguments.dataset)
            if not 0 <= arguments.index < len(records):
                raise ValueError("Board index is outside the dataset.")
            result = board_data(records[arguments.index].board)
            if arguments.output is not None:
                arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
                result = {"output": str(arguments.output), "index": arguments.index}
    elif arguments.command == "oracle":
        from bridge_evolve.evaluation.precompute import precompute
        result = precompute(arguments.dataset, arguments.cache)
    else:
        from bridge_evolve.evaluation.experiment import evaluate
        report = evaluate(arguments.config)
        result = {"report": str(report), "status": "completed"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
