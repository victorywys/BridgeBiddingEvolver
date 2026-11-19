"""Campaign-wide delivery contracts and scheduling over persisted research evidence."""

import copy
import json
import logging
from datetime import datetime, timedelta, timezone

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evolution.costs import budget_usage
from bridge_evolve.evolution.director import (
    EVIDENCE, IDEA, NOMINATE, REPORT_TEXT, StructuredRequestLimit, draft_protocol_queries, structured_request, validate_assignment,
)
from bridge_evolve.evolution.feedback import bounded
from bridge_evolve.evolution.models import request_body, token_reservation
from bridge_evolve.evolution.delivery import (
    enabled as solution_return_enabled, solution_reservation,
)
from bridge_evolve.evolution.research_tools import STRING, tool
from bridge_evolve.systems import validate_system
from bridge_evolve.systems.language import SystemError

DELIVERABLES = ("draft", "tests", "experiment", "counterexample", "candidate")
LOGGER = logging.getLogger(__name__)


class DeliveryBlocked(RuntimeError):
    pass


DECISION = tool("manage_research_campaign",
    "Set the next delivery contract, redirect/pause an unproductive branch, and queue concrete follow-up work. "
    "Host evidence alone determines delivery, validity and scores; this decision cannot publish or promote.",
    {"summary": REPORT_TEXT, "decision": {"type": "string", "enum": ["continue", "redirect", "pause", "stop"]},
     "instruction": REPORT_TEXT, "deliverable": {"type": "string", "enum": list(DELIVERABLES)},
     "remaining_gaps": {"type": "array", "maxItems": 40, "items": REPORT_TEXT},
     "delivery_outcome": {"type": "object", "additionalProperties": False,
         "required": ["status", "reason"], "properties": {
             "status": {"type": "string", "enum": ["delivered", "falsified"]}, "reason": REPORT_TEXT}},
     "task_updates": {"type": "array", "maxItems": 100, "items": {
         "type": "object", "additionalProperties": False,
         "required": ["task_id", "status", "priority", "reason"], "properties": {
             "task_id": STRING, "status": {"type": "string", "enum": ["pending", "paused", "cancelled"]},
             "priority": {"type": "integer", "minimum": 1, "maximum": 100}, "reason": REPORT_TEXT}}},
     "tasks": {"type": "array", "maxItems": 8, "items": {
         "type": "object", "additionalProperties": False, "required": ["lineage", "priority", "idea"],
         "properties": {"lineage": STRING, "priority": {"type": "integer", "minimum": 1, "maximum": 100},
                        "delivery_id": STRING,
                        "requires": {"type": "array", "uniqueItems": True,
                                     "items": {"type": "string", "enum": ["compiled", "tests"]}},
                        "idea": IDEA}}}},
    ("summary", "decision", "instruction", "deliverable", "remaining_gaps", "tasks"))
PROMPT = """You manage the WHOLE bridge research campaign, not one implementation job.
Own delivery, priorities, unfinished coverage and timely maintenance. Use concise Chinese, concrete
evidence and firm acceptance criteria, never humiliation or private reasoning. The local director
owns protocol hypotheses; implementers own IR; maintenance owns baseline/engine repair; the host
alone validates, evaluates and promotes. Do not equate rule counts, claimed progress, passed old
tests or conditional DDS gains with a stronger complete system.
Every contract ends at the host's fixed turn/time boundary. Specify a small falsifiable delivery:
draft = changed IR plus a public-stage design saved as an UNTESTED checkpoint;
tests = fresh passing authored tests on the current draft with new information;
experiment = a new completed real trial, including negative findings, not promotion evidence;
counterexample = a new actual test/contract/trial failure, not a schema error or vague blocker;
candidate = a genuinely published candidate through the existing host gates.
After broad drafting, demand current tests, a measured experiment, a counterexample or publication.
Do not keep polishing the same branch indefinitely. If a contract was missed, redirect concretely,
split into small queued tasks, pause, or stop; an unchanged extension is forbidden. Repeated missed
contracts force pause/stop. Preserve drafts and negative results. Never call an untested snapshot
a validated candidate. Inspect the global backlog and pending maintenance before allocating more
work. Queue executable IDEA assignments for missing or imperfect stages; use continue_from to
reuse a known unpublished workspace. Higher priority runs first at the next matching lineage unit.
Use task_updates to actually defer, cancel, reprioritize or resume pending/paused backlog entries.
Prose about postponing old tasks does not change scheduling. Pause superseded/blocked duplicates
explicitly and retain their evidence. Assigned work is controlled by its delivery decision, not
backlog updates. Resume continues the task's actual saved workspace, not its stale original input.
Stop a branch when warranted, not the global campaign. Only budget_authority is current resource
authorization; inherited task prose may quote spent allocations, and cannot grant them again.
Your own review is bounded to six model requests or 120 seconds at a saved-response boundary.
Use supplied receipts; query only missing facts and decide rather than retrying an unchanged query.
Invalid editable candidate IR belongs to its implementer: authorize raw read_system/edit_system,
then testing. It is not automatically a frozen-baseline/engine incident requiring another role.
Interface cost accounting does not remove implementer edit authority. Real baseline/engine incidents
are routed by the existing host repair queue; do not make unsupported maintenance routing a
prerequisite for fixing candidate definitions. No new resource authorization is implied."""
PROMPT += """
You own persistent delivery commitments, not merely executor stop decisions. Drafts are milestones,
not completed commitments. Stop/pause can replace an executor but cannot erase its delivery debt.
Use delivery_outcome only for a scoped tested/experimental/published deliverable, or a falsified
hypothesis backed by an actual fresh counterexample; explain the evidence. Never close a research
goal because an engineering incident occurred. Follow-up tasks for the same goal should name its
delivery_id and declare requires=['compiled'] or ['tests'] where appropriate.
After a missed contract, change the method or scope and reuse retained raw reads, including those
from invalid drafts. A recovery attempt must prioritize an edit/test or concrete counterexample,
not restart discovery. Engineering owns recorded platform/baseline incidents and their verified
replay, while research owns candidate IR and remains accountable after engineering hand-back.
For closeout_budget, give one direct pause/stop decision from the supplied receipt without queries;
do not spend the reserved review allowance on new exploration or promise unallocated work.
"""


