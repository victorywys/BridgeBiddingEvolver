import unittest
from dataclasses import FrozenInstanceError, fields

from bridge_evolve.core import Call, Hand, Seat, Strain, Vulnerability
from bridge_evolve.systems import Observation, compile_expression, decide, render_call, render_system, validate_system
from bridge_evolve.systems.inputs import decision_data, parse_observation
from bridge_evolve.systems.language import SystemError
from bridge_evolve.systems.render import render_expression
from bridge_evolve.systems.runtime import Evaluation
from systems.support import observation, raw_system, rule


class ExpressionSemanticsTests(unittest.TestCase):
    def evaluate(self, expression, obs=None):
        return Evaluation(obs or observation(), "start").evaluate(compile_expression(expression))

    def test_primitives_use_only_the_current_hand_and_public_context(self):
        cases = [
            ({"op": "hcp"}, 15), ({"op": "controls"}, 4),
            ({"op": "shape"}, (4, 3, 3, 3)), ({"op": "balanced"}, True),
            ({"op": "seat"}, "N"), ({"op": "role"}, "dealer"),
            ({"op": "state"}, "start"), ({"op": "vulnerable"}, False),
            ({"op": "previously_passed"}, False), ({"op": "has_contract_bid"}, False),
            ({"op": "length", "suit": "D"}, 4),
            ({"op": "honors", "suit": "S", "ranks": "AKQJT"}, 3),
            ({"op": "has", "card": "AS"}, True), ({"op": "has", "card": "AH"}, False),
            ({"op": "is_legal", "call": "X"}, False),
            ({"op": "is_legal", "call": "1NT"}, True),
        ]
        for expression, expected in cases:
            with self.subTest(expression=expression):
                self.assertEqual(self.evaluate(expression), expected)
                self.assertTrue(render_expression(compile_expression(expression)))

    def test_control_definitions_include_aces_and_explicit_shortage_policy(self):
        for suit, round_number, shortage, expected in (
            ("S", 1, False, True), ("S", 2, False, True),
            ("D", 1, False, False), ("D", 2, False, True),
            ("H", 2, True, False),
        ):
            self.assertEqual(
                self.evaluate({"op": "control", "suit": suit, "round": round_number, "shortage": shortage}),
                expected,
            )
        for hand, round_number, shortage, expected in (
            ("AKQJT.2.KQJ.9876", 1, True, False),
            ("AKQJT.2.KQJ.9876", 2, True, True),
            ("AKQJT.2.KQJ.9876", 2, False, False),
            ("AKQJT.-.KQJ.98765", 1, True, True),
            ("AKQJT.-.KQJ.98765", 2, True, True),
            ("AKQJT.-.KQJ.98765", 1, False, False),
        ):
            self.assertEqual(
                self.evaluate(
                    {"op": "control", "suit": "H", "round": round_number, "shortage": shortage},
                    observation(hand),
                ),
                expected,
            )

    def test_every_composite_operator_has_exact_execution_and_rendering(self):
        cases = [
            ("and", [True, False], False), ("or", [False, True], True),
            ("not", [False], True), ("eq", [2, 2], True), ("ne", [2, 3], True),
            ("lt", [2, 3], True), ("le", [2, 2], True),
            ("gt", [3, 2], True), ("ge", [2, 2], True),
            ("between", [9, 8, 9], True), ("between", [7, 8, 9], False),
            ("in", [[4, 3, 3, 3], [[4, 3, 3, 3], [4, 4, 3, 2]]], True),
            ("add", [1, 2, 3], 6), ("sub", [2, 3], -1),
            ("mul", [2, 3, 4], 24), ("min", [3, 2, 4], 2), ("max", [3, 2, 4], 4),
        ]
        for op, args, expected in cases:
            with self.subTest(op=op):
                expression = {"op": op, "args": args}
                self.assertEqual(self.evaluate(expression), expected)
                self.assertTrue(render_expression(compile_expression(expression)))

    def test_short_circuit_does_not_evaluate_unneeded_private_features(self):
        expression = compile_expression({"op": "and", "args": [
            False, {"op": "gt", "args": [{"op": "hcp"}, 10]},
        ]})
        evaluation = Evaluation(observation(), "start")
        self.assertFalse(evaluation.evaluate(expression))
        self.assertNotIn("hcp", [node.op for node in evaluation.values])

    def test_public_roles_and_vulnerability_follow_the_acting_seat(self):
        from dataclasses import replace
        for count, seat, role in (
            (0, Seat.NORTH, "dealer"), (1, Seat.EAST, "lho"),
            (2, Seat.SOUTH, "partner"), (3, Seat.WEST, "rho"),
        ):
            obs = replace(
                observation(history=(Call.pass_call(),) * count, seat=seat),
                vulnerability=Vulnerability.EW,
            )
            self.assertEqual(self.evaluate({"op": "role"}, obs), role)
            self.assertEqual(self.evaluate({"op": "vulnerable"}, obs), seat in (Seat.EAST, Seat.WEST))

    def test_derived_quality_and_control_condition_can_drive_a_real_decision(self):
        raw = raw_system()
        raw["features"] = {
            "quality": {"op": "add", "args": [
                {"op": "length", "suit": "S"},
                {"op": "mul", "args": [{"op": "honors", "suit": "S", "ranks": "AKQ"}, 2]},
            ]},
        }
        raw["rules"] = [rule("quality_and_control", {"op": "and", "args": [
            {"op": "ge", "args": [{"op": "ref", "name": "quality"}, 9]},
            {"op": "control", "suit": "D", "round": 2, "shortage": False},
        ]}, call="1S")]
        system = validate_system(raw)
        self.assertEqual(str(decide(system, observation()).call), "1S")
        self.assertIn("不计短套", render_system(system))


