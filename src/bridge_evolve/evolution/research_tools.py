"""Data-only research workspaces: full protocols, executable cases and trusted experiments."""

import copy
import hashlib
import json
import logging
import random
from pathlib import Path

from bridge_evolve.core import Seat
from bridge_evolve.evaluation.data import canonical_hash, board_data
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evaluation.record_log import agent_board, agent_decision, iter_records
from bridge_evolve.evolution.config import ROOT, blank_system
from bridge_evolve.evolution.execution import audit_module, comparison_changes
from bridge_evolve.evolution.models import response_text
from bridge_evolve.evolution.proposals import expand_condition
from bridge_evolve.systems import render_system, validate_system
from bridge_evolve.systems.inputs import decision_data, parse_board, parse_observation, parse_prefix, run_auction
from bridge_evolve.systems.navigation import at_pointer, pointer_parts, query_system
from bridge_evolve.systems.runtime import decide, replay

LOGGER = logging.getLogger(__name__)
MAX_RESULT_BYTES = 48000


def edit_document(document: dict, edits: list) -> dict:
    if not isinstance(edits, list) or not edits:
        raise ValueError("Provide nonempty JSON-pointer edits; there is no per-rule edit whitelist.")
    value = copy.deepcopy(document)
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) - {"op", "path", "value"}:
            raise ValueError("An edit accepts op, path and value.")
        operation, path = edit.get("op"), edit.get("path")
        parts = pointer_parts(path)
        if operation not in ("add", "replace", "remove", "test") or operation != "remove" and "value" not in edit:
            raise ValueError("Supported edit operations are add, replace, remove and test.")
        if operation == "test":
            if canonical_hash(at_pointer(value, path)) != canonical_hash(edit["value"]):
                raise ValueError("Edit precondition failed; read the current workspace before changing it.")
            continue
        if not parts:
            if operation == "remove" or not isinstance(edit.get("value"), dict):
                raise ValueError("The complete system must remain an object.")
            value = copy.deepcopy(edit["value"])
            continue
        container = at_pointer(value, "/" + "/".join(path.split("/")[1:-1])) if len(parts) > 1 else value
        key = parts[-1]
        if isinstance(container, list):
            if key == "-" and operation == "add":
                index = len(container)
            elif key.isdigit() and str(int(key)) == key:
                index = int(key)
            else:
                raise ValueError("Invalid array edit index.")
            maximum = len(container) if operation == "add" else len(container) - 1
            if not 0 <= index <= maximum:
                raise ValueError("Array edit index is outside the current document.")
            if operation == "add":
                container.insert(index, copy.deepcopy(edit["value"]))
            elif operation == "remove":
                container.pop(index)
            else:
                container[index] = copy.deepcopy(edit["value"])
        elif isinstance(container, dict):
            if operation != "add" and key not in container:
                raise ValueError("Edit target does not exist.")
            if operation == "remove":
                del container[key]
            else:
                container[key] = copy.deepcopy(edit["value"])
        else:
            raise ValueError("Edit target parent is not an object or array.")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 16 * 1024 * 1024:
        raise ValueError("Workspace exceeds the 16MiB resource limit; this is not a fitness penalty.")
    return value


def paged_value(value, offset=0, limit=20) -> dict:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Paged reads need offset>=0 and limit in [1,100].")
    result = {"total": len(value) if isinstance(value, (dict, list)) else None, "offset": offset}
    if isinstance(value, list):
        result["value"] = value[offset:offset + limit]
    elif isinstance(value, dict):
        keys = list(value)[offset:offset + limit]
        result["value"] = {key: value[key] for key in keys}
    else:
        result["value"] = value
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
        result.pop("value")
        if isinstance(value, dict):
            result["keys"] = list(value)[offset:offset + limit]
        elif isinstance(value, list):
            result["indices"] = list(range(offset, min(len(value), offset + limit)))
        result["needs_narrower_pointer"] = True
    return result


