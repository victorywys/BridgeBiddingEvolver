"""Bounded evidence packets and data-only protocol proposals."""

from __future__ import annotations

import copy
import json
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from bridge_evolve.automation import GATEWAY, _gateway_key
from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.record_log import agent_board, agent_decision
from bridge_evolve.systems import validate_system


class ContextBudgetError(ValueError):
    pass


def agent_packet(packet: dict) -> dict:
    value = copy.deepcopy(packet)

    def size():
        return len(json.dumps(value, ensure_ascii=False).encode())

    if size() > 160000 and len(value.get("deferred_groups", [])) > 32:
        value["deferred_group_count"] = len(value["deferred_groups"])
        value["deferred_groups"] = value["deferred_groups"][:32]
    while size() > 160000 and value.get("previous_attempts"):
        value["previous_attempts"].pop(0)
        value["deferred_previous_attempts"] = value.get("deferred_previous_attempts", 0) + 1
    return value


def expand_condition(value, features: dict, depth=0, budget=None):
    if budget is None:
        budget = [5000]
    budget[0] -= 1
    if depth > 64 or budget[0] < 0:
        raise ContextBudgetError("Rule dependency expansion exceeds the proposal context budget.")
    if isinstance(value, dict):
        if value.get("op") == "ref":
            return expand_condition(features[value["name"]], features, depth + 1, budget)
        return {key: expand_condition(item, features, depth + 1, budget) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_condition(item, features, depth + 1, budget) for item in value]
    return value


def rule_catalog(system, modules: list[str], hits: dict, limit=12, preferred=()) -> tuple[list[dict], list[str]]:
    raw = system.to_data()
    eligible = [rule for rule in raw["rules"] if modules == ["*"] or any(module in rule["id"] for module in modules)]
    eligible.sort(key=lambda rule: (rule["id"] not in preferred, -hits.get(rule["id"], 0), -rule["priority"], rule["id"]))
    result, deferred = [], []
    for rule in eligible:
        try:
            expanded = expand_condition(rule["when"], raw["features"])
        except ContextBudgetError:
            deferred.append(rule["id"])
            continue
        if len(json.dumps(expanded)) <= 24000:
            result.append({**rule, "when": expanded})
        if len(result) == limit:
            break
    return result, deferred


def evidence_packet(system, records: list[dict], modules: list[str], limit=8) -> dict:
    groups, hits = {}, Counter()
    out_of_scope = 0
    for record in records:
        for table_index, table in enumerate(record["tables"]):
            own_side = "NS" if table_index == 0 else "EW"
            if table["systems"].get(own_side, {}).get("hash") != system.content_hash:
                continue
            for turn, step in enumerate(table["auction"]):
                if ("NS" if step["seat"] in "NS" else "EW") != own_side:
                    continue
                rule = step.get("private_trace", {}).get("rule_id", "")
                if modules != ["*"] and not any(module in rule for module in modules):
                    out_of_scope += 1
                    continue
                hits[rule] += 1
                context = step.get("public_context", {})
                other_side = "EW" if own_side == "NS" else "NS"
                hand = record.get("hand_summary", {}).get(step["seat"], {})
                signature = [rule, table["systems"].get(other_side, {}).get("hash"), record["dealer"],
                             record["vulnerability"], step["seat"], context, hand.get("hcp", 0) // 3,
                             hand.get("shape"), [item["call"] for item in table["auction"][:turn]]]
                identity = canonical_hash(signature)[:16]
                group = groups.setdefault(identity, {"id": identity, "rule_id": rule, "occurrences": 0,
                                                     "imp_sum": 0, "examples": []})
                imp = record.get("imp")
                group["occurrences"] += 1
                group["imp_sum"] += imp if imp is not None else 0
                if len(group["examples"]) < 2:
                    group["examples"].append({"board": agent_board(record), "selected": agent_decision(record, table_index + 1, turn + 1),
                                              "diagnostic_findings": record.get("audit_result", {}).get("findings", []),
                                              "regression_board": {key: record[key] for key in ("dealer", "vulnerability", "hands")}})
    ordered = sorted(groups.values(), key=lambda group: (group["imp_sum"] / group["occurrences"], -group["occurrences"], group["id"]))
    selected = ordered[:limit]
    catalog, deferred_rules = rule_catalog(system, modules, hits, preferred={group["rule_id"] for group in selected})
    states = set()

    def find_states(value):
        if isinstance(value, dict):
            args = value.get("args", [])
            if value.get("op") == "eq" and len(args) == 2 and isinstance(args[0], dict) and args[0].get("op") == "state":
                if isinstance(args[1], str):
                    states.add(args[1])
            for item in value.values():
                find_states(item)
        elif isinstance(value, list):
            for item in value:
                find_states(item)

    for rule in catalog:
        find_states(rule["when"])
    transitions = []
    raw = system.to_data()
    for name in sorted(states):
        for transition in raw["protocol"]["states"][name]["transitions"][:8]:
            transitions.append({"state": name, **transition})
    packet = {"schema": "bridge-evolution-analysis/v1", "parent_hash": system.content_hash, "system": system.name,
              "scope": modules, "total_groups": len(groups), "selected_groups": selected,
              "deferred_groups": [group["id"] for group in ordered[limit:]],
              "deferred_rule_ids": deferred_rules, "out_of_scope_calls": out_of_scope,
              "rules": catalog, "transitions": transitions,
              "parameters": raw["protocol"]["parameters"]}

    def context_size():
        return len(json.dumps(agent_packet(packet), ensure_ascii=False).encode())

    if context_size() > 160000:
        packet["selected_groups"] = selected[:2]
        packet["deferred_groups"] = [group["id"] for group in ordered[2:]]
    while context_size() > 120000 and len(packet["rules"]) > 4:
        packet["rules"].pop()
    while context_size() > 120000 and len(packet["transitions"]) > 4:
        packet["transitions"].pop()
    if context_size() > 160000:
        raise ValueError("Approved analysis packet exceeds its context budget.")
    return packet


