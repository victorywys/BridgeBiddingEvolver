"""Durable tool-using protocol researchers, separate from the trusted tournament."""

import copy
import json
import time

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evolution.models import ModelClient, request_body, response_text
from bridge_evolve.evolution.feedback import short_text
from bridge_evolve.evolution.handoff import EngineRevisionReady
from bridge_evolve.evolution.research_tools import IMPLEMENTATION_TOOL, MAX_RESULT_BYTES, ResearchTools, TOOL_DEFINITIONS
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evolution.memory import prime_working_set, working_set
from bridge_evolve.evolution.costs import budget_usage, elapsed, move_request_to_maintenance
from bridge_evolve.evolution.execution import EvolutionStopped
from bridge_evolve.evolution.delivery import (
    DeliveryDeadline, DeliveryWindow, RETURN_CHOICE, RETURN_SOLUTION, enabled as solution_return_enabled,
)
from bridge_evolve.evolution.models import ModelRequestError, token_reservation
from bridge_evolve.evolution.context import (
    ContextCapacityError, assemble_context, latest_interaction, request_manifest,
)

RESEARCH_PROMPT = """Design executable v2 bridge protocols from blank; ALL bidding meanings/rules/states/features/transitions/fallback editable.
Complexity/novelty aren't fitness. Cover public facts, prioritized hand partitions, call guarantees/receivers/interference/exits.
Match sender/receiver; possible senders aren't guarantees. Expose holes/overlap/shadowing/impossible forcing.
Implement/RUN boundary/interference tests and real-DDS trials; falsify mechanisms, critique/repair/reject counterexamples.
Require empty-prefix natural whole-auction change witnesses, not injected histories/renaming/refusal removal.
Offline: approved past deals/DDS; DDS optima aren't bid labels. Runtime: ONLY own hand/public observations.
No host code/shell. Never edit opponents/scoring/evaluator/oracle/budgets/unseen data/engine; request missing operators.
Publish complete snapshots only after ALL tests/gates and changed_from_parent=true. Release isn't promotion: trusted fresh common-deal evaluation decides.
Retain drafts/failures; never invent results. Chinese public evidence, no private reasoning.
Reuse IR/diffs, not a scan of the whole IR; narrow pointers/read_result for missing/changed dependencies. Paginate; array edit_precondition.
Edit raw IR, not navigation metadata. Read/run frozen module contracts; only declared exterior immutable. Workspace owns tests/cases.
Handoff facts/blockers/next actions.
"""


