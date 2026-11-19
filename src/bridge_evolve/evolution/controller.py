"""Persistent, bounded protocol evolution with fresh evaluation rounds."""

from __future__ import annotations

import fcntl
import copy
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.systems import render_system
from bridge_evolve.evolution.config import ROOT, blank_system, engine_identity, normalize_config, save_json
from bridge_evolve.evolution.execution import Evaluator, EvolutionStopped, column_summary, comparison_changes, frozen_system, paired_gain
from bridge_evolve.evolution.proposals import LocalAgent, agent_packet, apply_proposal, evidence_packet, numeric_proposals
from bridge_evolve.evolution.models import ModelClient
from bridge_evolve.evolution.handoff import EngineRevisionReady


class EvaluationBlocked(RuntimeError):
    pass


def register_system(directory: Path, state: dict, system) -> str:
    identity = system.content_hash
    path = directory / "systems" / f"{identity}.json"
    raw = system.to_data()
    contents = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    specification = render_system(system)
    for target, text in ((path, contents), (path.with_suffix(".md"), specification)):
        encoded = text.encode("utf8")
        key = target.relative_to(directory).as_posix()
        if target.exists():
            if target.read_bytes() != encoded:
                raise ValueError("Frozen evolution IR/specification changed.")
        else:
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(encoded)
            temporary.replace(target)
        digest = hashlib.sha256(encoded).hexdigest()
        if key in state["system_files"] and state["system_files"][key] != digest:
            raise ValueError("Frozen evolution input changed.")
        state["system_files"][key] = digest
    state.setdefault("system_metadata", {}).setdefault(identity, {
        "hash": identity, "name": system.name, "rules": len(raw["rules"]), "features": len(raw["features"]),
        "states": len(raw["protocol"]["states"]), "registered_at": timestamp(),
    })
    return identity


def frozen_workspace(directory: Path, state: dict, workspace: dict) -> dict:
    selected = copy.deepcopy({key: workspace[key] for key in
        ("system", "tests", "cases", "notes", "last_tests", "last_trial", "prior_parent_evidence",
         "research_memory", "implementation_state", "last_development_evaluation", "best_solution",
         "provisional_proposals") if key in workspace})
    from bridge_evolve.evolution.memory import raw_read
    for operation in list(workspace.get("operations", {}).values())[-32:]:
        if operation["name"] != "read_system" or operation["status"] != "completed":
            continue
        saved = raw_read(selected["system"], operation["arguments"], operation.get("result", {}))
        if saved:
            memory = selected.setdefault("research_memory", {"nodes": {}, "seen": [], "no_information_streak": 0})
            memory.setdefault("raw_reads", {})[canonical_hash(operation["arguments"])] = saved
    results = copy.deepcopy(workspace.get("feedback_results", {}))
    pending = []

    def references(value):
        if isinstance(value, dict):
            reference = value.get("details_ref")
            if isinstance(reference, dict) and isinstance(reference.get("result_id"), str):
                pending.append(reference["result_id"])
            for key, child in value.items():
                if key not in ("actual", "expected", "system", "cases", "tests", "test_definitions"):
                    references(child)
        elif isinstance(value, list):
            for child in value:
                references(child)

    for key in ("last_tests", "last_trial", "notes", "prior_parent_evidence", "last_development_evaluation", "research_memory"):
        references(selected.get(key))
    while pending:
        identity = pending.pop()
        if identity in results:
            continue
        metadata = workspace.get("full_results", {}).get(identity)
        if metadata is None:
            raise ValueError("A saved feedback summary has no approved full-result source.")
        name = metadata["path"]
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or state["artifacts"].get(name) != metadata["sha256"]:
            raise ValueError("Saved feedback result is outside its frozen research provenance.")
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != metadata["sha256"]:
            raise ValueError("Saved feedback result hash mismatch.")
        value = json.loads(contents)
        if canonical_hash(value) != identity:
            raise ValueError("Saved feedback result content identity mismatch.")
        results[identity] = value
        references(value)
    if results:
        selected["feedback_results"] = results
    return selected