def tool(name, description, properties, required=()):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
OFFSET = {"type": "integer", "minimum": 0}
PAGE_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
OBJECT = {"type": "object", "additionalProperties": True}
TOOL_DEFINITIONS = [
    tool("navigate_system", "Preferred system navigation. request.view: catalog; search(query/kind/module/state/call); read(keys, dependencies=true); references(key,transitive); diff(kind/module, with compare_to); auction(dealer,vulnerability,history,call OR one-based turn); hand(observation). Stable node keys and dependency pages replace array-index hunting. Public call meanings never use private sender traces. public_ns/public_ew are optional registered system snapshots, not hands.",
         {"request": OBJECT, "snapshot": STRING, "compare_to": STRING, "public_ns": STRING, "public_ew": STRING},
         ("request",)),
    tool("read_workspace", "Read saved cases, executable tests, notes, prior tool results and releases using JSON pointers. Large values need narrower pointers.",
         {"pointer": STRING, "offset": OFFSET, "limit": PAGE_LIMIT}),
    tool("read_system", "Read any working or frozen system node by JSON pointer. Large nodes return keys/indices; narrow the pointer. No rule whitelist.",
         {"pointer": STRING, "snapshot": STRING, "offset": OFFSET, "limit": PAGE_LIMIT}),
    tool("edit_system", "Atomically edit ANY part of the working v2 system using JSON-pointer add/replace/remove/test. path='' replaces the entire system. Intermediate invalid versions are allowed; nothing changes the trusted engine.",
         {"edits": {"type": "array", "items": OBJECT}}, ("edits",)),
    tool("read_reference", "Read paged actual IR documentation or operator source. Topics: navigation, language, protocol, operators, protocol_operators, example.",
         {"topic": STRING, "offset": OFFSET, "limit": PAGE_LIMIT}, ("topic",)),
    tool("read_evidence", "Read approved diagnosis packet, completed round list, or records of a completed round. Never reads an unfinished round.",
         {"view": {"type": "string", "enum": ["packet", "rounds", "records"]}, "pointer": STRING, "round_id": STRING,
          "candidate": STRING, "offset": OFFSET, "limit": PAGE_LIMIT, "table": INTEGER, "turn": INTEGER}, ("view",)),
    tool("generate_cases", "Generate complete legal diagnostic deals with existing HCP/suit constraints. Optional hand_condition is a v2 boolean evaluated for seat as dealer in an empty auction. These are NOT uniform fitness samples.",
         {"sampling": OBJECT, "seat": STRING, "hand_condition": {}}, ("sampling",)),
    tool("add_cases", "Save explicit complete boards for regression. Each item has name and board={dealer,vulnerability,hands:{N,E,S,W}}.",
         {"cases": {"type": "array", "items": OBJECT}}, ("cases",)),
    tool("inspect_case", "Run and inspect a saved case for working/parent/frozen snapshot. Optional prefix is counterfactual, NOT evidence of a naturally reached auction. dds=true runs a strict actual two-table DDS audit and spends pair budget.",
         {"case_id": STRING, "snapshot": STRING, "side": STRING, "prefix": {"type": "array", "items": STRING},
          "table": INTEGER, "turn": INTEGER, "dds": {"type": "boolean"}}, ("case_id",)),
    tool("write_tests", "Replace executable JSON regression tests. Each test: name,case_id,mode=decision|auction,side=NS|EW,prefix:[calls],assertions:[{path,op:eq|ne|contains|ge|le,value}], optional changed_from_parent. A decision test checks the next actor on the specified side.",
         {"tests": {"type": "array", "items": OBJECT}}, ("tests",)),
    tool("run_tests", "Execute all authored regression assertions against the current system and return real failures. Does not manufacture tests from prose.", {}),
    tool("validate_system", "Compile the complete current IR and render its same-source natural-language specification.", {}),
    tool("trial", "Run an exploratory strict real-DDS common-deal comparison of working system and incumbent against both fixed opponents. Receive errors and actual behavior-change counts, then repair or redesign.",
         {"boards": INTEGER}),
    tool("consult", "Ask a separate Astra reviewer to challenge a design using selected full-system JSON pointers and actual test/trial evidence. It shares the global model budget and cannot edit the system.",
         {"question": STRING, "pointers": {"type": "array", "items": STRING}}, ("question",)),
    tool("research_note", "Persist a concise user-readable Chinese hypothesis, decision or result; do not record private internal reasoning.",
         {"text": STRING}, ("text",)),
    tool("publish_candidate", "Freeze a whole-system release and specification only after all authored tests pass. No 8-edit limit; all rules, states, meanings, priorities and fallback can change. Does not itself declare promotion.",
         {"hypothesis": STRING}, ("hypothesis",)),
    tool("finish_research", "Finish this research episode with a concise Chinese summary, including limitations or why no release was ready.",
         {"summary": STRING}, ("summary",)),
    tool("handoff", "Compact the model context using your concise research summary. Full tool results, cases, versions and responses remain on disk and readable. Mention unresolved hypotheses and next actions.",
         {"summary": STRING}, ("summary",)),
]


