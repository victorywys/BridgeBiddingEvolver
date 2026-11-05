import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from bridge_evolve.core import Call, Contract, Deal, Hand, Seat, Strain, Vulnerability, full_deck, legal_calls
from bridge_evolve.systems import compile_expression, decide, load_system, public_context, render_call, render_system, validate_system
from bridge_evolve.systems.inputs import Board, decision_data, parse_board, parse_observation, run_auction, run_uncontested
from bridge_evolve.systems.language import Announcement, SystemError, load_json, parse_call
from bridge_evolve.systems.runtime import Evaluation, Observation, _decide_and_advance, replay
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld, build_world, resolve_action
from bridge_evolve.systems.v2_language import compile_action
from systems.support import ROOT, observation


def op(name, *args):
    return {"op": name, "args": list(args)}


def ctx(name):
    return {"op": "context", "name": name}


def var(name):
    return {"op": "var", "name": name}


def raw_system():
    return {
        "schema_version": 2, "operator_version": 2, "name": "v2 test", "scope": "general",
        "features": {}, "announcements": [],
        "protocol": {"initial": "idle", "parameters": {}, "states": {
            "idle": {"transitions": [], "otherwise": "idle"},
        }},
        "rules": [], "fallback": {"kind": "call", "call": "P"},
    }


def rule(condition, action, identifier="rule", priority=1):
    return {"id": identifier, "priority": priority, "when": condition, "action": action}


def transition(identifier, condition, **fields):
    return {"id": identifier, "when": condition, "to": "idle", "set": {}, **fields}


def expression(raw, variables=None):
    return compile_expression(raw, version=2, variables=variables)


def obs(system, history=(), seat=None, hand="AKQ.KT9.KQ32.876", vulnerability=Vulnerability.NONE, other=None):
    calls = tuple(parse_call(call) for call in history)
    auction = replay(Seat.NORTH, calls)
    acting = seat or auction.next_seat
    definitions = ()
    if other is not None:
        own_side = acting.partnership.value
        definitions = ((own_side, system), ("EW" if own_side == "NS" else "NS", other))
    return Observation(
        parse_observation({
            "hand": hand, "seat": acting.value, "dealer": "N",
            "vulnerability": vulnerability.value, "history": list(history),
        }).hand,
        acting, Seat.NORTH, vulnerability, calls, (), definitions,
    )


