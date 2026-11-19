"""Event-driven, isolated baseline completion proposals with independent review."""

import copy
import hashlib
import json
import logging
import re
import shutil
import traceback
from pathlib import Path

from bridge_evolve.core import Seat
from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evolution.director import structured_request
from bridge_evolve.evolution.execution import EvolutionStopped, audit_module
from bridge_evolve.evolution.faults import baseline_branch_fault
from bridge_evolve.evolution.research_tools import OBJECT, STRING, tool
from bridge_evolve.systems import decide, validate_system
from bridge_evolve.systems.inputs import parse_board, run_auction
from bridge_evolve.systems.language import SystemError
from bridge_evolve.systems.runtime import Observation
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld

LOGGER = logging.getLogger(__name__)
PROPOSAL = tool("propose_baseline_completion", "Append only a missing baseline decision branch; no host files, scoring, existing protocol or candidate edits.",
    {"basis": STRING, "blocker": STRING, "rules": {"type": "array", "maxItems": 8, "items": OBJECT}},
    ("basis", "rules"))
REVIEW = tool("review_baseline_completion", "Independently review the declared system basis and scope; reject candidate-favoring or unsupported completions.",
              {"approve": {"type": "boolean"}, "reason": STRING}, ("approve", "reason"))
ENGINE_REQUEST = tool("request_engine_revision", "Report a reproducible framework defect and a bounded repair/validation plan. No active source or scoring edits.",
    {"diagnosis": STRING, "repair_plan": STRING, "required_tests": {"type": "array", "minItems": 1, "maxItems": 8, "items": STRING}},
    ("diagnosis", "repair_plan", "required_tests"))

REPAIRABLE = {"pending", "proposing", "validating", "reviewing", "requires_review",
              "requires_engine_revision", "engine_revision_proposed", "engineering", "deferred", "waiting_dependency",
              "solution_returned", "delivery_failed"}


def baseline_policy_fault(record, error, candidate, opponent):
    if (error.get("system_hash") != opponent
            or error.get("stage") in ("dds", "score", "scoring", "execution")
            or error.get("actor") not in ("N", "E", "S", "W")):
        return False
    table = next((row for row in record["tables"] if row["id"] == error.get("table")), None)
    if table is None:
        return False
    side = Seat(error["actor"]).partnership.value
    return table["systems"].get(side, {}).get("hash") == opponent


def record_incident(controller, record, candidate, opponent, round_id, *, baseline_owner=None, research_session=None):
    errors = record.get("adjudication", {}).get("errors", []) or record.get("audit_result", {}).get("errors", [])
    eligible = {item["hash"] for item in controller.config["baselines"]}
    eligible.update(version["hash"] for version in controller.state.get("repair_versions", {}).values())
    if baseline_owner is not None:
        if baseline_owner not in eligible:
            raise ValueError("A maintenance owner must be one of this experiment's baseline versions.")
        eligible.add(opponent)
    recorded = []
    if research_session is None:
        research_session = next((identity for identity in controller.state.get("research_sessions", {})
                                 if round_id.startswith("r-" + identity + "-")), None)
    for error in errors:
        completion = baseline_branch_fault(record, error, candidate, opponent)
        baseline = opponent in eligible and (completion or baseline_policy_fault(record, error, candidate, opponent))
        if not baseline and error.get("stage") not in ("dds", "score", "scoring", "execution"):
            continue
        kind = "baseline-completion" if completion else "baseline-policy" if baseline else "framework"
        site = re.search(r"(?:[Rr]ule[= ]+)([\w.-]+)", error.get("message", ""))
        identity = "repair-" + canonical_hash({"kind": kind, "owner": error.get("system_hash"),
            "stage": error.get("stage"), "message": site[1].rstrip(".") if site else error.get("message")})[:20]
        jobs = controller.state.setdefault("repair_jobs", {})
        if identity not in jobs:
            path = controller.directory / "repairs" / identity
            path.mkdir(parents=True, exist_ok=True)
            owner = baseline_owner or next((old for old, version in controller.state.get("repair_versions", {}).items()
                                           if version["hash"] == opponent), opponent)
            job = {"id": identity, "kind": kind, "status": "pending" if baseline else "requires_engine_revision",
                   "baseline": owner if baseline else None, "created_at": timestamp(), "occurrences": 0,
                   "source_baseline": opponent if baseline else None,
                   "error": copy.deepcopy(error), "evidence": [],
                   "permissions": ("repair-protocol-with-regression-review" if (baseline and
                       controller.config.get("research_budget_mode") == "campaign") or kind == "baseline-policy" else
                       "append-refusal-completion-only" if baseline else "isolated-engine-repair-and-tests")}
            jobs[identity] = job
            controller.milestone(identity, "repair_incident", repair=identity, summary=error.get("message"))
        job = jobs[identity]
        recorded.append(identity)
        if research_session:
            reference = {"run": controller.state["id"], "session": research_session, "operation": round_id}
            if reference not in job.setdefault("blocked_research", []):
                job["blocked_research"].append(reference)
        occurrence = canonical_hash({"round": round_id, "candidate": candidate, "opponent": opponent,
                                     "board": {key: record[key] for key in ("dealer", "vulnerability", "hands")
                                               if key in record}, "error": error})
        if occurrence in job.setdefault("occurrence_ids", []):
            continue
        job["occurrence_ids"].append(occurrence)
        job["occurrences"] += 1
        if job["status"] in ("accepted_for_next_run", "resolved_by_revision"):
            job.update(status="pending", iteration=job.get("iteration", 1) + 1,
                       next_action="出现新的独立反例，重新验证已交付工作稿。")
        name = f"repairs/{identity}/evidence-{len(job['evidence']) + 1}.json"
        job["evidence"].append(name)
        controller.artifact(controller.directory / name, {
            "board": {key: record[key] for key in ("dealer", "vulnerability", "hands")},
            "systems": {table["id"]: {side: value["hash"] for side, value in table["systems"].items()}
                        for table in record["tables"]},
            "candidate": candidate, "opponent": opponent, "error": error, "round": round_id,
            "family_id": record.get("family_id"),
            "tables": copy.deepcopy(record["tables"]),
        })
    return recorded


