"""Atomic source evidence and tool interactions in bounded model requests."""

import copy
import hashlib
import json
import logging

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evolution.feedback import bounded, json_bytes
from bridge_evolve.evolution.models import request_body

LOGGER = logging.getLogger(__name__)


class ContextCapacityError(ValueError):
    pass


def latest_interaction(messages):
    last = next((index for index in range(len(messages) - 1, -1, -1)
                 if messages[index].get("type") == "function_call_output"), None)
    if last is None:
        return []
    first = last
    while first and messages[first - 1].get("type") == "function_call_output":
        first -= 1
    ids = {message["call_id"] for message in messages[first:last + 1]}
    calls = {}
    for index in range(first - 1, -1, -1):
        message = messages[index]
        if message.get("type") == "function_call" and message["call_id"] in ids:
            calls.setdefault(message["call_id"], index)
            if len(calls) == len(ids):
                break
    if len(calls) != len(ids):
        raise ValueError("A retained tool result is missing its original function call.")
    return copy.deepcopy(messages[min(calls.values()):last + 1])


def result_key(pointer, value, offset=0):
    return canonical_hash({"pointer": pointer, "offset": offset, "value": value})


def assemble_context(config, prompt, context, tools, *, maximum=None, tail=(), required=(),
                     tool_choice=None, archive_tail=None):
    maximum = min(config["max_context_bytes"], config["max_context_bytes"] if maximum is None else maximum)
    if maximum <= 0:
        raise ContextCapacityError("Model input byte allowance must be positive.")
    context = copy.deepcopy(context)
    tail = copy.deepcopy(list(tail))
    contracts = {key: context.pop(key) for key in required if key in context}
    memory = context.pop("working_set", None)
    focus = context.pop("invalid_draft_focus", None)
    recent = context.pop("recent_tool_results", [])
    progress = context.pop("implementation_state", None)
    if progress is None and memory and "implementation_state" not in contracts:
        progress = memory.get("state")
    original_tail = tail
    native_ids = {message["call_id"] for message in tail if message.get("type") == "function_call_output"}
    native_values = set()
    for message in tail:
        if message.get("type") == "function_call_output":
            result = json.loads(message["output"])
            if (isinstance(result, dict) and "pointer" in result and "value" in result
                    and result.get("workspace_hash") == (memory or {}).get("system_document_hash")):
                native_values.add(result_key(result["pointer"], result["value"], result.get("offset", 0)))

    units = []
    for row in reversed(recent):
        if row.get("call_id") not in native_ids:
            units.append(("result", row.get("call_id"), row))
    if focus and "value" in focus:
        if result_key(focus["pointer"], focus["value"]) not in native_values:
            units.append(("focus", focus["pointer"], focus["value"]))
        focus = {key: value for key, value in focus.items() if key != "value"}
    if memory:
        for kind, field in (("raw", "raw_reads"), ("node", "nodes")):
            for item in memory.get(field, []):
                value = item["page"].get("value") if kind == "raw" else item["data"]
                offset = item["page"].get("offset", 0) if kind == "raw" else 0
                if result_key(item["pointer"], value, offset) not in native_values:
                    units.append((kind, item["pointer"], item))
        skeleton = {key: value for key, value in memory.items()
                    if key not in ("raw_reads", "nodes", "state", "instruction", "retained_nodes", "unloaded_dependencies")}
        memory = {**bounded(skeleton, limit=max(512, min(4000, maximum // 8))),
                  "system_document_hash": memory["system_document_hash"]}
    if progress is not None:
        units.append(("implementation_state", "/implementation_state", progress))
    omitted_tail = None

    def render(view, count):
        selected = units[:count]
        payload = {**view, **contracts}
        if memory is not None:
            payload["working_set"] = {
                **memory,
                "raw_reads": [value for kind, _, value in selected if kind == "raw"],
                "nodes": [value for kind, _, value in selected if kind == "node"],
                "retained_nodes": sum(kind == "node" for kind, _, _ in selected),
                "instruction": "Only source bodies present in this request are available. Archived or omitted data is not model memory.",
            }
        if focus:
            payload["invalid_draft_focus"] = dict(focus)
            for kind, _, value in selected:
                if kind == "focus":
                    payload["invalid_draft_focus"]["value"] = value
        results = [value for kind, _, value in selected if kind == "result"]
        if results:
            payload["recent_tool_results"] = list(reversed(results))
        for kind, _, value in selected:
            if kind == "implementation_state":
                payload["implementation_state"] = value
        payload["context_delivery"] = {
            "omitted_items": len(units) - count,
            "omitted_examples": [{"kind": kind, "pointer_or_call": key} for kind, key, _ in units[count:count + 4]],
            "latest_tool_interaction": "not_delivered" if omitted_tail else "full" if tail else "none",
            "implementation_state": ("full" if "implementation_state" in payload else
                                     "not_delivered" if progress is not None else "none"),
        }
        if progress is not None and "implementation_state" not in payload:
            payload["context_delivery"]["implementation_state_ref"] = {"workspace_pointer": "/implementation_state"}
        if omitted_tail:
            payload["context_delivery"]["tool_result_not_delivered"] = omitted_tail
        return [{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}, *tail]

    def fits(messages):
        return len(request_body(config, messages, tools, tool_choice=tool_choice)) <= maximum

    allowance = max(512, maximum)
    while True:
        view = bounded(context, limit=allowance)
        messages = render(view, len(units))
        if fits(messages):
            return messages
        if allowance == 512:
            break
        allowance = max(512, allowance // 2)

    if not fits(render(view, 0)) and tail:
        omitted_tail = {
            "call_ids": sorted(native_ids), "bytes": len(json_bytes(tail)),
            "sha256": canonical_hash(tail),
            "instruction": "The original tool interaction did not fit and is NOT supplied. Do not infer its contents; use a narrower authorized read or qualify the returned solution.",
        }
        if archive_tail:
            omitted_tail["details_ref"] = archive_tail(original_tail)
        LOGGER.warning("Latest tool interaction does not fit %s input bytes; explicitly retaining an omission receipt.", maximum)
        tail = []
    if not fits(render(view, 0)):
        raise ContextCapacityError("The input limit cannot hold the instructions, task contract and tool-result delivery receipt.")
    low, high = 0, len(units)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(render(view, middle)):
            low = middle
        else:
            high = middle - 1
    return render(view, low)


def request_manifest(config, messages, tools, *, tool_choice=None):
    results = [{"call_id": message["call_id"], "sha256": canonical_hash(json.loads(message["output"]))}
               for message in messages if message.get("type") == "function_call_output"]
    sources = []
    delivery = None
    for message in messages[1:2]:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str) or not content.startswith("{"):
            continue
        payload = json.loads(content)
        if not isinstance(payload, dict) or "context_delivery" not in payload:
            continue
        delivery = payload["context_delivery"]
        memory = payload.get("working_set") or {}
        for item in memory.get("raw_reads", []):
            sources.append({"kind": "raw", "pointer": item["pointer"], "node_hash": item["node_hash"],
                            "page_hash": canonical_hash(item["page"]), "offset": item["page"].get("offset", 0)})
        for item in memory.get("nodes", []):
            sources.append({"kind": "node", "pointer": item["pointer"], "node_hash": canonical_hash(item["data"])})
        focus = payload.get("invalid_draft_focus") or {}
        if "value" in focus:
            sources.append({"kind": "focus", "pointer": focus["pointer"], "node_hash": canonical_hash(focus["value"])})
        if "implementation_state" in payload:
            sources.append({"kind": "implementation_state", "pointer": "/implementation_state",
                            "sha256": canonical_hash(payload["implementation_state"])})
        for item in payload.get("recent_tool_results", []):
            if item.get("call_id") and "result" in item:
                results.append({"call_id": item["call_id"], "sha256": canonical_hash(item["result"])})
    body = request_body(config, messages, tools, tool_choice=tool_choice)
    return {"schema": "bridge-context-delivery/v1", "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(), "tool_results": results,
            "source_bodies": sources, "context_delivery": delivery}
