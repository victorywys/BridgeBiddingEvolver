import random
import json
import runpy
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from bridge_evolve.core import Deal, Hand, Seat, Vulnerability, full_deck
from bridge_evolve.systems import compile_expression, decide, load_system, validate_system
from bridge_evolve.systems.classics import build_classic, export_classics, read_manual
from bridge_evolve.systems.inputs import Board, parse_board, parse_hand, run_auction
from bridge_evolve.systems.language import SystemError, parse_call
from bridge_evolve.systems.runtime import Evaluation, Observation
from bridge_evolve.systems.v2_runtime import PublicContext, build_world, public_context
from regression_fixtures import frozen_v4


class ClassicTranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ccba = build_classic("ccba")
        cls.prec = build_classic("prec")

    def choose(self, system, hand, history=(), dealer=Seat.NORTH):
        calls = tuple(map(parse_call, history))
        seat = dealer
        for _ in calls:
            seat = seat.next()
        observation = Observation(parse_hand(hand), seat, dealer, Vulnerability.NONE, calls)
        return decide(system, observation)

    def test_manual_opening_families_and_strong_club_exception(self):
        cases = [
            (self.ccba, "AKQ.JT9.KQ32.876", "1NT"),
            (self.prec, "AKJ.QT9.QJ32.876", "1NT"),
            (self.prec, "AKQ.KT9.KQ32.876", "1C"),
            (self.prec, "AKQ.AJ9.KQ32.J76", "2NT"),
            (self.ccba, "AKQ.AJ9.KQ32.K76", "2C"),
            (self.prec, "AKQ9.KJ98.2.9876", "2D"),
            (self.prec, "AKQ9.KJ98.-.98765", "2D"),
            (self.prec, "9.KQJ9.832.AQJ87", "2C"),
        ]
        for system, hand, expected in cases:
            with self.subTest(system=system.name, hand=hand):
                self.assertEqual(str(self.choose(system, hand).call), expected)

    def test_strong_club_negative_4441_bids_a_four_card_major_not_the_singleton(self):
        for hand, expected in (("KQ76.7.AKQJ.AK93", "1S"),
                               ("7.KQ76.AKQJ.AK93", "1H"),
                               ("KQ76.AK93.7.AKQJ", "1H")):
            self.assertEqual(str(self.choose(self.prec, hand, ("1C", "P", "1D", "P")).call), expected)
        self.assertEqual(str(self.choose(self.ccba, "KQ76.7.AKQJ.AK93", ("2C", "P", "2D", "P")).call), "2S")

    def test_precision_minor_positive_rebid_conveys_five_cards(self):
        history = tuple(map(parse_call, ("1C", "P", "2D", "P", "2S", "P")))
        observation = Observation(parse_hand("J432.K.KJ732.863"), Seat.EAST, Seat.WEST, Vulnerability.BOTH, history)
        values = public_context(self.prec, observation)["values"]
        self.assertEqual(values["partner_S_min"], 5)
        self.assertEqual(str(decide(self.prec, observation).call), "3S")

    def test_negative_four_card_exception_does_not_weaken_positive_rebids(self):
        calls = tuple(map(parse_call, ("1C", "P", "1D", "P", "1S", "P")))
        observation = Observation(parse_hand("J432.K.KJ732.863"), Seat.EAST, Seat.WEST, Vulnerability.BOTH, calls)
        self.assertEqual(public_context(self.prec, observation)["values"]["partner_S_min"], 4)

    def test_minor_openings_publish_major_upper_bounds_and_natural_order(self):
        for system in (self.ccba, self.prec):
            observation = Observation(parse_hand("J432.K.KJ732.863"), Seat.SOUTH, Seat.NORTH, Vulnerability.NONE,
                                      tuple(map(parse_call, ("1D", "P"))))
            values = public_context(system, observation)["values"]
            self.assertEqual(values["partner_S_max"], 4)
            self.assertEqual(values["partner_H_max"], 4)
            if system is self.prec:
                self.assertEqual(values["partner_C_max"], 5)
            else:
                self.assertTrue(values["var.captain_D_ge_C"])

    def test_natural_major_order_survives_a_second_suit_and_tightens_bounds(self):
        calls = tuple(map(parse_call, ("1S", "P", "1NT", "P", "2H", "P")))
        for system in (self.ccba, self.prec):
            observation = Observation(parse_hand("J432.K.KJ732.863"), Seat.SOUTH, Seat.NORTH, Vulnerability.NONE, calls)
            values = public_context(system, observation)["values"]
            self.assertTrue(values["var.captain_S_ge_H"])
            self.assertEqual(values["partner_S_min"], 5)
            self.assertEqual(values["partner_H_min"], 4)
            self.assertEqual(values["partner_S_max"], 9)
            self.assertEqual(values["partner_H_max"], 6)
            self.assertEqual(values["partner_C_max"], 4)

    def test_positive_4441_has_a_shape_reply_not_a_fake_five_card_major(self):
        calls = ("1C", "P", "2D", "P")
        self.assertEqual(str(self.choose(self.prec, "AKQ9.KJ98.2.QJ76", calls).call), "3NT")
        self.assertEqual(str(self.choose(self.prec, "AKQJ.KQJ9.2.A987", calls).call), "2NT")
        observation = Observation(parse_hand("J432.K.KJ732.863"), Seat.EAST, Seat.WEST, Vulnerability.BOTH,
                                  tuple(map(parse_call, (*calls, "3NT", "P"))))
        values = public_context(self.prec, observation)["values"]
        self.assertEqual(values["partner_S_min"], values["partner_S_max"])
        self.assertEqual(values["partner_S_min"], 4)
        self.assertEqual(values["partner_D_max"], 1)
        self.assertEqual(str(decide(self.prec, observation).call), "4S")

    def test_board_580_records_the_public_update_immediately_after_two_spades(self):
        board = parse_board({"dealer": "W", "vulnerability": "both", "hands": {
            "N": "987.AJT75.6.Q942", "E": "J432.K.KJ732.863",
            "S": "-.Q98632.Q54.AT75", "W": "AKQT65.4.AT98.KJ",
        }})
        result = run_auction(self.ccba, self.prec, board, compact=True, trace=True)
        rebid = next(step for step in result["auction"] if step["seat"] == "W" and step["call"] == "2S")
        self.assertEqual(rebid["public_context_after"]["var.captain_S_min"], 5)
        self.assertEqual(rebid["public_context"]["var.captain_S_min"], 0)
        answer = next(step for step in result["auction"] if step["seat"] == "E" and step["call"] == "3S")
        self.assertEqual(answer["public_context"]["var.captain_S_min"], 5)
        self.assertGreaterEqual(answer["private_trace"]["inference"]["suit_min"]["S"], 9)
        self.assertGreaterEqual(answer["private_trace"]["inference"]["suit_max"]["S"], 10)
        self.assertEqual(result["contract"], {"level": 5, "strain": "S", "declarer": "W", "doubling": 1})

    def test_board_86_natural_rebid_does_not_invent_an_eight_card_minor_fit(self):
        board = parse_board({"dealer": "E", "vulnerability": "ew", "hands": {
            "N": "KT2.AT2.J85.AKT8", "E": "J654.9743.962.J6",
            "S": "A7.KQ865.AQ.Q752", "W": "Q983.J.KT743.943",
        }})
        opponent = load_system(Path(__file__).resolve().parents[1] / "systems/pass-v2.json")
        result = run_auction(self.ccba, opponent, board, compact=True, trace=True)
        rebid = next(step for step in result["auction"] if step["seat"] == "S" and step["call"] == "3C")
        self.assertIn(".opener_rebid.second_suit_C.", rebid["private_trace"]["rule_id"])
        after = rebid["public_context_after"]
        self.assertEqual(after["var.captain_C_min"], 4)
        self.assertEqual(after["var.responder_C_min"], 3)
        self.assertEqual(after["partnership_C_min"], 7)
        self.assertNotEqual(after["agreed_trump"], "C")
        self.assertNotEqual(after["state"], "slam_cue")
        checker = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/verify_classics.py"))["verify_bounds"]
        self.assertGreater(checker(result["auction"], board), 0)

    def test_board_86_precision_forced_three_hearts_is_not_a_solid_seven_card_jump(self):
        board = parse_board({"dealer": "E", "vulnerability": "ew", "hands": {
            "N": "KT2.AT2.J85.AKT8", "E": "J654.9743.962.J6",
            "S": "A7.KQ865.AQ.Q752", "W": "Q983.J.KT743.943",
        }})
        opponent = load_system(Path(__file__).resolve().parents[1] / "systems/pass-v2.json")
        result = run_auction(self.prec, opponent, board, compact=True, trace=True)
        rebid = next(step for step in result["auction"] if step["seat"] == "S" and step["call"] == "3H")
        self.assertIn(".strong_positive.longest_H.", rebid["private_trace"]["rule_id"])
        after = rebid["public_context_after"]
        self.assertEqual(after["var.captain_H_min"], 4)
        self.assertEqual(after["var.responder_H_min"], 2)
        self.assertEqual(after["partnership_H_min"], 6)
        self.assertNotEqual(after["state"], "slam_cue")
        checker = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/verify_classics.py"))["verify_bounds"]
        self.assertGreater(checker(result["auction"], board), 0)

    def test_precision_solid_jump_requires_the_lower_natural_bid_to_be_available(self):
        hand = "2.AKQJT98.A2.K76"
        jump = self.choose(self.prec, hand, ("1C", "P", "1NT", "P"))
        self.assertEqual(str(jump.call), "3H")
        self.assertIn(".solid_request_cues_H.", jump.trace.rule_id)
        forced = self.choose(self.prec, hand, ("1C", "P", "2S", "P"))
        self.assertEqual(str(forced.call), "3H")
        self.assertIn(".strong_positive.longest_H.", forced.trace.rule_id)
        for reply, minimum, phase in (("1NT", 7, "slam_cue"), ("2S", 4, "natural")):
            observation = Observation(parse_hand("Q32.K87.Q54.9876"), Seat.SOUTH, Seat.NORTH, Vulnerability.NONE,
                                      tuple(map(parse_call, ("1C", "P", reply, "P", "3H", "P"))))
            context = public_context(self.prec, observation)
            self.assertEqual(context["values"]["var.captain_H_min"], minimum)
            self.assertEqual(context["state"], phase)

    def test_unambiguous_four_four_minor_fit_still_allows_public_slam_exploration(self):
        for system in (self.ccba, self.prec):
            decision = self.choose(system, "A7.KQ865.AQ52.Q7", ("1H", "P", "2D", "P"))
            self.assertEqual(str(decision.call), "3D")
            observation = Observation(parse_hand("KT2.AT2.J852.AK8"), Seat.SOUTH, Seat.NORTH, Vulnerability.NONE,
                                      tuple(map(parse_call, ("1H", "P", "2D", "P", "3D", "P"))))
            context = public_context(system, observation)
            self.assertEqual(context["values"]["var.captain_D_min"], 4)
            self.assertEqual(context["values"]["agreed_trump_min"], 8)
            self.assertEqual(context["values"]["agreed_trump"], "D")
            self.assertEqual(context["state"], "slam_cue")

    def test_unwritten_rebid_repeats_a_real_five_card_suit_not_a_singleton(self):
        for system in (self.ccba, self.prec):
            result = self.choose(system, "AQ87.KJ987.K54.2", ("1H", "P", "1NT", "P"))
            self.assertEqual(str(result.call), "2H")

    def test_support_calls_publish_length_and_confirm_trump(self):
        for system in (self.ccba, self.prec):
            observation = Observation(parse_hand("AKJ87.K32.Q54.87"), Seat.NORTH, Seat.NORTH,
                Vulnerability.NONE, tuple(map(parse_call, ("1S", "P", "2S", "P"))))
            values = public_context(system, observation)["values"]
            self.assertEqual(values["partner_S_min"], 3)
            self.assertEqual(values["agreed_trump_min"], 8)
            self.assertEqual(values["agreed_trump"], "S")

    def test_board_580_support_now_records_the_selected_trump(self):
        history = tuple(map(parse_call, ("1C", "P", "2D", "P", "2S", "P", "4S", "P")))
        observation = Observation(parse_hand("AKQT65.4.AT98.KJ"), Seat.WEST, Seat.WEST, Vulnerability.BOTH, history)
        values = public_context(self.prec, observation)["values"]
        self.assertEqual(values["agreed_trump"], "S")
        self.assertEqual(values["partner_S_min"], 3)
        self.assertEqual(values["agreed_trump_min"], 8)

    def test_transfer_game_requires_a_known_eight_card_fit(self):
        history = ("1NT", "P", "2D", "P", "2H", "P")
        self.assertEqual(str(self.choose(self.ccba, "A32.KQJ98.Q4.T98", history).call), "3NT")
        self.assertEqual(str(self.choose(self.ccba, "A32.KQJ987.Q4.T9", history).call), "4H")

    def test_precision_4441_and_shape_relays_publish_their_promised_suits(self):
        for calls, expected in (
            (("1C", "P", "2NT", "P"), {"S": 4, "H": 4, "D": 4}),
            (("1C", "P", "1NT", "P", "2C", "P", "3C", "P", "3D", "P", "3S", "P"), {"C": 4, "S": 4}),
        ):
            observation = Observation(parse_hand("AK3.KQ8.A54.J762"), Seat.NORTH, Seat.NORTH,
                Vulnerability.NONE, tuple(map(parse_call, calls)))
            values = public_context(self.prec, observation)["values"]
            for strain, minimum in expected.items():
                self.assertGreaterEqual(values["partner_" + strain + "_min"], minimum)

    def test_competitive_natural_calls_publish_length_and_strength(self):
        tools = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/verify_classics.py"))
        catalog = {case["id"]: case for case in tools["semantic_cases"]()}
        for name in ("overcall_natural_new_suit_response", "two_level_natural_overcall_strength",
                     "takeout_advance_length", "michaels_unknown_minor_reveal",
                     "lebensohl_natural_weak_major", "lebensohl_natural_game_suit", "lebensohl_weak_diamond_reveal"):
            for system in (self.ccba, self.prec):
                with self.subTest(case=name, system=system.name):
                    result = tools["semantic_probe"](system, catalog[name], self.prec if system is self.ccba else self.ccba)
                    self.assertEqual(result["status"], "consistent", result)

    def test_interfered_michaels_preference_does_not_claim_natural_response_strength(self):
        probe = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/verify_classics.py"))["semantic_probe"]
        result = probe(self.prec, {"id": "interfered_michaels_preference", "history": ["1H", "2H", "3H"],
            "hand": "T82.762.4.AJ8754", "call": "3S", "requirements": [
                {"field": "owner_hcp_min", "value": 0, "relation": "eq"},
                {"field": "owner_S_min", "value": 0, "relation": "eq"}]}, self.ccba)
        self.assertEqual(result["status"], "consistent", result)

    def test_late_nt_interference_does_not_reenter_responder_conventions(self):
        for system, hand in ((self.ccba, "AKQ.JT9.KQ32.876"), (self.prec, "AKJ.QT9.QJ32.876")):
            history = tuple(map(parse_call, ("1NT", "P", "P", "2D")))
            observation = Observation(parse_hand(hand), Seat.NORTH, Seat.NORTH, Vulnerability.NONE, history)
            self.assertEqual(public_context(system, observation)["state"], "competition")
            self.assertEqual(str(decide(system, observation).call), "P")

    def test_late_strong_nt_interference_preserves_negative_responder_range(self):
        history = tuple(map(parse_call, ("1C", "P", "1D", "P", "1NT", "P", "P", "2D")))
        observation = Observation(parse_hand("AKT.AJT65.932.KQ"), Seat.WEST, Seat.WEST,
            Vulnerability.EW, history)
        values = public_context(self.prec, observation)["values"]
        self.assertEqual((values["var.responder_hcp_min"], values["var.responder_hcp_max"]), (0, 7))
        self.assertEqual(str(decide(self.prec, observation).call), "P")

    def test_two_over_one_game_force_can_show_a_four_card_suit_without_reverse_extras(self):
        for system, minimum in ((self.ccba, 12), (self.prec, 11)):
            self.assertEqual(str(self.choose(system, "QJT2.AK98.Q84.87", ("1D", "P", "2C", "P")).call), "2S")
            observation = Observation(parse_hand("AK3.Q65.87.AKQJ4"), Seat.SOUTH, Seat.NORTH,
                Vulnerability.NONE, tuple(map(parse_call, ("1D", "P", "2C", "P", "2S", "P"))))
            values = public_context(system, observation)["values"]
            self.assertEqual(values["partner_S_min"], 4)
            self.assertEqual(values["partner_hcp_min"], minimum)
            self.assertEqual(values["forcing"], "game")

    def test_artificial_checkback_diamonds_do_not_claim_six_cards(self):
        history = tuple(map(parse_call, ("1C", "P", "1D", "P", "1NT", "P")))
        observation = Observation(parse_hand("AK3.Q65.KJ987.87"), Seat.SOUTH, Seat.NORTH,
            Vulnerability.NONE, history)
        decision = decide(self.ccba, observation)
        self.assertEqual(str(decision.call), "2D")
        world = build_world(self.ccba, observation)
        world.step(decision.call)
        parameters = world.frames["NS"].parameters
        self.assertEqual(parameters["responder_D_min"], 4)
        self.assertEqual(parameters["responder_C_min"], 0)

    def test_lebensohl_takeout_double_hands_the_turn_to_the_opener(self):
        for system, hand, minimum in ((self.prec, "T.KQJ9865.KJ4.85", 10), (self.ccba, "T.KQJ9865.Q84.85", 8)):
            history = tuple(map(parse_call, ("1NT", "2S")))
            observation = Observation(parse_hand(hand), Seat.EAST, Seat.WEST, Vulnerability.NONE, history)
            decision = decide(system, observation)
            self.assertEqual(str(decision.call), "X")
            world = build_world(system, observation)
            world.step(decision.call)
            frame = world.frames["EW"]
            self.assertEqual((frame.state, frame.forcing, frame.target), ("competition", "one_round", Seat.WEST))
            self.assertEqual(frame.parameters["responder_hcp_min"], minimum)
            reply = Observation(parse_hand("J32.A4.AQ872.KJ7"), Seat.WEST, Seat.WEST,
                Vulnerability.NONE, history + tuple(map(parse_call, ("X", "P"))))
            self.assertEqual(str(decide(system, reply).call), "3D")
            after = build_world(system, reply)
            after.step(parse_call("3D"))
            self.assertEqual(after.frames["EW"].parameters["responder_hcp_min"], minimum)
            self.assertEqual(after.frames["EW"].parameters["captain_D_min"], 4)

    def test_readonly_semantic_probe_detects_the_known_natural_rebid_gap(self):
        probe = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/verify_classics.py"))["semantic_probe"]
        frozen = frozen_v4("prec")
        original_hash = frozen.content_hash
        result = probe(frozen, {"id": "strong_positive_spades", "dealer": "W",
            "history": ["1C", "P", "2D", "P"], "hand": "AKQT65.4.AT98.KJ", "call": "2S",
            "requirements": [{"field": "owner_S_min", "value": 5}]})
        self.assertEqual(result["status"], "confirmed_gap")
        self.assertEqual(result["findings"][0]["field"], "captain_S_min")
        self.assertEqual(frozen.content_hash, original_hash)
        control = probe(self.ccba, {"id": "natural_opening", "hand": "AKQJ87.K32.Q54.8", "call": "1S",
            "requirements": [{"field": "owner_S_min", "value": 5}]})
        self.assertEqual(control["status"], "consistent")

    def test_semantic_probe_separates_sender_defects_from_missing_information(self):
        tools = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/verify_classics.py"))
        catalog = {case["id"]: case for case in tools["semantic_cases"]()}
        frozen_ccba, frozen_prec = frozen_v4("ccba"), frozen_v4("prec")
        result = tools["semantic_probe"](frozen_ccba, catalog["economical_fallback_singleton"], frozen_prec)
        self.assertEqual(result["call"], "2C")
        self.assertEqual(result["findings"][0]["kind"], "natural_action_without_promised_length")
        self.assertEqual(result["findings"][0]["actual"], 1)
        artificial = tools["semantic_probe"](self.prec, catalog["strong_club_not_natural_clubs"], self.ccba)
        self.assertEqual(artificial["status"], "consistent")
        original_hash = frozen_prec.content_hash
        confirmation = tools["semantic_probe"](frozen_prec, catalog["unwritten_four_spade_agreement_580"], frozen_ccba)
        self.assertTrue(confirmation["counterfactual"])
        self.assertEqual(confirmation["call"], "4S")
        self.assertEqual(confirmation["findings"][0]["field"], "trump")
        self.assertEqual(frozen_prec.content_hash, original_hash)

    def test_corrected_public_semantic_contracts(self):
        tools = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/verify_classics.py"))
        systems = {"ccba": self.ccba, "prec": self.prec}
        for case in tools["semantic_cases"](corrected=True):
            for kind in case["systems"]:
                with self.subTest(case=case["id"], system=kind):
                    result = tools["semantic_probe"](systems[kind], case, systems["prec" if kind == "ccba" else "ccba"])
                    self.assertEqual(result["status"], "consistent", result)
                    self.assertFalse(result["counterfactual"])

    def test_manual_reader_preserves_suits_and_paragraph_numbers(self):
        document = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/><w:p><w:r><w:t>2</w:t><w:sym w:font="Symbol" w:char="F0A7"/><w:tab/><w:t>to 2</w:t><w:sym w:font="Symbol" w:char="F0A8"/></w:r></w:p></w:body></w:document>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.docx"
            with zipfile.ZipFile(path, "w") as output:
                output.writestr("word/document.xml", document)
            self.assertEqual(read_manual(path), ((1, ""), (2, "2C to 2D")))

    def test_translation_export_has_rule_links_scenarios_and_explicit_assumptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hashes = export_classics(root)
            manifest = json.loads((root / "translation.json").read_text())
            migration = json.loads((root / "migration.json").read_text())
            self.assertEqual(migration["systems"], hashes)
            self.assertEqual(manifest["profile_version"], "translation-v5.5.4")
            self.assertEqual(len(manifest["partnership_state"]["goals"]), 8)
            self.assertIn("hcp_intervals", manifest["partnership_state"])
            self.assertIn("pass_limits", manifest["partnership_state"])
            self.assertFalse(manifest["source_documents_verified"])
            self.assertIn("exclusion_conflict", manifest["assumptions"])
            self.assertEqual(len(manifest["modules"]), 9)
            for module in manifest["modules"]:
                self.assertTrue(any(module["rule_ids"].values()))
                self.assertTrue(hasattr(type(self), module["scenario_test"].rsplit("::", 1)[1]))

    def test_the_two_1nt_protocols_are_not_reused_by_only_changing_points(self):
        hand = "Q32.KJ987.Q4.987"
        self.assertEqual(str(self.choose(self.ccba, hand, ("1NT", "P")).call), "2D")
        self.assertEqual(str(self.choose(self.prec, hand, ("1NT", "P")).call), "2H")
        self.assertEqual(str(self.choose(self.prec, "AKJ.QT9.QJ32.876",
                                        ("1NT", "P", "2D", "P")).call), "2NT")

    def test_precision_weak_nt_invitation_does_not_impersonate_a_minor_transfer(self):
        hand = "KQ3.Q87.K54.J876"
        self.assertEqual(str(self.choose(self.prec, hand, ("1NT", "P")).call), "2C")
        self.assertEqual(str(self.choose(self.prec, "KJ3.KJ87.K54.Q76", ("1NT", "P", "2C", "P")).call), "2H")
        history = ("1NT", "P", "2C", "P", "2H", "P")
        self.assertEqual(str(self.choose(self.prec, hand, history).call), "2NT")
        observation = Observation(parse_hand("KJ3.KJ87.K54.Q76"), Seat.NORTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, history + ("2NT", "P"))))
        result = public_context(self.prec, observation)
        self.assertEqual(result["state"], "quantitative")
        self.assertEqual((result["values"]["partner_hcp_min"], result["values"]["partner_hcp_max"]), (10, 11))

    def test_competitive_notrump_uses_partnership_strength_not_responder_points_alone(self):
        history = ("1D", "1H", "P")
        for system in (self.ccba, self.prec):
            for hand, expected in (("AQ3.K8.KJ54.8762", "1NT"),
                                   ("AKQ.K8.KJ54.8762", "2NT"),
                                   ("AKQ.K8.AJ54.8762", "3NT")):
                with self.subTest(system=system.name, hand=hand):
                    self.assertEqual(str(self.choose(system, hand, history).call), expected)

    def test_opening_intervals_have_public_hcp_upper_bounds(self):
        for system, opening, minimum, maximum in ((self.prec, "1D", 11, 15), (self.ccba, "1C", 12, 21)):
            observation = Observation(parse_hand("AQ3.K8.KJ54.8762"), Seat.SOUTH, Seat.NORTH,
                Vulnerability.NONE, tuple(map(parse_call, (opening, "P"))))
            values = public_context(system, observation)["values"]
            self.assertEqual(values["partner_hcp_min"], minimum)
            self.assertEqual(values["partner_hcp_max"], maximum)

    def test_unopened_pass_caps_a_seat_and_survives_role_assignment(self):
        for system, maximum in ((self.prec, 10), (self.ccba, 11)):
            observation = Observation(parse_hand("AKJ87.A32.K54.A2"), Seat.SOUTH, Seat.NORTH,
                Vulnerability.NONE, tuple(map(parse_call, ("P", "P", "1S", "P", "2S", "P"))))
            values = public_context(system, observation)["values"]
            self.assertEqual(values["partner_hcp_max"], min(maximum, 10))
            self.assertEqual(values["var.seat_N_hcp_max"], maximum)
            public_again = public_context(system, replace(observation, hand=parse_hand("KQJ87.Q32.Q54.K2")))
            self.assertEqual(public_again["values"], values)

    def test_competitive_pass_is_not_automatically_a_weak_hand(self):
        observation = Observation(parse_hand("AKJ87.A32.K54.A2"), Seat.NORTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, ("1S", "2C", "P", "P"))))
        values = public_context(self.ccba, observation)["values"]
        self.assertEqual(values["partner_hcp_max"], 40)

    def test_private_point_interval_combines_own_hand_with_partner_range(self):
        from bridge_evolve.systems.inputs import decision_data
        observation = Observation(parse_hand("AQ3.K8.KJ54.8762"), Seat.SOUTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, ("1D", "P"))))
        output = decision_data(self.prec, observation, decide(self.prec, observation), True)
        inference = output["private_trace"]["inference"]
        self.assertEqual((inference["partnership_hcp_min"], inference["partnership_hcp_max"]), (24, 28))
        self.assertEqual(output["public_context"]["partnership_hcp_min"], 11)
        self.assertEqual(output["public_context"]["partnership_hcp_max"], 40)

    def test_precision_club_no_fit_does_not_call_game_with_only_twenty_three_known_points(self):
        history = ("2C", "P", "2D", "P", "2H", "P")
        self.assertEqual(str(self.choose(self.prec, "AQ32.K87.QJ54.76", history).call), "2NT")
        self.assertEqual(str(self.choose(self.prec, "AKQ3.K87.QJ54.76", history).call), "3NT")

    def test_lebensohl_does_not_jump_to_game_using_the_openers_points_alone(self):
        history = ("1NT", "P", "P", "2S", "2NT", "P", "3C", "P")
        self.assertEqual(str(self.choose(self.ccba, "AKJ.KQ8.Q54.J762", history).call), "P")

    def test_semi_forcing_notrump_response_can_include_twelve_points(self):
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "AQ3.87.KJ54.Q876", ("1S", "P")).call), "1NT")
            observation = Observation(parse_hand("AKJ87.K32.Q54.87"), Seat.NORTH, Seat.NORTH,
                Vulnerability.NONE, tuple(map(parse_call, ("1S", "P", "1NT", "P"))))
            self.assertEqual(public_context(system, observation)["values"]["partner_hcp_max"], 12)

    def test_unlimited_direct_nt_game_does_not_claim_a_false_seventeen_point_cap(self):
        self.assertEqual(str(self.choose(self.prec, "AK3.QJ8.K54.AJ76", ("1D", "P")).call), "3NT")
        observation = Observation(parse_hand("Q32.K87.AJ54.K76"), Seat.NORTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, ("1D", "P", "3NT", "P"))))
        self.assertEqual(public_context(self.prec, observation)["values"]["partner_hcp_max"], 40)

    def test_strong_two_positive_support_is_the_lowest_legal_raise_not_the_same_bid(self):
        result = self.choose(self.ccba, "KQ8.K542.AK3.AK7", ("2C", "P", "2H", "P"))
        self.assertEqual(str(result.call), "3H")

    def test_precision_strong_club_negative_nt_uses_transfers_not_weak_nt_stayman(self):
        result = self.choose(self.prec, "Q32.KJ987.Q4.987", ("1C", "P", "1D", "P", "1NT", "P"))
        self.assertEqual(str(result.call), "2D")

    def test_two_way_checkback_invitation_is_a_partnership_relay(self):
        for system, opening in ((self.ccba, "1C"), (self.prec, "1D")):
            history = (opening, "P", "1H", "P", "1NT", "P")
            with self.subTest(system=system.name):
                self.assertEqual(str(self.choose(system, "KQ3.AJ87.Q54.876", history).call), "2C")
                self.assertEqual(str(self.choose(system, "AQ3.KJ8.Q54.8762", history + ("2C", "P")).call), "2D")
                self.assertEqual(str(self.choose(system, "KQ3.AJ87.Q54.876", history + ("2C", "P", "2D", "P")).call), "2NT")
                self.assertEqual(str(self.choose(system, "AK3.KQ87.Q54.876", history).call), "2D")

    def test_two_way_checkback_weak_diamonds_stop_after_the_forced_relay(self):
        for system, opening in ((self.ccba, "1C"), (self.prec, "1D")):
            history = (opening, "P", "1H", "P", "1NT", "P")
            hand = "Q32.9874.KJ654.8"
            self.assertEqual(str(self.choose(system, hand, history).call), "2C")
            self.assertEqual(str(self.choose(system, hand, history + ("2C", "P", "2D", "P")).call), "P")

    def test_precision_xyz_and_ccba_fourth_suit_keep_different_two_club_meanings(self):
        history = ("1D", "P", "1H", "P", "1S", "P")
        invitation = "KQ3.AJ87.Q54.876"
        forcing = "AK3.KQ87.Q54.876"
        self.assertEqual(str(self.choose(self.prec, invitation, history).call), "2C")
        self.assertEqual(str(self.choose(self.prec, "AQJ3.K8.Q54.8762", history + ("2C", "P")).call), "2D")
        self.assertEqual(str(self.choose(self.ccba, forcing, history).call), "2C")
        self.assertEqual(str(self.choose(self.ccba, "AQJ3.K87.K54.876", history + ("2C", "P")).call), "2H")

    def test_fourth_suit_game_force_is_not_a_natural_four_card_suit(self):
        history = ("1D", "P", "1S", "P", "2C", "P")
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "AKJ87.32.Q54.KQ8", history).call), "2H")
            self.assertEqual(str(self.choose(system, "AQ3.K8.KJ54.8762", history + ("2H", "P")).call), "2S")

    def test_minor_opening_major_raise_inquiry_has_minimum_and_maximum_answers(self):
        for system, opening in ((self.ccba, "1C"), (self.prec, "1D")):
            history = (opening, "P", "1H", "P", "2H", "P")
            self.assertEqual(str(self.choose(system, "KQ3.AJ87.Q54.876", history).call), "2NT")
            self.assertEqual(str(self.choose(system, "AQ3.KQ87.K54.876", history + ("2NT", "P")).call), "4H")

    def test_mini_splinter_sends_shortness_only_after_the_public_question(self):
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "KQ32.AJ87.5.J876", ("1H", "P")).call), "3S")
            history = ("1H", "P", "3S", "P")
            self.assertEqual(str(self.choose(system, "AQ3.KQJ87.K54.87", history).call), "3NT")
            self.assertEqual(str(self.choose(system, "KQ32.AJ87.5.J876", history + ("3NT", "P")).call), "4D")

    def test_help_suit_invitation_has_acceptance_and_decline(self):
        history = ("1H", "P", "2H", "P")
        self.assertEqual(str(self.choose(self.ccba, "AQ3.AK987.876.K2", history).call), "3D")
        self.assertEqual(str(self.choose(self.ccba, "J32.Q87.AQ54.876", history + ("3D", "P")).call), "4H")
        self.assertEqual(str(self.choose(self.ccba, "J32.K87.9654.K76", history + ("3D", "P")).call), "3H")

    def test_jacoby_minimum_does_not_falsely_show_slam_values(self):
        history = ("1S", "P", "2NT", "P")
        self.assertEqual(str(self.choose(self.ccba, "KQJ87.Q32.Q54.K2", history).call), "4S")

    def test_precision_jacoby_maximum_respects_the_limited_opening(self):
        history = ("1S", "P", "2NT", "P")
        self.assertEqual(str(self.choose(self.prec, "AKQ987.Q32.K5.87", history).call), "3S")
        observation = Observation(parse_hand("KJ32.AQ87.54.J76"), Seat.SOUTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, history + ("3S", "P"))))
        values = public_context(self.prec, observation)["values"]
        self.assertEqual((values["partner_hcp_min"], values["partner_hcp_max"]), (14, 15))
        self.assertEqual(values["partner_S_min"], 6)

    def test_precision_nt_rebid_cannot_violate_its_declared_twelve_point_cap(self):
        history = ("1D", "P", "1H", "P")
        self.assertEqual(str(self.choose(self.prec, "AK3.Q8.AJ9876.32", history).call), "2D")
        self.assertEqual(str(self.choose(self.prec, "KJ3.K8.KJ9876.32", history).call), "1NT")

    def test_limit_raise_upper_bound_accounts_for_the_responders_known_minimum(self):
        self.assertEqual(str(self.choose(self.ccba, "AQJ3.K8.AQ54.K76", ("1D", "P", "1S", "P")).call), "3S")
        observation = Observation(parse_hand("KQ987.32.J54.876"), Seat.SOUTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, ("1D", "P", "1S", "P", "3S", "P"))))
        values = public_context(self.ccba, observation)["values"]
        self.assertEqual((values["partner_hcp_min"], values["partner_hcp_max"]), (16, 19))

    def test_michaels_and_unusual_nt_have_distinct_public_two_suit_meanings(self):
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "KQJ87.AJ987.32.4", ("1D",)).call), "2D")
            self.assertEqual(str(self.choose(system, "A32.87.KJ54.Q876", ("1D", "2D", "P")).call), "2S")
            self.assertEqual(str(self.choose(system, "2.87.AKJ54.KQ876", ("1S",)).call), "2NT")

    def test_michaels_unknown_minor_is_asked_and_can_stop_at_three_clubs(self):
        history = ("1H", "2H", "P")
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "2.87.KJ654.Q9876", history).call), "2NT")
            self.assertEqual(str(self.choose(system, "AQJ87.2.A4.KJ876", history + ("2NT", "P")).call), "3C")
            self.assertEqual(str(self.choose(system, "2.87.KJ654.Q9876", history + ("2NT", "P", "3C", "P")).call), "P")

    def test_lebensohl_weak_diamonds_and_stopped_game_use_the_forced_club_relay(self):
        for system in (self.ccba, self.prec):
            history = ("1NT", "2H")
            weak = "Q32.87.KJ654.876"
            self.assertEqual(str(self.choose(system, weak, history).call), "2NT")
            self.assertEqual(str(self.choose(system, "AKJ.KQ8.Q54.J762", history + ("2NT", "P")).call), "3C")
            self.assertEqual(str(self.choose(system, weak, history + ("2NT", "P", "3C", "P")).call), "3D")
            self.assertEqual(str(self.choose(system, "AQ3.K87.Q54.J876", history).call), "2NT")
            self.assertEqual(str(self.choose(system, "AQ3.K87.Q54.J876", history + ("2NT", "P", "3C", "P")).call), "3NT")

    def test_doubled_nt_escape_transfers_are_not_stayman(self):
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "KJ9876.32.54.876", ("1NT", "X")).call), "2H")
            self.assertEqual(str(self.choose(system, "AKJ.KQ8.Q54.J762", ("1NT", "X", "2H", "P")).call), "2S")

    def test_precision_point_shape_relay_preserves_two_four_card_suits(self):
        history = ("1C", "P", "1NT", "P", "2C", "P")
        self.assertEqual(str(self.choose(self.prec, "KJ87.Q32.54.AQ76", history).call), "3C")
        self.assertEqual(str(self.choose(self.prec, "AK3.KQ8.A54.J762", history + ("3C", "P")).call), "3D")
        self.assertEqual(str(self.choose(self.prec, "KJ87.Q32.54.AQ76", history + ("3C", "P", "3D", "P")).call), "3S")
        self.assertEqual(str(self.choose(self.prec, "KJ87.Q32.A54.Q76", history).call), "2NT")

    def test_precision_two_club_inquiry_shows_the_four_card_major_before_clubs(self):
        history = ("2C", "P", "2D", "P")
        self.assertEqual(str(self.choose(self.prec, "KQ87.32.54.AKJ76", history).call), "2S")
        self.assertEqual(str(self.choose(self.prec, "K87.32.54.AKJ976", history).call), "3C")

    def test_precision_short_diamond_inquiry_distinguishes_void_and_singleton(self):
        history = ("2D", "P", "3D", "P")
        self.assertEqual(str(self.choose(self.prec, "KQ87.KJ98.-.QJ876", history).call), "4C")
        self.assertEqual(str(self.choose(self.prec, "AQ87.KJ98.-.KJ876", history).call), "4D")
        self.assertEqual(str(self.choose(self.prec, "KQ87.KJ98.2.QJ87", history).call), "3H")

    def test_rkcb_queen_question_and_side_king_answer_are_public_continuations(self):
        history = ("1S", "P", "2NT", "P", "3S", "P", "4NT", "P", "5C", "P")
        for system, queen_hand, no_queen_hand in (
            (self.ccba, "Q98765.KJ.AQJ.QJ", "T98765.KQ.AQJ.KJ"),
            (self.prec, "Q98765.KJ.AQ3.QJ", "T98765.KQ.AQ3.QJ"),
        ):
            asker = "AKJT2.AQ2.K54.A2"
            self.assertEqual(str(self.choose(system, asker, history).call), "5D")
            self.assertEqual(str(self.choose(system, queen_hand, history + ("5D", "P")).call), "5H")
            self.assertEqual(str(self.choose(system, no_queen_hand, history + ("5D", "P")).call), "5S")
            self.assertEqual(str(self.choose(system, asker, history + ("5D", "P", "5H", "P")).call), "5NT")
            self.assertEqual(str(self.choose(system, queen_hand, history + ("5D", "P", "5H", "P", "5NT", "P")).call), "6H")
        self.assertEqual(str(self.choose(self.prec, "AKJ87.A32.K54.A2", history + ("5D", "P", "5H", "P")).call), "6S")

    def test_exclusion_question_ignores_the_ace_of_the_publicly_void_suit(self):
        history = ("1S", "P", "2NT", "P", "3S", "P")
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "AKQJ87.AK3.KQ54.-", history).call), "5C")
            self.assertEqual(str(self.choose(system, "T9654.QJ2.J98.A6", history + ("5C", "P")).call), "5D")

    def test_smolen_keeps_the_five_card_major_with_the_nt_opener(self):
        history = ("1NT", "P", "2C", "P", "2D", "P")
        self.assertEqual(str(self.choose(self.ccba, "AKJ87.KQ32.54.76", history).call), "3H")
        self.assertEqual(str(self.choose(self.ccba, "Q32.AJ8.KQ54.A76", history + ("3H", "P")).call), "4S")

    def test_two_nt_minor_stayman_is_not_a_direct_nt_game(self):
        history = ("2NT", "P")
        hand = "32.87.KJ654.Q876"
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, hand, history).call), "3S")
            self.assertEqual(str(self.choose(system, "AK3.AQ8.K54.AJ76", history + ("3S", "P")).call), "4C")

    def test_two_nt_transfer_cannot_be_interpreted_as_one_nt_minor_shortness(self):
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "KQJ987.32.54.876", ("2NT", "P")).call), "3H")
            observation = Observation(parse_hand("AKQ.AJ9.KQ32.J76"), Seat.NORTH, Seat.NORTH,
                Vulnerability.NONE, tuple(map(parse_call, ("2NT", "P", "3H", "P"))))
            context = public_context(system, observation)
            self.assertEqual(context["state"], "transfer")
            self.assertEqual(context["values"]["partner_S_min"], 5)
            self.assertEqual(context["values"]["partner_H_min"], 0)
            self.assertEqual(context["values"]["partner_hcp_min"], 4)

    def test_weak_diamonds_cannot_send_a_natural_bid_reserved_for_a_transfer(self):
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "85.K94.985432.53", ("2NT", "P")).call), "P")
            self.assertEqual(str(self.choose(system, "KJ9876.32.54.876", ("2NT", "P")).call), "3H")

    def test_checkback_preserves_light_opening_hcp_bounds(self):
        history = tuple(map(parse_call, ("P", "P", "1H", "P", "1S", "P", "1NT", "P")))
        for system in (self.ccba, self.prec):
            observation = Observation(parse_hand("KQJ8.32.Q54.8762"), Seat.NORTH, Seat.NORTH,
                                      Vulnerability.NONE, history)
            values = public_context(system, observation)["values"]
            self.assertEqual(values["partner_hcp_min"], 9)
            self.assertEqual(values["partnership_hcp_min"], 15)

    def test_negative_double_shows_unbid_major_and_gets_a_four_card_answer(self):
        for system in (self.ccba, self.prec):
            history = ("1D", "1S")
            self.assertEqual(str(self.choose(system, "32.KJ87.Q54.A876", history).call), "X")
            self.assertEqual(str(self.choose(system, "A3.AQ87.KJ54.876", history + ("X", "P")).call), "2H")

    def test_strength_redouble_does_not_reopen_a_chosen_doubled_contract(self):
        history = ("1D", "2NT", "X", "P", "3H", "P", "P", "X")
        for system in (self.ccba, self.prec):
            result = self.choose(system, "A96.9752.A9732.K", history, dealer=Seat.SOUTH)
            self.assertNotEqual(str(result.call), "XX")

    def test_public_fit_and_hcp_bounds_survive_interference_without_private_cards(self):
        history = tuple(map(parse_call, ("1S", "P", "2NT", "X")))
        for system in (self.ccba, self.prec):
            observation = Observation(parse_hand("AKJ87.A32.K54.A2"), Seat.NORTH, Seat.NORTH,
                                      Vulnerability.NONE, history)
            result = public_context(system, observation)
            self.assertEqual(result["values"]["agreed_trump"], "S")
            self.assertEqual(result["values"]["agreed_trump_min"], 9)
            self.assertEqual(result["values"]["partnership_hcp_min"], 24)
            self.assertEqual(result["values"]["goal"], "game")
            self.assertTrue(result["values"]["must_bid"])
            repeated = public_context(system, replace(observation, hand=parse_hand("KQJ87.Q32.Q54.K2")))
            self.assertEqual(repeated["values"], result["values"])

    def continuation_system(self, goal, captain_points=16):
        raw = self.ccba.to_data()
        parameters = raw["protocol"]["parameters"]
        for name, value in {"captain": "N", "trump": "S", "captain_S_min": 5, "responder_S_min": 3,
                            "captain_hcp_min": captain_points, "responder_hcp_min": 8}.items():
            parameters[name]["initial"] = value
        raw["protocol"] = {"initial": "natural", "parameters": parameters, "states": {"natural": {
            "otherwise": "natural", "transitions": [{"id": "goal", "when": {"op": "eq", "args": [
                {"op": "context", "name": "event.call"}, "1S"]}, "to": "natural", "set": {}, "forcing": goal}],
        }}}
        raw["rules"] = [rule for rule in raw["rules"] if rule["id"].startswith("ccba.natural.")]
        raw["announcements"] = []
        return validate_system(raw)

    def test_unwritten_continuation_uses_agreed_trump_and_committed_level(self):
        for goal, expected in (("game", "4S"), ("small_slam", "6S"), ("grand_slam", "7S")):
            system = self.continuation_system(goal)
            self.assertEqual(str(self.choose(system, "Q32.K87.A54.J876", ("1S", "P")).call), expected)

    def test_unwritten_continuation_can_decline_an_invitation(self):
        system = self.continuation_system("invite_game", captain_points=12)
        self.assertEqual(str(self.choose(system, "Q32.K87.A54.J876", ("1S", "P")).call), "P")

    def test_one_round_continuation_does_not_pretend_to_be_a_game_force(self):
        system = self.continuation_system("one_round", captain_points=12)
        self.assertEqual(str(self.choose(system, "Q32.K87.A54.J876", ("1S", "P")).call), "2S")

    def test_transfer_shows_heart_length_not_artificial_diamonds(self):
        history = tuple(map(parse_call, ("1NT", "P", "2D", "P")))
        observation = Observation(parse_hand("AKQ.JT9.KQ32.876"), Seat.NORTH, Seat.NORTH,
                                  Vulnerability.NONE, history)
        values = public_context(self.ccba, observation)["values"]
        self.assertEqual(values["partner_H_min"], 5)
        self.assertEqual(values["partner_D_min"], 0)
        self.assertEqual(values["partnership_hcp_min"], 15)

    def test_stayman_diamond_denial_does_not_promise_four_diamonds(self):
        observation = Observation(parse_hand("KQ32.AJ87.Q54.87"), Seat.SOUTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, ("1NT", "P", "2C", "P", "2D", "P"))))
        values = public_context(self.ccba, observation)["values"]
        self.assertEqual(values["partner_D_min"], 2)
        self.assertEqual(values["var.captain_H_min"], 2)

    def test_two_nt_responses_do_not_overstate_the_responders_point_lower_bound(self):
        for system in (self.ccba, self.prec):
            self.assertEqual(str(self.choose(system, "Q32.J87.Q54.9876", ("2NT", "P")).call), "3NT")
            self.assertEqual(str(self.choose(system, "KQ3.K87.QJ54.876", ("2NT", "P")).call), "3NT")
            self.assertEqual(str(self.choose(system, "KQ3.K87.QJ54.J76", ("2NT", "P")).call), "4NT")
            self.assertEqual(str(self.choose(system, "KQ3.KJ8.QJ54.J76", ("2NT", "P")).call), "4C")
            for response, expected in (("3NT", 5), ("4C", 12), ("4NT", 12)):
                observation = Observation(parse_hand("AKQ.AJ9.KQ32.J76"), Seat.NORTH, Seat.NORTH,
                    Vulnerability.NONE, tuple(map(parse_call, ("2NT", "P", response, "P"))))
                self.assertEqual(public_context(system, observation)["values"]["partner_hcp_min"], expected)

    def test_raises_and_shortness_answers_keep_conservative_public_bounds(self):
        observation = Observation(parse_hand("KQJ8.32.Q54.8762"), Seat.NORTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, ("P", "P", "1H", "P", "1S", "P", "2S", "P"))))
        self.assertEqual(public_context(self.ccba, observation)["values"]["partner_hcp_min"], 9)
        observation = replace(observation, seat=Seat.SOUTH, history=tuple(map(parse_call, ("1S", "P", "2H", "P", "4H", "P"))))
        self.assertEqual(public_context(self.ccba, observation)["values"]["partner_hcp_min"], 11)
        observation = replace(observation, history=tuple(map(parse_call, ("2C", "P", "2D", "P", "3H", "P"))))
        self.assertEqual(public_context(self.prec, observation)["values"]["var.captain_H_min"], 0)

    def test_strong_club_balanced_game_does_not_claim_a_twenty_five_point_opener(self):
        self.assertEqual(str(self.choose(self.prec, "AKQ8.32.AQ54.K76", ("1C", "P", "1H", "P")).call), "3NT")
        observation = Observation(parse_hand("Q32.KJ987.Q4.987"), Seat.SOUTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, ("1C", "P", "1H", "P", "3NT", "P"))))
        self.assertEqual(public_context(self.prec, observation)["values"]["partner_hcp_min"], 16)

    def test_invitation_after_sixteen_point_nt_can_be_seven_points(self):
        history = ("1C", "P", "1D", "P", "1NT", "P")
        self.assertEqual(str(self.choose(self.prec, "Q32.K87.Q54.9876", history).call), "2NT")
        observation = Observation(parse_hand("AKQ.KT9.KQ32.876"), Seat.NORTH, Seat.NORTH,
            Vulnerability.NONE, tuple(map(parse_call, history + ("2NT", "P"))))
        self.assertEqual(public_context(self.prec, observation)["values"]["partner_hcp_min"], 6)

    def test_negative_double_declares_its_kind_and_only_the_unbid_major(self):
        history = tuple(map(parse_call, ("1D", "1S", "X", "P")))
        observation = Observation(parse_hand("A3.AQ87.KJ54.876"), Seat.NORTH, Seat.NORTH,
                                  Vulnerability.NONE, history)
        values = public_context(self.ccba, observation)["values"]
        self.assertEqual(values["double_kind"], "negative")
        self.assertEqual(values["partner_H_min"], 4)
        self.assertEqual(values["partner_S_min"], 0)

    def test_opponent_bid_cannot_increase_our_captains_suit_bounds(self):
        history = tuple(map(parse_call, ("1C", "1D", "X", "1H")))
        observation = Observation(parse_hand("AKQ.KJ9.KQ32.876"), Seat.NORTH, Seat.NORTH,
                                  Vulnerability.NONE, history)
        values = public_context(self.prec, observation)["values"]
        self.assertEqual(values["var.captain_H_min"], 0)
        self.assertEqual(values["var.responder_H_min"], 0)

    def test_posthoc_auditor_rejects_overstated_public_bounds(self):
        checker = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/verify_classics.py"))["verify_bounds"]
        board = parse_board({"dealer": "N", "vulnerability": "none", "hands": {
            "N": "AKQ.JT9.KQ32.876", "E": "654.876.876.AKQJ", "S": "JT987.AKQ2.AJ.T9", "W": "32.543.T954.5432",
        }})
        step = {"seat": "N", "public_context": {"var.captain": "N", "var.captain_hcp_min": 15,
                "var.responder_hcp_min": 14, "var.captain_S_min": 3}}
        self.assertEqual(checker([step], board), 1)
        step["public_context"]["var.captain_S_min"] = 4
        with self.assertRaisesRegex(ValueError, "Unsound public bound"):
            checker([step], board)
        step["public_context"]["var.captain_S_min"] = 3
        step["public_context"]["var.captain_hcp_max"] = 14
        with self.assertRaisesRegex(ValueError, "Unsound public interval"):
            checker([step], board)
        step["public_context"]["var.captain_hcp_max"] = 15
        widened = {**step, "public_context": {**step["public_context"], "var.captain_hcp_max": 16}}
        with self.assertRaisesRegex(ValueError, "upper bound increased"):
            checker([step, widened], board)

    def test_posthoc_auditor_checks_suit_uppers_relations_and_after_call_updates(self):
        checker = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/verify_classics.py"))["verify_bounds"]
        board = parse_board({"dealer": "N", "vulnerability": "none", "hands": {
            "N": "AKQ.JT9.KQ32.876", "E": "654.876.876.AKQJ",
            "S": "JT987.AKQ2.AJ.T9", "W": "32.543.T954.5432",
        }})
        context = {"var.captain": "N", "var.captain_hcp_min": 15, "var.responder_hcp_min": 14,
                   "var.captain_S_min": 3, "var.captain_S_max": 3, "var.captain_S_ge_H": True}
        step = {"seat": "N", "public_context": context, "public_context_after": dict(context)}
        self.assertEqual(checker([step], board), 1)
        for update, error in (
            ({"var.captain_S_max": 2}, "Unsound public interval"),
            ({"var.captain_S_max": 4}, "upper bound increased"),
            ({"var.captain_S_ge_D": True}, "Unsound public suit relation"),
            ({"var.captain_S_ge_H": False}, "relation forgotten"),
        ):
            step["public_context_after"] = {**context, **update}
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, error):
                checker([step], board)

    def test_negative_double_is_not_repeated_after_partner_has_rebid(self):
        history = ("P", "1C", "1D", "X", "2D", "P", "P", "X", "P", "2S", "P", "P")
        result = self.choose(self.ccba, "J.K732.KJ984.J32", history, dealer=Seat.SOUTH)
        self.assertNotEqual(str(result.call), "X")

    def test_marshall_nt_overcall_is_minor_suits_not_a_balanced_strong_nt(self):
        for system in (self.ccba, self.prec):
            observation = Observation(
                parse_hand("32.A7.KJ654.Q876"), Seat.EAST, Seat.NORTH, Vulnerability.NONE,
                (parse_call("1C"),), public_definitions=(("NS", self.prec), ("EW", system)),
            )
            self.assertEqual(str(decide(system, observation).call), "1NT")
            response = Observation(
                parse_hand("A32.K87.Q54.J876"), Seat.WEST, Seat.NORTH, Vulnerability.NONE,
                tuple(map(parse_call, ("1C", "1NT", "P"))),
                public_definitions=(("NS", self.prec), ("EW", system)),
            )
            self.assertEqual(str(decide(system, response).call), "2C")

    def test_natural_clubs_cannot_use_the_call_reserved_for_landy(self):
        for system in (self.ccba, self.prec):
            observation = Observation(parse_hand("32.K2.AQ3.KQJ987"), Seat.EAST, Seat.NORTH, Vulnerability.NONE,
                (parse_call("1NT"),), public_definitions=(("NS", self.ccba), ("EW", system)))
            self.assertEqual(str(decide(system, observation).call), "3C")
            later = replace(observation, seat=Seat.WEST, history=tuple(map(parse_call, ("1NT", "3C", "P"))))
            values = public_context(system, later)["values"]
            self.assertEqual(values["var.captain_C_min"], 5)
            self.assertEqual(values["var.captain_H_min"], 0)

    def test_balanced_defense_cannot_use_marshalls_artificial_calls_naturally(self):
        for system in (self.ccba, self.prec):
            observation = Observation(parse_hand("AKQ3.K2.AJ54.Q87"), Seat.EAST, Seat.NORTH, Vulnerability.NONE,
                (parse_call("1C"),), public_definitions=(("NS", self.prec), ("EW", system)))
            self.assertEqual(str(decide(system, observation).call), "P")

    def test_high_level_preempt_does_not_trigger_an_unsubstantiated_takeout_double(self):
        board = parse_board({
            "dealer": "N", "vulnerability": "ns",
            "hands": {"N": "AKJT86432.-.J5.74", "E": "-.AKT952.63.QT865",
                      "S": "Q75.J763.AQ74.32", "W": "9.Q84.KT982.AKJ9"},
        })
        result = run_auction(self.ccba, self.prec, board, compact=True, trace=True)
        self.assertEqual([step["call"] for step in result["auction"]], ["4S", "P", "P", "P"])
        self.assertEqual(result["evaluation"], "not_evaluated")

    def test_gambling_three_nt_corrects_at_four_level_instead_of_rebidding_below_the_contract(self):
        board = parse_board({
            "dealer": "N", "vulnerability": "none",
            "hands": {"N": "974.K8.86542.A84", "E": "AQJ86.J73.3.Q752",
                      "S": "3.T52.AKQJT97.63", "W": "KT52.AQ964.-.KJT9"},
        })
        for ns, ew in ((self.ccba, self.prec), (self.prec, self.ccba)):
            result = run_auction(ns, ew, board, compact=True, trace=True)
            calls = [step["call"] for step in result["auction"]]
            self.assertIn("3NT", calls)
            self.assertIn("4C", calls)
            self.assertIn("4D", calls)

    def test_compact_and_full_execution_have_identical_calls_contracts_and_rule_ids(self):
        board = parse_board({
            "dealer": "N", "vulnerability": "none",
            "hands": {"N": "AKQ.JT9.KQ32.876", "E": "654.876.876.AKQJ",
                      "S": "JT987.AKQ2.AJ.T9", "W": "32.543.T954.5432"},
        })
        full = run_auction(self.ccba, self.prec, board, trace=True)
        compact = run_auction(self.ccba, self.prec, board, trace=True, compact=True)
        self.assertEqual(full["contract"], compact["contract"])
        self.assertEqual(
            [(step["call"], step["private_trace"]["rule_id"]) for step in full["auction"]],
            [(step["call"], step["private_trace"]["rule_id"]) for step in compact["auction"]],
        )
        south = next(step for step in compact["auction"] if step["seat"] == "S")
        self.assertEqual(south["public_context"]["partnership_hcp_min"], 15)
        self.assertEqual(south["private_trace"]["inference"]["partnership_hcp_min"], 30)
        self.assertIn("goal", compact["partnership_contexts"]["NS"])
        self.assertIn("agreed_trump_min", compact["partnership_contexts"]["NS"])

    def test_frozen_ir_roundtrip_and_explicit_nonrepairing_root_fallback(self):
        for system in (self.ccba, self.prec):
            self.assertEqual(validate_system(system.to_data()).content_hash, system.content_hash)
            self.assertEqual(system.fallback.kind, "error")
            self.assertIn("工程移植", system.name)

    def test_if_is_typed_short_circuit_and_public_taint_cannot_hide_in_unused_branch(self):
        expression = compile_expression({"op": "if", "args": [True, 3, 4]}, version=2)
        observation = Observation(parse_hand("AKQ.JT9.KQ32.876"), Seat.NORTH, Seat.NORTH, Vulnerability.NONE, ())
        self.assertEqual(Evaluation(observation, "idle").evaluate(expression), 3)
        guarded = compile_expression({"op": "if", "args": [True, 3, {"op": "keycards", "args": ["NT"]}]}, version=2)
        self.assertEqual(Evaluation(observation, "idle").evaluate(guarded), 3)
        with self.assertRaises(SystemError):
            compile_expression({"op": "if", "args": [True, 3, False]}, version=2)
        raw = self.ccba.to_data()
        first = raw["protocol"]["states"]["idle"]["transitions"][0]
        first["set"]["open_min"] = {"op": "if", "args": [True, 11, {"op": "hcp"}]}
        with self.assertRaisesRegex(SystemError, "private"):
            validate_system(raw)

    def test_independent_development_boards_execute_both_layouts_without_invalid_calls(self):
        rng = random.Random(8294031)
        deck = list(full_deck())
        for index in range(128):
            rng.shuffle(deck)
            board = Board(Deal(*(Hand(tuple(deck[start:start + 13])) for start in (0, 13, 26, 39))),
                          list(Seat)[index % 4], list(Vulnerability)[(index + index // 4) % 4])
            for ns, ew in ((self.ccba, self.prec), (self.prec, self.ccba)):
                result = run_auction(ns, ew, board, compact=True)
                self.assertTrue(result["auction"])
                self.assertEqual(result["auction"][-1]["call"], "P")
                self.assertEqual(result["evaluation"], "not_evaluated")


if __name__ == "__main__":
    unittest.main()
