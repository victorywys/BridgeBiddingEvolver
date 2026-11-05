"""Development stress checks and post-run verification for frozen classic IRs."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from bridge_evolve.core import Contract, Seat, Strain, Vulnerability, final_contract, score_ns, signed_imp
from bridge_evolve.evaluation.data import BoardRecord, canonical_hash, family_hash, load_dataset
from bridge_evolve.evaluation.experiment import source_snapshot
from bridge_evolve.evaluation.statistics import paired_summary
from bridge_evolve.evaluation.record_log import iter_records
from bridge_evolve.oracle import DDSAdapter
from bridge_evolve.systems import load_system, validate_system
from bridge_evolve.systems.inputs import parse_board, parse_hand, private_inference, run_auction
from bridge_evolve.systems.language import SystemError, parse_call
from bridge_evolve.systems.runtime import Observation, decide, hand_hcp, replay
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld, build_world


def json_rows(path: Path):
    yield from iter_records(path)


def file_hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify_bounds(steps: list[dict], board) -> int:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise SystemError(message, actor=step["seat"], system_hash=step.get("system_hash"))

    previous = {}
    checked = 0
    for step in steps:
        before, after = step.get("public_context", {}), step.get("public_context_after", {})
        rule = step.get("private_trace", {}).get("rule_id", "")
        if (rule.startswith(("ccba.", "prec.")) and after.get("state") == "void_answer"
                and before.get("state") != "void_answer"):
            excluded = step["call"][1:]
            hand = board.deal.hand(Seat(step["seat"]))
            require(excluded in "CDHS" and not any(card.suit == excluded for card in hand.cards),
                    f"Exclusion question must show an actual void; {step['seat']} {step['call']}, rule={rule}.")
            require(".exclusion.ask_" in rule, f"Natural policy call was reinterpreted as exclusion keycards: {rule}.")
        checked_step = False
        for context_name in ("public_context", "public_context_after"):
            context = step.get(context_name, {})
            if "var.captain_hcp_min" not in context:
                continue
            checked_step = True
            side = Seat(step["seat"]).partnership.value
            captain = context.get("var.captain", "")
            for owner in ("captain", "responder"):
                seat = Seat(captain) if captain else None
                if seat is not None and owner == "responder":
                    seat = seat.next().next()
                hand = board.deal.hand(seat) if seat is not None else None
                lengths = {suit: 0 if hand is None else sum(card.suit == suit for card in hand.cards) for suit in "CDHS"}
                for field in ("hcp", "C", "D", "H", "S"):
                    name = f"var.{owner}_{field}_min"
                    lower = context.get(name, 0)
                    actual = (0 if hand is None else hand_hcp(hand)) if field == "hcp" else lengths[field]
                    require(type(lower) is int and 0 <= lower <= actual,
                            f"Unsound public bound {side}/{name}={lower}, actual={actual}, rule={step.get('private_trace', {}).get('rule_id')}.")
                    require(lower >= previous.get((side, name), 0), f"Public bound decreased: {side}/{name}.")
                    previous[(side, name)] = lower
                    upper_name = f"var.{owner}_{field}_max"
                    if upper_name in context:
                        limit = 40 if field == "hcp" else 13
                        upper = context[upper_name]
                        require(type(upper) is int and lower <= actual <= upper <= limit,
                                f"Unsound public interval {side}/{owner}/{field}=[{lower},{upper}], actual={actual}, rule={step.get('private_trace', {}).get('rule_id')}.")
                        require(upper <= previous.get((side, upper_name), limit),
                                f"Public {field} upper bound increased: {side}/{owner}.")
                        previous[(side, upper_name)] = upper
                for left in "CDHS":
                    for right in "CDHS":
                        name = f"var.{owner}_{left}_ge_{right}"
                        if left == right or name not in context:
                            continue
                        relation = context[name]
                        require(type(relation) is bool and (not relation or lengths[left] >= lengths[right]),
                                f"Unsound public suit relation {side}/{name}.")
                        require(not previous.get((side, name), False) or relation, f"Public suit relation forgotten: {side}/{name}.")
                        previous[(side, name)] = relation
                for suit in "CDHS":
                    quality = hand is not None and (lengths[suit] >= 4 or (
                        lengths[suit] >= 3 and any(card.suit == suit and card.rank in "AKQ" for card in hand.cards)))
                    control = hand is not None and (lengths[suit] <= 1 or any(
                        card.suit == suit and card.rank in "AK" for card in hand.cards))
                    for suffix, actual in (("support_quality", quality), ("support_denied", hand is not None and not quality), ("control", control)):
                        name = f"var.{owner}_{suit}_{suffix}"
                        if name not in context:
                            continue
                        value = context[name]
                        require(type(value) is bool and (not value or actual), f"Unsound public quality/control {side}/{name}.")
                        require(not previous.get((side, name), False) or value, f"Public quality/control forgotten: {side}/{name}.")
                        previous[(side, name)] = value
            for seat in Seat:
                name = "var.seat_" + seat.value + "_hcp_max"
                if name not in context:
                    continue
                upper = context[name]
                require(type(upper) is int and hand_hcp(board.deal.hand(seat)) <= upper <= 40,
                        f"Unsound passed-hand upper bound {seat.value}={upper}.")
                require(upper <= previous.get((side, name), 40), f"Passed-hand upper bound increased: {seat.value}.")
                previous[(side, name)] = upper
            inference = step.get("private_trace", {}).get("inference", {}) if context_name == "public_context" else {}
            for field in ("hcp", "C", "D", "H", "S"):
                lower_name, upper_name = f"partnership_{field}_min", f"partnership_{field}_max"
                if lower_name not in context:
                    continue
                actual = sum(hand_hcp(board.deal.hand(seat)) if field == "hcp"
                             else sum(card.suit == field for card in board.deal.hand(seat).cards)
                             for seat in Seat if seat.partnership.value == side)
                limit = 40 if field == "hcp" else 13
                lower, upper = context[lower_name], context.get(upper_name, limit)
                require(0 <= lower <= actual <= upper <= limit,
                        f"Unsound joint {field} interval {side}=[{lower},{upper}], actual={actual}.")
                require(lower >= previous.get((side, lower_name), 0) and upper <= previous.get((side, upper_name), limit),
                        f"Public joint {field} interval weakened: {side}.")
                previous[(side, lower_name)], previous[(side, upper_name)] = lower, upper
                if field == "hcp" and "partnership_hcp_max" in inference:
                    require(lower <= inference["partnership_hcp_min"] <= actual <= inference["partnership_hcp_max"] <= upper,
                            f"Unsound private joint HCP interval for {step['seat']}.")
                elif field != "hcp" and field in inference.get("suit_min", {}):
                    require(lower <= inference["suit_min"][field] <= actual <= inference.get("suit_max", {}).get(field, limit) <= upper,
                            f"Unsound private joint {field} interval for {step['seat']}.")
        checked += checked_step
    return checked


def semantic_probe(system, spec: dict, opponent=None) -> dict:
    original_hash = system.content_hash
    if "augment_transition" in spec:
        patch = spec["augment_transition"]
        raw = system.to_data()
        matching = [item for item in raw["protocol"]["states"][patch["state"]]["transitions"] if patch["id_contains"] in item["id"]]
        require(len(matching) == 1, "A semantic counterfactual must identify exactly one public transition.")
        matching[0]["set"].update(patch["set"])
        system = validate_system(raw)
    dealer = Seat(spec.get("dealer", "N"))
    history = tuple(parse_call(call) for call in spec.get("history", ()))
    actor = replay(dealer, history).next_seat
    side = actor.partnership.value
    definitions = ((side, system),) + ((("EW" if side == "NS" else "NS", opponent),) if opponent is not None else ())
    observation = Observation(parse_hand(spec["hand"]), actor, dealer, Vulnerability.NONE, history,
                              public_definitions=definitions)
    decision = decide(system, observation)
    result = {"id": spec["id"], "system": system.name, "history": list(spec.get("history", ())),
              "dealer": dealer.value, "actor": actor.value, "hand": spec["hand"],
              "call": str(decision.call), "rule": decision.trace.rule_id, "state_before": decision.trace.state,
              "expected_call": spec["call"], "category": spec.get("category", "unspecified"),
              "source_method": spec.get("source_method", ""), "findings": [],
              "counterfactual": "augment_transition" in spec, "original_system_hash": original_hash,
              "executed_system_hash": system.content_hash,
              "private_inference": private_inference(observation, decision.trace.public_context)}
    if str(decision.call) != spec["call"]:
        return {**result, "status": "not_reproduced"}
    world = build_world(system, observation)
    before = PublicContext(world, side, actor)
    result["parameters_before"] = dict(before.frame.parameters)
    world.step(decision.call)
    after = PublicContext(world, side, actor)
    result["parameters_after"] = dict(after.frame.parameters)
    owner = "captain" if after.frame.parameters.get("captain") == actor.value else "responder"
    for requirement in spec.get("requirements", []):
        field = requirement["field"].replace("owner_", owner + "_")
        actual = after.frame.parameters.get(field, "" if field == "trump" else 0)
        expected = requirement["value"]
        satisfied = actual == expected if requirement.get("relation") == "eq" else actual >= expected if isinstance(expected, int) else actual == expected
        if not satisfied:
            result["findings"].append({"kind": "missing_public_information", "field": field, "expected": expected, "actual": actual})
    if "sender_minimum" in spec:
        requirement = spec["sender_minimum"]
        actual = sum(card.suit == requirement["suit"] for card in observation.hand.cards)
        if actual < requirement["length"]:
            result["findings"].append({"kind": "natural_action_without_promised_length", "suit": requirement["suit"],
                                       "expected": requirement["length"], "actual": actual})
    if "fit_minimum" in spec:
        requirement = spec["fit_minimum"]
        known = result["private_inference"]["suit_min"][requirement["suit"]]
        if known < requirement["length"]:
            result["findings"].append({"kind": "fit_without_eight_card_guarantee", "suit": requirement["suit"],
                                       "expected": requirement["length"], "known_minimum": known})
    result["status"] = "confirmed_gap" if result["findings"] else "consistent"
    return result


def semantic_cases(*, corrected=False) -> list[dict]:
    cases = []

    def add_case(identifier, category, method, history, hand, call, minimums=None, *,
                 kinds=("ccba", "prec"), dealer="N", trump=None, exact=None, sender=None, fit=None):
        requirements = [{"field": "owner_" + name + "_min", "value": value} for name, value in (minimums or {}).items()]
        requirements.extend({"field": "owner_" + name + "_min", "value": value, "relation": "eq"} for name, value in (exact or {}).items())
        if trump:
            requirements.append({"field": "trump", "value": trump})
        cases.append({"id": identifier, "category": category, "source_method": method,
                      "history": history.split() if history else [], "hand": hand, "call": call,
                      "requirements": requirements, "systems": list(kinds), "dealer": dealer,
                      **({"sender_minimum": sender} if sender else {}), **({"fit_minimum": fit} if fit else {})})

    if corrected:
        add_case("weak_nt_board55_quantitative_not_gerber", "quantitative", "notrump", "1NT P",
                 "KQ.Q75.AK9.KQ543", "4NT", {"hcp": 18}, kinds=("prec",))
        add_case("weak_nt_board55_accept_maximum", "quantitative", "notrump", "1NT P 4NT P",
                 "A8.AK64.QJ865.J6", "6NT", kinds=("prec",))
        add_case("weak_nt_quantitative_decline_minimum", "quantitative", "completion", "1NT P 4NT P",
                 "A8.AK64.Q9865.76", "P", kinds=("prec",))
        add_case("possible_major_game_invitation", "game_invitations", "completion", "1S P 2S P",
                 "KQJ987.AKQ2.54.3", "3S", {"hcp": 15})
        add_case("possible_major_game_acceptance", "game_invitations", "completion", "1S P 2S P 3S P",
                 "Q32.J87.AQ6.JT86", "4S", {"hcp": 10}, trump="S")
        add_case("possible_nt_game_after_suit_rebid", "game_invitations", "completion", "1S P 1NT P 2S P",
                 "Q3.AJ8.K54.Q8762", "2NT", {"hcp": 8})
        add_case("game_force_low_fit_keeps_slam_space", "fit_confirmation", "slam_cuebids", "1S P 2C P 2S P",
                 "KQ32.Q87.KJ4.Q76", "3S", {"S": 3}, trump="S")
        add_case("splinter_possible_slam_below_thirty", "controls", "slam_cuebids", "1S P 4D P",
                 "AJ987.KQJ.54.KQ3", "4H", exact={"H": 0})
        cases[-1]["requirements"].append({"field": "owner_H_control", "value": True, "relation": "eq"})
        add_case("precision_support_inquiry_772", "support_inquiry", "rebids", "1C P 1NT P",
                 "Q4.AKQ85.Q8.K985", "2H", {"H": 5}, kinds=("prec",), dealer="W")
        add_case("precision_support_sixth_step_772", "support_inquiry", "rebids", "1C P 1NT P 2H P",
                 "AKJ8.T974.AJ.A76", "3S", {"H": 3}, kinds=("prec",), dealer="W", trump="H")
        cases[-1]["requirements"].append({"field": "owner_H_support_quality", "value": True, "relation": "eq"})
        add_case("precision_slam_control_772", "controls", "slam_cuebids", "1C P 1NT P 2H P 3S P",
                 "Q4.AKQ85.Q8.K985", "4C", kinds=("prec",), dealer="W", exact={"C": 0},
                 fit={"suit": "H", "length": 9})
        cases[-1]["requirements"].append({"field": "owner_C_control", "value": True, "relation": "eq"})
        add_case("game_force_control_not_four_clubs", "controls", "slam_cuebids", "1S P 2NT P 3S P",
                 "KQ32.AJ87.54.K76", "4C", exact={"C": 0})
        cases[-1]["requirements"].append({"field": "owner_C_control", "value": True, "relation": "eq"})

    add_case("open_major", "openings", "openings", "", "AKQJ87.K32.Q54.8", "1S", {"S": 5})
    add_case("open_ccba_diamond", "openings", "openings", "", "AKJ3.2.KQ54.J876", "1D", {"D": 3}, kinds=("ccba",))
    add_case("open_precision_diamond", "openings", "openings", "", "AKJ3.2.KQ54.J876", "1D", {"D": 2}, kinds=("prec",))
    add_case("open_weak_two", "openings", "openings", "", "Q32.KJ9876.54.87", "2H", {"H": 6})
    add_case("open_three_preempt", "openings", "openings", "", "KQJ9876.32.54.87", "3S", {"S": 7})
    add_case("open_four_preempt", "openings", "openings", "", "KQJ98765.32.54.8", "4S", {"S": 8})
    add_case("open_notrump", "controls", "openings", "", "AKQ.JT9.KQ32.876", "1NT", {"C": 2, "D": 2, "H": 2, "S": 2})
    add_case("strong_club_not_natural_clubs", "controls", "openings", "", "AKQ.KT9.KQ32.876", "1C", kinds=("prec",), exact={"C": 0})
    add_case("precision_three_suit_opening", "controls", "openings", "", "AKQ9.KJ98.2.9876", "2D", {"S": 4, "H": 4, "C": 4}, kinds=("prec",))
    add_case("minor_major_response", "controls", "responses", "1D P", "KQJ8.32.Q54.8762", "1S", {"S": 4})
    add_case("minor_two_over_one_ccba", "controls", "responses", "1D P", "32.AQ8.K54.KQ876", "2C", {"C": 4}, kinds=("ccba",))
    add_case("minor_two_over_one_precision", "controls", "responses", "1D P", "32.AQ8.K54.KQ876", "2C", {"C": 5}, kinds=("prec",))
    add_case("simple_major_raise", "natural_responses", "fit_conventions", "1S P", "Q32.K87.654.A876", "2S", {"S": 3}, trump="S")
    add_case("preemptive_major_game_raise", "natural_responses", "responses", "1S P", "J9876.32.54.8762", "4S", {"S": 5}, trump="S")
    add_case("two_over_one_clubs", "natural_responses", "responses", "1S P", "32.AQ8.K54.KJ876", "2C", {"C": 3})
    add_case("two_over_one_diamonds", "natural_responses", "responses", "1S P", "32.AQ8.KJ54.K876", "2D", {"D": 4})
    add_case("two_over_one_hearts", "controls", "responses", "1S P", "32.KQJ98.A54.K76", "2H", {"H": 5})
    add_case("bergen_raise", "controls", "fit_conventions", "1S P", "QJ32.K87.54.Q876", "3C", {"S": 4}, trump="S")
    add_case("drury_support", "natural_responses", "fit_conventions", "P P 1S P", "Q32.K87.Q54.K876", "2C", {"S": 3}, trump="S")
    add_case("inverted_minor_raise", "controls", "responses", "1D P", "32.K87.AQJ54.K76", "2D", {"D": 4}, trump="D")
    add_case("precision_weak_jump", "natural_responses", "responses", "1D P", "32.KJ9876.54.876", "2H", {"H": 6}, kinds=("prec",))
    add_case("ccba_preemptive_minor_raise", "natural_responses", "responses", "1C P", "32.87.Q54.J98765", "3C", {"C": 5}, kinds=("ccba",))
    add_case("natural_second_major", "natural_rebids", "suit_rebids", "1D P 1H P", "AKJ3.K8.QJ54.876", "1S", {"S": 4})
    add_case("six_card_minor_rebid", "natural_rebids", "suit_rebids", "1D P 1S P", "2.K87.AQJ987.K76", "2D", {"D": 6})
    add_case("natural_second_minor", "natural_rebids", "suit_rebids", "1D P 1S P", "2.K87.AQJ54.K876", "2C", {"C": 4})
    add_case("ccba_reverse", "natural_rebids", "suit_rebids", "1D P 1S P", "2.AK87.AQJ54.K87", "2H", {"H": 4}, kinds=("ccba",))
    add_case("precision_reverse", "natural_rebids", "suit_rebids", "1D P 1S P", "2.AJ87.KQJ54.K87", "2H", {"H": 4}, kinds=("prec",))
    add_case("strong_positive_spades_580", "natural_rebids", "rebids", "1C P 2D P", "AKQT65.4.AT98.KJ", "2S", {"S": 5}, kinds=("prec",), dealer="W")
    add_case("strong_negative_spades", "natural_rebids", "rebids", "2C P 2D P", "AKQJ65.4.AKQ8.KJ", "2S", {"S": 5}, kinds=("ccba",))
    add_case("strong_balanced_nt_rebid", "natural_rebids", "rebids", "1C P 1D P", "AKQ.KT9.KQ32.876", "1NT", {"C": 2, "D": 2, "H": 2, "S": 2}, kinds=("prec",))
    add_case("strong_four_card_fallback_singleton", "unconstrained_senders", "rebids", "1C P 2D P", "AKQJ.2.A987.KQ87", "2H", kinds=("prec",), sender={"suit": "H", "length": 2})
    add_case("economical_fallback_singleton", "unconstrained_senders", "rebids", "1H P 1NT P", "AQ87.KJ987.K54.2", "2C", sender={"suit": "C", "length": 2})
    add_case("fourth_suit_support", "natural_rebids", "suit_rebids", "1D P 1S P 2C P 2H P", "AQ3.K8.KJ54.8762", "2S", {"S": 3})
    add_case("checkback_weak_long_major", "natural_rebids", "checkback", "1D P 1S P 1NT P", "KQJ987.32.Q4.876", "2S", {"S": 6})
    add_case("checkback_long_major_invite", "natural_rebids", "checkback", "1D P 1S P 1NT P 2C P 2D P", "AKJ87.K32.J4.876", "3S", {"S": 5})
    add_case("precision_point_shape_four", "shape_relays", "precision_conventions", "1C P 1NT P 2C P", "KJ87.Q32.54.AQ76", "3C", {"C": 4}, kinds=("prec",))
    add_case("precision_other_four_reveal", "shape_relays", "precision_conventions", "1C P 1NT P 2C P 3C P 3D P", "KJ87.Q32.54.AQ76", "3S", {"S": 4}, kinds=("prec",))
    add_case("precision_4333_four_reveal", "shape_relays", "precision_conventions", "1C P 1NT P 2C P 2NT P 3C P", "KJ87.Q32.A54.Q76", "3S", {"S": 4}, kinds=("prec",))
    add_case("precision_4441_positive", "shape_relays", "responses", "1C P", "KQ87.AJ98.K654.2", "2NT", {"S": 4, "H": 4, "D": 4}, kinds=("prec",))
    add_case("precision_club_four_major", "controls", "precision_conventions", "2C P 2D P", "KQ87.32.54.AKJ76", "2S", {"S": 4}, kinds=("prec",))
    add_case("precision_club_shortness_not_natural", "controls", "precision_conventions", "2C P 2D P", "KQ8.2.K54.AK9876", "3H", kinds=("prec",), exact={"H": 0})
    add_case("precision_weak_nt_natural_major", "notrump", "notrump", "1NT P", "KJ9876.32.54.876", "2S", {"S": 5}, kinds=("prec",))
    add_case("strong_nt_transfer", "controls", "notrump", "1NT P", "KJ9876.32.54.876", "2H", {"S": 5}, kinds=("ccba",), exact={"H": 0})
    add_case("stayman_major_answer", "controls", "notrump", "1NT P 2C P", "AQ3.KJ87.K54.Q76", "2H", {"H": 4})
    add_case("stayman_denial_not_natural", "controls", "notrump", "1NT P 2C P", "AKQ.KT9.KQ32.876", "2D", exact={"D": 2}, kinds=("ccba",))
    add_case("stayman_four_four_raise", "notrump", "notrump", "1NT P 2C P 2H P", "KQ3.AJ87.54.Q876", "4H", {"H": 4}, kinds=("ccba",), trump="H")
    add_case("transfer_five_two_not_assured_fit", "unconstrained_senders", "notrump", "1NT P 2D P 2H P", "A32.KQJ98.Q4.T98", "4H", kinds=("ccba",), fit={"suit": "H", "length": 8})
    add_case("natural_minor_nt_invite", "notrump", "nt_extensions", "1NT P", "432.K8.54.KQJ987", "3C", {"C": 6}, kinds=("ccba",))
    add_case("smolen_shape", "notrump", "nt_extensions", "1NT P 2C P 2D P", "AKJ87.KQ32.54.76", "3H", {"H": 4, "S": 5}, kinds=("ccba",))
    add_case("puppet_responder_four", "notrump", "notrump", "2NT P 3C P 3D P", "Q32.KJ87.54.Q876", "3S", {"H": 4})
    add_case("puppet_opener_four", "notrump", "notrump", "2NT P 3C P 3D P 3S P", "AK3.AQ87.K54.AJ6", "4H", {"H": 4}, trump="H")
    add_case("minor_stayman_both_minors", "notrump", "nt_extensions", "2NT P", "32.87.KJ654.Q876", "3S", {"C": 4, "D": 4})
    add_case("minor_stayman_answer", "notrump", "nt_extensions", "2NT P 3S P", "AK3.AQ8.K54.AJ76", "4C", {"C": 4}, trump="C")
    add_case("minor_shape_shortness", "notrump", "nt_extensions", "1NT P", "K32.2.AQ54.KJ876", "3H", {"C": 4, "D": 4}, kinds=("ccba",), exact={"H": 0})
    add_case("minor_transfer_diamond_correction", "notrump", "notrump", "1NT P 2S P 3C P", "Q32.87.KJ9876.54", "3D", {"D": 6}, kinds=("ccba",))
    add_case("doubled_nt_escape_transfer", "notrump", "competitive_conventions", "1NT X", "KJ9876.32.54.876", "2H", {"S": 5}, exact={"H": 0})
    add_case("natural_overcall", "controls", "competition", "1D", "KJ987.A32.K54.87", "1S", {"S": 5})
    add_case("overcall_natural_new_suit_response", "competition", "manual_competition", "1D 1H P", "KQJ87.32.Q54.876", "1S", {"S": 5, "hcp": 6})
    add_case("two_level_natural_overcall_strength", "competition", "competition", "1D", "432.K8.AQ3.KQ876", "2C", {"C": 5, "hcp": 11})
    add_case("takeout_advance_length", "competition", "competition", "1D X P", "QJ87.K32.54.8762", "1S", {"S": 4})
    add_case("negative_double_unbid_major", "controls", "manual_competition", "1D 1S", "32.KJ87.Q54.A876", "X", {"H": 4})
    add_case("michaels_both_majors", "controls", "competitive_conventions", "1D", "KQJ87.AJ987.32.4", "2D", {"H": 5, "S": 5})
    add_case("michaels_unknown_minor_reveal", "competition", "competitive_conventions", "1H 2H P 2NT P", "AQJ87.2.A4.KJ876", "3C", {"C": 5})
    add_case("lebensohl_natural_weak_major", "competition", "competitive_conventions", "1NT 2H", "KQJ87.32.54.8762", "2S", {"S": 5})
    add_case("lebensohl_natural_game_suit", "competition", "competitive_conventions", "1NT 2H", "32.87.AKQJ54.K76", "3D", {"D": 5, "hcp": 10})
    add_case("lebensohl_weak_diamond_reveal", "competition", "competitive_conventions", "1NT 2H 2NT P 3C P", "Q32.87.KJ654.876", "3D", {"D": 5})
    add_case("gambling_minor_correction", "natural_rebids", "responses", "3NT P 4C P", "3.87.AKQJT98.876", "4D", {"D": 7})
    add_case("preempt_natural_new_major_game", "natural_responses", "responses", "3H P", "AKQJ987.A32.K5.2", "4S", {"S": 7})
    add_case("rkcb_answer_not_natural_clubs", "controls", "questions", "1S P 2NT P 3S P 4NT P", "KQJ987.KQ3.KJ.32", "5C", exact={"C": 0})
    add_case("queen_question_not_natural_diamonds", "controls", "slam_continuations", "1S P 2NT P 3S P 4NT P 5C P", "AKJT2.AQ2.K54.A2", "5D", exact={"D": 0})
    add_case("unwritten_four_spade_agreement_580", "fit_confirmation", "completion", "1C P 2D P 2S P", "J432.K.KJ732.863", "4S", kinds=("prec",), dealer="W", trump="S")
    cases[-1]["augment_transition"] = {"state": "strong_positive", "id_contains": ".rebid_2S.", "set": {
        "captain_S_min": {"op": "max", "args": [{"op": "var", "name": "captain_S_min"}, 5]},
    }}
    if corrected:
        for case in cases:
            if case["id"] in ("strong_positive_spades_580", "strong_negative_spades", "six_card_minor_rebid"):
                case["requirements"][0]["value"] = 4 if case["id"] == "strong_negative_spades" else 5
                case["public_contract_basis"] = "Common guarantee across every sender of the visible call, not the private selected rule."
            elif case["id"] == "strong_four_card_fallback_singleton":
                case.update(call="3D", sender_minimum={"suit": "D", "length": 4},
                            requirements=[{"field": "owner_D_min", "value": 4}])
            elif case["id"] == "economical_fallback_singleton":
                case.update(call="2H", sender_minimum={"suit": "H", "length": 5},
                            requirements=[{"field": "owner_H_min", "value": 5}])
            elif case["id"] == "transfer_five_two_not_assured_fit":
                case["call"] = "3NT"
                del case["fit_minimum"]
            elif case["id"] == "unwritten_four_spade_agreement_580":
                del case["augment_transition"]
                case["call"] = "3S"
                case["requirements"].extend([{"field": "owner_S_min", "value": 3}, {"field": "captain_S_min", "value": 5}])
                case["fit_minimum"] = {"suit": "S", "length": 8}
        add_case("minor_five_card_fallback_common_meaning", "natural_rebids", "rebids", "1D P 1S P",
                 "2.KJ87.KQJ54.Q87", "2D", {"D": 5}, sender={"suit": "D", "length": 5})
        add_case("board_465_precision_real_four_card_major", "unconstrained_senders", "rebids", "1C P 1D P",
                 "KQ76.7.AKQJ.AK93", "1S", {"S": 4}, kinds=("prec",), sender={"suit": "S", "length": 4})
        add_case("board_465_ccba_real_four_card_major", "unconstrained_senders", "rebids", "2C P 2D P",
                 "KQ76.7.AKQJ.AK93", "2S", {"S": 4}, kinds=("ccba",), sender={"suit": "S", "length": 4})
        add_case("board_994_real_four_card_major", "unconstrained_senders", "rebids", "1C P 1D P",
                 "AKT9.K.AQ94.K842", "1S", {"S": 4}, kinds=("prec",), sender={"suit": "S", "length": 4})
        add_case("late_nt_interference_is_not_responder_lebensohl", "notrump", "competitive_conventions", "1NT P P 2D",
             "AKQ.JT9.KQ32.876", "P")
        add_case("late_strong_nt_interference_preserves_negative_range", "notrump", "competitive_conventions",
             "1C P 1D P 1NT P P 2D", "AKT.AJT65.932.KQ", "P", kinds=("prec",), dealer="W")
        cases[-1]["requirements"] = [{"field": "responder_hcp_min", "value": 0, "relation": "eq"},
                          {"field": "responder_hcp_max", "value": 7, "relation": "eq"}]
        for kind, minimum in (("ccba", 12), ("prec", 11)):
            add_case("two_over_one_new_suit_without_extras_" + kind, "natural_rebids", "rebids", "1D P 2C P",
                     "QJT2.AK98.Q84.87", "2S", {"S": 4}, kinds=(kind,), exact={"hcp": minimum},
                     sender={"suit": "S", "length": 4})
        add_case("artificial_checkback_diamonds_keep_the_original_natural_promise", "controls", "checkback",
                 "1C P 1D P 1NT P", "AK3.Q65.KJ987.87", "2D", kinds=("ccba",), exact={"D": 4, "C": 0})
        for kind, hand, minimum in (("prec", "T.KQJ9865.KJ4.85", 10), ("ccba", "T.KQJ9865.Q84.85", 8)):
            add_case("lebensohl_takeout_sender_" + kind, "competition", "competitive_conventions", "1NT 2S",
                     hand, "X", kinds=(kind,), exact={"hcp": minimum}, dealer="W")
            add_case("lebensohl_takeout_opener_reply_" + kind, "competition", "competitive_conventions", "1NT 2S X P",
                     "J32.A4.AQ872.KJ7", "3D", {"D": 4}, kinds=(kind,), dealer="W", sender={"suit": "D", "length": 4})
            cases[-1]["requirements"].append({"field": "responder_hcp_min", "value": minimum, "relation": "eq"})
        add_case("minor_opening_major_upper_bounds", "suit_intervals", "public_information", "1D P",
                 "J432.K.KJ732.863", "1S", {"S": 4})
        cases[-1]["requirements"].extend({"field": "captain_" + suit + "_max", "value": 4, "relation": "eq"} for suit in ("H", "S"))
        add_case("major_second_suit_order", "suit_relations", "public_information", "1S P 1NT P",
                 "AKJ87.KQ32.5.876", "2H", {"S": 5, "H": 4})
        cases[-1]["requirements"].extend([
            {"field": "captain_S_ge_H", "value": True, "relation": "eq"},
            {"field": "captain_H_max", "value": 6, "relation": "eq"},
            {"field": "captain_S_max", "value": 9, "relation": "eq"},
        ])
        add_case("precision_positive_4441_short_diamond", "suit_intervals", "rebids", "1C P 2D P",
                 "AKQ9.KJ98.2.QJ76", "3NT", {"S": 4, "H": 4, "C": 4}, kinds=("prec",))
        cases[-1]["requirements"].extend([
            {"field": "captain_D_max", "value": 1, "relation": "eq"},
            {"field": "captain_S_max", "value": 4, "relation": "eq"},
        ])
    return cases


def rule_local_minima(identifier: str, call) -> dict:
    name = identifier.split(".", 1)[1].rsplit(".r", 1)[0]
    strain = call.strain.value if call.strain else ""
    if not strain or strain == "NT":
        return {}
    patterns = (
        (r"open\.precision_diamond$", 2), (r"open\.preempt_4_", 8),
        (r"major\.simple_raise_", 3), (r"major\.preemptive_game_", 5), (r"fit\.drury_", 3),
        (r"major\.two_over_one_clubs_", 3), (r"major\.two_over_one_diamonds_", 4),
        (r"minor\.weak_jump_", 6), (r"minor\.preemptive_raise_", 5),
        (r"opener_rebid\.new_major_", 4), (r"opener_rebid\.six_card_rebid_", 6),
        (r"opener_rebid\.second_suit_", 4), (r"strong_(positive|negative)\.longest_", 5),
        (r"fourth\.support_", 3), (r"checkback\.weak_major_", 6), (r"checkback\.invite_suit_", 5),
        (r"precision\.(low_four_|other_four_|single_four_)", 4),
        (r"nt\.weak_major_", 5), (r"nt\.minor_invitation_", 6),
        (r"lebensohl\.(weak_two_|game_suit_)", 5), (r"lebensohl\.weak_diamonds$", 5),
        (r"competition\.natural_response_", 5), (r"competition\.advance_double_", 4),
        (r"gambling\.correct_diamonds$", 7), (r"preempt\.major_game_", 7),
    )
    for pattern, minimum in patterns:
        if re.search(pattern, name):
            promised = "S" if name.startswith(("major.simple_raise_", "major.preemptive_game_", "fit.drury_")) and name.endswith("S") else (
                "H" if name.startswith(("major.simple_raise_", "major.preemptive_game_", "fit.drury_")) and name.endswith("H") else strain)
            return {promised: minimum}
    if name.startswith("nt_escape.transfer_"):
        return {name.rsplit("_", 1)[1]: 5}
    if name.startswith("precision.shape_pair_3"):
        return {"C": 4} if name.endswith("3C") else dict.fromkeys({"3D": ("D", "H"), "3H": ("H", "S"), "3S": ("S", "D")}[name[-2:]], 4)
    return {}


def observed_semantic_candidates(directory: Path, systems: dict) -> dict:
    run = json.loads((directory / "run.json").read_text())
    require(run["status"] == "completed", "Semantic log review requires a completed run.")
    require(source_snapshot() == run["source_files"], "Source differs from frozen run; do not attach current-source review to old logs.")
    require({kind: system.content_hash for kind, system in systems.items()} ==
            {"ccba": run["system_a"]["hash"], "prec": run["system_b"]["hash"]}, "Semantic systems differ from frozen run.")
    for name in ("dataset-boards.jsonl", "auctions.jsonl"):
        require(file_hash(directory / name) == run["artifacts_sha256"][name], f"Frozen file hash mismatch: {name}.")
    boards = [parse_board(raw["board"]) for raw in json_rows(directory / "dataset-boards.jsonl")]
    by_hash = {system.content_hash: system for system in systems.values()}
    candidates, samples, untracked, sender_defects = Counter(), {}, Counter(), []
    decisions = tables = 0
    all_findings = set()
    for played in json_rows(directory / "auctions.jsonl"):
        board = boards[played["index"]]
        world = PublicWorld(
            {side: by_hash[content_hash] for side, content_hash in played["systems"].items()}, board.dealer, board.vulnerability)
        for turn, step in enumerate(played["auction"], 1):
            actor = Seat(step["seat"])
            call = parse_call(step["call"])
            world.step(call)
            decisions += 1
            identifier = step["private_trace"]["rule_id"]
            parameters = world.frames[actor.partnership.value].parameters
            owner = "captain" if parameters.get("captain") == actor.value else "responder"
            for strain, minimum in rule_local_minima(identifier, call).items():
                field = owner + "_" + strain + "_min"
                actual = parameters.get(field, 0)
                if actual < minimum:
                    key = (identifier.rsplit(".r", 1)[0], field, minimum, actual)
                    candidates[key] += 1
                    samples.setdefault(key, {"number": played["index"] + 1, "table": played["table"], "turn": turn,
                                              "seat": actor.value, "call": str(call), "history": [entry["call"] for entry in played["auction"][:turn]]})
                    all_findings.add(played["index"])
            if re.search(r"\.(economical_rebid|four_card_suit)\.r\d+$", identifier) and call.strain and call.strain != Strain.NOTRUMP:
                length = sum(card.suit == call.strain.value for card in board.deal.hand(actor).cards)
                if length < 2:
                    sender_defects.append({"number": played["index"] + 1, "table": played["table"], "turn": turn,
                                           "call": str(call), "length": length, "rule": identifier})
                    all_findings.add(played["index"])
            if ".natural_game_" in identifier and call.strain and parameters.get("trump") != call.strain.value:
                untracked[identifier.rsplit(".r", 1)[0]] += 1
                all_findings.add(played["index"])
        tables += 1
    return {"boards": len(boards), "tables": tables, "decisions": decisions,
            "boards_with_candidates": len(all_findings), "rule_local_omission_occurrences": sum(candidates.values()),
            "candidate_groups": [{"rule": key[0], "field": key[1], "rule_local_minimum": key[2], "written": key[3],
                                  "occurrences": count, "sample": samples[key]} for key, count in candidates.most_common()],
            "unconstrained_sender_counterexamples": sender_defects,
            "natural_game_without_trump_registration": dict(untracked),
            "limitations": "规则局部保证仅用于定位候选；同一公开叫品可能由多个私有分支发出，不能把命中分支的保证直接当作整条叫品的公开含义。定向契约另行验证相应上下文。"}


def semantic_contracts(systems_path: Path, report: dict) -> None:
    from bridge_evolve.systems.classics import build_classic
    from bridge_evolve.systems.compact import compact_system

    systems = {kind: load_system(systems_path / f"{kind}.json") for kind in ("ccba", "prec")}
    for kind, system in systems.items():
        compiled = build_classic(kind)
        require(system.content_hash == compiled.content_hash
                or system.content_hash == compact_system(compiled).content_hash,
            "Candidate IR differs from the current compiler or its lossless compact export; rebuild it before checking contracts.")
    report.update({"systems": {kind: system.content_hash for kind, system in systems.items()},
           "source_hash": canonical_hash(source_snapshot()), "probes": [],
           "scope": "Conditional public-prefix contracts for the current candidate; no old results or private hit traces are used to fill shared facts."})
    for spec in semantic_cases(corrected=True):
        for kind in spec["systems"]:
            result = semantic_probe(systems[kind], spec, systems["prec" if kind == "ccba" else "ccba"])
            report["probes"].append({"kind": kind, **result})
    report["probe_counts"] = dict(Counter(probe["status"] for probe in report["probes"]))
    require(all(probe["status"] == "consistent" and not probe["counterfactual"] for probe in report["probes"]),
        "Candidate semantic contracts failed; missing guarantees and unexpected calls both block acceptance.")


def semantic_review(systems_path: Path, directory: Path, report: dict) -> None:
    systems = {kind: load_system(systems_path / f"{kind}.json") for kind in ("ccba", "prec")}
    original_hashes = {kind: system.content_hash for kind, system in systems.items()}
    probes = []
    for spec in semantic_cases():
        for kind in spec["systems"]:
            result = semantic_probe(systems[kind], spec, systems["prec" if kind == "ccba" else "ccba"])
            probes.append({"kind": kind, **result})
    require({kind: system.content_hash for kind, system in systems.items()} == original_hashes, "Review changed a frozen definition.")
    methods = {node.name: node.lineno for node in ast.walk(ast.parse((Path(__file__).resolve().parents[1] / "src/bridge_evolve/systems/classics.py").read_text()))
               if isinstance(node, ast.FunctionDef)}
    coverage = {}
    for probe in probes:
        key = probe["source_method"]
        entry = coverage.setdefault(key, {"source_line": methods[key], "scenarios": 0, "confirmed_gap": 0, "consistent": 0, "not_reproduced": 0})
        entry["scenarios"] += 1
        entry[probe["status"]] += 1
    report.update({"systems": original_hashes, "source_hash": canonical_hash(source_snapshot()), "probes": probes,
                   "probe_counts": dict(Counter(probe["status"] for probe in probes)), "coverage": coverage,
                   "counterfactual_probes": sum(probe["counterfactual"] for probe in probes),
                   "observed_logs": observed_semantic_candidates(directory, systems),
                   "limitations": ["覆盖全部15个行为编译模块的静态阅读与代表性场景，不是对所有状态/手牌穷举。",
                                   "场景以前缀为条件并让待检牌手实际选择叫品，不单凭字段存在判定；不证明每个输入前缀都由当前双方策略完整到达。",
                                   "反事实场景显式标注，仅在内存补前置含义以隔离将牌确认缺陷；冻结体系和成绩不修改。",
                                   "止张、套质、大牌组合、短套上界及析取牌型含义未做全符号等价证明。"]})
    report["status"] = "completed-with-findings" if report["probe_counts"].get("confirmed_gap") else "passed"


def semantic_markdown(report: dict, destination: Path) -> str:
    counts = report["probe_counts"]
    observed = report["observed_logs"]
    source = Path(__file__).resolve().parents[1] / "src/bridge_evolve/systems/classics.py"
    source_link = Path(os.path.relpath(source, destination.parent)).as_posix()
    lines = ["# CCBA / 精确 v4：自然叫品语义完整性审查", "", "## 结论", "",
             "当前问题不是第580副的单点遗漏。多类自然应叫、再叫、支持和牌型接力没有把发送条件保证的信息完整写入公开账本；另有兜底自然动作未约束套长。",
             "本次仅新增只读检查和审查报告，不修改冻结体系、执行期代码或原有实验成绩。结果为completed-with-findings，不是策略验收通过。", "",
             f'- 静态阅读全部15个行为编译模块；定向复现{sum(counts.values())}次，其中问题实例{counts.get("confirmed_gap", 0)}次、对照符合预期{counts.get("consistent", 0)}次、未复现{counts.get("not_reproduced", 0)}次。问题实例包含同一模板在两套体系上的重复，不等于同样数量的独立bug。',
             f'- 明确标注的内存反事实{report["counterfactual_probes"]}次，仅用于隔离“前置长度补全后，支持叫仍不登记将牌”的第二层缺陷。',
             f'- 原1000副日志重放{observed["tables"]}桌、{observed["decisions"]}次决策；不重新运行策略选牌或计分，不借四手牌为策略填信息。',
             "", "## 高优先级问题", "",
             "1. **发送端不约束所叫自然套**：经济再叫和强开叫four_card_suit兜底可叫出单张花色。不能通过无条件补4/5张下界修复，否则会把真遗漏变成错误信息。",
             "2. **自然套/重叫长度未传播**：强开叫自然再叫、新高花、第二套、六张套重叫、竞争中新花等，只更新花色名称或阶段，长度下界缺失或未强化。",
             "3. **支持与将牌确认不完整**：简单加叫、Drury、Stayman支持等未写足支持长度；通用成局支持后未登记trump。转移后5张套加开叫人仅保证2张也可能直接选4M，是另一类未确认8张配合的风险。",
             "4. **形状信息与账本脱节**：精确4441、44牌型/4333接力、Smolen、傀儡和低花斯台曼等可能在其它参数里记了形状，但通用套长/配合推断只读下界账本，后续无法使用。",
             "5. **现有审计只检查不说错**：零下界不超过真值，因而不会失败；没有检验应表达的最小保证是否遗漏，也没有证明同一公开叫品全部分支的含义。", "",
             "## 模块覆盖", "", "| 模块 | 定向次数 | 问题实例 | 对照符合 | 未复现 | 源码 |",
             "| --- | ---: | ---: | ---: | ---: | --- |"]
    for module, data in report["coverage"].items():
        lines.append(f'| {module} | {data["scenarios"]} | {data["confirmed_gap"]} | {data["consistent"]} | {data["not_reproduced"]} | [{data["source_line"]}]({source_link}#L{data["source_line"]}) |')
    lines.extend(["", "## 定向问题实例", "", "每个输入前缀都是公开且合法的条件场景，待检牌手实际决定动作。下表保证依据对应场景的源代码与协议条件，不使用搭档私牌生成公共含义。完整手牌、前后参数和命中规则在同名JSON。", "",
                  "| 场景ID | 体系 | 公开前缀 → 本次动作 | 应记录/保证 | 实际 | 命中规则 |",
                  "| --- | --- | --- | --- | --- | --- |"])
    for probe in report["probes"]:
        for finding in probe["findings"]:
            if finding["kind"] == "missing_public_information":
                expected = f'{finding["field"]}={finding["expected"]}或更强'
                actual = str(finding["actual"]) or "空"
            elif finding["kind"] == "natural_action_without_promised_length":
                expected = f'自然{finding["suit"]}至少{finding["expected"]}张的最低契约'
                actual = f'实际{finding["actual"]}张'
            else:
                expected = f'{finding["suit"]}配合已知下界{finding["expected"]}张'
                actual = f'仅能保证{finding["known_minimum"]}张，非实际总张数'
            prefix = " ".join(probe["history"])
            counterfactual = "（内存反事实）" if probe["counterfactual"] else ""
            lines.append(f'| {probe["id"]}{counterfactual} | {probe["kind"]} | {prefix} → {probe["call"]} | {expected} | {actual} | {probe["rule"]} |')
    lines.extend(["", "## 正确对照与不能硬填的0", "",
                  "| 场景ID | 体系 | 动作 | 验证结果 |", "| --- | --- | --- | --- |"])
    for probe in report["probes"]:
        if probe["status"] != "confirmed_gap":
            lines.append(f'| {probe["id"]} | {probe["kind"]} | {probe["call"]} | {probe["status"]} |')
    lines.extend(["", "强梅花、Stayman否认、转移和关键张回答不能按字面花色增加长度；搭档示双套后的示选也不自动保证本人3张。析取保证（如至少一门4张高花）不能硬填到某一门独立下界。", "",
                  "## 原运行触发候选", "",
                  f'- 规则局部套长候选{observed["rule_local_omission_occurrences"]}次，涉及候选牌例{observed["boards_with_candidates"]}副（合并其它候选后去重）；不是独立bug或穷举公开含义证明。',
                  f'- 实际叫到不足2张花色的两个无约束兜底反例{len(observed["unconstrained_sender_counterexamples"])}次。',
                  f'- 自然成局叫品后未登记对应trump共{sum(observed["natural_game_without_trump_registration"].values())}次。',
                  observed["limitations"], "", "| 命中规则 | 字段 | 规则局部保证 | 实际写入 | 次数 | 原运行例子 |",
                  "| --- | --- | ---: | ---: | ---: | --- |"])
    for group in observed["candidate_groups"]:
        sample = group["sample"]
        lines.append(f'| {group["rule"]} | {group["field"]} | {group["rule_local_minimum"]} | {group["written"]} | {group["occurrences"]} | 第{sample["number"]}副/{sample["table"]}/第{sample["turn"]}叫 |')
    lines.extend(["", "## 修复顺序与限制", "",
                  "先消除无套长兜底和同叫品多义冲突，再按公共历史把所有可发同叫品分支取并集，补最小保证；支持叫同步确认将牌及支持下界；最后把完整性契约加入验证。不能从本次命中私有分支或搭档真牌直接共享信息。", ""])
    lines.extend("- " + item for item in report["limitations"])
    lines.extend(["", "本报告发现问题，不修复原运行；没有发布新体系成绩，也没有把原来的科学结果静默改写。", ""])
    return "\n".join(lines)


def development(systems: Path, dataset: Path, start: int, count: int | None, report: dict) -> None:
    manifest, records = load_dataset(dataset)
    require(manifest["split"] != "test", "Development checks must not inspect held-out test boards.")
    selected = records[start:] if count is None else records[start:start + count]
    require(start >= 0 and bool(selected) and (count is None or len(selected) == count), "Invalid development slice.")
    a, b = load_system(systems / "ccba.json"), load_system(systems / "prec.json")
    snapshot = source_snapshot()
    report.update({"dataset_hash": manifest["boards_sha256"], "dataset_split": manifest["split"],
                   "seed": manifest["seed"], "start": start, "boards": len(selected),
                   "systems": {"ccba": a.content_hash, "prec": b.content_hash},
                   "source_hash": canonical_hash(snapshot), "completed_boards": 0, "tables": 0, "bound_checked_decisions": 0})
    hits = Counter()
    for record in selected:
        report["active_board"] = record.index
        for ns, ew in ((a, b), (b, a)):
            result = run_auction(ns, ew, record.board, compact=True, trace=True)
            require(result["auction"][-1]["call"] == "P", f"Unfinished board {record.index}.")
            report["bound_checked_decisions"] += verify_bounds(result["auction"], record.board)
            hits.update(step["private_trace"]["rule_id"] for step in result["auction"])
            report["tables"] += 1
        report["completed_boards"] += 1
        if report["completed_boards"] % 64 == 0:
            print(json.dumps({"completed_boards": report["completed_boards"], "tables": report["tables"]}), file=sys.stderr, flush=True)
    require(source_snapshot() == snapshot, "Implementation changed during development verification.")
    report.update({"active_board": None, "rule_hits": dict(sorted(hits.items())), "invalid_boards": 0,
                   "deleted_or_replaced_boards": 0, "oracle_kind": "not_used_bidding_only"})


def audit(directory: Path, report: dict) -> None:
    run = json.loads((directory / "run.json").read_text())
    require(run["status"] == "completed", "Only a completed run can be audited.")
    require(source_snapshot() == run["source_files"], "Current source differs from the frozen run.")
    for name, expected in run["artifacts_sha256"].items():
        require(Path(name).name == name and file_hash(directory / name) == expected, f"Artifact hash mismatch: {name}.")
    require(file_hash(directory / "dataset-boards.jsonl") == run["dataset_hash"], "Frozen dataset hash mismatch.")
    records = []
    for raw in json_rows(directory / "dataset-boards.jsonl"):
        board = parse_board(raw["board"])
        require(raw["index"] == len(records) and raw["family_id"] == family_hash(board.deal), "Frozen board identity mismatch.")
        records.append(BoardRecord(raw["index"], raw["family_id"], board))
    boards = list(json_rows(directory / "boards.jsonl"))
    require(len(boards) == len(records) == run["completed_boards"] == run["boards"], "Board count mismatch.")
    a = load_system(directory / "systems" / (run["system_a"]["hash"] + ".json"))
    b = load_system(directory / "systems" / (run["system_b"]["hash"] + ".json"))
    require(a.content_hash == run["system_a"]["hash"] and b.content_hash == run["system_b"]["hash"], "System identity mismatch.")
    seen = set()
    bound_checked = 0
    first_calls = {}
    for played in json_rows(directory / "auctions.jsonl"):
        index, table_name = played["index"], played["table"]
        key = (index, table_name)
        require(key not in seen and table_name in ("table1", "table2"), "Duplicate or unknown table.")
        seen.add(key)
        record = records[index]
        bound_checked += verify_bounds(played["auction"], record.board)
        calls = tuple(parse_call(step["call"]) for step in played["auction"])
        actor = record.board.dealer
        for step in played["auction"]:
            require(step["seat"] == actor.value, f"Wrong acting seat on board {index}.")
            actor = actor.next()
        final = final_contract(replay(record.board.dealer, calls))
        expected_contract = ({"level": final.level, "strain": final.strain.value, "declarer": final.declarer.value,
                              "doubling": final.doubling} if isinstance(final, Contract) else {"passed_out": True})
        require(played["contract"] == expected_contract, f"Contract/declarer mismatch on board {index}.")
        score = score_ns(final, played["tricks"], record.board.vulnerability) if isinstance(final, Contract) else 0
        saved = boards[index][table_name]
        require(score == played["score_ns"] == saved["score_ns"], f"Score mismatch on board {index}.")
        require(saved["contract"] == played["contract"] and saved["tricks"] == played["tricks"], f"Table log mismatch on board {index}.")
        if index < 5:
            first_calls[key] = tuple(step["call"] for step in played["auction"])
    require(len(seen) == 2 * len(records), "Missing table histories.")
    values = []
    for index, row in enumerate(boards):
        require(row["index"] == index and row["family_id"] == records[index].family_id, "Score row identity mismatch.")
        difference = row["table1"]["score_ns"] - row["table2"]["score_ns"]
        require(row["point_difference_a"] == difference and row["imp_a"] == signed_imp(difference), f"IMP mismatch on board {index}.")
        values.append(row["imp_a"])
    summary = json.loads((directory / "summary.json").read_text())
    repeated = paired_summary(values, seed=run["config"]["bootstrap_seed"], resamples=run["config"]["bootstrap_resamples"])
    for key, value in repeated.items():
        require(summary[key] == value, f"Summary mismatch: {key}.")
    adapter = DDSAdapter()
    require(adapter.provenance.to_dict()["library_sha256"] == run["oracle"]["library_sha256"], "DDS build identity mismatch.")
    for record in records[:5]:
        table = adapter.solve(record.board.deal)
        for table_name, ns, ew in (("table1", a, b), ("table2", b, a)):
            saved = boards[record.index][table_name]
            if "passed_out" not in saved["contract"]:
                contract = saved["contract"]
                require(table.tricks(Seat(contract["declarer"]), Strain(contract["strain"])) == saved["tricks"], "Fresh DDS result mismatch.")
            replayed = run_auction(ns, ew, record.board, compact=True)
            require(tuple(step["call"] for step in replayed["auction"]) == first_calls[(record.index, table_name)], "Frozen policy replay mismatch.")
    report.update({"boards": len(records), "tables_legally_replayed": len(seen), "recomputed_scores": len(seen),
                   "score_check": "recomputed through core scoring API, not an independent scoring implementation",
                   "fresh_dds_table_crosschecks": min(5, len(records)), "policy_replayed_tables": min(10, 2 * len(records)),
                   "artifact_hashes_verified": True, "source_hash": run["source_hash"], "summary_reproduced": True,
                   "bound_checked_decisions": bound_checked})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    dev = commands.add_parser("development")
    dev.add_argument("--systems", type=Path, required=True)
    dev.add_argument("--dataset", type=Path, required=True)
    dev.add_argument("--start", type=int, default=0)
    dev.add_argument("--count", type=int)
    checked = commands.add_parser("audit")
    checked.add_argument("--run", type=Path, required=True)
    semantics = commands.add_parser("semantics")
    semantics.add_argument("--systems", type=Path, required=True)
    semantics.add_argument("--run", type=Path, required=True)
    contracts = commands.add_parser("contracts")
    contracts.add_argument("--systems", type=Path, required=True)
    for command in (dev, checked, semantics, contracts):
        command.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("Output already exists; verification records are immutable.")
    if arguments.command == "semantics" and arguments.output.with_suffix(".md").exists():
        parser.error("The semantic Markdown report already exists.")
    report = {"schema_version": 1, "mode": arguments.command, "status": "running", "errors": []}
    try:
        if arguments.command == "development":
            development(arguments.systems, arguments.dataset, arguments.start, arguments.count, report)
        elif arguments.command == "semantics":
            semantic_review(arguments.systems, arguments.run, report)
        elif arguments.command == "contracts":
            semantic_contracts(arguments.systems, report)
        else:
            audit(arguments.run, report)
        if report["status"] == "running":
            report["status"] = "passed"
    except (OSError, ValueError, RuntimeError) as error:
        report["status"] = "failed"
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with arguments.output.open("x", encoding="utf8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2, sort_keys=True)
        target.write("\n")
    if arguments.command == "semantics" and report["status"] != "failed":
        destination = arguments.output.with_suffix(".md")
        with destination.open("x", encoding="utf8") as target:
            target.write(semantic_markdown(report, destination))
    print(json.dumps({"status": report["status"], "report": str(arguments.output), "errors": report["errors"]}, ensure_ascii=False))
    return 0 if report["status"] in ("passed", "completed-with-findings") else 1


if __name__ == "__main__":
    raise SystemExit(main())