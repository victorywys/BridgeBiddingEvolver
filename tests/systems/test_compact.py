import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bridge_evolve.cli import main
from bridge_evolve.core import Seat, Vulnerability
from bridge_evolve.systems import decide, load_system, render_system, validate_system
from bridge_evolve.systems.classics import export_classics
from bridge_evolve.systems.compact import SHARED_PREFIX, compact_system, format_system
from bridge_evolve.systems.language import SystemError, parse_call
from bridge_evolve.systems.render import render_expression
from bridge_evolve.systems.v2_runtime import PublicWorld
from systems.support import ROOT
from systems.test_v2 import obs, op, raw_system, rule, transition, var, ctx


def repeated_system():
    raw = raw_system()
    raw["protocol"]["parameters"] = {
        "seen": {"type": "number", "initial": 0},
        "flag": {"type": "bool", "initial": False},
    }
    public = op("and", op("eq", ctx("event.by"), "ours"), op("eq", ctx("event.kind"), "BID"))
    updates = {"seen": op("if", public, op("add", var("seen"), 1), var("seen"))}
    raw["protocol"]["states"]["idle"]["transitions"] = [
        transition("opening", public, to="reply", set=updates),
    ]
    raw["protocol"]["states"]["reply"] = {
        "transitions": [transition("replying", public, set=updates)], "otherwise": "reply",
    }
    private = op("and", op("ge", {"op": "hcp"}, 0), op("ge", {"op": "length", "suit": "S"}, 0),
                 op("ge", {"op": "length", "suit": "H"}, 0))
    raw["rules"] = [
        rule(private, {"kind": "call", "call": "1C"}, "fixture.first", 2),
        rule(private, {"kind": "call", "call": "1D"}, "fixture.second", 1),
    ]
    return validate_system(raw)