def apply_proposal(system, packet: dict, proposal: dict):
    if (not isinstance(proposal, dict) or set(proposal) - {"parent_hash", "hypothesis", "issue_ids", "edits", "test_plan"}
            or proposal.get("parent_hash") != system.content_hash
            or not isinstance(proposal.get("hypothesis"), str) or not proposal["hypothesis"].strip()):
        raise ValueError("Proposal must identify its frozen parent and a hypothesis.")
    edits = proposal.get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 8:
        raise ValueError("A proposal needs one to eight atomic IR edits.")
    issues = proposal.get("issue_ids", [])
    known_issues = {group["id"] for group in packet["selected_groups"]}
    if not isinstance(issues, list) or any(not isinstance(issue, str) or issue not in known_issues for issue in issues):
        raise ValueError("Proposal references unknown evidence groups.")
    rules = {rule["id"] for rule in packet["rules"]}
    transitions = {(item["state"], item["id"]) for item in packet["transitions"]}
    raw = copy.deepcopy(system.to_data())
    touched = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) - {"kind", "id", "state", "field", "value"} or "value" not in edit:
            raise ValueError("Invalid IR edit.")
        if any(not isinstance(edit.get(key), str) for key in ("kind", "id", "field")) or (
                "state" in edit and not isinstance(edit["state"], str)):
            raise ValueError("IR edit selectors must be strings.")
        identity = (edit.get("kind"), edit.get("state"), edit.get("id"), edit.get("field"))
        if identity in touched:
            raise ValueError("Repeated edit target.")
        touched.add(identity)
        field = edit.get("field")
        if edit.get("kind") == "rule" and edit.get("id") in rules and field in ("when", "action"):
            target = next(rule for rule in raw["rules"] if rule["id"] == edit["id"])
            target[field] = edit["value"]
        elif edit.get("kind") == "transition" and (edit.get("state"), edit.get("id")) in transitions:
            target = next(item for item in raw["protocol"]["states"][edit["state"]]["transitions"] if item["id"] == edit["id"])
            if field == "when":
                target[field] = edit["value"]
            elif isinstance(field, str) and field.startswith("set.") and field[4:] in raw["protocol"]["parameters"]:
                target["set"][field[4:]] = edit["value"]
            else:
                raise ValueError("Only transition conditions and declared public assignments can be edited.")
        else:
            raise ValueError("Edit target is outside the approved packet.")
    candidate = validate_system(raw)
    if candidate.content_hash == system.content_hash:
        raise ValueError("Proposal makes no change.")
    return candidate


