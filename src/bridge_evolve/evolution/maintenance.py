"""Tool-using maintainers with durable evidence, executable checks and versioned delivery."""

import copy
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from bridge_evolve.core import Seat, final_contract
from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evolution.director import structured_request
from bridge_evolve.evolution.delivery import DeliveryDeadline, SolutionReturned
from bridge_evolve.evolution.execution import EvolutionStopped, audit_module
from bridge_evolve.evolution.models import MaintenanceBudgetExhausted, ModelRequestError
from bridge_evolve.evolution.research_tools import (
    IMPLEMENTATION_TOOL, STRING, TOOL_DEFINITIONS, ResearchTools, tool,
)
from bridge_evolve.systems import render_system, validate_system
from bridge_evolve.systems.inputs import decision_data, parse_board, protocol_record
from bridge_evolve.systems.language import SystemError
from bridge_evolve.systems.navigation import at_pointer, system_index
from bridge_evolve.systems.runtime import Observation, _decide_and_advance
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld

LOGGER = logging.getLogger(__name__)
_REPLAY_SYSTEMS = {}


class RepairDependencyPending(RuntimeError):
    pass


SUBMIT = tool("submit_repair", "Submit the current executable repair for host replay, regression and independent review. Failed checks return to you for further edits.",
    {"summary": STRING, "basis": STRING, "remaining_issues": STRING}, ("summary", "basis", "remaining_issues"))
REVIEW = tool("review_repair", "Accept or return a verified repair with a concrete next action. No editing or self-approval.",
    {"approve": {"type": "boolean"}, "reason": STRING}, ("approve", "reason"))
REPLAY = tool("replay_incidents", "Replay original failure boards from the empty auction using current or original definitions; retains partial history and exact public-bound errors.",
    {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
     "snapshot": STRING})
