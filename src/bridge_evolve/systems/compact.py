"""Lossless expression sharing in the existing rule IR."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from bridge_evolve.systems.language import SystemDefinition, SystemError, validate_system

SHARED_PREFIX = "shared."


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def compact_system(system: SystemDefinition) -> SystemDefinition:
    raw = system.to_data()
    original_features = raw["features"]
    if not isinstance(original_features, dict):
        raise SystemError("Features must be an object.")
    identities: dict[tuple[object, ...], int] = {}
    sizes: list[int] = []
    representatives: list[object] = []
    objects: dict[int, int] = {}
    expressions: dict[int, int] = {}
    counts: Counter[int] = Counter()

    def visit(value: object) -> int:
        if isinstance(value, dict):
            children = tuple((key, visit(child)) for key, child in value.items())
            signature = ("object", tuple(sorted(children)))
            size = 2 + sum(len(_json(key).encode()) + 1 + sizes[child] for key, child in children)
            size += max(0, len(children) - 1)
        elif isinstance(value, list):
            items = tuple(visit(child) for child in value)
            signature = ("array", items)
            size = 2 + sum(sizes[child] for child in items) + max(0, len(items) - 1)
        else:
            signature = (type(value).__name__, value)
            size = len(_json(value).encode())
        identifier = identities.get(signature)
        if identifier is None:
            identifier = len(sizes)
            identities[signature] = identifier
            sizes.append(size)
            representatives.append(value)
        if isinstance(value, dict):
            objects[id(value)] = identifier
        return identifier

    visit(raw)

    def mark(value: object) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("op"), str):
                identifier = objects[id(value)]
                expressions[id(value)] = identifier
                counts[identifier] += 1
            for child in value.values():
                mark(child)
        elif isinstance(value, list):
            for child in value:
                mark(child)

    for expression in original_features.values():
        mark(expression)
    for rule in raw["rules"]:
        mark(rule["when"])
        if "action" in rule:
            mark(rule["action"])
    mark(raw["fallback"])
    if system.version == 2:
        for state in raw["protocol"]["states"].values():
            for transition in state["transitions"]:
                mark(transition["when"])
                for expression in transition["set"].values():
                    mark(expression)
        mark(raw["announcements"])
    candidates = {identifier for identifier, count in counts.items() if count > 1 and sizes[identifier] >= 128}
    definitions: dict[str, object] = {}
    names: dict[int, str] = {}

    def share(value: object, *, definition: bool = False) -> object:
        if isinstance(value, dict):
            identifier = expressions.get(id(value))
            if not definition and identifier in candidates:
                if identifier in names:
                    return {"op": "ref", "name": names[identifier]}
                expression = representatives[identifier]
                if not isinstance(expression, dict):
                    raise SystemError("A shared expression must be an object.")
                digest = hashlib.sha256(
                    json.dumps(expression, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
                ).hexdigest()[:12]
                name = f"{SHARED_PREFIX}{expression['op']}.{digest}"
                if name in original_features:
                    raise SystemError(f"Shared feature name conflicts with an existing feature: {name}")
                replacement = share(expression, definition=True)
                if name in definitions and definitions[name] != replacement:
                    raise SystemError(f"Shared expression hash collision: {name}")
                definitions[name] = replacement
                names[identifier] = name
                return {"op": "ref", "name": name}
            return {key: share(child) for key, child in value.items()}
        if isinstance(value, list):
            return [share(child) for child in value]
        return value

    shared = {key: share(value) for key, value in raw.items()}
    features = shared["features"]
    if not isinstance(features, dict):
        raise SystemError("Shared features must be an object.")
    features.update(definitions)
    expanded: dict[str, object] = {}

    def expand(value: object) -> object:
        if isinstance(value, dict):
            name = value.get("name")
            if value.get("op") == "ref" and isinstance(name, str) and name in definitions:
                if name not in expanded:
                    expanded[name] = expand(definitions[name])
                return expanded[name]
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value

    restored = expand({
        **shared, "features": {name: value for name, value in features.items() if name not in definitions},
    })
    if restored != raw:
        raise SystemError("Expression sharing changed the expanded system definition.")
    return validate_system(shared)


def format_system(system: SystemDefinition) -> str:
    def render(value: object, depth: int) -> str:
        indent = "  " * depth
        if isinstance(value, dict):
            if not value:
                return "{}"
            if "op" in value or "kind" in value:
                inline = _json(value)
                if len(indent) + len(inline) <= 112:
                    return inline
            children = [f"{indent}  {_json(key)}: {render(child, depth + 1)}" for key, child in value.items()]
            return "{\n" + ",\n".join(children) + "\n" + indent + "}"
        if isinstance(value, list):
            if not value:
                return "[]"
            if all(not isinstance(child, (dict, list)) for child in value):
                inline = _json(value)
                if len(indent) + len(inline) <= 112:
                    return inline
            children = [f"{indent}  {render(child, depth + 1)}" for child in value]
            return "[\n" + ",\n".join(children) + "\n" + indent + "]"
        return _json(value)

    return render(system.to_data(), 0) + "\n"
