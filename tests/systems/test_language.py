import json
import tempfile
import unittest
from pathlib import Path

from bridge_evolve.systems import compile_expression, load_system, render_system, validate_system
from bridge_evolve.systems.language import SystemError, load_json, parse_call
from systems.support import ROOT, raw_system, rule


class LanguageValidationTests(unittest.TestCase):
    def test_immutable_roundtrip_preserves_semantics_and_hash(self):
        system = load_system(ROOT / "systems/1nt-v1.json")
        roundtrip = validate_system(json.loads(json.dumps(system.to_data())))
        self.assertEqual(system, roundtrip)
        self.assertEqual(system.content_hash, roundtrip.content_hash)
        self.assertEqual(render_system(system), render_system(roundtrip))
        raw = system.to_data()
        raw["fallback"] = {"call": "7NT"}
        changed = validate_system(raw)
        self.assertNotEqual(system.content_hash, changed.content_hash)
        self.assertEqual(str(system.fallback), "P")

    def test_unknown_fields_operators_and_versions_fail(self):
        for mutation in (
            {"secret_weights": []}, {"schema_version": True}, {"schema_version": 2},
            {"operator_version": 2}, {"scope": "made-up"},
        ):
            with self.subTest(mutation=mutation):
                raw = raw_system() | mutation
                with self.assertRaises(SystemError):
                    validate_system(raw)
        for expression in ({"op": "eval", "args": []}, {"op": "hcp", "file": "/tmp/private"}):
            with self.assertRaises(SystemError):
                compile_expression(expression)

    def test_types_arity_and_bounds_are_strict(self):
        for expression in (
            {"op": "and", "args": []},
            {"op": "not", "args": [1]},
            {"op": "eq", "args": [True, 1]},
            {"op": "lt", "args": ["8", 9]},
            {"op": "add", "args": [1]},
            {"op": "mul", "args": [True, 2]},
            {"op": "in", "args": [1, ["1", "2"]]},
            {"op": "between", "args": [1, 3, 2]},
            {"op": "control", "suit": "S", "round": 1},
            {"op": "control", "suit": "S", "round": True, "shortage": False},
            {"op": "control", "suit": "S", "round": 2, "shortage": "yes"},
            {"op": "honors", "suit": "S", "ranks": "AA"},
            {"op": "length", "suit": "NT"},
            {"op": "has", "card": "10S"},
            [], [1, True], 1.5, None,
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(SystemError):
                    compile_expression(expression)

    def test_feature_forward_references_are_typed_and_cycles_are_rejected(self):
        raw = raw_system()
        raw["features"] = {
            "twice": {"op": "mul", "args": [{"op": "ref", "name": "points"}, 2]},
            "points": {"op": "hcp"},
        }
        self.assertEqual(dict(validate_system(raw).features)["twice"].kind, "number")
        for features in (
            {"a": {"op": "ref", "name": "missing"}},
            {"a": {"op": "ref", "name": "a"}},
            {"a": {"op": "ref", "name": "b"}, "b": {"op": "ref", "name": "a"}},
        ):
            with self.subTest(features=features):
                with self.assertRaises(SystemError):
                    validate_system(raw | {"features": features})

    def test_protocol_targets_and_reachability_are_checked(self):
        raw = raw_system()
        for protocol in (
            {"initial": "missing", "states": {"start": {"on": {}, "otherwise": "start"}}},
            {"initial": "start", "states": {"start": {"on": {"1C": "missing"}, "otherwise": "start"}}},
            {"initial": "start", "states": {
                "start": {"on": {}, "otherwise": "start"},
                "unreachable": {"on": {}, "otherwise": "unreachable"},
            }},
        ):
            with self.assertRaises(SystemError):
                validate_system(raw | {"protocol": protocol})

    def test_static_rule_errors_fail_before_execution(self):
        for rules in (
            [rule("x", 15)],
            [rule("x", True, priority=True)],
            [rule("x", True), rule("x", False)],
            [rule("x", True), rule("y", True, call="1D")],
            [rule("fallback", True)],
            [rule("x", {"op": "ref", "name": "undefined"})],
            [rule("x", {"op": "eq", "args": [{"op": "state"}, "undefined"]})],
        ):
            with self.subTest(rules=rules):
                with self.assertRaises(SystemError):
                    validate_system(raw_system() | {"rules": rules})

    def test_json_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system.json"
            for content in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
                path.write_text(content)
                with self.assertRaises(SystemError):
                    load_json(path)

    def test_all_canonical_calls_roundtrip_and_noncanonical_input_fails(self):
        for call in ("P", "X", "XX", "1C", "1NT", "7NT"):
            self.assertEqual(str(parse_call(call)), call)
        for call in ("PASS", "1N", "0C", "8NT", "1nt", 1):
            with self.assertRaises(SystemError):
                parse_call(call)


if __name__ == "__main__":
    unittest.main()
