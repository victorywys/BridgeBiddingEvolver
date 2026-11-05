"""Versioned, typed rule IR. No user-supplied Python is executed."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TypeAlias

from bridge_evolve.core import Call, Strain

Value: TypeAlias = bool | int | str | tuple["Value", ...]


class SystemError(ValueError):
    """An invalid specification, observation, or protocol execution."""

    def __init__(self, message: str, *, actor: str | None = None, system_hash: str | None = None):
        super().__init__(message)
        self.actor = actor
        self.system_hash = system_hash


def object_fields(value: object, required: set[str], optional: set[str] | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SystemError("Expected an object with string keys.")
    fields: dict[str, object] = dict(value)
    missing = required - fields.keys()
    extra = fields.keys() - required - (optional or set())
    if missing or extra:
        raise SystemError(f"Object fields: missing={sorted(missing)}, unknown={sorted(extra)}")
    return fields


def text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemError("Expected a nonempty string.")
    return value


def integer(value: object) -> int:
    if type(value) is not int:
        raise SystemError("Expected an integer, not a boolean or float.")
    return value


def parse_call(value: object) -> Call:
    name = text(value)
    special = {"P": Call.pass_call, "X": Call.double, "XX": Call.redouble}
    if name in special:
        return special[name]()
    if not re.fullmatch(r"[1-7](C|D|H|S|NT)", name):
        raise SystemError(f"Invalid call: {name!r}; use P, X, XX, or 1C..7NT.")
    return Call.bid(int(name[0]), Strain(name[1:]))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SystemError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: str | Path) -> object:
    def reject_constant(value: str) -> None:
        raise SystemError(f"Non-finite JSON constant: {value}")

    return json.loads(
        Path(path).read_text(encoding="utf8"),
        object_pairs_hook=_unique_object,
        parse_constant=reject_constant,
    )


def _literal(value: object) -> tuple[Value, str]:
    if type(value) is bool:
        return value, "bool"
    if type(value) is int:
        return value, "number"
    if isinstance(value, str):
        return value, "string"
    if isinstance(value, list) and value:
        children = [_literal(item) for item in value]
        types = {kind for _, kind in children}
        if len(types) != 1:
            raise SystemError("Literal lists must be nonempty and homogeneous.")
        return tuple(item for item, _ in children), f"list[{children[0][1]}]"
    raise SystemError("Literals must be booleans, integers, strings, or nonempty homogeneous lists.")


@dataclass(frozen=True)
class Expression:
    op: str
    kind: str
    args: tuple[Expression, ...] = ()
    parameters: tuple[tuple[str, Value], ...] = ()
    literal: Value = False
    bindings: tuple[tuple[str, Expression], ...] = ()

    @cached_property
    def _structural_hash(self) -> int:
        return hash((self.op, self.kind, self.args, self.parameters, self.literal, self.bindings))

    def __hash__(self) -> int:
        return self._structural_hash

    def to_data(self) -> object:
        if self.op == "literal":
            return thaw(self.literal)
        result: dict[str, object] = {"op": self.op}
        result.update((key, thaw(value)) for key, value in self.parameters)
        result.update((key, value.to_data()) for key, value in self.bindings)
        if self.op not in ("ref",) and self.args:
            result["args"] = [argument.to_data() for argument in self.args]
        return result


def thaw(value: Value) -> object:
    return [thaw(item) for item in value] if isinstance(value, tuple) else value


def _check_state_literals(expression: Expression, names: set[str]) -> None:
    def is_state(node: Expression) -> bool:
        return node.op == "state" or (
            node.op == "context" and dict(node.parameters).get("name") == "state"
        )

    candidates: list[Value] = []
    if expression.op in ("eq", "ne"):
        left, right = expression.args
        if is_state(left) and right.op == "literal":
            candidates.append(right.literal)
        if is_state(right) and left.op == "literal":
            candidates.append(left.literal)
    if expression.op == "in":
        left, right = expression.args
        if is_state(left) and right.op == "literal" and isinstance(right.literal, tuple):
            candidates.extend(right.literal)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate not in names:
            raise SystemError(f"Condition references undefined protocol state: {candidate}")
    for child in expression.args:
        _check_state_literals(child, names)
    for _, child in expression.bindings:
        _check_state_literals(child, names)


PRIMITIVES = {
    "hcp": ("number", "大牌点（A=4、K=3、Q=2、J=1）"),
    "controls": ("number", "控制点（A=2、K=1）"),
    "shape": ("list[number]", "四套张数降序排列"),
    "balanced": ("bool", "牌型属于4333、4432或5332"),
    "seat": ("string", "当前座位（N/E/S/W）"),
    "role": ("string", "相对发牌人的角色（dealer/lho/partner/rho）"),
    "state": ("string", "根据全部公开叫牌重建的协议状态"),
    "vulnerable": ("bool", "本方有局"),
    "previously_passed": ("bool", "本人此前曾经Pass"),
    "has_contract_bid": ("bool", "公开历史中已有定约叫品（不含Pass/X/XX）"),
}


def compile_expression(
    value: object, resolve: Callable[[str], Expression] | None = None,
    *, version: int = 1, variables: dict[str, str] | None = None,
) -> Expression:
    if not isinstance(value, dict):
        literal, kind = _literal(value)
        return Expression("literal", kind, literal=literal)
    op = text(value.get("op"))
    if version == 2:
        from bridge_evolve.systems.v2_language import compile_extension
        extension = compile_extension(value, resolve, variables or {})
        if extension is not None:
            return extension
    if op in PRIMITIVES:
        object_fields(value, {"op"})
        return Expression(op, PRIMITIVES[op][0])
    if op == "ref":
        fields = object_fields(value, {"op", "name"})
        name = text(fields["name"])
        if resolve is None:
            raise SystemError(f"Feature reference is not defined: {name}")
        expression = resolve(name)
        return Expression(op, expression.kind, (expression,), (("name", name),))
    if op in ("length", "honors", "control", "has", "is_legal"):
        required = {
            "length": {"suit"}, "honors": {"suit", "ranks"},
            "control": {"suit", "round", "shortage"}, "has": {"card"}, "is_legal": {"call"},
        }[op]
        fields = object_fields(value, {"op"} | required)
        parameters: list[tuple[str, Value]] = []
        if "suit" in required:
            suit = text(fields["suit"])
            if suit not in ("C", "D", "H", "S"):
                raise SystemError("A hand feature suit must be C, D, H, or S.")
            parameters.append(("suit", suit))
        if op == "honors":
            ranks = text(fields["ranks"])
            if len(set(ranks)) != len(ranks) or not set(ranks) <= set("AKQJT98765432"):
                raise SystemError("honors.ranks must contain distinct ranks, using T for ten.")
            parameters.append(("ranks", ranks))
        if op == "control":
            round_number = integer(fields["round"])
            if round_number not in (1, 2) or type(fields["shortage"]) is not bool:
                raise SystemError("control requires round=1/2 and an explicit boolean shortage.")
            parameters.extend((("round", round_number), ("shortage", fields["shortage"])))
        if op == "has":
            card = text(fields["card"])
            if not re.fullmatch(r"[AKQJT98765432][CDHS]", card):
                raise SystemError("has.card must be rank+suit, for example AS or TD.")
            parameters.append(("card", card))
        if op == "is_legal":
            parameters.append(("call", str(parse_call(fields["call"]))))
        kind = "number" if op in ("length", "honors") else "bool"
        return Expression(op, kind, parameters=tuple(parameters))
    fields = object_fields(value, {"op", "args"})
    raw = fields["args"]
    if not isinstance(raw, list):
        raise SystemError(f"{op}.args must be a list.")
    args = tuple(compile_expression(item, resolve, version=version, variables=variables) for item in raw)
    kinds = [argument.kind for argument in args]
    result = "bool"
    if op in ("and", "or"):
        valid = len(args) >= 1 and all(kind == "bool" for kind in kinds)
    elif op == "not":
        valid = kinds == ["bool"]
    elif op in ("eq", "ne"):
        valid = len(args) == 2 and kinds[0] == kinds[1]
    elif op in ("lt", "le", "gt", "ge"):
        valid = kinds == ["number", "number"]
    elif op == "between":
        valid = kinds == ["number", "number", "number"]
        if valid and args[1].op == args[2].op == "literal":
            lower, upper = args[1].literal, args[2].literal
            if isinstance(lower, int) and isinstance(upper, int) and lower > upper:
                raise SystemError("between lower bound cannot exceed its upper bound.")
    elif op == "in":
        valid = len(args) == 2 and kinds[1] == f"list[{kinds[0]}]"
    elif op in ("add", "mul", "min", "max"):
        result = "number"
        valid = len(args) >= 2 and all(kind == "number" for kind in kinds)
    elif op == "sub":
        result = "number"
        valid = kinds == ["number", "number"]
    else:
        raise SystemError(f"Unknown operator: {op}")
    if not valid:
        raise SystemError(f"Invalid argument types/arity for {op}: {kinds}")
    return Expression(op, result, args)


@dataclass(frozen=True)
class ProtocolState:
    name: str
    on: tuple[tuple[str, str], ...]
    otherwise: str
    transitions: tuple[Transition, ...] = ()


@dataclass(frozen=True)
class Parameter:
    name: str
    kind: str
    initial: Value
    domain: tuple[Value, ...] = ()


@dataclass(frozen=True)
class Action:
    kind: str
    call: Call | None = None
    expressions: tuple[tuple[str, Expression], ...] = ()
    message: str = ""

    def to_data(self) -> dict[str, object]:
        result: dict[str, object] = {"kind": self.kind}
        if self.call is not None:
            result["call"] = str(self.call)
        if self.message:
            result["message"] = self.message
        result.update((name, expression.to_data()) for name, expression in self.expressions)
        return result


@dataclass(frozen=True)
class Transition:
    id: str
    when: Expression
    target: str
    assignments: tuple[tuple[str, Expression], ...] = ()
    forcing: str | None = None
    allow_pass: bool = False

    def to_data(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.id, "when": self.when.to_data(), "to": self.target,
            "set": {name: expression.to_data() for name, expression in self.assignments},
            "allow_pass": self.allow_pass,
        }
        if self.forcing is not None:
            result["forcing"] = self.forcing
        return result


@dataclass(frozen=True)
class Announcement:
    id: str
    when: Expression
    tags: tuple[str, ...]
    suit: Expression | None = None

    def to_data(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.id, "when": self.when.to_data(), "tags": list(self.tags),
        }
        if self.suit is not None:
            result["suit"] = self.suit.to_data()
        return result


@dataclass(frozen=True)
class Rule:
    id: str
    priority: int
    when: Expression
    call: Call | Action


@dataclass(frozen=True)
class SystemDefinition:
    name: str
    scope: str
    features: tuple[tuple[str, Expression], ...]
    initial: str
    states: tuple[ProtocolState, ...]
    rules: tuple[Rule, ...]
    fallback: Call | Action
    version: int = 1
    parameters: tuple[Parameter, ...] = ()
    announcements: tuple[Announcement, ...] = ()

    def to_data(self) -> dict[str, object]:
        if self.version == 2:
            from bridge_evolve.systems.v2_language import system_data
            return system_data(self)
        return {
            "schema_version": 1, "operator_version": 1, "name": self.name, "scope": self.scope,
            "features": {name: expression.to_data() for name, expression in self.features},
            "protocol": {
                "initial": self.initial,
                "states": {
                    state.name: {"on": dict(state.on), "otherwise": state.otherwise}
                    for state in self.states
                },
            },
            "rules": [
                {"id": rule.id, "priority": rule.priority, "when": rule.when.to_data(), "call": str(rule.call)}
                for rule in self.rules
            ],
            "fallback": {"call": str(self.fallback)},
        }

    @cached_property
    def content_hash(self) -> str:
        canonical = json.dumps(self.to_data(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def __hash__(self) -> int:
        return hash(self.content_hash)

    def state_after(self, history: tuple[Call, ...]) -> str:
        if self.version != 1:
            raise SystemError("v2 state requires dealer, vulnerability and both public definitions; use public_context().")
        states = {state.name: state for state in self.states}
        current = self.initial
        for call in history:
            state = states[current]
            current = dict(state.on).get(str(call), state.otherwise)
        return current


def validate_system(value: object) -> SystemDefinition:
    if isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 2:
        from bridge_evolve.systems.v2_language import validate_v2
        return validate_v2(value)
    fields = object_fields(value, {
        "schema_version", "operator_version", "name", "scope", "features", "protocol", "rules", "fallback",
    })
    if integer(fields["schema_version"]) != 1 or integer(fields["operator_version"]) != 1:
        raise SystemError("Only schema_version=1 and operator_version=1 are supported.")
    scope = text(fields["scope"])
    if scope not in ("uncontested-1nt", "general"):
        raise SystemError("scope must be uncontested-1nt or general.")
    raw_features = fields["features"]
    if not isinstance(raw_features, dict) or any(not isinstance(key, str) or not key for key in raw_features):
        raise SystemError("features must map nonempty names to expressions.")
    compiled: dict[str, Expression] = {}
    visiting: set[str] = set()

    def resolve(name: str) -> Expression:
        if name in visiting:
            raise SystemError(f"Cyclic feature reference: {name}")
        if name not in raw_features:
            raise SystemError(f"Undefined feature reference: {name}")
        if name not in compiled:
            visiting.add(name)
            compiled[name] = compile_expression(raw_features[name], resolve)
            visiting.remove(name)
        return compiled[name]

    for name in raw_features:
        resolve(name)
    protocol = object_fields(fields["protocol"], {"initial", "states"})
    initial = text(protocol["initial"])
    raw_states = protocol["states"]
    if not isinstance(raw_states, dict) or not raw_states:
        raise SystemError("protocol.states must be a nonempty object.")
    states: list[ProtocolState] = []
    for name, raw in raw_states.items():
        name = text(name)
        state = object_fields(raw, {"on", "otherwise"})
        if not isinstance(state["on"], dict):
            raise SystemError(f"State {name}.on must map calls to state names.")
        transitions = tuple(
            (str(parse_call(call)), text(target)) for call, target in state["on"].items()
        )
        states.append(ProtocolState(name, transitions, text(state["otherwise"])))
    names = {state.name for state in states}
    if initial not in names:
        raise SystemError(f"Undefined initial protocol state: {initial}")
    for state in states:
        if any(target not in names for _, target in state.on) or state.otherwise not in names:
            raise SystemError(f"Undefined state transition from {state.name}.")
    reachable = {initial}
    while True:
        expanded = reachable | {
            target for state in states if state.name in reachable
            for target in [state.otherwise, *(target for _, target in state.on)]
        }
        if expanded == reachable:
            break
        reachable = expanded
    if reachable != names:
        raise SystemError(f"Unreachable protocol states: {sorted(names - reachable)}")
    for expression in compiled.values():
        _check_state_literals(expression, names)
    raw_rules = fields["rules"]
    if not isinstance(raw_rules, list):
        raise SystemError("rules must be a list.")
    rules: list[Rule] = []
    identifiers: set[str] = set()
    seen: set[tuple[int, Expression]] = set()
    for raw in raw_rules:
        rule = object_fields(raw, {"id", "priority", "when", "call"})
        identifier = text(rule["id"])
        if identifier == "fallback" or identifier in identifiers:
            raise SystemError(f"Duplicate or reserved rule id: {identifier}")
        identifiers.add(identifier)
        condition = compile_expression(rule["when"], resolve)
        if condition.kind != "bool":
            raise SystemError(f"Rule {identifier} condition must be boolean.")
        _check_state_literals(condition, names)
        priority = integer(rule["priority"])
        if (priority, condition) in seen:
            raise SystemError(f"Identical conditions at the same priority: {identifier}")
        seen.add((priority, condition))
        rules.append(Rule(identifier, priority, condition, parse_call(rule["call"])))
    fallback = object_fields(fields["fallback"], {"call"})
    return SystemDefinition(
        text(fields["name"]), scope, tuple((name, resolve(name)) for name in raw_features),
        initial, tuple(states), tuple(rules), parse_call(fallback["call"]),
    )


def load_system(path: str | Path) -> SystemDefinition:
    return validate_system(load_json(path))