def record_tool_incident(controller, evidence):
    error = copy.deepcopy(evidence["error"])
    from bridge_evolve.evolution.config import ROOT
    error["message"] = error["message"].replace(str(controller.directory), "<run>").replace(str(ROOT), "<engine>")
    identity = "repair-" + canonical_hash({
        "kind": "framework", "tool": evidence["tool"], "error": error})[:20]
    fingerprint = identity
    jobs = controller.state.setdefault("repair_jobs", {})
    if jobs.get(identity, {}).get("status") == "resolved_by_revision":
        identity = "repair-" + canonical_hash({"incident": fingerprint, "engine": controller.state["engine_hash"]})[:20]
    job = jobs.setdefault(identity, {
        "id": identity, "kind": "framework", "status": "requires_engine_revision", "baseline": None,
        "fingerprint": fingerprint,
        "created_at": timestamp(), "occurrences": 0, "evidence": [], "error": evidence["error"],
        "permissions": "isolated-engine-repair-and-tests"})
    occurrence = canonical_hash({key: evidence[key] for key in ("run", "session", "operation")})
    if occurrence not in job.setdefault("occurrence_ids", []):
        job["occurrence_ids"].append(occurrence)
        name = f"repairs/{identity}/evidence-{len(job['evidence']) + 1}.json"
        (controller.directory / name).parent.mkdir(parents=True, exist_ok=True)
        controller.artifact(controller.directory / name, evidence)
        job["evidence"].append(name)
        job["occurrences"] += 1
        job.setdefault("blocked_research", []).append({
            key: evidence[key] for key in ("run", "session", "operation")} | {"blocking": bool(evidence.get("blocking"))})
        if job["status"] == "resolved_by_revision":
            job.update(status="requires_engine_revision", iteration=job.get("iteration", 1) + 1)
        controller.milestone(identity + ":" + occurrence, "repair_incident", repair=identity,
                             summary=evidence["error"]["message"])
    references = [ref for ref in job["blocked_research"]
                  if (ref["run"], ref["session"]) == (evidence["run"], evidence["session"])]
    if len(references) >= 2:
        for ref in references:
            ref["blocking"] = True
    if any(ref["blocking"] for ref in references):
        controller.state["recovery_incident"] = identity
    return identity