class CompactSystemTests(unittest.TestCase):
    def test_existing_schema_roundtrip_is_deterministic_and_does_not_mutate_input(self):
        original = repeated_system()
        before = copy.deepcopy(original.to_data())
        compact = compact_system(original)
        self.assertEqual(original.to_data(), before)
        self.assertEqual(compact.version, original.version)
        self.assertGreater(len(compact.features), len(original.features))
        self.assertEqual(compact.content_hash, compact_system(original).content_hash)
        self.assertEqual(compact.content_hash, validate_system(compact.to_data()).content_hash)
        self.assertNotEqual(compact.content_hash, original.content_hash)
        self.assertEqual([(item.id, item.priority) for item in compact.rules],
                         [(item.id, item.priority) for item in original.rules])

    def test_decisions_and_public_state_updates_are_unchanged(self):
        original = repeated_system()
        compact = compact_system(original)
        before, after = [decide(system, obs(system)) for system in (original, compact)]
        self.assertEqual(str(before.call), "1C")
        self.assertEqual(before.call, after.call)
        self.assertEqual(before.trace.rule_id, after.trace.rule_id)
        self.assertEqual(before.trace.public_context, after.trace.public_context)
        worlds = [PublicWorld({"NS": system}, Seat.NORTH, Vulnerability.NONE) for system in (original, compact)]
        for call in ("1C", "P", "1D", "P", "P", "P"):
            for world in worlds:
                world.step(parse_call(call))
            self.assertEqual(worlds[0].frames, worlds[1].frames)
        self.assertTrue(worlds[1].auction.ended)
        self.assertEqual(worlds[1].frames["NS"].parameters["seen"], 2)

    def test_short_circuiting_and_invalid_selected_branches_are_preserved(self):
        for selected in (True, False):
            raw = raw_system()
            condition = op("if", selected, op("ge", {"op": "hcp"}, 0),
                           op("ge", op("keycards", "NT"), 0))
            raw["rules"] = [
                rule(condition, {"kind": "call", "call": "1C"}, "first", 2),
                rule(condition, {"kind": "call", "call": "1D"}, "second", 1),
            ]
            original = validate_system(raw)
            for system in (original, compact_system(original)):
                with self.subTest(selected=selected, shared=len(system.features)):
                    if selected:
                        self.assertEqual(str(decide(system, obs(system)).call), "1C")
                    else:
                        with self.assertRaises(SystemError):
                            decide(system, obs(system))

    def test_shared_private_expressions_cannot_be_used_in_public_transitions(self):
        compact = compact_system(repeated_system())
        private_name = next(name for name, expression in compact.features
                            if name.startswith(SHARED_PREFIX) and expression.op == "and"
                            and any(child.op == "ge" for child in expression.args))
        raw = compact.to_data()
        raw["protocol"]["states"]["idle"]["transitions"][0]["when"] = {"op": "ref", "name": private_name}
        with self.assertRaisesRegex(SystemError, "cannot access private"):
            validate_system(raw)

    def test_existing_feature_names_are_not_overwritten(self):
        original = repeated_system()
        compact = compact_system(original)
        name = next(name for name, _ in compact.features if name.startswith(SHARED_PREFIX))
        raw = original.to_data()
        raw["features"][name] = True
        with self.assertRaisesRegex(SystemError, "conflicts with an existing feature"):
            compact_system(validate_system(raw))

    def test_feature_and_parameter_named_op_are_not_mistaken_for_expressions(self):
        raw = raw_system()
        label = "not-an-expression-operator-" * 8
        raw["features"]["op"] = label
        raw["protocol"]["parameters"]["op"] = {"type": "string", "initial": ""}
        raw["protocol"]["states"]["idle"]["transitions"] = [
            transition("remember", True, set={"op": label}),
        ]
        original = validate_system(raw)
        compact = compact_system(original)
        self.assertEqual(compact.to_data(), original.to_data())
        world = PublicWorld({"NS": compact}, Seat.NORTH, Vulnerability.NONE)
        world.step(parse_call("1C"))
        self.assertEqual(world.frames["NS"].parameters["op"], label)

    def test_mixed_json_layout_preserves_every_field_and_keeps_short_expressions_inline(self):
        compact = compact_system(repeated_system())
        rendered = format_system(compact)
        self.assertEqual(json.loads(rendered), compact.to_data())
        self.assertIn('{"op":"ref","name":"shared.', rendered)
        self.assertIn('\n  "protocol": {', rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_v2_specification_defines_references_once_instead_of_expanding_uses(self):
        compact = compact_system(repeated_system())
        specification = render_system(compact)
        self.assertIn("使用处按名称引用", specification)
        for name, _ in compact.features:
            self.assertEqual(specification.count(f"- {name}："), 1)
        reference = compact.rules[0].when
        self.assertEqual(render_expression(reference, expand_references=False),
                         f"自定义特征「{dict(reference.parameters)['name']}」")
        self.assertGreater(len(render_expression(reference)),
                           len(render_expression(reference, expand_references=False)))
        self.assertEqual(specification, render_system(validate_system(compact.to_data())))

    def test_v1_systems_still_use_the_existing_language_and_decisions(self):
        original = load_system(ROOT / "systems/1nt-v1.json")
        compact = compact_system(original)
        self.assertEqual(compact.version, 1)
        self.assertEqual(json.loads(format_system(compact)), compact.to_data())

    def test_compact_classic_export_records_original_identity_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
                "bridge_evolve.systems.classics.build_classic", return_value=repeated_system()):
            destination = Path(temporary) / "compact"
            hashes = export_classics(destination, compact=True)
            migration = json.loads((destination / "migration.json").read_text())
            self.assertEqual(migration["systems"], hashes)
            self.assertEqual(migration["representation"]["source_systems"],
                             {"ccba": repeated_system().content_hash, "prec": repeated_system().content_hash})
            self.assertTrue(all(count > 0 for count in migration["representation"]["added_features"].values()))
            self.assertEqual(load_system(destination / "ccba.json").content_hash, hashes["ccba"])
            with self.assertRaises(FileExistsError):
                export_classics(destination, compact=True)

    def test_build_classics_cli_accepts_compact_flag(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
                "bridge_evolve.systems.classics.build_classic", return_value=repeated_system()):
            destination = Path(temporary) / "compact"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = main(["system", "build-classics", "--compact", "--output", str(destination)])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout.getvalue())["systems"]["ccba"],
                             load_system(destination / "ccba.json").content_hash)