AUDIT = tool("audit_repair", "Run original counterexamples and a saved random differential protocol corpus. No DDS/model tokens; not a strength score.", {})
READ_INCIDENT = tool("read_incident", "Read complete saved evidence or validation by JSON pointer; paged, never an unrecoverable summary.",
    {"index": {"type": "integer", "minimum": 0}, "pointer": STRING,
     "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}})
PROMPT = """You own maintenance DELIVERY, not a one-shot patch suggestion.
Diagnose the actual counterexample, query the exact rules/announcements/transitive dependencies you need,
edit the isolated working definition, replay failures and neighbouring/boundary cases, and submit a tested repair.
You may fix existing rules, priorities, features, public announcements, state transitions and their continuations.
Do not preserve a bug just because it is an old rule. Missing context means QUERY it, not stop for approval.
Read stable semantic keys and source pointers; do not dump or reread the entire multi-megabyte system.
Keep sender, public meaning and subsequent choices consistent with the declared bridge system.
Never turn errors into blanket Pass, remove public information merely to evade checks, or weaken the opponent
to help a research candidate. Legitimate bug-fix behaviour changes are allowed and independently reviewed.
The host runs the original full boards and a saved differential corpus, then an independent reviewer.
Failure or reviewer feedback is another engineering iteration, not the end of the job.
No scoring or frozen run changes. Delivery is a new executable baseline version and a continuation handoff.
Use update_implementation_state to retain verified facts, missing evidence and next action.
Chinese reports describe changes, actual checks and remaining limitations, never private reasoning."""


def replay_board(definitions, raw, *, stop=lambda: None):
    board = parse_board(raw)
    world = PublicWorld(definitions, board.dealer, board.vulnerability)
    steps = []
    checked_bounds = {}
    check_bounds = audit_module().bounds_checker()
    while not world.auction.ended:
        stop()
        seat = world.auction.next_seat
        system = definitions[seat.partnership.value]
        observation = Observation(board.deal.hand(seat), seat, board.dealer, board.vulnerability,
                                  world.auction.calls, (), tuple(definitions.items()))
        decision = None
        try:
            decision = _decide_and_advance(system, observation, world)
            step = {"system_hash": system.content_hash, "seat": seat.value, "call": str(decision.call),
                    "state": decision.trace.state, "public_context": protocol_record(decision.trace.public_context),
                    "public_context_after": protocol_record(PublicContext(world, seat.partnership.value, seat).snapshot()),
                    "private_trace": {"rule_id": decision.trace.rule_id}}
            steps.append(step)
            check_bounds([step], board, previous=checked_bounds)
        except SystemError as error:
            return {"status": "failed", "error": {"type": "SystemError", "message": str(error),
                    "actor": error.actor or seat.value, "system_hash": error.system_hash or system.content_hash,
                    "stage": "checks" if decision is not None else "auction"},
                    "position": {"seat": seat.value, "hand": raw["hands"][seat.value],
                        "legal_calls": [str(call) for call in observation.legal_actions],
                        "public_context": protocol_record(decision.trace.public_context) if decision is not None
                            else protocol_record(PublicContext(world, seat.partnership.value, seat).snapshot()),
                        "timing": "before_failed_decision" if decision is None else "before_unsound_announcement"},
                    "auction": steps, "history": [str(call) for call in world.auction.calls]}
    contract = final_contract(world.auction)
    return {"status": "passed", "auction": steps, "history": [str(call) for call in world.auction.calls],
            "contract": str(contract), "scope": "Natural complete auction and public guarantees, no DDS score."}


def initialize_replays(systems):
    global _REPLAY_SYSTEMS
    _REPLAY_SYSTEMS = {identity: validate_system(raw) for identity, raw in systems.items()}


def replay_task(task):
    board, layouts = task
    return tuple(replay_board({side: _REPLAY_SYSTEMS[identity] for side, identity in layout.items()}, board)
                 for layout in layouts)


def parallel_replays(tasks, *, workers, stop):
    stop()
    if workers == 1:
        for board, layouts in tasks:
            yield tuple(replay_board(layout, board, stop=stop) for layout in layouts)
        return
    systems, serialized = {}, []
    for board, layouts in tasks:
        stop()
        rows = []
        for layout in layouts:
            rows.append({side: system.content_hash for side, system in layout.items()})
            for system in layout.values():
                if system.content_hash not in systems:
                    systems[system.content_hash] = system.to_data()
        serialized.append((board, rows))
    executor = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
        initializer=initialize_replays, initargs=(systems,))
    try:
        for result in executor.map(replay_task, serialized):
            stop()
            yield result
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def regression_comparisons(layouts, boards, *, workers, stop):
    rows = [(row, side) for row in boards for side in ("NS", "EW")]
    tasks = [(row["board"], layouts[side]) for row, side in rows]
    for (row, side), (before, after) in zip(
            rows, parallel_replays(tasks, workers=workers, stop=stop), strict=True):
        yield row, side, before, after


def semantic_changes(original, candidate):
    before, after = system_index(original), system_index(candidate)
    changes = []
    for key in dict.fromkeys([*before.nodes, *after.nodes]):
        left, right = before.nodes.get(key), after.nodes.get(key)
        old, new = left.data if left else None, right.data if right else None
        if (right or left).kind == "state" and isinstance(old, dict) and isinstance(new, dict):
            old = {key: value for key, value in old.items() if key != "transitions"}
            new = {key: value for key, value in new.items() if key != "transitions"}
        if canonical_hash(old) != canonical_hash(new):
            changes.append({"key": key, "before": old, "after": new,
                            "pointer": (right or left).pointer})
    return changes


class RepairTools(ResearchTools):
    budget_name = "maintenance"

    def __init__(self, controller, job):
        self.job = job
        versions = controller.state.get("repair_versions", {})
        version = versions.get(job["baseline"], {})
        parent = version.get("hash", job["baseline"])
        path = controller.directory / "repairs" / job["id"] / "workspace.json"
        if version.get("repair") == job["id"]:
            parent = controller.read_artifact(path)["parent"]
        initial = controller.system(parent).to_data()
        super().__init__(controller, job["id"], parent, {"selected_groups": [], "total_groups": 0},
                         initial, None, path=path)
        if self.data["parent"] != parent:
            previous = self.full_result(self.data["system"])
            self.data.update(system=initial, parent=parent, rebased_from=previous)
            self.data.pop("repair_validation", None)
            job["iteration"] = job.get("iteration", 1) + 1
            self.data["notes"].append({"kind": "research", "text":
                "同基线已有另一项验收修复；当前工作稿已以该新版为亲本，旧工作稿完整保存在rebased_from。读取旧改动并显式整合，不能覆盖已交付修复。"})
        for index, name in enumerate(job["evidence"]):
            evidence = controller.read_artifact(controller.directory / name)
            side = Seat(evidence["error"]["actor"]).partnership.value
            self.data["cases"].setdefault(f"incident-{index}", {
                "name": f"原始故障 {index + 1}", "board": evidence["board"], "side": side,
                "layout": evidence["systems"][evidence["error"]["table"]], "source": name})
        self.aliases = {job["baseline"], self.parent}
        self.aliases.update(controller.read_artifact(controller.directory / name)["error"]["system_hash"]
                            for name in job["evidence"])
        job["workspace"] = self.path.relative_to(controller.directory).as_posix()
        self.save()

    @property
    def definitions(self):
        allowed = {"read_system", "navigate_system", "read_workspace", "edit_system", "read_reference",
                   "read_result", "generate_cases", "add_cases", "inspect_case", "write_tests", "run_tests",
                   "validate_system", "research_note"}
        return [item for item in TOOL_DEFINITIONS if item["name"] in allowed] + [
            IMPLEMENTATION_TOOL, READ_INCIDENT, REPLAY, AUDIT]

    def stop(self):
        self.controller.maintenance_stop()

    def save(self):
        super().save()
        self.job.update(tool_calls=len(self.data["operations"]), updated_at=timestamp())

    def layout(self, layout, snapshot="working", *, own=None):
        versions = self.controller.state.get("repair_versions", {})
        own = self.system(snapshot) if own is None else own
        return {side: own if identity in self.aliases
                else self.controller.system(versions.get(identity, {}).get("hash", identity))
                for side, identity in layout.items()}

    def play(self, case_id, snapshot="working", side="NS", prefix=(), *, decision=False):
        case = self.data["cases"][case_id]
        own = self.system(snapshot)
        raw = case["board"]
        if "layout" in case:
            definitions = self.layout(case["layout"], snapshot, own=own)
        else:
            peer = next(item["hash"] for item in self.controller.config["baselines"]
                        if item["hash"] != self.job["baseline"])
            definitions = self.layout({side: self.job["baseline"],
                                      "EW" if side == "NS" else "NS": peer}, snapshot, own=own)
        if decision:
            from bridge_evolve.systems.inputs import parse_prefix
            from bridge_evolve.systems.runtime import decide, replay
            calls = parse_prefix(list(prefix))
            actor = replay(Seat(raw["dealer"]), calls).next_seat
            board = parse_board(raw)
            observation = Observation(board.deal.hand(actor), actor, board.dealer,
                                      board.vulnerability, calls, (), tuple(definitions.items()))
            own = definitions[actor.partnership.value]
            return decision_data(own, observation, decide(own, observation), True)
        if prefix:
            raise ValueError("Maintenance whole-board regressions start from the empty auction; use a decision test for a counterfactual prefix.")
        result = replay_board(definitions, raw, stop=self.stop)
        if result["status"] != "passed":
            raise SystemError(result["error"]["message"], actor=result["error"]["actor"],
                              system_hash=result["error"]["system_hash"])
        return result

    def do_inspect_case(self, args, *_):
        if args.get("dds"):
            raise ValueError("Maintenance protocol diagnostics do not consume research DDS scores; replay_incidents and audit_repair check complete auctions.")
        case = self.data["cases"][args["case_id"]]
        layout = case.get("layout")
        if layout is None:
            return super().do_inspect_case(args, {}, "")
        result = replay_board(self.layout(layout, args.get("snapshot", "working")), case["board"], stop=self.stop)
        if "turn" in args:
            result = {**result, "selected_step": result["auction"][args["turn"] - 1]}
        return result

    def do_validate_system(self, _args, *_):
        system = self.system()
        return {"status": "typed_and_rendered", "system_hash": system.content_hash,
                "specification_bytes": len(render_system(system).encode()), "scope": "Not a semantic proof."}

    def do_read_incident(self, args, *_):
        index = args.get("index", 0)
        evidence = self.controller.read_artifact(self.controller.directory / self.job["evidence"][index])
        return self.page(at_pointer(evidence, args.get("pointer", "")), args.get("offset", 0), args.get("limit", 20))

    def do_replay_incidents(self, args, *_):
        offset, limit = args.get("offset", 0), args.get("limit", 4)
        snapshot = args.get("snapshot", "working")
        own = self.system(snapshot)
        rows = []
        for index, name in enumerate(self.job["evidence"][offset:offset + limit], offset):
            evidence = self.controller.read_artifact(self.controller.directory / name)
            layout = evidence["systems"][evidence["error"]["table"]]
            rows.append({"index": index, "case_id": f"incident-{index}",
                **replay_board(self.layout(layout, snapshot, own=own), evidence["board"], stop=self.stop)})
        return {"total": len(self.job["evidence"]), "offset": offset, "results": rows}

    def do_audit_repair(self, _args, *_):
        return self.audit()

    def replay_evidence(self, *, system=None):
        own = self.system() if system is None else system
        rows = []
        for index, name in enumerate(self.job["evidence"]):
            self.stop()
            evidence = self.controller.read_artifact(self.controller.directory / name)
            rows.extend((index, evidence, table, layout) for table, layout in evidence["systems"].items())
        tasks = [(evidence["board"], (self.layout(layout, own=own),)) for _, evidence, _, layout in rows]
        self.job["validation_progress"] = {"phase": "incidents", "completed_tables": 0,
            "total_tables": len(rows), "workers": self.controller.config["workers"]}
        self.controller.checkpoint()
        results = parallel_replays(tasks, workers=self.controller.config["workers"], stop=self.stop)
        for completed, (row, (actual,)) in enumerate(zip(rows, results, strict=True), 1):
            if completed % 4 == 0 or completed == len(rows):
                self.job["validation_progress"]["completed_tables"] = completed
                self.controller.checkpoint()
            yield *row, actual

    def dependency(self, evidence, table, layout, actual):
        from bridge_evolve.evolution.controller import register_system
        from bridge_evolve.evolution.repairs import record_incident
        own = self.system()
        definitions = self.layout(layout, own=own)
        register_system(self.controller.directory, self.controller.state, own)
        error = {**actual["error"], "table": table,
                 "stage": "checks" if "public" in actual["error"]["message"].lower() else "auction"}
        peer = error["system_hash"]
        anchors = {item["hash"] for item in self.controller.config["baselines"]}
        anchors.update(version["hash"] for version in self.controller.state.get("repair_versions", {}).values())
        if peer not in anchors:
            self.job.update(blocked_by=[], dependency_owner="research",
                            next_action="当前基线已推进到研究候选自身故障；候选需由研究岗位修复，不归基线维护者改动。")
            return
        record = {**evidence["board"], "tables": [{"id": table,
            "systems": {side: {"hash": definition.content_hash} for side, definition in definitions.items()},
            "error": error, "auction": actual["auction"]}], "audit_result": {"errors": [error]}}
        record_incident(self.controller, record, own.content_hash, peer, "maintenance:" + self.job["id"])
        self.job["blocked_by"] = [job["id"] for job in self.controller.state.get("repair_jobs", {}).values()
                                  if job["error"].get("system_hash") == peer]

    def audit(self):
        previous_status = self.job["status"]
        try:
            result = self._audit()
            self.job["validation"] = result
            return result
        finally:
            if self.job["status"] == "validating":
                self.job["status"] = "proposing" if previous_status == "validating" else previous_status
                self.controller.checkpoint()

    def _audit(self):
        system = self.system()
        original = self.controller.system(self.parent)
        changes = semantic_changes(original, system)
        if not changes:
            raise ValueError("No executable repair has been made.")
        if any(change["key"] == "fallback" for change in changes):
            raise ValueError("Repair the responsible branch, not the global error/Pass fallback.")
        corpus = self.path.parent / "regression-boards.json"
        boards = self.controller.read_artifact(corpus)
        if boards is None:
            module = audit_module()
            config = module.sampling_config({"count": 256, "seed": int(self.job["id"][-12:], 16)})
            boards = list(module.sampled_boards(config, cancelled=lambda: self.stop() or False))
            self.controller.artifact(corpus, boards)
        peer = next(item["hash"] for item in self.controller.config["baselines"]
                    if item["hash"] != self.job["baseline"])
        for row in boards:
            for side in ("NS", "EW"):
                self.data["cases"][f"regression-{row['index']}-{side}"] = {
                    "name": f"维护差分回归 {row['index']} / {side}", "board": row["board"], "side": side,
                    "source": "maintenance-regression", "layout": {side: self.job["baseline"],
                        "EW" if side == "NS" else "NS": peer}}
        fingerprint = canonical_hash({"system": system.content_hash, "evidence": self.job["evidence"],
                                      "versions": self.controller.state.get("repair_versions", {}),
                                      "tests": self.data["tests"], "cases": self.data["cases"]})
        if self.data.get("repair_validation", {}).get("fingerprint") == fingerprint:
            return self.data["repair_validation"]
        self.job["status"] = "validating"
        self.controller.checkpoint()
        failures, blocked, replayed = [], [], 0
        for index, evidence, table, layout, actual in self.replay_evidence(system=system):
            replayed += 1
            if actual["status"] != "passed":
                row = {"case_id": f"incident-{index}", "table": table, **actual}
                (failures if actual["error"]["system_hash"] == system.content_hash else blocked).append(row)
                if actual["error"]["system_hash"] != system.content_hash:
                    self.dependency(evidence, table, layout, actual)
        if failures or blocked:
            result = {"status": "failed" if failures else "waiting_dependency", "fingerprint": fingerprint,
                      "system_hash": system.content_hash, "replayed_tables": replayed,
                      "incident_failures": failures, "dependency_failures": blocked, "changes": changes,
                      "regression_boards": 0, "regression_tables": 0,
                      "scope": "Original counterexample replay failed; no random regression claimed."}
            self.data["repair_validation"] = result
            self.controller.artifact(self.path.parent / "validation.json", result)
            self.save()
            return result
        changed, original_valid, new_failures, unscoped = [], 0, [], []
        pre_existing = []
        touched_rules = {item["key"][5:] for item in changes if item["key"].startswith("rule:")}
        semantic_edit = any(not item["key"].startswith("rule:") for item in changes)
        layouts = {side: {side: self.job["baseline"], "EW" if side == "NS" else "NS": peer}
                   for side in ("NS", "EW")}
        comparisons = {side: (self.layout(layout, "parent", own=original), self.layout(layout, own=system))
                       for side, layout in layouts.items()}
        self.job["validation_progress"] = {"completed_tables": 0, "total_tables": len(boards) * 2,
                                           "workers": self.controller.config["workers"]}
        self.controller.checkpoint()
        for completed, (row, side, before, after) in enumerate(regression_comparisons(
                comparisons, boards, workers=self.controller.config["workers"], stop=self.stop), 1):
            if before["status"] == "passed":
                original_valid += 1
                if after["status"] != "passed":
                    failure = {"board": row["index"], "side": side, **after}
                    if after["error"]["system_hash"] == system.content_hash:
                        new_failures.append(failure)
                    else:
                        blocked.append(failure)
                        self.dependency({"board": row["board"]}, "table1", layouts[side], after)
            else:
                pre_existing.append({"case_id": f"regression-{row['index']}-{side}",
                    "board": row["index"], "side": side, "before": before["error"],
                    "after": after.get("error"), "after_status": after["status"]})
            if before["history"] != after["history"]:
                witness = {"board": row["index"], "side": side, "before": before, "after": after}
                changed.append(witness)
                if before["status"] == after["status"] == "passed" and not semantic_edit:
                    old_rules = {step["private_trace"]["rule_id"] for step in before["auction"]}
                    new_rules = {step["private_trace"]["rule_id"] for step in after["auction"]}
                    if not (old_rules | new_rules) & touched_rules:
                        unscoped.append(witness)
            if completed % 8 == 0 or completed == len(boards) * 2:
                self.job["validation_progress"]["completed_tables"] = completed
                self.controller.checkpoint()
        tests = self.test_results()
        result = {"status": "failed" if failures or new_failures or unscoped or tests["failed"]
                  else "waiting_dependency" if blocked else "passed", "fingerprint": fingerprint,
                  "system_hash": system.content_hash, "replayed_tables": replayed,
                  "incident_failures": failures, "dependency_failures": blocked,
                  "regression_boards": len(boards), "regression_tables": len(boards) * 2,
                  "original_valid_tables": original_valid, "new_failures": new_failures,
                  "pre_existing_failures": pre_existing,
                  "unscoped_changes": unscoped, "changed_tables": len(changed), "changes": changes,
                  "change_witnesses": changed, "authored_tests": tests,
                  "scope": "Counterexample replay and differential protocol regression, not exhaustive coverage or proof of optimal strength."}
        self.data["repair_validation"] = result
        self.controller.artifact(self.path.parent / "validation.json", result)
        self.save()
        return result


def repair(controller, job):
    if job["kind"] == "framework":
        from bridge_evolve.evolution.engine_maintenance import repair_engine
        return repair_engine(controller, job)
    try:
        controller.maintenance_stop()
        workspace = RepairTools(controller, job)
        published = controller.state.get("repair_versions", {}).get(job["baseline"])
        if published is None and job.get("source_baseline") != job["baseline"]:
            published = {"hash": job["baseline"], "repair": job.get("continued_from"),
                         "scope": "Explicitly revised frozen baseline."}
        if published and published["hash"] == workspace.parent:
            results = []
            resolved = True
            current_system = workspace.system()
            for name in job["evidence"]:
                evidence = controller.read_artifact(controller.directory / name)
                for layout in evidence["systems"].values():
                    actual = replay_board(workspace.layout(layout, own=current_system), evidence["board"], stop=workspace.stop)
                    results.append(actual)
                    if actual["status"] != "passed":
                        resolved = False
                        break
                if not resolved:
                    break
            if results and resolved:
                controller.artifact(workspace.path.parent / "resolution.json", {"results": results, "version": published})
                job.update(status="resolved_by_revision", candidate=published["hash"],
                           validation={"replayed_tables": len(results), "status": "passed"},
                           resolved_by=published["repair"], next_action="已由同基线另一项验收版本覆盖，原反例复放通过，无需重复改规则。")
                controller.checkpoint()
                return
        job.update(status="proposing", permissions="repair-protocol-with-regression-review",
                   next_action="查询故障依赖、编辑工作稿并复现／验证；失败自动回到维护者。")
        directory = workspace.path.parent
        attempt = job.setdefault("iteration", 1)
        while True:
            controller.maintenance_stop()
            context = {"incident": job["error"], "baseline": job["baseline"], "working": workspace.system().content_hash,
                "evidence_count": len(job["evidence"]), "workspace": workspace.summary(),
                "previous_review": job.get("review"), "last_period_solution": job.get("latest_solution"), "instruction":
                "Start with replay_incidents and the exact responsible node. All IR is queryable; missing definitions are not an approval blocker."}

            def validate_submission(proposal):
                if not proposal["summary"].strip() or not proposal["basis"].strip():
                    raise ValueError("Submit concrete changes and their declared-system basis.")
                if not workspace.data["tests"]:
                    raise ValueError("Save at least one behavioural regression for the repaired branch or its boundary with write_tests, then submit.")
                validation = workspace.audit()
                if validation["status"] == "waiting_dependency":
                    raise RepairDependencyPending("原反例已经推进到另一个体系的缺陷；保存工作稿，先修依赖后复测。")
                if validation["status"] != "passed":
                    reference = workspace.full_result(validation)
                    raise ValueError(f"Repair checks returned {validation['status']}; inspect read_result({reference['result_id']}) and continue fixing.")

            tools = [(definition, lambda args, call_id, name=definition["name"]: workspace.execute(call_id, name, args))
                     for definition in workspace.definitions]
            proposal = structured_request(controller, directory / f"implementation-{attempt}.json", PROMPT,
                context, SUBMIT, f"repair:{job['id']}:implement:{attempt}", validator=validate_submission,
                tools=tools, feedback=workspace.feedback,
                solution_evidence=lambda: {"workspace": copy.deepcopy(workspace.data), "acceptance": "not_yet_independently_reviewed"})
            job.update(status="reviewing", proposal=proposal, validation=workspace.data["repair_validation"])
            controller.checkpoint()
            readonly = {"read_system", "navigate_system", "read_incident", "read_result", "read_workspace", "replay_incidents"}
            def validate_review(review):
                if not review["reason"].strip():
                    raise ValueError("Independent review needs a concrete reason or next engineering action.")

            review = structured_request(controller, directory / f"review-{attempt}.json",
                "Independently review this working baseline repair, not candidate fitness. Query exact original/current "
                "definitions and dependencies as needed. Allow necessary bug-fix changes to existing rules and public "
                "protocol; reject arbitrary weakening, removal of legitimate promises or hidden fallback. "
                "Tests are actual host evidence, not proof of global strength. On rejection give an executable next step.",
                {"incident": job["error"], "proposal": proposal, "validation": workspace.feedback(job["validation"], limit=24000)},
                REVIEW, f"repair:{job['id']}:review:{attempt}", validator=validate_review,
                tools=[item for item in tools if item[0]["name"] in readonly], feedback=workspace.feedback)
            job["review"] = review
            if not review["approve"]:
                attempt += 1
                job.update(iteration=attempt, status="proposing", next_action=review["reason"])
                controller.checkpoint()
                continue
            from bridge_evolve.evolution.controller import register_system
            candidate = workspace.system()
            register_system(controller.directory, controller.state, candidate)
            controller.artifact(directory / "candidate.json", candidate.to_data())
            version = {"hash": candidate.content_hash, "path": str(directory / "candidate.json"),
                       "repair": job["id"], "original": job["baseline"]}
            controller.state.setdefault("repair_versions", {})[job["baseline"]] = version
            job.update(status="accepted_for_next_run", candidate=candidate.content_hash,
                candidate_path=(directory / "candidate.json").relative_to(controller.directory).as_posix(),
                finished_at=timestamp(), next_action="自动准备使用已修复基线的新评测／研究续接，旧分数保留。",
                deployment="新基线已独立验收；交接到新冻结实验，不修改旧成绩。")
            job.pop("repair_error", None)
            controller.checkpoint()
            return
    except SolutionReturned as error:
        job.setdefault("solution_returns", []).append(error.value["id"])
        job.update(status="solution_returned", latest_solution=error.value["solution"],
                   iteration=job.get("iteration", 1) + 1,
                   next_action=error.value["solution"]["next_action"])
        job.pop("repair_error", None)
    except DeliveryDeadline as error:
        LOGGER.warning("Maintenance %s could not return a solution: %s", job["id"], error)
        job.update(status="delivery_failed", repair_error=str(error), iteration=job.get("iteration", 1) + 1,
                   next_action="本周期方案返回失败；保留原始响应与工作稿，下周期从当前工作继续，不冒称交付。")
    except RepairDependencyPending as error:
        action = job.get("next_action") if job.get("dependency_owner") == "research" else str(error)
        job.update(status="waiting_dependency", repair_error=str(error), next_action=action)
    except (MaintenanceBudgetExhausted, EvolutionStopped) as error:
        if (controller.directory / "cancel-requested").exists():
            raise
        job.update(status="deferred", repair_error=str(error), next_action="恢复维护预算后从已保存工具会话继续。")
    except (ModelRequestError, OSError, ValueError, RuntimeError) as error:
        LOGGER.exception("Maintenance %s stopped with retained evidence", job["id"])
        job.update(status="deferred", repair_error=str(error), next_action="从原输入与已保存工作稿重试维护，不重抽失败牌。")
    controller.checkpoint()