class V2ValidationTests(unittest.TestCase):
    def test_impossible_public_hcp_interval_is_an_explicit_error(self):
        raw = raw_system()
        raw["protocol"]["parameters"] = {"captain_hcp_min": {"type": "number", "initial": 11},
                                         "captain_hcp_max": {"type": "number", "initial": 15}}
        raw["protocol"]["states"]["idle"]["transitions"] = [transition("bad_interval", True, set={"captain_hcp_max": 10})]
        world = PublicWorld({"NS": validate_system(raw)}, Seat.NORTH, Vulnerability.NONE)
        with self.assertRaisesRegex(SystemError, "Inconsistent public HCP interval"):
            world.step(parse_call("1C"))

    def test_all_eight_calling_goals_have_distinct_pass_obligations(self):
        for goal, required in (("none", False), ("one_round", True), ("invite_game", False),
                               ("game", True), ("slam_try", True), ("small_slam", True),
                               ("invite_grand", True), ("grand_slam", True)):
            raw = raw_system()
            raw["protocol"]["states"]["idle"]["transitions"] = [
                transition("start", op("eq", ctx("event.call"), "1S"), forcing=goal),
            ]
            world = PublicWorld({"NS": validate_system(raw)}, Seat.NORTH, Vulnerability.NONE)
            world.step(parse_call("1S"))
            world.step(parse_call("P"))
            context = PublicContext(world, "NS", Seat.SOUTH)
            self.assertEqual(context.value("goal"), goal)
            self.assertEqual(context.value("must_bid"), required)
            with self.subTest(goal=goal):
                if required:
                    with self.assertRaisesRegex(SystemError, "forcing"):
                        world.step(parse_call("P"))
                else:
                    world.step(parse_call("P"))

    def test_slam_commitments_survive_game_and_resolve_only_at_the_required_level(self):
        for goal, final in (("small_slam", "6S"), ("invite_grand", "6S"), ("grand_slam", "7S")):
            raw = raw_system()
            raw["protocol"]["states"]["idle"]["transitions"] = [
                transition("start", op("eq", ctx("event.call"), "1S"), forcing=goal),
            ]
            world = PublicWorld({"NS": validate_system(raw)}, Seat.NORTH, Vulnerability.NONE)
            for call in ("1S", "P", "4S", "P"):
                world.step(parse_call(call))
            self.assertTrue(PublicContext(world, "NS", Seat.NORTH).value("must_bid"))
            for call in (final, "P", "P", "P"):
                world.step(parse_call(call))
            self.assertEqual(world.frames["NS"].forcing, "none")
            self.assertEqual(world.frames["NS"].goal, goal)

    def test_lower_goals_and_probe_answers_cannot_erase_a_grand_slam_commitment(self):
        for intermediate in ("one_round", "slam_try", "invite_game", "game"):
            raw = raw_system()
            raw["protocol"]["states"]["idle"]["transitions"] = [
                transition("start", op("eq", ctx("event.call"), "1S"), forcing="grand_slam"),
                transition("continue", op("eq", ctx("event.call"), "2C"), forcing=intermediate),
            ]
            world = PublicWorld({"NS": validate_system(raw)}, Seat.NORTH, Vulnerability.NONE)
            for call in ("1S", "P", "2C", "P"):
                world.step(parse_call(call))
            self.assertEqual(world.frames["NS"].goal, "grand_slam")
            self.assertTrue(PublicContext(world, "NS", Seat.NORTH).value("must_bid"))

    def test_roundtrip_hash_and_v1_stability(self):
        system = load_system(ROOT / "systems/general-v2.json")
        self.assertEqual(validate_system(json.loads(json.dumps(system.to_data()))), system)
        self.assertEqual(system.content_hash, validate_system(system.to_data()).content_hash)
        self.assertEqual(render_system(system), render_system(validate_system(system.to_data())))
        self.assertIn("captain_hcp_min", render_system(system))
        self.assertIn("last_bid_by", render_system(system))
        self.assertEqual(
            load_system(ROOT / "systems/1nt-v1.json").content_hash,
            "81a5fc2ce1feb94ba15aa4e777eaf50b48120d05cdd90cc5a1483a783b53fb95",
        )

    def test_private_data_cannot_drive_public_transitions_or_declarations_even_via_aliases(self):
        private = [
            {"op": "hcp"}, op("gt", {"op": "length", "suit": "S"}, 4),
            op("eq", {"op": "role"}, "dealer"), op("eq", ctx("partner_seat"), "S"),
            {"op": "ref", "name": "private"},
        ]
        for condition in private:
            raw = raw_system()
            raw["features"] = {"private": op("ge", {"op": "hcp"}, 10)}
            raw["protocol"]["states"]["idle"]["transitions"] = [transition("bad", condition)]
            with self.subTest(condition=condition):
                with self.assertRaises(SystemError):
                    validate_system(raw)
            raw["protocol"]["states"]["idle"]["transitions"] = []
            raw["announcements"] = [{"id": "bad", "when": condition, "tags": ["natural"]}]
            with self.assertRaises(SystemError):
                validate_system(raw)

    def test_event_values_are_not_available_in_decisions_or_announcement_tag_cycles(self):
        raw = raw_system()
        raw["rules"] = [rule(op("eq", ctx("event.call"), "1C"), {"kind": "call", "call": "P"})]
        with self.assertRaisesRegex(SystemError, "event"):
            validate_system(raw)
        raw["rules"] = []
        raw["announcements"] = [{"id": "cycle", "when": op("in", "natural", ctx("event.tags")), "tags": ["natural"]}]
        with self.assertRaisesRegex(SystemError, "public phase"):
            validate_system(raw)

    def test_public_assignments_cannot_read_private_features_and_must_be_typed(self):
        for assignment in ({"op": "hcp"}, True, var("unknown")):
            raw = raw_system()
            raw["protocol"]["parameters"] = {"x": {"type": "number", "initial": 0}}
            raw["protocol"]["states"]["idle"]["transitions"] = [
                transition("bad", True, set={"x": assignment}),
            ]
            with self.subTest(assignment=assignment):
                with self.assertRaises(SystemError):
                    validate_system(raw)

    def test_invalid_context_parameters_states_actions_and_domains_fail(self):
        for mutate in (
            lambda raw: raw["rules"].append(rule(op("eq", ctx("secret"), ""), {"kind": "call", "call": "P"})),
            lambda raw: raw["rules"].append(rule(op("eq", ctx("state"), "undefined"), {"kind": "call", "call": "P"})),
            lambda raw: raw["rules"].append(rule(True, {"kind": "bid", "level": True, "strain": "S"})),
            lambda raw: raw["protocol"]["parameters"].update({"trump": {"type": "string", "initial": "NT", "values": ["NT"]}}),
            lambda raw: raw["protocol"]["parameters"].update({"x": {"type": "number", "initial": 0, "values": [True]}}),
            lambda raw: raw["protocol"]["parameters"].update({"x": {"type": "string", "initial": "", "values": ["S"]}}),
            lambda raw: raw["protocol"]["states"]["idle"].update({"otherwise": "undefined"}),
        ):
            raw = raw_system()
            mutate(raw)
            with self.assertRaises(SystemError):
                validate_system(raw)

    def test_v1_rejects_v2_features_and_dynamic_fields(self):
        for value in (ctx("opponents_vulnerable"), var("trump"), {"op": "length", "suit": ctx("last_bid_strain")}):
            with self.assertRaises(SystemError):
                compile_expression(value)