class ResearchTools:
    def __init__(self, controller, identity: str, parent: str, packet: dict, initial: dict, client):
        self.controller, self.identity, self.parent, self.packet, self.client = controller, identity, parent, packet, client
        self.path = controller.directory / "research" / identity / "workspace.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = controller.read_artifact(self.path) or {
            "system": copy.deepcopy(initial), "tests": [], "cases": {}, "notes": [], "operations": {},
            "releases": [], "lab_boards": 0, "finished": False, "parent": parent,
        }
        self.save()

    def save(self):
        metadata = self.controller.state.get("research_sessions", {}).get(self.identity)
        if metadata is not None:
            metadata.update(working=self.summary(), tool_calls=len(self.data["operations"]), releases=len(self.data["releases"]),
                            updated_at=timestamp())
        self.controller.artifact(self.path, self.data)

    def summary(self):
        raw = self.data["system"]
        counts = {key: len(raw[key]) if isinstance(raw.get(key), (dict, list)) else None for key in ("rules", "features")}
        states = raw.get("protocol", {}).get("states") if isinstance(raw.get("protocol"), dict) else None
        last_note = next((note for note in reversed(self.data["notes"]) if note["kind"] == "research"), None)
        tests = self.data.get("last_tests")
        return {"name": raw.get("name"), "hash": canonical_hash(raw), **counts,
                "states": len(states) if isinstance(states, dict) else None, "cases": len(self.data["cases"]),
                "authored_tests": len(self.data["tests"]), "tests": tests,
                "tests_current": tests is not None and tests["system_hash"] == canonical_hash(raw),
                "trial": self.data.get("last_trial"),
                "note": last_note["text"][:1500] if last_note else None}

    def system(self, snapshot="working"):
        if snapshot == "working":
            system = validate_system(self.data["system"])
            if system.version != 2 or system.scope != "general":
                raise ValueError("A research candidate must be a complete general v2 system.")
            return system
        identity = self.parent if snapshot == "parent" else snapshot
        if f"systems/{identity}.json" not in self.controller.state["system_files"]:
            raise ValueError("Snapshot must be working, parent, or a registered frozen system hash.")
        return self.controller.system(identity)

    def execute(self, call_id: str, name: str, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            raise ValueError("Research tool arguments must be an object.")
        fingerprint = canonical_hash({"name": name, "arguments": arguments})
        previous = self.data["operations"].get(call_id)
        if previous is not None:
            if previous["fingerprint"] != fingerprint:
                raise ValueError("A tool call ID was reused with different arguments.")
            if previous["status"] == "completed":
                return previous["result"]
        if previous is None:
            if len(self.data["operations"]) >= self.controller.config["max_tool_calls"]:
                raise ValueError("This episode's tool-call budget is exhausted.")
            previous = {"fingerprint": fingerprint, "name": name, "arguments": copy.deepcopy(arguments), "status": "started",
                        "seed": random.SystemRandom().randrange(2**53), "started_at": timestamp()}
            self.data["operations"][call_id] = previous
            self.save()
        self.controller.stop()
        self.controller.state["activity"] = {"kind": "tool", "session": self.identity, "name": name,
                                            "call_id": call_id, "started_at": previous["started_at"]}
        self.save()
        try:
            definition = next((definition for definition in TOOL_DEFINITIONS if definition["name"] == name), None)
            if definition is None:
                raise ValueError("Unknown or unauthorized research tool.")
            if set(arguments) - definition["parameters"]["properties"].keys() or (
                    set(definition["parameters"]["required"]) - arguments.keys()):
                raise ValueError("Research tool arguments have missing or unsupported fields.")
            result = getattr(self, "do_" + name)(arguments, previous, call_id)
            result = {"ok": True, **result}
        except (ValueError, KeyError, IndexError, TypeError) as error:
            LOGGER.warning("Research tool %s rejected: %s", name, error)
            result = {"ok": False, "type": type(error).__name__, "error": str(error)}
        result = copy.deepcopy(result)
        previous.update(status="completed", result=result, finished_at=timestamp())
        self.controller.state["activity"] = {"kind": "research", "session": self.identity, "started_at": timestamp(),
                                            "last_tool": name, "ok": result["ok"]}
        payload = None
        if not result["ok"]:
            payload = {"kind": "tool_issue", "tool": name, "error": result.get("error")}
        elif name == "research_note":
            payload = {"kind": "hypothesis", "summary": arguments["text"]}
        elif name == "consult":
            payload = {"kind": "review", "question": arguments["question"], "summary": result["review"]}
        elif name == "edit_system":
            payload = {"kind": "design_change", "before": result["before"], "after": result["after"],
                       "edits": len(arguments["edits"]), "rules": result["rules"]}
        elif name == "run_tests":
            payload = {"kind": "tests", "tests": {key: result[key] for key in ("tests", "passed", "failed", "change_witnesses", "system_hash")},
                       "failures": result["failures"]}
        elif name == "trial":
            payload = {"kind": "trial", **{key: result[key] for key in ("round_id", "boards", "system_hash", "status", "delta", "changes", "errors", "error_count") if key in result}}
        elif name == "publish_candidate":
            payload = {"kind": "release", "candidate": result["hash"], "summary": arguments["hypothesis"], "tests": result["tests"]}
        if payload is not None:
            self.controller.milestone("tool:" + self.identity + ":" + call_id, session=self.identity,
                                      lineage=self.identity.rsplit("-", 1)[-1], **payload)
        self.save()
        return result

    def do_read_system(self, args, *_):
        snapshot = args.get("snapshot", "working")
        raw = self.data["system"] if snapshot == "working" else self.system(snapshot).to_data()
        return {"pointer": args.get("pointer", ""), "workspace_hash": canonical_hash(self.data["system"]),
                **paged_value(at_pointer(raw, args.get("pointer", "")), args.get("offset", 0), args.get("limit", 20))}

    def do_navigate_system(self, args, *_):
        snapshot = args.get("snapshot", "working")
        system = self.system(snapshot)
        comparison = self.system(args["compare_to"]) if "compare_to" in args else None
        definitions = {side: self.system(args[key]) for side, key in (("NS", "public_ns"), ("EW", "public_ew")) if key in args}
        result = query_system(system, args["request"], compare_to=comparison, definitions=definitions)
        result["snapshot"] = snapshot
        result["working_hash"] = canonical_hash(self.data["system"])
        keys = set(args["request"].get("keys", []))
        for field in ("matches", "compatible_rules"):
            keys.update(item["key"] for item in result.get(field, {}).get("items", []))
        result["related_evidence"] = [
            {"id": group["id"], "rule_id": group["rule_id"], "occurrences": group["occurrences"],
             "pointer": f"/selected_groups/{position}"}
            for position, group in enumerate(self.packet["selected_groups"])
            if not keys or "rule:" + group["rule_id"] in keys][:4]
        return result

    def do_read_workspace(self, args, *_):
        pointer = args.get("pointer", "")
        if not pointer:
            return {"keys": list(self.data), "cases": len(self.data["cases"]), "tests": len(self.data["tests"]),
                    "notes": len(self.data["notes"]), "operations": len(self.data["operations"]),
                    "releases": len(self.data["releases"]), "working_hash": canonical_hash(self.data["system"]),
                    "instruction": "Read /cases, /tests, /notes or a specific /operations/<call_id> node."}
        value = at_pointer(self.data, pointer)
        if pointer == "/operations":
            value = {identity: {key: row[key] for key in ("name", "status")} for identity, row in value.items()}
        return {"pointer": pointer, **paged_value(value, args.get("offset", 0), args.get("limit", 20))}

    def do_edit_system(self, args, *_):
        before = canonical_hash(self.data["system"])
        self.data["system"] = edit_document(self.data["system"], args["edits"])
        rules = self.data["system"].get("rules")
        return {"before": before, "after": canonical_hash(self.data["system"]),
                "rules": len(rules) if isinstance(rules, list) else None, "validation": "not_yet_run"}

    def do_read_reference(self, args, *_):
        files = {"navigation": ROOT / "docs/13-system-navigation.md",
                 "language": ROOT / "docs/07-executable-systems.md", "protocol": ROOT / "docs/08-general-protocols-v2.md",
                 "operators": ROOT / "src/bridge_evolve/systems/language.py",
                 "protocol_operators": ROOT / "src/bridge_evolve/systems/v2_language.py", "example": ROOT / "systems/general-v2.json"}
        if args["topic"] not in files:
            raise ValueError("Unknown reference topic.")
        lines = files[args["topic"]].read_text().splitlines()
        return {"topic": args["topic"], **paged_value(lines, args.get("offset", 0), args.get("limit", 80))}

    def do_read_evidence(self, args, *_):
        if args["view"] == "packet":
            return paged_value(at_pointer(self.packet, args.get("pointer", "")),
                               args.get("offset", 0), args.get("limit", 20))
        if "pointer" in args:
            raise ValueError("Evidence pointers apply only to view=packet.")
        if args["view"] == "rounds":
            return paged_value([{key: row[key] for key in ("id", "status", "boards", "systems") if key in row}
                                for row in self.controller.state["rounds"].values() if row["status"] in ("checked", "blocked")],
                               args.get("offset", 0), args.get("limit", 20))
        identity = args["round_id"]
        saved = self.controller.state["rounds"].get(identity)
        if saved is None or saved["status"] not in ("checked", "blocked"):
            raise ValueError("Only completed approved rounds are readable.")
        path = self.controller.directory / "rounds" / identity / "summary.json"
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != saved["summary_sha256"]:
            raise ValueError("Research evidence summary identity mismatch.")
        summary = json.loads(contents)
        candidate = args.get("candidate", self.parent)
        logs = [name for key, name in summary["logs"].items() if key.startswith(candidate + "/")]
        offset, limit = args.get("offset", 0), args.get("limit", 2)
        paged_value([], offset, limit)
        selected = []
        index = 0
        for name in logs:
            log = self.controller.directory / name
            if hashlib.sha256(log.read_bytes()).hexdigest() != summary["log_sha256"][name]:
                raise ValueError("Research evidence log identity mismatch.")
            for record in iter_records(log):
                if offset <= index < offset + limit:
                    item = {"board": agent_board(record),
                            "regression_board": {key: record[key] for key in ("dealer", "vulnerability", "hands")},
                            "errors": record["audit_result"]["errors"]}
                    if "turn" in args:
                        item["decision"] = agent_decision(record, args.get("table", 1), args["turn"])
                    selected.append(item)
                index += 1
                if index >= offset + limit:
                    return {"records": selected, "offset": offset}
        return {"records": selected, "offset": offset}

    def reserve_lab(self, count: int, operation: dict):
        if "lab_reserved" in operation:
            return
        if type(count) is not int or count < 1 or self.data["lab_boards"] + count > self.controller.config["max_lab_boards"]:
            raise ValueError("This episode's diagnostic-board budget is exhausted.")
        self.data["lab_boards"] += count
        operation["lab_reserved"] = count
        self.save()

    def do_generate_cases(self, args, operation, call_id):
        module = audit_module()
        sampling = module.sampling_config({**args["sampling"], "seed": args["sampling"].get("seed", operation["seed"])})
        self.reserve_lab(sampling["count"], operation)
        predicate = None
        if "hand_condition" in args:
            raw = blank_system().to_data()
            condition = expand_condition(args["hand_condition"], self.data["system"].get("features", {}))
            raw["rules"] = [{"id": "research.hand_condition", "priority": 1, "when": condition,
                             "action": {"kind": "call", "call": "P"}}]
            predicate = validate_system(raw)
        seat = Seat(args.get("seat", "N"))
        needed = sampling["count"]
        sampling["count"] = min(1000, max(needed, 1000 if predicate is not None else needed))
        accepted = []
        for raw in module.sampled_boards(sampling, cancelled=lambda: self.controller.stop() or False):
            if predicate is not None:
                observation = parse_observation({"hand": raw["board"]["hands"][seat.value], "seat": seat.value,
                                                 "dealer": seat.value, "vulnerability": raw["board"]["vulnerability"], "history": []})
                if decide(predicate, observation).trace.rule_id != "research.hand_condition":
                    continue
            key = canonical_hash({"call": call_id, "index": len(accepted)})[:16]
            self.data["cases"][key] = {"name": f"generated-{len(accepted) + 1}", "board": raw["board"],
                                       "sampling": {**sampling, "requested_count": needed, "hand_condition": args.get("hand_condition")}}
            accepted.append({"case_id": key, "board": raw["board"]})
            if len(accepted) == needed:
                break
        if len(accepted) != needed:
            raise ValueError(f"Only {len(accepted)}/{needed} cases meet the hand predicate; no condition was relaxed.")
        return {"cases": accepted, "scope": "conditional diagnostics, not fitness"}

    def do_add_cases(self, args, operation, call_id):
        cases = args["cases"]
        if not isinstance(cases, list) or not cases:
            raise ValueError("Provide nonempty complete cases.")
        boards = [board_data(parse_board(case["board"])) for case in cases]
        self.reserve_lab(len(boards), operation)
        result = []
        for index, (case, board) in enumerate(zip(cases, boards, strict=True)):
            key = canonical_hash({"call": call_id, "index": index})[:16]
            self.data["cases"][key] = {"name": str(case.get("name", key)), "board": board}
            result.append({"case_id": key, "board": board})
        return {"cases": result}

    def play(self, case_id: str, snapshot="working", side="NS", prefix=(), *, decision=False):
        if side not in ("NS", "EW"):
            raise ValueError("side must be NS or EW.")
        raw = self.data["cases"][case_id]["board"]
        own = self.system(snapshot)
        opponent = self.controller.system(self.controller.config["baselines"][0]["hash"])
        definitions = {"NS": own if side == "NS" else opponent, "EW": own if side == "EW" else opponent}
        calls = parse_prefix(list(prefix))
        if decision:
            auction = replay(Seat(raw["dealer"]), calls)
            if auction.ended or auction.next_seat.partnership.value != side:
                raise ValueError("A decision case must stop before an actor belonging to the candidate side.")
            seat = auction.next_seat
            observation = parse_observation({"hand": raw["hands"][seat.value], "seat": seat.value,
                                             "dealer": raw["dealer"], "vulnerability": raw["vulnerability"],
                                             "history": list(prefix), "public_definitions": {
                                                 key: system.to_data() for key, system in definitions.items()}})
            return decision_data(own, observation, decide(own, observation), True)
        return run_auction(definitions["NS"], definitions["EW"], parse_board(raw), prefix=calls, trace=True, compact=True)

    def do_inspect_case(self, args, operation, _call_id):
        case_id, snapshot = args["case_id"], args.get("snapshot", "working")
        if args.get("dds"):
            if args.get("prefix"):
                raise ValueError("DDS comparison uses actual whole auctions; counterfactual prefixes are debugging only.")
            self.reserve_lab(1, operation)
            own, opponent = self.system(snapshot), self.controller.system(self.controller.config["baselines"][0]["hash"])
            self.controller.evaluator.reserve_pair()
            self.save()
            definitions = {system.content_hash: system for system in (own, opponent)}
            engine = audit_module().BoardAuditEngine(Path(self.controller.config["project"]),
                                                     {key: {"system": system} for key, system in definitions.items()})
            record, _, findings = engine.batch_board({"index": 0, "board": self.data["cases"][case_id]["board"]},
                {"system_a": own.content_hash, "system_b": opponent.content_hash}, definitions, fresh_dds=True)
            if record.get("dds") and record["dds"]["oracle"] != self.controller.state["oracle"]:
                raise RuntimeError("Research DDS provenance differs from the frozen evaluator.")
            return {"board": agent_board(record), "errors": record["audit_result"]["errors"], "findings": findings,
                    "scope": "diagnostic two-table comparison"}
        result = self.play(case_id, snapshot, args.get("side", "NS"), args.get("prefix", []))
        if "turn" in args:
            turn = args["turn"]
            if type(turn) is not int or not 1 <= turn <= len(result["auction"]):
                raise ValueError("turn is a one-based existing auction step.")
            return {"contract": result["contract"], "step": result["auction"][turn - 1]}
        return {"contract": result["contract"], "auction": [{key: step[key] for key in ("seat", "call", "state", "source") if key in step}
                                                           for step in result["auction"]],
                "counterfactual_prefix": bool(args.get("prefix"))}

    def do_write_tests(self, args, *_):
        tests = args["tests"]
        if not isinstance(tests, list) or not 1 <= len(tests) <= 256:
            raise ValueError("Provide 1-256 executable regression tests.")
        names = set()
        for case in tests:
            if not isinstance(case, dict) or set(case) - {"name", "case_id", "mode", "side", "prefix", "assertions", "changed_from_parent"}:
                raise ValueError("Unsupported executable test fields.")
            name = case.get("name")
            if not isinstance(name, str) or not name.strip() or name in names or case.get("case_id") not in self.data["cases"]:
                raise ValueError("Each test needs a unique name and an existing case_id.")
            names.add(name)
            if case.get("mode") not in ("decision", "auction") or case.get("side", "NS") not in ("NS", "EW"):
                raise ValueError("Test mode must be decision or auction, with side NS/EW.")
            parse_prefix(case.get("prefix", []))
            assertions = case.get("assertions")
            if not isinstance(assertions, list) or not assertions:
                raise ValueError("A test must assert actual behavior, not only run without crashing.")
            for assertion in assertions:
                if (not isinstance(assertion, dict) or set(assertion) != {"path", "op", "value"}
                        or assertion["op"] not in ("eq", "ne", "contains", "ge", "le")):
                    raise ValueError("An assertion needs path, op and value.")
                pointer_parts(assertion["path"])
        self.data["tests"] = copy.deepcopy(tests)
        return {"tests_written": len(tests), "status": "not_yet_run"}

    def test_results(self):
        failures, passed, changed = [], 0, 0
        for case in self.data["tests"]:
            try:
                kwargs = {"side": case.get("side", "NS"), "prefix": case.get("prefix", []), "decision": case["mode"] == "decision"}
                actual = self.play(case["case_id"], **kwargs)
                for assertion in case["assertions"]:
                    value, expected, op = at_pointer(actual, assertion["path"]), assertion["value"], assertion["op"]
                    if op in ("ge", "le"):
                        if type(value) not in (int, float) or type(expected) not in (int, float):
                            raise ValueError("Ordered assertions require numbers.")
                        ok = value >= expected if op == "ge" else value <= expected
                    elif op == "contains":
                        ok = expected in value
                    else:
                        equal = canonical_hash(value) == canonical_hash(expected)
                        ok = equal if op == "eq" else not equal
                    if not ok:
                        raise ValueError(f"Assertion {assertion['path']} {op} failed: actual={value!r}, expected={expected!r}")
                if case.get("changed_from_parent"):
                    parent = self.play(case["case_id"], snapshot="parent", **kwargs)
                    new_calls = actual["call"] if kwargs["decision"] else [step["call"] for step in actual["auction"]]
                    old_calls = parent["call"] if kwargs["decision"] else [step["call"] for step in parent["auction"]]
                    if new_calls == old_calls:
                        raise ValueError("Expected a real action change from the parent, but calls are identical.")
                    changed += 1
                passed += 1
            except (ValueError, KeyError, IndexError, TypeError) as error:
                failures.append({"test": case["name"], "type": type(error).__name__, "error": str(error)})
        return {"tests": len(self.data["tests"]), "passed": passed, "failed": len(failures),
                "change_witnesses": changed, "failures": failures[:10], "system_hash": self.system().content_hash,
                "scope": "authored regression tests, not exhaustive protocol proof"}

    def do_run_tests(self, _args, *_):
        result = self.test_results()
        self.data["last_tests"] = result
        return result

    def do_validate_system(self, _args, *_):
        system = self.system()
        specification = render_system(system)
        return {"system_hash": system.content_hash, "rules": len(system.rules), "specification_bytes": len(specification.encode()),
                "status": "typed_and_rendered", "scope": "not an exhaustive semantic proof"}

    def do_trial(self, args, operation, call_id):
        from bridge_evolve.evolution.controller import register_system
        count = args.get("boards", min(16, self.controller.config["boards"]["screen"]))
        self.reserve_lab(count, operation)
        system = self.system()
        identity = register_system(self.controller.directory, self.controller.state, system)
        self.controller.checkpoint()
        round_id = "r-" + self.identity + "-" + canonical_hash(call_id)[:12]
        result = self.controller.evaluator.evaluate(round_id, list(dict.fromkeys([identity, self.parent])), count)
        outcome = {"round_id": round_id, "boards": count, "system_hash": identity, "status": result["status"][identity],
                   "objective": result["objective"].get(identity), "parent_objective": result["objective"].get(self.parent),
                   "errors": result["errors"][identity][:5], "error_count": len(result["errors"][identity]),
                   "scope": "adaptive exploratory experiment, not promotion evidence"}
        if result["status"][identity] == "checked" and result["status"][self.parent] == "checked":
            outcome["changes"] = comparison_changes(self.controller.directory, result, identity, self.parent)
            outcome["delta"] = result["objective"][identity] - result["objective"][self.parent]
        self.data["last_trial"] = outcome
        return outcome

    def do_consult(self, args, *_):
        if not isinstance(args["question"], str) or not args["question"].strip():
            raise ValueError("Ask a concrete research question.")
        pointers = args.get("pointers", ["/rules", "/protocol", "/fallback"])
        views = {pointer: paged_value(at_pointer(self.data["system"], pointer)) for pointer in pointers}
        response = self.client.request([
            {"role": "system", "content": "Independently challenge this bridge protocol design. You are an advisory reviewer, not the evaluator. Check sender/receiver/continuation consistency, hand-information boundaries, unsupported assumptions and test gaps. Give concise evidence-based Chinese advice, not a private thinking transcript. Do not claim tests were run."},
            {"role": "user", "content": json.dumps({"question": args["question"], "system": views,
                "actual_tests": self.data.get("last_tests"), "actual_trial": self.data.get("last_trial")}, ensure_ascii=False)},
        ], [], label=self.identity + ":review", output_tokens=min(8192, self.controller.config["max_output_tokens"]))
        advice = response_text(response)
        if not advice:
            raise ValueError("The configured reviewer returned no advisory text.")
        self.data["notes"].append({"kind": "review", "question": args["question"], "text": advice})
        return {"review": advice, "model": self.controller.config["model"], "scope": "advice, not independent validation"}

    def do_research_note(self, args, *_):
        if not isinstance(args["text"], str) or not 1 <= len(args["text"]) <= 16000:
            raise ValueError("Research notes must contain 1-16000 characters.")
        self.data["notes"].append({"kind": "research", "text": args["text"]})
        return {"saved": True, "note": len(self.data["notes"])}

    def do_publish_candidate(self, args, *_):
        from bridge_evolve.evolution.controller import register_system
        if not isinstance(args["hypothesis"], str) or not args["hypothesis"].strip():
            raise ValueError("A release needs a concrete hypothesis.")
        if len(self.data["releases"]) >= self.controller.config["max_releases"]:
            raise ValueError("This episode's release budget is exhausted.")
        system = self.system()
        parent = self.system("parent").to_data()
        if canonical_hash({key: value for key, value in system.to_data().items() if key != "name"}) == canonical_hash(
                {key: value for key, value in parent.items() if key != "name"}):
            raise ValueError("The candidate is identical to its incumbent; publish an actual executable change.")
        tests = self.test_results()
        if not tests["tests"] or tests["failed"] or not tests["change_witnesses"]:
            raise ValueError("Release requires passing executable tests and a changed_from_parent witness: "
                             + json.dumps(tests, ensure_ascii=False))
        if any(release["hash"] == system.content_hash for release in self.data["releases"]):
            raise ValueError("This exact whole-system release was already published in the episode.")
        identity = register_system(self.controller.directory, self.controller.state, system)
        release = {"hash": identity, "parent": self.parent, "hypothesis": args["hypothesis"],
                   "tests": tests, "test_definitions": copy.deepcopy(self.data["tests"]),
                   "cases": {case["case_id"]: self.data["cases"][case["case_id"]] for case in self.data["tests"]},
                   "specification": f"systems/{identity}.md", "origin": self.identity}
        path = self.path.parent / f"release-{len(self.data['releases']) + 1}.json"
        self.controller.artifact(path, release)
        self.data["releases"].append({**{key: release[key] for key in ("hash", "parent", "hypothesis", "tests")},
                                      "path": str(path.relative_to(self.controller.directory))})
        return {"published": True, "hash": identity, "tests": tests,
                "release": str(path.relative_to(self.controller.directory)), "promotion": "not_yet_evaluated"}

    def do_finish_research(self, args, *_):
        if not isinstance(args["summary"], str) or not args["summary"].strip():
            raise ValueError("A research episode needs an explicit outcome summary.")
        self.data.update(finished=True, summary=args["summary"])
        return {"finished": True, "releases": len(self.data["releases"])}

    def do_handoff(self, args, *_):
        if not isinstance(args["summary"], str) or not 1 <= len(args["summary"]) <= 16000:
            raise ValueError("A handoff summary needs 1-16000 characters.")
        self.data["handoff"] = args["summary"]
        self.data["notes"].append({"kind": "handoff", "text": args["summary"]})
        return {"saved": True, "complete_history_retained": True}