def import_research_branches(source, previous, directory, state):
    from bridge_evolve.evolution.inspection import checked_artifact
    from bridge_evolve.systems import validate_system
    sources = {**previous.get("research_branches", {}), **previous.get("research_sessions", {})}
    mapping = state.setdefault("research_branch_imports", {}).setdefault(previous["id"], {})
    for identity, metadata in sources.items():
        lineage = metadata["lineage"]
        if lineage not in state["champions"]:
            continue
        original = checked_artifact(source, metadata["workspace"])
        workspace = frozen_workspace(source, previous, original)
        publications = original.get("releases")
        if publications is None:
            publications = [{"hash": row["hash"], "path": row["release"]}
                            for row in previous.get("candidate_archive", {}).values()
                            if row.get("release") and Path(row["release"]).parent == Path(metadata["workspace"]).parent]
        workspace["releases"] = copy.deepcopy(publications)
        if metadata["parent"] != state["champions"][lineage]:
            for test in workspace["tests"]:
                test.pop("changed_from_parent", None)
        workspace["prior_parent_evidence"] = {
            "source_run": previous["id"], "source_session": identity, "source_parent": metadata["parent"],
            **{key: workspace.pop(key) for key in ("last_tests", "last_trial", "last_development_evaluation") if key in workspace},
            "scope": "Historical development evidence only; not this run's tests or fitness."}
        branch_id = mapping.get(identity, identity if identity in previous.get("research_branches", {}) else "imported-" + identity)
        if identity not in mapping and branch_id in state.get("research_branches", {}):
            branch_id = "imported-" + previous["id"].removeprefix("evo-") + "-" + identity
        mapping[identity] = branch_id
        path = directory / "branches" / branch_id / "workspace.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        save_json(path, workspace)
        name = path.relative_to(directory).as_posix()
        state["artifacts"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        state.setdefault("research_branches", {})[branch_id] = {
            "id": branch_id, "lineage": lineage, "parent": state["champions"][lineage],
            "source_parent": metadata["parent"], "source_run": previous["id"], "source_session": identity,
            "workspace": name, "hash": canonical_hash(workspace["system"]),
            "title": metadata.get("assignment", {}).get("title", workspace["system"]["name"]),
            "status": "retained_candidate" if publications else "retained_draft"}
        for index, published in enumerate(publications):
            release_path = published["path"]
            if release_path not in previous["artifacts"]:
                release_path = previous["candidate_archive"][published["hash"]]["release"]
            release = checked_artifact(source, release_path)
            system = validate_system(workspace["system"]) if published["hash"] == canonical_hash(
                workspace["system"]) else None
            if system is None:
                contents = (source / f"systems/{published['hash']}.json").read_bytes()
                if hashlib.sha256(contents).hexdigest() != previous["system_files"].get(f"systems/{published['hash']}.json"):
                    raise ValueError("An imported portfolio candidate changed from its frozen identity.")
                system = validate_system(json.loads(contents))
            register_system(directory, state, system)
            parent_contents = (source / f"systems/{release['parent']}.json").read_bytes()
            if hashlib.sha256(parent_contents).hexdigest() != previous["system_files"].get(f"systems/{release['parent']}.json"):
                raise ValueError("An imported candidate's design parent changed.")
            register_system(directory, state, validate_system(json.loads(parent_contents)))
            release["historical_provenance"] = {"run": previous["id"], "release": published["path"]}
            if "research_resources_at_publication" in release:
                release["historical_provenance"]["resources"] = release.pop("research_resources_at_publication")
            target = path.parent / f"release-{index + 1}.json"
            save_json(target, release)
            release_name = target.relative_to(directory).as_posix()
            state["artifacts"][release_name] = hashlib.sha256(target.read_bytes()).hexdigest()
            workspace["releases"][index]["path"] = release_name
            state["candidate_archive"][system.content_hash] = {
                "hash": system.content_hash, "parent": release["parent"], "lineage": lineage,
                "release": release_name, "hypothesis": release["hypothesis"],
                "historical_provenance": release["historical_provenance"], "status": "retained"}
        save_json(path, workspace)
        state["artifacts"][name] = hashlib.sha256(path.read_bytes()).hexdigest()


def initialize(config: dict, output: Path | None = None, project: Path = ROOT, *, preloaded: dict | None = None) -> Path:
    from bridge_evolve.systems import load_system, validate_system
    normalized = normalize_config(config, project, preloaded=preloaded)
    directory = (output or project / ".runtime/evolution" / ("evo-" + uuid.uuid4().hex)).resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    (directory / "systems").mkdir()
    (directory / "analysis").mkdir()
    (directory / "proposals").mkdir()
    state = {"schema": "bridge-evolution-run/v1", "id": directory.name, "name": normalized["name"],
             "status": "ready", "created_at": timestamp(), "updated_at": timestamp(), "engine_hash": engine_identity(),
             "config_hash": canonical_hash(normalized), "generation": 0, "phase": "queued",
             "champions": {}, "system_files": {}, "rounds": {}, "generations": {},
             "artifacts": {}, "recoveries": [],
             "evaluated_pairs": 0, "agent_requests": 0, "promotion_attempts": 0, "goal_checks": 0,
             "spent_seconds": 0, "plateau_generations": 0, "goal": {"status": "not_checked"},
             "research_sessions": {}, "candidate_archive": {}, "milestones": {}}
    for label, baseline in zip(("A", "B"), normalized["baselines"], strict=True):
        path = Path(baseline["path"])
        system = preloaded[path] if preloaded is not None and path in preloaded else load_system(path)
        identity = register_system(directory, state, system)
        if "existing" in normalized["origins"]:
            state["champions"][label] = identity
    if "blank" in normalized["origins"]:
        state["champions"]["Z"] = register_system(directory, state, blank_system())
    if normalized["module_contract"] is not None:
        contract = normalized["module_contract"]
        anchor = load_system(Path(contract["anchor_path"]))
        if anchor.content_hash != contract["definition"]["anchor_hash"]:
            raise ValueError("Controlled research anchor identity changed before initialization.")
        state["champions"]["Z"] = register_system(directory, state, anchor)
        target = directory / "module-contract.json"
        save_json(target, contract["definition"])
        state["system_files"]["module-contract.json"] = hashlib.sha256(target.read_bytes()).hexdigest()
        state["module_contract"] = {"path": "module-contract.json", "hash": canonical_hash(contract["definition"]),
                                    "anchor": anchor.content_hash}
    state["origin_hashes"] = dict(state["champions"])
    seed_source = normalized["research_seed_run"] or normalized["champion_seed_run"]
    if seed_source is not None:
        from bridge_evolve.evolution.inspection import checked_artifact
        source = Path(seed_source)
        previous = status(source)
        previous_config = json.loads((source / "config.json").read_text())
        if canonical_hash(previous_config) != previous["config_hash"]:
            raise ValueError("Source research configuration identity mismatch.")
        if previous["status"] in ("queued", "running"):
            raise ValueError("Finish or cancel the source research before starting a continuation.")
        changed_benchmarks = ([value["hash"] for value in previous_config["baselines"]]
                              != [value["hash"] for value in normalized["baselines"]])
        if changed_benchmarks:
            if normalized["benchmark_revision_reason"] is None:
                raise ValueError("Research continuation must retain both original benchmark identities unless an explicit benchmark_revision_reason starts a separately identified experiment.")
            state["benchmark_revision"] = {
                "source_run": previous["id"], "reason": normalized["benchmark_revision_reason"],
                "previous": previous_config["baselines"], "current": normalized["baselines"],
                "historical_comparison_issues": previous.get("comparison_issues", {}),
            }
        state["research_seeds"] = {}
        for label, parent in state["champions"].items():
            source_parent = previous["champions"].get(label)
            if source_parent is None:
                continue
            if normalized["research_seed_run"] is not None:
                old_anchor = next((value["hash"] for key, value in zip(("A", "B"), previous_config["baselines"], strict=True)
                                   if key == label), None)
                if source_parent != old_anchor:
                    name = f"systems/{source_parent}.json"
                    contents = (source / name).read_bytes()
                    if hashlib.sha256(contents).hexdigest() != previous["system_files"].get(name):
                        raise ValueError("Source research parent differs from its frozen identity.")
                    system = load_system(source / name)
                    if system.content_hash != source_parent:
                        raise ValueError("Source research parent content hash mismatch.")
                    identity = register_system(directory, state, system)
                    if normalized["research_seed_parent"] == "source":
                        parent = identity
                        state["champions"][label] = parent
            if normalized["champion_seed_run"] is not None:
                identity = previous["champions"].get(label)
                if identity is None:
                    continue
                name = f"systems/{identity}.json"
                contents = (source / name).read_bytes()
                if hashlib.sha256(contents).hexdigest() != previous["system_files"].get(name):
                    raise ValueError("The source champion file differs from its frozen identity.")
                champion = load_system(source / name)
                if champion.content_hash != identity:
                    raise ValueError("The source champion content hash is inconsistent.")
                goal_validation = None
                if normalized.get("protocol_goal") is not None:
                    from bridge_evolve.evolution.goals import protocol_goal
                    goal_validation = protocol_goal(normalized["protocol_goal"]).check(champion)
                    if goal_validation["status"] != "passed":
                        raise ValueError("The published continuation parent does not satisfy the protocol goal: "
                                         + json.dumps(goal_validation["failures"][:2], ensure_ascii=False))
                if normalized.get("module_contract") is not None:
                    from bridge_evolve.evolution.contracts import ModuleContract
                    contract = normalized["module_contract"]
                    validation = ModuleContract(contract["definition"], load_system(Path(contract["anchor_path"]))).check(champion)
                    if validation["status"] != "passed":
                        raise ValueError("The continuation parent violates the frozen module contract.")
                parent = register_system(directory, state, champion)
                state["champions"][label] = parent
                release_entry = next((entry for generation in reversed(list(previous["generations"].values()))
                                      for entry in generation["candidates"].values()
                                      if entry.get("hash") == identity and entry.get("release")), None)
                selected = {"system": champion.to_data(), "tests": [], "cases": {}, "notes": []}
                if release_entry is not None:
                    release = checked_artifact(source, release_entry["release"])
                    if release["hash"] != identity:
                        raise ValueError("Source release and champion do not match.")
                    selected.update(tests=copy.deepcopy(release["test_definitions"]),
                                    cases=copy.deepcopy(release["cases"]))
                    for test in selected["tests"]:
                        test.pop("changed_from_parent", None)
                    selected["notes"] = [{"kind": "research", "text": release["hypothesis"]}]
                else:
                    prior_seed = previous.get("research_seeds", {}).get(label)
                    if prior_seed is not None and prior_seed.get("parent") == identity:
                        seed_bytes = (source / prior_seed["path"]).read_bytes()
                        if hashlib.sha256(seed_bytes).hexdigest() != previous["system_files"].get(prior_seed["path"]):
                            raise ValueError("The inherited source regression seed differs from its frozen identity.")
                        inherited = json.loads(seed_bytes)["workspace"]
                        selected.update({key: copy.deepcopy(inherited[key]) for key in ("tests", "cases", "notes")})
                seed = {"workspace": selected, "source_run": previous["id"], "source_session": release_entry.get("research_session") if release_entry else None,
                        "source_engine": previous["engine_hash"], "source_model": previous_config["model"],
                        "source_system_sha256": previous["system_files"][name], "parent": parent,
                        "kind": "champion"}
                if goal_validation is not None:
                    seed["goal_validation"] = goal_validation
                target = directory / f"research-seed-{label}.json"
                save_json(target, seed)
                name = target.relative_to(directory).as_posix()
                state["system_files"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
                state["research_seeds"][label] = {"path": name, **{key: seed[key] for key in seed if key != "workspace"}}
                continue
            sessions = [(key, value) for key, value in previous.get("research_sessions", {}).items()
                        if value["lineage"] == label and value["parent"] == source_parent]
            if not sessions:
                continue
            identity, metadata = sessions[-1]
            workspace = checked_artifact(source, metadata["workspace"])
            selected = frozen_workspace(source, previous, workspace)
            if (normalized["research_seed_parent"] == "origin"
                    and validate_system(selected["system"]).content_hash != parent):
                raise ValueError("The saved draft must match the configured origin protocol when retaining its development parent.")
            if parent != source_parent:
                selected["prior_parent_evidence"] = {
                    "source_parent": source_parent, "tests": copy.deepcopy(selected["tests"]),
                    **{key: selected.pop(key) for key in ("last_tests", "last_trial") if key in selected},
                }
                for test in selected["tests"]:
                    test.pop("changed_from_parent", None)
                selected["notes"].append({"kind": "research", "text": "本次开发亲本已改变；原回归断言保留，旧变化见证转为亲本回归。旧测试/探索结果保存在prior_parent_evidence，不能当成当前结果；重跑继承测试，并新增相对当前parent的自然行为变化见证。"})
            contents = {"workspace": selected, "source_run": previous["id"], "source_session": identity,
                        "source_engine": previous["engine_hash"], "source_model": previous_config["model"],
                        "source_workspace_sha256": previous["artifacts"][metadata["workspace"]], "parent": parent,
                        "source_parent": source_parent,
                        "kind": "workspace"}
            target = directory / f"research-seed-{label}.json"
            save_json(target, contents)
            name = target.relative_to(directory).as_posix()
            state["system_files"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
            state["research_seeds"][label] = {"path": name, **{key: contents[key] for key in contents if key != "workspace"}}
        if not state["research_seeds"]:
            raise ValueError("No compatible saved research workspace matches the selected origins and incumbents.")
        if normalized.get("research_budget_mode") == "campaign":
            import_research_branches(source, previous, directory, state)
            if normalized.get("campaign_manager"):
                from bridge_evolve.evolution.manager import import_management
                import_management(previous, state)
        if normalized["champion_seed_run"] is not None or normalized["research_seed_run"] is not None:
            source_order = list(previous["champions"])
            state["champions"] = dict(sorted(state["champions"].items(),
                key=lambda item: source_order.index(item[0]) if item[0] in source_order else len(source_order)))
            inherited_issues = copy.deepcopy(previous.get("comparison_issues", {}))
            for identity, saved in previous["rounds"].items():
                if saved["status"] != "blocked":
                    continue
                contents = (source / "rounds" / identity / "summary.json").read_bytes()
                if hashlib.sha256(contents).hexdigest() != saved["summary_sha256"]:
                    raise ValueError("Source blocked-comparison evidence differs from its frozen summary.")
                summary = json.loads(contents)
                errors = [error for rows in summary["errors"].values() for error in rows]
                inherited_issues.setdefault(previous["id"] + ":" + identity, {
                    "round": identity, "source_run": previous["id"], "boards": saved["boards"],
                    "errors": errors[:8], "error_count": len(errors),
                })
            if changed_benchmarks:
                state["benchmark_revision"]["historical_comparison_issues"] = inherited_issues
            else:
                from bridge_evolve.evolution.faults import archive_legacy_baseline_issues
                state["comparison_issues"] = archive_legacy_baseline_issues(
                    state, normalized, inherited_issues, source_run=previous["id"],
                    previous_policy=previous_config.get("baseline_fault_policy", "strict"))
    state["initial_champions"] = dict(state["champions"])
    if normalized["replay"] is not None:
        contents = Path(normalized["replay"]).read_bytes()
        (directory / "replay.json").write_bytes(contents)
        state["system_files"]["replay.json"] = hashlib.sha256(contents).hexdigest()
    save_json(directory / "config.json", normalized)
    save_json(directory / "state.json", state)
    if normalized["evaluation_sources"]:
        Controller(directory).evaluations.import_sources()
    if normalized.get("repair_seed_run"):
        from bridge_evolve.evolution.repairs import import_incidents
        controller = Controller(directory)
        started = time.monotonic()
        import_incidents(controller, Path(normalized["repair_seed_run"]))
        prior = status(Path(normalized["repair_seed_run"]))
        handoff = prior.get("continuation") or {}
        if handoff.get("path") == str(directory) and handoff.get("recovery"):
            recovery = copy.deepcopy(handoff["recovery"])
            if recovery["expected_engine"] != controller.state["engine_hash"]:
                raise ValueError("Recovery initialized a different engine than the accepted handoff.")
            controller.state["recovery"] = recovery
            controller.state["recovery_history"] = [*copy.deepcopy(prior.get("recovery_history", [])), recovery.copy()]
            controller.checkpoint()
        if normalized.get("maintenance_budget"):
            controller.state["maintenance_seconds"] = controller.state.get("maintenance_seconds", 0) + time.monotonic() - started
            controller.checkpoint()
    return directory


def status(directory: Path) -> dict:
    state = json.loads((directory / "state.json").read_text(encoding="utf8"))
    if state["status"] == "running":
        with (directory / "controller.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                state.update(status="interrupted", error="Controller stopped; resume reuses the saved rounds and budgets.")
    return state


def cancel(directory: Path) -> dict:
    state = status(directory)
    (directory / "cancel-requested").touch(exist_ok=True)
    return {"id": state["id"], "status": state["status"], "cancel_requested": True}


class Controller:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.load_frozen_state()
        self.agent = None
        self.evaluator = None
        self.processing_evaluations = False
        self.evaluation_deadline = None
        self.evaluation_job = None
        self.processing_maintenance = False
        self.maintenance_started = None
        self.maintenance_previous = 0

    @property
    def evaluations(self):
        from bridge_evolve.evolution.queue import EvaluationQueue
        return EvaluationQueue(self)

    @property
    def module_contract(self):
        entry = self.state.get("module_contract")
        if entry is None:
            return None
        from bridge_evolve.evolution.contracts import ModuleContract
        data = json.loads((self.directory / entry["path"]).read_text(encoding="utf8"))
        if canonical_hash(data) != entry["hash"]:
            raise ValueError("The frozen module contract changed.")
        return ModuleContract(data, self.system(entry["anchor"]))

    @property
    def protocol_goal(self):
        from bridge_evolve.evolution.goals import protocol_goal
        return protocol_goal(self.config.get("protocol_goal"))

    @property
    def research_contract(self):
        return self.module_contract or self.protocol_goal

    def growth_seconds_held(self):
        return sum(job["remaining_seconds"] for job in self.state.get("growth_checks", {}).values()
                   if job["status"] in ("reserved", "running"))

    def development_versions(self):
        versions = list(self.state.get("origin_hashes", {}).values())
        versions.extend(self.state.get("initial_champions", {}).values())
        versions.extend(promotion["candidate"] for generation in self.state["generations"].values()
                        for promotion in generation["promotions"].values() if promotion["status"] == "promoted")
        return list(dict.fromkeys(versions))

    def reserve_growth(self, number):
        if not self.config.get("growth_boards"):
            return
        key = str(number)
        jobs = self.state.setdefault("growth_checks", {})
        if key not in jobs:
            seconds = self.config["growth_seconds"]
            used = self.previous_seconds + time.monotonic() - self.started - self.state.get("maintenance_seconds", 0)
            if used + self.evaluations.held_seconds() + self.growth_seconds_held() + seconds > self.config["max_seconds"]:
                raise EvolutionStopped("Insufficient reserved time for common-new-deal growth verification.")
            capacity = len(self.development_versions()) + len(self.state["champions"])
            pairs = capacity * len(self.config["baselines"]) * self.config["growth_boards"]
            jobs[key] = {"id": f"growth-g{number:03d}", "status": "reserved", "reserved_pairs": pairs,
                         "reserved_seconds": seconds, "remaining_seconds": seconds, "spent_seconds": 0}
            self.checkpoint()
        job = jobs[key]
        if job["status"] in ("reserved", "running"):
            try:
                self.evaluator.reserve_evaluation(job["id"], job["reserved_pairs"])
            except EvolutionStopped as error:
                job.update(status="budget_blocked", remaining_seconds=0, error=str(error))
                self.checkpoint()
                raise

    def verify_growth(self, number):
        if not self.config.get("growth_boards"):
            return
        job = self.state.get("growth_checks", {}).get(str(number))
        if job is None or job["status"] not in ("reserved", "running"):
            return
        if "lease_epoch" in job:
            unknown = min(job["remaining_seconds"], max(0, time.time() - job["lease_epoch"]))
            job["remaining_seconds"] -= unknown
            self.previous_seconds += unknown
        if job["remaining_seconds"] <= 0:
            job.update(status="budget_exhausted", error="Interrupted growth verification exhausted its conservative time lease.")
            self.evaluator.release_evaluation(job["id"])
            self.checkpoint()
            return
        versions = job.setdefault("versions", self.development_versions())
        old_stop, old_reservation = self.evaluator.stop, self.evaluator.active_reservation
        old_processing, old_deadline, old_job = self.processing_evaluations, self.evaluation_deadline, self.evaluation_job
        started = time.monotonic()
        remaining = job["remaining_seconds"]
        self.processing_evaluations = True
        self.evaluation_deadline, self.evaluation_job = started + remaining, job
        self.evaluator.active_reservation = job["id"]

        def stop_growth():
            self.stop()
            job["lease_epoch"] = time.time()

        self.evaluator.stop = stop_growth
        job["status"] = "running"
        self.checkpoint()
        try:
            result = self.evaluator.evaluate(job["id"], versions, self.config["growth_boards"])
            job["round"] = result["id"]
            if any(result["status"][identity] != "checked" for identity in versions):
                job.update(status="blocked", error="Growth verification contains invalid or blocked versions; no scores were filled.")
            else:
                curve = []
                for index, identity in enumerate(versions):
                    row = {"hash": identity, "objective": result["objective"][identity], "means": result["means"][identity],
                           "delta_to_seed": result["objective"][identity] - result["objective"][versions[0]]}
                    if index:
                        row["against_previous"] = paired_gain(result, identity, versions[index - 1], result["anchors"],
                            seed=result["seed"], alpha=.05, resamples=self.config["resamples"])
                        from bridge_evolve.evolution.faults import score_contributions
                        row["score_contributions"] = score_contributions(result, identity, versions[index - 1])
                    curve.append(row)
                job.update(status="completed", boards=result["boards"], curve=curve,
                           scope="All accepted versions share a fresh common block; non-monotone results are retained. Development evidence, not global confirmation.")
                self.milestone("growth:" + str(number), "growth_verified", generation=number, versions=len(versions),
                               boards=result["boards"], summary="全部开发版本在同一批新牌上重测；下降和不确定性不隐藏。")
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            job.update(status="budget_exhausted" if isinstance(error, EvolutionStopped) else "failed", error=str(error))
            raise
        finally:
            elapsed = time.monotonic() - started
            job["remaining_seconds"] = max(0, remaining - elapsed)
            job["spent_seconds"] = job["reserved_seconds"] - job["remaining_seconds"]
            job.pop("lease_epoch", None)
            self.evaluator.release_evaluation(job["id"])
            self.evaluator.stop, self.evaluator.active_reservation = old_stop, old_reservation
            self.processing_evaluations, self.evaluation_deadline, self.evaluation_job = old_processing, old_deadline, old_job
            if job["status"] not in ("reserved", "running"):
                job["remaining_seconds"] = 0
            if job["status"] == "completed":
                self.artifact(self.directory / f"growth-{number:03d}.json", job)
            self.checkpoint()

    def process_evaluations(self):
        self.evaluations.process()

    def process_repairs(self):
        from bridge_evolve.evolution.repairs import RepairQueue
        from bridge_evolve.evolution.manager import CampaignManager
        manager = CampaignManager(self) if self.config.get("campaign_manager") else None
        if manager:
            manager.reconcile()
        started = time.monotonic()
        previous = self.state.get("maintenance_seconds", 0)
        self.processing_maintenance = bool(self.config.get("maintenance_budget"))
        self.maintenance_started = started
        self.maintenance_previous = previous
        try:
            RepairQueue(self).process()
        finally:
            if manager:
                manager.reconcile()
            if self.processing_maintenance:
                accounted = self.state.get("maintenance_seconds", 0) - previous
                self.state["maintenance_seconds"] = self.state.get("maintenance_seconds", 0) + max(
                    0, time.monotonic() - started - accounted)
                self.checkpoint()
            self.processing_maintenance = False
            self.maintenance_started = None

    def model_time_window(self):
        remaining = max(0, self.config["max_seconds"] - self.previous_seconds
                   - (time.monotonic() - self.started) + self.state.get("maintenance_seconds", 0)
                   - self.evaluations.held_seconds() - self.growth_seconds_held())
        window = getattr(self, "delivery_window", None)
        return min(remaining, window.timeout()) if window else remaining

    def maintenance_time_window(self):
        budget = self.config.get("maintenance_budget")
        if not budget:
            return self.model_time_window()
        spent = (self.maintenance_previous + time.monotonic() - self.maintenance_started
                 if self.maintenance_started is not None else self.state.get("maintenance_seconds", 0))
        remaining = max(0, budget["max_seconds"] - spent)
        window = getattr(self, "delivery_window", None)
        return min(remaining, window.timeout()) if window else remaining

    def maintenance_stop(self):
        if (self.directory / "cancel-requested").exists():
            raise EvolutionStopped("Cancellation requested.")
        window = getattr(self, "delivery_window", None)
        if window:
            window.check()
        if self.maintenance_time_window() <= 0:
            from bridge_evolve.evolution.models import MaintenanceBudgetExhausted
            raise MaintenanceBudgetExhausted("The separate maintenance time budget is exhausted.")

    def load_frozen_state(self):
        self.config = json.loads((self.directory / "config.json").read_text(encoding="utf8"))
        self.state = status(self.directory)
        if canonical_hash(self.config) != self.state["config_hash"] or engine_identity() != self.state["engine_hash"]:
            raise ValueError("Evolution config or trusted engine differs from the frozen run; start a new experiment.")
        if (ROOT / "runtime-manifest.json").exists():
            from bridge_evolve.evolution.runtime import validate_runtime
            validate_runtime(ROOT)
        for name, digest in self.state["system_files"].items():
            if hashlib.sha256((self.directory / name).read_bytes()).hexdigest() != digest:
                raise ValueError("Evolution input identity changed.")
        self.previous_seconds = self.state["spent_seconds"]
        self.started = time.monotonic()

    def checkpoint(self):
        self.state["spent_seconds"] = self.previous_seconds + time.monotonic() - self.started
        self.state["updated_at"] = timestamp()
        save_json(self.directory / "state.json", self.state)

    def milestone(self, identity: str, kind: str, **details):
        self.state.setdefault("milestones", {}).setdefault(identity, {
            "id": identity, "kind": kind, "timestamp": timestamp(), **details,
        })

    def stop(self):
        window = getattr(self, "delivery_window", None)
        if window:
            window.check()
        if self.processing_maintenance:
            self.maintenance_stop()
            return
        if (self.directory / "cancel-requested").exists():
            raise EvolutionStopped("Cancellation requested.")
        revision = self.state.get("repair_engine")
        if (self.state["status"] == "running" and revision and revision["hash"] != self.state["engine_hash"]
                and not self.processing_evaluations and not (window and window.closing)):
            raise EngineRevisionReady("An accepted isolated engine revision is ready; checkpoint and continue on the new engine.")
        research_seconds = self.previous_seconds + time.monotonic() - self.started - self.state.get("maintenance_seconds", 0)
        if research_seconds >= self.config["max_seconds"]:
            raise EvolutionStopped("Wall-clock budget exhausted.")
        if self.processing_evaluations:
            if self.evaluation_job is not None and self.evaluation_deadline is not None:
                self.evaluation_job["remaining_seconds"] = max(0, self.evaluation_deadline - time.monotonic())
                self.evaluation_job["lease_updated_at"] = timestamp()
            if self.evaluation_deadline is not None and time.monotonic() >= self.evaluation_deadline:
                raise EvolutionStopped("The candidate evaluation time allocation is exhausted.")
        elif self.config["source"] == "research" and (
                research_seconds + self.evaluations.held_seconds() + self.growth_seconds_held()
                >= self.config["max_seconds"]):
            raise EvolutionStopped("Research time allowance exhausted; queued evaluation time remains reserved.")

    def system(self, identity):
        return frozen_system(str(self.directory / "systems"), identity)

    def artifact(self, path: Path, value: dict):
        save_json(path, value)
        self.state["artifacts"][path.relative_to(self.directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.checkpoint()

    def workspace_artifact(self, path: Path, value: dict):
        key = path.relative_to(self.directory).as_posix()
        if key in self.state.get("workspace_writes", {}):
            self.read_artifact(path)
        encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        digest = hashlib.sha256(encoded).hexdigest()
        self.state.setdefault("workspace_writes", {})[key] = {
            "before": self.state["artifacts"].get(key), "after": digest}
        self.checkpoint()
        self.artifact(path, value)
        if self.state["artifacts"][key] != digest:
            raise ValueError("A provisional workspace changed during its tracked write.")
        del self.state["workspace_writes"][key]
        self.checkpoint()

    def read_artifact(self, path: Path):
        key = path.relative_to(self.directory).as_posix()
        digest = self.state["artifacts"].get(key)
        pending = self.state.get("workspace_writes", {}).get(key)
        if pending:
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if digest not in (pending["before"], pending["after"]):
                raise ValueError("The pending workspace write disagrees with its trusted checkpoint.")
            if actual == pending["after"]:
                self.state["artifacts"][key] = digest = actual
                outcome = "committed"
            elif actual == digest == pending["before"]:
                outcome = "not_written"
            else:
                raise ValueError("The workspace matches neither side of its tracked write; refusing recovery.")
            del self.state["workspace_writes"][key]
            self.state.setdefault("workspace_recoveries", []).append({
                "path": key, "outcome": outcome, "timestamp": timestamp()})
            self.checkpoint()
        if not path.exists():
            if digest is not None:
                raise ValueError("A checkpointed evolution artifact is missing.")
            return None
        if digest is None:
            orphan = path.with_name(path.name + ".uncheckpointed-" + uuid.uuid4().hex)
            path.rename(orphan)
            self.state["recoveries"].append(str(orphan.relative_to(self.directory)))
            self.checkpoint()
            return None
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != digest:
            raise ValueError("Evolution analysis/proposal artifact hash mismatch.")
        return json.loads(contents)

    def round(self, identity: str, systems: list[str], stage: str) -> dict:
        if self.state.get("activity", {}).get("round") != identity:
            self.state["activity"] = {"kind": "evaluation", "round": identity, "stage": stage, "started_at": timestamp()}
        result = self.evaluator.evaluate(identity, systems, self.config["boards"][stage])
        self.milestone("round:" + identity, "evaluation", round=identity, stage=stage, boards=self.config["boards"][stage],
                       statuses=result["status"], objective=result["objective"], means=result["means"])
        self.checkpoint()
        blocked = [candidate for candidate in systems if result["status"][candidate] == "blocked"]
        if blocked:
            detail = next((error["message"] for candidate in blocked for error in result["errors"][candidate]), "unknown evaluator error")
            errors = [error for candidate in blocked for error in result["errors"][candidate]]
            anchors = {value["hash"] for value in self.config["baselines"]}
            if (self.config["source"] == "research" or self.processing_evaluations) and errors and all(
                    error.get("system_hash") in anchors and error["stage"] not in ("dds", "score", "scoring", "execution")
                    for error in errors):
                self.state.setdefault("comparison_issues", {})[identity] = {
                    "round": identity, "boards": self.config["boards"][stage], "errors": errors[:8], "error_count": len(errors)}
                self.milestone("comparison-blocked:" + identity, "comparison_blocked", round=identity,
                               error_count=len(errors), summary=detail)
                self.checkpoint()
                return result
            raise EvaluationBlocked("Comparison blocked by an opponent or evaluator failure: " + detail)
        return result

    def invalid_candidate(self, entry: dict, result: dict):
        errors = result["errors"][entry["hash"]]
        blocked = result["status"][entry["hash"]] == "blocked"
        entry.update(status="comparison_blocked" if blocked else "invalid", error_round=result["id"], error_count=len(errors), errors=errors[:3])
        self.milestone("invalid:" + result["id"] + ":" + entry["hash"], "comparison_blocked" if blocked else "candidate_invalid",
                       lineage=entry["lineage"], candidate=entry["hash"], round=result["id"],
                       error_count=len(errors), errors=errors[:3])

    def proposals(self, generation: int, label: str, parent: str, packet: dict) -> dict:
        base = self.directory / "proposals" / f"g{generation:03d}-{label}"
        path = base.with_suffix(".json")
        result = self.read_artifact(path)
        if result is not None:
            if result.get("parent_hash") != parent:
                raise ValueError("Stored proposals reference a different parent.")
            return result
        packet = agent_packet(packet)
        self.artifact(base.with_suffix(".packet.json"), packet)
        source = self.config["source"]
        if source == "research":
            if self.config.get("research_architecture", "legacy") == "director":
                from bridge_evolve.evolution.director import DirectedResearch
                result = DirectedResearch(self, generation, label, parent, packet).run()
            else:
                from bridge_evolve.evolution.research import ResearchSession
                research = ResearchSession(self, generation, label, parent, packet).run()
                result = {"source": "research", "analysis": [], "research_session": research["session"],
                          "research_status": research["status"], "summary": research["summary"],
                          "proposals": [{"parent_hash": parent, "hypothesis": release["hypothesis"],
                                         "snapshot_hash": release["hash"], "release": release["path"]}
                                        for release in research["releases"]]}
        elif source == "numbers":
            result = numeric_proposals(self.system(parent), packet, offset=2 * (generation - 1))
        elif source == "replay":
            replay = json.loads((self.directory / "replay.json").read_text(encoding="utf8"))
            if not isinstance(replay, list) or any(not isinstance(value, dict) for value in replay):
                raise ValueError("Replay input must be an array of proposals.")
            result = {"source": "replay", "analysis": [],
                      "proposals": [value for value in replay if value.get("parent_hash") == parent][:2]}
        else:
            result = None
            for attempt in range(2):
                self.stop()
                if self.state["agent_requests"] >= self.config["max_agent_requests"]:
                    raise EvolutionStopped("Agent request budget exhausted.")
                self.state["agent_requests"] += 1
                self.state["phase"] = f"g{generation}:agent:{label}"
                self.checkpoint()
                try:
                    if self.agent is None:
                        self.agent = LocalAgent(Path(self.config["project"]), self.config["model"], self.config["agent_timeout"],
                                                client=ModelClient(self.config, self.state, self.checkpoint, self.stop))
                    result = self.agent.propose(packet, request_number=self.state["agent_requests"])
                    break
                except EvolutionStopped:
                    raise
                except (OSError, ValueError, RuntimeError) as error:
                    save_json(base.with_suffix(f".error-{self.state['agent_requests']}.json"),
                              {"status": "agent_error", "error": str(error), "request": self.state["agent_requests"]})
                    if attempt:
                        raise
                    packet = {**packet, "previous_response_error": str(error)}
            if result is None:
                raise RuntimeError("The configured agent returned no result.")
        limit = self.config["max_releases"] if source == "research" else 2
        if not isinstance(result.get("proposals"), list) or len(result["proposals"]) > limit:
            raise ValueError("Proposal source exceeded its per-lineage budget.")
        result["parent_hash"] = parent
        self.artifact(path, result)
        return result

    def generation(self, number: int):
        key = str(number)
        generation = self.state["generations"].setdefault(key, {
            "parents": dict(self.state["champions"]), "status": "running", "candidates": {}, "promotions": {}})
        if generation["status"] == "completed":
            return
        parents = generation["parents"]
        self.milestone("generation:" + key, "generation_started", generation=number, parents=parents)
        research = self.config["source"] == "research"
        diagnosis = self.round(f"g{number:03d}-diagnosis", list(parents.values()), "diagnosis")
        if not research and any(diagnosis["status"][identity] != "checked" for identity in parents.values()):
            raise EvaluationBlocked("An incumbent is invalid on its fresh diagnosis round.")
        for label, parent in parents.items():
            analysis_path = self.directory / "analysis" / f"g{number:03d}-{label}.json"
            packet = self.read_artifact(analysis_path)
            if packet is not None:
                if packet["parent_hash"] != parent:
                    raise ValueError("Analysis parent identity mismatch.")
            else:
                contract = self.config.get("module_contract")
                packet = evidence_packet(self.system(parent), self.evaluator.records(diagnosis, parent), self.config["modules"],
                                         state_prefix=contract["definition"]["state_prefix"] if contract else None)
                packet["diagnosis_status"] = diagnosis["status"][parent]
                packet["diagnosis_errors"] = diagnosis["errors"][parent][:8]
                if self.state.get("comparison_issues"):
                    packet["comparison_warning"] = "Known fixed-opponent contract failures block formal ranking/promotion; do not change the frozen opponents or claim a win. Research, authored tests and diagnostic experiments may continue."
                packet["previous_attempts"] = [
                    {**{key: value[key] for key in ("status", "error", "delta", "full_delta") if key in value},
                     "hypothesis": value["proposal_summary"].get("hypothesis", "")[:1500],
                     "targets": value["proposal_summary"].get("targets", []), "errors": value.get("errors", [])}
                    for prior in self.state["generations"].values() for value in prior["candidates"].values()
                    if value.get("parent") == parent][-6:]
                self.artifact(analysis_path, packet)
            batch = self.proposals(number, label, parent, packet)
            generation.setdefault("analysis", {})[label] = {
                "groups": packet["total_groups"], "selected": len(packet["selected_groups"]),
                "deferred": len(packet["deferred_groups"]), "agent_analyzed": len(batch.get("analysis", [])),
                "path": str(analysis_path.relative_to(self.directory))}
            if self.config.get("research_architecture") == "director":
                self.checkpoint()
                continue
            for index, proposal in enumerate(batch["proposals"]):
                proposal_id = f"{label}-{index}"
                if proposal_id in generation["candidates"]:
                    continue
                entry = {"lineage": label, "parent": parent, "status": "proposed", "candidate_id": proposal_id,
                         "proposal_ref": f"proposals/g{number:03d}-{label}.json#proposals/{index}",
                         "proposal_summary": {"hypothesis": proposal["hypothesis"] if isinstance(proposal, dict) and isinstance(proposal.get("hypothesis"), str) else "",
                            "targets": [{key: edit.get(key) for key in ("kind", "id", "state", "field")} for edit in proposal.get("edits", [])
                                        if isinstance(edit, dict)] if isinstance(proposal, dict) and isinstance(proposal.get("edits"), list) else []}}
                try:
                    if research:
                        release = self.read_artifact(self.directory / proposal["release"])
                        if release is None or release["parent"] != parent or release["hash"] != proposal["snapshot_hash"]:
                            raise ValueError("Whole-system research release identity mismatch.")
                        identity = release["hash"]
                        self.system(identity)
                        entry.update(research_session=batch["research_session"], release=proposal["release"],
                                     tests=release["tests"])
                        job = self.evaluations.enqueue(proposal["release"], lineage=label, generation=number)
                        entry["evaluation"] = job["id"]
                        entry["proposal_summary"]["targets"] = [{"kind": "whole_system", "id": identity}]
                    else:
                        candidate = apply_proposal(self.system(parent), packet, proposal)
                        identity = register_system(self.directory, self.state, candidate)
                    seen = identity in self.state["champions"].values() or any(
                            value.get("hash") == identity for previous in self.state["generations"].values()
                            for value in previous["candidates"].values())
                    if seen and not research:
                        entry.update(status="duplicate", hash=identity)
                    else:
                        entry.update(status="eligible", hash=identity, previously_seen=seen)
                    self.state.setdefault("candidate_archive", {}).setdefault(identity, {"occurrences": []})["occurrences"].append(
                        {"generation": number, "candidate": proposal_id, "parent": parent, "lineage": label})
                except (ValueError, RuntimeError) as error:
                    entry.update(status="rejected", error=str(error))
                generation["candidates"][proposal_id] = entry
                self.checkpoint()
        if research:
            self.process_evaluations()
            if self.config.get("research_architecture") == "director":
                self.complete_generation(number, generation)
                return
            entries = list(generation["candidates"].values())
            if self.config.get("selection_policy") == "parent-improvement":
                entries.sort(key=lambda entry: entry.get("full_delta", self.evaluations.jobs.get(
                    entry.get("evaluation"), {}).get("outcome", {}).get("full_delta", float("-inf"))), reverse=True)
            for entry in entries:
                if "evaluation" not in entry:
                    continue
                job = self.evaluations.jobs[entry["evaluation"]]
                entry.update(job.get("outcome", {"status": job["status"]}))
                if "delta" in job:
                    entry["delta"] = job["delta"]
                if entry["status"] == "development_complete":
                    self.confirm_candidate(number, generation, entry)
            self.complete_generation(number, generation)
            return
        if "validation_candidates" not in generation:
            generation["validation_candidates"] = [key for key, entry in generation["candidates"].items() if entry["status"] == "eligible"]
            self.checkpoint()
        validation_ids = generation["validation_candidates"]
        valid = []
        if validation_ids:
            validation = self.round(f"g{number:03d}-validity",
                                    [generation["candidates"][key]["hash"] for key in validation_ids], "validity")
            for key in validation_ids:
                entry = generation["candidates"][key]
                if validation["status"][entry["hash"]] == "checked":
                    valid.append(key)
                else:
                    self.invalid_candidate(entry, validation)
        if "screen_candidates" not in generation:
            generation["screen_candidates"] = valid
            self.checkpoint()
        eligible = [generation["candidates"][key] for key in generation["screen_candidates"]]
        if eligible:
            screen = self.round(f"g{number:03d}-screen", [entry["hash"] for entry in eligible] + list(parents.values()), "screen")
            if not research and any(screen["status"][parent] != "checked" for parent in parents.values()):
                raise EvaluationBlocked("An incumbent failed its fresh common screening round.")
            survivors = []
            for entry in eligible:
                identity, parent = entry["hash"], entry["parent"]
                if screen["status"][identity] != "checked":
                    self.invalid_candidate(entry, screen)
                elif screen["status"][parent] != "checked":
                    entry.update(status="comparison_blocked", error="The incumbent has no complete valid score on this common round.")
                elif research or screen["objective"][identity] >= screen["objective"][parent]:
                    survivors.append(entry)
                else:
                    entry.update(status="screen_rejected", delta=screen["objective"][identity] - screen["objective"][parent])
            survivors.sort(key=lambda entry: (-(screen["objective"][entry["hash"]] - screen["objective"][entry["parent"]]), entry["hash"]))
            finalists = []
            if research:
                finalists = survivors
            else:
                for entry in survivors:
                    if not any(value["lineage"] == entry["lineage"] for value in finalists):
                        finalists.append(entry)
                    if len(finalists) == (2 if self.config["profile"] == "research" else 1):
                        break
            if finalists:
                full = self.round(f"g{number:03d}-full", [entry["hash"] for entry in finalists] + list(parents.values()), "full")
                if not research and any(full["status"][parent] != "checked" for parent in parents.values()):
                    raise EvaluationBlocked("An incumbent failed its fresh full round.")
                if research:
                    finalists.sort(key=lambda entry: -(
                        full["objective"][entry["hash"]] - full["objective"][entry["parent"]]
                        if entry["hash"] in full["objective"] and entry["parent"] in full["objective"] else float("-inf")))
                for entry in finalists:
                    identity, parent = entry["hash"], entry["parent"]
                    if full["status"][identity] != "checked":
                        self.invalid_candidate(entry, full)
                        continue
                    if full["status"][parent] != "checked":
                        entry.update(status="comparison_blocked", error="The incumbent has no complete valid score on this common round.")
                        continue
                    delta = full["objective"][identity] - full["objective"][parent]
                    entry["full_delta"] = delta
                    if research:
                        entry["changes"] = comparison_changes(self.directory, full, identity, parent)
                        if not entry["changes"]["changed_families"]:
                            entry["status"] = "insufficient_coverage"
                            continue
                    if delta < (0 if research else self.config["min_promotion_delta"]):
                        entry["status"] = "no_full_gain"
                        continue
                    self.confirm_candidate(number, generation, entry)
        self.complete_generation(number, generation)

    def confirm_candidate(self, number, generation, entry):
        if not self.config.get("allow_promotion", True):
            entry["status"] = "development_only"
            return
        if self.config.get("research_budget_mode") == "campaign":
            challenge = self.queue_challenge(entry["hash"], entry["lineage"], number=number)
            self.process_challenges()
            entry["status"] = challenge["status"]
            return
        identity, parent = entry["hash"], entry["parent"]
        research = self.config["source"] == "research"
        promotion_key = entry["lineage"] + "-" + identity[:12] if research else entry["lineage"]
        existing = generation["promotions"].get(promotion_key)
        if research and any(value["status"] == "promoted" and value.get("lineage") == entry["lineage"]
                            for value in generation["promotions"].values()):
            entry["status"] = "portfolio_retained"
            return
        if existing is not None and existing["status"] != "allocated":
            entry["status"] = existing["status"]
            return
        if research and any(value.get("candidate") == identity and value.get("parent") == parent
                            and value["status"] in ("promoted", "not_confirmed")
                            for prior in self.state["generations"].values() if prior is not generation
                            for value in prior["promotions"].values()):
            entry["status"] = "previously_evaluated"
            return
        if self.state.get("comparison_issues"):
            entry.update(status="comparison_blocked", error="Known frozen-opponent contract failures block formal promotion.")
            return
        attempts = self.state["promotion_attempts"]
        if existing is None and attempts >= self.config["max_promotions"]:
            raise EvolutionStopped("Fresh promotion attempt budget exhausted.")
        promotion = generation["promotions"].setdefault(promotion_key, {
            "attempt": attempts + 1, "candidate": identity, "parent": parent, "status": "allocated", "lineage": entry["lineage"]})
        if promotion["status"] != "allocated":
            return
        if promotion["attempt"] > attempts:
            self.state["promotion_attempts"] = promotion["attempt"]
        self.checkpoint()
        result = self.round(f"g{number:03d}-promotion-{promotion_key}", [identity, parent], "promotion")
        if result["status"][identity] != "checked" or result["status"][parent] != "checked":
            promotion["status"] = "blocked" if "blocked" in (result["status"][identity], result["status"][parent]) else "invalid"
            if result["status"][identity] != "checked":
                self.invalid_candidate(entry, result)
            else:
                entry.update(status="comparison_blocked", error="The incumbent failed the fresh promotion comparison.")
        else:
            development = self.config.get("selection_policy") == "parent-improvement"
            gain = paired_gain(result, identity, parent, result["anchors"], seed=result["seed"],
                               alpha=self.config["promotion_alpha"] if development else self.config["promotion_alpha"] / self.config["max_promotions"],
                               resamples=self.config["resamples"])
            promotion["statistics"] = gain
            from bridge_evolve.evolution.faults import score_contributions
            contributions = score_contributions(result, identity, parent)
            if contributions is not None:
                promotion["score_contributions"] = contributions
            if development:
                accepted = gain["mean"] > 0
                promotion.update(selection_policy="parent-improvement", global_confirmed=False)
            else:
                accepted = (gain["lower"] > 0 and gain["mean"] >= self.config["min_promotion_delta"]
                            and min(gain["opponent_deltas"]) >= -self.config["max_opponent_regression"])
            promotion["status"] = "promoted" if accepted else "not_confirmed"
            entry["status"] = promotion["status"]
            if accepted:
                self.state["champions"][entry["lineage"]] = identity
        self.milestone("promotion:" + str(promotion["attempt"]), "promotion", generation=number,
                       selection_policy=self.config.get("selection_policy", "confirmed"),
                       lineage=entry["lineage"], **{key: promotion[key] for key in
                       ("attempt", "candidate", "parent", "status", "statistics") if key in promotion})
        self.checkpoint()

    def queue_challenge(self, candidate, lineage, *, number=None):
        if lineage not in self.state["champions"]:
            raise ValueError("A challenge must name an existing research lineage.")
        if candidate not in self.state["candidate_archive"]:
            raise ValueError("A challenge requires a retained candidate with a frozen release and actual validation.")
        metadata = self.state["candidate_archive"][candidate]
        if not metadata.get("release"):
            raise ValueError("An unfinished branch must be implemented and published before it can challenge.")
        jobs = [job for job in self.evaluations.jobs.values() if job["candidate"] == candidate]
        if not jobs:
            self.evaluations.enqueue(metadata["release"], lineage=lineage,
                                     generation=number or max(map(int, self.state["generations"]), default=1))
            self.process_evaluations()
            jobs = [job for job in self.evaluations.jobs.values() if job["candidate"] == candidate]
        if not any(job["status"] in ("development_complete", "no_full_gain", "insufficient_coverage") for job in jobs):
            raise ValueError("The candidate has no completed valid current-run development evaluation; it cannot challenge yet.")
        incumbent = self.state["champions"][lineage]
        identity = "c-" + canonical_hash({"candidate": candidate, "incumbent": incumbent, "lineage": lineage})[:20]
        queue = self.state.setdefault("challenge_queue", {})
        existing = next((job for job in queue.values() if job["candidate"] == candidate
                         and job["parent"] == incumbent and job["lineage"] == lineage), None)
        if existing is not None:
            return existing
        if identity not in queue:
            queue[identity] = {"id": identity, "candidate": candidate, "parent": incumbent,
                               "design_parent": metadata.get("parent"), "lineage": lineage,
                               "generation": number or max(map(int, self.state["generations"]), default=1),
                               "status": "reigning" if candidate == incumbent else "queued", "created_at": timestamp()}
            self.checkpoint()
        return queue[identity]

    def process_challenges(self):
        for job in self.state.get("challenge_queue", {}).values():
            if job["status"] not in ("queued", "allocated"):
                continue
            if self.state.get("comparison_issues"):
                job.update(status="comparison_blocked", error="Frozen benchmark faults block formal comparison.")
                self.checkpoint()
                continue
            if "attempt" not in job:
                if self.state["promotion_attempts"] >= self.config["max_promotions"]:
                    job.update(status="budget_blocked", error="Campaign challenge allowance exhausted; candidate retained.")
                    self.checkpoint()
                    continue
                job["parent"] = self.state["champions"][job["lineage"]]
                if job["parent"] == job["candidate"]:
                    job["status"] = "reigning"
                    self.checkpoint()
                    continue
                previous = next((row for row in self.state["challenge_queue"].values()
                                 if row is not job and row["candidate"] == job["candidate"]
                                 and row["parent"] == job["parent"] and row["lineage"] == job["lineage"]
                                 and "attempt" in row), None)
                if previous is not None:
                    job.update(status="duplicate", duplicate_of=previous["id"])
                    self.checkpoint()
                    continue
                self.state["promotion_attempts"] += 1
                job.update(status="allocated", attempt=self.state["promotion_attempts"])
                self.checkpoint()
            generation = self.state["generations"].get(str(job["generation"]))
            if generation is not None:
                generation["promotions"][job["id"]] = job
            try:
                result = self.round(job["id"] + "-challenge", [job["candidate"], job["parent"]], "promotion")
            except EvolutionStopped as error:
                if (self.directory / "cancel-requested").exists():
                    raise
                job.update(status="budget_blocked", error=str(error))
                self.checkpoint()
                continue
            candidate, incumbent = job["candidate"], job["parent"]
            if result["status"][candidate] != "checked" or result["status"][incumbent] != "checked":
                job.update(status="comparison_blocked" if result["status"][incumbent] != "checked" else "invalid",
                           error="The fresh current-champion comparison did not produce two valid complete scores.")
                self.checkpoint()
                continue
            development = self.config.get("selection_policy") == "parent-improvement"
            gain = paired_gain(result, candidate, incumbent, result["anchors"], seed=result["seed"],
                               alpha=self.config["promotion_alpha"] if development else
                                     self.config["promotion_alpha"] / self.config["max_promotions"],
                               resamples=self.config["resamples"])
            accepted = gain["mean"] > 0 if development else (
                gain["lower"] > 0 and gain["mean"] >= self.config["min_promotion_delta"]
                and min(gain["opponent_deltas"]) >= -self.config["max_opponent_regression"])
            from bridge_evolve.evolution.faults import score_contributions
            job.update(statistics=gain, boards=result["boards"], status="promoted" if accepted else "not_confirmed",
                       selection_policy=self.config.get("selection_policy", "confirmed"),
                       global_confirmed=False if development else accepted,
                       score_contributions=score_contributions(result, candidate, incumbent), finished_at=timestamp())
            if accepted:
                self.state["champions"][job["lineage"]] = candidate
                self.state.setdefault("champion_history", []).append({
                    "candidate": candidate, "previous": incumbent, "challenge": job["id"], "timestamp": timestamp()})
            self.state["candidate_archive"][candidate]["last_challenge"] = job["id"]
            self.milestone(job["id"], "promotion", **{key: job[key] for key in
                           ("attempt", "candidate", "parent", "lineage", "status", "statistics")})
            self.checkpoint()

    def complete_generation(self, number, generation):
        key = str(number)
        generation["status"] = "completed"
        promoted = any(value["status"] == "promoted" for value in generation["promotions"].values())
        self.state["plateau_generations"] = 0 if promoted else self.state["plateau_generations"] + 1
        self.state["generation"] = number
        self.milestone("generation-end:" + key, "generation_completed", generation=number,
                       candidates=len(generation["candidates"]), promotions=sum(value["status"] == "promoted" for value in generation["promotions"].values()))
        self.checkpoint()

    def verify_goal(self, *, force=False):
        if self.state.get("comparison_issues"):
            self.state["goal"] = {"status": "blocked", "reason": "Frozen-opponent contract failures remain unresolved; no 10000-board check was consumed."}
            self.checkpoint()
            return
        if self.state["goal"]["status"] not in ("not_checked", "allocated"):
            return
        if self.state["goal"]["status"] == "not_checked":
            if self.state["goal_checks"] >= self.config["max_goal_checks"]:
                raise EvolutionStopped("10000-board verification budget exhausted.")
            origins = set(self.state.get("origin_hashes", {}).values()) or {baseline["hash"] for baseline in self.config["baselines"]}
            evolved = list(dict.fromkeys(identity for identity in self.state["champions"].values() if identity not in origins))
            if not evolved:
                self.state["goal"] = {"status": "not_checked", "reason": "No evolved champion; no 10000-board check was consumed."}
                self.checkpoint()
                return
            selection_id = "goal-selection-" + canonical_hash(evolved)[:12]
            result = self.round(selection_id, evolved, "full")
            checked = [identity for identity in evolved if result["status"][identity] == "checked"]
            if not checked:
                raise EvaluationBlocked("No valid champion is available for goal verification.")
            selected = min(checked, key=lambda identity: (-min(result["means"][identity].values()), -result["objective"][identity], identity))
            if not force and min(result["means"][selected].values()) < self.config["goal_delta"]:
                self.state["goal"] = {"status": "not_checked", "candidate": selected,
                                      "reason": "Fresh selection evidence is below the target; no 10000-board check was consumed."}
                self.checkpoint()
                return
            self.state["goal_checks"] += 1
            self.state["goal"] = {"status": "allocated", "candidate": selected}
            self.checkpoint()
        selected = self.state["goal"]["candidate"]
        result = self.evaluator.evaluate("goal-10000", [selected], self.config["goal_boards"])
        if result["status"][selected] != "checked":
            self.state["goal"]["status"] = result["status"][selected]
            self.checkpoint()
            if result["status"][selected] == "blocked":
                raise EvaluationBlocked("10000-board check was blocked by the evaluator or fixed opponent.")
            return
        statistics = column_summary([result["scores"][selected][anchor] for anchor in result["anchors"]],
                                   seed=result["seed"], alpha=self.config["goal_alpha"] / len(result["anchors"]),
                                   resamples=max(20000, self.config["resamples"]))
        lower = min(value["lower"] for value in statistics)
        self.state["goal"].update(status="target_met" if lower > self.config["goal_delta"] else
                                  "statistically_better_only" if lower > 0 else "not_established",
                                  opponents=dict(zip(result["anchors"], statistics, strict=True)), boards=10000)
        self.checkpoint()

    def report(self):
        from bridge_evolve.evolution.costs import resource_summary
        from bridge_evolve.evolution.presentation import render_report, research_summary
        lines = ["# 叫牌体系自进化", "", self.state["name"], "",
                 f"状态：{self.state['status']}；已完成{self.state['generation']}代。",
                 f"提案来源：{self.config['source']}；Agent请求{self.state['agent_requests']}次；已保留配对预算{self.state['evaluated_pairs']}。",
                 f"模型：{self.config['model']}；请求上限{self.config['max_agent_requests']}；配对上限{self.config['max_pairs']}；总时间上限{self.config['max_seconds']}秒。",
                 "", "每个评测轮次独立随机生成新牌，同轮候选和对手使用共同牌；没有预留测试集。",
                 "", "## 当前冠军", "", *[f"- {label}：`{identity}`" for label, identity in self.state["champions"].items()],
                 "", "## 代际结果", ""]
        if self.config.get("development_sampling") is not None:
            lines.extend(["", "## 局部条件开发评价", "",
                          "本轮不运行均匀全局复筛；以下收益仅针对冻结条件分布，不能解释为全局提升或冠军晋级。",
                          "```json", json.dumps(self.config["development_sampling"], ensure_ascii=False, indent=2), "```"])
        if self.config.get("selection_policy") == "parent-improvement":
            lines.extend(["", "## 开发亲本选择", "",
                          "仅在新共同牌上相对上一亲本平均配对IMP严格增加时开发升级；不要求击败成熟体系，不以显著性或单对手保护作为本批升级门槛。",
                          "这不是全局确认。置信区间和失败保留，后续共同新牌成长检查可能揭示回退。"])
        if self.config.get("protocol_goal"):
            lines.extend(["", "## 体系目标", "", self.config["protocol_goal"],
                          "全Pass仅为起始研究骨架。所有发布物均须通过公开逼叫义务和自然开叫Pass/同伴非Pass回应见证。"])
        if self.state.get("growth_checks"):
            lines.extend(["", "## 共同新牌成长验证", ""])
            for number, job in self.state["growth_checks"].items():
                lines.extend([f"### 单元{number}：{job['status']}", "", "```json",
                              json.dumps({key: job[key] for key in ("round", "boards", "versions", "curve", "scope", "error")
                                          if key in job}, ensure_ascii=False, indent=2), "```"])
        for number, generation in self.state["generations"].items():
            lines.append(f"- 第{number}代：{generation['status']}，{len(generation['candidates'])}个提案，"
                         f"{sum(value['status'] == 'promoted' for value in generation['promotions'].values())}次晋级。")
        if self.state.get("model_usage"):
            lines.extend(["", "## 模型用量", "", "```json", json.dumps(self.state["model_usage"], ensure_ascii=False, indent=2), "```",
                          "", "token先保守预留，收到实际用量后结算；未知用量保留预留额。没有可靠价格数据，不伪造货币费用。"])
        resources = resource_summary(self.state)
        save_json(self.directory / "resources.json", resources)
        lines.extend(["", "## 研究资源与吞吐", "", "```json", json.dumps(resources, ensure_ascii=False, indent=2), "```",
                      "", "AI Credits只取实际上游收据；缺失不补0。共享研究成本不按发布次数重复相加，资源指标不进入体系适应度。"])
        if self.state.get("benchmark_revision"):
            lines.extend(["", "## 明确修订的参考体系", "", self.state["benchmark_revision"]["reason"],
                          "", "旧参考、错误牌、模型用量和成绩仍在来源实验保存；本次使用新参考重新发牌，不把两种参考的成绩拼接。"])
        if self.state.get("research_sessions"):
            lines.extend(["", "## 自主研究", ""])
            for identity, session in self.state["research_sessions"].items():
                lines.append(f"- {identity}：{session['status']}，{session['turns']}轮，{session['tool_calls']}次工具，{session['releases']}个完整候选；[{session['model']}]({session['path']})。")
        if self.state.get("evaluation_queue"):
            lines.extend(["", "## 独立候选评价", "", "队列按候选、父本、引擎及评测配置去重；开发评价不等于晋级。", ""])
            for identity, job in self.state["evaluation_queue"].items():
                outcome = job.get("outcome", {})
                lines.append(f"- {identity}：{job['status']}；候选`{job['candidate']}`；"
                             f"{len(job['occurrences'])}次发布来源；"
                             f"开发收益差{outcome.get('full_delta', '—')}；"
                             f"预留配对{job['remaining_pairs']}/{job['reserved_pairs']}。")
                if outcome.get("evaluation_scope") == "conditional" and "statistics" in outcome:
                    lines.append(f"  条件收益差95%配对区间：{outcome['statistics']['ci95']}；"
                                 f"实际改变{outcome['changes']['changed_families']}/{outcome['statistics']['boards']}个牌族。")
            lines.extend(["", f"执行模式：{self.state.get('execution_mode', 'research')}。evaluation-only不初始化模型或消耗确认尝试。"])
        if self.state.get("comparison_issues"):
            lines.extend(["", "## 正式比较受阻", "", "固定对手存在未解决的公开协议/执行缺陷。研究可以继续，但失败牌不删除、不补零分，受阻成绩不排名，也不确认晋级或万副达标。", ""])
            for identity, issue in self.state["comparison_issues"].items():
                lines.append(f"- {identity}：{issue['error_count']}项检查问题；原始证据保留。")
        lines.extend(["", "## 10000副目标", "", "```json", json.dumps(self.state["goal"], ensure_ascii=False, indent=2), "```"])
        if self.state.get("error"):
            lines.extend(["", "## 停止原因", "", self.state["error"]])
        lines.extend(["", ("开发升级只说明本批新牌上优于上一亲本；是否持续改善应查看共同新牌成长验证，不等于成熟体系达标。"
                           if self.config.get("selection_policy") == "parent-improvement" else
                           "运行完成不代表已经有提升；仅完整通过新牌确认的候选更新冠军。原始两个锚点不变。"), ""])
        overview = render_report(research_summary(self.directory, self.state, self.config), self.state["name"])
        (self.directory / "report.md").write_text(overview + "\n# 技术附录\n\n" + "\n".join(lines), encoding="utf8")

    def run(self, *, goal_only=False, evaluation_only=False, repair_only=False):
        from bridge_evolve.evolution.manager import CampaignManager, DeliveryBlocked
        evaluation_only = evaluation_only or self.config.get("evaluation_only", False)
        if repair_only and (goal_only or evaluation_only):
            raise ValueError("Repair-only is separate from evaluation and goal verification.")
        if goal_only and self.config.get("development_sampling") is not None:
            raise ValueError("Global goal verification requires a separately frozen uniform evaluation run, not a conditional cohort.")
        if goal_only and self.config.get("selection_policy") == "parent-improvement":
            raise ValueError("Development parent upgrades are not global confirmation; use a separate frozen global verification run.")
        if goal_only and evaluation_only:
            raise ValueError("Goal verification and evaluation-only are separate modes.")
        lock_path = self.directory / "controller.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("This evolution experiment already has a running controller.") from error
            self.load_frozen_state()
            self.state["status"] = "running"
            self.state["execution_mode"] = "repair-only" if repair_only else "evaluation-only" if evaluation_only else "research"
            self.state.pop("error", None)
            if self.state.get("recovery") and self.state["recovery"]["status"] != "research_resumed":
                self.state["recovery"].update(status="running", started_at=timestamp())
            self.checkpoint()
            try:
                self.evaluator = Evaluator(self.directory, self.config, self.state, self.checkpoint, self.stop)
                from bridge_evolve.evolution.repairs import record_incident
                self.evaluator.incident_callback = lambda record, candidate, opponent, identity: record_incident(
                    self, record, candidate, opponent, identity)
                if evaluation_only and not self.evaluations.jobs:
                    raise ValueError("No frozen candidate evaluations are queued; import a stopped run or published archive first.")
                if repair_only:
                    self.process_repairs()
                else:
                    if self.config.get("research_architecture") == "director":
                        self.process_repairs()
                    self.process_evaluations()
                if not goal_only and not evaluation_only and not repair_only:
                    for number in range(1, self.config["generations"] + 1):
                        self.stop()
                        self.reserve_growth(number)
                        self.generation(number)
                        self.process_challenges()
                        self.verify_growth(number)
                        if self.config.get("research_architecture") == "director":
                            self.process_repairs()
                        plateau = self.config.get("plateau_limit", 3)
                        if plateau and self.state["plateau_generations"] >= plateau:
                            break
                if not evaluation_only and not repair_only and (goal_only or self.config["automatic_goal_check"]):
                    self.verify_goal(force=goal_only)
                if any(job["status"] in ("budget_blocked", "budget_exhausted") for job in [
                        *self.evaluations.jobs.values(), *self.state.get("challenge_queue", {}).values()]):
                    self.state.update(status="budget_exhausted", error="Some candidate evaluations lack their declared pair/time allocation.")
                elif repair_only and any(job["status"] not in (
                        "accepted_for_next_run", "accepted_engine_revision", "resolved_by_revision", "rejected")
                        for job in self.state.get("repair_jobs", {}).values()):
                    self.state.update(status="blocked", blocked_component="maintenance",
                                      error="Maintenance still has unaccepted repairs; returning a solution is not acceptance.")
                elif self.state.get("comparison_issues"):
                    self.state.update(status="blocked", error="Research episodes completed, but frozen-opponent contract failures block formal comparison and promotion.")
                else:
                    self.state["status"] = "target_met" if self.state["goal"]["status"] == "target_met" else "completed"
            except EngineRevisionReady as error:
                self.state.update(status="maintenance_ready", error=str(error))
            except EvolutionStopped as error:
                self.state.update(status="cancelled" if (self.directory / "cancel-requested").exists() else "budget_exhausted", error=str(error))
            except EvaluationBlocked as error:
                self.state.update(status="blocked", error=str(error))
            except DeliveryBlocked as error:
                self.state.update(status="blocked", blocked_component="research_delivery", error=str(error))
            except KeyboardInterrupt:
                self.state.update(status="interrupted", error="Interrupted; completed pair cache and fresh round inputs are retained.")
            except (OSError, KeyError, IndexError, TypeError) as error:
                from bridge_evolve.evolution.repairs import record_controller_incident
                incident = record_controller_incident(self, error)
                self.state.update(status="failed", error=str(error), recovery_incident=incident)
            except (ValueError, RuntimeError, sqlite3.Error) as error:
                self.state.update(status="failed", error=str(error))
            finally:
                if (self.evaluator is not None and self.state["status"] not in ("cancelled", "interrupted")
                        and not repair_only and engine_identity() == self.state["engine_hash"]):
                    try:
                        self.process_evaluations()
                    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                        self.state["evaluation_error"] = str(error)
                        self.milestone("queue-stop:" + str(self.state["agent_requests"]), "evaluation_stopped", error=str(error))
                        if self.state["status"] in ("completed", "target_met"):
                            self.state.update(status="budget_exhausted" if isinstance(error, EvolutionStopped) else "failed",
                                              error=str(error))
                    try:
                        for number in list(self.state.get("growth_checks", {})):
                            self.verify_growth(int(number))
                    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                        self.state["growth_error"] = str(error)
                        if self.state["status"] in ("completed", "target_met"):
                            self.state.update(status="failed", error=str(error))
                    if self.config.get("research_architecture") == "director":
                        self.process_repairs()
                if self.state["status"] == "running":
                    self.state.update(status="failed", error="Unexpected interruption; inspect the original exception. No result was finalized.")
                if self.config.get("campaign_manager"):
                    CampaignManager(self).finalize()
                if self.evaluator is not None:
                    self.evaluator.close()
                self.state["activity"] = {"kind": "stopped", "status": self.state["status"], "started_at": timestamp()}
                self.milestone("run-stop:" + str(self.state["agent_requests"]) + ":" + self.state["status"], "run_stopped",
                               status=self.state["status"], error=self.state.get("error"))
                self.checkpoint()
                self.report()
                fcntl.flock(lock, fcntl.LOCK_UN)
        if (self.config.get("auto_continue_repairs") and self.state["status"] not in ("cancelled", "interrupted")):
            from bridge_evolve.evolution.handoff import prepare
            try:
                prepare(self)
                self.report()
            except (OSError, ValueError, RuntimeError) as error:
                self.state["continuation_error"] = str(error)
                self.checkpoint()
        return self.state


def run(directory: Path, *, goal_only=False, evaluation_only=False, repair_only=False) -> dict:
    result = Controller(directory).run(goal_only=goal_only, evaluation_only=evaluation_only, repair_only=repair_only)
    while result.get("continuation") and result["status"] == "continued":
        handoff = result["continuation"]
        if handoff.get("engine_revision"):
            from bridge_evolve.evolution.handoff import run_engine_handoff
            try:
                return run_engine_handoff(handoff)
            except (OSError, ValueError, RuntimeError) as error:
                controller = Controller(directory)
                controller.state.update(status="blocked", continuation_error=str(error))
                controller.state["continuation"].update(status="failed", error=str(error))
                controller.checkpoint()
                controller.report()
                return controller.state
        directory = Path(handoff["path"])
        result = Controller(directory).run()
    return result