def management_due(session):
    if not session.controller.config.get("campaign_manager") or not session.assignment:
        return None
    branch = session.controller.state.get("management", {}).get("branches", {}).get(session.identity, {})
    contract = branch.get("contract")
    if contract is None:
        raise RuntimeError("Managed research requires a host delivery contract before starting.")
    manager = CampaignManager(session.controller)
    manager.reconcile()
    commitment = manager.data["deliveries"].get(branch.get("delivery_id"))
    if solution_return_enabled(session.controller.config):
        if contract["review"] in session.tools.data.get("solution_returns", {}):
            return "solution_return"
        if commitment and commitment["status"] == "blocked_engineering":
            session.force_solution_reason = "engineering_blocker"
        elif session.tools.data.get("research_memory", {}).get("no_information_streak", 0) >= 12:
            session.force_solution_reason = "repeated_reads"
        return None
    if commitment and commitment["status"] == "blocked_engineering":
        return "engineering_blocker"
    if session.data["turns"] - contract["start_turn"] >= contract["turns"]:
        return "turn_deadline"
    if datetime.now(timezone.utc) >= datetime.fromisoformat(contract["deadline_at"]):
        return "time_deadline"
    if session.tools.data.get("research_memory", {}).get("no_information_streak", 0) >= 12:
        return "repeated_reads"
    return None


def management_view(state, config):
    limits = config.get("campaign_manager")
    if not limits:
        return None
    management = state.get("management", {})
    return {"limits": limits, "remaining_gaps": management.get("remaining_gaps", []),
            "branches": [{"session": identity, **{key: branch.get(key) for key in
                         ("status", "contract", "missed_deliveries", "last_receipt")}}
                         for identity, branch in management.get("branches", {}).items()],
            "reviews": [{key: row.get(key) for key in
                        ("id", "session", "created_at", "receipt", "checkpoint", "decision", "allowed_decisions",
                         "decision_source", "error", "interruption")}
                        for row in list(management.get("reviews", {}).values())[-12:]],
            "tasks": list(management.get("tasks", {}).values())[-100:],
            "deliveries": list(management.get("deliveries", {}).values()),
            "checkpoints": list(management.get("checkpoints", {}).values())[-12:],
            "solution_returns": list(state.get("solution_deliveries", {}).values())[-40:],
            "delivery_windows": list(state.get("delivery_windows", {}).values())[-40:],
            "source_run": management.get("source_run"),
            "scope": "Delivery supervision, not bidding fitness. Checkpoint snapshots are unpublished."}


def import_management(previous, state):
    old = previous.get("management")
    if not old:
        return
    source_tasks = copy.deepcopy(old.get("tasks", {}))
    for review_id, review in old.get("reviews", {}).items():
        if not review.get("applied"):
            continue
        for index, assignment in enumerate(review.get("decision", {}).get("tasks", [])):
            legacy_id = f"{review_id}-task-{index + 1:02d}"
            saved = source_tasks.get(legacy_id)
            if not saved or "historical_task" not in saved:
                continue
            fingerprint = canonical_hash({"lineage": assignment["lineage"], "idea": assignment["idea"]})
            if saved.get("fingerprint") == fingerprint:
                continue
            identity = f"{previous['id']}-{legacy_id}"
            if identity in source_tasks:
                if source_tasks[identity].get("fingerprint") != fingerprint:
                    raise ValueError("A saved management task conflicts with its originating review.")
                continue
            source_tasks[identity] = {
                **copy.deepcopy(assignment), "id": identity, "status": "pending", "fingerprint": fingerprint,
                "created_at": review["created_at"], "source_session": review["session"],
                "recovered_from_review": review_id}
    tasks = {}
    for identity, task in source_tasks.items():
        if task["status"] not in ("pending", "assigned", "paused", "blocked_budget", "blocked_engineering", "waiting_evidence"):
            continue
        task = copy.deepcopy(task)
        assigned = task.get("session")
        metadata = previous.get("research_sessions", {}).get(assigned)
        if metadata and metadata.get("releases"):
            continue
        source = assigned if metadata else task["idea"].get("continue_from")
        if source:
            branch = state.get("research_branch_imports", {}).get(previous["id"], {}).get(
                source, "imported-" + source if source in previous.get("research_sessions", {}) else source)
            if branch not in state.get("research_branches", {}):
                raise ValueError("A queued management task lost its saved continuation branch.")
            task["idea"]["continue_from"] = branch
        task.update(status="paused" if task["status"] == "paused" else "pending",
                    historical_task=identity, source_run=previous["id"])
        for key in ("unit", "session", "outcome"):
            task.pop(key, None)
        tasks[identity] = task
    state["management"] = {"source_run": previous["id"], "tasks": tasks, "reviews": {}, "branches": {},
                           "deliveries": {}, "checkpoints": {},
                           "remaining_gaps": copy.deepcopy(old.get("remaining_gaps", []))}
    for identity, original in old.get("deliveries", {}).items():
        if original["status"] in ("delivered", "falsified"):
            continue
        goal = copy.deepcopy(original)
        source = goal.get("session")
        mapped = state.get("research_branch_imports", {}).get(previous["id"], {}).get(source, source)
        if mapped not in state.get("research_branches", {}):
            raise ValueError("An unfinished research delivery lost its saved workspace.")
        goal.update(session=mapped, status="needs_assignment", source_run=previous["id"])
        goal.setdefault("sources", []).append({"run": state["id"], "session": mapped})
        state["management"]["deliveries"][identity] = goal


