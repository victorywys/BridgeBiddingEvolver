"""Versioned implementation working sets retained across model-context compaction."""

import copy

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evolution.feedback import json_bytes
from bridge_evolve.evolution.research_tools import paged_value
from bridge_evolve.systems.language import SystemError
from bridge_evolve.systems.navigation import at_pointer, system_index

READ_TOOLS = {"navigate_system", "read_system", "read_workspace", "read_result", "read_reference", "read_evidence", "read_contract"}


def read_arguments(arguments):
    return {"snapshot": arguments.get("snapshot", "working"), "pointer": arguments.get("pointer", ""),
            "offset": arguments.get("offset", 0), "limit": arguments.get("limit", 20)}


def raw_read(system, arguments, result, *, maximum_bytes=12000):
    if not result.get("ok") or arguments.get("snapshot", "working") != "working":
        return None
    pointer = arguments.get("pointer", "")
    try:
        value = at_pointer(system, pointer)
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    page = paged_value(value, arguments.get("offset", 0), arguments.get("limit", 20), maximum_bytes=maximum_bytes)
    observed = {key: value for key, value in result.items() if key not in ("ok", "pointer", "workspace_hash")}
    if page.get("needs_narrower_pointer") or canonical_hash(page) != canonical_hash(observed):
        return None
    return {"pointer": pointer, "node_hash": canonical_hash(value), "arguments": read_arguments(arguments),
            "page": copy.deepcopy(page)}


def raw_page_matches(current, saved):
    if canonical_hash(current) != saved["node_hash"]:
        return False
    if isinstance(current, dict):
        page = saved["page"]
        value = page["value"]
        offset = page.get("offset", 0)
        return isinstance(value, dict) and list(value) == list(current)[offset:offset + len(value)]
    return True


def cached_read(tools, arguments):
    normalized = read_arguments(arguments)
    if normalized["snapshot"] != "working":
        return None
    saved = tools.data.get("research_memory", {}).get("raw_reads", {}).get(canonical_hash(normalized))
    if saved is None or saved.get("arguments") != normalized:
        return None
    try:
        current = at_pointer(tools.data["system"], saved["pointer"])
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if not raw_page_matches(current, saved):
        return None
    return {"ok": True, "pointer": saved["pointer"], "workspace_hash": canonical_hash(tools.data["system"]),
            **copy.deepcopy(saved["page"])}


def prime_working_set(tools):
    memory = tools.data.setdefault("research_memory", {"nodes": {}, "seen": [], "no_information_streak": 0})
    try:
        index = system_index(tools.system())
    except ValueError as error:
        memory["compilation_error"] = str(error)
        return
    for node in index.nodes.values():
        if node.key not in memory["nodes"]:
            memory["nodes"][node.key] = {"key": node.key, "pointer": node.pointer, "data": copy.deepcopy(node.data),
                                         "semantic_hash": index.fingerprint(node.key)}
    while len(memory["nodes"]) > 512:
        memory["nodes"].pop(next(iter(memory["nodes"])))