class PublicContextTests(unittest.TestCase):
    def setUp(self):
        self.system = validate_system(raw_system())
        self.other = validate_system(raw_system() | {"name": "opponent"})

    def values(self, history=(), **kwargs):
        return public_context(self.system, obs(self.system, history, other=self.other, **kwargs))["values"]

    def test_both_vulnerabilities_and_all_seats_are_distinguishable(self):
        for count in range(4):
            for vulnerability in Vulnerability:
                values = self.values(("P",) * count, vulnerability=vulnerability)
                seat = list(Seat)[count]
                self.assertEqual(values["own_vulnerable"], vulnerability.is_vulnerable(seat))
                self.assertEqual(values["opponents_vulnerable"], vulnerability.is_vulnerable(seat.next()))
                self.assertEqual(values["vulnerability"], vulnerability.value)
        self.assertNotEqual(
            self.values(vulnerability=Vulnerability.NONE)["opponents_vulnerable"],
            self.values(vulnerability=Vulnerability.EW)["opponents_vulnerable"],
        )
        raw = raw_system()
        raw["rules"] = [rule(ctx("opponents_vulnerable"), {"kind": "call", "call": "X"})]
        defender = validate_system(raw)
        self.assertEqual(str(decide(defender, obs(defender, ("1C",), vulnerability=Vulnerability.NS)).call), "X")
        self.assertEqual(str(decide(defender, obs(defender, ("1C",), vulnerability=Vulnerability.NONE)).call), "P")

    def test_dynamic_opening_roles_passed_status_and_public_flow(self):
        values = self.values(("P", "P", "1H", "P"))
        self.assertEqual(values["opener"], "S")
        self.assertEqual(values["responder"], "N")
        self.assertEqual(values["dynamic_role"], "responder")
        self.assertEqual(values["opening_position"], 3)
        self.assertTrue(values["passed_before_opening"])
        values = self.values(("1C", "X", "1H"))
        self.assertEqual(values["overcaller"], "E")
        self.assertEqual(values["advancer"], "W")
        self.assertEqual(values["dynamic_role"], "advancer")
        self.assertEqual(values["last_actor"], "S")
        self.assertEqual(values["last_bid"], "1H")
        self.assertEqual(values["opponent_bid_strain"], "H")
        self.assertEqual(values["unbid_suits"], ["D", "S"])

    def profile(self, artificial):
        raw = raw_system()
        raw["announcements"] = [{
            "id": "opening", "when": op("and", op("not", {"op": "has_contract_bid"}), op("eq", ctx("event.call"), "1C")),
            "tags": ["artificial", "strong_club"] if artificial else ["natural"],
            **({} if artificial else {"suit": "C"}),
        }]
        return validate_system(raw)

    def test_opponent_public_meaning_changes_a_decision_without_hidden_information(self):
        raw = raw_system()
        raw["rules"] = [rule(
            {"op": "announced", "which": "opening", "tag": "strong_club"},
            {"kind": "call", "call": "X"},
        )]
        defender = validate_system(raw)
        self.assertEqual(str(decide(defender, obs(defender, ("1C",), other=self.profile(True))).call), "X")
        self.assertEqual(str(decide(defender, obs(defender, ("1C",), other=self.profile(False))).call), "P")
        with self.assertRaisesRegex(SystemError, "public definition"):
            decide(defender, obs(defender, ("1C",)))
        raw["rules"] = []
        raw["protocol"]["parameters"] = {"seen": {"type": "bool", "initial": False}}
        raw["protocol"]["states"]["idle"]["transitions"] = [
            transition("remember_opponent", op("and",
                op("eq", ctx("event.by"), "ours"),
                {"op": "announced", "which": "opponents", "tag": "strong_club"},
            ), set={"seen": True}),
        ]
        observer = validate_system(raw)
        world = PublicWorld({"NS": self.profile(True), "EW": observer}, Seat.NORTH, Vulnerability.NONE)
        world.step(parse_call("1C"))
        world.step(Call.pass_call())
        self.assertTrue(world.frames["EW"].parameters["seen"])
        self.assertEqual(PublicContext(world, "EW", Seat.EAST).frame.parameters,
                         PublicContext(world, "EW", Seat.WEST).frame.parameters)

    def test_declared_suit_is_not_the_physical_artificial_bid_suit(self):
        raw = raw_system()
        raw["announcements"] = [{
            "id": "transfer", "when": op("eq", ctx("event.call"), "2D"),
            "tags": ["artificial", "transfer"], "suit": "H",
        }]
        system = validate_system(raw)
        result = public_context(system, obs(system, ("1NT", "P", "2D", "P")))
        self.assertEqual(result["values"]["partner_bid_strain"], "D")
        self.assertEqual(result["values"]["partner_shown_suit"], "H")

    def test_non_contract_call_annotations_include_takeout_doubles(self):
        opponent_raw = raw_system()
        opponent_raw["announcements"] = [{
            "id": "takeout", "when": op("eq", ctx("event.call"), "X"), "tags": ["takeout_double"],
        }]
        own_raw = raw_system()
        own_raw["rules"] = [rule(
            {"op": "announced", "which": "opponent_call", "tag": "takeout_double"},
            {"kind": "call", "call": "1D"},
        )]
        system = validate_system(own_raw)
        current = obs(system, ("1C", "X"), other=validate_system(opponent_raw))
        self.assertEqual(str(decide(system, current).call), "1D")

    def test_private_hands_do_not_change_public_context_and_public_evaluator_has_no_hand(self):
        sample = obs(self.system, ("1C", "P"), other=self.other)
        changed = replace(sample, hand=observation("9876.987.987.987").hand)
        self.assertEqual(public_context(self.system, sample), public_context(self.system, changed))
        raw = raw_system()
        declaration = Announcement("invalid", expression(op("ge", {"op": "hcp"}, 0)), ("natural",))
        forged = replace(self.system, announcements=(declaration,))
        world = PublicWorld({"NS": forged}, Seat.NORTH, Vulnerability.NONE)
        with self.assertRaisesRegex(SystemError, "private hand"):
            world.step(parse_call("1C"))

    def test_conflicting_public_declarations_fail_instead_of_selecting_a_private_branch(self):
        for announcements in (
            [{"id": "a", "when": True, "tags": ["natural"]}, {"id": "b", "when": True, "tags": ["artificial"]}],
            [{"id": "a", "when": True, "tags": [], "suit": "H"}, {"id": "b", "when": True, "tags": [], "suit": "S"}],
        ):
            system = validate_system(raw_system() | {"announcements": announcements})
            world = PublicWorld({"NS": system}, Seat.NORTH, Vulnerability.NONE)
            with self.assertRaisesRegex(SystemError, "Conflicting"):
                world.step(parse_call("1C"))

    def test_own_public_definition_must_match_and_unknown_fields_are_rejected(self):
        with self.assertRaisesRegex(SystemError, "differs"):
            public_context(self.system, replace(obs(self.system), public_definitions=(("NS", self.other),)))
        raw = load_json(ROOT / "examples/general-observation.json")
        raw["public_definitions"] = {"NS": self.system.to_data(), "EW": self.other.to_data()}
        self.assertEqual(len(parse_observation(raw).public_definitions), 2)
        raw["seed"] = 123
        with self.assertRaises(SystemError):
            parse_observation(raw)