class ResearchSession:
    def __init__(self, controller, generation: int, lineage: str, parent: str, packet: dict, *, assignment=None, identity=None):
        self.controller = controller
        self.identity = identity or f"g{generation:03d}-{lineage}"
        self.assignment = assignment
        self.timeboxed_solution = bool(assignment and solution_return_enabled(controller.config))
        self.delivery_window = None
        self.force_solution_reason = None
        self.campaign = controller.config.get("research_budget_mode") == "campaign"
        self.compaction_ratio = .92 if self.campaign else .7
        self.definitions = TOOL_DEFINITIONS + ([IMPLEMENTATION_TOOL] if assignment else [])
        if self.timeboxed_solution:
            self.definitions = [item for item in self.definitions if item["name"] != "finish_research"] + [RETURN_SOLUTION]
        self.prompt = RESEARCH_PROMPT if assignment is None else (
            "You are the implementation/feasibility worker for ONE assigned protocol hypothesis. "
            "The research director owns strategy. Implement, test and report only this assignment; do not invent unrelated projects. "
            "Finish explicitly if inexpressible, blocked or rejected. Publishing ends this worker after trusted evaluation; "
            "the director will receive the report and decide next steps. Use update_implementation_state for stage/next_action. "
            "Persist the current public-position/hand-partition/call/continuation table in stage_design with that tool; "
            "reuse it after handoff and read only changed or missing interfaces. The director needs outcomes and gaps, not DSL detail. "
            "Retained IR working sets are current source, not hints to reread. Never edit baselines or scoring.\n" + RESEARCH_PROMPT)
        if self.timeboxed_solution:
            self.prompt += (
                "\nThis is a bounded function-like call: return_solution is MANDATORY before its turn/time limit. "
                "Each request includes the current countdown; the final slot is reserved exclusively for return. "
                "Return a concrete current-best scheme even if incomplete/unimplemented/untested. "
                "Plain text, blockers or more research steps alone do not finish the call. "
                "Publication still needs all existing gates and also requires a solution return afterward.")
        self.path = controller.directory / "research" / self.identity / "session.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        initial = controller.system(parent).to_data()
        carried = None
        continued_from = None
        seed = controller.state.get("research_seeds", {}).get(lineage)
        if seed is not None and seed.get("parent", parent) == parent:
            contents = json.loads((controller.directory / seed["path"]).read_text())
            carried = contents["workspace"]
            initial = carried["system"]
            continued_from = {"run": seed["source_run"], "session": seed["source_session"], "frozen_seed": seed["path"]}
        if assignment and assignment.get("base_candidate"):
            candidate = assignment["base_candidate"]
            if f"systems/{candidate}.json" not in controller.state["system_files"]:
                raise ValueError("The assigned research base must be a registered frozen candidate.")
            initial = controller.system(candidate).to_data()
            metadata = controller.state["candidate_archive"].get(candidate)
            release = controller.read_artifact(controller.directory / metadata["release"]) if metadata and metadata.get("release") else None
            carried = {"system": initial, "tests": copy.deepcopy(release["test_definitions"]) if release else [],
                       "cases": copy.deepcopy(release["cases"]) if release else {},
                       "notes": [{"kind": "research", "text": "Explicit director portfolio branch; historical scores are not new evidence."}]}
            for test in carried["tests"]:
                test.pop("changed_from_parent", None)
            continued_from = {"candidate": candidate, "comparison_parent": parent, "explicit_director_branch": True}
        for key, row in reversed(list(controller.state.get("research_sessions", {}).items())) if assignment is None else []:
            if key != self.identity and row["lineage"] == lineage and row["parent"] == parent:
                previous = controller.read_artifact(controller.directory / row["workspace"])
                if previous is not None:
                    from bridge_evolve.evolution.controller import frozen_workspace
                    initial, carried = previous["system"], frozen_workspace(controller.directory, controller.state, previous)
                    continued_from = row["workspace"]
                    break
        if assignment and assignment.get("continue_from"):
            metadata = (controller.state.get("research_sessions", {}).get(assignment["continue_from"])
                        or controller.state.get("research_branches", {}).get(assignment["continue_from"]))
            if (metadata is None or metadata["parent"] != parent and not self.campaign or metadata["lineage"] != lineage
                    or assignment["continue_from"] == self.identity):
                raise ValueError("A director continuation must name another saved job on the same frozen parent and lineage.")
            previous = controller.read_artifact(controller.directory / metadata["workspace"])
            if previous.get("releases"):
                raise ValueError("Published jobs must be compared as frozen candidates, not resumed as unfinished drafts.")
            from bridge_evolve.evolution.controller import frozen_workspace
            carried = frozen_workspace(controller.directory, controller.state, previous)
            if metadata["parent"] != parent:
                for test in carried["tests"]:
                    test.pop("changed_from_parent", None)
            initial = carried["system"]
            continued_from = {"implementation": assignment["continue_from"], "explicit_director_handoff": True}
        if carried is None:
            release_entry = next((entry for generation in reversed(list(controller.state["generations"].values()))
                                  for entry in generation["candidates"].values()
                                  if entry.get("hash") == parent and entry.get("release")), None)
            if release_entry is not None:
                release = controller.read_artifact(controller.directory / release_entry["release"])
                if release is not None:
                    carried = {"system": initial, "tests": copy.deepcopy(release["test_definitions"]),
                               "cases": release["cases"], "notes": [{"kind": "research", "text": release["hypothesis"]}]}
                    for test in carried["tests"]:
                        test.pop("changed_from_parent", None)
                    continued_from = {"release": release_entry["release"], "champion": parent}
        self.client = ModelClient(controller.config, controller.state, controller.checkpoint, controller.stop,
            request_timeout=controller.model_time_window,
            reserve=(lambda: (self.delivery_window.reserve if self.delivery_window and not self.delivery_window.closing else {})
                     if self.timeboxed_solution else controller.state.get("management", {}).get("branches", {}).get(
                         self.identity, {}).get("closeout_reserve", {})) if controller.config.get("campaign_manager") else None)
        self.tools = ResearchTools(controller, self.identity, parent, packet, initial, self.client)
        if assignment is not None:
            if self.tools.data.get("assignment") not in (None, assignment):
                raise ValueError("The frozen implementation assignment changed during recovery.")
            self.tools.data["assignment"] = copy.deepcopy(assignment)
        if carried is not None and not self.tools.data["operations"]:
            migrated = set()
            for identity, value in carried.get("feedback_results", {}).items():
                if self.tools.full_result(value)["result_id"] != identity:
                    raise ValueError("Inherited feedback result identity changed.")
                migrated.add(identity)

            def remap(value):
                if isinstance(value, dict):
                    result = {key: remap(child) for key, child in value.items()}
                    if result.get("result_id") in migrated and "session" in result:
                        result["session"] = self.identity
                    return result
                if isinstance(value, list):
                    return [remap(child) for child in value]
                return value

            for key in ("tests", "cases", "notes", "last_tests", "last_trial", "prior_parent_evidence",
                        "research_memory", "implementation_state", "last_development_evaluation", "best_solution",
                        "provisional_proposals"):
                if key in carried:
                    value = remap(carried[key]) if key in ("notes", "last_tests", "last_trial", "prior_parent_evidence", "last_development_evaluation") else copy.deepcopy(carried[key])
                    self.tools.data[key] = self.tools.feedback(value, limit=6000) if key in ("last_tests", "last_trial") else value
            if assignment and self.tools.data.get("research_memory"):
                self.tools.data["research_memory"]["no_information_streak"] = 0
            self.tools.save()
        if controller.research_contract is not None and not self.tools.data["operations"]:
            for case in controller.research_contract.cases():
                self.tools.data["cases"].setdefault(case["case_id"], {
                    "name": case["id"], "board": case["board"], "source": case["source"], "side": case["side"]})
            self.tools.save()
        self.data = controller.read_artifact(self.path) or {
            "schema": "bridge-research-session/v1", "id": self.identity, "lineage": lineage, "parent": parent,
            "model": controller.config["model"], "status": "ready", "turns": 0, "spent_seconds": 0,
            "continued_from": continued_from,
            "messages": [], "pending_calls": [], "responses": [], "events": [], "context_compactions": 0,
        }
        self.previous_seconds = self.data["spent_seconds"]
        self.started = time.monotonic()
        if self.campaign:
            prime_working_set(self.tools)
            self.tools.save()
        if not self.data["messages"]:
            self.data["messages"] = self.initial_messages(packet)
        if len(request_body(controller.config, self.data["messages"], self.definitions)) > controller.config["max_context_bytes"] * self.compaction_ratio:
            self.compact("Recovered saved research context; full evidence is retained through bounded result references.")
        self.save()

    def initial_messages(self, packet: dict) -> list:
        config = self.controller.config
        generation = self.controller.state["generations"].get(str(int(self.identity[1:4])), {})
        feedback = [{key: value[key] for key in ("lineage", "status", "errors", "full_delta", "changes", "proposal_summary") if key in value}
                    for prior in self.controller.state["generations"].values() if prior is not generation
                    for value in prior["candidates"].values() if value.get("lineage") == self.data["lineage"]][-12:]
        intro = {
            "task": config["research_task"], "session": self.identity, "lineage": self.data["lineage"],
            "incumbent_hash": self.data["parent"], "working_top_level": list(self.tools.data["system"]),
            "working_rule_count": len(self.tools.data["system"]["rules"]) if isinstance(self.tools.data["system"].get("rules"), list) else None,
            "origin": "controlled-module" if config.get("module_contract") else "blank" if self.data["lineage"] == "Z" else "existing",
            "module_contract": {key: value for key, value in config["module_contract"]["definition"].items()
                                if key not in ("cases", "anchor")} if config.get("module_contract") else None,
            "continued_from": self.data.get("continued_from"),
            "benchmark_revision": self.controller.state.get("benchmark_revision"),
            "budgets": {key: config[key] for key in ("max_research_turns", "max_tool_calls", "max_releases",
                        "max_lab_boards", "research_seconds", "max_agent_requests", "max_agent_tokens", "max_pairs")},
            "diagnosis": {"groups": packet["total_groups"], "selected_groups": [
                {"id": group["id"], "rule_id": group["rule_id"], "occurrences": group["occurrences"],
                 "mean_imp": group["imp_sum"] / group["occurrences"], "examples": len(group["examples"]),
                 "pointer": f"/selected_groups/{position}"}
                for position, group in enumerate(packet["selected_groups"][:8])]},
            "navigation": self.navigation_intro(packet),
            "previous_research_outcomes": self.tools.feedback(feedback, limit=6000),
            "saved_tests": self.tools.feedback(self.tools.data.get("last_tests"), limit=6000),
            "saved_trial": self.tools.feedback(self.tools.data.get("last_trial"), limit=6000),
            "saved_contract": self.tools.feedback(self.tools.data.get("last_contract"), limit=6000),
            "instruction": (
                "Continue the saved design rather than rebuilding from blank. Inspect saved tests/notes/trial, repair if needed, re-run tests and publish a coherent release when ready. Previous trials are historical development evidence, not this run's fitness."
                if self.data.get("continued_from") is not None else "Start with a research note and inspect what you need."
            ) + " All rules are editable; diagnosis is a clue, not a whitelist. Use read_workspace for saved cases/tests and read_evidence for the complete approved packet.",
        }
        if self.controller.config.get("development_sampling") is not None:
            intro["development_sampling"] = self.controller.config["development_sampling"]
            intro["evaluation_scope"] = "Conditional development, not uniform global strength. trial and release evaluation draw fresh shared cohort deals; saved diagnostic cases are not reused as scores."
        if self.controller.protocol_goal is not None:
            intro["protocol_goal"] = self.controller.protocol_goal.data
            intro["selection_policy"] = self.controller.config["selection_policy"]
        if self.assignment is not None:
            intro["implementation_assignment"] = self.assignment
            if config.get("campaign_manager"):
                intro["management_contract"] = self.controller.state.get("management", {}).get(
                    "branches", {}).get(self.identity, {}).get("contract")
            intro["instruction"] = "Execute this assigned hypothesis against its frozen parent. Reuse inherited regressions, add a natural change witness. Publish or return a concrete blocker; the director decides further research."
        if self.campaign:
            intro["working_set"] = working_set(self.tools, min(768000, config["max_context_bytes"] // 2))
            intro["instruction"] += (
                " The actual current protocol working set is supplied, not a navigation hint. Do not fetch unchanged definitions again. "
                "Use large targeted generate_cases cohorts and trial.sampling with natural auction targets; the host expands DDS "
                "batches until sufficient changed-family coverage or the explicit trial cap. Generated cases and DDS trials "
                "have separate ample budgets. Retained candidates may be improved or merged with merge_candidate; still test "
                "the integrated protocol completely. Interface corrections use a separate maintenance ledger.")
        return self.context_messages(intro)

    def context_messages(self, context, *, maximum=None):
        if self.controller.config.get("campaign_manager"):
            context["management_contract"] = self.controller.state.get("management", {}).get(
                "branches", {}).get(self.identity, {}).get("contract")
        if self.campaign:
            priority = ("task", "management_contract", "implementation_assignment", "protocol_goal", "implementation_state", "working_set",
                        "development_evaluation", "handoff", "notes", "last_tests", "last_trial")
            context = {**{key: context[key] for key in priority if key in context},
                       **{key: value for key, value in context.items() if key not in priority}}
        return assemble_context(self.controller.config, self.prompt, context, self.definitions,
            maximum=maximum, tail=latest_interaction(self.data["messages"]),
            required=("assignment", "implementation_assignment", "management_contract"),
            archive_tail=lambda messages: self.tools.full_result({"messages": messages}))

    def record_request_context(self, messages, definitions, choice):
        manifest = request_manifest(self.controller.config, messages, definitions, tool_choice=choice)
        number = str(self.data["turns"] + 1)
        self.data.setdefault("context_requests", {})[number] = {**manifest, "status": "prepared"}
        self.controller.artifact(self.path.parent / f"request-{int(number):04d}-input.json", {
            "messages": messages, "tool_names": [item["name"] for item in definitions],
            "tool_choice": choice, "context": manifest})
        self.save()

    def receive_request_context(self):
        number = str(self.data["turns"] + 1)
        manifest = self.data["context_requests"][number]
        manifest["status"] = "received"
        memory = self.tools.data.setdefault("research_memory", {"nodes": {}, "seen": [], "no_information_streak": 0})
        delivered = memory.setdefault("context_deliveries", {})
        for result in manifest["tool_results"]:
            operation = self.tools.data["operations"].get(result["call_id"])
            if operation is not None:
                delivered.pop(result["call_id"], None)
                delivered[result["call_id"]] = {
                    "request": int(number), "sha256": result["sha256"],
                    "full_tool_result": (operation.get("status") == "completed" and "result" in operation
                                         and canonical_hash(operation["result"]) == result["sha256"])}
        while len(delivered) > 512:
            delivered.pop(next(iter(delivered)))
        self.tools.save()

    def navigation_intro(self, packet: dict) -> dict:
        raw = self.tools.data["system"]
        rules = raw.get("rules")
        features = raw.get("features")
        rule_ids = {rule.get("id") for rule in rules if isinstance(rule, dict)} if isinstance(rules, list) else set()
        keys = ["rule:" + group["rule_id"] for group in packet["selected_groups"]
                if group["rule_id"] in rule_ids]
        return {
            "tool": "navigate_system",
            "inventory": {"rules": len(rules) if isinstance(rules, list) else None,
                          "features": len(features) if isinstance(features, dict) else None,
                          "cases": len(self.tools.data["cases"]), "tests": len(self.tools.data["tests"]),
                          "notes": len(self.tools.data["notes"]), "releases": len(self.tools.data["releases"])},
            "start": {"view": "read", "keys": list(dict.fromkeys(keys))[:4]} if keys else {"view": "catalog"},
            "comparison": {"request": {"view": "diff", "kind": "transition"}, "compare_to": "parent"},
            "evidence": "read_evidence(view=packet,pointer=/selected_groups/<index>/examples/<index>)",
            "scope": "Default to relevant context, not restricted editing. Saved work belongs to this session's lineage.",
            "intermediate_edits": "If semantic compilation fails, use raw read_system/edit_system to repair; no valid fallback is substituted.",
        }

    def save(self):
        self.data["spent_seconds"] = self.previous_seconds + time.monotonic() - self.started
        workspace = str(self.tools.path.relative_to(self.controller.directory))
        self.controller.state.setdefault("research_sessions", {})[self.identity] = {
            **{key: self.data[key] for key in ("id", "lineage", "parent", "model", "status", "turns", "spent_seconds")},
            "tool_calls": len(self.tools.data["operations"]), "releases": len(self.tools.data["releases"]),
            "research_turns": self.data["turns"] - len(self.data.get("maintenance_turns", [])),
            "maintenance_turns": len(self.data.get("maintenance_turns", [])),
            "path": str(self.path.relative_to(self.controller.directory)), "workspace": workspace,
            "updated_at": timestamp(), "working": self.tools.summary(),
            **({"role": "implementer", "assignment": self.assignment} if self.assignment else {}),
        }
        self.controller.artifact(self.path, self.data)

    def recover_interface(self, message):
        if not self.controller.config.get("maintenance_budget"):
            raise ValueError(message)
        row = next((row for row in reversed(self.controller.state.get("model_requests", []))
                    if row["label"] == f"{self.identity}:turn:{self.data['turns']}"), None)
        if row is not None:
            move_request_to_maintenance(self.controller, row["request"], message)
        numbers = self.data.setdefault("maintenance_turns", [])
        if self.data["turns"] not in numbers:
            numbers.append(self.data["turns"])
        self.data["recovering_interface"] = True
        event_id = f"{self.identity}:response:{self.data['turns']}"
        self.controller.state.setdefault("maintenance_events", {})[event_id] = {
            "id": event_id, "kind": "model_interface", "session": self.identity, "error": message}
        self.data["messages"].append({"role": "user", "content":
            f"Response interface error: {message}. The original response and fee are preserved; no partial tool was executed. "
            "Return a complete valid tool call. This correction uses the separate maintenance budget."})
        self.save()

    def compact(self, summary: str):
        if self.campaign:
            prime_working_set(self.tools)
        self.data["events"].append({"type": "context_handoff", "summary": short_text(summary, 4000)})
        context = {
                "task": self.controller.config["research_task"], "session": self.identity, "parent": self.data["parent"],
                "handoff": summary, "notes": self.tools.data["notes"][-4:], "last_tests": self.tools.data.get("last_tests"),
                "last_trial": self.tools.data.get("last_trial"),
                "releases": [{key: release[key] for key in ("hash", "hypothesis", "path")} for release in self.tools.data["releases"]],
                "navigation": self.navigation_intro(self.tools.packet),
                "tool_history": [{"id": key, "name": value["name"], "ok": value.get("result", {}).get("ok")}
                                 for key, value in list(self.tools.data["operations"].items())[-24:]],
                "instruction": "Use read_result for omitted evidence and navigate_system for targeted protocol context.",
            }
        memory = working_set(self.tools, min(768000, self.controller.config["max_context_bytes"] // 2)
                             if self.campaign else min(32000, self.controller.config["max_context_bytes"] // 5))
        if memory is not None:
            context["working_set"] = memory
            context["navigation"].pop("start", None)
            context["instruction"] = "Continue the saved next_action. Actual current IR is retained above; only fetch missing or invalidated information, not the same start node."
        if self.assignment is not None:
            context["implementation_assignment"] = self.assignment
            context["implementation_state"] = self.tools.data.get("implementation_state")
        context["development_evaluation"] = self.tools.data.get("last_development_evaluation")
        context["protocol_goal"] = self.controller.protocol_goal.data if self.controller.protocol_goal else None
        self.data["messages"] = self.context_messages(context)
        self.data["context_compactions"] += 1
        self.save()

    def progress_checkpoint(self):
        turn = self.data["turns"]
        limit = self.assignment["turn_budget"] if self.assignment else self.controller.config["max_research_turns"]
        if (turn not in (24, 48, 64) and not (turn > 64 and turn % 32 == 0)
                or turn >= limit):
            return
        checkpoints = self.data.setdefault("unit_checkpoints", [])
        if turn not in checkpoints:
            working = self.tools.summary()
            if len(self.tools.data["releases"]) >= self.controller.config["max_releases"]:
                hint = "发布额度已用完；根据实际评价形成结论并finish_research，不再开启新方向。"
            elif working["authored_tests"] and not working["tests_current"]:
                hint = "已有回归尚未验证当前稿；优先run_tests，再修复或发布，不重复通读接口。"
            else:
                hint = "继续当前完整假设的实现、测试或发布；该检查点不重开上下文，也不是新一代。"
            usage = self.controller.state.get("model_usage", {})
            pool = budget_usage(self.controller.state, "research") if self.controller.config.get("maintenance_budget") else None
            charged = pool["charged_tokens"] if pool else usage.get("charged_tokens")
            checkpoint = {
                "research_unit_checkpoint": turn,
                "remaining_turns": limit - turn,
                "remaining_requests": self.controller.config["max_agent_requests"] - (
                    pool["requests"] if pool else self.controller.state["agent_requests"]),
                "remaining_tokens": self.controller.config["max_agent_tokens"] - charged if type(charged) is int else None,
                "working": {key: working[key] for key in ("hash", "authored_tests", "tests_current")},
                "instruction": hint,
            }
            self.data["messages"].append({"role": "user", "content": json.dumps(checkpoint, ensure_ascii=False)})
            self.data["events"].append({"type": "unit_checkpoint", "turn": turn, "summary": hint, "timestamp": timestamp()})
            checkpoints.append(turn)
        self.controller.milestone(f"checkpoint:{self.identity}:{turn}", "research_checkpoint",
                                  session=self.identity, lineage=self.data["lineage"], turns=turn,
                                  summary=f"第{turn}回合软检查点；保留上下文继续同一研究单元。")
        self.save()

    def solution_context(self):
        from bridge_evolve.evolution.manager import CampaignManager
        from bridge_evolve.systems.navigation import at_pointer
        manager = CampaignManager(self.controller)
        contract = manager.branch(self.identity).get("contract", {})
        receipt = manager.receipt(self, contract)
        pointer = receipt["compilation"].get("pointer")
        focus = None
        if isinstance(pointer, str) and pointer.startswith("/protocol/states/"):
            state_pointer = "/".join(pointer.split("/")[:4])
            focus = {"pointer": state_pointer, "value": at_pointer(self.tools.data["system"], state_pointer)}
            if receipt["compilation"]["error"].startswith("Undefined transition") and pointer.endswith("/to"):
                focus["unresolved_transition"] = {"pointer": pointer, "to": at_pointer(self.tools.data["system"], pointer)}
        return {
            "assignment": self.assignment, "management_contract": contract,
            "implementation_state": self.tools.data.get("implementation_state"),
            "previous_solution": self.tools.data.get("best_solution"),
            "provisional_proposals": [{key: value[key] for key in
                ("id", "status", "paths", "verification", "acceptance")}
                for value in list(self.tools.data.get("provisional_proposals", {}).values())[-10:]],
            "invalid_draft_focus": focus,
            "working_set": working_set(self.tools, min(12000, self.controller.config["max_context_bytes"] // 4)),
            "actual_evidence": receipt,
            "recent_tool_results": [{"call_id": key, "tool": row["name"], "arguments": row["arguments"],
                                     "status": row["status"], "result": row.get("result")}
                                    for key, row in list(self.tools.data["operations"].items())[-6:]],
            "instruction": "Return a concrete current-best scheme, not just next research steps or a blocker. Unimplemented and untested parts must be explicit."}

    def defer_pending_calls(self):
        for call in self.data["pending_calls"]:
            self.data["messages"].append({
                "type": "function_call_output", "call_id": call["call_id"],
                "output": json.dumps({"ok": False, "deferred_for_solution_return": True,
                                      "error": "The bounded call is returning its solution; unfinished work remains saved."})})
            self.data["events"].append({"type": "tool_deferred_for_delivery", "name": call["name"],
                                       "call_id": call["call_id"], "timestamp": timestamp()})
        self.data["pending_calls"] = []
        self.save()

    def run(self) -> dict:
        if not self.timeboxed_solution:
            return self._run()
        contract = self.controller.state["management"]["branches"][self.identity]["contract"]
        self.delivery_window = DeliveryWindow(self.controller,
            self.path.parent / (contract["review"] + ".delivery.json"), self.identity + ":" + contract["review"],
            start_turn=contract["start_turn"], deadline_at=contract["deadline_at"], session=self.identity)
        self.tools.delivery_window = self.delivery_window
        with self.delivery_window.active():
            while True:
                try:
                    return self._run()
                except EngineRevisionReady:
                    if self.delivery_window.closing:
                        raise
                    self.delivery_window.closing = True
                    self.force_solution_reason = "engine_handoff"
                    self.defer_pending_calls()
                    continue
                except (DeliveryDeadline, ModelRequestError) as error:
                    if (not self.delivery_window.closing and self.delivery_window.seconds_left() > 0
                            and (isinstance(error, DeliveryDeadline) or self.delivery_window.should_return(self.data["turns"]))):
                        self.delivery_window.closing = True
                        self.force_solution_reason = ("turn_closeout" if
                            self.delivery_window.data["requests"] >= self.delivery_window.data["turns"] - 1
                            else "time_closeout")
                        self.defer_pending_calls()
                        continue
                    self.delivery_window.fail(error, self.data["turns"])
                    raise
                except (EvolutionStopped, RuntimeError, ValueError, OSError) as error:
                    self.delivery_window.fail(error, self.data["turns"])
                    raise

    def _run(self) -> dict:
        if self.data["status"] in ("completed", "needs_more_research", "stalled"):
            return self.result()
        self.data["status"] = "running"
        self.controller.milestone("research:" + self.identity, "research_started", session=self.identity,
                                  lineage=self.data["lineage"], parent=self.data["parent"], continued_from=self.data.get("continued_from"))
        self.save()
        while True:
            cached = self.data["pending_calls"]
            if self.timeboxed_solution and not cached:
                saved = self.controller.read_artifact(self.path.parent / f"response-{self.data['turns'] + 1:04d}.json")
                cached = saved.get("output", []) if saved else []
            saved_return = self.timeboxed_solution and len(cached) == 1 and cached[0].get("name") == RETURN_SOLUTION["name"]
            if not saved_return:
                self.controller.stop()
            self.controller.state["phase"] = self.identity + "-research"
            while self.data["pending_calls"]:
                call = self.data["pending_calls"][0]
                try:
                    arguments = json.loads(call["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("Research tool arguments must be a JSON object.")
                except (ValueError, TypeError) as error:
                    result = {"ok": False, "type": type(error).__name__, "error": str(error), "failure_kind": "interface"}
                else:
                    result = self.tools.execute(call["call_id"], call["name"], arguments)
                if result.get("failure_kind") in ("interface", "baseline", "framework") and self.controller.config.get("maintenance_budget"):
                    row = next((row for row in reversed(self.controller.state.get("model_requests", []))
                                if row["label"] == f"{self.identity}:turn:{self.data['turns']}"), None)
                    if row is not None:
                        move_request_to_maintenance(self.controller, row["request"], result["error"])
                    numbers = self.data.setdefault("maintenance_turns", [])
                    if self.data["turns"] not in numbers:
                        numbers.append(self.data["turns"])
                    self.data["recovering_interface"] = result["failure_kind"] == "interface"
                    operation = self.tools.data["operations"].get(call["call_id"])
                    if operation is not None:
                        operation["budget"] = "maintenance"
                        self.tools.save()
                elif result["ok"]:
                    self.data["recovering_interface"] = False
                output = json.dumps(result, ensure_ascii=False)
                if len(output.encode()) > self.tools.result_limit:
                    output = json.dumps({"ok": result["ok"], "needs_narrower_read": True,
                        "result_pointer": "/operations/" + call["call_id"].replace("~", "~0").replace("/", "~1") + "/result"})
                self.data["messages"].append({"type": "function_call_output", "call_id": call["call_id"], "output": output})
                self.data["events"].append({"type": "tool_result", "call_id": call["call_id"], "name": call["name"],
                                           "ok": result["ok"], "summary": output[:2000], "timestamp": timestamp()})
                self.data["pending_calls"].pop(0)
                self.save()
                if self.delivery_window and self.delivery_window.data["status"] == "returned":
                    self.defer_pending_calls()
                    break
                if result.get("evaluation") and self.controller.evaluator is not None:
                    if self.timeboxed_solution:
                        self.force_solution_reason = "evaluation_queued"
                        if call["name"] == "publish_candidate":
                            self.tools.data.update(finished=True, summary=(
                                f"已发布「{self.assignment['title']}」并排队可信评价；"
                                "本周期先返回具体方案，实际评价与晋升尚未完成。"))
                        self.tools.save()
                        self.defer_pending_calls()
                        continue
                    self.controller.process_evaluations()
                    job = self.controller.evaluations.jobs[result["evaluation"]["id"]]
                    self.data["messages"].append({"role": "user", "content": json.dumps({
                        "development_evaluation": self.tools.feedback(job, limit=12000),
                        "instruction": "This is development feedback, not promotion. Continue or finish using the actual result.",
                    }, ensure_ascii=False)})
                    self.tools.data["last_development_evaluation"] = self.tools.feedback(job, limit=8000)
                    if self.assignment is not None and call["name"] == "publish_candidate":
                        outcome = job.get("outcome", {})
                        gain = outcome.get("full_delta")
                        verdicts = {"development_complete": "开发评价已完成", "no_full_gain": "本轮未优于亲本",
                                    "insufficient_coverage": "样本未触及修改", "invalid": "未通过有效性验收",
                                    "comparison_blocked": "评价受阻", "budget_blocked": "评价资源不足",
                                    "budget_exhausted": "评价额度用尽"}
                        score = f"，相对亲本{gain:+.4f} IMP/副" if type(gain) in (int, float) else ""
                        tests = result["tests"]
                        self.tools.data.update(finished=True, summary=(
                            f"已实现并冻结「{self.assignment['title']}」。"
                            f"真实回归{tests['passed']}/{tests['tests']}通过，"
                            f"{tests['change_witnesses']}项相对亲本的行为变化见证。"
                            f"{verdicts.get(job['status'], '评价任务已保存')}{score}。"
                            "已返回研究负责人，由可信新牌比较决定是否接受；本汇报不自行宣称升级。"))
                    self.tools.save()
                    self.save()
            returned = self.delivery_window and self.delivery_window.data["status"] == "returned"
            if self.tools.data["finished"] and (not self.timeboxed_solution or returned):
                self.data["status"] = "completed"
                self.controller.milestone("research-end:" + self.identity, "research_completed", session=self.identity,
                    lineage=self.data["lineage"], releases=len(self.tools.data["releases"]), summary=self.tools.data.get("summary", ""))
                self.save()
                return self.result()
            if self.timeboxed_solution and self.tools.data["finished"]:
                self.force_solution_reason = "completed_work"
            from bridge_evolve.evolution.manager import management_due
            reason = management_due(self)
            if reason:
                self.data["status"] = "awaiting_management"
                self.data["management_reason"] = reason
                self.controller.state["management"]["branches"][self.identity]["status"] = "awaiting_management"
                self.data["events"].append({"type": "management_handoff", "reason": reason, "timestamp": timestamp()})
                self.controller.milestone(f"manager-handoff:{self.identity}:{self.data['turns']}",
                    "management_handoff", session=self.identity, reason=reason, turns=self.data["turns"])
                self.save()
                return self.result()
            self.progress_checkpoint()
            no_information = self.tools.data.get("research_memory", {}).get("no_information_streak", 0)
            if self.assignment and no_information >= 12 and not self.campaign:
                self.data["status"] = "stalled"
                self.tools.data["summary"] = "连续读取未产生新信息，已保留工作集并返回负责人；未伪造发布或成绩。"
                self.tools.save()
                self.save()
                return self.result()
            if self.assignment and no_information in (6, 12):
                self.data["messages"].append({"role": "user", "content": "Repeated reads returned no new information. Use the retained working set to implement/test, or report a concrete blocker. Do not restart navigation."})
            turn_limit = self.assignment["turn_budget"] if self.assignment else self.controller.config["max_research_turns"]
            seconds_limit = self.assignment.get("seconds_budget", self.controller.config["research_seconds"]) if self.assignment else self.controller.config["research_seconds"]
            research_turns = self.data["turns"] - len(self.data.get("maintenance_turns", []))
            maintenance_seconds = sum(elapsed(row.get("started_at"), row.get("finished_at")) or 0
                                      for row in self.controller.state.get("model_requests", [])
                                      if row.get("budget") == "maintenance" and row["label"].startswith(self.identity + ":"))
            operations = sum(row.get("budget", "research") == "research"
                             for row in self.tools.data["operations"].values())
            if (research_turns >= turn_limit
                    or operations >= self.controller.config["max_tool_calls"]
                    or self.data["spent_seconds"] - maintenance_seconds >= seconds_limit):
                if self.timeboxed_solution:
                    self.force_solution_reason = "episode_closeout"
                else:
                    self.data["status"] = "needs_more_research"
                    self.tools.data["summary"] = (
                        f"研究回合/工具/时间额度达到本会话上限；保留{len(self.tools.data['releases'])}个发布物、"
                        f"{len(self.tools.data['tests'])}项已编写测试及全部工作，未完成部分不计为有效候选。")
                    self.tools.save()
                    self.controller.milestone("research-end:" + self.identity, "research_budget", session=self.identity,
                        lineage=self.data["lineage"], releases=len(self.tools.data["releases"]), turns=self.data["turns"])
                    self.data["events"].append({"type": "episode_budget", "summary": "Research episode budget reached; saved work is retained for the next generation."})
                    self.save()
                    return self.result()
            if self.tools.data.get("handoff"):
                summary = self.tools.data.pop("handoff")
                self.tools.save()
                self.compact(summary)
            if len(request_body(self.controller.config, self.data["messages"], self.definitions)) > self.controller.config["max_context_bytes"] * self.compaction_ratio:
                self.compact("Automatic context checkpoint. Consult the saved notes, tests, trial and full tool-result ledger before continuing.")
            response_path = self.path.parent / f"response-{self.data['turns'] + 1:04d}.json"
            response = self.controller.read_artifact(response_path)
            if response is None:
                pool = "maintenance" if self.data.get("recovering_interface") else "research"
                messages, definitions, choice = self.data["messages"], self.definitions, None
                if self.timeboxed_solution:
                    window = self.delivery_window
                    window.closing = bool(self.force_solution_reason or window.should_return(self.data["turns"]))
                    window.reason = (self.force_solution_reason or ("turn_closeout" if
                        window.data["requests"] >= window.data["turns"] - 1 else "time_closeout")) if window.closing else None
                    closing_messages = window.return_messages(self.solution_context(),
                        tail=latest_interaction(self.data["messages"]),
                        required=("assignment", "management_contract"),
                        archive_tail=lambda messages: self.tools.full_result({"messages": messages}))
                    reserve = window.hold_return_request(closing_messages, "research")
                    usage = budget_usage(self.controller.state, "research")
                    reminder = window.preview_reminder()
                    def work_fits(candidate):
                        return (usage["requests"] + 2 <= self.controller.config["max_agent_requests"]
                            and usage["charged_tokens"] + reserve["tokens"] + token_reservation(
                                request_body(self.controller.config, [*candidate, reminder], definitions),
                                self.controller.config["max_output_tokens"]) <= self.controller.config["max_agent_tokens"])
                    focused = False
                    if not window.closing and not work_fits(messages):
                        focused = True
                        reminder_bytes = (len(request_body(self.controller.config, [reminder], definitions))
                                          - len(request_body(self.controller.config, [], definitions)))
                        maximum = min(self.controller.config["max_context_bytes"] - reminder_bytes,
                            self.controller.config["max_agent_tokens"] - usage["charged_tokens"]
                            - reserve["tokens"] - self.controller.config["max_output_tokens"] - 4096 - reminder_bytes)
                        try:
                            if maximum <= 0:
                                raise ContextCapacityError("No input allowance remains after the delivery reserve.")
                            messages = self.context_messages(self.solution_context(), maximum=maximum)
                        except ContextCapacityError:
                            window.closing, window.reason = True, "resource_closeout"
                    if not window.closing and not work_fits(messages):
                        window.closing, window.reason = True, "resource_closeout"
                    if window.closing:
                        pool = "research"
                        messages, definitions, choice = closing_messages, [RETURN_SOLUTION], RETURN_CHOICE
                    messages = [*messages, window.reminder(self.data["turns"])]
                    self.data.setdefault("delivery_requests", {})[str(self.data["turns"] + 1)] = {
                        "window": window.data["id"], "closing": window.closing,
                        "context_mode": "solution_return" if window.closing else "budget_focus" if focused else "conversation"}
                    self.save()
                if self.controller.config.get("campaign_manager") and pool == "research" and not self.timeboxed_solution:
                    from bridge_evolve.evolution.manager import CampaignManager
                    if not CampaignManager(self.controller).worker_budget_available(self):
                        self.data.update(status="awaiting_management", management_reason="closeout_budget")
                        self.controller.state["management"]["branches"][self.identity]["status"] = "awaiting_management"
                        self.data["events"].append({"type": "management_handoff", "reason": "closeout_budget", "timestamp": timestamp()})
                        self.save()
                        return self.result()
                self.record_request_context(messages, definitions, choice)
                try:
                    client = self.client if pool == "research" else ModelClient(
                        self.controller.config, self.controller.state, self.controller.checkpoint,
                        self.controller.maintenance_stop, request_timeout=self.controller.maintenance_time_window, budget=pool)
                    options = {"tool_choice": choice} if choice else {}
                    response = client.request(messages, definitions,
                                              label=f"{self.identity}:turn:{self.data['turns'] + 1}", **options)
                except EvolutionStopped as error:
                    if pool != "maintenance" or (self.controller.directory / "cancel-requested").exists():
                        raise
                    self.data["status"] = "blocked_harness"
                    self.tools.data.update(finished=True, summary=f"接口修复独立额度不足，研究草稿已保留：{error}")
                    self.tools.save()
                    self.save()
                    return self.result()
                self.controller.artifact(response_path, response)
                if pool == "maintenance":
                    self.data.setdefault("maintenance_turns", []).append(self.data["turns"] + 1)
            if str(self.data["turns"] + 1) in self.data.get("context_requests", {}):
                self.receive_request_context()
            self.data["turns"] += 1
            self.data["responses"].append(str(response_path.relative_to(self.controller.directory)))
            if response.get("status") == "incomplete" or any(item.get("status") == "incomplete" for item in response["output"]):
                self.recover_interface("Incomplete model response.")
                continue
            calls = []
            messages = []
            try:
                for item in response["output"]:
                    if item.get("type") == "function_call":
                        if not all(isinstance(item.get(key), str) and item[key] for key in ("name", "arguments", "call_id")):
                            raise ValueError("The configured model returned an invalid function call.")
                        if len(item["name"].encode()) > 128 or len(item["call_id"].encode()) > 512:
                            raise ValueError("The configured model returned oversized function-call metadata.")
                        message = {key: item[key] for key in ("type", "name", "arguments", "call_id")}
                        messages.append(message)
                        calls.append(message)
                    elif item.get("type") == "message":
                        if item.get("role") != "assistant" or not isinstance(item.get("content"), list) or any(
                                not isinstance(block, dict) for block in item["content"]):
                            raise ValueError("The configured model returned an invalid assistant message.")
                        messages.append({key: item[key] for key in ("type", "role", "content")})
                    elif item.get("type") != "reasoning":
                        raise ValueError("The configured research model returned an unsupported output item.")
                text = response_text(response)
                if not calls and not text:
                    raise ValueError("The configured research model returned neither tools nor a readable outcome.")
            except ValueError as error:
                self.recover_interface(str(error))
                continue
            if self.timeboxed_solution and self.data.get("delivery_requests", {}).get(str(self.data["turns"]), {}).get("closing"):
                if len(calls) != 1 or calls[0]["name"] != RETURN_SOLUTION["name"]:
                    error = RuntimeError("The closing response did not call return_solution; no periodic delivery was fabricated.")
                    self.save()
                    self.delivery_window.fail(error, self.data["turns"])
                    raise error
            self.data["messages"].extend(messages)
            if text:
                self.data["events"].append({"type": "research_message", "text": text, "timestamp": timestamp()})
            self.data["pending_calls"] = calls
            self.save()
            if not calls:
                if self.timeboxed_solution:
                    self.force_solution_reason = "worker_summary"
                else:
                    self.tools.execute(f"final-{self.data['turns']}", "finish_research", {"summary": text})

    def result(self):
        result = {"source": "research", "session": self.identity, "parent_hash": self.data["parent"],
                "status": self.data["status"], "summary": self.tools.data.get("summary", ""),
                "releases": self.tools.data["releases"]}
        if self.timeboxed_solution:
            contract = self.controller.state["management"]["branches"][self.identity].get("contract", {})
            result["solution_return"] = self.tools.data.get("solution_returns", {}).get(contract.get("review"))
        return result
