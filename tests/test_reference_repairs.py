import unittest

from bridge_evolve.core import Seat, Vulnerability
from bridge_evolve.systems import decide, load_system
from bridge_evolve.systems.classics import build_classic
from bridge_evolve.systems.inputs import parse_board, parse_hand, run_auction
from bridge_evolve.systems.language import parse_call
from bridge_evolve.systems.runtime import Observation
from bridge_evolve.systems.v2_runtime import PublicContext, build_world
from tools.verify_classics import verify_bounds
from test_board_viewer import ROOT

BOARD_101 = {"dealer": "N", "vulnerability": "ns", "hands": {
    "N": "9.AT53.AQ42.AT75", "E": "J86.J98742.9.962",
    "S": "KT75.Q.KJ76.KJ43", "W": "AQ432.K6.T853.Q8"}}
BOARD_11 = {"dealer": "S", "vulnerability": "none", "hands": {
    "N": "98.54.J9642.A963", "E": "74.AKT9873.Q.KJ8",
    "S": "KT32.J.KT873.Q42", "W": "AQJ65.Q62.A5.T75"}}
PARENT = "14649d251a62e0a7ce32c7a173cca9c7903d7d8f0b0f3318f903ed3b18a2ec7f"
CANDIDATE = "7a68e80669e1488bbcdc910ec6a61d78b6648478da888fbe436158e01a0e67f5"


class ReferenceRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ccba, cls.prec = build_classic("ccba"), build_classic("prec")
        cls.old = load_system(ROOT / "systems/classics-v5.5.4-compact/ccba.json")

    def observe(self, hand, history, dealer=Seat.NORTH, vulnerability=Vulnerability.NONE):
        calls = tuple(parse_call(item) for item in history.split())
        seat = dealer
        for _ in calls:
            seat = seat.next()
        return Observation(parse_hand(hand), seat, dealer, vulnerability, calls,
                           public_definitions=(("NS", self.ccba), ("EW", self.ccba)))

    def test_exact_uncovered_fourth_suit_interference_has_an_explicit_natural_continuation(self):
        history = "1D P 1S P 2C P 2H 2S"
        observation = self.observe(BOARD_101["hands"]["N"], history, vulnerability=Vulnerability.NS)
        old = Observation(observation.hand, observation.seat, observation.dealer, observation.vulnerability,
                          observation.history, public_definitions=(("NS", self.old), ("EW", self.old)))
        with self.assertRaisesRegex(ValueError, "No declared classic-policy completion"):
            decide(self.old, old)
        decision = decide(self.ccba, observation)
        self.assertIn("competition.forcing_known_rebid_", decision.trace.rule_id)
        self.assertEqual(str(decision.call), "3C")
        world = build_world(self.ccba, observation)
        world.step(decision.call)
        context = PublicContext(world, "NS", Seat.NORTH)
        self.assertEqual(context.value("forcing"), "game")
        self.assertEqual(context.frame.parameters["captain_C_min"], 4)

    def test_known_rebid_does_not_invent_a_four_card_suit_or_run_when_not_forced(self):
        history = "1D P 1S P 2C P 2H 2S"
        for hand, expected in (("9.AT53.AQ42.AT75", "3C"), ("9.AT5.AQ432.AT75", "5D")):
            observation = self.observe(hand, history, vulnerability=Vulnerability.NS)
            result = decide(self.ccba, observation)
            self.assertEqual(str(result.call), expected)
        observation = self.observe("9.AT53.AQ42.AT75", "1D 1S P P")
        self.assertNotIn("competition.forcing_known_rebid_", decide(self.ccba, observation).trace.rule_id)

    def test_three_card_support_keeps_the_eight_card_fit_without_claiming_four(self):
        observation = self.observe(BOARD_11["hands"]["W"], "P 1S P 2H P", dealer=Seat.SOUTH)
        decision = decide(self.ccba, observation)
        self.assertEqual(str(decision.call), "3H")
        world = build_world(self.ccba, observation)
        world.step(decision.call)
        context = PublicContext(world, "EW", Seat.WEST)
        self.assertEqual(context.frame.parameters["captain_H_min"], 3)
        self.assertEqual(context.value("agreed_trump_min"), 8)

    def test_four_card_one_level_response_still_requires_four_support(self):
        observation = self.observe("A.KQ53.AQ42.KJ75", "1D P 1H P")
        decision = decide(self.ccba, observation)
        world = build_world(self.ccba, observation)
        world.step(decision.call)
        context = PublicContext(world, "NS", Seat.NORTH)
        self.assertGreaterEqual(context.frame.parameters["captain_H_min"], 4)

    def test_both_original_failure_boards_complete_and_keep_public_guarantees_true(self):
        z_parent = load_system(ROOT / f"systems/research-z/{PARENT}.json")
        z_candidate = load_system(ROOT / f"systems/research-z/{CANDIDATE}.json")
        cases = ((BOARD_101, self.ccba), (BOARD_101, self.prec), (BOARD_101, z_parent),
                 (BOARD_11, z_candidate), (BOARD_11, self.prec))
        for raw, opponent in cases:
            board = parse_board(raw)
            for ns, ew in ((self.ccba, opponent), (opponent, self.ccba)):
                with self.subTest(board=raw["dealer"], ns=ns.name, ew=ew.name):
                    result = run_auction(ns, ew, board, trace=True, compact=True)
                    expected = sum(any("var.captain_hcp_min" in step.get(field, {})
                                       for field in ("public_context", "public_context_after"))
                                   for step in result["auction"])
                    self.assertGreater(expected, 0)
                    self.assertEqual(verify_bounds(result["auction"], board), expected)


if __name__ == "__main__":
    unittest.main()