def record_controller_incident(controller, error):
    activity = controller.state.get("activity", {})
    return record_tool_incident(controller, {
        "schema": "bridge-tool-incident/v1", "run": controller.state["id"],
        "session": activity.get("session", "controller"),
        "operation": activity.get("call_id") or "controller:" + controller.state.get("phase", "startup"),
        "tool": activity.get("name", "controller"), "arguments": {}, "blocking": True,
        "error": {"stage": "controller", "type": type(error).__name__, "message": str(error)},
        "source_hints": [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                         for frame in traceback.extract_tb(error.__traceback__)[-12:]],
    })


def record_reference_incident(controller, error, session, operation):
    anchors = {item["hash"] for item in controller.config["baselines"]}
    if error.system_hash not in anchors:
        return {"owner": "research", "reason": "The failed reference is not a frozen baseline."}
    side = Seat(error.actor).partnership.value
    opponent = error.system_hash
    candidate = error.systems["EW" if side == "NS" else "NS"]
    detail = {"stage": "auction", "type": "SystemError", "message": str(error),
              "actor": error.actor, "system_hash": opponent, "table": "table1"}
    record = {**error.board, "tables": [{"id": "table1", "systems": {
        key: {"hash": value} for key, value in error.systems.items()}, "error": detail}],
        "audit_result": {"errors": [detail]}}
    jobs = record_incident(controller, record, candidate, opponent, f"tool:{session}:{operation}", research_session=session)
    return {"owner": "maintenance", "evidence_saved": True, "jobs": jobs,
            "instruction": "Do not repeat or redraw this failed reference operation. Continue independent protocol work."}