def numeric_proposals(system, packet: dict, count=2, offset=0) -> dict:
    sites = []

    def visit(value, path):
        if isinstance(value, dict):
            args = value.get("args", [])
            if value.get("op") in ("ge", "gt", "le", "lt") and len(args) == 2 and type(args[1]) is int and 8 <= args[1] <= 37:
                left = json.dumps(args[0])
                if "hcp" in left:
                    sites.append(path + ["args", 1])
            for key, item in value.items():
                visit(item, path + [key])
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, path + [index])

    options = []
    for rule in packet["rules"]:
        sites.clear()
        visit(rule["when"], [])
        if sites:
            options.append((rule, list(sites[0])))
    proposals = []
    for index in range(count):
        if not options:
            break
        rule, path = options[((offset + index) // 2) % len(options)]
        condition = copy.deepcopy(rule["when"])
        target = condition
        for key in path[:-1]:
            target = target[key]
        radius = min(3, 1 + (offset + index) // (2 * len(options)))
        target[path[-1]] += radius if (offset + index) % 2 == 0 else -radius
        proposals.append({"parent_hash": system.content_hash, "hypothesis": "Controlled HCP threshold experiment; benefit is not assumed.",
                          "issue_ids": [], "edits": [{"kind": "rule", "id": rule["id"], "field": "when", "value": condition}]})
    return {"analysis": [], "proposals": proposals, "source": "numbers"}


class LocalAgent:
    def __init__(self, project, model: str, timeout: int, *, client=None):
        self.model, self.timeout = model, timeout
        self.client = client
        self.key = _gateway_key(project, model) if client is None else None

    def propose(self, packet: dict, count=2, *, request_number=None) -> dict:
        packet = agent_packet(packet)
        if len(json.dumps(packet, ensure_ascii=False).encode()) > 160000:
            raise ContextBudgetError("Approved analysis packet exceeds its context budget.")
        prompt = (
            "You design executable bridge partnership protocols. This is post-hoc DEVELOPMENT evidence, not policy input. "
            "Return JSON only: {analysis:[{issue_id,conclusion,rationale}],proposals:[{parent_hash,hypothesis,issue_ids,edits,test_plan}]}. "
            f"Return at most {count} proposals, or none if unsupported. Never treat DDS makeability as a mandatory bid. "
            "Edit only rules and transitions in this packet. Rule edits: {kind:'rule',id,field:'when'|'action',value}. "
            "Transition edits: {kind:'transition',state,id,field:'when'|'set.PARAMETER',value}. "
            "Use the supplied v2 IR syntax, keeping rule priorities and all other fields fixed. "
            "Synchronize truthful public HCP/length guarantees, responses and safe exits when necessary. "
            "No file paths, Python, tools, new operators, hidden hands, DDS, dataset IDs or seeds in emitted policy. "
            "Explain expected gain and risk; the evaluator, not you, decides whether a candidate is better."
        )
        payload = {"model": self.model, "store": False, "stream": False, "tools": [],
                   "max_output_tokens": 8192, "input": [
                       {"role": "system", "content": [{"type": "input_text", "text": prompt}]},
                       {"role": "user", "content": [{"type": "input_text", "text": json.dumps(packet, ensure_ascii=False)}]}]}
        if self.client is not None:
            data = self.client.request(payload["input"], [], label="legacy-local-proposal",
                output_tokens=min(8192, self.client.config["max_output_tokens"]), reserved_request=request_number)
        else:
            request = Request(GATEWAY + "/responses", data=json.dumps(payload).encode(),
                              headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
            try:
                with build_opener(ProxyHandler({})).open(request, timeout=self.timeout) as response:
                    body = response.read(2 * 1024 * 1024 + 1)
            except HTTPError as error:
                code = error.code
                error.close()
                raise RuntimeError(f"Local proposal agent returned HTTP {code}.") from None
            except (URLError, TimeoutError) as error:
                raise RuntimeError("Local proposal agent was unavailable or timed out.") from error
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("Agent response exceeded the bounded size.")
            data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("output", []), list):
            raise ValueError("Agent returned an invalid response envelope.")
        parts = []
        for item in data.get("output", []):
            if not isinstance(item, dict):
                raise ValueError("Agent returned an invalid output item.")
            if item.get("type") in ("function_call", "custom_tool_call"):
                raise ValueError("Agent tools are disabled; no tool output is executed.")
            content = item.get("content", [])
            if not isinstance(content, list) or any(not isinstance(value, dict) for value in content):
                raise ValueError("Agent returned invalid message content.")
            parts.extend(value["text"] for value in content
                         if value.get("type") == "output_text" and isinstance(value.get("text"), str))
        text = "\n".join(parts) or data.get("output_text", "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Agent returned no textual proposal.")
        text = text.strip()
        if text.startswith("```json") and text.rstrip().endswith("```"):
            text = text[7:text.rfind("```")].strip()
        result = json.loads(text)
        if (not isinstance(result, dict) or not isinstance(result.get("proposals"), list)
                or len(result["proposals"]) > count or not isinstance(result.get("analysis", []), list)):
            raise ValueError("Agent did not return the bounded proposal schema.")
        known = {group["id"] for group in packet["selected_groups"]}
        seen = set()
        for item in result.get("analysis", []):
            if (not isinstance(item, dict) or item.get("issue_id") not in known or item["issue_id"] in seen
                    or any(not isinstance(item.get(key), str) for key in ("conclusion", "rationale"))):
                raise ValueError("Agent analysis did not identify distinct approved problem groups.")
            seen.add(item["issue_id"])
        if not isinstance(data.get("usage", {}), dict):
            raise ValueError("Agent returned invalid usage metadata.")
        return {**result, "source": "agent", "usage": {key: value for key, value in data.get("usage", {}).items()
                                                     if type(value) is int and value >= 0}}