class DecisionBoundaryTests(unittest.TestCase):
    def test_highest_priority_wins_independent_of_file_order(self):
        raw = raw_system()
        rules = [rule("low", True, "1C", 1), rule("high", True, "1NT", 2)]
        for ordered in (rules, list(reversed(rules))):
            result = decide(validate_system(raw | {"rules": ordered}), observation())
            self.assertEqual(str(result.call), "1NT")
            self.assertEqual(result.trace.rule_id, "high")

    def test_runtime_overlap_is_an_error_even_when_actions_are_identical(self):
        condition = {"op": "ge", "args": [{"op": "hcp"}, 10]}
        raw = raw_system() | {"rules": [rule("a", True), rule("b", condition)]}
        with self.assertRaisesRegex(SystemError, "Conflicting"):
            decide(validate_system(raw), observation())

    def test_illegal_selected_rule_cannot_fall_through_to_pass(self):
        raw = raw_system() | {"rules": [rule("illegal", True, "X", 100), rule("safe", True, "1C", 1)]}
        with self.assertRaisesRegex(SystemError, "no fallback"):
            decide(validate_system(raw), observation())
        raw["rules"] = [rule("guarded", {"op": "is_legal", "call": "X"}, "X")]
        self.assertEqual(str(decide(validate_system(raw), observation()).call), "P")

    def test_illegal_fallback_also_fails(self):
        with self.assertRaisesRegex(SystemError, "fallback"):
            decide(validate_system(raw_system() | {"fallback": {"call": "X"}}), observation())

    def test_public_meaning_never_selects_a_private_branch(self):
        raw = raw_system() | {"rules": [
            rule("strong", {"op": "ge", "args": [{"op": "hcp"}, 15]}, "1NT", 2),
            rule("other", True, "1NT", 1),
        ]}
        system = validate_system(raw)
        strong, weak = observation(), observation("9876.987.987.987")
        a, b = decide(system, strong), decide(system, weak)
        self.assertNotEqual(a.trace.rule_id, b.trace.rule_id)
        self.assertEqual(render_call(system, a.call), render_call(system, b.call))
        output = decision_data(system, strong, a, False)
        self.assertNotIn("private_trace", output)
        self.assertIn("strong", output["public_meaning"])
        self.assertIn("other", output["public_meaning"])
        self.assertIn("private_trace", decision_data(system, strong, a, True))

    def test_observation_is_immutable_and_has_no_hidden_deal_or_oracle_fields(self):
        self.assertEqual(
            {field.name for field in fields(Observation)},
            {"hand", "seat", "dealer", "vulnerability", "history", "public_systems", "public_definitions"},
        )
        obs = observation()
        with self.assertRaises(FrozenInstanceError):
            obs.seat = Seat.EAST
        with self.assertRaises(SystemError):
            Observation(Hand(list(obs.hand.cards)), Seat.NORTH, Seat.NORTH, Vulnerability.NONE, ())
        for secret in ("dds", "board_id", "seed", "partner_hand", "private_trace"):
            raw = {
                "hand": "AKQ.JT9.KQ32.876", "seat": "N", "dealer": "N",
                "vulnerability": "none", "history": [], secret: "forbidden",
            }
            with self.subTest(secret=secret):
                with self.assertRaisesRegex(SystemError, "unknown"):
                    parse_observation(raw)

    def test_turn_legality_and_auction_completion_are_validated(self):
        with self.assertRaises(SystemError):
            observation(seat=Seat.SOUTH)
        with self.assertRaises(ValueError):
            observation(history=(Call.double(),))
        with self.assertRaises(SystemError):
            observation(history=(Call.pass_call(),) * 4)
        obs = observation(history=(Call.pass_call(),) * 2, seat=Seat.SOUTH)
        evaluation = Evaluation(obs, "start")
        self.assertFalse(evaluation.evaluate(compile_expression({"op": "previously_passed"})))
        history = (Call.pass_call(), Call.bid(1, Strain.CLUBS), Call.pass_call(), Call.pass_call())
        evaluation = Evaluation(observation(history=history), "start")
        self.assertTrue(evaluation.evaluate(compile_expression({"op": "previously_passed"})))
        self.assertTrue(evaluation.evaluate(compile_expression({"op": "has_contract_bid"})))


if __name__ == "__main__":
    unittest.main()