def import_incidents(controller, source):
    """Rebuild the maintenance backlog from immutable evidence, never rewrite its source."""
    from bridge_evolve.evaluation.record_log import RecordReader
    from bridge_evolve.evolution.controller import register_system, status
    from bridge_evolve.evolution.inspection import checked_artifact, frozen_system_export
    from bridge_evolve.systems import validate_system
    previous = status(source)
    imported = controller.state.setdefault("repair_sources", [])
    if previous["id"] in imported:
        return
    anchors = {item["hash"] for item in controller.config["baselines"]}
    source_config = json.loads((source / "config.json").read_text())
    if canonical_hash(source_config) != previous["config_hash"]:
        raise ValueError("Maintenance source configuration changed.")
    owners = {old["hash"]: current["hash"] for old, current in zip(
        source_config["baselines"], controller.config["baselines"], strict=True)}

    def accept(record, candidate, opponent, identity, *, owner=None):
        target = owners.get(owner or opponent, owner or opponent)
        if target not in anchors:
            return
        for table in record["tables"]:
            for definition in table["systems"].values():
                identity_hash = definition["hash"]
                if f"systems/{identity_hash}.json" not in controller.state["system_files"]:
                    register_system(controller.directory, controller.state,
                                    validate_system(json.loads(frozen_system_export(source, identity_hash, "json"))))
        if not record.get("audit_result", {}).get("errors") and record.get("adjudication", {}).get("errors"):
            record["audit_result"] = {**record["audit_result"], "errors": record["adjudication"]["errors"]}
        record_incident(controller, record, candidate, opponent, previous["id"] + ":" + identity, baseline_owner=target)

    for job in previous.get("repair_jobs", {}).values():
        for name in job["evidence"]:
            evidence = checked_artifact(source, name)
            if evidence.get("schema") == "bridge-tool-incident/v1":
                record_tool_incident(controller, evidence)
                continue
            error = evidence["error"]
            tables = evidence.get("tables") or [{"id": key, "systems": {
                side: {"hash": value} for side, value in layout.items()},
                **({"error": error} if key == error.get("table") else {})}
                for key, layout in evidence["systems"].items()]
            accept({**evidence["board"], "tables": tables, "family_id": evidence.get("family_id"),
                    "audit_result": {"errors": [error]}}, evidence["candidate"], evidence["opponent"], evidence["round"],
                   owner=job.get("baseline"))
    for identity, saved in previous["rounds"].items():
        if "summary_sha256" not in saved:
            continue
        contents = (source / "rounds" / identity / "summary.json").read_bytes()
        if hashlib.sha256(contents).hexdigest() != saved["summary_sha256"]:
            raise ValueError("Maintenance source round evidence changed.")
        summary = json.loads(contents)
        for pair, name in summary["logs"].items():
            candidate, opponent = pair.split("/")
            contribution = summary.get("score_components", {}).get(candidate, {}).get(opponent, {})
            relevant = [error for error in summary["errors"].get(candidate, [])
                        if error.get("system_hash") in (None, opponent)]
            if not relevant and not contribution.get("forfeit_pairs"):
                continue
            path = source / name
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != summary["log_sha256"][name]:
                    raise ValueError("Maintenance source pair evidence changed.")
                stream.seek(0)
                reader = RecordReader(stream)
                for entry in reader.entries(projection="index"):
                    if not (entry.record.get("audit_result", {}).get("errors")
                            or entry.record.get("adjudication", {}).get("errors")):
                        continue
                    position = stream.tell()
                    stream.seek(entry.offset)
                    record = reader.selected(stream.read(entry.length))
                    stream.seek(position)
                    accept(record, candidate, opponent, identity)
    for identity, old_job in previous.get("repair_jobs", {}).items():
        job = controller.state.get("repair_jobs", {}).get(identity)
        if job is None:
            continue
        for key in ("blocked_research", "blocked_deliveries"):
            if key in old_job:
                job[key] = copy.deepcopy(old_job[key])
        if not old_job.get("workspace"):
            continue
        prefix = f"repairs/{identity}/"
        engineering = old_job["kind"] == "framework"
        selected = {"engineering.json"} if engineering else {"workspace.json", "regression-boards.json"}
        sessions = ("engineering-", "result-") if engineering else ("implementation-", "review-", "results/")
        for name in previous["artifacts"]:
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix):]
            if tail.startswith(("evidence-", "candidate", "validation", "resolution")):
                continue
            if tail not in selected and not tail.startswith(sessions):
                continue
            target = controller.directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            controller.artifact(target, checked_artifact(source, name))
        if engineering:
            for name in ("engine", "original-engine"):
                shutil.copytree(source / prefix / name, controller.directory / prefix / name,
                    symlinks=True, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        job.update(workspace=old_job["workspace"], iteration=old_job.get("iteration", 1),
                   continued_from={"run": previous["id"], "repair": identity})
        for name in ("tool_calls", "implementation_state", "review", "next_action"):
            if name in old_job:
                job[name] = copy.deepcopy(old_job[name])
    from bridge_evolve.evolution.config import engine_identity
    revision = previous.get("repair_engine")
    if revision and revision["hash"] == engine_identity():
        for identity, old_job in previous.get("repair_jobs", {}).items():
            if (old_job["status"] == "accepted_engine_revision" and identity in controller.state.get("repair_jobs", {})
                    and ("covers" not in revision or identity in revision["covers"])):
                controller.state["repair_jobs"][identity].update(status="resolved_by_revision",
                    resolved_by={"run": previous["id"], "repair": identity}, validation=old_job["validation"],
                    next_action="当前执行引擎已包含此项已验收源码修订。")
    imported.append(previous["id"])
    controller.checkpoint()


def reproduce(controller, incident):
    evidence = controller.read_artifact(controller.directory / incident["evidence"][0])
    layout = evidence["systems"][evidence["error"]["table"]]
    definitions = {side: controller.system(identity) for side, identity in layout.items()}
    board = parse_board(evidence["board"])
    world = PublicWorld(definitions, board.dealer, board.vulnerability)
    while not world.auction.ended:
        seat = world.auction.next_seat
        system = definitions[seat.partnership.value]
        observation = Observation(board.deal.hand(seat), seat, board.dealer, board.vulnerability,
                                  world.auction.calls, (), tuple(definitions.items()))
        try:
            decision = decide(system, observation)
            world.step(decision.call)
        except SystemError as error:
            if system.content_hash != incident["baseline"] or not str(error).startswith("Unimplemented protocol branch:"):
                raise ValueError("The original incident did not reproduce as the same baseline refusal.") from error
            return {"evidence": evidence, "state": PublicContext(world, seat.partnership.value, seat).value("state"),
                    "seat": seat.value, "history": [str(call) for call in world.auction.calls],
                    "hand": evidence["board"]["hands"][seat.value]}
    raise ValueError("The original baseline refusal did not reproduce.")


def completion_system(original, proposal, state, suffix):
    if not proposal["basis"].strip() or len(proposal["basis"]) > 4000:
        raise ValueError("A baseline completion requires a bounded, explicit system basis.")
    if not 1 <= len(proposal["rules"]) <= 8:
        raise ValueError("No complete bounded baseline patch was supplied; explicit scope review is required.")
    if original["fallback"].get("kind") != "error":
        raise ValueError("Automatic completion is limited to an explicit error fallback; existing decision changes need review.")
    raw = copy.deepcopy(original)
    minimum = min((rule["priority"] for rule in raw["rules"]), default=0)
    for index, rule in enumerate(proposal["rules"]):
        if set(rule) != {"id", "when", "action"} or not isinstance(rule["when"], dict):
            raise ValueError("Completion rules accept only id, when and action; priorities are assigned by the trusted host.")
        conditions = rule["when"].get("args", []) if rule["when"].get("op") == "and" else [rule["when"]]
        guard = {"op": "eq", "args": [{"op": "state"}, state]}
        if guard not in conditions:
            raise ValueError("A completion rule must explicitly conjunct the reproduced public protocol state.")
        action = rule["action"]
        if not isinstance(action, dict):
            raise ValueError("Completion actions must be typed IR objects, not arbitrary source or text.")
        if action.get("kind") not in ("call", "bid", "cheapest", "step") or action.get("kind") == "call" and action.get("call") == "P":
            raise ValueError("A completion cannot substitute Pass or another error as its response.")
        raw["rules"].append({**copy.deepcopy(rule), "priority": minimum - index - 1})
    raw["name"] += " · completion " + suffix
    result = validate_system(raw)
    normalized = result.to_data()
    before = validate_system(original).to_data()
    if normalized["rules"][:len(before["rules"])] != before["rules"] or any(
            normalized[key] != value for key, value in before.items() if key not in ("name", "rules")):
        raise ValueError("Baseline completion changed existing executable or public semantics.")
    return result


def validate_completion(controller, incident, candidate, reproduced):
    baseline = controller.system(incident["baseline"])
    checked = 0
    for name in incident["evidence"]:
        evidence = controller.read_artifact(controller.directory / name)
        board = parse_board(evidence["board"])
        for layout in evidence["systems"].values():
            definitions = {side: candidate if identity == baseline.content_hash else controller.system(identity)
                           for side, identity in layout.items()}
            result = run_auction(definitions["NS"], definitions["EW"], board, compact=True, trace=True)
            audit_module().bounds_checker()(result["auction"], board)
            checked += 1
    if not checked:
        raise ValueError("No complete original counterexample was validated.")
    return {"status": "passed", "replayed_tables": checked, "preserved_old_rules": len(baseline.rules),
            "public_protocol_unchanged": True, "old_decision_preservation":
            "All original rules and features are unchanged; appended rules are below every original priority and only replace the explicit error fallback.",
            "scope": "Counterexamples and structural noninterference, not proof of optimal strength on newly implemented hands."}


class RepairQueue:
    def __init__(self, controller):
        self.controller = controller

    def engineering_progress(self, job):
        data = self.controller.read_artifact(self.controller.directory / job["workspace"]) if job.get("workspace") else {}
        if data is None:
            raise ValueError("The saved engineering workspace is missing; cannot claim maintenance progress.")
        return canonical_hash({
            "changes": data.get("changes", {}),
            "tests": sorted({canonical_hash({key: row.get(key) for key in
                ("selectors", "source_hash", "returncode", "original")}) for row in data.get("tests", [])}),
            "original_reproduction": {key: data.get("original_reproduction", {}).get(key)
                                     for key in ("selectors", "source_hash", "returncode")},
            "review": job.get("review"),
        })

    def continue_engineering(self, job, before):
        supervisor = job["period_supervision"]
        supervisor["dispatches"] += 1
        if job["status"] not in ("solution_returned", "delivery_failed", "deferred"):
            supervisor.update(status=job["status"], updated_at=timestamp())
            self.controller.checkpoint()
            return False
        supervisor["stalled_periods"] = (
            supervisor["stalled_periods"] + 1 if self.engineering_progress(job) == before else 0)
        from bridge_evolve.evolution.costs import budget_usage
        budget = self.controller.config["maintenance_budget"]
        used = budget_usage(self.controller.state, "maintenance")
        limit = (self.controller.config.get("campaign_manager") or {}).get("max_missed_deliveries", 2)
        if (used["requests"] >= budget["max_requests"] or used["charged_tokens"] >= budget["max_tokens"]
                or self.controller.maintenance_time_window() <= 0):
            reason = "独立维护余额不足；保留当前隔离代码、测试及方案，不借用研究额度。"
        elif supervisor["stalled_periods"] >= limit:
            reason = f"连续{limit}个工程周期没有新增源码、测试或复核证据；保留工单并明确阻塞，不继续空转。"
        else:
            supervisor.update(status="queued", updated_at=timestamp())
            self.controller.checkpoint()
            return True
        supervisor.update(status="blocked", reason=reason, updated_at=timestamp())
        job.update(repair_error=reason, next_action=reason)
        LOGGER.warning("Engineering continuation %s blocked: %s", job["id"], reason)
        self.controller.milestone(f"repair-continuation:{job['id']}:{supervisor['dispatches']}",
                                 "repair_continuation_blocked", repair=job["id"], summary=reason)
        self.controller.checkpoint()
        return False

    def process(self):
        controller = self.controller
        budget = controller.config.get("repair_attempts", 0)
        jobs = controller.state.get("repair_jobs", {})
        attempted = sum("proposal_response" in job for job in jobs.values())
        pending = sorted(jobs.values(), key=lambda job: (
            not bool(job.get("blocked_deliveries")), job["kind"] == "baseline-completion"))
        visited = set()
        while pending:
            if controller.config.get("campaign_manager"):
                from bridge_evolve.evolution.manager import CampaignManager
                CampaignManager(controller).reconcile()
                pending.sort(key=lambda job: (
                    not bool(job.get("blocked_deliveries")), job["kind"] == "baseline-completion"))
            job = pending.pop(0)
            visited.add(job["id"])
            if (controller.config.get("research_budget_mode") == "campaign" or job["kind"] == "baseline-policy"):
                if job["status"] in REPAIRABLE and budget:
                    supervised = (job["kind"] == "framework" and controller.config.get("auto_continue_repairs")
                                  and controller.config.get("maintenance_budget"))
                    if supervised:
                        supervisor = job.setdefault("period_supervision", {
                            "owner": "maintenance_controller", "dispatches": 0, "stalled_periods": 0})
                        supervisor.update(status="running", updated_at=timestamp())
                        supervisor.pop("reason", None)
                        before = self.engineering_progress(job)
                        controller.checkpoint()
                    from bridge_evolve.evolution.maintenance import repair
                    repair(controller, job)
                    if supervised and self.continue_engineering(job, before):
                        visited.discard(job["id"])
                pending.extend(row for row in jobs.values() if row["id"] not in visited
                               and all(queued["id"] != row["id"] for queued in pending))
                if job["status"] in ("accepted_for_next_run", "accepted_engine_revision", "resolved_by_revision"):
                    for waiting in jobs.values():
                        if waiting["status"] == "waiting_dependency" and waiting.get("blocked_by") and all(
                                jobs[identity]["status"] in ("accepted_for_next_run", "resolved_by_revision")
                                for identity in waiting["blocked_by"]):
                            visited.discard(waiting["id"])
                            if waiting not in pending:
                                pending.append(waiting)
                continue
            if (job["status"] not in ("pending", "proposing", "validating", "reviewing", "requires_engine_revision")
                    or "proposal_response" not in job and attempted >= budget):
                continue
            from bridge_evolve.evolution.costs import budget_usage
            maintenance = controller.config.get("maintenance_budget")
            available_requests = (maintenance["max_requests"] - budget_usage(controller.state, "maintenance")["requests"]
                                  if maintenance else controller.config["max_agent_requests"] - controller.state["agent_requests"])
            available_time = controller.maintenance_time_window() if maintenance else controller.model_time_window()
            if available_requests < 2 or available_time < 30:
                job["deferred_reason"] = "Repair request/time budget unavailable; research evidence and current scoring remain unchanged."
                controller.checkpoint()
                continue
            if "proposal_response" not in job:
                attempted += 1
            directory = controller.directory / "repairs" / job["id"]
            try:
                if job["kind"] == "framework":
                    path = directory / "engine-request.json"
                    job["proposal_response"] = path.relative_to(controller.directory).as_posix()
                    controller.checkpoint()
                    diagnosis = structured_request(controller, path,
                        "You are the standby framework repair diagnostician. Inspect the recorded failure, distinguish "
                        "candidate defects from host/solver/tool faults, and return a reproducible repair and validation plan. "
                        "You cannot edit source, scoring, oracle, opponents or active artifacts here. Engine changes must be "
                        "implemented and reviewed in an isolated engineering revision, never hot-patched into a frozen run.",
                        {"error": job["error"], "evidence": [controller.read_artifact(controller.directory / name)
                                                          for name in job["evidence"][:2]]},
                        ENGINE_REQUEST, "repair:" + job["id"] + ":diagnose")
                    if any(not diagnosis[key].strip() or len(diagnosis[key]) > 4000 for key in ("diagnosis", "repair_plan")):
                        raise ValueError("A framework repair request needs a concrete, bounded diagnosis and plan.")
                    job.update(status="engine_revision_proposed", diagnosis=diagnosis, finished_at=timestamp(),
                               deployment="需要隔离工程修订和独立验收；不会在研究进程内执行源码补丁。")
                    controller.checkpoint()
                    continue
                reproduced = reproduce(controller, job)
                job.update(status="proposing", reproduced={key: reproduced[key] for key in ("state", "seat", "history")})
                path = directory / "proposal.json"
                job["proposal_response"] = path.relative_to(controller.directory).as_posix()
                controller.state["activity"] = {"kind": "repair", "repair": job["id"], "stage": "proposing", "started_at": timestamp()}
                controller.checkpoint()
                original = controller.system(job["baseline"]).to_data()
                state = original["protocol"]["states"][reproduced["state"]]
                proposal = structured_request(controller, path,
                    "You repair a baseline's MISSING declared branch, never improve a research candidate's chance. "
                    "You have no filesystem or scoring tools. Propose only 1-8 id/when/action rules; host assigns priorities "
                    "below all old rules. Each when must conjunct eq(state,reproduced_state). Do not use blanket Pass, "
                    "invent promises, weaken old behavior or change public protocol. Cite the existing protocol basis in Chinese. "
                    "If existing-state completion cannot solve it, return rules=[] and a concrete blocker; never fake a fix.",
                    {"incident": job["error"], "reproduction": {key: reproduced[key] for key in ("state", "seat", "history", "hand")},
                     "baseline_state": state, "rules": original["rules"], "features": original["features"],
                     "fallback": original["fallback"], "public_announcements": original["announcements"]},
                    PROPOSAL, "repair:" + job["id"] + ":propose")
                job["status"] = "validating"
                controller.checkpoint()
                if not proposal["rules"]:
                    job.update(status="requires_review", repair_error=proposal.get("blocker") or proposal["basis"],
                               finished_at=timestamp())
                    controller.checkpoint()
                    continue
                candidate = completion_system(original, proposal, reproduced["state"], job["id"][-6:])
                validation = validate_completion(controller, job, candidate, reproduced)
                controller.artifact(directory / "candidate.json", candidate.to_data())
                controller.artifact(directory / "validation.json", validation)
                job.update(status="reviewing", candidate=candidate.content_hash, validation=validation)
                controller.checkpoint()
                review = structured_request(controller, directory / "review.json",
                    "Independently review a proposed baseline completion. You cannot edit or deploy it. Reject if its "
                    "system basis is unsupported, it is designed to help the research candidate, it weakens existing "
                    "semantics or only disguises failure. Structural checks are evidence, not proof of new-domain strength. "
                    "Only approve a defensible declared-protocol completion with the supplied real counterexample checks.",
                    {"incident": job["error"], "reproduction": job["reproduced"], "proposal": proposal,
                     "original_public_state": state, "validation": validation},
                    REVIEW, "repair:" + job["id"] + ":review")
                if not review["reason"].strip() or len(review["reason"]) > 4000:
                    raise ValueError("Independent repair acceptance requires an explicit bounded rationale.")
                job.update(status="accepted_for_next_run" if review["approve"] else "rejected",
                           review=review, candidate_path=(directory / "candidate.json").relative_to(controller.directory).as_posix(),
                           finished_at=timestamp(), deployment="Never replaces this run's frozen baseline; use the exported version in a new experiment.")
            except EvolutionStopped:
                if (controller.directory / "cancel-requested").exists():
                    raise
                job.update(status="deferred", repair_error="Repair budget exhausted; no baseline or score changed.")
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                LOGGER.warning("Repair %s rejected: %s", job["id"], error)
                job.update(status="requires_review", repair_error=str(error), finished_at=timestamp())
            controller.checkpoint()
