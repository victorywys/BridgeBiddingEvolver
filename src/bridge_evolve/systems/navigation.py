"""Versioned, bounded research views over the unchanged executable system."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from functools import lru_cache

from bridge_evolve.core import Call, Seat, Vulnerability
from bridge_evolve.systems.inputs import parse_observation, parse_prefix, protocol_record
from bridge_evolve.systems.language import (
    Action, Expression, SystemDefinition, SystemError, object_fields, parse_call, thaw,
)
from bridge_evolve.systems.render import render_action, render_expression
from bridge_evolve.systems.runtime import Evaluation, PublicView, decide, replay
from bridge_evolve.systems.v2_language import PRIVATE_OPERATORS
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld

SCHEMA = "bridge-system-navigation/v1"
PAGE_BYTES = 28000
UNKNOWN = object()
NODE_KINDS = {"rule", "transition", "announcement", "feature", "parameter", "state", "fallback"}


def pointer_parts(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or pointer and not pointer.startswith("/"):
        raise ValueError("A JSON pointer must be empty or begin with '/'.")
    parts = pointer.split("/")[1:] if pointer else []
    for part in parts:
        if "~" in part.replace("~0", "").replace("~1", ""):
            raise ValueError("Invalid JSON pointer escape.")
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def at_pointer(value, pointer: str):
    for part in pointer_parts(pointer):
        if isinstance(value, list):
            if not part.isdigit() or str(int(part)) != part:
                raise ValueError("Array pointers require canonical nonnegative indices.")
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError("JSON pointer traverses a scalar.")
    return value


def escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _transition_key(state: str, identifier: str) -> str:
    return "transition:" + escape_pointer(state) + "/" + escape_pointer(identifier)


def _encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _digest(value) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _bounds(offset=0, limit=20):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise SystemError("Navigation requires offset>=0 and limit in [1,100].")
    return offset, limit


def _page(items: list, offset=0, limit=20) -> dict:
    offset, limit = _bounds(offset, limit)
    selected, size = [], 0
    for item in items[offset:offset + limit]:
        length = len(_encoded(item))
        if size + length > PAGE_BYTES:
            if selected:
                break
            item = {key: value for key, value in item.items() if key not in ("data", "description")}
            item["definition_omitted"] = "Use the source pointer or a narrower section of this node."
            length = len(_encoded(item))
            if length > PAGE_BYTES:
                raise SystemError("A navigation descriptor exceeds the response budget.")
        selected.append(item)
        size += length
    end = offset + len(selected)
    return {"total": len(items), "offset": offset, "items": selected,
            "next_offset": end if end < len(items) else None}


def _references(value, operator: str) -> set[str]:
    if isinstance(value, dict):
        found = {value["name"]} if value.get("op") == operator and isinstance(value.get("name"), str) else set()
        for child in value.values():
            found.update(_references(child, operator))
        return found
    if isinstance(value, list):
        return set().union(*(_references(child, operator) for child in value)) if value else set()
    return set()


def _namespace(identifier: str) -> str:
    parts = identifier.split(".")
    return ".".join(parts[:2]) if len(parts) > 2 else parts[0]


def _preview(expression: Expression) -> str:
    remaining = [60]

    def expand(item):
        remaining[0] -= 1
        if remaining[0] <= 0:
            return item
        if item.op == "ref":
            return expand(item.args[0])
        return replace(item, args=tuple(expand(child) for child in item.args),
                       bindings=tuple((key, expand(child)) for key, child in item.bindings))

    text = render_expression(expand(expression), expand_references=False)
    return text if len(text) <= 1600 else text[:1599] + "\u2026"


class PublicFilter:
    """Only proven public falsehood removes a possible private sender."""

    def __init__(self, state: str, evaluation: Evaluation | None = None):
        self.state, self.evaluation = state, evaluation
        self.cache = {}
        self.unavailable = set()

    def value(self, expression: Expression):
        if expression not in self.cache:
            self.cache[expression] = self._value(expression)
        return self.cache[expression]

    def _value(self, expression: Expression):
        op = expression.op
        if op in PRIVATE_OPERATORS:
            return UNKNOWN
        if op == "is_legal" and expression.bindings:
            # A dynamic call may only be defined behind an earlier private guard.
            return UNKNOWN
        if op == "literal":
            return expression.literal
        if op == "ref":
            return self.value(expression.args[0])
        if op == "state" or op == "context" and dict(expression.parameters)["name"] == "state":
            return self.state
        if op in ("and", "or"):
            decisive = False if op == "and" else True
            unknown = False
            for child in expression.args:
                value = self.value(child)
                if value is decisive:
                    return decisive
                unknown |= value is UNKNOWN
            return UNKNOWN if unknown else not decisive
        if op == "if":
            condition = self.value(expression.args[0])
            if condition is UNKNOWN:
                return UNKNOWN
            return self.value(expression.args[1 if condition else 2])
        if self.evaluation is None:
            arguments = [self.value(child) for child in expression.args]
            if not arguments or any(value is UNKNOWN for value in arguments):
                return UNKNOWN
            if op == "not":
                return not arguments[0]
            if op == "eq":
                return arguments[0] == arguments[1]
            if op == "ne":
                return arguments[0] != arguments[1]
            if op == "in":
                return arguments[0] in arguments[1]
            return UNKNOWN
        public = self.evaluation.public
        parameters = dict(expression.parameters)
        if op == "role":
            seat = self.evaluation.observation.dealer
            for role in ("dealer", "lho", "partner", "rho"):
                if seat == self.evaluation.observation.seat:
                    return role
                seat = seat.next()
        if public is not None:
            selector = parameters.get("which") if op == "announced" else {
                "partner_shown_suit": "partner", "opponent_shown_suit": "opponents",
            }.get(parameters.get("name")) if op == "context" else None
            if selector is not None:
                event = public.selected_event(selector)
                if event is not None and not event.known:
                    self.unavailable.add(event.seat.partnership.value)
                    return UNKNOWN
        arguments = [self.value(child) for child in expression.args]
        bindings = [(key, self.value(child), child.kind) for key, child in expression.bindings]
        if any(value is UNKNOWN for value in arguments) or any(value is UNKNOWN for _, value, _ in bindings):
            return UNKNOWN
        literal_args = tuple(Expression("literal", child.kind, literal=value)
                             for child, value in zip(expression.args, arguments, strict=True))
        literal_bindings = tuple((key, Expression("literal", kind, literal=value)) for key, value, kind in bindings)
        return self.evaluation.evaluate(replace(expression, args=literal_args, bindings=literal_bindings))


@dataclass(frozen=True)
class Node:
    key: str
    kind: str
    identifier: str
    pointer: str
    module: str
    data: dict | str | int | bool | list
    dependencies: tuple[str, ...]
    state: str = ""
    priority: int | None = None


class SystemIndex:
    def __init__(self, system: SystemDefinition):
        self.system, self.raw = system, system.to_data()
        self.nodes: dict[str, Node] = {}
        self.expressions: dict[str, Expression] = {}
        self.actions: dict[str, Call | Action] = {}
        self.users = defaultdict(set)
        self._fingerprints = {}
        self._feature_fingerprints = {}
        for name, expression in system.features:
            key = "feature:" + name
            self._add(key, "feature", name, "/features/" + escape_pointer(name), "shared", expression.to_data())
            self.expressions[key] = expression
        for name, value in self.raw["protocol"].get("parameters", {}).items():
            self._add("parameter:" + name, "parameter", name, "/protocol/parameters/" + escape_pointer(name),
                      "parameters", value)
        for state in system.states:
            pointer = "/protocol/states/" + escape_pointer(state.name)
            self._add("state:" + state.name, "state", state.name, pointer, "state:" + state.name,
                      {"otherwise": state.otherwise, "on": dict(state.on),
                       "transitions": [_transition_key(state.name, item.id) for item in state.transitions]},
                      state=state.name)
            for position, transition in enumerate(state.transitions):
                key = _transition_key(state.name, transition.id)
                self._add(key, "transition", transition.id, f"{pointer}/transitions/{position}",
                          "state:" + state.name, transition.to_data(), state=state.name)
                self.expressions[key] = transition.when
        for position, announcement in enumerate(system.announcements):
            key = "announcement:" + announcement.id
            self._add(key, "announcement", announcement.id, f"/announcements/{position}",
                      "announcements", announcement.to_data())
            self.expressions[key] = announcement.when
        for position, rule in enumerate(system.rules):
            key = "rule:" + rule.id
            self._add(key, "rule", rule.id, f"/rules/{position}", "rules:" + _namespace(rule.id),
                      self.raw["rules"][position], priority=rule.priority)
            self.expressions[key], self.actions[key] = rule.when, rule.call
        self._add("fallback", "fallback", "fallback", "/fallback", "fallback", self.raw["fallback"])
        self.actions["fallback"] = system.fallback
        for node in self.nodes.values():
            for dependency in node.dependencies:
                if dependency not in self.nodes:
                    raise SystemError(f"Unresolved navigation dependency: {dependency}")
                self.users[dependency].add(node.key)

    def _add(self, key, kind, identifier, pointer, module, data, *, state="", priority=None):
        if key in self.nodes:
            raise SystemError(f"Duplicate semantic navigation key: {key}")
        dependencies = {"feature:" + name for name in _references(data, "ref")}
        dependencies.update("parameter:" + name for name in _references(data, "var"))
        if kind == "transition":
            dependencies.update("parameter:" + name for name in data["set"])
        self.nodes[key] = Node(key, kind, identifier, pointer, module, data, tuple(sorted(dependencies)), state, priority)

    def _node(self, key: str) -> Node:
        if not isinstance(key, str) or key not in self.nodes:
            raise SystemError(f"Unknown semantic node: {key!r}. Search the current system instead of guessing an array index.")
        return self.nodes[key]

    def descriptor(self, key: str, *, definition=False) -> dict:
        node = self._node(key)
        result = {"key": key, "kind": node.kind, "id": node.identifier, "pointer": node.pointer, "module": node.module}
        if node.state:
            result["state"] = node.state
        if node.priority is not None:
            result["priority"] = node.priority
        if node.kind in ("rule", "transition", "announcement"):
            result["edit_precondition"] = {"op": "test", "path": node.pointer + "/id", "value": node.identifier}
        if key in self.actions:
            result["action"] = render_action(self.actions[key], expand_references=False)
        if definition:
            result.update(data=copy.deepcopy(node.data), dependencies=list(node.dependencies))
            if key in self.expressions:
                result["description"] = _preview(self.expressions[key])
            result["external_users"] = len(self.users[key])
        return result

    def envelope(self, **contents):
        return {"schema": SCHEMA, "system_hash": self.system.content_hash, "name": self.system.name, **contents}

    def catalog(self, offset=0, limit=20):
        modules = defaultdict(Counter)
        for node in self.nodes.values():
            modules[node.module][node.kind] += 1
        entries = [{"id": name, "counts": dict(counts)} for name, counts in sorted(modules.items())]
        return self.envelope(view="catalog", counts=dict(Counter(node.kind for node in self.nodes.values())),
                             initial_state=self.system.initial, modules=_page(entries, offset, limit),
                             states=[state.name for state in self.system.states][:100],
                             states_total=len(self.system.states))

    def search(self, *, query="", kind=None, module=None, state=None, call=None, offset=0, limit=20):
        if not isinstance(query, str) or any(value is not None and not isinstance(value, str)
                                           for value in (kind, module, state, call)):
            raise SystemError("Search filters must be strings.")
        if kind is not None and kind not in NODE_KINDS:
            raise SystemError(f"Unknown navigation node kind: {kind}")
        if state is not None and "state:" + state not in self.nodes:
            raise SystemError(f"Unknown protocol state: {state}")
        if call is not None:
            call = str(parse_call(call))
        public_filter = PublicFilter(state) if state is not None else None
        selected = []
        for node in self.nodes.values():
            if kind is not None and node.kind != kind or module is not None and node.module != module:
                continue
            if state is not None:
                if node.state and node.state != state:
                    continue
                if node.kind == "rule" and public_filter.value(self.expressions[node.key]) is False:
                    continue
                if node.kind not in ("rule", "transition", "state"):
                    continue
            if call is not None and not self._may_emit(node.key, call):
                continue
            if query:
                haystack = (node.identifier + " " + node.module + " " + json.dumps(node.data, ensure_ascii=False)).casefold()
                if not all(word in haystack for word in query.casefold().split()):
                    continue
            selected.append(node.key)
        selected.sort(key=lambda key: (self.nodes[key].kind != "rule", -(self.nodes[key].priority or 0), key))
        return self.envelope(view="search", matches=_page([self.descriptor(key) for key in selected], offset, limit),
                             filter_semantics="Conservative navigation hints, not a proof of private rule selection.")

    def _may_emit(self, key: str, call: str) -> bool:
        action = self.actions.get(key)
        if isinstance(action, Call):
            return str(action) == call
        if isinstance(action, Action):
            if action.kind == "call":
                return str(action.call) == call
            return action.kind != "error" and call not in ("P", "X", "XX")
        data = self.nodes[key].data
        return call in json.dumps(data, ensure_ascii=False)

    def read(self, keys, *, sections=None, dependencies=True, offset=0, limit=20):
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 20
                or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)):
            raise SystemError("Read requires 1-20 distinct semantic node keys.")
        if type(dependencies) is not bool:
            raise SystemError("dependencies must be boolean.")
        roots = [self._node(key).key for key in keys]
        selected = {}
        if sections is not None:
            if not isinstance(sections, list) or not 1 <= len(sections) <= 10:
                raise SystemError("sections requires 1-10 relative JSON pointers.")
            for pointer in sections:
                pointer_parts(pointer)
            for key in roots:
                node = self.nodes[key]
                selected[key] = {pointer: copy.deepcopy(at_pointer(node.data, pointer)) for pointer in sections}
        root_dependencies = {}
        for key in roots:
            if key not in selected:
                root_dependencies[key] = self.nodes[key].dependencies
                continue
            data = selected[key]
            direct = {"feature:" + name for name in _references(data, "ref")}
            direct.update("parameter:" + name for name in _references(data, "var"))
            if self.nodes[key].kind == "transition":
                for pointer in sections:
                    parts = pointer_parts(pointer)
                    if len(parts) > 1 and parts[0] == "set":
                        direct.add("parameter:" + parts[1])
                    elif parts == ["set"]:
                        direct.update("parameter:" + name for name in data[pointer])
            root_dependencies[key] = tuple(sorted(direct))
        closure, pending = set(), list(roots)
        while pending:
            key = pending.pop()
            for dependency in root_dependencies.get(key, self.nodes[key].dependencies):
                if dependency not in closure and dependency not in roots:
                    closure.add(dependency)
                    pending.append(dependency)
        order = roots + (sorted(closure) if dependencies else [])
        definitions = []
        for key in order:
            item = self.descriptor(key, definition=True)
            if key in selected:
                item.update(data=selected[key], sections=sections, dependencies=list(root_dependencies[key]))
                if "/when" not in sections:
                    item.pop("description", None)
            definitions.append(item)
        page = _page(definitions, offset, limit)
        included = {item["key"] for item in page["items"] if "definition_omitted" not in item}
        affected = set().union(*(self.users[key] for key in roots)) - set(roots) - closure
        states = {self.nodes[key].state for key in roots if self.nodes[key].state}
        targets = {self.nodes[key].data["to"] for key in roots if self.nodes[key].kind == "transition"}
        return self.envelope(view="read", roots=roots, definitions=page,
            dependencies={"total": len(closure), "complete_in_response": closure <= included,
                          "scope": "Transitive explicit feature/parameter references of the selected sections."},
            interfaces={"states": [self.descriptor("state:" + state) for state in sorted(states | targets)],
                        "context_fields": sorted(set().union(*(
                            _references(selected.get(key, self.nodes[key].data), "context") for key in order))),
                        "external_users": len(affected),
                        "external_user_sample": sorted(affected)[:12],
                        "priority": "All rules keep their original priorities; a slice is not a standalone executable system."})

    def references(self, key, *, transitive=False, offset=0, limit=20):
        self._node(key)
        if type(transitive) is not bool:
            raise SystemError("transitive must be boolean.")
        found, pending = set(), [key]
        while pending:
            for user in self.users[pending.pop()]:
                if user not in found:
                    found.add(user)
                    if transitive:
                        pending.append(user)
        return self.envelope(view="references", key=key, transitive=transitive,
                             users=_page([self.descriptor(user) for user in sorted(found)], offset, limit))

    def fingerprint(self, key: str) -> str:
        if key not in self._fingerprints:
            def semantic(value):
                if isinstance(value, dict):
                    if value.get("op") == "ref":
                        name = value["name"]
                        if name not in self._feature_fingerprints:
                            self._feature_fingerprints[name] = semantic(self.raw["features"][name])
                        return self._feature_fingerprints[name]
                    return _digest({name: semantic(child) for name, child in value.items()})
                if isinstance(value, list):
                    return _digest([semantic(child) for child in value])
                return _digest([type(value).__name__, value])
            node = self._node(key)
            data = node.data
            if node.kind == "state":
                data = {**data, "transitions": sorted(data["transitions"])}
            self._fingerprints[key] = semantic(data)
        return self._fingerprints[key]

    def diff(self, other: SystemIndex, *, kind=None, module=None, offset=0, limit=20):
        if any(value is not None and not isinstance(value, str) for value in (kind, module)):
            raise SystemError("Diff filters must be strings.")
        if kind is not None and kind not in NODE_KINDS:
            raise SystemError(f"Unknown navigation node kind: {kind}")
        changed = []
        for key in sorted(self.nodes.keys() | other.nodes.keys()):
            node = self.nodes.get(key, other.nodes.get(key))
            if kind is not None and node.kind != kind or module is not None and node.module != module:
                continue
            if kind is None and node.kind == "feature" and node.identifier.startswith("shared."):
                continue
            if key in self.nodes and key in other.nodes and self.fingerprint(key) == other.fingerprint(key):
                continue
            changed.append({"key": key, "change": "added" if key not in other.nodes else
                            "removed" if key not in self.nodes else "changed",
                            "before": other.descriptor(key) if key in other.nodes else None,
                            "after": self.descriptor(key) if key in self.nodes else None})
        return self.envelope(view="diff", compared_to=other.system.content_hash,
                             changes=_page(changed, offset, limit),
                             comparison="Resolved expression semantics, not array positions or generated feature names.")


@lru_cache(maxsize=4)
def system_index(system: SystemDefinition) -> SystemIndex:
    return SystemIndex(system)


def _definitions(system, actor: Seat, definitions: dict | None):
    result = dict(definitions or {})
    side = actor.partnership.value
    if side in result and result[side].content_hash != system.content_hash:
        raise SystemError("The queried system differs from the actor's public partnership definition.")
    result[side] = system
    return result


def _public_snapshot(context: PublicContext) -> dict:
    result = protocol_record(context.snapshot())
    result.update(("var." + name, thaw(value)) for name, value in context.frame.parameters.items())
    result["forcing_target"] = context.frame.target.value if context.frame.target is not None else ""
    result["auction_ended"] = context.world.auction.ended
    return result


def auction_lookup(system: SystemDefinition, request: dict, definitions: dict | None = None):
    fields = object_fields(request, {"view"}, {"dealer", "vulnerability", "history", "call", "turn", "offset", "limit"})
    dealer, vulnerability = Seat(fields.get("dealer", "N")), Vulnerability(fields.get("vulnerability", "none"))
    history = parse_prefix(fields.get("history", []))
    call = fields.get("call")
    if "turn" in fields:
        turn = fields["turn"]
        if call is not None or type(turn) is not int or not 1 <= turn <= len(history):
            raise SystemError("turn is one-based, inside history, and cannot be combined with call.")
        history, call = history[:turn - 1], str(history[turn - 1])
    auction = replay(dealer, history)
    if auction.ended:
        raise SystemError("The queried position is after the auction ended; select a turn in history.")
    actor = auction.next_seat
    world = PublicWorld(_definitions(system, actor, definitions), dealer, vulnerability)
    for previous in history:
        world.step(previous)
    side = actor.partnership.value
    context = PublicContext(world, side, actor)
    state_before = context.frame.state
    before_public = _public_snapshot(context)
    before = dict(context.snapshot())
    evaluation = Evaluation(PublicView(actor, dealer, vulnerability, history), state_before, context)
    public_filter = PublicFilter(state_before, evaluation)
    index = system_index(system)
    possible = [rule for rule in system.rules if public_filter.value(rule.when) is not False]
    possible.sort(key=lambda rule: (-rule.priority, rule.id))
    compatible = [rule for rule in possible if call is None or index._may_emit("rule:" + rule.id, str(parse_call(call)))]
    result = index.envelope(view="auction", dealer=dealer.value, vulnerability=vulnerability.value,
        history=[str(item) for item in history], actor=actor.value, state=state_before,
        public_context=protocol_record(tuple(before.items())),
        compatible_rules=_page([index.descriptor("rule:" + rule.id) for rule in compatible],
                               fields.get("offset", 0), fields.get("limit", 20)),
        possible_rules_total=len(possible), unavailable_definitions=sorted(public_filter.unavailable),
        selection_semantics="Public-compatible senders only; a hand is required to select the actual rule.")
    if call is not None:
        call = parse_call(call)
        prior = copy.copy(world)
        prior.events = list(world.events)
        world.step(call)
        event = world.events[-1]
        event_evaluation = prior.evaluation(side, event)
        state = next(state for state in system.states if state.name == state_before)
        transitions = [item for item in state.transitions if event_evaluation.evaluate(item.when)]
        announcement_evaluation = prior.evaluation(side, replace(event, tags=(), shown_suit=""))
        announcements = [item for item in system.announcements
                         if announcement_evaluation.evaluate(item.when)]
        after = dict(PublicContext(world, side, actor).snapshot())
        changes = {key: {"before": thaw(before[key]), "after": thaw(value)} for key, value in after.items()
                   if key in before and value != before[key]}
        keys = [_transition_key(state.name, item.id) for item in transitions]
        keys += ["announcement:" + item.id for item in announcements]
        result.pop("public_context")
        result["public_meaning"] = {
            "basis": "public-protocol", "call": str(call), "actor": actor.value,
            "state_before": state_before, "state_after": world.frames[side].state,
            "tags": list(event.tags), "shown_suit": event.shown_suit,
            "before": before_public, "after": _public_snapshot(PublicContext(world, side, actor)),
            "changes": changes, "source_keys": keys,
            "updated_parameters": sorted({name for item in transitions for name, _ in item.assignments}),
            "transition_id": transitions[0].id if transitions else "",
        }
        result["suggested_keys"] = keys + ["state:" + world.frames[side].state]
    else:
        result["suggested_keys"] = ["state:" + state_before]
    return result


def hand_lookup(system: SystemDefinition, request: dict, definitions: dict | None = None):
    fields = object_fields(request, {"view", "observation"}, {"offset", "limit"})
    observation = parse_observation(fields["observation"])
    disclosed = dict(observation.public_definitions)
    for side, definition in (definitions or {}).items():
        if side in disclosed and disclosed[side].content_hash != definition.content_hash:
            raise SystemError("Conflicting public partnership definitions.")
        disclosed[side] = definition
    observation = replace(observation, public_definitions=tuple(disclosed.items()))
    decision = decide(system, observation)
    result = auction_lookup(system, {"view": "auction", "dealer": observation.dealer.value,
        "vulnerability": observation.vulnerability.value, "history": [str(call) for call in observation.history],
        "call": str(decision.call), **{key: fields[key] for key in ("offset", "limit") if key in fields}}, disclosed)
    key = "fallback" if decision.trace.rule_id == "fallback" else "rule:" + decision.trace.rule_id
    result.update(view="hand", decision={"call": str(decision.call), "rule_key": key,
                  "state": decision.trace.state, "checked_rules": len(decision.trace.checked_rules)},
                  private=True)
    result["suggested_keys"] = [key] + result["suggested_keys"]
    return result


def query_system(system: SystemDefinition, request: dict, *, compare_to: SystemDefinition | None = None,
                 definitions: dict | None = None) -> dict:
    if not isinstance(request, dict):
        raise SystemError("A system query must be an object.")
    view = request.get("view", "catalog")
    if view == "auction":
        return auction_lookup(system, request, definitions)
    if view == "hand":
        return hand_lookup(system, request, definitions)
    allowed = {
        "catalog": {"offset", "limit"},
        "search": {"query", "kind", "module", "state", "call", "offset", "limit"},
        "read": {"keys", "sections", "dependencies", "offset", "limit"},
        "references": {"key", "transitive", "offset", "limit"},
        "diff": {"kind", "module", "offset", "limit"},
    }
    if view not in allowed:
        raise SystemError(f"Unknown system navigation view: {view!r}")
    fields = object_fields(request, set(), allowed[view] | {"view"})
    fields.pop("view", None)
    index = system_index(system)
    if view == "read" and "keys" not in fields or view == "references" and "key" not in fields:
        raise SystemError(f"{view} requires explicit semantic node keys.")
    if view == "diff":
        if compare_to is None:
            raise SystemError("A semantic diff requires an explicit comparison system.")
        return index.diff(system_index(compare_to), **fields)
    return getattr(index, view)(**fields)
