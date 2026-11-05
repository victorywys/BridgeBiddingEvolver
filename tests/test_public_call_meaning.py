import copy
import io
import json
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from bridge_evolve.core import Seat, Vulnerability
from bridge_evolve.systems import load_system, validate_system
from bridge_evolve.systems.inputs import protocol_record
from bridge_evolve.systems.language import parse_call
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld
from test_board_viewer import ROOT, sample_run, viewer, write_smoke_run
from systems.test_v2 import ctx, op, raw_system


class PublicCallMeaningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ccba = load_system(ROOT / "systems/classics-v5.3-compact/ccba.json")
        cls.prec = load_system(ROOT / "systems/classics-v5.3-compact/prec.json")

    def public_steps(self, calls, precision=False, dealer=Seat.NORTH):
        definitions = {"NS": self.prec if precision else self.ccba, "EW": self.ccba if precision else self.prec}
        world = PublicWorld(definitions, dealer, Vulnerability.NONE)
        steps = []
        for text in calls.split():
            seat = world.auction.next_seat
            side = seat.partnership.value
            before = protocol_record(PublicContext(world, side, seat).snapshot())
            call = parse_call(text)
            world.step(call)
            after = protocol_record(PublicContext(world, side, seat).snapshot())
            steps.append({"seat": seat.value, "call": text, "public_context": before, "public_context_after": after})
        return steps, definitions

    def explain(self, calls, precision=False):
        steps, definitions = self.public_steps(calls, precision)
        result = viewer.explain_public_auction(steps, "N", "none", definitions)
        self.assertTrue(all(item["basis"] == "frozen-public-protocol" and not item["warnings"] for item in result), result)
        return result

    def test_precision_diamond_explains_range_major_denial_and_club_upper(self):
        meaning = self.explain("1D", precision=True)[0]
        self.assertIn("11–15大牌点", meaning["summary"])
        self.assertIn("方块至少2张", meaning["summary"])
        self.assertIn("没有五张高花", meaning["summary"])
        self.assertIn("草花最多5张", meaning["summary"])
        self.assertTrue(meaning["new_information"])

    def test_quantitative_four_nt_is_distinct_from_gerber_retreat(self):
        quantitative = self.explain("1NT P 4NT", precision=True)[-1]
        self.assertIn("定量邀贯", quantitative["purpose"])
        self.assertIn("允许搭档拒绝", quantitative["purpose"])
        retreat = self.explain("1NT P 4C P 4S P 4NT", precision=True)[-1]
        self.assertIn("Gerber问A后的无将止叫", retreat["purpose"])
        self.assertIn("不是定量邀贯", retreat["purpose"])
        accepted = self.explain("1NT P 4NT P 6NT", precision=True)[-1]
        self.assertIn("接受无将定量邀贯", accepted["purpose"])
        declined = self.explain("1NT P 4NT P P", precision=True)[-1]
        self.assertIn("拒绝无将定量邀贯", declined["purpose"])

    def test_five_card_positive_and_four_card_negative_are_not_confused(self):
        positive = self.explain("1C P 2D P 2S", precision=True)[-1]
        negative = self.explain("1C P 1D P 1S", precision=True)[-1]
        self.assertIn("黑桃至少5张", positive["summary"])
        self.assertIn("黑桃至少4张", negative["summary"])
        self.assertNotIn("黑桃至少5张", negative["summary"])

    def test_repeat_call_retains_five_cards_without_claiming_six(self):
        meaning = self.explain("1S P 1NT P 2S")[-1]
        self.assertIn("黑桃至少5张", meaning["summary"])
        self.assertNotIn("黑桃至少6张", meaning["summary"])
        self.assertTrue(any("黑桃至少5张" in text for text in meaning["retained_information"]))
        self.assertFalse(any("黑桃至少5张" in text for text in meaning["new_information"]))

    def test_suit_relation_and_derived_intervals_are_readable(self):
        meaning = self.explain("1S P 1NT P 2H")[-1]
        self.assertIn("黑桃不少于红桃", meaning["summary"])
        self.assertIn("黑桃5–9张", meaning["summary"])
        self.assertIn("红桃4–6张", meaning["summary"])

    def test_transfer_does_not_claim_the_literal_diamond_suit(self):
        meaning = self.explain("1NT P 2D")[-1]
        self.assertIn("转移", meaning["purpose"])
        self.assertIn("红桃", meaning["purpose"])
        self.assertIn("红桃至少5张", meaning["summary"])
        self.assertNotIn("方块至少5张", meaning["summary"])
        self.assertTrue(any("字面" in text for text in meaning["notes"]))

    def test_stayman_question_and_denial_do_not_invent_natural_suits(self):
        meanings = self.explain("1NT P 2C P 2D")
        self.assertIn("高花问叫", meanings[2]["purpose"])
        self.assertNotIn("草花至少", meanings[2]["summary"])
        self.assertIn("否定", meanings[-1]["purpose"])
        self.assertNotIn("方块至少5张", meanings[-1]["summary"])

    def test_pass_is_limited_before_opening_but_not_automatically_in_competition(self):
        opening = self.explain("P", precision=True)[0]
        self.assertIn("最多10大牌点", opening["summary"])
        competition = self.explain("1NT P")[1]
        self.assertNotIn("最多10大牌点", competition["summary"])
        self.assertFalse(competition["new_information"])

    def test_negative_double_explains_public_kind_suit_and_partner_obligation(self):
        meaning = self.explain("1D 1S X")[-1]
        self.assertIn("否定性加倍", meaning["purpose"])
        self.assertIn("红桃至少4张", meaning["summary"])
        self.assertTrue(any("北家" in text and "履行答叫义务" in text for text in meaning["notes"]))

    def test_strength_redouble_has_its_public_hcp_guarantee(self):
        meaning = self.explain("1D X XX")[-1]
        self.assertIn("实力再加倍", meaning["purpose"])
        self.assertIn("至少10大牌点", meaning["summary"])

    def test_escape_redouble_is_not_mislabeled_as_strength(self):
        meaning = self.explain("1NT X XX")[-1]
        self.assertIn("逃叫再加倍", meaning["purpose"])
        self.assertNotIn("至少10大牌点", meaning["summary"])
        self.assertTrue(any("答叫义务" in text for text in meaning["notes"]))

    def test_four_nt_requires_an_actual_public_keycard_transition(self):
        meaning = self.explain("1S P 2NT P 4NT")[-1]
        self.assertNotEqual(meaning["purpose"], "关键张问叫")
        raw = raw_system()
        raw["protocol"] = {"initial": "jacoby", "parameters": {}, "states": {
            "jacoby": {"otherwise": "jacoby", "transitions": [
                {"id": "ccba.jacoby.ask_keys.t1", "when": op("eq", ctx("event.call"), "4NT"), "to": "rkcb", "set": {}},
            ]},
            "rkcb": {"otherwise": "rkcb", "transitions": []},
        }}
        system = validate_system(raw)
        described = viewer.explain_public_auction([{"seat": "N", "call": "4NT"}], "N", "none", {"NS": system, "EW": system})[0]
        self.assertEqual(described["purpose"], "关键张问叫")

    def test_drury_reply_is_not_mislabeled_as_a_natural_diamond_suit(self):
        meaning = self.explain("P P 1H P 2C P 2D")[-1]
        self.assertIn("Drury", meaning["purpose"])
        self.assertNotIn("方块至少5张", meaning["summary"])

    def test_keycard_reply_is_a_lower_bound_not_an_exact_hidden_count(self):
        meanings = self.explain("1S P 2NT P 3S P 4NT P 5C")
        self.assertIn("关键张问叫", meanings[6]["purpose"])
        self.assertTrue(any("自然无将止叫" in text for text in meanings[6]["notes"]))
        self.assertIn("关键张", meanings[-1]["purpose"])
        self.assertTrue(any("下界" in text and "确切" in text for text in meanings[-1]["notes"]))
        self.assertNotIn("草花至少5张", meanings[-1]["summary"])

    def test_keycard_answer_buckets_and_queen_denial_are_public_not_private_counts(self):
        prefix = "1S P 2NT P 3S P 4NT P "
        for call, count, queen in (("5C", "1／4", ""), ("5D", "0／3", ""),
                                   ("5H", "2／5", "明确不含将牌Q"), ("5S", "2／5", "明确含将牌Q")):
            with self.subTest(call=call):
                meaning = self.explain(prefix + call)[-1]
                self.assertIn(count + "个关键张档", meaning["summary"])
                self.assertTrue(any(count in text for text in meaning["new_information"]))
                if queen:
                    self.assertIn(queen, meaning["summary"])
                else:
                    self.assertNotIn("不含将牌Q", meaning["summary"])
                    self.assertNotIn("明确含将牌Q", meaning["summary"])

    def test_gerber_answers_distinguish_aces_from_five_keycards(self):
        for call, phrase in (("4D", "0／4个A档"), ("4H", "显示1个A"), ("4S", "显示2个A"), ("4NT", "显示3个A")):
            with self.subTest(call=call):
                meaning = self.explain("1NT P 4C P " + call)[-1]
                self.assertIn(phrase, meaning["summary"])
                self.assertNotIn("个关键张档", meaning["summary"])

    def test_exclusion_half_steps_are_not_mislabeled_as_extra_keycards(self):
        for call, phrase in (("5S", "显示0个关键张"), ("5NT", "显示1个关键张，明确不含将牌Q"),
                             ("6C", "显示1个关键张，明确含将牌Q")):
            with self.subTest(call=call):
                meaning = self.explain("1S P 2NT P 3S P 5H P " + call)[-1]
                self.assertIn("不计红桃A", meaning["summary"])
                self.assertIn(phrase, meaning["summary"])
                self.assertNotIn("1.5个关键张", meaning["summary"])

    def test_explicit_queen_question_can_deny_q_without_confusing_other_false_flags(self):
        prefix = "1S P 2NT P 3S P 4NT P 5C P 5D P "
        for call, phrase in (("5S", "明确否认将牌Q"), ("6S", "公开保证有将牌Q"),
                             ("6C", "显示草花K")):
            with self.subTest(call=call):
                meaning = self.explain(prefix + call)[-1]
                self.assertIn(phrase, meaning["summary"])

    def test_splinter_explains_shortness_and_does_not_hide_the_terminal_bug(self):
        meaning = self.explain("1H P 3NT")[-1]
        self.assertIn("短套", meaning["purpose"])
        self.assertIn("黑桃单缺", meaning["summary"])
        self.assertNotIn("自然无将开叫", meaning["summary"])
        self.assertTrue(any("执行账本" in text and "正确性" in text for text in meaning["notes"]))

    def test_shortness_reply_and_later_natural_call_are_not_confused(self):
        short = self.explain("2C P 2D P 3S", precision=True)[-1]
        self.assertIn("黑桃单缺", short["summary"])
        self.assertIn("草花至少6张", short["summary"])
        later = self.explain("1H P 3NT P 4H")[-1]
        self.assertNotIn("短套显示", later["purpose"])

    def test_explicit_4441_reply_is_not_mislabeled_as_natural_nt(self):
        meanings = self.explain("1C P 2D P 3NT P P", precision=True)
        reply = meanings[4]
        self.assertIn("4441", reply["purpose"])
        self.assertIn("方块单张", reply["summary"])
        self.assertTrue(any("自然无将止叫" in text for text in reply["notes"]))
        self.assertIn("接受4441", meanings[-1]["purpose"])

    def test_pass_retains_old_guarantees_without_repeating_them_as_new(self):
        meaning = self.explain("1S P 2S P P P")[4]
        self.assertFalse(meaning["new_information"])
        self.assertIn("未记录新的量化", meaning["summary"])
        self.assertNotIn("黑桃至少5张", meaning["summary"])
        self.assertIn("黑桃至少5张", meaning["retained_information"])

    def test_final_pass_explains_the_public_contract_without_using_dds(self):
        meaning = self.explain("1S P 2S P P P")[-1]
        self.assertTrue(any("竞叫结束" in text and "2黑桃" in text and "北家做庄" in text for text in meaning["notes"]))

    def test_old_missing_after_snapshot_is_explicit_and_never_written_back(self):
        steps, definitions = self.public_steps("1D", precision=True)
        del steps[0]["public_context_after"]
        original = copy.deepcopy(steps)
        meaning = viewer.explain_public_auction(steps, "N", "none", definitions)[0]
        self.assertIn("没有五张高花", meaning["summary"])
        self.assertTrue(any("旧日志未保存后态" in text for text in meaning["warnings"]))
        self.assertEqual(steps, original)

    def test_replay_mismatch_is_not_laundered_into_a_verified_guarantee(self):
        steps, definitions = self.public_steps("1S")
        steps[0]["public_context_after"]["var.captain_S_min"] = 6
        meaning = viewer.explain_public_auction(steps, "N", "none", definitions)[0]
        self.assertEqual(meaning["basis"], "recorded-public-information")
        self.assertTrue(meaning["warnings"])
        self.assertIn("所附公共快照记为", meaning["summary"])

    def test_private_rule_hand_and_dds_are_not_inputs_to_the_explanation(self):
        steps, definitions = self.public_steps("1D", precision=True)
        first = viewer.explain_public_auction(steps, "N", "none", definitions)
        altered = copy.deepcopy(steps)
        altered[0].update(hand="AKQJT98765432.-.-.-",
                          private_trace={"rule_id": "PRIVATE.SECRET", "inference": {"hcp": 37}})
        with patch.object(viewer, "run_auction", side_effect=AssertionError("Must not run a private policy")), \
                patch.object(viewer, "DDSAdapter", side_effect=AssertionError("Must not query DDS")):
            second = viewer.explain_public_auction(altered, "N", "none", definitions)
        self.assertEqual(first, second)
        self.assertNotIn("PRIVATE.SECRET", str(second))
        self.assertNotIn("37大牌点", str(second))

    def test_future_public_calls_do_not_rewrite_the_meaning_of_an_earlier_prefix(self):
        first = self.explain("1S P 1NT P 2H P 2S")
        second = self.explain("1S P 1NT P 2H P 3H")
        self.assertEqual(first[:5], second[:5])

    def test_missing_definitions_are_explicit_instead_of_guessing_a_convention(self):
        meaning = viewer.explain_public_auction([{"seat": "N", "call": "1C"}], "N", "none")[0]
        self.assertEqual(meaning["basis"], "recorded-public-information")
        self.assertTrue(meaning["warnings"])
        self.assertFalse(meaning["new_information"])

    def test_imported_prose_is_rebuilt_not_trusted_as_a_verified_explanation(self):
        bundle = viewer.assemble_run(*sample_run(), "fixture")
        bundle["boards"][0]["tables"][0]["auction"][0]["public_explanation"] = {
            "basis": "frozen-public-protocol", "summary": "PRIVATE.SECRET",
        }
        imported = viewer.import_json(json.dumps(bundle).encode(), "import.json")
        meaning = imported["boards"][0]["tables"][0]["auction"][0]["public_explanation"]
        self.assertNotIn("PRIVATE.SECRET", meaning["summary"])
        self.assertEqual(meaning["basis"], "recorded-public-information")
        self.assertTrue(meaning["warnings"])

    def test_zip_uses_matching_embedded_frozen_definitions(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "fixture"
            run = write_smoke_run(directory)
            contents = io.BytesIO()
            with zipfile.ZipFile(contents, "w") as archive:
                for name in ("run.json", "dataset-boards.jsonl", "boards.jsonl", "auctions.jsonl",
                             *(f"systems/{run[role]['hash']}.json" for role in ("system_a", "system_b"))):
                    archive.writestr("frozen/" + name, (directory / name).read_bytes())
            imported = viewer.import_file(contents.getvalue(), "fixture.zip")
            meanings = [step["public_explanation"] for table in imported["boards"][0]["tables"] for step in table["auction"]]
            self.assertTrue(all(meaning["basis"] == "frozen-public-protocol" for meaning in meanings))

    def test_wrong_registered_definition_identity_is_an_explicit_error(self):
        bundle = viewer.assemble_run(*sample_run(), "fixture")
        with self.assertRaisesRegex(ValueError, "definition hashes"):
            viewer.annotate_public_meanings(bundle["boards"][0], {"a": self.ccba, "b": self.prec})


if __name__ == "__main__":
    unittest.main()