class ParameterAndActionTests(unittest.TestCase):
    def setUp(self):
        self.demo = load_system(ROOT / "systems/general-v2.json")

    def test_the_same_keycard_protocol_works_for_all_four_trumps(self):
        for trump in "CDHS":
            raw = raw_system()
            raw["protocol"]["parameters"] = {
                "trump": {"type": "string", "initial": trump, "values": ["C", "D", "H", "S"]},
            }
            raw["features"] = {"keys": {"op": "keycards", "args": [var("trump")]}}
            raw["rules"] = [rule(
                op("ge", {"op": "ref", "name": "keys"}, 0),
                {"kind": "bid", "level": 6, "strain": var("trump")},
            )]
            system = validate_system(raw)
            current = obs(system)
            result = decide(system, current)
            self.assertEqual(str(result.call), "6" + trump)
            key_node = next(node for node, _ in result.trace.values if node.op == "keycards")
            actual = dict(result.trace.values)[key_node]
            expected = sum(card.rank == "A" or (card.suit == trump and card.rank == "K") for card in current.hand.cards)
            self.assertEqual(actual, expected)
            self.assertIn(trump, render_system(system))

    def test_public_updates_are_simultaneous_and_domains_checked_at_runtime(self):
        raw = raw_system()
        raw["protocol"]["parameters"] = {
            "x": {"type": "number", "initial": 1}, "y": {"type": "number", "initial": 0},
        }
        raw["protocol"]["states"]["idle"]["transitions"] = [
            transition("copy_old_x", True, set={"x": op("add", var("x"), 1), "y": var("x")}),
        ]
        system = validate_system(raw)
        values = public_context(system, obs(system, ("1C",)))["values"]
        self.assertEqual((values["var.x"], values["var.y"]), (2, 1))
        raw["protocol"]["parameters"]["x"]["values"] = [1]
        with self.assertRaisesRegex(SystemError, "domain"):
            public_context(validate_system(raw), obs(validate_system(raw), ("1C",)))

    def test_partner_reconstruction_agrees_and_no_cross_deal_state_survives(self):
        raw = self.demo
        after_fit = ("1S", "P", "3S", "P")
        sample = obs(raw, after_fit)
        first = public_context(raw, sample)
        # PublicWorld itself is independent of whose hand/turn will be queried.
        a = build_world(raw, sample)
        b = build_world(raw, replace(sample, hand=observation("9876.987.987.987").hand))
        self.assertEqual(a.frames["NS"].parameters, b.frames["NS"].parameters)
        self.assertEqual(PublicContext(a, "NS", Seat.NORTH).frame.parameters,
                         PublicContext(a, "NS", Seat.SOUTH).frame.parameters)
        self.assertEqual(first["values"]["agreed_trump"], "S")
        self.assertEqual(public_context(raw, obs(raw))["values"]["agreed_trump"], "")
        self.assertEqual(public_context(raw, sample), first)

    def test_cheapest_order_is_global_not_input_order_and_empty_candidates_fail(self):
        system = validate_system(raw_system())
        current = obs(system, ("1H",))
        world = build_world(system, current)
        evaluation = Evaluation(current, "idle", PublicContext(world, "EW", Seat.EAST))
        action = compile_action(
            {"kind": "cheapest", "strains": ["S", "C"], "min_level": 1, "max_level": 7},
            expression,
        )
        self.assertEqual(str(resolve_action(action, evaluation)), "1S")
        dynamic = compile_action(
            {"kind": "cheapest", "strains": ctx("unbid_suits"), "min_level": 2, "max_level": 2},
            expression,
        )
        self.assertEqual(str(resolve_action(dynamic, evaluation)), "2C")
        empty = compile_action(
            {"kind": "cheapest", "strains": {"op": "list", "item_type": "string", "args": []}, "min_level": 1, "max_level": 7},
            expression,
        )
        with self.assertRaisesRegex(SystemError, "No legal"):
            resolve_action(empty, evaluation)

    def test_step_actions_cross_strains_nt_and_fail_above_7nt_or_when_illegal(self):
        for after, count, expected in (("4NT", 1, "5C"), ("5S", 1, "5NT"), ("5NT", 1, "6C")):
            system = validate_system(raw_system() | {"fallback": {"kind": "step", "after": after, "count": count}})
            self.assertEqual(str(decide(system, obs(system, (after,))).call), expected)
        for after, count in (("7NT", 1), ("4NT", 0), ("P", 1)):
            system = validate_system(raw_system() | {"fallback": {"kind": "step", "after": after, "count": count}})
            with self.assertRaises(SystemError):
                decide(system, obs(system))
        system = validate_system(raw_system() | {"fallback": {"kind": "step", "after": "1C", "count": 1}})
        with self.assertRaisesRegex(SystemError, "illegal"):
            decide(system, obs(system, ("1NT",)))

    def test_one_shared_public_question_and_answer_protocol_handles_every_trump(self):
        raw = self.demo.to_data()
        condition = raw["protocol"]["states"]["idle"]["transitions"][0]["when"]
        condition["args"][-1]["args"][1] = ["C", "D", "H", "S"]
        system = validate_system(raw)
        for trump, expected in (("C", "5H"), ("D", "5D"), ("H", "5H"), ("S", "5H")):
            history = ("1" + trump, "P", "3" + trump, "P", "4NT", "P")
            current = obs(system, history, hand="T87.AJ9.AKJ9.T94")
            self.assertEqual(public_context(system, current)["values"]["agreed_trump"], trump)
            self.assertEqual(str(decide(system, current).call), expected)

    def test_dynamic_suits_control_honors_legal_guard_and_explicit_unimplemented_actions(self):
        raw = raw_system()
        raw["protocol"]["parameters"] = {
            "suit": {"type": "string", "initial": "S"}, "call": {"type": "string", "initial": "1S"},
        }
        raw["rules"] = [rule(op("and",
            op("ge", {"op": "length", "suit": var("suit")}, 3),
            op("eq", {"op": "honors", "suit": var("suit"), "ranks": "AKQ"}, 3),
            {"op": "control", "suit": var("suit"), "round": 1, "shortage": False},
            {"op": "is_legal", "call": var("call")},
        ), {"kind": "bid", "level": 1, "strain": var("suit")})]
        system = validate_system(raw)
        self.assertEqual(str(decide(system, obs(system)).call), "1S")
        self.assertIn("公开协议参数", render_system(system))
        raw["protocol"]["parameters"]["suit"]["initial"] = ""
        with self.assertRaisesRegex(SystemError, "defined"):
            decide(validate_system(raw), obs(validate_system(raw)))
        system = validate_system(raw_system() | {"fallback": {"kind": "error", "message": "Uncovered branch"}})
        with self.assertRaisesRegex(SystemError, "Uncovered branch"):
            decide(system, obs(system))

    def test_conflicting_transitions_fail_even_if_the_targets_match(self):
        raw = raw_system()
        raw["protocol"]["states"]["idle"]["transitions"] = [
            transition("a", True), transition("b", True),
        ]
        system = validate_system(raw)
        with self.assertRaisesRegex(SystemError, "Conflicting public transitions"):
            decide(system, obs(system))

    def test_public_meaning_does_not_depend_on_private_rule_hit(self):
        raw = raw_system()
        raw["rules"] = [rule(
            op("ge", {"op": "hcp"}, 15),
            {"kind": "cheapest", "strains": ["C", "D"], "min_level": 1, "max_level": 1},
        )]
        raw["fallback"] = {"kind": "call", "call": "1C"}
        system = validate_system(raw)
        a, b = decide(system, obs(system)), decide(system, obs(system, hand="9876.987.987.987"))
        self.assertEqual(a.call, b.call)
        self.assertNotEqual(a.trace.rule_id, b.trace.rule_id)
        self.assertEqual(render_call(system, a.call), render_call(system, b.call))


