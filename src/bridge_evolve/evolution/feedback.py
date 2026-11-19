"""Bounded views for untrusted research output; full evidence stays separate."""

import copy
import json


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def short_text(value, limit=1200):
    text = str(value)
    if len(text.encode()) <= limit:
        return text
    return text.encode()[:max(0, limit - 24)].decode("utf8", errors="ignore") + " [details omitted]"


def value_preview(value, limit=240):
    if isinstance(value, dict):
        keys = ", ".join(short_text(key, 40) for key in list(value)[:5])
        return short_text(f"object with {len(value)} fields: {keys}", limit)
    if isinstance(value, (list, tuple)):
        return f"array with {len(value)} items"
    return short_text(value, limit)


def bounded(value, *, limit=12000, reference=None):
    size = len(json_bytes(value))
    if size <= limit:
        return copy.deepcopy(value)
    important = ("ok", "status", "type", "error", "tests", "passed", "failed", "change_witnesses",
                 "system_hash", "hash", "round_id", "boards", "delta", "error_count", "failures")

    def shrink(item, text, count, depth):
        if isinstance(item, str):
            return short_text(item, text)
        if isinstance(item, dict):
            if not depth:
                return {"object_fields": len(item)}
            keys = [key for key in important if key in item]
            keys += [key for key in item if key not in keys]
            selected = keys[:count]
            result = {key: shrink(item[key], text, count, depth - 1) for key in selected}
            if len(selected) < len(item):
                result["omitted_fields"] = len(item) - len(selected)
            return result
        if isinstance(item, (list, tuple)):
            if not depth:
                return {"array_items": len(item)}
            result = [shrink(child, text, count, depth - 1) for child in item[:count]]
            if len(item) > count:
                result.append({"omitted_items": len(item) - count})
            return result
        return item

    for text, count, depth in ((800, 16, 5), (300, 8, 4), (120, 4, 3), (40, 2, 1)):
        view = shrink(value, text, count, depth)
        if not isinstance(view, dict):
            view = {"preview": view}
        view.update(details_omitted=True, full_bytes=size)
        if reference is not None:
            view["details_ref"] = reference
        if len(json_bytes(view)) <= limit:
            return view
    view = {"details_omitted": True, "full_bytes": size, "value_type": type(value).__name__}
    if isinstance(value, dict):
        view.update((key, value[key]) for key in important if key in value and type(value[key]) in (bool, int, float, type(None)))
    if reference is not None:
        view["details_ref"] = reference
    if len(json_bytes(view)) <= limit:
        return view
    raise ValueError("The bounded feedback reference exceeds the configured response limit.")


class AssertionFailure(ValueError):
    def __init__(self, path, operation, actual, expected):
        self.details = {"path": path, "operation": operation, "actual": actual, "expected": expected}
        super().__init__(f"Assertion {path!r} {operation} failed: "
                         f"actual={value_preview(actual)}, expected={value_preview(expected)}")
