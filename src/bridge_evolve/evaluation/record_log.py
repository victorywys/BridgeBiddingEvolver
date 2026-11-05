"""Lossless, streaming JSON records with shared dictionaries and public-state deltas."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, TextIO

SCHEMA = "bridge-record-log/v2"
SINGLE_SCHEMA = "bridge-record/v2"
HEADER = {"schema": SCHEMA, "row": ["keys+", "strings+", "expressions+", "layouts+", "record"],
          "nodes": ["object", "array", "string-ref", "public-delta", "expression-ref"]}
CONTEXT_FIELDS = frozenset(("public_context", "public_context_after"))
INDEX_SKIP = frozenset((*CONTEXT_FIELDS, "partnership_contexts"))


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def same_value(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_value(value, right[key]) for key, value in left.items())
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(same_value(a, b) for a, b in zip(left, right, strict=True))
    return left == right


class RecordEncoder:
    def __init__(self) -> None:
        self.keys: dict[str, int] = {}
        self.strings: dict[str, int] = {}
        self.expressions: dict[str, int] = {}
        self.layouts: dict[tuple[int, ...], int] = {}

    def encode(self, record: dict) -> list:
        if not isinstance(record, dict):
            raise ValueError("A record log row must be an object.")
        additions: list[list] = [[], [], [], []]
        contexts: dict[int, dict] = {}

        def intern(value: str, pool: dict[str, int], column: int, literal=None) -> int:
            if value not in pool:
                pool[value] = len(pool)
                additions[column].append(value if literal is None else copy.deepcopy(literal))
            return pool[value]

        def key_id(key: str) -> int:
            if not isinstance(key, str):
                raise ValueError("Record object keys must be strings.")
            return intern(key, self.keys, 0)

        def pack(value, channel=0, field="", plain_strings=False):
            if value is None or type(value) in (bool, int):
                return value
            if type(value) is float:
                if not math.isfinite(value):
                    raise ValueError("Record numbers must be finite.")
                return value
            if isinstance(value, str):
                if len(value) < 8 or plain_strings or field in ("id", "family_id", "deal_hash"):
                    return value
                return [2, intern(value, self.strings, 1)]
            if isinstance(value, (list, tuple)):
                return [1, *[pack(item, 2 * index if field == "tables" else channel, field, plain_strings)
                             for index, item in enumerate(value)]]
            if not isinstance(value, dict):
                raise ValueError(f"Unsupported record value: {type(value).__name__}.")
            if field == "expression":
                identity = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
                return [4, intern(identity, self.expressions, 2, value)]
            if field in CONTEXT_FIELDS:
                previous = contexts.get(channel, {})
                changed = {}
                for key, item in value.items():
                    if key not in previous or not same_value(item, previous[key]):
                        changed[key] = item
                removed = [key_id(key) for key in previous if key not in value]
                packed = pack(changed, channel, "delta")
                contexts[channel] = value
                return [3, channel, packed, removed]
            if value.get("seat") in ("N", "E", "S", "W"):
                channel = (channel // 2) * 2 + (0 if value["seat"] in "NS" else 1)
            layout = tuple(key_id(key) for key in value)
            if layout not in self.layouts:
                self.layouts[layout] = len(self.layouts)
                additions[3].append(list(layout))
            output = [0, self.layouts[layout]]
            for key, item in value.items():
                output.append(pack(item, channel, key, plain_strings or key == "hands"))
            return output

        packed = pack(record)
        return [*additions, packed]


class RecordDecoder:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.strings: list[str] = []
        self.expressions: list[dict] = []
        self.layouts: list[tuple[str, ...]] = []
        self._key_set: set[str] = set()
        self._string_set: set[str] = set()
        self._expression_set: set[str] = set()
        self._layout_set: set[tuple[str, ...]] = set()

    @staticmethod
    def _reference(pool: list, index: object):
        if type(index) is not int or not 0 <= index < len(pool):
            raise ValueError("Invalid compact-record dictionary reference.")
        return pool[index]

    def decode(self, row: object, *, apply_updates=True, projection="full") -> dict:
        if projection not in ("full", "index"):
            raise ValueError("Unsupported record projection.")
        if not isinstance(row, list) or len(row) != 5 or any(not isinstance(value, list) for value in row[:4]):
            raise ValueError("Invalid compact-record row.")
        if apply_updates:
            for additions, pool, known in ((row[0], self.keys, self._key_set), (row[1], self.strings, self._string_set)):
                for value in additions:
                    if not isinstance(value, str) or value in known:
                        raise ValueError("Invalid or repeated compact-record dictionary entry.")
                    known.add(value)
                    pool.append(value)
            for expression in row[2]:
                if not isinstance(expression, dict):
                    raise ValueError("Invalid compact-record expression.")
                identity = json.dumps(expression, sort_keys=True, separators=(",", ":"), allow_nan=False)
                if identity in self._expression_set:
                    raise ValueError("Repeated compact-record expression.")
                self._expression_set.add(identity)
                self.expressions.append(expression)
            for layout in row[3]:
                if not isinstance(layout, list):
                    raise ValueError("Invalid compact-record layout.")
                columns = tuple(self._reference(self.keys, index) for index in layout)
                if len(set(columns)) != len(columns) or columns in self._layout_set:
                    raise ValueError("Invalid or repeated compact-record layout.")
                self._layout_set.add(columns)
                self.layouts.append(columns)
        contexts: dict[int, dict] = {}

        def unpack(node, field=""):
            if node is None or type(node) in (bool, int, str):
                return node
            if type(node) is float:
                if not math.isfinite(node):
                    raise ValueError("Record numbers must be finite.")
                return node
            if not isinstance(node, list) or not node or type(node[0]) is not int:
                raise ValueError("Invalid compact-record node.")
            tag = node[0]
            if tag == 1:
                return [unpack(item, field) for item in node[1:]]
            if tag in (2, 4) and len(node) == 2:
                value = self._reference(self.strings if tag == 2 else self.expressions, node[1])
                return copy.deepcopy(value) if tag == 4 else value
            if tag == 0 and len(node) >= 2:
                columns = self._reference(self.layouts, node[1])
                if len(node) - 2 != len(columns):
                    raise ValueError("Compact-record values do not match their layout.")
                output = {}
                for key, item in zip(columns, node[2:], strict=True):
                    if projection == "index" and (key in INDEX_SKIP
                            or field == "private_trace" and key != "rule_id"
                            or field == "public_explanation" and key != "summary"
                            or field == "audit_result" and key not in ("status", "errors")):
                        continue
                    output[key] = unpack(item, key)
                return output
            if (tag == 3 and len(node) == 4 and type(node[1]) is int and node[1] >= 0
                    and isinstance(node[2], list) and isinstance(node[3], list)):
                context = dict(contexts.get(node[1], {}))
                changed = unpack(node[2], "delta")
                if not isinstance(changed, dict):
                    raise ValueError("Invalid public-delta values.")
                context.update(changed)
                for reference in node[3]:
                    key = self._reference(self.keys, reference)
                    if key in changed or key not in context:
                        raise ValueError("Invalid public-delta removal.")
                    del context[key]
                contexts[node[1]] = context
                return copy.deepcopy(context)
            raise ValueError("Invalid compact-record tag or shape.")

        result = unpack(row[4])
        if not isinstance(result, dict):
            raise ValueError("A decoded record must be an object.")
        return result


@dataclass(frozen=True)
class RecordEntry:
    record: dict
    offset: int
    length: int
    sha256: str


class RecordReader:
    def __init__(self, source: BinaryIO):
        self.source = source
        self.decoder: RecordDecoder | None = None
        self.single = False
        self.first_offset = source.tell()
        self.first = source.readline()
        while self.first and not self.first.strip():
            self.first_offset = source.tell()
            self.first = source.readline()
        if not self.first:
            return
        try:
            value = json.loads(self.first)
        except json.JSONDecodeError:
            source.seek(self.first_offset)
            self.first = source.read()
            value = json.loads(self.first)
        if isinstance(value, dict) and value.get("schema") == SCHEMA:
            if value != HEADER:
                raise ValueError("Unsupported compact-record header.")
            self.decoder = RecordDecoder()
        elif isinstance(value, dict) and value.get("schema") == SINGLE_SCHEMA:
            if set(value) != {"schema", "row"}:
                raise ValueError("Unsupported compact single-record header.")
            self.decoder = RecordDecoder()
            self.single = True
        elif isinstance(value, dict) and str(value.get("schema", "")).startswith(("bridge-record-log/", "bridge-record/")):
            raise ValueError("Unsupported compact-record version.")

    def entries(self, *, projection="full") -> Iterator[RecordEntry]:
        first = self.first if self.decoder is None or self.single else None
        offset = self.first_offset
        while True:
            if first is not None:
                contents, first = first, None
            else:
                offset = self.source.tell()
                contents = self.source.readline()
            if not contents:
                return
            if not contents.strip():
                continue
            value = json.loads(contents)
            row = value["row"] if self.single else value
            record = self.decoder.decode(row, projection=projection) if self.decoder is not None else value
            if not isinstance(record, dict):
                raise ValueError("A record log row must be an object.")
            yield RecordEntry(record, offset, len(contents), hashlib.sha256(contents).hexdigest())
            if self.single:
                if self.source.read().strip():
                    raise ValueError("A compact single-record file contains extra rows.")
                return

    def selected(self, contents: bytes) -> dict:
        value = json.loads(contents)
        row = value["row"] if self.single else value
        result = self.decoder.decode(row, apply_updates=False) if self.decoder is not None else value
        if not isinstance(result, dict):
            raise ValueError("A record log row must be an object.")
        return result


class RecordWriter:
    def __init__(self, path: Path):
        self.path = path
        self.encoder = RecordEncoder()
        self.count = self.expanded_bytes = self.stored_bytes = 0
        self.target: TextIO | None = None

    def __enter__(self):
        self.target = self.path.open("x", encoding="utf8")
        try:
            self._write(json_text(HEADER) + "\n")
        except BaseException:
            self.target.close()
            raise
        return self

    def _write(self, text: str) -> None:
        if self.target is None:
            raise ValueError("Record writer is not open.")
        self.target.write(text)
        self.stored_bytes += len(text.encode("utf8"))

    def write(self, record: dict) -> None:
        self.expanded_bytes += len(json_text(record).encode("utf8")) + 1
        self._write(json_text(self.encoder.encode(record)) + "\n")
        self.count += 1

    def flush(self) -> None:
        if self.target is None:
            raise ValueError("Record writer is not open.")
        self.target.flush()

    def __exit__(self, *_args):
        if self.target is not None:
            self.target.close()

    def metadata(self) -> dict:
        return {"schema": SCHEMA, "file": self.path.name, "records": self.count,
                "stored_bytes": self.stored_bytes, "expanded_bytes": self.expanded_bytes,
                "keys": len(self.encoder.keys), "strings": len(self.encoder.strings),
                "expressions": len(self.encoder.expressions), "layouts": len(self.encoder.layouts)}


def iter_records(path: Path, *, projection="full") -> Iterator[dict]:
    with path.open("rb") as source:
        for entry in RecordReader(source).entries(projection=projection):
            yield entry.record


def encode_records(records: Iterator[dict]) -> bytes:
    encoder = RecordEncoder()
    chunks = [json_text(HEADER) + "\n"]
    chunks.extend(json_text(encoder.encode(record)) + "\n" for record in records)
    return "".join(chunks).encode("utf8")

def write_single_record(path: Path, record: dict) -> dict:
    encoder = RecordEncoder()
    contents = json_text({"schema": SINGLE_SCHEMA, "row": encoder.encode(record)}) + "\n"
    with path.open("x", encoding="utf8") as target:
        target.write(contents)
    return {"schema": SINGLE_SCHEMA, "file": path.name, "records": 1,
            "stored_bytes": len(contents.encode("utf8")), "expanded_bytes": len(json_text(record).encode("utf8")) + 1,
            "keys": len(encoder.keys), "strings": len(encoder.strings),
            "expressions": len(encoder.expressions), "layouts": len(encoder.layouts)}


def decode_records(contents: bytes, *, projection="full") -> list[dict]:
    reader = RecordReader(io.BytesIO(contents))
    return [entry.record for entry in reader.entries(projection=projection)]

def select_record(path: Path, *, index: int | None = None, number: int | None = None,
                  table: int = 1, expand: bool = True) -> dict:
    if (index is None) == (number is None) or index is not None and index < 0 or number is not None and number < 1:
        raise ValueError("Select exactly one nonnegative index or positive board number.")
    with path.open("rb") as source:
        reader = RecordReader(source)
        for offset, entry in enumerate(reader.entries(projection="index")):
            record = entry.record
            matched = (record.get("index", offset) == index if index is not None
                       else record.get("number", record.get("index", offset) + 1) == number)
            if not matched or record.get("table", f"table{table}") != f"table{table}":
                continue
            if not expand:
                return record
            source.seek(entry.offset)
            return reader.selected(source.read(entry.length))
    raise ValueError("Selected board was not found.")


def agent_board(record: dict) -> dict:
    board = record.get("board", record)
    summary = {"schema": "bridge-agent-board/v1",
               "columns": {"hands": ["N", "E", "S", "W"], "auction": ["seat", "call", "rule"]},
               **{key: record[key] for key in ("number", "index", "family_id", "imp", "point_difference") if key in record},
               **{key: board[key] for key in ("dealer", "vulnerability") if key in board}}
    if "hands" in board:
        summary["hands"] = [board["hands"][seat] for seat in "NESW"]
    if "hand_summary" in record:
        summary["hcp"] = [record["hand_summary"][seat]["hcp"] for seat in "NESW"]
        summary["shape_SHDC"] = [record["hand_summary"][seat]["shape"] for seat in "NESW"]
    tables = record.get("tables", [record] if "auction" in record else [])
    summary["tables"] = [{**{key: table[key] for key in ("id", "table", "systems", "contract", "tricks", "score_ns", "error") if key in table},
        "auction": [[step["seat"], step["call"], step.get("private_trace", {}).get("rule_id")] for step in table.get("auction", [])]}
        for table in tables]
    if record.get("audit_result"):
        summary["audit"] = {key: record["audit_result"][key] for key in ("status", "errors")}
    if record.get("dds"):
        summary["dds_NESW_CDHSNT"] = [[record["dds"]["table"][seat][strain] for strain in ("C", "D", "H", "S", "NT")] for seat in "NESW"]
        summary["par"] = record["dds"]["par"]
    return summary


def agent_decision(record: dict, table_number: int, turn: int) -> dict:
    tables = record.get("tables", [record] if "auction" in record else [])
    if "tables" not in record and tables and record.get("table", "table1") == f"table{table_number}":
        table = record
    elif 1 <= table_number <= len(tables):
        table = tables[table_number - 1]
    else:
        raise ValueError("Selected table does not exist.")
    steps = table.get("auction", [])
    if not 1 <= turn <= len(steps):
        raise ValueError("Selected auction turn does not exist.")
    board = record.get("board", record)
    step = steps[turn - 1]
    return {"schema": "bridge-agent-decision/v1", "number": record.get("number", record.get("index", 0) + 1),
            "dealer": board.get("dealer"), "vulnerability": board.get("vulnerability"),
            "table": table.get("id", table.get("table", f"table{table_number}")), "systems": table.get("systems", {}),
            "hand": board.get("hands", {}).get(step["seat"]), "turn": turn,
            "prefix": [[item["seat"], item["call"]] for item in steps[:turn - 1]], "decision": copy.deepcopy(step)}


def record_statistics(records: Iterator[dict], *, top: int = 20) -> dict:
    if type(top) is not int or not 0 <= top <= 10000:
        raise ValueError("Rule/error group limit must be between zero and 10000.")
    count = tables = decisions = failures = unassessed = 0
    rules, contracts, errors = Counter(), Counter(), Counter()
    for record in records:
        count += 1
        unassessed += "audit_result" not in record
        played = record.get("tables", [record] if "auction" in record else [])
        tables += len(played)
        for table in played:
            for step in table.get("auction", []):
                decisions += 1
                rules[step.get("private_trace", {}).get("rule_id", "unrecorded")] += 1
            contract = table.get("contract")
            if contract is not None:
                contracts["Pass" if contract.get("passed_out") else f"{contract['level']}{contract['strain']}"] += 1
        found = record.get("audit_result", {}).get("errors", [])
        failures += bool(found)
        for error in found:
            errors[f"{error.get('stage', 'unknown')}: {error['message']}"] += 1
    rule_groups, error_groups = rules.most_common(), errors.most_common()
    return {"schema": "bridge-agent-statistics/v1", "records": count, "tables": tables, "decisions": decisions,
            "failed_records": failures, "status_unrecorded_records": unassessed, "contracts": dict(contracts.most_common()),
            "rules": dict(rule_groups[:top] if top else rule_groups),
            "errors": dict(error_groups[:top] if top else error_groups),
            "other_rules_hits": sum(count for _, count in rule_groups[top:]) if top else 0,
            "other_errors_hits": sum(count for _, count in error_groups[top:]) if top else 0}
