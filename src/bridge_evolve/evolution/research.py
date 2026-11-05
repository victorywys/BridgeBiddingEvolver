"""Durable tool-using protocol researchers, separate from the trusted tournament."""

import copy
import json
import time

from bridge_evolve.evolution.models import ModelClient, response_text
from bridge_evolve.evolution.research_tools import MAX_RESULT_BYTES, ResearchTools, TOOL_DEFINITIONS
from bridge_evolve.evaluation.experiment import timestamp

RESEARCH_PROMPT = """You are an autonomous bridge partnership-protocol researcher, not a JSON patch suggester.
Deliver whole executable v2 bidding systems and same-source complete specifications.
You may add, delete or rewrite ALL bidding meanings, rules, priorities, features,
announcements, public states, transitions, responses and fallback. You may create
an original system from the blank legal scaffold. Existing conventions are not sacred.
Complexity, rule count, novelty and description length are NOT fitness penalties.

Work iteratively with the research tools: locate the relevant branch and its dependencies,
form hypotheses, generate legal constrained cases, debug actual auctions, author and
RUN executable regression tests, run strict real-DDS exploratory trials, request an
independent critique when useful, repair counterexamples and continue improving.
Intermediate edits need not immediately improve the incumbent. Keep sender, public
meaning, receiver, subsequent answers, interference and exits mutually consistent.
Test actual action changes: disabling a refusal rule is not proof of accepting 3NT.
Do not treat one deal's double-dummy best contract as a required bid.

You are an offline researcher and may inspect approved complete past deals and DDS.
The submitted runtime policy may use ONLY its own hand and public observations.
No evaluator, scoring, oracle, opponents, budgets, unseen evaluation data or engine edits.
No arbitrary host code or shell is executed: all protocol edits are data interpreted
by the trusted typed v2 engine. Missing operators require a separate engine-extension
request, never hidden code in a policy. read_reference exposes the actual language.

Publish only complete snapshots whose executable authored tests really pass,
including at least one test with changed_from_parent=true that witnesses an actual
changed call or auction. A renamed or behaviorally untouched policy is not a new design.
Use publish_candidate for releases and finish_research for an explicit outcome.
A release is NOT a promotion: the outer evaluator uses fresh common random boards.
You may publish several independently coherent alternatives within the release budget.
Do not waste a release on renaming an unchanged policy. It is acceptable to explain
that a design remains incomplete; never claim an experiment or test that did not run.

Write concise user-facing research notes, hypotheses, critique questions and final
summaries in Chinese. Record evidence and decisions, not a private thinking transcript.
Large reads are paged; follow keys/indices with narrower JSON pointers. Use handoff
to compact context when needed; all saved work and tool history remain available.

Start with navigate_system, not a scan of the whole IR. Search by state, call,
module or rule ID; batch read stable semantic keys and their dependency closure.
Use references before changing shared features, and diff against parent to locate
migration changes without comparing generated hashes by hand. Definition pages
explicitly report remaining dependencies; follow next_offset when needed.
Use returned edit_precondition tests before editing array-addressed nodes.
Use auction queries for public meaning at a prefix or selected one-based turn,
and hand queries for actual decisions. Possible senders are not public guarantees.
Read unrelated branches only when an interface or shared dependency requires it.
No module is an edit whitelist: cross-branch redesign remains allowed, and full
system validation and authored regression tests still gate every release.
The experiment-wide task may mention other lineages; this session's saved tests
and cases are exactly those in its own workspace inventory, not another lineage's.
"""


