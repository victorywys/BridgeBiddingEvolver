import random
import unittest
from dataclasses import replace

from bridge_evolve.core import Deal, Hand, Seat, Vulnerability, full_deck
from bridge_evolve.systems import decide, load_system, render_system, validate_system
from bridge_evolve.systems.inputs import Board, parse_board, parse_hand, run_uncontested
from bridge_evolve.systems.language import SystemError, load_json, parse_call
from bridge_evolve.systems.runtime import BALANCED_SHAPES, hand_hcp, hand_shape
from systems.support import ROOT, observation


class BaselineProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = load_system(ROOT / "systems/1nt-v1.json")

    def choose(self, hand, history=()):
        calls = tuple(parse_call(call) for call in history)
        seat = (Seat.NORTH, Seat.EAST, Seat.SOUTH, Seat.WEST)[len(calls) % 4]
        return decide(self.system, observation(hand, calls, seat))

    def test_opening_includes_both_point_boundaries_and_excludes_other_hands(self):
        for hand, expected in (
            ("AKQ.JT9.KQ32.876", "1NT"),
            ("AKQ.QT9.KQ32.876", "1NT"),
            ("AKQ.KT9.KQ32.876", "1NT"),
            ("AKQ.AT9.KQ32.876", "P"),
            ("AKQ.987.KQ32.876", "P"),
            ("AKQJT.2.KQJ.9876", "P"),
        ):
            with self.subTest(hand=hand):
                self.assertEqual(str(self.choose(hand).call), expected)

    def test_responder_point_boundaries_and_longest_major_tie_break(self):
        for hand, expected in (
            ("T987.KQ2.Q94.987", "P"),
            ("T987.KQ2.QJ4.987", "2NT"),
            ("J987.KQ2.QJ4.987", "2NT"),
            ("Q987.KQ2.QJ4.987", "3NT"),
            ("98765.432.32.432", "2H"),
            ("432.98765.32.432", "2D"),
            ("KJ987.QT987.A2.3", "2D"),
            ("KJ9876.QT987.A.3", "2H"),
        ):
            with self.subTest(hand=hand):
                self.assertEqual(str(self.choose(hand, ("1NT", "P")).call), expected)

    def test_transfer_sending_acceptance_and_continuation_are_linked(self):
        for request, acceptance, weak, invite, strong in (
            ("2D", "2H", "432.98765.32.432", "Q32.KJ987.Q4.987", "AKQ2.JT987.AJ.T9"),
            ("2H", "2S", "98765.432.32.432", "KJ987.Q32.Q4.987", "JT987.AKQ2.AJ.T9"),
        ):
            prefix = ("1NT", "P", request, "P")
            self.assertEqual(str(self.choose("AKQ.JT9.KQ32.876", prefix).call), acceptance)
            for hand, expected in ((weak, "P"), (invite, "2NT"), (strong, "3NT")):
                self.assertEqual(str(self.choose(hand, (*prefix, acceptance, "P")).call), expected)

    def test_invitation_and_game_choices_use_opener_fit_and_maximum(self):
        for request, acceptance, suit in (("2D", "2H", "H"), ("2H", "2S", "S")):
            for last, minimum, maximum in (("2NT", f"3{suit}", f"4{suit}"), ("3NT", f"4{suit}", f"4{suit}")):
                history = ("1NT", "P", request, "P", acceptance, "P", last, "P")
                self.assertEqual(str(self.choose("AKQ.JT9.KQ32.876", history).call), minimum)
                self.assertEqual(str(self.choose("AKQ.KT9.KQ32.876", history).call), maximum)
            no_fit = "AKQJ.98.KQ32.876" if suit == "H" else "98.AKQJ.KQ32.876"
            no_fit_max = "AKQJ.98.KQJ2.J87" if suit == "H" else "98.AKQJ.KQJ2.J87"
            history = ("1NT", "P", request, "P", acceptance, "P", "2NT", "P")
            self.assertEqual(str(self.choose(no_fit, history).call), "P")
            self.assertEqual(str(self.choose(no_fit_max, history).call), "3NT")
            history = (*history[:-2], "3NT", "P")
            self.assertEqual(str(self.choose(no_fit, history).call), "P")
        history = ("1NT", "P", "2NT", "P")
        self.assertEqual(str(self.choose("AKQ.JT9.KQ32.876", history).call), "P")
        self.assertEqual(str(self.choose("AKQ.KT9.KQ32.876", history).call), "3NT")

    def test_public_state_reconstruction_and_unexpected_interference(self):
        self.assertEqual(self.system.state_after(()), "start")
        self.assertEqual(self.system.state_after(tuple(map(parse_call, ("1NT", "P")))), "respond")
        history = ("1NT", "X")
        self.assertEqual(self.system.state_after(tuple(map(parse_call, history))), "outside")
        result = self.choose("JT987.AKQ2.AJ.T9", history)
        self.assertEqual(str(result.call), "P")
        self.assertEqual(result.trace.state, "outside")

    def test_example_full_deal_finishes_in_4s_by_north_without_claiming_a_score(self):
        board = parse_board(load_json(ROOT / "examples/board.json"))
        result = run_uncontested(self.system, board, trace=True)
        self.assertEqual([entry["call"] for entry in result["auction"]], [
            "1NT", "P", "2H", "P", "2S", "P", "3NT", "P", "4S", "P", "P", "P",
        ])
        self.assertEqual(result["contract"], {"level": 4, "strain": "S", "declarer": "N", "doubling": 1})
        self.assertEqual(result["evaluation"], "not_evaluated")
        self.assertNotIn("score", result)
        for entry in result["auction"]:
            if entry["seat"] in ("E", "W"):
                self.assertNotIn("private_trace", entry)

    def test_other_hidden_hands_cannot_change_a_players_decision(self):
        board = parse_board(load_json(ROOT / "examples/board.json"))
        swapped = replace(board, deal=Deal(board.deal.north, board.deal.west, board.deal.south, board.deal.east))
        a, b = run_uncontested(self.system, board, trace=True), run_uncontested(self.system, swapped, trace=True)
        self.assertEqual(a, b)
        public = render_system(self.system)
        for _ in range(3):
            self.assertEqual(run_uncontested(self.system, board), run_uncontested(self.system, swapped))
            self.assertEqual(render_system(self.system), public)
        rotated = replace(board, deal=Deal(
            board.deal.north, board.deal.south, board.deal.west, board.deal.east,
        ))
        self.assertEqual(
            run_uncontested(self.system, board, trace=True)["auction"][0],
            run_uncontested(self.system, rotated, trace=True)["auction"][0],
        )

    def test_sending_and_public_interpretation_can_be_changed_as_one_protocol(self):
        raw = self.system.to_data()
        for rule in raw["rules"]:
            if rule["id"] == "transfer_spades":
                rule["call"] = "2C"
        transitions = raw["protocol"]["states"]["respond"]["on"]
        transitions["2C"] = transitions.pop("2H")
        changed = validate_system(raw)
        board = parse_board(load_json(ROOT / "examples/board.json"))
        result = run_uncontested(changed, board)
        self.assertEqual(result["auction"][2]["call"], "2C")
        self.assertEqual(result["auction"][4]["call"], "2S")
        self.assertEqual(result["contract"]["strain"], "S")
        self.assertNotEqual(changed.content_hash, self.system.content_hash)
        self.assertIn("2C", render_system(changed))

    def test_invalid_or_out_of_scope_inputs_are_rejected_not_repaired(self):
        board = parse_board(load_json(ROOT / "examples/board.json"))
        with self.assertRaisesRegex(SystemError, "fixes N"):
            run_uncontested(self.system, replace(board, dealer=Seat.EAST))
        with self.assertRaisesRegex(SystemError, "outside"):
            run_uncontested(self.system, replace(
                board, deal=Deal(board.deal.east, board.deal.north, board.deal.south, board.deal.west),
            ))
        raw = load_json(ROOT / "examples/board.json")
        raw["hands"]["E"] = raw["hands"]["N"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_board(raw)
        for hand in ("AKQ..KQ32.876", "AKQ.JT9.KQ32.87", "AAQ.JT9.KQ32.876"):
            with self.assertRaises(ValueError):
                parse_hand(hand)

    def test_random_complete_auctions_are_legal_reproducible_and_cover_all_rules(self):
        rng = random.Random(73021)
        deck = list(full_deck())
        accepted = 0
        covered = set()
        while accepted < 300:
            rng.shuffle(deck)
            north = Hand(tuple(deck[:13]))
            if not 15 <= hand_hcp(north) <= 17 or hand_shape(north) not in BALANCED_SHAPES:
                continue
            accepted += 1
            board = Board(Deal(north, *(Hand(tuple(deck[i:i + 13])) for i in (13, 26, 39))),
                          Seat.NORTH, Vulnerability.NONE)
            result = run_uncontested(self.system, board, trace=True)
            self.assertEqual(result, run_uncontested(self.system, board, trace=True))
            for entry in result["auction"]:
                if "private_trace" in entry:
                    covered.add(entry["private_trace"]["rule_id"])
                    self.assertIn(entry["call"], entry["legal_calls"])
        self.assertEqual(covered - {"fallback"}, {rule.id for rule in self.system.rules})


if __name__ == "__main__":
    unittest.main()
