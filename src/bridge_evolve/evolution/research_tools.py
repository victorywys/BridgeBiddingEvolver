"""Data-only research workspaces: full protocols, executable cases and trusted experiments."""

import copy
import hashlib
import json
import logging
import random
import re
from pathlib import Path

from bridge_evolve.core import Seat
from bridge_evolve.evaluation.data import canonical_hash, board_data
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evaluation.record_log import agent_board, agent_decision, iter_records
from bridge_evolve.evolution.config import ROOT, blank_system
from bridge_evolve.evolution.execution import audit_module, comparison_changes
from bridge_evolve.evolution.feedback import AssertionFailure, bounded, json_bytes, short_text
from bridge_evolve.evolution.costs import move_request_to_maintenance, session_costs, tool_costs
from bridge_evolve.evolution.models import response_text
from bridge_evolve.evolution.proposals import expand_condition
from bridge_evolve.systems import render_system, validate_system
from bridge_evolve.systems.inputs import decision_data, parse_board, parse_observation, parse_prefix, run_auction
from bridge_evolve.systems.language import SystemError, object_fields, source_at, source_pointer
from bridge_evolve.systems.navigation import QUERY_FIELDS, at_pointer, pointer_parts, query_system
from bridge_evolve.systems.runtime import decide, replay

LOGGER = logging.getLogger(__name__)
MAX_RESULT_BYTES = 48000
MISSING = object()


def merge_documents(base, working, other, pointer=""):
    def same(left, right):
        return left is right if left is MISSING or right is MISSING else canonical_hash(left) == canonical_hash(right)

    def clone(value):
        return MISSING if value is MISSING else copy.deepcopy(value)

    if same(working, other) or same(base, other):
        return clone(working), []
    if same(base, working):
        return clone(other), []
    if all(isinstance(value, dict) for value in (base, working, other)):
        result, conflicts = {}, []
        for key in dict.fromkeys([*working, *other, *base]):
            value, issues = merge_documents(base.get(key, MISSING), working.get(key, MISSING),
                                            other.get(key, MISSING), source_pointer(pointer, key))
            if value is not MISSING:
                result[key] = value
            conflicts.extend(issues)
        return result, conflicts
    if all(isinstance(value, list) and all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in value)
           and len({row["id"] for row in value}) == len(value) for value in (base, working, other)):
        value, conflicts = merge_documents(*({row["id"]: row for row in rows} for rows in (base, working, other)), pointer)
        return list(value.values()), conflicts
    return clone(working), [pointer]


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


def paged_value(value, offset=0, limit=20, *, maximum_bytes=MAX_RESULT_BYTES) -> dict:
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
    if isinstance(value, (dict, list)):
        result["next_offset"] = offset + limit if offset + limit < len(value) else None
    if len(json.dumps(result, ensure_ascii=False).encode()) > maximum_bytes:
        result.pop("value")
        if isinstance(value, dict):
            result["keys"] = list(value)[offset:offset + limit]
        elif isinstance(value, list):
            result["indices"] = list(range(offset, min(len(value), offset + limit)))
        result["needs_narrower_pointer"] = True
    return result


def tool(name, description, properties, required=(), *, definitions=None):
    parameters = {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}
    if definitions:
        parameters["$defs"] = definitions
    return {"type": "function", "name": name, "description": description, "parameters": parameters}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
OFFSET = {"type": "integer", "minimum": 0}
PAGE_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
OBJECT = {"type": "object", "additionalProperties": True}
TURN = {"type": "integer", "minimum": 1, "description": "One-based auction step (first=1)."}
TABLE = {"type": "integer", "enum": [1, 2]}
SIDE = {"type": "string", "enum": ["NS", "EW"]}
SEAT = {"type": "string", "enum": ["N", "E", "S", "W"]}
SUIT_RANGE = {"type": "array", "minItems": 2, "maxItems": 2,
              "items": {"type": "integer", "minimum": 0, "maximum": 13}}
HAND_CONSTRAINTS = {"type": "object", "additionalProperties": False, "properties": {
    "hcp": {"type": "array", "minItems": 2, "maxItems": 2,
            "items": {"type": "integer", "minimum": 0, "maximum": 40}},
    **{suit: {"$ref": "#/$defs/suit_range"} for suit in "SHDC"},
}}
SAMPLING = {"type": "object", "additionalProperties": False, "properties": {
    "count": {"type": "integer", "minimum": 1, "maximum": 100000},
    "seed": {"type": ["integer", "null", "string"], "minimum": 0, "maximum": 2**53 - 1, "pattern": r"\s*",
             "description": "Integer seed, or omit/null/blank for a fresh seed."},
    "workers": {"type": "integer", "minimum": 1, "maximum": 4},
    "attempt_limit": {"type": "integer", "minimum": 1, "maximum": 100000000},
    "dealer": {"type": "string", "enum": ["N", "E", "S", "W", "cycle"]},
    "vulnerability": {"type": "string", "enum": ["none", "ns", "ew", "both", "cycle"]},
    "constraints": {"type": "object", "additionalProperties": False,
                    "properties": {seat: {"$ref": "#/$defs/hand"} for seat in "NESW"},
                    "description": 'Per-seat direct ranges, e.g. {"S":{"hcp":[0,7],"D":[6,7]}}; no suit_lengths.'},
}}
NAVIGATION_PROPERTIES = {
    "view": {"type": "string", "enum": ["catalog", "search", "read", "references", "diff", "auction", "hand"]},
    "query": STRING, "kind": {"type": "string", "enum": [
        "rule", "transition", "announcement", "feature", "parameter", "state", "fallback"]},
    "module": STRING, "state": STRING, "call": STRING, "key": STRING,
    "keys": {"type": "array", "minItems": 1, "maxItems": 20, "items": STRING,
             "description": "Batch 1-20 distinct keys returned by search; do not guess IDs."},
    "sections": {"type": "array", "minItems": 1, "maxItems": 10,
                 "items": {"type": "string", "pattern": r"(/.*)?"}},
    "dependencies": {"type": "boolean"}, "transitive": {"type": "boolean"},
    "offset": OFFSET, "limit": PAGE_LIMIT, "dealer": SEAT,
    "vulnerability": {"type": "string", "enum": ["none", "ns", "ew", "both"]},
    "history": {"type": "array", "items": STRING}, "turn": TURN, "observation": OBJECT,
}
NAVIGATION_REQUEST = {"type": "object", "additionalProperties": False, "properties": NAVIGATION_PROPERTIES,
    "required": ["view"], "oneOf": [
    {"properties": {"view": {"enum": [view]}},
     "propertyNames": {"enum": ["view", *sorted(fields)]},
     "required": ["view"] + (["keys"] if view == "read" else ["key"] if view == "references"
                            else ["observation"] if view == "hand" else [])}
    for view, fields in QUERY_FIELDS.items()
]}
COHORT = {"type": "object", "additionalProperties": False, "properties": {
    "name": STRING, "constraints": SAMPLING["properties"]["constraints"],
    "dealer": SAMPLING["properties"]["dealer"], "vulnerability": SAMPLING["properties"]["vulnerability"],
    "attempt_limit": SAMPLING["properties"]["attempt_limit"], "hand_conditions": OBJECT,
    "auction": {"type": "object", "additionalProperties": False, "properties": {
        "reference": STRING, "opponent": STRING, "side": SIDE,
        "prefix": {"type": "array", "items": STRING},
        "rule_ids": {"type": "array", "minItems": 1, "items": STRING}, "state": STRING}},
}}