def remember(tools, name, arguments, result):
    memory = tools.data.setdefault("research_memory", {"nodes": {}, "seen": [], "no_information_streak": 0})
    if result.get("ok") and name == "navigate_system" and arguments.get("snapshot", "working") == "working":
        index = system_index(tools.system())
        for node in result.get("definitions", {}).get("items", []):
            if node.get("data_kind") == "source_node" and "data" in node:
                memory["nodes"].pop(node["key"], None)
                memory["nodes"][node["key"]] = {"key": node["key"], "pointer": node["pointer"],
                                               "data": copy.deepcopy(node["data"]),
                                               "semantic_hash": index.fingerprint(node["key"])}
    if result.get("ok") and name == "read_system" and arguments.get("snapshot", "working") == "working":
        saved = raw_read(tools.data["system"], arguments, result, maximum_bytes=tools.result_limit)
        if saved:
            reads = memory.setdefault("raw_reads", {})
            key = canonical_hash(read_arguments(arguments))
            reads.pop(key, None)
            reads[key] = saved
            while len(reads) > 32:
                reads.pop(next(iter(reads)))
        try:
            index = system_index(tools.system())
        except ValueError as error:
            memory["compilation_error"] = str(error)
            index = None
        if index is not None:
            for node in index.nodes.values():
                if node.pointer == arguments.get("pointer") and len(json_bytes(node.data)) <= 12000:
                    memory["nodes"].pop(node.key, None)
                    memory["nodes"][node.key] = {"key": node.key, "pointer": node.pointer, "data": copy.deepcopy(node.data),
                                                "semantic_hash": index.fingerprint(node.key)}
                    break
    maximum_nodes = 512 if tools.controller.config.get("research_budget_mode") == "campaign" else 32
    while len(memory["nodes"]) > maximum_nodes:
        memory["nodes"].pop(next(iter(memory["nodes"])))
    contents = result
    if name == "read_workspace" and arguments.get("pointer", "") in ("", "/operations"):
        contents = {key: value for key, value in result.items() if key not in ("operations", "notes", "value")}
    signature = canonical_hash({"tool": name, "arguments": read_arguments(arguments) if name == "read_system" else arguments,
                                "result": contents})
    new_information = signature not in memory["seen"]
    if new_information:
        memory["seen"].append(signature)
        memory["seen"] = memory["seen"][-512:]
    if name in READ_TOOLS and not new_information:
        memory["no_information_streak"] += 1
    elif name in READ_TOOLS:
        memory["no_information_streak"] = 0
    elif name in ("edit_system", "write_tests", "add_cases", "generate_cases", "trial", "publish_candidate"):
        memory["no_information_streak"] = 0
    memory["last_tool"] = name


def working_set(tools, maximum):
    memory = tools.data.get("research_memory", {})
    if not memory:
        return None
    result = {"system_document_hash": canonical_hash(tools.data["system"]), "nodes": [], "raw_reads": [],
              "state": tools.data.get("implementation_state"), "invalidated": [],
              "instruction": "Only the source bodies actually included in the request are available; omitted or archived reads are not model memory."}
    for key, saved in reversed(list(memory.get("raw_reads", {}).items())):
        try:
            current = at_pointer(tools.data["system"], saved["pointer"])
        except (ValueError, KeyError, IndexError, TypeError):
            result["invalidated"].append("raw:" + key)
            continue
        if not raw_page_matches(current, saved):
            result["invalidated"].append("raw:" + key)
        elif len(json_bytes({**result, "raw_reads": result["raw_reads"] + [saved]})) <= maximum:
            result["raw_reads"].append(copy.deepcopy(saved))
    try:
        index = system_index(tools.system())
    except ValueError as error:
        result.update(compilation_error=str(error), instruction="The draft does not compile. Use the exact error and raw pointers to repair, not a substituted valid snapshot.")
        return result
    for saved in reversed(list(memory.get("nodes", {}).values())):
        node = index.nodes.get(saved["key"])
        if node is None:
            result["invalidated"].append(saved["key"])
            continue
        current = {"key": node.key, "pointer": node.pointer, "data": node.data, "dependencies": list(node.dependencies),
                   "changed_since_read": (canonical_hash(node.data) != canonical_hash(saved["data"])
                                          or saved.get("semantic_hash", index.fingerprint(node.key)) != index.fingerprint(node.key))}
        if len(json_bytes({**result, "nodes": result["nodes"] + [current]})) <= maximum:
            result["nodes"].append(copy.deepcopy(current))
    result["retained_nodes"] = len(result["nodes"])
    result["known_nodes"] = len(memory.get("nodes", {}))
    included = {node["key"] for node in result["nodes"]}
    result["unloaded_dependencies"] = sorted({key for node in result["nodes"] for key in node["dependencies"]} - included)
    return result
