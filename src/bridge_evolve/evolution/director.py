"""Event-driven research direction over isolated implementation jobs and trusted evidence."""

import copy
import json
from datetime import datetime, timezone

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evolution.feedback import bounded, json_bytes
from bridge_evolve.evolution.models import MaintenanceBudgetExhausted, ModelClient, ModelRequestError
from bridge_evolve.evolution.costs import move_request_to_maintenance
from bridge_evolve.evolution.execution import EvolutionStopped
from bridge_evolve.evolution.presentation import frozen_system as checked_frozen_system, system_description
from bridge_evolve.evolution.research_tools import STRING, tool, validate_arguments
from bridge_evolve.systems import validate_system
from bridge_evolve.systems.language import SystemError

REPORT_TEXT = {"type": "string", "minLength": 1, "maxLength": 16000, "pattern": r"[\s\S]*\S[\s\S]*"}
IDEA = {"type": "object", "additionalProperties": False, "required": [
    "title", "hypothesis", "mechanism", "implementation", "falsification", "focus_keys"],
    "properties": {**{key: REPORT_TEXT for key in ("hypothesis", "mechanism", "implementation", "falsification")},
                   "title": {**REPORT_TEXT, "maxLength": 320},
                   "focus_keys": {"type": "array", "maxItems": 100, "items": STRING}, "continue_from": STRING,
                   "base_candidate": STRING,
                   "merge_candidates": {"type": "array", "maxItems": 16, "items": STRING}}}
PLAN = tool("submit_research_plan", "Submit independent, falsifiable protocol hypotheses; do not write IR.",
            {"summary": REPORT_TEXT, "ideas": {"type": "array", "minItems": 1, "maxItems": 16, "items": IDEA}},
            ("summary", "ideas"))
REVIEW = tool("review_implementation", "Decide the next research action using the trusted report, not implementation details.",
              {"decision": {"type": "string", "enum": ["continue", "stop"]}, "learning": REPORT_TEXT,
               "next_idea": IDEA}, ("decision", "learning"))
PROMPT = """You lead bridge protocol research, not DSL engineering. Propose diverse, coherent,
falsifiable information-encoding and partnership-protocol hypotheses. You may redesign whole
protocols; novelty, complexity and familiarity are not rewards. Define mechanism, interfaces and
counterexamples, not JSON patches. An independent implementer handles detailed IR, cases and tests.
Frame each mechanism as a reachable PUBLIC AUCTION STAGE, its hand partitions, and the set of calls:
sender conditions, public promises, partner continuations, interference and stopping obligations.
Sketch that stage-level interface in mechanism/implementation, not a lone attractive bid.
Delegate detailed coverage, overlaps, priority shadowing and impossible obligations to the implementer;
judge their compact reports instead of rereading every rule.
The host evaluator alone decides validity, scores and parent upgrades. Do not select favorable
evaluation deals, modify opponents or treat DD best contracts as policy labels.
Read compact actual protocol/evidence summaries and implementer reports. After each result,
challenge what it means, record learning and stop or redirect the next hypothesis. Do not ask for
raw logs or poll waiting tasks. Failed/inexpressible ideas are useful recorded outcomes.
Use continue_from only to explicitly resume an unfinished session on the same frozen parent;
otherwise jobs start independently. Use concise Chinese. Summaries are evidence/decisions,
never private internal reasoning."""
EVIDENCE = tool("inspect_research_evidence", "Read protocol semantics, portfolio, or actual worker progress. For a saved draft, use view='protocol' and session only; omit candidate. Copy protocol_queries from the context or portfolio instead of guessing. candidate alone selects a registered frozen version. Reading never publishes, validates or scores.",
    {"view": {"type": "string", "enum": ["protocol", "portfolio", "progress"]},
     "candidate": {**STRING, "pattern": r"^[0-9a-f]{64}$",
                   "description": "Optional 64-character lowercase SHA-256; never a placeholder. Without session: registered frozen version. With session: exact current document_hash or system_hash; normally omit."},
     "session": {**STRING, "minLength": 1,
                 "description": "Known session or retained-branch ID; sufficient to read its current saved draft without candidate."},
     "query": STRING}, ("view",))
NOMINATE = tool("nominate_challenge", "Queue an already validated/evaluated candidate for a fresh common-deal challenge against the CURRENT champion. You nominate, the host alone evaluates and decides. No scores are reused.",
    {"candidate": STRING, "lineage": STRING}, ("candidate", "lineage"))


class StructuredRequestLimit(RuntimeError):
    pass