def validate_arguments(value, schema, pointer="", *, root=None):
    """Enforce the schema subset used by the trusted tool definitions before side effects."""
    root = schema if root is None else root
    if "oneOf" in schema:
        matched = 0
        errors = []
        for variant in schema["oneOf"]:
            try:
                validate_arguments(value, variant, pointer, root=root)
            except SystemError as error:
                errors.append(str(error))
            else:
                matched += 1
        if matched != 1:
            raise SystemError("Arguments must match exactly one declared interface variant: " + "; ".join(errors),
                              pointer=pointer)
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise RuntimeError("Trusted tool schemas require local JSON references.")
        return validate_arguments(value, at_pointer(root, reference[1:]), pointer, root=root)
    expected = schema.get("type")
    allowed = expected if isinstance(expected, list) else [expected] if expected else []
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int,
               "boolean": type(value) is bool, "null": value is None}
    if allowed and not any(matches[kind] for kind in allowed):
        raise SystemError(f"Expected type {expected}.", pointer=pointer)
    if "enum" in schema and value not in schema["enum"]:
        raise SystemError(f"Expected one of {schema['enum']}.", pointer=pointer)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if schema.get("additionalProperties") is False:
            object_fields(value, required, set(properties) - required, pointer=pointer)
        elif any(not isinstance(key, str) for key in value):
            raise SystemError("Expected an object with string keys.", pointer=pointer)
        elif required - value.keys():
            raise SystemError(f"Missing required fields: {sorted(required - value.keys())}.", pointer=pointer)
        for key in value:
            if "propertyNames" in schema:
                validate_arguments(key, schema["propertyNames"], source_pointer(pointer, key), root=root)
            if key in properties:
                validate_arguments(value[key], properties[key], source_pointer(pointer, key), root=root)
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise SystemError(f"Array length must be in [{schema.get('minItems', 0)},{schema.get('maxItems', 'unbounded')}].",
                              pointer=pointer)
        if "items" in schema:
            for index, item in enumerate(value):
                validate_arguments(item, schema["items"], source_pointer(pointer, index), root=root)
    elif type(value) is int:
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise SystemError(f"Integer must be in [{schema.get('minimum', 'unbounded')},{schema.get('maximum', 'unbounded')}].",
                              pointer=pointer)
    elif isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            raise SystemError("String length is outside the declared interface bounds.", pointer=pointer)
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise SystemError(f"String must match {schema['pattern']!r}.", pointer=pointer)


TOOL_DEFINITIONS = [
    tool("read_contract", "Frozen requirements or consumed diagnostic cases; not evaluation data.",
         {"view": {"type": "string", "enum": ["interface", "cases"]}, "offset": OFFSET, "limit": PAGE_LIMIT}, ("view",)),
    tool("run_contract", "Test frozen requirements; not strength or exhaustive proof.", {}),
    tool("read_result", "Immutable result by ID/pointer; page collections or slice text. No host paths.",
         {"result_id": STRING, "pointer": STRING, "offset": OFFSET, "limit": PAGE_LIMIT,
          "text_offset": OFFSET, "text_limit": {"type": "integer", "minimum": 1, "maximum": 8000}}, ("result_id",)),
    tool("navigate_system", "Search/read stable keys, references, diff or auction/hand. Edit data, not metadata. sections uses /pointers; public_ns/ew are snapshot IDs.",
         {"request": NAVIGATION_REQUEST, "snapshot": STRING, "compare_to": STRING, "public_ns": STRING, "public_ew": STRING},
         ("request",)),
    tool("read_workspace", "Saved cases/tests/notes/proposals/results/releases by pointer; paged.",
         {"pointer": STRING, "offset": OFFSET, "limit": PAGE_LIMIT}),
    tool("read_system", "Raw working/frozen IR by pointer; large nodes need narrower pointers.",
         {"pointer": STRING, "snapshot": STRING, "offset": OFFSET, "limit": PAGE_LIMIT}),
    tool("edit_system", "Atomic JSON-pointer add/replace/remove/test; path='' replaces IR. Invalid drafts allowed.",
         {"edits": {"type": "array", "items": OBJECT}}, ("edits",)),
    tool("read_reference", "Paged reference topics: module, forcing_pass, navigation, language, protocol, operators, protocol_operators, example.",
         {"topic": STRING, "offset": OFFSET, "limit": PAGE_LIMIT}, ("topic",)),
    tool("read_evidence", "Packet or completed rounds/records, never unfinished data. Omit pointer for rounds/records.",
         {"view": {"type": "string", "enum": ["packet", "rounds", "records"]}, "pointer": STRING, "round_id": STRING,
          "candidate": STRING, "offset": OFFSET, "limit": PAGE_LIMIT, "table": TABLE, "turn": TURN}, ("view",)),
    tool("generate_cases", "Batch diagnostics; cohort targets hands/natural reference auctions. Cases are not fitness.",
         {"sampling": SAMPLING, "seat": SEAT, "hand_condition": {}, "cohort": COHORT}, ("sampling",),
         definitions={"hand": HAND_CONSTRAINTS, "suit_range": SUIT_RANGE}),
    tool("add_cases", "Save complete regression boards: cases=[{name,board:{dealer,vulnerability,hands:{N,E,S,W}}}].",
         {"cases": {"type": "array", "items": OBJECT}}, ("cases",)),
    tool("inspect_case", "One-based turn; side=our pair. Non-DDS table=1, prefix=counterfactual. DDS table=1/2 means our NS/EW, requires empty prefix; diagnostic pair cost.",
         {"case_id": STRING, "snapshot": STRING, "side": SIDE, "prefix": {"type": "array", "items": STRING},
          "table": TABLE, "turn": TURN, "dds": {"type": "boolean"}}, ("case_id",)),
    tool("write_tests", "Replace JSON tests: name,case_id,mode=decision|auction,side,prefix,assertions[{path,op:eq|ne|contains|ge|le,value}],changed_from_parent. Decision tests stop before our actor.",
         {"tests": {"type": "array", "items": OBJECT}}, ("tests",)),
    tool("run_tests", "Run authored tests on current IR; report actual failures.", {}),
    tool("validate_system", "Compile IR/render specification; not exhaustive proof.", {}),
    tool("trial", "Fresh shared DDS; declare sampling or uniform=true. Auto-expands to coverage/max_boards. auction.reference='parent'; conditional is NOT global fitness.",
         {"boards": {"type": "integer", "minimum": 1, "maximum": 100000}, "sampling": COHORT,
          "uniform": {"type": "boolean"}, "max_boards": {"type": "integer", "minimum": 1, "maximum": 100000},
          "min_changed_families": {"type": "integer", "minimum": 0, "maximum": 100000}},
         definitions={"hand": HAND_CONSTRAINTS, "suit_range": SUIT_RANGE}),
    tool("consult", "Read-only critique: IR pointers, /tests or /workspace/...; reports missing nodes.",
         {"question": STRING, "pointers": {"type": "array", "minItems": 1, "maxItems": 100, "items": STRING}}, ("question",)),
    tool("merge_candidate", "Three-way merge using base; conflicts leave draft unchanged. Imports tests/cases. Retest and evaluate afresh.",
         {"candidate": STRING, "base": STRING}, ("candidate", "base")),
    tool("research_note", "Save Chinese hypothesis/decision/result, not private reasoning.",
         {"text": STRING}, ("text",)),
    tool("publish_candidate", "Freeze IR/specification after tests and changed_from_parent witness; not promotion.",
         {"hypothesis": STRING}, ("hypothesis",)),
    tool("finish_research", "End with Chinese outcome/limitations; no release if incomplete.",
         {"summary": STRING}, ("summary",)),
    tool("handoff", "Compact using unresolved hypotheses/next actions; full work remains saved.",
         {"summary": STRING}, ("summary",)),
]
IMPLEMENTATION_TOOL = tool("update_implementation_state", "Save the worker's stage, stage_design table, verified facts, blocker and next action; no private reasoning.",
    {"stage": {"type": "string", "enum": ["feasibility", "implementing", "testing", "blocked", "ready"]},
     "verified": STRING, "blocker": STRING, "next_action": STRING,
     "stage_design": {**STRING, "description": "Human-readable public position -> hand partitions -> calls, sender conditions and public promises -> partner continuations/interference/exits; record coverage and priority gaps."}},
    ("stage", "verified", "blocker", "next_action"))


