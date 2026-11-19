import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_evolve.systems import decide, load_system, validate_system
from bridge_evolve.systems.compact import compact_system
from bridge_evolve.systems.inputs import parse_observation
from bridge_evolve.systems.navigation import at_pointer, query_system, system_index
from bridge_evolve.systems.review import review_position
from systems.test_v2 import ctx, op, raw_system

ROOT = Path(__file__).resolve().parents[2]


class NavigationTests(unittest.TestCase):
    def system(self):
        raw = raw_system()
        raw["features"] = {"enough": op("ge", {"op": "hcp"}, 12), "alias": {"op": "ref", "name": "enough"}}
        raw["rules"] = [
            {"id": "test.open", "priority": 20, "when": op("and", op("eq", {"op": "state"}, "idle"),
             {"op": "ref", "name": "alias"}), "action": {"kind": "call", "call": "1S"}},
            {"id": "test.answer", "priority": 10, "when": op("eq", {"op": "state"}, "answer"),
             "action": {"kind": "call", "call": "2S"}},
        ]
        raw["protocol"] = {"initial": "idle", "parameters": {
            "promised": {"type": "number", "initial": 0},
        }, "states": {
            "idle": {"otherwise": "idle", "transitions": [
                {"id": "show", "when": op("eq", ctx("event.call"), "1S"),
                 "to": "answer", "set": {"promised": 5}},
            ]},
            "answer": {"otherwise": "answer", "transitions": []},
        }}
        return validate_system(raw)

    def test_catalog_search_and_transitive_dependencies_use_semantic_keys(self):
        system = self.system()
        catalog = query_system(system, {})
        self.assertEqual(catalog["counts"]["rule"], 2)
        self.assertEqual(catalog["system_hash"], system.content_hash)
        search = query_system(system, {"view": "search", "state": "idle", "call": "1S", "kind": "rule"})
        self.assertEqual([item["key"] for item in search["matches"]["items"]], ["rule:test.open"])
        read = query_system(system, {"view": "read", "keys": ["rule:test.open"]})
        self.assertEqual({item["key"] for item in read["definitions"]["items"]},
                         {"rule:test.open", "feature:alias", "feature:enough"})
        self.assertTrue(read["dependencies"]["complete_in_response"])
        users = query_system(system, {"view": "references", "key": "feature:enough", "transitive": True})
        self.assertEqual({item["key"] for item in users["users"]["items"]}, {"feature:alias", "rule:test.open"})
        read["definitions"]["items"][0]["data"]["id"] = "cannot-mutate-source"
        self.assertEqual(system_index(system).nodes["rule:test.open"].data["id"], "test.open")

    def test_state_filter_handles_or_not_and_shared_guards_conservatively(self):
        raw = self.system().to_data()
        raw["features"]["other"] = op("not", op("eq", {"op": "state"}, "idle"))
        raw["rules"][0]["when"] = op("and", {"op": "ref", "name": "other"}, op("ge", {"op": "hcp"}, 15))
        system = validate_system(raw)
        idle = query_system(system, {"view": "search", "state": "idle", "kind": "rule"})
        self.assertEqual(idle["matches"]["total"], 0)
        answer = query_system(system, {"view": "search", "state": "answer", "kind": "rule"})
        self.assertEqual(answer["matches"]["total"], 2)

    def test_every_full_definition_is_the_actual_normalized_source_for_v1_and_v2(self):
        for system in (self.system(), load_system(ROOT / "systems/1nt-v1.json")):
            raw = system.to_data()
            index = system_index(system)
            for key, node in index.nodes.items():
                with self.subTest(version=system.version, key=key):
                    definition = index.descriptor(key, definition=True)
                    self.assertEqual(definition["data"], at_pointer(raw, node.pointer))
                    self.assertEqual(definition["data_kind"], "source_node")
            state = index.descriptor("state:" + system.initial, definition=True)
            if system.version == 2:
                self.assertNotIn("on", state["data"])
                self.assertIsInstance(state["data"]["transitions"][0], dict)
                self.assertEqual(state["transition_keys"], ["transition:idle/show"])
            else:
                self.assertIn("on", state["data"])
                self.assertNotIn("transitions", state["data"])

    def test_state_sections_follow_actual_transitions_and_only_selected_assignments(self):
        read = query_system(self.system(), {"view": "read", "keys": ["state:idle"],
                                           "sections": ["/transitions/0/set/promised"]})
        item = read["definitions"]["items"][0]
        self.assertEqual(item["data"], {"/transitions/0/set/promised": 5})
        self.assertEqual(item["data_kind"], "sections")
        self.assertNotIn("transition_keys", item)
        self.assertEqual({item["key"] for item in read["definitions"]["items"]},
                         {"state:idle", "parameter:promised"})

    def test_oversized_states_omit_source_and_links_instead_of_returning_fake_ir(self):
        raw = raw_system()
        raw["protocol"]["states"]["idle"]["transitions"] = [
            {"id": f"large.{index}", "when": False, "to": "idle", "set": {}} for index in range(600)]
        read = query_system(validate_system(raw), {"view": "read", "keys": ["state:idle"]})
        item = read["definitions"]["items"][0]
        self.assertIn("definition_omitted", item)
        self.assertNotIn("data", item)
        self.assertNotIn("transition_keys", item)
        self.assertEqual(item["summary"]["transition_count"], 600)
        self.assertLess(len(json.dumps(read).encode()), 48000)

    def test_auction_lookup_never_reads_a_hand_or_uses_private_sender_as_public_meaning(self):
        system = self.system()
        with patch("bridge_evolve.systems.runtime.PublicView.hand", new_callable=unittest.mock.PropertyMock,
                   side_effect=AssertionError("Private hand read")):
            result = query_system(system, {"view": "auction", "history": [], "call": "1S"})
        meaning = result["public_meaning"]
        self.assertEqual(meaning["changes"]["var.promised"]["after"], 5)
        self.assertNotIn("decision", result)
        self.assertEqual(meaning["source_keys"], ["transition:idle/show"])
        self.assertEqual(result["compatible_rules"]["total"], 1)

    def test_hand_decision_is_exact_and_kept_outside_the_public_meaning(self):
        system = self.system()
        observation = {"hand": "AKQ.JT9.KQ32.876", "seat": "N", "dealer": "N",
                       "vulnerability": "none", "history": []}
        result = query_system(system, {"view": "hand", "observation": observation})
        self.assertEqual(result["decision"]["call"], str(decide(system, parse_observation(observation)).call))
        self.assertEqual(result["decision"]["rule_key"], "rule:test.open")
        public = query_system(system, {"view": "auction", "call": "1S"})
        self.assertEqual(result["public_meaning"], public["public_meaning"])
        self.assertTrue(result["private"])

    def test_human_review_resolves_dynamic_public_calls_without_reading_or_selecting_a_hand(self):
        raw = raw_system()
        raw["protocol"]["parameters"]["negative_call"] = {"type": "string", "initial": "1C"}
        choose = op("if", op("eq", {"op": "var", "name": "negative_call"}, "1C"), "C", "D")
        raw["rules"] = [
            {"id": "internal.negative", "priority": 20, "when": op("le", {"op": "hcp"}, 7),
             "action": {"kind": "bid", "level": 1, "strain": choose}},
            {"id": "internal.positive", "priority": 10, "when": op("ge", {"op": "hcp"}, 8),
             "action": {"kind": "bid", "level": 1, "strain": op("if",
                        op("eq", {"op": "var", "name": "negative_call"}, "1C"), "D", "H")}},
        ]
        with patch("bridge_evolve.systems.runtime.PublicView.hand", new_callable=unittest.mock.PropertyMock,
                   side_effect=AssertionError("Private hand read")):
            result = review_position(validate_system(raw), {"call": "1C"})
        self.assertEqual(len(result["choices"]), 38)
        self.assertEqual(len(result["selected"]["senders"]), 1)
        self.assertIn("大牌点至多7点", result["selected"]["senders"][0]["condition"])
        self.assertNotIn("negative_call", result["selected"]["senders"][0]["condition"])
        self.assertEqual(result["selected"]["public_meaning"]["after"]["var.negative_call"], "1C")
        self.assertFalse(any("hand" in key or "decision" in key for key in result))
        status = {item["call"]: item["status"] for item in result["choices"]}
        self.assertEqual(status["1D"], "defined")
        self.assertEqual(status["1S"], "unused")
        self.assertEqual(status["XX"], "illegal")
        raw["rules"] = [{"id": "private.choice", "priority": 1, "when": True, "action": {
            "kind": "bid", "level": op("if", op("le", {"op": "hcp"}, 7), 1, 2),
            "strain": op("if", op("le", {"op": "hcp"}, 7), "C", "D")}}]
        split = review_position(validate_system(raw), {"call": "1C"})
        statuses = {item["call"]: item["status"] for item in split["choices"]}
        self.assertEqual(statuses["2D"], "defined")
        self.assertEqual(statuses["1D"], "unused")
        self.assertEqual(statuses["2C"], "unused")
        raw["rules"][0]["action"] = {"kind": "cheapest", "min_level": 1, "max_level": 7, "strains": {
            "op": "list", "item_type": "string",
            "args": [op("if", op("ge", {"op": "length", "suit": "S"}, {"op": "length", "suit": "H"}), "S", "H")]}}
        split = review_position(validate_system(raw), {"call": "1S"})
        self.assertIn("黑桃不少于红桃", split["selected"]["senders"][0]["condition"])
        self.assertEqual(split["selected"]["status"], "defined")
        self.assertEqual(split["issues"], [])
        raw["rules"][0]["when"] = op("in", op("if",
            op("ge", {"op": "length", "suit": "S"}, {"op": "length", "suit": "H"}), "S", "H"), ["S"])
        described = review_position(validate_system(raw), {"call": "1S"})
        self.assertIn("黑桃不少于红桃", described["selected"]["senders"][0]["condition"])
        self.assertNotIn('则"S"', described["selected"]["senders"][0]["condition"])

    def test_review_preserves_priority_exclusions_and_does_not_hide_unimplemented_hand_partitions(self):
        raw = raw_system()
        raw["rules"] = [
            {"id": "strong", "priority": 20, "when": op("ge", {"op": "hcp"}, 16),
             "action": {"kind": "call", "call": "1C"}},
            {"id": "other", "priority": 10, "when": True, "action": {"kind": "call", "call": "1D"}},
        ]
        result = review_position(validate_system(raw), {"call": "1D"})
        self.assertIn("大牌点至少16点", result["selected"]["senders"][0]["exclusions"])
        self.assertEqual(result["selected"]["senders"][0]["exclusion_details"],
                         [{"condition": "大牌点至少16点", "calls": ["1C"]}])
        raw["rules"][0]["when"] = True
        covered = review_position(validate_system(raw), {"call": "1D"})
        self.assertEqual(covered["selected"]["status"], "unused")
        raw["rules"] = raw["rules"][:1]
        raw["rules"][0]["when"] = op("ge", {"op": "hcp"}, 16)
        raw["fallback"] = {"kind": "error", "message": "No weak response implemented"}
        missing = review_position(validate_system(raw), {"call": "1D"})
        self.assertEqual(missing["selected"]["status"], "missing")
        self.assertIn("未实现", missing["issues"][0]["message"])

    def test_review_associates_same_call_and_dynamic_priority_branches_without_private_hands(self):
        raw = raw_system()
        raw["rules"] = [
            {"id": "preferred", "priority": 20, "when": op("ge", {"op": "hcp"}, 16),
             "action": {"kind": "call", "call": "1D"}},
            {"id": "other", "priority": 10, "when": True, "action": {"kind": "call", "call": "1D"}},
        ]
        with patch("bridge_evolve.systems.runtime.PublicView.hand", new_callable=unittest.mock.PropertyMock,
                   side_effect=AssertionError("Private hand read")):
            result = review_position(validate_system(raw), {"call": "1D"})
            self.assertEqual(result["selected"]["senders"][1]["exclusion_details"],
                             [{"condition": "大牌点至少16点", "calls": ["1D"]}])
            raw["rules"][0]["action"] = {"kind": "bid", "level": 1, "strain": op("if",
                op("ge", {"op": "length", "suit": "S"}, {"op": "length", "suit": "H"}), "S", "H")}
            dynamic = review_position(validate_system(raw), {"call": "1D"})
            detail = dynamic["selected"]["senders"][0]["exclusion_details"][0]
            self.assertEqual(detail["calls"], ["1H", "1S"])
            self.assertEqual(detail["condition"], "大牌点至少16点")
            raw["rules"][0]["action"]["level"] = op("add", {"op": "hcp"}, 1)
            unknown = review_position(validate_system(raw), {"call": "1D"})
            self.assertEqual(unknown["selected"]["senders"][0]["exclusion_details"][0]["calls"], [])
            self.assertIn("动态叫品", unknown["issues"][0]["message"])
            raw["rules"][0]["action"] = {"kind": "error", "message": "Unimplemented preferred branch"}
            missing = review_position(validate_system(raw), {"call": "1D"})
            self.assertEqual(missing["selected"]["senders"][0]["exclusion_details"][0],
                             {"condition": "大牌点至少16点", "calls": []})
            self.assertIn("未实现", missing["issues"][0]["message"])

    def test_review_exposes_unexecutable_steps_without_substituting_a_call(self):
        raw = raw_system()
        raw["rules"] = [{"id": "overflow", "priority": 1, "when": True,
                         "action": {"kind": "step", "after": "7NT", "count": 1}}]
        result = review_position(validate_system(raw), {"call": "P"})
        self.assertEqual(result["selected"]["senders"], [])
        self.assertEqual(result["selected"]["status"], "missing")
        self.assertIn("exceeds 7NT", result["issues"][0]["details"][0])

    def test_review_renders_publicly_bound_suit_lengths_and_exposes_unbound_suits(self):
        raw = raw_system()
        raw["protocol"]["parameters"]["chosen_suit"] = {"type": "string", "initial": "S"}
        raw["rules"] = [{"id": "dynamic.length", "priority": 1,
                         "when": op("ge", {"op": "length", "suit": {"op": "var", "name": "chosen_suit"}}, 5),
                         "action": {"kind": "call", "call": "1C"}}]
        known = review_position(validate_system(raw), {"call": "1C"})
        self.assertEqual(known["selected"]["status"], "defined")
        self.assertEqual(known["selected"]["senders"][0]["condition"], "黑桃至少5张")
        raw["protocol"]["parameters"]["chosen_suit"]["initial"] = ""
        unknown = review_position(validate_system(raw), {"call": "1C"})
        self.assertEqual(unknown["selected"]["status"], "missing")
        self.assertIn("尚未确定", unknown["selected"]["senders"][0]["condition"])

    def test_review_switches_partnerships_and_never_uses_our_protocol_to_explain_an_unknown_opponent(self):
        system = self.system()
        unknown = review_position(system, {"history": ["P"], "call": "1S"}, side="NS")
        self.assertIsNone(unknown["acting_system"])
        self.assertEqual(unknown["selected"]["status"], "missing")
        self.assertIsNone(unknown["selected"]["public_meaning"])
        known = review_position(system, {"history": ["P"], "call": "1S"}, side="NS", definitions={"EW": system})
        self.assertEqual(known["acting_system"]["hash"], system.content_hash)
        self.assertEqual(known["selected"]["status"], "defined")

    def test_review_historical_turn_ignores_future_calls_and_ended_positions_have_no_legal_next_call(self):
        system = self.system()
        earlier = review_position(system, {"history": ["1S", "P", "2S"], "turn": 1})
        direct = review_position(system, {"call": "1S"})
        self.assertEqual(earlier["selected"], direct["selected"])
        ended = review_position(system, {"history": ["P"] * 4})
        self.assertTrue(ended["ended"])
        self.assertTrue(all(item["status"] == "illegal" for item in ended["choices"]))

    def test_selected_turn_does_not_read_later_calls(self):
        system = self.system()
        selected = query_system(system, {"view": "auction", "history": ["1S", "P", "2S"], "turn": 1})
        direct = query_system(system, {"view": "auction", "call": "1S"})
        self.assertEqual(selected["public_meaning"], direct["public_meaning"])
        self.assertEqual(selected["history"], [])

    def test_semantic_diff_ignores_array_reordering_and_compaction(self):
        original = self.system()
        raw = original.to_data()
        raw["rules"].reverse()
        reordered = validate_system(raw)
        diff = query_system(reordered, {"view": "diff", "kind": "rule"}, compare_to=original)
        self.assertEqual(diff["changes"]["total"], 0)
        compact = compact_system(original)
        diff = query_system(compact, {"view": "diff"}, compare_to=original)
        self.assertEqual(diff["changes"]["total"], 0)
        raw["features"]["enough"]["args"][1] = 15
        changed = validate_system(raw)
        diff = query_system(changed, {"view": "diff", "kind": "rule"}, compare_to=original)
        self.assertEqual([item["key"] for item in diff["changes"]["items"]], ["rule:test.open"])

    def test_real_expression_sharing_and_reordered_public_assignments_are_not_semantic_changes(self):
        raw = self.system().to_data()
        repeated = op("and", *(op("ge", {"op": "hcp"}, value) for value in range(6, 16)))
        raw["features"]["long"] = copy.deepcopy(repeated)
        for rule in raw["rules"]:
            rule["when"] = op("and", rule["when"], copy.deepcopy(repeated))
        original = validate_system(raw)
        compact = compact_system(original)
        self.assertTrue(any(name.startswith("shared.") for name, _ in compact.features))
        self.assertEqual(query_system(compact, {"view": "diff"}, compare_to=original)["changes"]["total"], 0)
        raw["protocol"]["parameters"]["other"] = {"type": "number", "initial": 0}
        transitions = raw["protocol"]["states"]["idle"]["transitions"]
        transitions[0]["set"]["other"] = 1
        transitions.append({"id": "pass", "when": op("eq", ctx("event.call"), "P"),
                            "to": "idle", "set": {}})
        original = validate_system(raw)
        transitions[0]["set"] = dict(reversed(list(transitions[0]["set"].items())))
        transitions.reverse()
        reordered = validate_system(raw)
        self.assertEqual(query_system(reordered, {"view": "diff"}, compare_to=original)["changes"]["total"], 0)

    def test_transition_component_escaping_prevents_ambiguous_stable_ids(self):
        raw = raw_system()
        raw["protocol"]["states"] = {
            "idle": {"otherwise": "idle", "transitions": [
                {"id": "one", "when": op("eq", ctx("event.call"), "1C"), "to": "a/b", "set": {}},
                {"id": "two", "when": op("eq", ctx("event.call"), "1D"), "to": "a", "set": {}},
            ]},
            "a/b": {"otherwise": "a/b", "transitions": [
                {"id": "c", "when": False, "to": "a/b", "set": {}}]},
            "a": {"otherwise": "a", "transitions": [
                {"id": "b/c", "when": False, "to": "a", "set": {}}]},
        }
        index = system_index(validate_system(raw))
        self.assertIn("transition:a~1b/c", index.nodes)
        self.assertIn("transition:a/b~1c", index.nodes)

    def test_error_actions_are_not_reported_as_possible_senders(self):
        raw = self.system().to_data()
        raw["rules"].append({"id": "missing", "priority": 50, "when": True,
                             "action": {"kind": "error", "message": "not implemented"}})
        result = query_system(validate_system(raw), {"view": "auction", "call": "1S"})
        self.assertNotIn("rule:missing", {item["key"] for item in result["compatible_rules"]["items"]})

    def test_blank_systems_have_valid_empty_searches(self):
        blank = validate_system(raw_system())
        result = query_system(blank, {"view": "search", "kind": "rule", "state": "idle"})
        self.assertEqual(result["matches"]["total"], 0)
        with self.assertRaises(ValueError):
            query_system(blank, {"view": "search", "kind": "invented"})

    def test_public_filter_does_not_evaluate_a_private_guarded_dynamic_call(self):
        raw = self.system().to_data()
        raw["protocol"]["parameters"]["future_call"] = {"type": "string", "initial": ""}
        raw["rules"].insert(0, {"id": "guarded", "priority": 50,
            "when": op("and", op("ge", {"op": "hcp"}, 30),
                       {"op": "is_legal", "call": {"op": "var", "name": "future_call"}}),
            "action": {"kind": "call", "call": "2NT"}})
        system = validate_system(raw)
        review = review_position(system, {"call": "2NT"})
        self.assertEqual(review["selected"]["status"], "missing")
        self.assertIn("动态叫品", review["selected"]["senders"][0]["condition"])
        observation = {"hand": "AKQ.JT9.KQ32.876", "seat": "N", "dealer": "N",
                       "vulnerability": "none", "history": []}
        result = query_system(system, {"view": "hand", "observation": observation})
        self.assertEqual(result["decision"]["call"], "1S")
        self.assertEqual(str(decide(system, parse_observation(observation)).call), "1S")
        observation["hand"] = "AKQJ.AKQ.AKQ.AKQ"
        with self.assertRaises(ValueError):
            query_system(system, {"view": "hand", "observation": observation})

    def test_edit_preconditions_identify_the_current_source_after_reordering(self):
        raw = self.system().to_data()
        raw["rules"].reverse()
        result = query_system(validate_system(raw), {"view": "search", "query": "test.open", "kind": "rule"})
        guard = result["matches"]["items"][0]["edit_precondition"]
        self.assertEqual(guard["op"], "test")
        self.assertEqual(at_pointer(raw, guard["path"]), guard["value"])

    def test_parameters_and_transition_interfaces_are_in_the_dependency_view(self):
        read = query_system(self.system(), {"view": "read", "keys": ["transition:idle/show"]})
        self.assertIn("parameter:promised", {item["key"] for item in read["definitions"]["items"]})
        self.assertEqual({item["id"] for item in read["interfaces"]["states"]}, {"idle", "answer"})

    def test_selected_sections_do_not_pull_in_unrelated_shared_definitions(self):
        system = self.system()
        read = query_system(system, {"view": "read", "keys": ["rule:test.open"], "sections": ["/action"]})
        self.assertEqual(read["dependencies"]["total"], 0)
        self.assertEqual(read["definitions"]["items"][0]["data"], {"/action": {"kind": "call", "call": "1S"}})
        read = query_system(system, {"view": "read", "keys": ["transition:idle/show"], "sections": ["/set/promised"]})
        self.assertEqual({item["key"] for item in read["definitions"]["items"]},
                         {"transition:idle/show", "parameter:promised"})

    def test_bad_queries_and_ended_auctions_are_explicit_errors(self):
        system = self.system()
        for request in (
            {"view": "unknown"}, {"view": "read"}, {"view": "read", "keys": ["rule:missing"]},
            {"view": "search", "state": "missing"}, {"view": "catalog", "limit": True},
            {"view": "catalog", "extra": 1}, {"view": "diff"},
            {"view": "auction", "history": ["P"] * 4}, {"view": "auction", "history": [], "turn": 1},
        ):
            with self.subTest(request=request), self.assertRaises((ValueError, KeyError)):
                query_system(system, request)

    def test_large_system_queries_fit_one_tool_response_and_do_not_change_ir(self):
        system = load_system(ROOT / "systems/classics-v5.5.4-compact/prec.json")
        identity = system.content_hash
        catalog = query_system(system, {"view": "catalog", "limit": 100})
        self.assertLess(len(json.dumps(catalog, ensure_ascii=False).encode()), 48000)
        lookup = query_system(system, {"view": "auction", "history": ["1NT", "P"], "call": "4NT"},
                              definitions={"EW": system})
        self.assertEqual(lookup["public_meaning"]["state_before"], "nt")
        self.assertTrue(lookup["public_meaning"]["after"]["var.nt_slam_invited"])
        read = query_system(system, {"view": "read", "keys": lookup["public_meaning"]["source_keys"][:3]})
        self.assertLess(len(json.dumps(read, ensure_ascii=False).encode()), 48000)
        self.assertLess(len(json.dumps(lookup, ensure_ascii=False).encode()), 48000)
        self.assertEqual(system.content_hash, identity)
        self.assertEqual(system.to_data(), system_index(system).raw)


if __name__ == "__main__":
    unittest.main()