def structured_request(controller, path, prompt, context, definition, label, *, validator=None, tools=(), feedback=None,
                       max_steps=None, max_seconds=None, allow_evidence=True, solution_evidence=None):
    from bridge_evolve.evolution.delivery import DeliveryWindow, SolutionReturned, enabled
    arguments = dict(validator=validator, tools=tools, feedback=feedback, max_steps=max_steps,
                     max_seconds=max_seconds, allow_evidence=allow_evidence)
    if not (enabled(controller.config) and label.startswith("repair:")
            and (":implement:" in label or ":engine:" in label)):
        return _structured_request(controller, path, prompt, context, definition, label, **arguments)
    conversation = controller.read_artifact(path.with_suffix(".conversation.json")) or {}
    window = DeliveryWindow(controller, path.with_suffix(".delivery.json"), label,
                            start_turn=conversation.get("step", 0), repair=label.split(":")[1])
    with window.active():
        try:
            value = _structured_request(controller, path, prompt, context, definition, label,
                                        **arguments, delivery=window, solution_evidence=solution_evidence)
            evidence = solution_evidence() if solution_evidence else {"submitted_result": value}
            window.complete(value, evidence, 0, source="validated_submission" if validator else "structured_submission")
            return value
        except SolutionReturned:
            raise
        except (RuntimeError, OSError) as error:
            window.fail(error, 0)
            raise


