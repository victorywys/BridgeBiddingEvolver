import unittest

from bridge_evolve.core import Seat, Vulnerability
from bridge_evolve.systems import decide
from bridge_evolve.systems.classics import build_classic
from bridge_evolve.systems.inputs import parse_board, parse_hand, private_inference, run_auction
from bridge_evolve.systems.language import parse_call
from bridge_evolve.systems.runtime import Observation
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld, public_context
from tools.verify_classics import verify_bounds
from test_board_viewer import viewer
from regression_fixtures import regression_cases

BOARD_772 = {"dealer": "W", "vulnerability": "both", "hands": {
    "N": "T652.62.KT2.JT42", "E": "AKJ8.T974.AJ.A76",
    "S": "973.J3.976543.Q3", "W": "Q4.AKQ85.Q8.K985",
}}


class SlamProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ccba, cls.prec = build_classic("ccba"), build_classic("prec")

    def observation(self, hand, history, dealer=Seat.NORTH):
        calls = tuple(map(parse_call, history.split()))
        return Observation(parse_hand(hand), list(Seat)[(list(Seat).index(dealer) + len(calls)) % 4],
                           dealer, Vulnerability.NONE, calls)

    def test_support_question_and_sixth_step_publish_both_suit_promises(self):
        world = PublicWorld({"EW": self.prec, "NS": self.ccba}, Seat.WEST, Vulnerability.BOTH)
        for text in "1C P 1NT P 2H".split():
            world.step(parse_call(text))
        values = PublicContext(world, "EW", Seat.EAST).snapshot()
        self.assertEqual(dict(values)["var.captain_H_min"], 5)
        self.assertEqual(dict(values)["var.responder_H_min"], 2)
        for text in "P 3S".split():
            world.step(parse_call(text))
        values = dict(PublicContext(world, "EW", Seat.WEST).snapshot())
        self.assertEqual(values["agreed_trump"], "H")
        self.assertEqual(values["partnership_H_min"], 8)
        self.assertEqual(values["var.responder_H_min"], 3)
        self.assertTrue(values["var.responder_H_support_quality"])
        self.assertLessEqual(values["var.responder_S_max"], 4)
        observation = self.observation(BOARD_772["hands"]["W"], "1C P 1NT P 2H P 3S P", Seat.WEST)
        self.assertEqual(private_inference(observation, tuple(values.items()))["suit_min"]["H"], 9)

    def test_support_quality_accepts_three_with_q_or_better_and_four_small(self):
        for hand, expected in (("KQ43.A32.543.432", "2NT"), ("AQ43.Q32.543.432", "2NT"),
                               ("AK43.J32.J54.432", "2S"), ("AKQ4.432.543.432", "2S"),
                               ("AKQ4.5432.543.43", "2NT")):
            with self.subTest(hand=hand):
                result = decide(self.prec, self.observation(hand, "1C P 1NT P 2H P"))
                self.assertEqual(str(result.call), expected)

    def test_board772_reaches_a_real_heart_slam_without_dds_in_policy(self):
        board = parse_board(BOARD_772)
        result = run_auction(self.ccba, self.prec, board, compact=True, trace=True)
        self.assertGreaterEqual(result["contract"]["level"], 6)
        self.assertEqual(result["contract"]["strain"], "H")
        self.assertEqual(verify_bounds(result["auction"], board), len(result["auction"]))
        self.assertTrue(any("slam_controls" in step["private_trace"]["rule_id"] for step in result["auction"]))
        self.assertTrue(any(step.get("public_context_after", {}).get("var.captain_C_control") for step in result["auction"]))
        meanings = viewer.explain_public_auction(result["auction"], "W", "both",
            {"NS": self.ccba, "EW": self.prec})
        self.assertTrue(all(not item["warnings"] for item in meanings))
        question = next(meaning for step, meaning in zip(result["auction"], meanings) if step["call"] == "2H")
        answer = next(meaning for step, meaning in zip(result["auction"], meanings) if step["call"] == "3S")
        self.assertIn("红桃至少5张", question["summary"])
        self.assertIn("红桃支持至少Qxx", answer["summary"])

    def test_game_force_trump_return_requests_cues_but_not_six_level_commitment(self):
        world = PublicWorld({"NS": self.ccba, "EW": self.prec}, Seat.NORTH, Vulnerability.NONE)
        for text in "1S P 2NT P 3S".split():
            world.step(parse_call(text))
        values = dict(PublicContext(world, "NS", Seat.SOUTH).snapshot())
        self.assertEqual(values["state"], "slam_cue")
        self.assertTrue(values["var.slam_active"])
        self.assertNotIn(values["forcing"], ("small_slam", "grand_slam"))
        world.step(parse_call("P"))
        world.step(parse_call("4C"))
        values = dict(PublicContext(world, "NS", Seat.NORTH).snapshot())
        self.assertTrue(values["var.responder_C_control"])
        self.assertEqual(values["var.responder_C_min"], 0)
        self.assertTrue(values["must_bid"])
        self.assertFalse(values["goal_met"])

    def test_artificial_game_level_splinter_does_not_clear_game_force(self):
        world = PublicWorld({"NS": self.ccba, "EW": self.prec}, Seat.NORTH, Vulnerability.NONE)
        for text in "1H P 3NT".split():
            world.step(parse_call(text))
        self.assertEqual(world.frames["NS"].forcing, "game")
        world.step(parse_call("P"))
        observation = self.observation("AQ3.KQJ87.K54.87", "1H P 3NT P")
        self.assertNotEqual(str(decide(self.ccba, observation).call), "P")

    def test_precision_solid_positive_requests_controls_explicitly(self):
        observation = self.observation("K2.AKQ9876.K5.A2", "1C P 1NT P")
        decision = decide(self.prec, observation)
        self.assertEqual(str(decision.call), "3H")
        world = PublicWorld({"NS": self.prec}, Seat.NORTH, Vulnerability.NONE)
        for text in "1C P 1NT P 3H".split():
            world.step(parse_call(text))
        self.assertEqual(world.frames["NS"].state, "slam_cue")
        self.assertTrue(world.frames["NS"].parameters["slam_solid"])
        self.assertEqual(world.frames["NS"].parameters["captain_H_min"], 7)

    def test_public_control_and_quality_do_not_depend_on_observer_hand(self):
        history = "1C P 1NT P 2H P 3S P 4C P"
        first = public_context(self.prec, self.observation(BOARD_772["hands"]["E"], history))
        second = public_context(self.prec, self.observation("KQ43.A32.543.432", history))
        self.assertEqual(first["values"], second["values"])

    def test_weak_long_club_reply_cannot_impersonate_a_thirteen_point_two_over_one(self):
        for system in (self.ccba, self.prec):
            decision = decide(system, self.observation("-.97.543.AJT98763", "1H P"))
            self.assertEqual(str(decision.call), "P")
        board = parse_board({"dealer": "S", "vulnerability": "ns", "hands": {
            "N": "-.97.543.AJT98763", "E": "KQJT954.T6.JT.K4",
            "S": "A72.KQJ32.Q762.Q", "W": "863.A854.AK98.52",
        }})
        for ns, ew in ((self.ccba, self.prec), (self.prec, self.ccba)):
            result = run_auction(ns, ew, board, compact=True, trace=True)
            self.assertEqual(verify_bounds(result["auction"], board), len(result["auction"]))

    def test_late_two_nt_after_strong_club_negative_is_not_marshall_minors(self):
        world = PublicWorld({"NS": self.ccba, "EW": self.prec}, Seat.EAST, Vulnerability.NONE)
        for text in "1C P 1D 2NT".split():
            world.step(parse_call(text))
        frame = world.frames["NS"]
        self.assertEqual((frame.parameters["two_suit_a"], frame.parameters["two_suit_b"]), ("C", "H"))
        self.assertEqual(frame.parameters["captain_C_min"], 5)
        self.assertEqual(frame.parameters["captain_H_min"], 5)
        self.assertEqual(frame.parameters["captain_D_min"], 0)

    def test_weak_minor_responses_exit_the_first_reply_phase_without_illegal_rebids(self):
        for case in regression_cases("weak_minor_exits"):
            raw = case["board"]
            board = parse_board(raw)
            for ns, ew in ((self.ccba, self.prec), (self.prec, self.ccba)):
                with self.subTest(dealer=board.dealer, system=ns.name):
                    result = run_auction(ns, ew, board, compact=True, trace=True)
                    self.assertEqual(verify_bounds(result["auction"], board), len(result["auction"]))

    def test_late_low_point_strong_club_double_cannot_impersonate_twelve_points(self):
        for case in regression_cases("late_strong_club_double"):
            raw = case["board"]
            board = parse_board(raw)
            for ns, ew in ((self.ccba, self.prec), (self.prec, self.ccba)):
                with self.subTest(dealer=board.dealer, system=ns.name):
                    result = run_auction(ns, ew, board, compact=True, trace=True)
                    self.assertEqual(verify_bounds(result["auction"], board), len(result["auction"]))


if __name__ == "__main__":
    unittest.main()