class CampaignManager:
    def __init__(self, controller):
        self.controller = controller
        self.limits = controller.config["campaign_manager"]
        self.data = controller.state.setdefault("management", {
            "branches": {}, "reviews": {}, "tasks": {}, "remaining_gaps": []})
        self.data.setdefault("deliveries", {})
        self.data.setdefault("checkpoints", {})

    def commitment(self, session, job):
        branch = self.branch(session.identity)
        task = self.data["tasks"].get(job.get("management_task"), {})
        identity = branch.get("delivery_id") or task.get("delivery_id") or (
            self.controller.state["id"] + ":" + session.identity)
        goal = self.data["deliveries"].setdefault(identity, {
            "id": identity, "owner": "research_team", "lineage": session.data["lineage"],
            "title": job["idea"]["title"], "idea": copy.deepcopy(job["idea"]), "status": "active",
            "attempts": [], "sources": [], "evidence": [], "blocked_by": []})
        branch["delivery_id"] = identity
        if task:
            task["delivery_id"] = identity
        if session.identity not in goal["attempts"]:
            goal["attempts"].append(session.identity)
        source = {"run": self.controller.state["id"], "session": session.identity}
        if source not in goal["sources"]:
            goal["sources"].append(source)
        goal.update(session=session.identity, task=job.get("management_task"))
        if goal["status"] in ("needs_assignment", "blocked_budget", "blocked_delivery"):
            goal["status"] = "active"
        return goal

    def reconcile(self):
        jobs = self.controller.state.get("repair_jobs", {})
        for goal in self.data["deliveries"].values():
            if goal["status"] in ("delivered", "falsified"):
                continue
            live = self.controller.state.get("research_sessions", {}).get(goal["session"])
            metadata = live or self.controller.state.get("research_branches", {}).get(goal["session"])
            if metadata:
                name = metadata["workspace"]
                version = self.controller.state["artifacts"].get(name)
                if goal.get("evidence_workspace_sha256") != version:
                    workspace = self.controller.read_artifact(self.controller.directory / name)
                    if workspace is None:
                        raise ValueError("A delivery's current evidence workspace is missing.")
                    goal["last_receipt"] = self.workspace_receipt(workspace, metadata.get("turns", 0),
                        self.data["branches"].get(goal["session"], {}).get("contract", {}), retained=live is None)
                    goal["evidence_workspace_sha256"] = version
            sources = {(source["run"], source["session"]) for source in goal["sources"]}
            blockers = [job for job in jobs.values() if any(
                (ref["run"], ref["session"]) in sources and ref.get("blocking", True)
                for ref in job.get("blocked_research", [])) and job["status"] != "resolved_by_revision"]
            for job in blockers:
                for identity in job.get("blocked_by", []):
                    if identity in jobs and jobs[identity]["status"] != "resolved_by_revision" and jobs[identity] not in blockers:
                        blockers.append(jobs[identity])
            goal["blocked_by"] = [job["id"] for job in blockers]
            for job in blockers:
                if goal["id"] not in job.setdefault("blocked_deliveries", []):
                    job["blocked_deliveries"].append(goal["id"])
            if blockers:
                goal.update(status="blocked_engineering",
                            next_action="Engineering must replay and resolve the linked incident, then return this saved work.")
            elif goal["status"] == "blocked_engineering":
                goal.update(status="needs_assignment", next_action="Verified repair is active; resume the saved delivery.")
        for task in self.data["tasks"].values():
            goal = self.data["deliveries"].get(task.get("delivery_id"))
            if not goal or task["status"] not in ("pending", "waiting_evidence"):
                continue
            receipt = goal.get("last_receipt", {})
            ready = all(receipt.get("compilation", {}).get("status") == "typed" if item == "compiled"
                        else receipt.get("tests_current") and receipt.get("tests", {}).get("failed") == 0
                        and (receipt.get("tests", {}).get("tests") or 0) > 0
                        for item in task.get("requires", []))
            task["status"] = "pending" if ready and not goal["blocked_by"] else "waiting_evidence"

    def recover(self):
        self.reconcile()
        for goal in self.data["deliveries"].values():
            if goal["status"] != "needs_assignment":
                continue
            if any(task.get("delivery_id") == goal["id"] and task["status"] in ("pending", "assigned")
                   for task in self.data["tasks"].values()):
                continue
            if len(goal["attempts"]) > self.limits["max_missed_deliveries"]:
                goal.update(status="blocked_delivery", next_action=(
                    "Research team still owns this delivery; repeated executor handoffs produced no accepted outcome. "
                    "A new concrete method is required, not another renamed attempt."))
                continue
            idea = copy.deepcopy(goal["idea"])
            idea.pop("base_candidate", None)
            idea.pop("merge_candidates", None)
            idea["continue_from"] = goal["session"]
            idea["implementation"] = (
                "DELIVERY RECOVERY: integrate the saved draft and retained raw-read evidence. Do not restart the "
                "same discovery scan. First complete the smallest edit/test or reproducible counterexample "
                "missing from the last receipt, then validate the current version. " + idea["implementation"])
            identity = f"{goal['id']}-recovery-{len(goal['attempts']):02d}"
            self.data["tasks"].setdefault(identity, {
                "id": identity, "lineage": goal["lineage"], "priority": 100, "idea": idea,
                "status": "pending", "delivery_id": goal["id"], "requires": [],
                "fingerprint": canonical_hash({"lineage": goal["lineage"], "idea": idea}),
                "created_at": timestamp(), "source_session": goal["session"], "source": "host_delivery_recovery"})

    def require_progress(self, lineage):
        remaining = [goal for goal in self.data["deliveries"].values()
                     if goal["lineage"] == lineage and goal["status"] not in ("delivered", "falsified")]
        if remaining:
            raise DeliveryBlocked("Unfinished research delivery has no runnable handoff: " + "; ".join(
                goal["title"] + " — " + goal.get("next_action", goal["status"]) for goal in remaining))

    def claim(self, lineage, unit):
        self.recover()
        existing = [task for task in self.data["tasks"].values()
                    if task.get("unit") == unit and task["status"] == "assigned"]
        if existing:
            return sorted(existing, key=lambda task: (-task["priority"], task["id"]))
        pending = sorted((task for task in self.data["tasks"].values()
                          if task["lineage"] == lineage and task["status"] == "pending"),
                         key=lambda task: (-task["priority"], task["id"]))
        urgent = [task for task in pending if self.data["deliveries"].get(
            task.get("delivery_id"), {}).get("status") == "needs_assignment"]
        selected = (urgent or pending)[:self.controller.config["hypotheses_per_unit"]]
        for task in selected:
            validate_assignment(self.controller, task["idea"], lineage)
            task.update(status="assigned", unit=unit)
        self.controller.checkpoint()
        return selected

    def branch(self, identity):
        return self.data["branches"].setdefault(identity, {
            "status": "awaiting_management", "reviews": [], "missed_deliveries": 0, "draft_only_deliveries": 0})

    def resumed_idea(self, task):
        idea = copy.deepcopy(task["idea"])
        if task.get("session"):
            idea["continue_from"] = task["session"]
            idea.pop("base_candidate", None)
            idea.pop("merge_candidates", None)
        validate_assignment(self.controller, idea, task["lineage"])
        return idea

    def receipt(self, session, contract):
        return self.workspace_receipt(session.tools.data, session.data["turns"], contract)

    def workspace_receipt(self, data, turn, contract, *, retained=False):
        if retained:
            history, releases, turn, contract = {}, [], 0, {}
        else:
            history, releases = data["operations"], data["releases"]
        document = canonical_hash(data["system"])
        tests_hash = canonical_hash(data["tests"])
        tests = data.get("last_tests") or {}
        current_tests = tests.get("document_hash") == document
        operations = list(history.values())[contract.get("operations", 0):]
        results = [(row["name"], row.get("result", {})) for row in operations if row["status"] == "completed"]
        try:
            system_hash = validate_system(data["system"]).content_hash
        except SystemError as error:
            compilation = {"status": "invalid_draft", "error": str(error), "pointer": error.pointer}
            system_hash = None
        else:
            compilation = {"status": "typed", "system_hash": system_hash}
        changed = "document_hash" in contract and document != contract["document_hash"]
        new_test_information = changed or tests_hash != contract.get("tests_hash") or not contract.get("tests_current")
        fresh_tests = new_test_information and current_tests and any(
            name == "run_tests" and result.get("ok") and result.get("document_hash") == document
            and result.get("tests", 0) > 0 and result.get("failed") == 0 for name, result in results)
        trials = [result for name, result in results if name == "trial" and result.get("ok")
                  and result.get("status") == "checked" and result.get("round_id")]
        failures = [result for name, result in results if result.get("ok") and (
            name == "run_tests" and result.get("failed", 0) > 0
            or name == "run_contract" and result.get("status") == "failed"
            or name == "trial" and result.get("error_count", 0) > 0)]
        fresh_failure = bool(failures) and canonical_hash(failures[-1]) != contract.get("failure_hash")
        deliveries = {
            "draft": changed and bool((data.get("implementation_state") or {}).get("stage_design", "").strip()),
            "tests": bool(fresh_tests), "experiment": bool(trials), "counterexample": fresh_failure,
            "candidate": len(releases) > contract.get("releases", 0)}
        solution = data.get("solution_returns", {}).get(contract.get("review"))
        deliveries["solution"] = solution is not None
        return {"document_hash": document, "tests_hash": tests_hash, "compilation": compilation,
                "tests_current": current_tests, "tests": {key: tests.get(key) for key in
                    ("tests", "passed", "failed", "change_witnesses", "protocol_witnesses")},
                "operations": len(history), "new_tool_calls": len(operations),
                "evidence_scope": "retained_snapshot" if retained else "live_workspace",
                "turn": turn, "turns_used": turn - contract.get("start_turn", 0),
                "deliveries": deliveries, "expected": contract.get("deliverable"),
                "met": bool(solution) if contract.get("solution_return") else deliveries.get(contract.get("deliverable"), False),
                "solution_return": solution,
                "new_trial_rounds": [row["round_id"] for row in trials],
                "failure_hash": canonical_hash(failures[-1]) if failures else contract.get("failure_hash"),
                "releases": len(releases),
                "late_seconds": max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(
                    contract.get("deadline_at", timestamp()))).total_seconds())}

    def checkpoint(self, session, identity, receipt):
        return self.checkpoint_data(session.identity, session.tools.data, identity, receipt)

    def checkpoint_data(self, session_id, data, identity, receipt):
        cases = {test["case_id"]: data["cases"][test["case_id"]] for test in data["tests"]
                 if test.get("case_id") in data["cases"]}
        value = {"schema": "bridge-managed-checkpoint/v1", "session": session_id, "receipt": receipt,
                 "system": data["system"], "tests": data["tests"], "cases": cases,
                 "implementation_state": data.get("implementation_state"),
                 "provisional_proposals": copy.deepcopy(data.get("provisional_proposals", {})),
                 "last_tests": data.get("last_tests"), "last_contract": data.get("last_contract"),
                 "last_trial": data.get("last_trial"),
                 "scope": "Unpublished saved draft and actual evidence; not promotion or complete coverage."}
        path = self.controller.directory / "management" / "checkpoints" / f"{identity}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.controller.artifact(path, value)
        name = path.relative_to(self.controller.directory).as_posix()
        reference = {"path": name, "sha256": self.controller.state["artifacts"][name]}
        self.data["checkpoints"][identity] = {
            "id": identity, "session": session_id, "checkpoint": reference, "receipt": receipt}
        return reference

    def context(self, session, row):
        state = self.controller.state
        config = self.controller.config
        authority = {}
        for pool in ("research", "maintenance"):
            usage = budget_usage(state, pool)
            maximum = ({"requests": config["max_agent_requests"], "tokens": config["max_agent_tokens"]}
                       if pool == "research" else
                       {"requests": config["maintenance_budget"]["max_requests"],
                        "tokens": config["maintenance_budget"]["max_tokens"]} if config.get("maintenance_budget")
                       else {"requests": 0, "tokens": 0})
            authority[pool] = {"limits": maximum, "used": usage,
                              "remaining_requests": max(0, maximum["requests"] - usage["requests"]),
                              "remaining_tokens": max(0, maximum["tokens"] - usage["charged_tokens"])}
            maximum_seconds = config["max_seconds"] if pool == "research" else (
                config["maintenance_budget"]["max_seconds"] if config.get("maintenance_budget") else 0)
            used_seconds = (state["spent_seconds"] - state.get("maintenance_seconds", 0) if pool == "research"
                            else state.get("maintenance_seconds", 0))
            authority[pool].update(limit_seconds=maximum_seconds, remaining_seconds=max(0, maximum_seconds - max(0, used_seconds)))
        return {"budget_authority": authority,
                "authorization_policy": "Only this frozen run's limits authorize spending. Do not repeat old allocations from task prose as new authorization.",
                "task": config["research_task"], "session": session.identity,
                "assignment": session.assignment, "limits": self.limits,
                "review": row, "remaining_gaps": self.data["remaining_gaps"],
                "delivery_commitments": list(self.data["deliveries"].values()),
                "protocol_queries": draft_protocol_queries(self.controller),
                "global_tasks": list(self.data["tasks"].values()),
                "branches": {identity: {key: metadata.get(key) for key in
                    ("lineage", "status", "turns", "research_turns", "spent_seconds", "releases", "working")}
                    for identity, metadata in state.get("research_sessions", {}).items()},
                "retained_branches": state.get("research_branches", {}),
                "evaluations": [{key: job.get(key) for key in ("id", "candidate", "status", "stage", "outcome")}
                                for job in state.get("evaluation_queue", {}).values()],
                "maintenance": [{key: job.get(key) for key in
                    ("id", "kind", "status", "created_at", "occurrences", "error", "next_action")}
                    for job in state.get("repair_jobs", {}).values()],
                "budget_pools": {pool: budget_usage(state, pool) for pool in ("research", "maintenance")}}

    def closeout_context(self, session, row):
        context = self.context(session, row)
        goal = self.data["deliveries"][self.branch(session.identity)["delivery_id"]]
        return {key: context[key] for key in ("budget_authority", "authorization_policy", "session", "review")} | {
            "delivery": {key: goal[key] for key in ("id", "title", "owner", "status", "blocked_by")},
            "instruction": "Resource closeout only: directly pause/stop from this host receipt, no new exploration."}

    def solution_review_context(self, session, row):
        return self.closeout_context(session, row) | {
            "current_best_solution": row["receipt"].get("solution_return"),
            "remaining_gaps": self.data["remaining_gaps"],
            "instruction": (
                "solution_return mode: this executor MUST return a concrete best available scheme every period, "
                "even if imperfect. receipt.met is this function-return obligation, NOT implementation/test success. "
                "The saved solution fulfills the period; decide the NEXT concrete implementation or validation slice. "
                "Do not pause merely because a real scheme is incomplete or untested. Scientific acceptance, "
                "delivery_outcome and candidate promotion still need their existing actual-evidence gates. "
                "No new resource authorization; preserve the saved solution if remaining allocation cannot fund more work.")}

    def worker_budget_available(self, session, *, initial=False):
        config, state = self.controller.config, self.controller.state
        usage = budget_usage(state, "research") if config.get("maintenance_budget") else {
            "requests": state["agent_requests"], "charged_tokens": state.get("model_usage", {}).get("charged_tokens", 0)}
        receipt = self.receipt(session, self.branch(session.identity).get("contract", {}))
        row = {"id": session.identity + "-closeout", "session": session.identity, "created_at": timestamp(),
               "closing": True, "terminal": False, "allowed_decisions": ["pause", "stop"], "receipt": receipt,
               "checkpoint": {"path": "management/checkpoints/" + session.identity + "-closeout.json", "sha256": "0" * 64}}
        messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": json.dumps(
            bounded(self.closeout_context(session, row), limit=config["max_context_bytes"] // 2), ensure_ascii=False)}]
        closing = token_reservation(request_body(config, messages, [DECISION]), config["max_output_tokens"]) + 8192
        self.branch(session.identity)["closeout_reserve"] = {
            "tokens": closing, "requests": 1, "seconds": min(120, config["agent_timeout"])}
        worker = token_reservation(request_body(config, session.data["messages"], session.definitions), config["max_output_tokens"])
        opening = 0
        if initial:
            messages[1]["content"] = json.dumps(bounded(self.context(session, row),
                limit=config["max_context_bytes"] // 2), ensure_ascii=False)
            opening = token_reservation(request_body(config, messages, [DECISION, EVIDENCE, NOMINATE]),
                                        config["max_output_tokens"]) + 8192
        return (usage["requests"] + 2 + initial <= config["max_agent_requests"]
                and usage["charged_tokens"] + worker + closing + opening <= config["max_agent_tokens"]
                and self.controller.model_time_window() > min(120, config["agent_timeout"]))

    def finalize(self):
        state = self.controller.state
        self.reconcile()
        remaining = [goal for goal in self.data["deliveries"].values()
                     if goal["status"] not in ("delivered", "falsified")]
        if remaining and state["status"] in ("completed", "target_met"):
            state.update(status="blocked", blocked_component="research_delivery",
                         error="Research deliveries remain open; executor completion is not acceptance.")
        for goal in remaining:
            if state["status"] == "budget_exhausted" and goal["status"] != "blocked_engineering":
                goal.update(status="blocked_budget", next_action=state.get("error", "Research resource allocation exhausted."))
            identity = goal["session"]
            metadata = state.get("research_sessions", {}).get(identity)
            if metadata is None:
                continue
            data = self.controller.read_artifact(self.controller.directory / metadata["workspace"])
            session = self.controller.read_artifact(self.controller.directory / metadata["path"])
            if data is None or session is None:
                raise ValueError("Cannot finalize a research delivery without its saved workspace/session.")
            branch = self.branch(identity)
            pending = self.data["reviews"].get(branch.get("pending_review"))
            if pending and not pending.get("applied"):
                pending["interruption"] = {"status": state["status"], "reason": state.get("error", state["status"])}
            receipt = self.workspace_receipt(data, session["turns"], branch.get("contract", {}))
            fingerprint = canonical_hash({key: receipt[key] for key in
                ("document_hash", "tests_hash", "operations", "turn", "failure_hash", "tests_current", "releases")})[:12]
            checkpoint_id = f"{identity}-host-stop-{state['agent_requests']}-{fingerprint}"
            existing = self.data["checkpoints"].get(checkpoint_id)
            if existing is None:
                self.checkpoint_data(identity, data, checkpoint_id, receipt)
                self.data["checkpoints"][checkpoint_id]["source"] = "host_run_stop"
            goal["last_receipt"] = receipt
            if checkpoint_id not in goal["evidence"]:
                goal["evidence"].append(checkpoint_id)
            if state["status"] == "budget_exhausted" and goal["status"] != "blocked_engineering":
                goal.update(status="blocked_budget", next_action=state.get("error", "Research resource allocation exhausted."))
            elif goal["status"] in ("active", "needs_assignment"):
                goal.update(status="needs_assignment", next_action=state.get("error", "Resume the saved delivery."))
            status = "blocked_engineering" if goal["blocked_by"] else (
                "blocked_budget" if state["status"] == "budget_exhausted" else state["status"])
            if session["status"] in ("ready", "running", "awaiting_management", "blocked_harness"):
                session.update(status=status, stopped_reason=state.get("error", state["status"]))
                self.controller.artifact(self.controller.directory / metadata["path"], session)
                metadata["status"] = status
            if branch["status"] in ("active", "awaiting_management"):
                branch["status"] = status
            task = self.data["tasks"].get(goal.get("task"))
            if task and task["status"] == "assigned":
                task.update(status=status, outcome=receipt)
        for task in self.data["tasks"].values():
            if task["status"] == "assigned":
                task["status"] = "pending"
                for key in ("unit", "session"):
                    task.pop(key, None)
        for unit in state.get("research_units", {}).values():
            if unit["status"] in ("planning", "implementing", "awaiting_management", "evaluating"):
                unit.update(status=state["status"], error=state.get("error"))
                for job in unit["jobs"]:
                    if job["status"] in ("implementing", "awaiting_management"):
                        job["status"] = state["status"]
        self.controller.checkpoint()

    def prepare(self, session, job):
        self.commitment(session, job)
        branch = self.branch(session.identity)
        if branch.get("pending_review"):
            return self.review(session, job)
        if branch["status"] == "awaiting_management" and branch.get("contract"):
            return self.review(session, job)
        if branch["status"] in ("paused", "stopped", "delivered", "needs_followup"):
            return False
        if not branch.get("contract"):
            return self.review(session, job, initial=True)
        return True

    def review(self, session, job, *, initial=False, terminal=False):
        branch = self.branch(session.identity)
        goal = self.commitment(session, job)
        self.reconcile()
        if terminal and branch.get("terminal_review") and self.data["reviews"][branch["terminal_review"]].get("applied"):
            return False
        identity = branch.get("pending_review")
        if identity is None:
            identity = f"{session.identity}-r{len(branch['reviews']) + 1:04d}"
            receipt = self.receipt(session, branch.get("contract") or {})
            reason = session.data.get("management_reason")
            timeboxed = solution_return_enabled(self.controller.config)
            can_start = True
            if initial and timeboxed:
                usage = budget_usage(self.controller.state, "research")
                can_start = (usage["requests"] < self.controller.config["max_agent_requests"]
                    and usage["charged_tokens"] + solution_reservation(self.controller.config) <= self.controller.config["max_agent_tokens"]
                    and self.controller.model_time_window() > min(120, self.controller.config["agent_timeout"]))
            closing = (reason == "closeout_budget" or initial and not can_start
                       or initial and not timeboxed and not self.worker_budget_available(session, initial=True))
            engineering = goal["status"] == "blocked_engineering"
            if not initial and reason != "engineering_blocker":
                branch["missed_deliveries"] = 0 if receipt["met"] else branch["missed_deliveries"] + 1
                substantial = any(receipt["deliveries"][key] for key in DELIVERABLES if key != "draft")
                branch["draft_only_deliveries"] = 0 if substantial else branch["draft_only_deliveries"] + 1
            allowed = (["pause", "stop"] if terminal or closing or engineering or branch["missed_deliveries"] >= self.limits["max_missed_deliveries"]
                       else ["redirect", "pause", "stop"] if not initial and not receipt["met"]
                       else ["continue", "redirect", "pause", "stop"])
            row = {"id": identity, "session": session.identity, "created_at": timestamp(), "terminal": terminal,
                   "closing": closing, "host_preflight": closing and initial, "blocked_by": list(goal["blocked_by"]),
                   "host_solution_start": initial and timeboxed and can_start,
                   "receipt": receipt, "allowed_decisions": allowed,
                   "checkpoint": self.checkpoint(session, identity, receipt)}
            self.data["reviews"][identity] = row
            branch["pending_review"] = identity
            branch["reviews"].append(identity)
            branch["last_receipt"] = receipt
            if terminal:
                branch["terminal_review"] = identity
            if timeboxed and not initial:
                config = self.controller.config
                context = self.solution_review_context(session, row)
                messages = [{"role": "system", "content": PROMPT},
                            {"role": "user", "content": json.dumps(bounded(context,
                                limit=config["max_context_bytes"] // 2), ensure_ascii=False)}]
                reservation = token_reservation(request_body(config, messages, [DECISION]), config["max_output_tokens"])
                usage = budget_usage(self.controller.state, "research")
                next_return = solution_reservation(config) if not terminal else 0
                if (usage["requests"] + 1 + (not terminal) > config["max_agent_requests"]
                        or usage["charged_tokens"] + reservation + next_return > config["max_agent_tokens"]):
                    row.update(closing=True, host_preflight=True, allowed_decisions=["pause", "stop"])
            self.controller.checkpoint()
        row = self.data["reviews"][identity]
        if not row.get("decision"):
            self.controller.state["activity"] = {"kind": "manager", "session": session.identity,
                                                 "stage": "delivery_review", "started_at": timestamp()}
            self.controller.checkpoint()

            def validate(decision):
                if decision["decision"] not in row["allowed_decisions"]:
                    raise ValueError(f"Missed delivery contracts require one of {row['allowed_decisions']}; no unchanged extension.")
                if (branch["draft_only_deliveries"] >= 2 and decision["deliverable"] == "draft"
                        and row["receipt"]["compilation"]["status"] == "typed"
                        and decision["decision"] in ("continue", "redirect")):
                    raise ValueError("Two draft-only checkpoints require tests, an experiment, a counterexample or a candidate next.")
                outcome = decision.get("delivery_outcome")
                if outcome:
                    accepted = (row["receipt"]["deliveries"]["counterexample"] if outcome["status"] == "falsified"
                                else any(row["receipt"]["deliveries"][key] for key in ("tests", "experiment", "candidate")))
                    if not accepted or row["blocked_by"]:
                        raise ValueError("Delivery closure needs actual current research evidence, not a draft, pause, or engineering failure.")
                    if decision["decision"] not in ("pause", "stop"):
                        raise ValueError("Close a delivered/falsified commitment and return control, rather than allocate it more work.")
                updates = {}
                for update in decision.get("task_updates", []):
                    target = self.data["tasks"].get(update["task_id"])
                    if target is None or target["status"] not in ("pending", "paused"):
                        raise ValueError("Backlog updates require an existing pending/paused task; use a delivery decision for assigned work.")
                    if update["task_id"] in updates:
                        raise ValueError("Update each backlog task only once per decision.")
                    if update["status"] == "pending":
                        self.resumed_idea(target)
                    updates[update["task_id"]] = update["status"]
                seen = set()
                for task in decision["tasks"]:
                    if task["lineage"] not in self.controller.state["champions"]:
                        raise ValueError("Management tasks must target an existing campaign lineage.")
                    validate_assignment(self.controller, task["idea"], task["lineage"])
                    if task.get("delivery_id") and task["delivery_id"] not in self.data["deliveries"]:
                        raise ValueError("Follow-up tasks must name an existing delivery commitment.")
                    if self.data["deliveries"].get(task.get("delivery_id"), {}).get("status") in ("delivered", "falsified"):
                        raise ValueError("A completed delivery cannot accept another executor; define a new research goal.")
                    if task.get("requires") and not task.get("delivery_id"):
                        raise ValueError("Evidence prerequisites require an explicit delivery_id.")
                    fingerprint = canonical_hash({"lineage": task["lineage"], "idea": task["idea"]})
                    if fingerprint in seen or any(existing.get("fingerprint") == fingerprint
                            and updates.get(existing["id"], existing["status"]) in ("pending", "assigned")
                            for existing in self.data["tasks"].values()):
                        raise ValueError("This management assignment is already queued; do not duplicate active work.")
                    seen.add(fingerprint)

            path = self.controller.directory / "management" / f"{identity}.json"
            try:
                if row["host_solution_start"]:
                    row["decision"] = {
                        "summary": "Host opened a bounded solution-return call from the existing director assignment.",
                        "decision": "continue", "instruction": (
                            "Implement the assigned slice using saved evidence, then return the best concrete scheme "
                            "within this period. Imperfect or untested work must still return a scheme with explicit gaps. "
                            "Reserve the final request/time for return_solution, not further discovery."),
                        "deliverable": "draft", "tasks": [], "remaining_gaps": self.data["remaining_gaps"]}
                    row["decision_source"] = "host_solution_call"
                elif row["host_preflight"]:
                    row["decision"] = {
                        "summary": "Host retained saved work and any actual returned solution because the next cycle cannot be funded.",
                        "decision": "pause", "instruction": "Research allocation cannot fund the next worker/review cycle. Keep the actual returned solution and workspace; do not borrow maintenance funds.",
                        "deliverable": row["receipt"].get("expected") or "tests", "tasks": [],
                        "remaining_gaps": self.data["remaining_gaps"]}
                    row["decision_source"] = "host_delivery_reserve"
                elif row["blocked_by"]:
                    row["decision"] = {
                        "summary": "Host-linked engineering incidents block this delivery; no research rejection is inferred.",
                        "decision": "pause", "instruction": "Repair and activate " + ", ".join(row["blocked_by"]) + ", then resume this saved work.",
                        "deliverable": row["receipt"].get("expected") or "tests", "tasks": [],
                        "remaining_gaps": self.data["remaining_gaps"]}
                    row["decision_source"] = "host_engineering_dependency"
                else:
                    row["decision"] = structured_request(self.controller, path, PROMPT,
                        self.solution_review_context(session, row) if solution_return_enabled(self.controller.config)
                        else self.closeout_context(session, row) if row["closing"] else self.context(session, row), DECISION,
                        f"campaign:manager:{identity}", validator=validate,
                        max_steps=1 if row["closing"] else 6, max_seconds=120, allow_evidence=not row["closing"])
                    row["decision_source"] = "model"
            except StructuredRequestLimit as error:
                LOGGER.warning("Campaign manager paused %s: %s", session.identity, error)
                row.update(error=str(error), decision_source="host_review_limit")
                row["decision"] = {
                    "summary": "Host suspended this branch because its management review did not return a timely valid decision.",
                    "decision": "pause", "instruction": str(error) + " All drafts, evidence and fees are retained; this is not a successful management delivery.",
                    "deliverable": row["receipt"].get("expected") or "draft", "tasks": [],
                    "remaining_gaps": self.data["remaining_gaps"]}
                self.controller.state.setdefault("maintenance_events", {})["manager-limit:" + identity] = {
                    "id": "manager-limit:" + identity, "kind": "manager_review_limit",
                    "error": str(error), "session": session.identity}
            self.controller.checkpoint()
        decision = row["decision"]
        if not row.get("applied"):
            if decision["decision"] in ("continue", "redirect") and "next_contract" not in row:
                row["next_contract"] = {key: row["receipt"].get(key) for key in
                    ("document_hash", "tests_hash", "tests_current", "operations", "releases", "failure_hash")}
                row["next_contract"].update(
                    start_turn=session.data["turns"], turns=self.limits["turns"], seconds=self.limits["seconds"],
                    deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=self.limits["seconds"])).isoformat(),
                    deliverable=decision["deliverable"], instruction=decision["instruction"], review=identity)
                if solution_return_enabled(self.controller.config):
                    row["next_contract"]["solution_return"] = True
                self.controller.checkpoint()
            for update in decision.get("task_updates", []):
                task = self.data["tasks"][update["task_id"]]
                if update["status"] == "pending":
                    task["idea"] = self.resumed_idea(task)
                    task["fingerprint"] = canonical_hash({"lineage": task["lineage"], "idea": task["idea"]})
                    for key in ("unit", "session"):
                        task.pop(key, None)
                task.update(status=update["status"], priority=update["priority"],
                    last_management_action={"run": self.controller.state["id"], "review": identity,
                                            "reason": update["reason"], "status": update["status"],
                                            "priority": update["priority"]})
            for index, task in enumerate(decision["tasks"]):
                task_id = f"{self.controller.state['id']}-{identity}-task-{index + 1:02d}"
                fingerprint = canonical_hash({"lineage": task["lineage"], "idea": task["idea"]})
                existing = self.data["tasks"].get(task_id)
                if existing and existing.get("fingerprint") != fingerprint:
                    raise RuntimeError("A management task identity cannot refer to a different assignment.")
                self.data["tasks"].setdefault(task_id, {
                    **copy.deepcopy(task), "id": task_id, "status": "pending", "created_at": row["created_at"],
                    "fingerprint": fingerprint,
                    "source_session": session.identity,
                    **({"delivery_id": goal["id"]} if not task.get("delivery_id")
                       and task["idea"].get("continue_from") == session.identity else {})})
            self.data["remaining_gaps"] = decision["remaining_gaps"]
            notice_id = "management:" + identity
            if notice_id not in session.data.setdefault("management_notices", []):
                session.data["messages"].append({"role": "user", "content": json.dumps({
                    "campaign_manager": decision, "delivery_limits": self.limits,
                    "instruction": "Meet this concrete delivery contract. The host will return control at the next turn/time boundary.",
                }, ensure_ascii=False)})
                session.data["management_notices"].append(notice_id)
            if decision["decision"] in ("pause", "stop"):
                branch["status"] = "paused" if decision["decision"] == "pause" else "stopped"
                if row["terminal"]:
                    branch["status"] = "delivered" if row["receipt"]["met"] else "needs_followup"
                else:
                    session.data["status"] = "managed_pause" if branch["status"] == "paused" else "managed_stop"
                    session.tools.data["summary"] = decision["instruction"]
                    session.tools.save()
                task = self.data["tasks"].get(job.get("management_task"))
                if task:
                    task.update(status=branch["status"], outcome=row["receipt"])
                if goal["status"] != "blocked_engineering":
                    goal["status"] = "blocked_budget" if row["closing"] else "needs_assignment"
            else:
                branch["contract"] = copy.deepcopy(row["next_contract"])
                branch["status"] = "active"
                if goal["status"] != "blocked_engineering":
                    goal["status"] = "active"
                session.tools.data.setdefault("research_memory", {})["no_information_streak"] = 0
                session.tools.save()
            goal["last_receipt"] = row["receipt"]
            if row["receipt"].get("solution_return"):
                goal["latest_solution"] = row["receipt"]["solution_return"]
            goal["next_deliverable"] = decision["deliverable"]
            goal["next_action"] = decision["instruction"]
            if identity not in goal["evidence"]:
                goal["evidence"].append(identity)
            outcome = decision.get("delivery_outcome")
            if outcome:
                goal.update(status=outcome["status"], outcome=copy.deepcopy(outcome), accepted_checkpoint=row["checkpoint"])
            elif row["terminal"] and row["receipt"]["deliveries"]["candidate"]:
                goal.update(status="delivered", outcome={"status": "delivered", "reason": "Host-gated candidate published."},
                            accepted_checkpoint=row["checkpoint"])
            if goal["status"] in ("delivered", "falsified"):
                for task in self.data["tasks"].values():
                    if task.get("delivery_id") == goal["id"] and task["status"] in ("pending", "paused", "waiting_evidence"):
                        task.update(status="cancelled", cancellation_reason="Delivery accepted; redundant execution is not needed.")
            session.data.pop("management_reason", None)
            self.controller.artifact(session.path, session.data)
            self.controller.state["research_sessions"][session.identity]["status"] = session.data["status"]
            branch.pop("pending_review", None)
            row["applied"] = True
            self.controller.milestone(notice_id, "management_decision", session=session.identity,
                summary=decision["summary"], decision=decision["decision"], deliverable=decision["deliverable"],
                met=row["receipt"]["met"], checkpoint=row["checkpoint"])
            self.controller.checkpoint()
        return branch["status"] == "active"

    def finish(self, session, job):
        self.review(session, job, terminal=True)

    def return_unstarted(self, jobs):
        for job in jobs:
            task = self.data["tasks"].get(job.get("management_task"))
            if task and job["status"] in ("pending", "cancelled") and task["status"] == "assigned":
                task["status"] = "pending"
                for key in ("unit", "session"):
                    task.pop(key, None)
        self.controller.checkpoint()