class ForcingTests(unittest.TestCase):
    def forcing_system(self, forcing):
        raw = raw_system()
        raw["protocol"]["states"]["idle"]["transitions"] = [
            transition("force", op("and",
                op("eq", ctx("event.by"), "ours"), op("eq", ctx("event.call"), "1C"),
            ), forcing=forcing),
        ]
        return raw

    def test_forcing_is_not_a_bridge_legality_rule_and_one_round_expires_on_the_target(self):
        system = validate_system(self.forcing_system("one_round"))
        current = obs(system, ("1C", "P"))
        self.assertIn(Call.pass_call(), current.legal_actions)
        with self.assertRaisesRegex(SystemError, "forcing violation"):
            decide(system, current)
        result = public_context(system, obs(system, ("1C", "P", "1D", "P")))
        self.assertEqual(result["values"]["forcing"], "none")

    def test_game_force_survives_a_nested_one_round_request(self):
        raw = self.forcing_system("game")
        raw["protocol"]["states"]["idle"]["transitions"].append(
            transition("relay", op("and",
                op("eq", ctx("event.by"), "ours"), op("eq", ctx("event.call"), "1D"),
            ), forcing="one_round"),
        )
        system = validate_system(raw)
        current = obs(system, ("1C", "P", "1D", "P", "1H", "P"))
        self.assertEqual(public_context(system, current)["values"]["forcing"], "game")
        with self.assertRaisesRegex(SystemError, "forcing violation"):
            decide(system, current)
        result = public_context(system, obs(system, ("1C", "P", "3NT", "P")))
        self.assertEqual(result["values"]["forcing"], "none")

    def test_explicit_forcing_pass_is_allowed_but_cannot_end_with_an_unresolved_force(self):
        raw = self.forcing_system("game")
        raw["protocol"]["states"]["idle"]["transitions"].append(
            transition("forcing_pass", op("eq", ctx("event.call"), "P"), allow_pass=True),
        )
        system = validate_system(raw)
        self.assertEqual(str(decide(system, obs(system, ("1C", "P"))).call), "P")
        world = PublicWorld({"NS": system}, Seat.NORTH, Vulnerability.NONE)
        for call in ("1C", "P", "P"):
            world.step(parse_call(call))
        with self.assertRaisesRegex(SystemError, "unresolved"):
            world.step(Call.pass_call())

    def test_interference_requires_an_explicit_force_exit(self):
        raw = self.forcing_system("game")
        raw["protocol"]["states"]["idle"]["transitions"].append(
            transition("competitive_exit", op("and",
                op("eq", ctx("event.by"), "opponents"), op("eq", ctx("event.kind"), "BID"),
            ), forcing="none"),
        )
        system = validate_system(raw)
        current = obs(system, ("1C", "1S"))
        self.assertEqual(str(decide(system, current).call), "P")
        self.assertEqual(public_context(system, current)["values"]["forcing"], "none")


