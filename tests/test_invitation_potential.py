import unittest
from dataclasses import replace

from bridge_evolve.core import Seat, Vulnerability
from bridge_evolve.systems import decide, load_system, validate_system
from bridge_evolve.systems.classics import build_classic
from bridge_evolve.systems.inputs import parse_hand, parse_board, run_auction
from bridge_evolve.systems.language import parse_call
from bridge_evolve.systems.runtime import Evaluation, Observation
from bridge_evolve.systems.v2_runtime import PublicContext, build_world, public_context
from test_board_viewer import ROOT
from tools.verify_classics import verify_bounds
from regression_fixtures import regression_cases


class InvitationPotentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ccba, cls.prec = build_classic("ccba"), build_classic("prec")
        cls.old = load_system(ROOT / "systems/classics-v5.4.2-compact/ccba.json")
        cls.previous_precision = load_system(ROOT / "systems/classics-v5.5.2-compact/prec.json")
        raw = cls.ccba.to_data()
        transition = next(item for item in raw["protocol"]["states"]["idle"]["transitions"] if ".open_1NT." in item["id"])
        transition["set"].update(open_min=16, open_max=19, captain_hcp_min=16, captain_hcp_max=19)
        cls.wide_nt = validate_system(raw)

    def observe(self, hand, history):
        calls = tuple(parse_call(call) for call in history.split())
        return Observation(parse_hand(hand), list(Seat)[len(calls) % 4], Seat.NORTH, Vulnerability.NONE, calls)

    def feature(self, system, observation, name):
        world = build_world(system, observation)
        context = PublicContext(world, observation.seat.partnership.value, observation.seat)
        evaluation = Evaluation(observation, context.frame.state, context)
        return evaluation.evaluate(dict(system.features)[name])

    def test_two_over_one_low_fit_preserves_slam_space_even_at_public_minimum(self):
        for system in (self.ccba, self.prec):
            observation = self.observe("KQ32.Q87.KJ4.Q76", "1S P 2C P 2S P")
            decision = decide(system, observation)
            self.assertEqual(str(decision.call), "3S")
            world = build_world(system, observation)
            world.step(decision.call)
            context = PublicContext(world, "NS", Seat.NORTH)
            self.assertEqual(context.frame.state, "slam_cue")
            self.assertEqual(context.value("agreed_trump_min"), 8)
            self.assertEqual(context.frame.parameters["responder_hcp_min"], 13)
            self.assertEqual(context.frame.parameters["responder_S_min"], 3)
            self.assertNotIn(context.frame.forcing, ("small_slam", "grand_slam"))

    def test_unknown_upper_limit_without_controls_does_not_trigger_keycards(self):
        observation = self.observe("K32.KQJ87.Q54.Q2", "1H P 2D P 2H P 3H P")
        self.assertEqual(self.feature(self.ccba, observation, "partner_max"), 40)
        self.assertFalse(self.feature(self.ccba, observation, "slam_willing_H"))
        self.assertNotEqual(str(decide(self.ccba, observation).call), "4NT")

    def test_splinter_fit_and_working_controls_can_start_below_thirty_hcp(self):
        observation = self.observe("AJ987.KQJ.54.KQ3", "1S P 4D P")
        self.assertEqual(self.feature(self.ccba, observation, "total_min"), 29)
        self.assertTrue(self.feature(self.ccba, observation, "shape_values_S"))
        self.assertTrue(self.feature(self.ccba, observation, "slam_possible_S"))
        self.assertTrue(self.feature(self.ccba, observation, "small_slam_values_S"))
        decision = decide(self.ccba, observation)
        self.assertEqual(str(decision.call), "4H")
        world = build_world(self.ccba, observation)
        world.step(decision.call)
        self.assertTrue(world.frames["NS"].parameters["captain_H_control"])
        self.assertEqual(world.frames["NS"].parameters["captain_hcp_min"], 11)

    def test_splinter_wasted_honors_reduce_private_fit_value_not_public_hcp(self):
        useful = self.observe("AJ987.KQ2.54.AQ3", "1S P 4D P")
        wasted = self.observe("AJ987.542.KQ.QA3", "1S P 4D P")
        self.assertEqual(self.feature(self.ccba, useful, "total_min"), self.feature(self.ccba, wasted, "total_min"))
        self.assertGreater(self.feature(self.ccba, useful, "fit_value_S"), self.feature(self.ccba, wasted, "fit_value_S"))
        self.assertEqual(public_context(self.ccba, useful)["values"], public_context(self.ccba, wasted)["values"])

    def test_finite_partnership_ceiling_below_slam_blocks_slam_interest(self):
        observation = self.observe("KQ3.AJ87.Q54.876", "1D P 1H P 2H P")
        self.assertLess(self.feature(self.prec, observation, "total_max"), 29)
        self.assertFalse(self.feature(self.prec, observation, "slam_possible_H"))
        self.assertEqual(str(decide(self.prec, observation).call), "2NT")

    def test_natural_nt_game_invitation_is_not_limited_to_a_last_one_nt_call(self):
        observation = self.observe("Q3.AJ8.K54.Q8762", "1S P 1NT P 2S P")
        self.assertEqual(str(decide(self.old, observation).call), "P")
        self.assertEqual(str(decide(self.ccba, observation).call), "2NT")
        world = build_world(self.ccba, observation)
        world.step(parse_call("2NT"))
        self.assertEqual(world.frames["NS"].forcing, "invite_game")
        self.assertTrue(world.frames["NS"].parameters["game_invited"])
        self.assertEqual(world.frames["NS"].parameters["responder_hcp_min"], 8)

    def test_game_invitation_can_start_with_only_twenty_one_known_points(self):
        observation = self.observe("KQJ987.AKQ2.54.3", "1S P 2S P")
        self.assertEqual(self.feature(self.ccba, observation, "total_min"), 21)
        self.assertEqual(self.feature(self.ccba, observation, "total_max"), 25)
        self.assertTrue(self.feature(self.ccba, observation, "game_invite_values"))
        self.assertEqual(str(decide(self.ccba, observation).call), "3S")

    def test_major_invitation_publishes_sound_extras_and_can_be_accepted_or_declined(self):
        observation = self.observe("AKJ987.AKQ2.54.3", "1S P 2S P")
        self.assertEqual(str(decide(self.ccba, observation).call), "3S")
        history = "1S P 2S P 3S P"
        high = self.observe("Q32.J87.AQ6.JT86", history)
        low = self.observe("Q32.J87.K65.T986", history)
        values = public_context(self.ccba, high)["values"]
        self.assertEqual(values["partner_hcp_min"], 15)
        self.assertEqual(values["forcing"], "invite_game")
        self.assertEqual(str(decide(self.ccba, high).call), "4S")
        self.assertEqual(str(decide(self.old, high).call), "P")
        self.assertEqual(str(decide(self.ccba, low).call), "P")
        world = build_world(self.ccba, high)
        world.step(parse_call("4S"))
        self.assertEqual(world.frames["NS"].parameters["responder_hcp_min"], 10)

    def test_nt_slam_invitation_uses_finite_possible_thirty_three_not_guaranteed_thirty_one(self):
        observation = self.observe("AQ3.K87.K54.Q876", "1NT P")
        self.assertEqual(self.feature(self.wide_nt, observation, "total_min"), 30)
        self.assertEqual(self.feature(self.wide_nt, observation, "total_max"), 33)
        self.assertTrue(self.feature(self.wide_nt, observation, "nt_slam_interest"))
        self.assertIn(str(decide(self.wide_nt, observation).call), ("4C", "4NT"))

    def test_board55_weak_nt_range_uses_quantitative_invite_before_gerber(self):
        observation = self.observe("KQ.Q75.AK9.KQ543", "1NT P")
        self.assertEqual(self.feature(self.prec, observation, "total_min"), 32)
        self.assertEqual(self.feature(self.prec, observation, "total_max"), 34)
        self.assertEqual(str(decide(self.previous_precision, observation).call), "4C")
        decision = decide(self.prec, observation)
        self.assertEqual(str(decision.call), "4NT")
        world = build_world(self.prec, observation)
        world.step(decision.call)
        context = PublicContext(world, "NS", observation.seat)
        self.assertEqual(context.frame.state, "quantitative")
        self.assertEqual(context.frame.forcing, "slam_try")
        self.assertEqual(context.frame.parameters["responder_hcp_min"], 18)
        self.assertEqual(context.value("partnership_hcp_min"), 31)

    def test_precision_quantitative_slam_accepts_fifteen_and_can_decline_thirteen_or_fourteen(self):
        for hand, hcp, call in (
            ("A8.AK64.QJ865.J6", 15, "6NT"),
            ("A8.AK64.QJ865.76", 14, "P"),
            ("A8.AK64.Q9865.76", 13, "P"),
        ):
            with self.subTest(hcp=hcp):
                observation = self.observe(hand, "1NT P 4NT P")
                self.assertEqual(self.feature(self.prec, observation, "total_min"), hcp + 18)
                self.assertEqual(str(decide(self.prec, observation).call), call)
                world = build_world(self.prec, observation)
                world.step(parse_call(call))
                self.assertEqual(world.frames["NS"].forcing, "none")

    def test_board55_full_deal_reaches_south_six_nt_with_sound_public_information(self):
        board = parse_board({"dealer": "S", "vulnerability": "both", "hands": {
            "N": "KQ.Q75.AK9.KQ543", "E": "T975.JT98.T74.82",
            "S": "A8.AK64.QJ865.J6", "W": "J6432.32.32.AT97",
        }})
        played = run_auction(self.prec, self.ccba, board, compact=True, trace=True)
        self.assertEqual([step["call"] for step in played["auction"] if step["call"] != "P"],
                         ["1NT", "4NT", "6NT"])
        self.assertEqual(played["contract"], {"level": 6, "strain": "NT", "declarer": "S", "doubling": 1})
        self.assertEqual(verify_bounds(played["auction"], board), len(played["auction"]))
        invitation = next(step for step in played["auction"] if step["call"] == "4NT")
        acceptance = next(step for step in played["auction"] if step["call"] == "6NT")
        self.assertEqual(invitation["public_context_after"]["var.responder_hcp_min"], 18)
        self.assertEqual(invitation["private_trace"]["inference"]["partnership_hcp_max"], 34)
        self.assertEqual(acceptance["private_trace"]["inference"]["partnership_hcp_min"], 33)

    def test_gerber_still_checks_aces_when_the_small_slam_point_floor_is_already_met(self):
        observation = self.observe("KQ.Q75.AK9.KQ543", "1NT P")
        self.assertEqual(self.feature(self.ccba, observation, "total_min"), 34)
        self.assertEqual(str(decide(self.ccba, observation).call), "4C")
        for system in (self.ccba, self.prec):
            borderline = self.observe("KQ3.K87.QJ54.J76", "2NT P")
            enough = self.observe("KQ3.KJ8.QJ54.J76", "2NT P")
            self.assertEqual(self.feature(system, borderline, "total_min"), 32)
            self.assertEqual(str(decide(system, borderline).call), "4NT")
            self.assertEqual(self.feature(system, enough, "total_min"), 33)
            self.assertEqual(str(decide(system, enough).call), "4C")

    def test_genuine_gerber_retreat_is_not_silently_reinterpreted_as_quantitative(self):
        observation = self.observe("A8.AK64.QJ865.J6", "1NT P 4C P 4S P 4NT P")
        context = public_context(self.prec, observation)
        self.assertEqual(context["state"], "finished")
        self.assertEqual(str(decide(self.prec, observation).call), "P")

    def test_club_ace_does_not_change_a_borderline_quantitative_invitation(self):
        for hand in ("KQ.Q75.AK9.KQ543", "KQ.Q75.KQ9.AK543"):
            self.assertEqual(str(decide(self.prec, self.observe(hand, "1NT P")).call), "4NT")

    def test_unknown_nt_ceiling_is_not_treated_as_a_guaranteed_thirty_three(self):
        observation = self.observe("K32.Q87.A54.J876", "1C P 1NT P 2C P 2D P 3NT P")
        self.assertFalse(self.feature(self.prec, observation, "nt_slam_interest"))
        self.assertNotIn(str(decide(self.prec, observation).call), ("4C", "4NT", "6NT", "7NT"))

    def test_other_private_hands_never_change_public_invitation_meaning(self):
        observation = self.observe("Q32.J87.AQ6.JT86", "1S P 2S P 3S P")
        changed = replace(observation, hand=parse_hand("KQ32.Q87.KJ4.Q76"))
        self.assertEqual(public_context(self.ccba, observation)["values"], public_context(self.ccba, changed)["values"])

    def test_weak_unbounded_overcall_is_not_treated_as_a_uniform_forty_point_ceiling(self):
        observation = self.observe("KQJ87.Q32.Q54.K2", "1D 1S P 2S P")
        self.assertFalse(self.feature(self.ccba, observation, "game_invite_values"))

    def test_heart_survey_cannot_force_a_weak_inviter_over_five_hearts(self):
        observation = self.observe("K32.AJ987.AJ54.2", "1H P 2NT P 3S P")
        self.assertTrue(self.feature(self.ccba, observation, "slam_interest_H"))
        self.assertFalse(self.feature(self.ccba, observation, "small_slam_values_H"))
        self.assertFalse(self.feature(self.ccba, observation, "safe_keycards_H"))
        self.assertEqual(str(decide(self.ccba, observation).call), "4C")

    def test_spade_survey_can_still_probe_and_decline_below_six(self):
        observation = self.observe("AJ87.2.K32.AJ765", "1S P 2NT P 3H P")
        self.assertTrue(self.feature(self.ccba, observation, "safe_keycards_S"))
        self.assertFalse(self.feature(self.ccba, observation, "small_slam_values_S"))
        self.assertEqual(str(decide(self.ccba, observation).call), "4NT")
        later = self.observe("AJ87.2.K32.AJ765", "1S P 2NT P 3H P 4NT P 5S P")
        self.assertEqual(str(decide(self.ccba, later).call), "P")

    def test_followup_queen_question_cannot_turn_a_weak_survey_into_a_forced_slam(self):
        observation = self.observe("KJ87.A32.A54.A76", "1S P 2NT P 3H P 4NT P 5C P")
        self.assertFalse(self.feature(self.ccba, observation, "small_slam_values_S"))
        self.assertEqual(str(decide(self.ccba, observation).call), "5S")

    def test_incomplete_major_fit_is_reselected_low_not_misread_as_exclusion(self):
        for example in regression_cases("minor_fit_reselection"):
            board = parse_board(example["board"])
            seat = Seat(example["seat"])
            system = self.ccba if seat.partnership.value == "NS" else self.prec
            observation = Observation(board.deal.hand(seat), seat, board.dealer, board.vulnerability,
                tuple(parse_call(call) for call in example["prefix"]),
                public_definitions=(("NS", self.ccba), ("EW", self.prec)))
            decision = decide(system, observation)
            expected = example["expected_call"][-1]
            self.assertEqual(str(decision.call), example["expected_call"])
            world = build_world(system, observation)
            world.step(decision.call)
            context = PublicContext(world, seat.partnership.value, seat)
            self.assertEqual(context.value("agreed_trump"), expected)
            self.assertGreaterEqual(context.value("agreed_trump_min"), 8)
            played = run_auction(self.ccba, self.prec, board, compact=True, trace=True)
            self.assertEqual(verify_bounds(played["auction"], board), len(played["auction"]))


if __name__ == "__main__":
    unittest.main()