class ResearchSession:
    def __init__(self, controller, generation: int, lineage: str, parent: str, packet: dict):
        self.controller = controller
        self.identity = f"g{generation:03d}-{lineage}"
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
        for key, row in reversed(list(controller.state.get("research_sessions", {}).items())):
            if key != self.identity and row["lineage"] == lineage and row["parent"] == parent:
                previous = controller.read_artifact(controller.directory / row["workspace"])
                if previous is not None:
                    initial, carried = previous["system"], previous
                    continued_from = row["workspace"]
                    break
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
        self.client = ModelClient(controller.config, controller.state, controller.checkpoint, controller.stop)
        self.tools = ResearchTools(controller, self.identity, parent, packet, initial, self.client)
        if carried is not None and not self.tools.data["operations"]:
            for key in ("tests", "cases", "notes", "last_tests", "last_trial"):
                if key in carried:
                    self.tools.data[key] = copy.deepcopy(carried[key])
            self.tools.save()
        self.data = controller.read_artifact(self.path) or {
            "schema": "bridge-research-session/v1", "id": self.identity, "lineage": lineage, "parent": parent,
            "model": controller.config["model"], "status": "ready", "turns": 0, "spent_seconds": 0,
            "continued_from": continued_from,
            "messages": [], "pending_calls": [], "responses": [], "events": [], "context_compactions": 0,
        }
        self.previous_seconds = self.data["spent_seconds"]
        self.started = time.monotonic()
        if not self.data["messages"]:
            self.data["messages"] = self.initial_messages(packet)
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
            "origin": "blank" if self.data["lineage"] == "Z" else "existing",
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
            "previous_research_outcomes": feedback,
            "saved_tests": self.tools.data.get("last_tests"), "saved_trial": self.tools.data.get("last_trial"),
            "instruction": (
                "Continue the saved design rather than rebuilding from blank. Inspect saved tests/notes/trial, repair if needed, re-run tests and publish a coherent release when ready. Previous trials are historical development evidence, not this run's fitness."
                if self.data.get("continued_from") is not None else "Start with a research note and inspect what you need."
            ) + " All rules are editable; diagnosis is a clue, not a whitelist. Use read_workspace for saved cases/tests and read_evidence for the complete approved packet.",
        }
        return [{"role": "system", "content": RESEARCH_PROMPT},
                {"role": "user", "content": json.dumps(intro, ensure_ascii=False)}]

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
            "path": str(self.path.relative_to(self.controller.directory)), "workspace": workspace,
            "updated_at": timestamp(), "working": self.tools.summary(),
        }
        self.controller.artifact(self.path, self.data)

    def compact(self, summary: str):
        self.data["events"].append({"type": "context_handoff", "summary": summary})
        self.data["messages"] = [
            {"role": "system", "content": RESEARCH_PROMPT},
            {"role": "user", "content": json.dumps({
                "task": self.controller.config["research_task"], "session": self.identity, "parent": self.data["parent"],
                "handoff": summary, "notes": self.tools.data["notes"][-4:], "last_tests": self.tools.data.get("last_tests"),
                "last_trial": self.tools.data.get("last_trial"),
                "releases": [{key: release[key] for key in ("hash", "hypothesis", "path")} for release in self.tools.data["releases"]],
                "navigation": self.navigation_intro(self.tools.packet),
                "tool_history": [{"id": key, "name": value["name"], "ok": value.get("result", {}).get("ok")}
                                 for key, value in list(self.tools.data["operations"].items())[-24:]],
                "instruction": "All original tool results are retained. Continue using read_workspace/read_system/read_evidence.",
            }, ensure_ascii=False)},
        ]
        self.data["context_compactions"] += 1
        self.save()

    def run(self) -> dict:
        if self.data["status"] in ("completed", "needs_more_research"):
            return self.result()
        self.data["status"] = "running"
        self.controller.milestone("research:" + self.identity, "research_started", session=self.identity,
                                  lineage=self.data["lineage"], parent=self.data["parent"], continued_from=self.data.get("continued_from"))
        self.save()
        while True:
            self.controller.stop()
            self.controller.state["phase"] = self.identity + "-research"
            while self.data["pending_calls"]:
                call = self.data["pending_calls"][0]
                try:
                    arguments = json.loads(call["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("Research tool arguments must be a JSON object.")
                except (ValueError, TypeError) as error:
                    result = {"ok": False, "type": type(error).__name__, "error": str(error)}
                else:
                    result = self.tools.execute(call["call_id"], call["name"], arguments)
                output = json.dumps(result, ensure_ascii=False)
                if len(output.encode()) > MAX_RESULT_BYTES:
                    output = json.dumps({"ok": result["ok"], "needs_narrower_read": True,
                        "result_pointer": "/operations/" + call["call_id"].replace("~", "~0").replace("/", "~1") + "/result"})
                self.data["messages"].append({"type": "function_call_output", "call_id": call["call_id"], "output": output})
                self.data["events"].append({"type": "tool_result", "call_id": call["call_id"], "name": call["name"],
                                           "ok": result["ok"], "summary": output[:2000], "timestamp": timestamp()})
                self.data["pending_calls"].pop(0)
                self.save()
            if self.tools.data["finished"]:
                self.data["status"] = "completed"
                self.controller.milestone("research-end:" + self.identity, "research_completed", session=self.identity,
                    lineage=self.data["lineage"], releases=len(self.tools.data["releases"]), summary=self.tools.data.get("summary", ""))
                self.save()
                return self.result()
            if (self.data["turns"] >= self.controller.config["max_research_turns"]
                    or len(self.tools.data["operations"]) >= self.controller.config["max_tool_calls"]
                    or self.data["spent_seconds"] >= self.controller.config["research_seconds"]):
                self.data["status"] = "needs_more_research"
                self.controller.milestone("research-end:" + self.identity, "research_budget", session=self.identity,
                    lineage=self.data["lineage"], releases=len(self.tools.data["releases"]), turns=self.data["turns"])
                self.data["events"].append({"type": "episode_budget", "summary": "Research episode budget reached; saved work is retained for the next generation."})
                self.save()
                return self.result()
            if self.tools.data.get("handoff"):
                summary = self.tools.data.pop("handoff")
                self.tools.save()
                self.compact(summary)
            if len(json.dumps(self.data["messages"], ensure_ascii=False).encode()) > self.controller.config["max_context_bytes"] * .7:
                self.compact("Automatic context checkpoint. Consult the saved notes, tests, trial and full tool-result ledger before continuing.")
            response_path = self.path.parent / f"response-{self.data['turns'] + 1:04d}.json"
            response = self.controller.read_artifact(response_path)
            if response is None:
                response = self.client.request(self.data["messages"], TOOL_DEFINITIONS,
                                               label=f"{self.identity}:turn:{self.data['turns'] + 1}")
                self.controller.artifact(response_path, response)
            self.data["turns"] += 1
            self.data["responses"].append(str(response_path.relative_to(self.controller.directory)))
            calls = []
            for item in response["output"]:
                if item.get("type") == "function_call":
                    if not all(isinstance(item.get(key), str) and item[key] for key in ("name", "arguments", "call_id")):
                        raise ValueError("The configured model returned an invalid function call.")
                    message = {key: item[key] for key in ("type", "name", "arguments", "call_id")}
                    self.data["messages"].append(message)
                    calls.append(message)
                elif item.get("type") == "message":
                    self.data["messages"].append({key: item[key] for key in ("type", "role", "content")})
                elif item.get("type") != "reasoning":
                    raise ValueError("The configured research model returned an unsupported output item.")
            text = response_text(response)
            if text:
                self.data["events"].append({"type": "research_message", "text": text, "timestamp": timestamp()})
            self.data["pending_calls"] = calls
            self.save()
            if not calls:
                if not text:
                    raise ValueError("The configured research model returned neither tools nor a readable outcome.")
                self.tools.execute(f"final-{self.data['turns']}", "finish_research", {"summary": text})

    def result(self):
        return {"source": "research", "session": self.identity, "parent_hash": self.data["parent"],
                "status": self.data["status"], "summary": self.tools.data.get("summary", ""),
                "releases": self.tools.data["releases"]}
