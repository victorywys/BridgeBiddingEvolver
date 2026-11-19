"""Durable development evaluations, independent of further model requests."""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evaluation.statistics import paired_summary
from bridge_evolve.evolution.execution import EvolutionStopped, comparison_changes
from bridge_evolve.evolution.feedback import bounded, short_text
from bridge_evolve.evolution.faults import score_contributions
from bridge_evolve.systems.inputs import parse_board

if TYPE_CHECKING:
    from bridge_evolve.evolution.controller import Controller

ACTIVE = {"allocating", "queued", "running"}
LOGGER = logging.getLogger(__name__)


class EvaluationQueue:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.state, self.config = controller.state, controller.config
        self.jobs = self.state.setdefault("evaluation_queue", {})

    def held_seconds(self):
        return sum(job["remaining_seconds"] for job in self.jobs.values() if job["status"] in ACTIVE)

    def enqueue(self, release_path, *, lineage, generation=0, source=None):
        release = self.controller.read_artifact(self.controller.directory / release_path)
        if release is None:
            raise ValueError("A published release is missing.")
        if (not isinstance(release, dict) or not {"hash", "parent", "test_definitions", "cases"} <= release.keys()
                or not isinstance(release["test_definitions"], list) or not isinstance(release["cases"], dict)):
            raise ValueError("A frozen release requires executable test_definitions and complete cases.")
        candidate, parent = release["hash"], release["parent"]
        self.controller.system(candidate)
        self.controller.system(parent)
        regression_hash = canonical_hash({key: release[key] for key in ("test_definitions", "cases")})
        intent = {
            "candidate": candidate, "parent": parent, "engine": self.state["engine_hash"],
            "baselines": [item["hash"] for item in self.config["baselines"]],
            "boards": {key: self.config["boards"][key] for key in ("validity", "screen", "full")},
            "scoring": "two-table-family-imp/v1", "stage": "development", "regressions": regression_hash,
            "module_contract": self.state.get("module_contract", {}).get("hash"),
        }
        if self.config.get("development_sampling") is not None:
            intent["development_sampling"] = self.config["development_sampling"]
        if self.config.get("protocol_goal") is not None:
            intent["protocol_goal"] = self.config["protocol_goal"]
        if self.config.get("baseline_fault_policy", "strict") != "strict":
            intent["baseline_fault_policy"] = self.config["baseline_fault_policy"]
        identity = "q-" + canonical_hash(intent)[:20]
        occurrence = {"release": release_path, "generation": generation, "source": source}
        if identity in self.jobs:
            job = self.jobs[identity]
            if occurrence not in job["occurrences"]:
                job["occurrences"].append(occurrence)
                self.controller.checkpoint()
            return job
        pairs = 2 * len(self.config["baselines"]) * sum(self.config["boards"][key] for key in ("validity", "screen", "full"))
        seconds = self.config["evaluation_seconds"]
        used_time = self.controller.previous_seconds + time.monotonic() - self.controller.started - self.state.get("maintenance_seconds", 0)
        held_pairs = sum(job["remaining_pairs"] for job in self.jobs.values() if job["status"] in ACTIVE)
        enough = (self.state["evaluated_pairs"] + held_pairs + pairs <= self.config["max_pairs"]
                  and used_time + self.held_seconds() + self.controller.growth_seconds_held() + seconds <= self.config["max_seconds"])
        job = {
            "id": identity, "candidate": candidate, "parent": parent, "lineage": lineage,
            "release": release_path, "regression_hash": regression_hash,
            "research_resources_at_publication": release.get("research_resources_at_publication"),
            "occurrences": [occurrence], "created_at": timestamp(),
            "status": "allocating" if enough else "budget_blocked", "rounds": {},
            "reserved_pairs": pairs, "remaining_pairs": pairs if enough else 0,
            "reserved_seconds": seconds, "remaining_seconds": seconds if enough else 0, "spent_seconds": 0,
        }
        self.jobs[identity] = job
        self.controller.checkpoint()
        if enough:
            if self.controller.evaluator is not None:
                try:
                    self.controller.evaluator.reserve_evaluation(identity, pairs)
                except EvolutionStopped as error:
                    job.update(status="budget_blocked", remaining_pairs=0, remaining_seconds=0, error=str(error))
                else:
                    job["status"] = "queued"
            else:
                job["status"] = "queued"
        else:
            job["error"] = "Insufficient unreserved pair/time allowance; the frozen release remains available."
        self.controller.milestone("queue:" + identity, "evaluation_queued", evaluation=identity,
                                  candidate=candidate, status=job["status"], reserved_pairs=pairs)
        self.controller.checkpoint()
        return job

    def _regressions(self, job):
        if "regressions" in job:
            saved = self.controller.read_artifact(self.controller.directory / job["regressions"])
            if saved is None:
                raise ValueError("A completed release regression report is missing.")
            return saved
        from bridge_evolve.evolution.research_tools import ResearchTools
        release = self.controller.read_artifact(self.controller.directory / job["release"])
        identity = "eval-" + job["id"]
        job["test_session"] = identity
        job["test_workspace"] = f"research/{identity}/workspace.json"
        self.controller.checkpoint()
        tools = ResearchTools(self.controller, identity, job["parent"], {"selected_groups": []},
                              self.controller.system(job["candidate"]).to_data(), None)
        try:
            for case in release["cases"].values():
                parse_board(case["board"])
            tools.data["cases"] = copy.deepcopy(release["cases"])
            tools.do_write_tests({"tests": release["test_definitions"]}, None, None)
            tools.save()
            result = tools.test_results()
        except (ValueError, KeyError, TypeError) as error:
            LOGGER.warning("Release regression contract rejected for %s: %s", job["id"], short_text(error))
            result = {"tests": len(release["test_definitions"]), "passed": 0, "failed": 1, "change_witnesses": 0,
                      "system_hash": job["candidate"],
                      "failures": [{"type": type(error).__name__, "error": short_text(error),
                                    "details_ref": tools.full_result({"error": str(error), "release": release})}]}
        tools.data["last_tests"] = result
        tools.save()
        path = self.controller.directory / "evaluations" / job["id"] / "regressions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.controller.artifact(path, result)
        job["regressions"] = path.relative_to(self.controller.directory).as_posix()
        self.controller.checkpoint()
        return result

    def _evaluate(self, job):
        contract = self.controller.research_contract
        if contract is not None:
            result = contract.check(self.controller.system(job["candidate"]), stop=self.controller.stop)
            job["module_contract" if self.controller.module_contract is not None else "protocol_goal"] = result
            if result["status"] != "passed":
                return {"status": "invalid", "error": "Frozen module contract failed.", "module_contract": result}
        tests = self._regressions(job)
        job["tests"] = tests
        if not tests["tests"] or tests["failed"] or not tests["change_witnesses"]:
            return {"status": "invalid", "error": "The release's actual regression/witness gate failed.", "tests": tests}
        if self.controller.protocol_goal is not None and not tests.get("protocol_witnesses"):
            return {"status": "invalid", "error": "The natural opening-Pass protocol witness was not reproduced.", "tests": tests}
        if self.controller.module_contract is not None:
            release = self.controller.read_artifact(self.controller.directory / job["release"])
            if not any(test.get("changed_from_parent") and test["mode"] == "auction" and not test.get("prefix")
                       for test in release["test_definitions"]):
                return {"status": "invalid", "error": "The controlled module release has no natural whole-auction behavior witness."}
        candidate, parent = job["candidate"], job["parent"]
        systems = list(dict.fromkeys([candidate, parent]))
        for stage in ("validity", "screen", "full"):
            identity = job["id"] + "-" + stage
            job["stage"] = stage
            job["rounds"][stage] = identity
            self.controller.checkpoint()
            result = self.controller.round(identity, systems, stage)
            job.setdefault("round_resources", {})[stage] = result.get("resources")
            if result["status"][candidate] != "checked" or result["status"][parent] != "checked":
                blocked = "blocked" in (result["status"][candidate], result["status"][parent])
                errors = result["errors"][candidate] + result["errors"][parent]
                return {"status": "comparison_blocked" if blocked or result["status"][parent] != "checked" else "invalid",
                        "error_count": len(errors), "errors": errors[:8], "round": identity}
            if stage == "screen":
                job["delta"] = result["objective"][candidate] - result["objective"][parent]
        changes = comparison_changes(self.controller.directory, result, candidate, parent)
        delta = result["objective"][candidate] - result["objective"][parent]
        status = "insufficient_coverage" if not changes["changed_families"] else "no_full_gain" if delta < 0 else "development_complete"
        outcome = {"status": status, "full_delta": delta, "changes": changes, "round": result["id"],
                   "objective": result["objective"][candidate], "means": result["means"][candidate],
                   "scope": "development feedback, not independent promotion evidence"}
        if "protocol_coverage" in result:
            outcome["protocol_coverage"] = result["protocol_coverage"][candidate]
        contributions = score_contributions(result, candidate, parent)
        if contributions is not None:
            outcome["score_contributions"] = contributions
        sampling = self.config.get("development_sampling")
        if sampling is not None:
            anchors = result["anchors"]
            values = [sum(result["scores"][candidate][anchor][index] - result["scores"][parent][anchor][index]
                          for anchor in anchors) / len(anchors) for index in range(result["boards"])]
            statistics = paired_summary(values, seed=result["seed"], resamples=self.config["resamples"])
            statistics.pop("tables")
            statistics["sample_unit"] = "One shared base family; opponent-mix paired candidate-minus-parent IMP."
            outcome.update(evaluation_scope="conditional", development_sampling=sampling, statistics=statistics,
                           scope="Frozen-cohort competitive development: candidate-minus-parent paired IMP, not global strength or promotion evidence.")
        return outcome

    def process(self):
        evaluator = self.controller.evaluator
        if evaluator is None:
            raise RuntimeError("Queued evaluation requires the trusted evaluator, not a model client.")
        self.controller.processing_evaluations = True
        try:
            for job in self.jobs.values():
                if job["status"] not in ACTIVE:
                    continue
                if job["status"] == "running" and job.get("lease_updated_at"):
                    unknown = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(job["lease_updated_at"])).total_seconds())
                    charge = min(job["remaining_seconds"], unknown)
                    job["remaining_seconds"] -= charge
                    job["spent_seconds"] = job["reserved_seconds"] - job["remaining_seconds"]
                    self.controller.previous_seconds += charge
                    job.pop("lease_updated_at", None)
                    if not job["remaining_seconds"]:
                        evaluator.release_evaluation(job["id"])
                        job.update(status="budget_exhausted", remaining_pairs=0,
                            outcome={"status": "budget_exhausted", "error": "An interrupted evaluation's conservative time lease is exhausted."})
                        self.controller.checkpoint()
                        continue
                self.controller.stop()
                evaluator.reserve_evaluation(job["id"], job["reserved_pairs"])
                started = time.monotonic()
                remaining = job["remaining_seconds"]
                old_stop = evaluator.stop
                self.controller.evaluation_deadline = started + remaining
                self.controller.evaluation_job = job

                def stop_job():
                    self.controller.stop()
                    elapsed = time.monotonic() - started
                    job["remaining_seconds"] = max(0, remaining - elapsed)
                    if elapsed >= remaining:
                        raise EvolutionStopped("The candidate evaluation time allocation is exhausted.")

                evaluator.stop = stop_job
                evaluator.active_reservation = job["id"]
                job.update(status="running", stage="regressions")
                job["lease_updated_at"] = timestamp()
                self.state["phase"] = job["id"] + "-regressions"
                self.state["activity"] = {"kind": "evaluation", "evaluation": job["id"],
                                          "stage": "regressions", "started_at": timestamp()}
                self.controller.checkpoint()
                try:
                    outcome = self._evaluate(job)
                except EvolutionStopped as error:
                    if (self.controller.directory / "cancel-requested").exists() or time.monotonic() - started < remaining:
                        job["status"] = "queued"
                        raise
                    outcome = {"status": "budget_exhausted", "error": str(error)}
                except (OSError, ValueError, RuntimeError) as error:
                    job["status"] = "queued"
                    job["last_error"] = short_text(error)
                    raise
                finally:
                    elapsed = time.monotonic() - started
                    job["remaining_seconds"] = max(0, remaining - elapsed)
                    job["spent_seconds"] = job["reserved_seconds"] - job["remaining_seconds"]
                    job["remaining_pairs"] = evaluator.remaining_evaluation_pairs(job["id"])
                    evaluator.stop = old_stop
                    evaluator.active_reservation = None
                    self.controller.evaluation_deadline = None
                    self.controller.evaluation_job = None
                    job.pop("lease_updated_at", None)
                    self.controller.checkpoint()
                job.update(outcome=outcome, status=outcome["status"], finished_at=timestamp(),
                           remaining_seconds=0, remaining_pairs=0)
                evaluator.release_evaluation(job["id"])
                self.controller.milestone("queue-result:" + job["id"], "candidate_evaluated", evaluation=job["id"],
                    candidate=job["candidate"], result=bounded(outcome, limit=8000))
                self.controller.checkpoint()
        finally:
            self.controller.processing_evaluations = False

    def import_sources(self):
        from bridge_evolve.evolution.controller import register_system, status
        from bridge_evolve.evolution.inspection import checked_artifact, frozen_system_export
        for source in self.config["evaluation_sources"]:
            path = Path(source)
            if path.is_dir():
                previous = status(path)
                if previous["status"] in ("running", "queued"):
                    raise ValueError("Stop the source run before importing frozen releases.")
                previous_config = json.loads((path / "config.json").read_text())
                if canonical_hash(previous_config) != previous["config_hash"]:
                    raise ValueError("Source evaluation configuration identity mismatch.")
                baselines = previous_config["baselines"]
                releases = []
                seen_paths = set()
                for session, metadata in previous.get("research_sessions", {}).items():
                    workspace = checked_artifact(path, metadata["workspace"])
                    for item in workspace["releases"]:
                        if item["path"] in seen_paths:
                            continue
                        seen_paths.add(item["path"])
                        releases.append((checked_artifact(path, item["path"]),
                            {"run": previous["id"], "release": item["path"], "engine": previous["engine_hash"],
                             "model": previous_config["model"], "session": session, "lineage": metadata["lineage"]}))
                for job in previous.get("evaluation_queue", {}).values():
                    if job["release"] in seen_paths:
                        continue
                    seen_paths.add(job["release"])
                    release = checked_artifact(path, job["release"])
                    releases.append((release, {"run": previous["id"], "release": job["release"],
                        "engine": previous["engine_hash"], "model": previous_config["model"],
                        "session": release["origin"], "lineage": job["lineage"],
                        "execution_mode": previous.get("execution_mode"), "publication_sources": job["occurrences"]}))

                def frozen(identity):
                    return json.loads(frozen_system_export(path, identity, "json"))
            else:
                index = json.loads(path.read_text())
                if index.get("schema") != "bridge-published-research-archive/v1":
                    raise ValueError("Portable evaluation input must be a published research archive index.")
                baselines = index.get("source_baselines")
                releases = []
                for item in index["snapshots"]:
                    if "release" not in item:
                        continue
                    release_path = (path.parent / item["release"]).resolve()
                    if not release_path.is_relative_to(path.parent.resolve()):
                        raise ValueError("A published release must stay inside its archive.")
                    release = json.loads(release_path.read_text())
                    if release["hash"] != item["hash"] or release["parent"] != item["parent"]:
                        raise ValueError("Portable release identity differs from its manifest.")
                    releases.append((release, {"run": item["source_run"], "release": item["release"],
                        "engine": item["source_engine"], "model": item["source_model"], "session": release["origin"]}))

                def frozen(identity):
                    file = path.parent / f"{identity}.json"
                    return json.loads(file.read_text())
            current = self.config["baselines"]
            changed = baselines is None or [item["hash"] for item in baselines] != [item["hash"] for item in current]
            if changed:
                if not self.config["benchmark_revision_reason"]:
                    raise ValueError("Imported releases require an explicit benchmark_revision_reason for changed/unknown references.")
                source_run = previous["id"] if path.is_dir() else next(
                    (item["source_run"] for item in index["snapshots"] if item.get("source_run")), "published-archive")
                self.state["benchmark_revision"] = {"source_run": source_run, "source_archive": str(path),
                    "reason": self.config["benchmark_revision_reason"], "source_reference_unknown": baselines is None,
                    "previous": baselines, "current": current, "historical_comparison_issues": previous.get("comparison_issues", {}) if path.is_dir() else {}}
            elif path.is_dir():
                from bridge_evolve.evolution.faults import archive_legacy_baseline_issues
                inherited = archive_legacy_baseline_issues(self.state, self.config, previous.get("comparison_issues", {}),
                    source_run=previous["id"], previous_policy=previous_config.get("baseline_fault_policy", "strict"))
                self.state.setdefault("comparison_issues", {}).update(inherited)
            for release, provenance in releases:
                for identity in (release["parent"], release["hash"]):
                    raw = frozen(identity)
                    system = load_system_data(raw, identity)
                    register_system(self.controller.directory, self.state, system)
                lineage = provenance.get("lineage", provenance["session"].rsplit("-", 1)[-1])
                if lineage not in ("A", "B", "Z"):
                    raise ValueError("Imported release has an unknown lineage.")
                incumbent = self.state["champions"].get(lineage)
                if lineage == "Z" and incumbent == self.state["origin_hashes"].get("Z"):
                    self.state["champions"][lineage] = release["parent"]
                    self.state["initial_champions"][lineage] = release["parent"]
                target = self.controller.directory / "imported-releases" / (release["hash"] + "-" + canonical_hash(release)[:12] + ".json")
                target.parent.mkdir(exist_ok=True)
                existing = self.controller.read_artifact(target)
                if existing is None:
                    self.controller.artifact(target, release)
                elif canonical_hash(existing) != canonical_hash(release):
                    raise ValueError("Duplicate candidate releases disagree on frozen regression provenance.")
                self.enqueue(target.relative_to(self.controller.directory).as_posix(), lineage=lineage, source=provenance)
        self.controller.checkpoint()


def load_system_data(raw, identity):
    from bridge_evolve.systems import validate_system
    system = validate_system(raw)
    if system.content_hash != identity or system.version != 2 or system.scope != "general":
        raise ValueError("Imported executable protocol differs from its frozen identity.")
    return system