def _structured_request(controller, path, prompt, context, definition, label, *, validator=None, tools=(), feedback=None,
                        max_steps=None, max_seconds=None, allow_evidence=True, delivery=None, solution_evidence=None):
    from bridge_evolve.evolution.context import latest_interaction, request_manifest
    from bridge_evolve.evolution.delivery import DeliveryDeadline, RETURN_CHOICE, RETURN_SOLUTION, SolutionReturned
    from bridge_evolve.evolution.costs import budget_usage
    from bridge_evolve.evolution.models import request_body, token_reservation
    campaign = controller.config.get("research_budget_mode") == "campaign" or bool(tools) and label.startswith("repair:")
    resolved = path.with_suffix(".resolved.json")
    saved = controller.read_artifact(resolved) if campaign else None
    if saved is not None:
        validate_arguments(saved, definition["parameters"])
        if validator:
            validator(saved)
        return saved
    conversation_path = path.with_suffix(".conversation.json")
    conversation = controller.read_artifact(conversation_path) if campaign else None
    limit = controller.config["max_context_bytes"] // 2 if campaign else min(18000, controller.config["max_context_bytes"] // 3)
    if conversation is None:
        conversation = {"step": 0, "recovering": False, "started_at": timestamp(), "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(feedback(context, limit=limit) if feedback else
                                                 bounded(context, limit=limit), ensure_ascii=False)}]}
        if max_seconds is not None:
            controller.artifact(conversation_path, conversation)
    elif max_seconds is not None and "started_at" not in conversation:
        conversation["started_at"] = timestamp()
        controller.artifact(conversation_path, conversation)
    evidence_tools = [EVIDENCE, NOMINATE] if allow_evidence and campaign and any(role in label for role in (":director:", ":manager:")) else []
    evidence_tools += [item[0] for item in tools]
    definitions = [definition, *evidence_tools]
    if delivery:
        definitions.append(RETURN_SOLUTION)
    while True:
        step = conversation["step"]
        response_path = path if step == 0 else path.with_name(path.stem + f"-step-{step}.json")
        response = controller.read_artifact(response_path)
        maintenance = controller.config.get("maintenance_budget") and (
            conversation["recovering"] or label.startswith("repair:"))
        request_label = label if step == 0 else label + f":step:{step}"
        request_definitions = definitions
        if delivery and conversation.get("delivery_phases", {}).get(str(step)) == "return":
            delivery.closing = True
            request_definitions = [RETURN_SOLUTION]
        if response is None:
            if max_steps is not None and step >= max_steps:
                raise StructuredRequestLimit(f"Management review reached its {max_steps}-request limit without a valid decision.")
            if max_seconds is not None and (datetime.now(timezone.utc) - datetime.fromisoformat(
                    conversation["started_at"])).total_seconds() >= max_seconds:
                raise StructuredRequestLimit(f"Management review reached its {max_seconds}-second deadline without a valid decision.")
            request_messages, choice = conversation["messages"], None
            if delivery:
                delivery.closing = delivery.should_return(step)
                delivery.reason = delivery.reason or (("turn_closeout" if
                    delivery.data["requests"] >= delivery.data["turns"] - 1 else "time_closeout") if delivery.closing else None)
                return_messages = delivery.return_messages({
                    "assignment": context,
                    "actual_saved_work": solution_evidence() if solution_evidence else context},
                    tail=latest_interaction(conversation["messages"]))
                pool = "maintenance" if maintenance else "research"
                reserve = delivery.hold_return_request(return_messages, pool)
                usage = budget_usage(controller.state, pool)
                budget = controller.config["maintenance_budget"] if maintenance else controller.config
                request_limit = budget["max_requests"] if maintenance else budget["max_agent_requests"]
                token_limit = budget["max_tokens"] if maintenance else budget["max_agent_tokens"]
                if (not delivery.closing and (
                        usage["requests"] + 2 > request_limit
                        or usage["charged_tokens"] + reserve["tokens"] + token_reservation(
                            request_body(controller.config, request_messages, definitions),
                            controller.config["max_output_tokens"]) > token_limit)):
                    delivery.closing, delivery.reason = True, "resource_closeout"
                if delivery.closing:
                    request_messages, request_definitions, choice = return_messages, [RETURN_SOLUTION], RETURN_CHOICE
                request_messages = [*request_messages, delivery.reminder(step)]
                conversation.setdefault("delivery_phases", {})[str(step)] = "return" if delivery.closing else "work"
                controller.artifact(conversation_path, conversation)
            client = ModelClient(controller.config, controller.state, controller.checkpoint,
                                 controller.maintenance_stop if maintenance else controller.stop,
                                 request_timeout=controller.maintenance_time_window if maintenance else controller.model_time_window,
                                 budget="maintenance" if maintenance else "research",
                                 **({"reserve": lambda: delivery.reserve if not delivery.closing else {}} if delivery else {}))
            input_path = path.parent / (path.stem + ".inputs") / f"{step:04d}.json"
            input_path.parent.mkdir(parents=True, exist_ok=True)
            controller.artifact(input_path, {
                "messages": request_messages, "tool_names": [item["name"] for item in request_definitions],
                "tool_choice": choice,
                "context": request_manifest(controller.config, request_messages, request_definitions, tool_choice=choice)})
            try:
                response = client.request(request_messages, request_definitions, label=request_label,
                    output_tokens=controller.config["max_output_tokens"], **({"tool_choice": choice} if choice else {}))
            except (DeliveryDeadline, ModelRequestError) as error:
                if delivery and not delivery.closing and delivery.seconds_left() > 0 and delivery.should_return(step):
                    delivery.closing, delivery.reason = True, "time_closeout"
                    continue
                raise
            controller.artifact(response_path, response)
        try:
            if response.get("status") == "incomplete" or any(item.get("status") == "incomplete" for item in response["output"]):
                details = response.get("incomplete_details")
                reason = details.get("reason", "unspecified") if isinstance(details, dict) else "unspecified"
                raise ValueError(f"{label} returned an incomplete structured result ({reason}); the original response is preserved.")
            calls = [item for item in response["output"] if item.get("type") == "function_call"]
            if (len(calls) != 1 or calls[0].get("name") not in {item["name"] for item in request_definitions}
                    or not isinstance(calls[0].get("arguments"), str)
                    or not isinstance(calls[0].get("call_id"), str) or not calls[0]["call_id"]):
                raise ValueError(f"{label} must return exactly one authorized structured result or evidence query.")
            call = calls[0]
            try:
                arguments = json.loads(call["arguments"])
            except ValueError as error:
                raise ValueError(f"{label} returned invalid structured JSON.") from error
            selected = next(item for item in request_definitions if item["name"] == call["name"])
            validate_arguments(arguments, selected["parameters"])
            if len(json_bytes(arguments)) > (limit if campaign else 16000):
                raise ValueError("A research/repair report exceeds its configured input/output transport allowance.")
            if delivery and selected["name"] == RETURN_SOLUTION["name"]:
                evidence = solution_evidence() if solution_evidence else {"assignment": context}
                value = delivery.complete(arguments, evidence, step + 1)
                raise SolutionReturned(value)
            if selected["name"] == definition["name"]:
                if validator:
                    validator(arguments)
                if campaign:
                    controller.artifact(resolved, arguments)
                return arguments
            handler = next((handler for tool, handler in tools if tool["name"] == selected["name"]), None)
            if handler is not None:
                result = handler(arguments, call["call_id"])
            elif selected["name"] == EVIDENCE["name"]:
                result = director_evidence(controller, arguments)
            else:
                result = controller.queue_challenge(arguments["candidate"], arguments["lineage"])
        except DeliveryDeadline:
            if not delivery or delivery.closing or delivery.seconds_left() <= 0:
                raise
            delivery.closing, delivery.reason = True, "tool_closeout"
            conversation["messages"].extend([
                {key: call[key] for key in ("type", "name", "arguments", "call_id")},
                {"type": "function_call_output", "call_id": call["call_id"], "output": json.dumps({
                    "ok": False, "deferred_for_solution_return": True,
                    "error": "The tool reached the solution-return boundary; its saved work remains available."})}])
        except ValueError as error:
            if not campaign or not controller.config.get("maintenance_budget"):
                raise
            row = next((row for row in reversed(controller.state.get("model_requests", []))
                        if row["label"] == request_label), None)
            if row is not None:
                move_request_to_maintenance(controller, row["request"], str(error))
            event_id = request_label + ":format"
            controller.state.setdefault("maintenance_events", {})[event_id] = {
                "id": event_id, "kind": "structured_interface", "error": str(error),
                "response": response_path.relative_to(controller.directory).as_posix()}
            conversation["messages"].append({"role": "user", "content":
                f"Structured interface error: {error}. The original response and fee are retained. "
                "Return a complete result matching the declared tool schema; no research score was changed. "
                "This correction uses the separate maintenance budget."})
            conversation["recovering"] = True
        else:
            conversation["messages"].extend([
                {key: call[key] for key in ("type", "name", "arguments", "call_id")},
                {"type": "function_call_output", "call_id": call["call_id"],
                 "output": json.dumps(feedback(result, limit=limit // 2) if feedback else
                                      bounded(result, limit=limit // 2), ensure_ascii=False)}])
            conversation["recovering"] = False
        conversation["step"] += 1
        if len(json_bytes(conversation["messages"])) > controller.config["max_context_bytes"] * .8:
            conversation["messages"] = [conversation["messages"][0], conversation["messages"][1],
                                        *conversation["messages"][-4:]]
            conversation["messages"].append({"role": "user", "content":
                "Earlier queried evidence remains in the immutable conversation ledger. Query only missing specifics; submit your decision when ready."})
        controller.artifact(conversation_path, conversation)


def evidence_workspace(controller, identity):
    from bridge_evolve.evolution.inspection import checked_artifact
    metadata = (controller.state.get("research_sessions", {}).get(identity)
                or controller.state.get("research_branches", {}).get(identity))
    if metadata is None:
        raise ValueError("Choose a known research session or retained branch.")
    try:
        workspace = checked_artifact(controller.directory, metadata["workspace"])
    except (OSError, ValueError) as error:
        raise RuntimeError("Trusted research workspace evidence is missing or has failed identity checks.") from error
    if not isinstance(workspace, dict):
        raise RuntimeError("Trusted research workspace evidence is not a workspace object.")
    return workspace


def draft_protocol_queries(controller):
    return {identity: {"view": "protocol", "session": identity}
            for identity in controller.state.get("research_sessions", {}) | controller.state.get("research_branches", {})}


def director_evidence(controller, arguments):
    view = arguments["view"]
    if view == "portfolio":
        return {"champions": controller.state["champions"], "candidates": controller.state["candidate_archive"],
                "continuable_branches": controller.state.get("research_branches", {}),
                "protocol_queries": draft_protocol_queries(controller),
                "challenge_queue": controller.state.get("challenge_queue", {}),
                "instruction": "You may base an idea on a retained candidate, continue a draft, or merge compatible candidates. The host alone validates and scores."}
    if view == "progress":
        workspace = evidence_workspace(controller, arguments.get("session"))
        return {key: workspace.get(key) for key in
                ("implementation_state", "summary", "last_tests", "last_trial", "last_contract", "notes", "releases")}
    if "session" in arguments:
        workspace = evidence_workspace(controller, arguments["session"])
        document_hash = canonical_hash(workspace["system"])
        try:
            system = validate_system(workspace["system"])
        except SystemError as error:
            if "candidate" in arguments and arguments["candidate"] != document_hash:
                raise ValueError(f"The saved session draft is invalid and has document_hash={document_hash}, "
                                 "not the requested version; omit candidate or use this exact document hash.") from error
            return {"status": "invalid_draft",
                    "evidence_source": {"kind": "draft", "session": arguments["session"],
                                        "document_hash": document_hash, "hash": None, "system_hash": None},
                    "compile_error": {"error": str(error), "pointer": error.pointer},
                    "implementation_state": copy.deepcopy(workspace.get("implementation_state")),
                    "progress_query": {"view": "progress", "session": arguments["session"]},
                    "evidence_scope": "The saved editable draft cannot compile. This is a draft defect, not a query "
                                      "interface failure, validated bridge counterexample or publication. The implementer "
                                      "may read/edit raw IR to fix it; repeating this query will not change the result."}
        identity = system.content_hash
        if "candidate" in arguments and arguments["candidate"] not in (document_hash, identity):
            raise ValueError(f"The saved session draft has document_hash={document_hash} and system_hash={identity}, "
                             f"not {arguments['candidate']!r}. No other version was substituted; retry with "
                             f"view='protocol', session={arguments['session']!r} and no candidate, or use one of "
                             "these current hashes for an exact-version check.")
        source = {"kind": "draft", "session": arguments["session"], "hash": identity,
                  "document_hash": document_hash, "system_hash": identity}
        scope = ("Saved unpublished draft semantics, not validation, DDS, fitness or publication evidence. "
                 "document_hash identifies the saved IR; system_hash identifies its normalized executable system. "
                 "Recorded progress may refer to earlier draft hashes.")
    else:
        identity = arguments.get("candidate") or next(iter(controller.state["champions"].values()))
        if f"systems/{identity}.json" not in controller.state["system_files"]:
            raise ValueError("Choose a registered frozen candidate hash, or use session to inspect its saved draft. "
                             "Unknown hashes are not filesystem paths.")
        try:
            system = checked_frozen_system(controller.directory, controller.state, identity)
        except (OSError, ValueError) as error:
            raise RuntimeError("Trusted frozen system evidence is missing or has failed identity checks.") from error
        source = {"kind": "frozen", "hash": identity}
        scope = "Registered frozen version semantics, not validation or strength evidence."
    description = copy.deepcopy(system_description(system))
    description.update(evidence_source=source, evidence_scope=scope)
    query = arguments.get("query", "").casefold()
    if query:
        description["branches"] = [row for row in description["branches"]
                                    if query in json.dumps(row, ensure_ascii=False).casefold()]
    return description


def validate_idea(idea, *, campaign=False):
    validate_arguments(idea, IDEA)
    for field in ("title", "hypothesis", "mechanism", "implementation", "falsification"):
        if not idea[field].strip() or len(idea[field]) > (320 if field == "title" else 16000 if campaign else 2400):
            raise ValueError(f"Research idea {field} is empty or too long.")
    if len(set(idea["focus_keys"])) != len(idea["focus_keys"]):
        raise ValueError("Research focus keys must be distinct.")
    return copy.deepcopy(idea)


def validate_assignment(controller, idea, lineage):
    validate_idea(idea, campaign=True)
    if idea.get("base_candidate") and f"systems/{idea['base_candidate']}.json" not in controller.state["system_files"]:
        raise ValueError("base_candidate must name a registered frozen version, not a guessed hash.")
    if idea.get("continue_from"):
        source = (controller.state.get("research_sessions", {}).get(idea["continue_from"])
                  or controller.state.get("research_branches", {}).get(idea["continue_from"]))
        if source is None or source["lineage"] != lineage:
            raise ValueError("continue_from must name an existing unfinished job or imported branch in this lineage.")
        if idea["continue_from"] in controller.state.get("research_sessions", {}) and source["releases"]:
            raise ValueError("Continue a published version with base_candidate, not by resuming its completed worker.")
    if any(f"systems/{identity}.json" not in controller.state["system_files"] for identity in idea.get("merge_candidates", [])):
        raise ValueError("merge_candidates must name registered frozen versions.")


class DirectedResearch:
    def __init__(self, controller, generation, lineage, parent, packet):
        self.controller, self.generation, self.lineage, self.parent, self.packet = controller, generation, lineage, parent, packet
        self.identity = f"g{generation:03d}-{lineage}"
        self.path = controller.directory / "director" / self.identity
        self.path.mkdir(parents=True, exist_ok=True)
        units = controller.state.setdefault("research_units", {})
        self.unit = units.setdefault(self.identity, {
            "id": self.identity, "generation": generation, "lineage": lineage, "parent": parent,
            "status": "planning", "jobs": [], "created_at": timestamp(), "worker_turns": 0,
        })
        if self.unit["parent"] != parent:
            raise ValueError("A directed unit cannot resume against a different frozen parent.")

    def context(self):
        controller = self.controller
        description = system_description(controller.system(self.parent))
        return {"task": controller.config["research_task"], "unit": self.identity, "parent": self.parent,
                "protocol": {key: description[key] for key in ("name", "rules", "states", "opening", "branches")},
                "constraints": controller.research_contract.data if controller.protocol_goal else
                    {key: value for key, value in controller.research_contract.data.items() if key != "cases"}
                    if controller.module_contract else None,
                "diagnosis": [{"rule": row["rule_id"], "occurrences": row["occurrences"],
                               "mean_imp": row["imp_sum"] / row["occurrences"]}
                              for row in self.packet["selected_groups"][:6]],
                "previous_results": [unit.get("summary") for unit in controller.state["research_units"].values()
                                     if unit["id"] != self.identity and unit.get("summary")][-4:],
                "max_ideas": controller.config["hypotheses_per_unit"] if controller.config.get("research_budget_mode") == "campaign"
                             else min(controller.config["hypotheses_per_unit"], controller.config["max_releases"]),
                "unit_worker_turn_budget": controller.config["max_research_turns"],
                "selection_policy": controller.config.get("selection_policy", "confirmed"),
                **({"retained_branches": controller.state.get("research_branches", {}),
                    "protocol_queries": draft_protocol_queries(controller),
                    "candidate_portfolio": controller.state["candidate_archive"],
                    "budget_mode": "Campaign resources, not equal per-idea shares. Extend promising work; stop unproductive work explicitly.",
                    "research_instruction": "Explore structural mechanisms, including ambitious redesigns, not just patches. "
                        "Use on-demand evidence tools. base_candidate may name a retained nonchampion; continue_from may name "
                        "an imported unfinished branch. merge_candidates names compatible versions for a tested three-way merge. "
                        "Require targeted hand/auction effect trials with adequate changed-family coverage; generating more deals "
                        "and DDS batches is programmatic and does not require model tokens per deal. Uniform challenges remain separate."}
                   if controller.config.get("research_budget_mode") == "campaign" else {})}

    def report(self, job):
        controller = self.controller
        session = controller.state["research_sessions"][job["session"]]
        workspace = controller.read_artifact(controller.directory / session["workspace"])
        result = {
            "idea": job["idea"], "worker_status": session["status"],
            "feasibility": workspace.get("implementation_state"), "summary": workspace.get("summary"),
            "actual_tests": workspace.get("last_tests"), "actual_contract": workspace.get("last_contract"),
            "actual_trial": workspace.get("last_trial"),
            "releases": [], "evidence_session": job["session"],
        }
        generation = controller.state["generations"][str(self.generation)]
        for release in workspace["releases"]:
            queue = next(row for row in controller.evaluations.jobs.values()
                         if row["release"] == release["path"] or row["candidate"] == release["hash"] and row["parent"] == self.parent)
            entry_id = f"{self.lineage}-directed-{len(generation['candidates'])}"
            entry = next((entry for entry in generation["candidates"].values()
                          if entry.get("release") == release["path"]), None)
            if entry is None:
                entry = {"lineage": self.lineage, "hash": release["hash"], "parent": self.parent,
                         "release": release["path"], "research_session": job["session"], "evaluation": queue["id"],
                         "candidate_id": entry_id, "tests": release["tests"],
                         "proposal_summary": {"hypothesis": release["hypothesis"], "targets": []}}
                generation["candidates"][entry_id] = entry
            entry.update(queue.get("outcome", {"status": queue["status"]}))
            controller.state["candidate_archive"].setdefault(release["hash"], {}).update(
                hash=release["hash"], parent=self.parent, lineage=self.lineage, release=release["path"],
                development=copy.deepcopy(queue.get("outcome")), hypothesis=release["hypothesis"])
            if entry["status"] == "development_complete" or (
                    controller.config.get("research_budget_mode") == "campaign"
                    and entry["status"] in ("no_full_gain", "insufficient_coverage")):
                controller.confirm_candidate(self.generation, generation, entry)
            upgrade = next((row for row in generation["promotions"].values() if row.get("candidate") == release["hash"]), None)
            result["releases"].append({"hash": release["hash"], "development": queue.get("outcome"),
                                       "decision": entry["status"], "upgrade": upgrade})
        return result

    def run(self):
        from bridge_evolve.evolution.research import ResearchSession
        controller, unit = self.controller, self.unit
        campaign = controller.config.get("research_budget_mode") == "campaign"
        from bridge_evolve.evolution.manager import CampaignManager
        manager = CampaignManager(controller) if controller.config.get("campaign_manager") else None
        if controller.evaluator is None:
            raise RuntimeError("Directed research requires its trusted evaluator before making model requests.")
        if "plan" not in unit:
            controller.state["activity"] = {"kind": "director", "unit": self.identity, "stage": "planning", "started_at": timestamp()}
            controller.checkpoint()
            maximum = controller.config["hypotheses_per_unit"] if campaign else min(
                controller.config["hypotheses_per_unit"], controller.config["max_releases"])
            definition = copy.deepcopy(PLAN)
            definition["parameters"]["properties"]["ideas"]["maxItems"] = maximum

            def validate_plan(plan):
                for idea in plan["ideas"]:
                    validate_assignment(controller, idea, self.lineage)
                if len({canonical_hash(idea) for idea in plan["ideas"]}) != len(plan["ideas"]):
                    raise ValueError("A director plan repeats the same hypothesis.")

            assigned = manager.claim(self.lineage, self.identity) if manager else []
            if manager and not assigned:
                manager.require_progress(self.lineage)
            try:
                plan = ({"summary": "全局管理者派发的优先补缺任务；复用原草稿，不重置研究资源。",
                         "ideas": [task["idea"] for task in assigned]} if assigned else
                        structured_request(controller, self.path / "plan.json", PROMPT, self.context(), definition,
                                           self.identity + ":director:plan",
                                           validator=validate_plan if campaign else None))
            except (MaintenanceBudgetExhausted, ModelRequestError) as error:
                if not campaign:
                    raise
                unit.update(status="blocked_harness", error=str(error), summary={"error": str(error)})
                controller.checkpoint()
                return {"source": "research", "proposals": [], "research_status": "blocked_harness", "summary": str(error)}
            if not plan["summary"].strip() or len(plan["summary"]) > (16000 if campaign else 4000):
                raise ValueError("The research director must provide a bounded, explicit plan summary.")
            if len(plan["ideas"]) > maximum:
                raise ValueError("The director exceeded this unit's frozen hypothesis allocation.")
            ideas = [validate_idea(idea, campaign=campaign) for idea in plan["ideas"]]
            if len({canonical_hash(idea) for idea in ideas}) != len(ideas):
                raise ValueError("A director plan repeats the same hypothesis.")
            unit["plan"] = plan["summary"]
            unit["jobs"] = [{"id": f"{self.identity}-h{index + 1:02d}", "idea": idea, "status": "pending",
                             "parent": self.parent} for index, idea in enumerate(ideas)]
            for job, task in zip(unit["jobs"], assigned):
                job["management_task"] = task["id"]
                task["session"] = job["id"]
            controller.checkpoint()
        releases = []
        for position, job in enumerate(unit["jobs"]):
            if job["status"] == "cancelled":
                continue
            if job.get("review") or job.get("management_review"):
                releases.extend(job["report"]["releases"])
                if job.get("review", {}).get("decision") == "stop":
                    break
                continue
            if campaign:
                controller.process_repairs()
            if not job.get("report"):
                remaining = controller.config["max_research_turns"] - (0 if campaign else unit["worker_turns"])
                spent = sum(controller.state["research_sessions"][row["session"]]["spent_seconds"]
                            for row in unit["jobs"] if row.get("session"))
                seconds = controller.config["research_seconds"] - (0 if campaign else spent)
                if (remaining <= 0 or seconds <= 0) and not job.get("session"):
                    job.update(status="budget_exhausted", summary="本单元执行回合额度已用完，未启动此假设。")
                    continue
                pending = len(unit["jobs"]) - position
                job.setdefault("turn_budget", controller.config["max_research_turns"] if campaign else max(1, remaining // pending))
                job.setdefault("seconds_budget", controller.config["research_seconds"] if campaign else seconds / pending)
                job.update(status="implementing", session=job["id"])
                unit["status"] = "implementing"
                controller.checkpoint()
                def worker():
                    return ResearchSession(controller, self.generation, self.lineage, self.parent, self.packet,
                        assignment={**job["idea"], "job_id": job["id"], "turn_budget": job["turn_budget"],
                                    "seconds_budget": job["seconds_budget"]}, identity=job["id"])

                session = worker()
                try:
                    while True:
                        if manager:
                            if not manager.prepare(session, job):
                                break
                            session = worker()
                        session.run()
                        if not manager or session.data["status"] != "awaiting_management":
                            if manager:
                                manager.finish(session, job)
                            break
                        job["status"] = unit["status"] = "awaiting_management"
                        controller.checkpoint()
                        controller.process_evaluations()
                        controller.process_repairs()
                        if not manager.review(session, job):
                            break
                        job["status"] = unit["status"] = "implementing"
                        controller.checkpoint()
                except ModelRequestError as error:
                    if manager and manager.branch(session.identity).get("pending_review"):
                        raise
                    if not campaign:
                        raise
                    session.data["status"] = "blocked_harness"
                    session.tools.data.update(finished=True, summary=f"模型网关故障，草稿与费用已保留：{error}")
                    event_id = self.identity + ":" + job["id"] + ":gateway"
                    controller.state.setdefault("maintenance_events", {})[event_id] = {
                        "id": event_id, "kind": "model_gateway", "session": job["id"], "error": str(error)}
                    session.tools.save()
                    session.save()
                unit["worker_turns"] = sum(controller.state["research_sessions"][row["session"]].get("research_turns",
                                         controller.state["research_sessions"][row["session"]]["turns"])
                                           for row in unit["jobs"] if row.get("session"))
                unit["status"] = "evaluating"
                controller.checkpoint()
                job["report"] = self.report(job)
                job["status"] = "reported"
                controller.artifact(self.path / f"report-{position + 1}.json", job["report"])
            if manager:
                goal = manager.data["deliveries"][manager.branch(job["session"])["delivery_id"]]
                if goal["status"] == "blocked_budget":
                    raise EvolutionStopped("Worker handed back before spending its reserved delivery-review allowance; saved work requires more research allocation.")
                if goal["status"] == "blocked_engineering":
                    job.update(status="blocked_engineering", management_review=manager.branch(job["session"])["reviews"][-1])
                    releases.extend(job["report"]["releases"])
                    controller.checkpoint()
                    continue
            controller.state["activity"] = {"kind": "director", "unit": self.identity, "stage": "reflection", "started_at": timestamp()}
            controller.checkpoint()
            review_definition = copy.deepcopy(REVIEW)
            if position + 1 >= len(unit["jobs"]):
                review_definition["parameters"]["properties"].pop("next_idea")
            elif unit["jobs"][position + 1].get("management_task"):
                review_definition["parameters"]["properties"].pop("next_idea")

            def validate_review(review):
                if "next_idea" in review:
                    validate_assignment(controller, review["next_idea"], self.lineage)

            try:
                review = structured_request(controller, self.path / f"review-{position + 1}.json", PROMPT,
                    {"task": controller.config["research_task"], "trusted_implementation_report": job["report"],
                     "remaining_jobs": len(unit["jobs"]) - position - 1,
                     "remaining_worker_turns": controller.config["max_research_turns"] - (0 if campaign else unit["worker_turns"]),
                     "instruction": "Scores and upgrade decisions are fixed by the host. Record learning; optionally replace only the next unstarted idea, or stop."},
                    review_definition, self.identity + f":director:review:{position + 1}",
                    validator=validate_review if campaign else None)
            except (MaintenanceBudgetExhausted, ModelRequestError) as error:
                if controller.config.get("research_budget_mode") != "campaign":
                    raise
                job.update(review_error=str(error), status="reported_without_director_feedback")
                controller.state.setdefault("maintenance_events", {})[job["id"] + ":review"] = {
                    "id": job["id"] + ":review", "kind": "director_feedback", "error": str(error)}
                controller.checkpoint()
                releases.extend(job["report"]["releases"])
                continue
            if not review["learning"].strip() or len(review["learning"]) > (
                    16000 if controller.config.get("research_budget_mode") == "campaign" else 4000):
                raise ValueError("The director must report a bounded evidence-based research conclusion.")
            job["review"] = review
            if "next_idea" in review:
                if position + 1 >= len(unit["jobs"]):
                    raise ValueError("The director cannot create unbudgeted extra implementation jobs.")
                unit["jobs"][position + 1]["idea"] = validate_idea(review["next_idea"],
                    campaign=controller.config.get("research_budget_mode") == "campaign")
            releases.extend(job["report"]["releases"])
            controller.checkpoint()
            if review["decision"] == "stop":
                for later in unit["jobs"][position + 1:]:
                    later.update(status="cancelled", summary="研究负责人依据实际反馈停止此方向。")
                break
        if manager:
            manager.return_unstarted(unit["jobs"])
        unit.update(status="completed", finished_at=timestamp(),
                    summary={"plan": unit["plan"], "reports": [
                        {"title": job["idea"]["title"], "status": job["status"], "session": job.get("session"),
                         "learning": job.get("review", {}).get("learning"),
                         "results": job.get("report", {}).get("releases", [])} for job in unit["jobs"]]})
        controller.checkpoint()
        proposals = []
        for job in unit["jobs"]:
            if not job.get("session"):
                continue
            workspace = controller.read_artifact(controller.directory / controller.state["research_sessions"][job["session"]]["workspace"])
            proposals.extend({"parent_hash": self.parent, "hypothesis": release["hypothesis"],
                              "snapshot_hash": release["hash"], "release": release["path"]}
                             for release in workspace["releases"])
        return {"source": "research", "analysis": [], "research_session": self.identity,
                "research_status": unit["status"], "summary": unit["summary"], "proposals": proposals}