class GeneralRunnerTests(unittest.TestCase):
    def setUp(self):
        self.pass_system = load_system(ROOT / "systems/pass-v2.json")
        self.demo = load_system(ROOT / "systems/general-v2.json")
        self.board = parse_board(load_json(ROOT / "examples/general-board.json"))

    def test_incremental_auctions_match_independent_prefix_reconstruction_exactly(self):
        def reconstructed(system, observation, world):
            decision = _decide_and_advance(system, observation, build_world(system, observation))
            world.step(decision.call)
            return decision

        prefix = tuple(map(parse_call, ("1S", "P", "3S", "P", "4NT", "P")))
        for compact in (False, True):
            for history in ((), prefix):
                with self.subTest(compact=compact, prefix=history):
                    actual = run_auction(self.demo, self.pass_system, self.board,
                                         compact=compact, trace=True, prefix=history)
                    with patch("bridge_evolve.systems.runtime._decide_and_advance", side_effect=reconstructed):
                        reference = run_auction(self.demo, self.pass_system, self.board,
                                                compact=compact, trace=True, prefix=history)
                    self.assertEqual(actual, reference)

    def test_runner_does_not_rebuild_the_public_world_for_each_decision(self):
        with patch("bridge_evolve.systems.v2_runtime.build_world", side_effect=AssertionError("unexpected replay")):
            result = run_auction(self.demo, self.pass_system, self.board, compact=True, trace=True)
        self.assertEqual(result["contract"]["level"], 6)
        self.assertTrue(all(step.get("public_context") for step in result["auction"]))

    def test_incremental_state_rejects_mismatched_history_dealer_vulnerability_or_system(self):
        observation = Observation(self.board.deal.north, Seat.NORTH, self.board.dealer, self.board.vulnerability,
                                  (), (), (("NS", self.demo), ("EW", self.pass_system)))
        worlds = [
            PublicWorld({"NS": self.demo, "EW": self.pass_system}, Seat.EAST, self.board.vulnerability),
            PublicWorld({"NS": self.demo, "EW": self.pass_system}, self.board.dealer, Vulnerability.BOTH),
            PublicWorld({"NS": self.pass_system, "EW": self.pass_system}, self.board.dealer, self.board.vulnerability),
        ]
        history = PublicWorld({"NS": self.demo, "EW": self.pass_system}, self.board.dealer, self.board.vulnerability)
        history.step(parse_call("1S"))
        worlds.append(history)
        for world in worlds:
            with self.subTest(world=world), self.assertRaisesRegex(SystemError, "Incremental public state"):
                _decide_and_advance(self.demo, observation, world)

    def test_public_announcements_share_one_evaluation_without_changing_their_union(self):
        raw = raw_system()
        raw["announcements"] = [
            {"id": f"same-public-condition-{index}", "when": op("eq", ctx("event.call"), "1C"),
             "tags": ["natural"], "suit": "C"} for index in range(12)
        ]
        world = PublicWorld({"NS": validate_system(raw)}, Seat.NORTH, Vulnerability.NONE)
        original = PublicWorld.evaluation
        calls = []

        def counted(current, side, event):
            calls.append((side, event.call))
            return original(current, side, event)

        with patch.object(PublicWorld, "evaluation", counted):
            world.step(parse_call("1C"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(world.events[-1].tags, ("natural",))
        self.assertEqual(world.events[-1].shown_suit, "C")

    def test_every_dealer_and_vulnerability_can_pass_out_without_a_1nt_filter(self):
        for dealer in Seat:
            for vulnerability in Vulnerability:
                result = run_auction(self.pass_system, self.pass_system, replace(
                    self.board, dealer=dealer, vulnerability=vulnerability,
                ))
                self.assertEqual(result["contract"], {"passed_out": True})
                self.assertEqual(len(result["auction"]), 4)
                self.assertEqual(result["auction"][0]["seat"], dealer.value)
                self.assertNotIn("score", result)

    def test_real_parameterized_question_and_answer_auction(self):
        result = run_auction(self.demo, self.pass_system, self.board, trace=True)
        self.assertEqual([step["call"] for step in result["auction"]], [
            "1S", "P", "3S", "P", "4NT", "P", "5H", "P", "6S", "P", "P", "P",
        ])
        self.assertEqual(result["contract"], {"level": 6, "strain": "S", "declarer": "N", "doubling": 1})
        self.assertEqual(result["evaluation"], "not_evaluated")
        self.assertEqual(result["auction"][6]["public_context"]["agreed_trump"], "S")

    def test_systems_rotate_to_ew_and_compete_without_shared_private_state(self):
        # One-position rotation swaps partnerships and preserves all public seat meanings.
        rotated = Board(Deal(self.board.deal.west, self.board.deal.north, self.board.deal.east, self.board.deal.south),
                        Seat.EAST, Vulnerability.NS)
        result = run_auction(self.pass_system, self.demo, rotated)
        self.assertEqual(result["contract"]["declarer"], "E")
        self.assertEqual(result["contract"]["strain"], "S")
        self.assertEqual(result["systems"]["EW"], self.demo.content_hash)
        self.assertEqual(run_auction(self.demo, self.pass_system, self.board),
                         run_auction(self.demo, self.pass_system, self.board))

    def test_prefix_continuation_is_explicit_and_replayed_through_public_protocol(self):
        prefix = tuple(map(parse_call, ("1S", "P", "3S", "P", "4NT", "P")))
        result = run_auction(self.demo, self.pass_system, self.board, prefix=prefix)
        self.assertTrue(all(item["source"] == "prefix" for item in result["auction"][:6]))
        self.assertEqual(result["auction"][6]["call"], "5H")
        self.assertEqual(result["contract"]["level"], 6)
        result = run_auction(self.pass_system, self.pass_system, self.board, prefix=(Call.pass_call(),) * 4)
        self.assertEqual(result["contract"], {"passed_out": True})
        with self.assertRaises(ValueError):
            run_auction(self.pass_system, self.pass_system, self.board, prefix=(Call.double(),))
        with self.assertRaises(ValueError):
            run_auction(self.pass_system, self.pass_system, self.board, prefix=(Call.pass_call(),) * 5)

    def test_v1_can_run_in_general_mode_but_local_scope_is_not_mislabelled(self):
        from systems.support import raw_system as v1_raw
        v1 = validate_system(v1_raw())
        self.assertEqual(run_auction(v1, self.pass_system, self.board)["contract"], {"passed_out": True})
        local = load_system(ROOT / "systems/1nt-v1.json")
        with self.assertRaisesRegex(SystemError, "scope=general"):
            run_auction(local, self.pass_system, self.board)
        board = parse_board(load_json(ROOT / "examples/board.json"))
        self.assertEqual(run_uncontested(local, board)["contract"]["level"], 4)

    def test_both_sides_can_bid_double_and_redouble_with_real_declarer_detection(self):
        ns_raw = raw_system()
        ns_raw["rules"] = [
            rule(op("not", {"op": "has_contract_bid"}), {"kind": "call", "call": "1C"}, "open", 2),
            rule({"op": "is_legal", "call": "XX"}, {"kind": "call", "call": "XX"}, "redouble", 1),
        ]
        ew_raw = raw_system()
        ew_raw["rules"] = [rule({"op": "is_legal", "call": "X"}, {"kind": "call", "call": "X"})]
        result = run_auction(validate_system(ns_raw), validate_system(ew_raw), self.board)
        self.assertEqual([item["call"] for item in result["auction"]], ["1C", "X", "XX", "P", "P", "P"])
        self.assertEqual(result["contract"], {"level": 1, "strain": "C", "declarer": "N", "doubling": 4})


if __name__ == "__main__":
    unittest.main()