class ResearchTools:
    budget_name = "research"

    def __init__(self, controller, identity: str, parent: str, packet: dict, initial: dict, client, *, path=None):
        self.controller, self.identity, self.parent, self.packet, self.client = controller, identity, parent, packet, client
        self.path = path or controller.directory / "research" / identity / "workspace.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = controller.read_artifact(self.path) or {
            "system": copy.deepcopy(initial), "tests": [], "cases": {}, "notes": [], "operations": {},
            "releases": [], "lab_boards": 0, "finished": False, "parent": parent,
        }
        self.save()

    @property
    def definitions(self):
        definitions = TOOL_DEFINITIONS + ([IMPLEMENTATION_TOOL] if self.data.get("assignment") else [])
        from bridge_evolve.evolution.delivery import RETURN_SOLUTION, enabled
        return ([item for item in definitions if item["name"] != "finish_research"] + [RETURN_SOLUTION]
                if enabled(self.controller.config) and self.data.get("assignment") else definitions)

    def stop(self):
        self.controller.stop()

    def save(self):
        metadata = self.controller.state.get("research_sessions", {}).get(self.identity)
        working = self.summary() if metadata is not None else None
        tracked = bool(self.data.get("provisional_proposals"))
        if tracked:
            self.controller.workspace_artifact(self.path, self.data)
        if metadata is not None:
            metadata.update(working=working, tool_calls=len(self.data["operations"]), releases=len(self.data["releases"]),
                            updated_at=timestamp())
        if tracked:
            self.controller.checkpoint()
        else:
            self.controller.artifact(self.path, self.data)

    def summary(self):
        raw = self.data["system"]
        document_hash = canonical_hash(raw)
        counts = {key: len(raw[key]) if isinstance(raw.get(key), (dict, list)) else None for key in ("rules", "features")}
        states = raw.get("protocol", {}).get("states") if isinstance(raw.get("protocol"), dict) else None
        last_note = next((note for note in reversed(self.data["notes"]) if note["kind"] == "research"), None)
        tests = bounded(self.data.get("last_tests"), limit=6000,
                        reference={"session": self.identity, "pointer": "/last_tests"})
        return {"name": raw.get("name"), "hash": document_hash, "document_hash": document_hash, **counts,
                "states": len(states) if isinstance(states, dict) else None, "cases": len(self.data["cases"]),
                "authored_tests": len(self.data["tests"]), "tests": tests,
                "tests_current": tests is not None and tests.get("document_hash", tests.get("system_hash")) == document_hash,
                "trial": bounded(self.data.get("last_trial"), limit=6000,
                                 reference={"session": self.identity, "pointer": "/last_trial"}),
                "note": last_note["text"][:1500] if last_note else None,
                "provisional_proposals": [{key: value[key] for key in ("id", "status", "paths", "verification", "acceptance")}
                                         for value in list(self.data.get("provisional_proposals", {}).values())[-10:]],
                "resource_usage": tool_costs(self.data["operations"])}

    def do_read_contract(self, args, *_):
        contract = self.controller.research_contract
        if contract is None:
            raise ValueError("This experiment has no frozen protocol requirement or module contract.")
        data = contract.data
        if args["view"] == "interface":
            return {key: value for key, value in data.items() if key not in ("cases", "anchor")}
        return self.page(contract.cases(), args.get("offset", 0), args.get("limit", 4))

    def do_run_contract(self, _args, *_):
        contract = self.controller.research_contract
        if contract is None:
            raise ValueError("This experiment has no frozen protocol requirement or module contract.")
        result = contract.check(self.system(), stop=self.controller.stop)
        self.data["last_contract"] = result
        return result

    def require_contract(self, system):
        contract = self.controller.research_contract
        if contract is not None:
            result = contract.check(system, stop=self.controller.stop)
            self.data["last_contract"] = result
            if result["status"] != "passed":
                label = "Protocol goal" if self.controller.protocol_goal is not None else "Module contract"
                raise ValueError(label + " failed: " + json.dumps(self.feedback(result, limit=6000), ensure_ascii=False))

    def full_result(self, value):
        identity = canonical_hash(value)
        path = self.path.parent / "results" / f"{identity}.json"
        path.parent.mkdir(exist_ok=True)
        name = path.relative_to(self.controller.directory).as_posix()
        existing = self.controller.read_artifact(path)
        if existing is None:
            self.controller.artifact(path, value)
        elif canonical_hash(existing) != identity:
            raise ValueError("An immutable research result differs from its identity.")
        self.data.setdefault("full_results", {})[identity] = {
            "path": name, "sha256": self.controller.state["artifacts"][name], "bytes": len(json_bytes(value))}
        self.save()
        return {"result_id": identity, "session": self.identity}

    def feedback(self, value, *, limit=12000):
        if len(json_bytes(value)) <= limit:
            return copy.deepcopy(value)
        reference = self.full_result(value)
        return bounded(value, limit=limit, reference=reference)

    def page(self, value, offset=0, limit=20):
        result = paged_value(value, offset, limit, maximum_bytes=self.result_limit)
        if result.get("needs_narrower_pointer"):
            result["details_ref"] = self.full_result(value)
        return result

    @property
    def result_limit(self):
        return min(512000, self.controller.config["max_context_bytes"] // 3) if (
            self.controller.config.get("research_budget_mode") == "campaign") else MAX_RESULT_BYTES

    def do_read_result(self, args, *_):
        saved = self.data.get("full_results", {}).get(args["result_id"])
        if saved is None:
            raise ValueError("Unknown approved research result.")
        path = self.controller.directory / saved["path"]
        if self.controller.state["artifacts"].get(saved["path"]) != saved["sha256"]:
            raise ValueError("Research result identity changed.")
        full = self.controller.read_artifact(path)
        if full is None:
            raise ValueError("A frozen research result is missing.")
        value = at_pointer(full, args.get("pointer", ""))
        if isinstance(value, str):
            offset, limit = args.get("text_offset", 0), args.get("text_limit", 2000)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
                raise ValueError("Result text reads require text_offset>=0 and text_limit in [1,8000].")
            return {"value": value[offset:offset + limit], "offset": offset, "total_chars": len(value),
                    "next_offset": offset + limit if offset + limit < len(value) else None}
        return paged_value(value, args.get("offset", 0), args.get("limit", 20))

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
            if (name != "return_solution" and sum(row.get("budget", "research") == "research"
                    for row in self.data["operations"].values()) >= self.controller.config["max_tool_calls"]):
                raise ValueError("This episode's tool-call budget is exhausted.")
            previous = {"fingerprint": fingerprint, "name": name, "arguments": copy.deepcopy(arguments), "status": "started",
                        "seed": random.SystemRandom().randrange(2**53), "started_at": timestamp(), "budget": self.budget_name}
            self.data["operations"][call_id] = previous
            self.save()
        if name != "return_solution":
            self.stop()
        self.controller.state["activity"] = {"kind": "tool", "session": self.identity, "name": name,
                                            "call_id": call_id, "started_at": previous["started_at"]}
        self.save()
        interface = True
        try:
            if self.data.get("assignment") and self.data.get("finished") and name != "return_solution":
                raise ValueError("This implementation job is finished; further edits require a new director assignment.")
            definition = next((definition for definition in self.definitions if definition["name"] == name), None)
            if definition is None:
                raise ValueError("Unknown or unauthorized research tool.")
            validate_arguments(arguments, definition["parameters"])
            interface = False
            from bridge_evolve.evolution.memory import cached_read
            result = cached_read(self, arguments) if name == "read_system" else None
            previous["cache_hit"] = result is not None
            if result is None:
                result = {"ok": True, **getattr(self, "do_" + name)(arguments, previous, call_id)}
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            LOGGER.warning("Research tool %s rejected: %s", short_text(name, 128), short_text(error))
            result = {"ok": False, "type": type(error).__name__, "error": str(error)}
            from bridge_evolve.evolution.memory import READ_TOOLS
            design_error = isinstance(error, SystemError) and error.pointer is not None and not error.pointer.startswith("/request")
            if interface or isinstance(error, OSError) or (name in READ_TOOLS or name == "consult") and not design_error:
                result["failure_kind"] = "interface"
                event_id = self.identity + ":" + call_id
                self.controller.state.setdefault("maintenance_events", {})[event_id] = {
                    "id": event_id, "kind": "tool_interface", "session": self.identity, "tool": name,
                    "error": str(error), "timestamp": timestamp()}
            if isinstance(error, SystemError) and error.pointer is not None:
                result["pointer"] = error.pointer
            from bridge_evolve.evolution.sampling import ReferenceAuctionError
            if isinstance(error, ReferenceAuctionError):
                from bridge_evolve.evolution.repairs import record_reference_incident
                result["repair"] = record_reference_incident(self.controller, error, self.identity, call_id)
                if result["repair"]["owner"] == "maintenance":
                    result["failure_kind"] = "baseline"
            elif not interface and (isinstance(error, OSError) or
                    name in READ_TOOLS | {"consult"} and isinstance(error, (KeyError, IndexError, TypeError))):
                from bridge_evolve.evolution.repairs import record_tool_incident
                identity = record_tool_incident(self.controller, {
                    "schema": "bridge-tool-incident/v1", "run": self.controller.state["id"],
                    "session": self.identity, "operation": call_id, "tool": name,
                    "arguments": copy.deepcopy(arguments),
                    "blocking": isinstance(error, OSError),
                    "error": {"stage": "research_tool", "type": type(error).__name__, "message": str(error)},
                    "workspace": copy.deepcopy({key: self.data[key] for key in
                                                ("system", "tests", "implementation_state") if key in self.data})})
                result["failure_kind"] = "framework"
                self.controller.state["maintenance_events"][self.identity + ":" + call_id]["kind"] = "tool_framework"
                result["repair"] = {"owner": "maintenance", "evidence_saved": True, "jobs": [identity]}
        result = copy.deepcopy(result)
        result = self.feedback(result, limit=self.result_limit)
        from bridge_evolve.evolution.memory import remember
        remember(self, name, arguments, result)
        previous.update(status="completed", result=result, finished_at=timestamp())
        recovery = self.controller.state.get("recovery")
        if result["ok"] and name != "return_solution" and recovery and recovery["status"] != "research_resumed":
            recovery.update(status="research_resumed", resumed_at=timestamp(), session=self.identity, tool=name,
                            document_hash=canonical_hash(self.data["system"]))
            self.controller.state["recovery_history"][-1] = copy.deepcopy(recovery)
            self.controller.artifact(self.controller.directory / "recovery-receipt.json", copy.deepcopy(recovery))
            self.controller.milestone("recovery-resumed", "research_resumed", **copy.deepcopy(recovery))
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
                                          lineage=self.identity.split("-")[1] if self.identity.startswith("g") else None, **payload)
        self.save()
        return result

    def do_update_implementation_state(self, args, *_):
        if any(len(args[key]) > 2000 for key in ("verified", "blocker", "next_action")) or not args["next_action"].strip():
            raise ValueError("Implementation state needs bounded facts and an explicit next action.")
        self.data["implementation_state"] = copy.deepcopy(args)
        return {"saved": True, **args}

    def do_read_system(self, args, *_):
        snapshot = args.get("snapshot", "working")
        raw = self.data["system"] if snapshot == "working" else self.system(snapshot).to_data()
        return {"pointer": args.get("pointer", ""), "workspace_hash": canonical_hash(self.data["system"]),
                **self.page(at_pointer(raw, args.get("pointer", "")), args.get("offset", 0), args.get("limit", 20))}

    def do_navigate_system(self, args, *_):
        snapshot = args.get("snapshot", "working")
        system = self.system(snapshot)
        comparison = self.system(args["compare_to"]) if "compare_to" in args else None
        definitions = {side: self.system(args[key]) for side, key in (("NS", "public_ns"), ("EW", "public_ew")) if key in args}
        with source_at("/request"):
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
        return {"pointer": pointer, **self.page(value, args.get("offset", 0), args.get("limit", 20))}

    def do_edit_system(self, args, *_):
        before = canonical_hash(self.data["system"])
        self.data["system"] = edit_document(self.data["system"], args["edits"])
        rules = self.data["system"].get("rules")
        return {"before": before, "after": canonical_hash(self.data["system"]),
                "rules": len(rules) if isinstance(rules, list) else None, "validation": "not_yet_run"}

    def do_merge_candidate(self, args, *_):
        source = self.system(args["candidate"]).to_data()
        base = self.system(args["base"]).to_data()
        source["name"] = base["name"] = self.data["system"]["name"]
        merged, conflicts = merge_documents(base, self.data["system"], source)
        if conflicts:
            return {"merged": False, "conflicts": conflicts, "draft_unchanged": True,
                    "instruction": "Resolve these semantic conflicts explicitly with edit_system; no partial merge was applied."}
        self.data["system"] = merged
        imported = 0
        for metadata in self.controller.state.get("candidate_archive", {}).values():
            if metadata.get("hash") != args["candidate"] or not metadata.get("release"):
                continue
            release = self.controller.read_artifact(self.controller.directory / metadata["release"])
            self.data["cases"].update(copy.deepcopy(release["cases"]))
            for test in release["test_definitions"]:
                test = copy.deepcopy(test)
                test["name"] = args["candidate"][:8] + ":" + test["name"]
                test.pop("changed_from_parent", None)
                if test not in self.data["tests"]:
                    self.data["tests"].append(test)
                    imported += 1
            break
        self.data.setdefault("merged_from", []).append({"candidate": args["candidate"], "base": args["base"]})
        return {"merged": True, "hash": canonical_hash(merged), "imported_tests": imported,
                "validation": "not_yet_run", "promotion": "not_evaluated"}

    def do_read_reference(self, args, *_):
        from bridge_evolve.evolution.runtime import REFERENCES
        if args["topic"] not in REFERENCES:
            raise ValueError("Unknown reference topic.")
        lines = (ROOT / REFERENCES[args["topic"]]).read_text().splitlines()
        return {"topic": args["topic"], **self.page(lines, args.get("offset", 0), args.get("limit", 80))}

    def do_read_evidence(self, args, *_):
        if args["view"] == "packet":
            return self.page(at_pointer(self.packet, args.get("pointer", "")),
                             args.get("offset", 0), args.get("limit", 20))
        if args.get("pointer", "") != "":
            raise ValueError("Evidence pointers apply only to view=packet.")
        if args["view"] == "rounds":
            page = paged_value([{key: row[key] for key in ("id", "status", "boards", "systems") if key in row}
                               for row in self.controller.state["rounds"].values() if row["status"] in ("checked", "blocked")],
                              args.get("offset", 0), args.get("limit", 20))
            if "value" not in page:
                return page
            for row in page["value"]:
                saved = self.controller.state["rounds"][row["id"]]
                contents = (self.controller.directory / "rounds" / row["id"] / "summary.json").read_bytes()
                if hashlib.sha256(contents).hexdigest() != saved["summary_sha256"]:
                    raise ValueError("Research evidence summary identity mismatch.")
                summary = json.loads(contents)
                row.update(candidate_statuses=summary["status"], objective=summary["objective"], means=summary["means"])
            return page
        identity = args["round_id"]
        saved = self.controller.state["rounds"].get(identity)
        if saved is None or saved["status"] not in ("checked", "blocked"):
            raise ValueError("Only completed approved rounds are readable.")
        path = self.controller.directory / "rounds" / identity / "summary.json"
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != saved["summary_sha256"]:
            raise ValueError("Research evidence summary identity mismatch.")
        summary = json.loads(contents)
        candidate = self.parent if args.get("candidate") in (None, "") else args["candidate"]
        if candidate not in saved["systems"]:
            raise ValueError("The selected candidate is not part of this completed round.")
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
                    return {"records": selected, "offset": offset, "candidate": candidate}
        return {"records": selected, "offset": offset, "candidate": candidate}

    def reserve_lab(self, count: int, operation: dict):
        if "lab_reserved" in operation:
            return
        if type(count) is not int or count < 1 or self.data["lab_boards"] + count > self.controller.config["max_lab_boards"]:
            raise ValueError("This episode's diagnostic-board budget is exhausted.")
        self.data["lab_boards"] += count
        operation["lab_reserved"] = count
        self.save()

    def cohort(self, raw):
        from bridge_evolve.evolution.sampling import normalize_sampling
        raw = copy.deepcopy(raw)
        raw.setdefault("name", self.identity + " targeted mechanism")
        raw.setdefault("constraints", {})
        if "auction" in raw:
            target = raw["auction"]
            target["reference"] = self.system(target.get("reference", "parent")).content_hash
            target["opponent"] = self.system(target.get("opponent", self.controller.config["baselines"][0]["hash"])).content_hash
            if target["reference"] == self.system().content_hash and target["reference"] != self.parent:
                raise ValueError("Auction cohorts must be frozen independently of the current draft; use reference='parent'.")
        return normalize_sampling(raw)

    def do_generate_cases(self, args, operation, call_id):
        module = audit_module()
        sampling = module.sampling_config(
            {**args["sampling"], "seed": args["sampling"].get("seed", operation["seed"])}, max_count=100000)
        self.reserve_lab(sampling["count"], operation)
        if "cohort" in args:
            from bridge_evolve.evolution.sampling import conditional_source
            cohort = self.cohort(args["cohort"])
            draw = {}
            source = conditional_source(cohort, sampling["count"], sampling["seed"],
                                        self.stop, draw, self.controller.system)
            accepted = []
            for raw in source:
                key = canonical_hash({"call": call_id, "index": len(accepted)})[:16]
                self.data["cases"][key] = {"name": f"targeted-{len(accepted) + 1}", "board": raw["board"],
                                          "sampling": cohort}
                accepted.append({"case_id": key, "board": raw["board"]})
            return {"cases": accepted, "generated": len(accepted), "sampling": cohort, "draw": draw,
                    "scope": "targeted diagnostic cases, not fitness"}
        predicate = None
        if "hand_condition" in args:
            raw = blank_system().to_data()
            condition = expand_condition(args["hand_condition"], self.data["system"].get("features", {}))
            raw["rules"] = [{"id": "research.hand_condition", "priority": 1, "when": condition,
                             "action": {"kind": "call", "call": "P"}}]
            predicate = validate_system(raw)
        seat = Seat(args.get("seat", "N"))
        needed = sampling["count"]
        sampling["count"] = min(100000, max(needed, 100000 if predicate is not None else needed))
        accepted = []
        for raw in module.sampled_boards(sampling, cancelled=lambda: self.stop() or False):
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
        if not args.get("dds") and args.get("table", 1) != 1:
            raise SystemError("Without DDS there is only table=1; table=2 requires DDS.", pointer="/table")
        if args.get("dds"):
            if args.get("prefix"):
                raise ValueError("DDS comparison uses actual whole auctions; counterfactual prefixes are debugging only.")
            table = args.get("table", 2 if args.get("side") == "EW" else 1)
            side = "NS" if table == 1 else "EW"
            if "side" in args and args["side"] != side:
                raise SystemError("side conflicts with table: table=1 is candidate NS, table=2 is candidate EW.",
                                  pointer="/side")
            self.reserve_lab(1, operation)
            own, opponent = self.system(snapshot), self.controller.system(self.controller.config["baselines"][0]["hash"])
            self.controller.evaluator.reserve_pair(canonical_hash({
                "session": self.identity, "operation": operation["seed"], "case": case_id, "snapshot": own.content_hash}))
            self.save()
            definitions = {system.content_hash: system for system in (own, opponent)}
            engine = audit_module().BoardAuditEngine(Path(self.controller.config["project"]),
                                                     {key: {"system": system} for key, system in definitions.items()})
            record, _, findings = engine.batch_board({"index": 0, "board": self.data["cases"][case_id]["board"]},
                {"system_a": own.content_hash, "system_b": opponent.content_hash}, definitions, fresh_dds=True)
            from bridge_evolve.evolution.faults import adjudicate_baseline
            record = adjudicate_baseline(record, own.content_hash, opponent.content_hash,
                                          self.controller.config.get("baseline_fault_policy", "strict"))
            if record.get("dds") and record["dds"]["oracle"] != self.controller.state["oracle"]:
                raise RuntimeError("Research DDS provenance differs from the frozen evaluator.")
            result = {"board": agent_board(record), "errors": record["audit_result"]["errors"], "findings": findings,
                      "selection": {"table": table, "side": side}, "scope": "diagnostic two-table comparison"}
            if record.get("adjudication"):
                result["adjudication"] = record["adjudication"]
            if "turn" in args:
                result["step"] = agent_decision(record, table, args["turn"])["decision"]
                result["selection"]["turn"] = args["turn"]
            return result
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
        failures, passed, changed, protocol_witnesses = [], 0, 0, 0
        goal = self.controller.protocol_goal
        for case in self.data["tests"]:
            self.stop()
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
                        raise AssertionFailure(assertion["path"], op, value, expected)
                if case.get("changed_from_parent"):
                    parent = self.play(case["case_id"], snapshot="parent", **kwargs)
                    new_calls = actual["call"] if kwargs["decision"] else [step["call"] for step in actual["auction"]]
                    old_calls = parent["call"] if kwargs["decision"] else [step["call"] for step in parent["auction"]]
                    if new_calls == old_calls:
                        raise ValueError("Expected a real action change from the parent, but calls are identical.")
                    changed += 1
                if (goal is not None and case["mode"] == "auction" and not case.get("prefix")
                        and case.get("changed_from_parent") and goal.natural_witness(actual, case.get("side", "NS"))):
                    protocol_witnesses += 1
                passed += 1
            except (ValueError, KeyError, IndexError, TypeError) as error:
                details = {"test": case["name"], "type": type(error).__name__,
                           **(error.details if isinstance(error, AssertionFailure) else {"error": str(error)})}
                reference = self.full_result(details)
                failures.append({"test": case["name"], "type": type(error).__name__,
                                 "error": short_text(error), "details_ref": reference})
        return {"tests": len(self.data["tests"]), "passed": passed, "failed": len(failures),
                "change_witnesses": changed, "failures": failures[:10], "system_hash": self.system().content_hash,
                "document_hash": canonical_hash(self.data["system"]),
                **({"protocol_witnesses": protocol_witnesses} if goal is not None else {}),
                "scope": "authored regression tests, not exhaustive protocol proof"}

    def do_run_tests(self, _args, *_):
        result = self.test_results()
        self.data["last_tests"] = result
        return result

    def do_validate_system(self, _args, *_):
        system = self.system()
        self.require_contract(system)
        specification = render_system(system)
        return {"system_hash": system.content_hash, "rules": len(system.rules), "specification_bytes": len(specification.encode()),
                "status": "typed_and_rendered", "scope": "not an exhaustive semantic proof"}

    def do_trial(self, args, operation, call_id):
        from bridge_evolve.evolution.controller import register_system
        count = args.get("boards", min(16, self.controller.config["boards"]["screen"]))
        campaign = self.controller.config.get("research_budget_mode") == "campaign"
        if args.get("uniform") and "sampling" in args:
            raise ValueError("Choose targeted sampling or uniform=true, not both.")
        if campaign and "sampling" not in args and not args.get("uniform") and self.controller.config.get("development_sampling") is None:
            raise ValueError("Choose trial.sampling for a targeted mechanism test, or explicitly set uniform=true for global exploration.")
        sampling = self.cohort(args["sampling"]) if "sampling" in args else None
        if args.get("uniform") and self.controller.config.get("development_sampling") is not None:
            raise ValueError("This frozen conditional experiment cannot silently switch to uniform trials.")
        if not campaign:
            self.reserve_lab(count, operation)
        system = self.system()
        identity = register_system(self.controller.directory, self.controller.state, system)
        self.controller.checkpoint()
        prefix = "r-" + self.identity + "-" + canonical_hash(call_id)[:12]
        required = args.get("min_changed_families", 16 if campaign else 0)
        available = self.controller.config.get("max_trial_boards", 256) - self.data.get("trial_boards", 0)
        available += sum(operation.get("trial_allocations", {}).values())
        requested = args.get("max_boards", count * 8 if campaign else count)
        maximum = min(requested, available) if campaign else requested
        if maximum < count:
            raise ValueError(f"Trial asks for {count} initial boards but only {maximum} can fit; "
                             f"remaining independent DDS trial allowance is {available}. Generated cases use a separate budget.")
        outcomes = []
        drawn, changed = 0, 0
        while drawn < maximum:
            batch = min(count if not outcomes else max(count, drawn), maximum - drawn)
            round_id = prefix if not outcomes else prefix + f"-b{len(outcomes) + 1}"
            if campaign:
                allocations = operation.setdefault("trial_allocations", {})
                if round_id not in allocations:
                    used = self.data.get("trial_boards", 0)
                    if used + batch > self.controller.config["max_trial_boards"]:
                        raise ValueError("The independent DDS exploratory budget is exhausted; generated-case budget is unchanged.")
                    self.data["trial_boards"] = used + batch
                    allocations[round_id] = batch
                    self.save()
            result = self.controller.evaluator.evaluate(round_id, list(dict.fromkeys([identity, self.parent])),
                                                        batch, **({"sampling": sampling} if sampling is not None else {}))
            outcome = self.trial_outcome(result, identity, batch)
            outcomes.append(outcome)
            drawn += batch
            changed += outcome.get("changes", {}).get("changed_families", 0)
            if outcome["status"] != "checked" or changed >= required:
                break
        outcome = copy.deepcopy(outcomes[-1])
        outcome.update(boards=drawn, rounds=[row["round_id"] for row in outcomes],
                       effect_coverage={"changed_families": changed, "required": required,
                                        "status": "sufficient" if changed >= required else "insufficient",
                                        "maximum_boards": maximum, "requested_maximum_boards": requested,
                                        "limited_by_remaining_trial_budget": maximum < requested},
                       scope="adaptive conditional mechanism experiment, NOT global fitness or promotion evidence"
                           if sampling is not None else "adaptive uniform exploration, not promotion evidence")
        if all("sampling_draw" in row for row in outcomes):
            outcome["sampling_draw"] = {key: sum(row["sampling_draw"][key] for row in outcomes)
                                        for key in ("attempts", "accepted")}
        if all("delta" in row for row in outcomes):
            for key in ("delta", "objective", "parent_objective"):
                outcome[key] = sum(row[key] * row["boards"] for row in outcomes) / drawn
            outcome["changes"] = {key: sum(row["changes"][key] for row in outcomes) for key in outcomes[0]["changes"]}
            if all("score_contributions" in row for row in outcomes):
                contribution = copy.deepcopy(outcomes[0]["score_contributions"])
                for key in ("played_contribution", "forfeit_contribution", "total_delta"):
                    contribution[key] = sum(row["score_contributions"][key] * row["boards"] for row in outcomes) / drawn
                for key in ("candidate_forfeit_pairs", "parent_forfeit_pairs"):
                    contribution[key] = sum(row["score_contributions"][key] for row in outcomes)
                outcome["score_contributions"] = contribution
        self.data["last_trial"] = outcome
        return outcome

    def trial_outcome(self, result, identity, count):
        round_id = result["id"]
        outcome = {"round_id": round_id, "boards": count, "system_hash": identity, "status": result["status"][identity],
                   "objective": result["objective"].get(identity), "parent_objective": result["objective"].get(self.parent),
                   "errors": result["errors"][identity][:5], "error_count": len(result["errors"][identity]),
                   "scope": "adaptive exploratory experiment, not promotion evidence"}
        if result.get("evaluation_scope") == "conditional":
            outcome.update(evaluation_scope="conditional", development_sampling=result["development_sampling"],
                           sampling_draw=result["sampling_draw"])
        if "protocol_coverage" in result:
            outcome["protocol_coverage"] = result["protocol_coverage"][identity]
        from bridge_evolve.evolution.faults import score_contributions
        contributions = score_contributions(result, identity, self.parent)
        if contributions is not None:
            outcome["score_contributions"] = contributions
        if result["status"][identity] == "checked" and result["status"][self.parent] == "checked":
            outcome["changes"] = comparison_changes(self.controller.directory, result, identity, self.parent)
            outcome["delta"] = result["objective"][identity] - result["objective"][self.parent]
        return outcome

    def do_consult(self, args, *_):
        if not isinstance(args["question"], str) or not args["question"].strip():
            raise ValueError("Ask a concrete research question.")
        pointers = args.get("pointers", ["/rules", "/protocol", "/fallback"])
        if not isinstance(pointers, list) or not 1 <= len(pointers) <= 100:
            raise ValueError("A consultation needs 1-100 explicit system or workspace pointers.")
        selections = [(pointer, pointer_parts(pointer)) for pointer in pointers]
        views, workspace_views, pointer_errors = {}, {}, {}
        for pointer, parts in selections:
            scope, document, target, destination = "system", self.data["system"], pointer, views
            if parts and parts[0] == "workspace":
                scope, document, destination = "workspace", self.data, workspace_views
                target = pointer[len("/workspace"):]
            elif parts and parts[0] == "tests":
                scope, document, destination = "workspace", self.data, workspace_views
            try:
                value = at_pointer(document, target)
            except ValueError as error:
                pointer_errors[pointer] = {"scope": scope, "type": type(error).__name__, "error": str(error)}
            else:
                destination[pointer] = paged_value(value, maximum_bytes=self.result_limit)
        evidence = self.feedback({"question": args["question"], "system": views,
            "workspace": workspace_views, "pointer_errors": pointer_errors,
            "actual_tests": self.data.get("last_tests"), "actual_trial": self.data.get("last_trial")},
            limit=min(self.result_limit, self.controller.config["max_context_bytes"] // 2))
        messages = [
            {"role": "system", "content": "Independently challenge this bridge protocol design. You are an advisory reviewer, not the evaluator. Check sender/receiver/continuation consistency, hand-information boundaries, unsupported assumptions and test gaps. Give concise evidence-based Chinese advice, not a private thinking transcript. Do not claim tests were run."},
            {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
        ]
        window = getattr(self.controller, "delivery_window", None)
        if window:
            from bridge_evolve.evolution.delivery import DeliveryDeadline
            if window.should_return(0):
                raise DeliveryDeadline("The last request is reserved for returning the executor's solution, not consultation.")
            messages.append(window.reminder(self.controller.state["research_sessions"][self.identity]["turns"]))
        response = self.client.request(messages, [], label=self.identity + ":review",
                                       output_tokens=self.controller.config["max_output_tokens"])
        advice = response_text(response)
        if not advice:
            raise ValueError("The configured reviewer returned no advisory text.")
        self.data["notes"].append({"kind": "review", "question": args["question"], "text": advice})
        return {"review": advice, "model": self.controller.config["model"],
                "pointer_errors": pointer_errors, "scope": "advice, not independent validation"}

    def do_research_note(self, args, *_):
        if not isinstance(args["text"], str) or not 1 <= len(args["text"]) <= 16000:
            raise ValueError("Research notes must contain 1-16000 characters.")
        self.data["notes"].append({"kind": "research", "text": args["text"]})
        return {"saved": True, "note": len(self.data["notes"])}

    def do_return_solution(self, args, *_):
        from bridge_evolve.evolution.manager import CampaignManager
        window = getattr(self, "delivery_window", None)
        if window is None:
            raise ValueError("A solution return requires an active bounded executor call.")
        json_bytes(args)
        manager = CampaignManager(self.controller)
        contract = manager.branch(self.identity)["contract"]
        turn = self.controller.state["research_sessions"][self.identity]["turns"]
        existing = self.controller.read_artifact(window.path.with_name(window.path.stem + ".return.json"))
        if existing is not None:
            value = window.complete(args, existing["evidence"], turn)
        else:
            provisional = self.provisional_patch(args, window.data["id"]) if "provisional_patch" in args else None
            receipt = manager.workspace_receipt(self.data, turn, contract)
            checkpoint = manager.checkpoint_data(self.identity, self.data, window.data["id"], receipt)
            evidence = {"checkpoint": checkpoint, "actual_receipt": receipt}
            if provisional is not None:
                evidence["provisional_patch"] = provisional
            value = window.complete(args, evidence, turn)
        self.data["best_solution"] = copy.deepcopy(args)
        self.data["summary"] = (
            "本周期具体方案已返回，实际实现与验证另计。\n方案：" + args["proposal"]
            + "\n已知局限：" + args["limitations"] + "\n下一步：" + args["next_action"])
        self.data.setdefault("solution_returns", {})[contract["review"]] = {
            "id": value["id"], "run": self.controller.state["id"], "solution": copy.deepcopy(args),
            "result": window.data["result"], "returned_at": value["returned_at"]}
        if value.get("provisional_application"):
            self.data["solution_returns"][contract["review"]]["provisional_application"] = copy.deepcopy(value["provisional_application"])
        return {"solution_return": self.data["solution_returns"][contract["review"]],
                "provisional_application": value.get("provisional_application"),
                "instruction": "The period returned a concrete solution; implementation and verification are reported separately."}

    def provisional_patch(self, solution, delivery):
        patch = solution["provisional_patch"]
        identity = "proposal-" + canonical_hash({"delivery": delivery, "patch": patch})[:24]
        proposals = self.data.setdefault("provisional_proposals", {})
        if identity in proposals:
            return proposals[identity]
        before = canonical_hash(self.data["system"])
        value = {"id": identity, "status": "proposed", "paths": [edit.get("path", "") for edit in patch["edits"]],
                 "proposal": solution["proposal"], "basis": solution["basis"], "limitations": solution["limitations"],
                 "falsification": patch["falsification"], "patch": copy.deepcopy(patch), "before": before,
                 "verification": "unverified_at_application", "acceptance": "not_accepted", "created_at": timestamp(),
                 "delivery": delivery}
        try:
            if before != patch["document_hash"]:
                raise ValueError("The provisional proposal targets a stale draft; no edits were applied.")
            updated = edit_document(self.data["system"], patch["edits"])
            json_bytes(updated)
            after = canonical_hash(updated)
        except ValueError as error:
            LOGGER.warning("Provisional proposal %s was not applied: %s", identity, error)
            value.update(status="not_applied", error=str(error))
        else:
            self.data["system"] = updated
            value.update(status="applied_provisional", after=after)
            self.data["notes"].append({"kind": "research", "text":
                "临时提案已写入草稿，未验证／未验收：" + identity + "\n" + solution["limitations"]})
        proposals[identity] = value
        self.save()
        return value

    def do_publish_candidate(self, args, *_):
        from bridge_evolve.evolution.controller import register_system
        if not isinstance(args["hypothesis"], str) or not args["hypothesis"].strip():
            raise ValueError("A release needs a concrete hypothesis.")
        if len(self.data["releases"]) >= self.controller.config["max_releases"]:
            raise ValueError("This episode's release budget is exhausted.")
        system = self.system()
        self.require_contract(system)
        if self.controller.module_contract is not None and any(
                job["candidate"] == system.content_hash and "outcome" in job
                for job in self.controller.evaluations.jobs.values()):
            raise ValueError("This controlled-module snapshot already has a completed evaluation; submit a distinct design or conclude the episode.")
        parent = self.system("parent").to_data()
        if canonical_hash({key: value for key, value in system.to_data().items() if key != "name"}) == canonical_hash(
                {key: value for key, value in parent.items() if key != "name"}):
            raise ValueError("The candidate is identical to its incumbent; publish an actual executable change.")
        tests = self.test_results()
        self.data["last_tests"] = tests
        if not tests["tests"] or tests["failed"] or not tests["change_witnesses"]:
            raise ValueError("Release requires passing executable tests and a changed_from_parent witness: "
                             + json.dumps(self.feedback(tests, limit=6000), ensure_ascii=False))
        if self.controller.protocol_goal is not None and not tests.get("protocol_witnesses"):
            raise ValueError("The protocol goal needs a natural empty-prefix opening-Pass/partner-response change witness.")
        if self.controller.module_contract is not None and not any(
                test.get("changed_from_parent") and test["mode"] == "auction" and not test.get("prefix")
                for test in self.data["tests"]):
            raise ValueError("Controlled module releases require a naturally reached whole-auction behavior witness, not only forced-prefix decisions.")
        if any(release["hash"] == system.content_hash for release in self.data["releases"]):
            raise ValueError("This exact whole-system release was already published in the episode.")
        identity = register_system(self.controller.directory, self.controller.state, system)
        release = {"hash": identity, "parent": self.parent, "hypothesis": args["hypothesis"],
                   "tests": tests, "test_definitions": copy.deepcopy(self.data["tests"]),
                   "cases": {case["case_id"]: self.data["cases"][case["case_id"]] for case in self.data["tests"]},
                   "specification": f"systems/{identity}.md", "origin": self.identity,
                   "research_resources_at_publication": session_costs(self.controller.state, self.identity)}
        if self.controller.module_contract is not None:
            release["module_contract"] = self.data["last_contract"]
        if self.controller.protocol_goal is not None:
            release["protocol_goal"] = self.data["last_contract"]
        path = self.path.parent / f"release-{len(self.data['releases']) + 1}.json"
        self.controller.artifact(path, release)
        self.data["releases"].append({**{key: release[key] for key in ("hash", "parent", "hypothesis", "tests")},
                                      "path": str(path.relative_to(self.controller.directory))})
        evaluation = self.controller.evaluations.enqueue(path.relative_to(self.controller.directory).as_posix(),
            lineage=self.identity.split("-")[1], generation=int(self.identity[1:4]))
        return {"published": True, "hash": identity, "tests": tests,
                "release": str(path.relative_to(self.controller.directory)), "promotion": "not_yet_evaluated",
                "evaluation": {"id": evaluation["id"], "status": evaluation["status"]}}

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
