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


def initialize(config: dict, output: Path | None = None, project: Path = ROOT, *, preloaded: dict | None = None) -> Path:
    from bridge_evolve.systems import load_system
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
                    parent = register_system(directory, state, system)
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
            selected = copy.deepcopy({key: workspace[key] for key in ("system", "tests", "cases", "notes", "last_tests", "last_trial") if key in workspace})
            if parent != source_parent:
                for test in selected["tests"]:
                    test.pop("changed_from_parent", None)
                selected["notes"].append({"kind": "research", "text": "本次采用明确修订后的参考体系；工作区来自旧父本。请对照新的parent修复旧公开协议，重跑继承测试，并补充新的行为变化见证。旧试验不作为本次成绩。"})
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
                state["comparison_issues"] = inherited_issues
    state["initial_champions"] = dict(state["champions"])
    if normalized["replay"] is not None:
        contents = Path(normalized["replay"]).read_bytes()
        (directory / "replay.json").write_bytes(contents)
        state["system_files"]["replay.json"] = hashlib.sha256(contents).hexdigest()
    save_json(directory / "config.json", normalized)
    save_json(directory / "state.json", state)
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

    def load_frozen_state(self):
        self.config = json.loads((self.directory / "config.json").read_text(encoding="utf8"))
        self.state = status(self.directory)
        if canonical_hash(self.config) != self.state["config_hash"] or engine_identity() != self.state["engine_hash"]:
            raise ValueError("Evolution config or trusted engine differs from the frozen run; start a new experiment.")
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
        if (self.directory / "cancel-requested").exists():
            raise EvolutionStopped("Cancellation requested.")
        if self.previous_seconds + time.monotonic() - self.started >= self.config["max_seconds"]:
            raise EvolutionStopped("Wall-clock budget exhausted.")

    def system(self, identity):
        return frozen_system(str(self.directory / "systems"), identity)

    def artifact(self, path: Path, value: dict):
        save_json(path, value)
        self.state["artifacts"][path.relative_to(self.directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.checkpoint()

    def read_artifact(self, path: Path):
        key = path.relative_to(self.directory).as_posix()
        digest = self.state["artifacts"].get(key)
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
            if self.config["source"] == "research" and errors and all(
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
                packet = evidence_packet(self.system(parent), self.evaluator.records(diagnosis, parent), self.config["modules"])
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
                    promotion_key = entry["lineage"] + "-" + identity[:12] if research else entry["lineage"]
                    existing = generation["promotions"].get(promotion_key)
                    if research and any(value["status"] == "promoted" and value.get("lineage") == entry["lineage"]
                                        for value in generation["promotions"].values()):
                        entry["status"] = "portfolio_retained"
                        continue
                    if existing is not None and existing["status"] != "allocated":
                        if research and entry.get("status") == "eligible":
                            entry["status"] = "portfolio_retained"
                        continue
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
                    if self.state.get("comparison_issues"):
                        entry.update(status="comparison_blocked",
                                     error="Known frozen-opponent contract failures remain unresolved; research continues but formal promotion is blocked.")
                        continue
                    attempts = self.state["promotion_attempts"]
                    if existing is None and attempts >= self.config["max_promotions"]:
                        raise EvolutionStopped("Fresh promotion attempt budget exhausted.")
                    promotion = generation["promotions"].setdefault(promotion_key, {
                        "attempt": attempts + 1, "candidate": identity, "parent": parent, "status": "allocated",
                        "lineage": entry["lineage"]})
                    if promotion["status"] != "allocated":
                        continue
                    if promotion["attempt"] > attempts:
                        self.state["promotion_attempts"] = promotion["attempt"]
                    self.checkpoint()
                    result = self.round(f"g{number:03d}-promotion-{promotion_key}", [identity, parent], "promotion")
                    if result["status"][identity] != "checked" or result["status"][parent] != "checked":
                        promotion["status"] = "blocked" if "blocked" in (result["status"][identity], result["status"][parent]) else "invalid"
                        if result["status"][identity] != "checked":
                            self.invalid_candidate(entry, result)
                        elif result["status"][parent] != "checked":
                            entry.update(status="comparison_blocked", error="The incumbent failed the fresh promotion comparison.")
                    else:
                        gain = paired_gain(result, identity, parent, result["anchors"], seed=result["seed"],
                                           alpha=self.config["promotion_alpha"] / self.config["max_promotions"],
                                           resamples=self.config["resamples"])
                        promotion["statistics"] = gain
                        accepted = (gain["lower"] > 0 and gain["mean"] >= self.config["min_promotion_delta"]
                                    and min(gain["opponent_deltas"]) >= -self.config["max_opponent_regression"])
                        promotion["status"] = "promoted" if accepted else "not_confirmed"
                        entry["status"] = promotion["status"]
                        if accepted:
                            self.state["champions"][entry["lineage"]] = identity
                    self.milestone("promotion:" + str(promotion["attempt"]), "promotion",
                                   generation=number, lineage=entry["lineage"], **{
                                       key: promotion[key] for key in ("attempt", "candidate", "parent", "status", "statistics") if key in promotion})
                    self.checkpoint()
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
        lines = ["# 叫牌体系自进化", "", self.state["name"], "",
                 f"状态：{self.state['status']}；已完成{self.state['generation']}代。",
                 f"提案来源：{self.config['source']}；Agent请求{self.state['agent_requests']}次；已保留配对预算{self.state['evaluated_pairs']}。",
                 f"模型：{self.config['model']}；请求上限{self.config['max_agent_requests']}；配对上限{self.config['max_pairs']}；总时间上限{self.config['max_seconds']}秒。",
                 "", "每个评测轮次独立随机生成新牌，同轮候选和对手使用共同牌；没有预留测试集。",
                 "", "## 当前冠军", "", *[f"- {label}：`{identity}`" for label, identity in self.state["champions"].items()],
                 "", "## 代际结果", ""]
        for number, generation in self.state["generations"].items():
            lines.append(f"- 第{number}代：{generation['status']}，{len(generation['candidates'])}个提案，"
                         f"{sum(value['status'] == 'promoted' for value in generation['promotions'].values())}次晋级。")
        if self.state.get("model_usage"):
            lines.extend(["", "## 模型用量", "", "```json", json.dumps(self.state["model_usage"], ensure_ascii=False, indent=2), "```",
                          "", "token先保守预留，收到实际用量后结算；未知用量保留预留额。没有可靠价格数据，不伪造货币费用。"])
        if self.state.get("benchmark_revision"):
            lines.extend(["", "## 明确修订的参考体系", "", self.state["benchmark_revision"]["reason"],
                          "", "旧参考、错误牌、模型用量和成绩仍在来源实验保存；本次使用新参考重新发牌，不把两种参考的成绩拼接。"])
        if self.state.get("research_sessions"):
            lines.extend(["", "## 自主研究", ""])
            for identity, session in self.state["research_sessions"].items():
                lines.append(f"- {identity}：{session['status']}，{session['turns']}轮，{session['tool_calls']}次工具，{session['releases']}个完整候选；[{session['model']}]({session['path']})。")
        if self.state.get("comparison_issues"):
            lines.extend(["", "## 正式比较受阻", "", "固定对手存在未解决的公开协议/执行缺陷。研究可以继续，但失败牌不删除、不补零分，受阻成绩不排名，也不确认晋级或万副达标。", ""])
            for identity, issue in self.state["comparison_issues"].items():
                lines.append(f"- {identity}：{issue['error_count']}项检查问题；原始证据保留。")
        lines.extend(["", "## 10000副目标", "", "```json", json.dumps(self.state["goal"], ensure_ascii=False, indent=2), "```"])
        if self.state.get("error"):
            lines.extend(["", "## 停止原因", "", self.state["error"]])
        lines.extend(["", "运行完成不代表已经有提升；仅完整通过新牌确认的候选更新冠军。原始两个锚点不变。", ""])
        (self.directory / "report.md").write_text("\n".join(lines), encoding="utf8")

    def run(self, *, goal_only=False):
        lock_path = self.directory / "controller.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("This evolution experiment already has a running controller.") from error
            self.load_frozen_state()
            self.state["status"] = "running"
            self.state.pop("error", None)
            self.checkpoint()
            try:
                self.evaluator = Evaluator(self.directory, self.config, self.state, self.checkpoint, self.stop)
                if not goal_only:
                    for number in range(1, self.config["generations"] + 1):
                        self.stop()
                        self.generation(number)
                        plateau = self.config.get("plateau_limit", 3)
                        if plateau and self.state["plateau_generations"] >= plateau:
                            break
                if goal_only or self.config["automatic_goal_check"]:
                    self.verify_goal(force=goal_only)
                if self.state.get("comparison_issues"):
                    self.state.update(status="blocked", error="Research episodes completed, but frozen-opponent contract failures block formal comparison and promotion.")
                else:
                    self.state["status"] = "target_met" if self.state["goal"]["status"] == "target_met" else "completed"
            except EvolutionStopped as error:
                self.state.update(status="cancelled" if (self.directory / "cancel-requested").exists() else "budget_exhausted", error=str(error))
            except EvaluationBlocked as error:
                self.state.update(status="blocked", error=str(error))
            except KeyboardInterrupt:
                self.state.update(status="interrupted", error="Interrupted; completed pair cache and fresh round inputs are retained.")
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                self.state.update(status="failed", error=str(error))
            finally:
                if self.state["status"] == "running":
                    self.state.update(status="failed", error="Unexpected interruption; inspect the original exception. No result was finalized.")
                if self.evaluator is not None:
                    self.evaluator.close()
                self.state["activity"] = {"kind": "stopped", "status": self.state["status"], "started_at": timestamp()}
                self.milestone("run-stop:" + str(self.state["agent_requests"]) + ":" + self.state["status"], "run_stopped",
                               status=self.state["status"], error=self.state.get("error"))
                self.checkpoint()
                self.report()
                fcntl.flock(lock, fcntl.LOCK_UN)
        return self.state


def run(directory: Path, *, goal_only=False) -> dict:
    return Controller(directory).run(goal_only=goal_only)
