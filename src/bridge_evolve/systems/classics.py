"""Auditable engineering translations of the supplied CCBA/Precision manuals.

This module compiles expressions into ordinary v2 IR. It is never called by a
player at bidding time; no hand-dependent Python policy is hidden behind it.
"""

from __future__ import annotations

import json
import hashlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from bridge_evolve.systems.language import SystemDefinition, validate_system
from bridge_evolve.systems.render import render_system

SUITS = ("S", "H", "D", "C")
SOURCE_HASHES = {
    "ccba": "f0830fca5d16e994e91f9e910b6a55bee9a910d9cd58385a7af6260b5d93f8b9",
    "prec": "130cb74d34a16117ce7df1e82d9a7963b752f945d815bfa793d9375986f7633d",
}
PROFILE_VERSION = "translation-v5.5.4"


def read_manual(path: Path) -> tuple[tuple[int, str], ...]:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    symbols = {"F0A7": "C", "F0A8": "D", "F0A9": "H", "F0AA": "S", "F0D6": "sqrt"}
    with zipfile.ZipFile(path) as document:
        root = ElementTree.fromstring(document.read("word/document.xml"))
    paragraphs = []
    for index, paragraph in enumerate(root.iter(f"{{{namespace}}}p"), 1):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{{{namespace}}}t":
                parts.append(node.text or "")
            elif node.tag == f"{{{namespace}}}sym" and node.get(f"{{{namespace}}}font") == "Symbol":
                code = node.get(f"{{{namespace}}}char")
                if code not in symbols:
                    raise ValueError(f"Unsupported manual symbol {code} at {path.name}:P{index}.")
                parts.append(symbols[code])
            elif node.tag in (f"{{{namespace}}}tab", f"{{{namespace}}}br"):
                parts.append(" ")
        text = " ".join("".join(parts).split())
        paragraphs.append((index, text))
    return tuple(paragraphs)


def op(name, *args):
    return {"op": name, "args": list(args)}


def all_of(*args):
    return op("and", *args) if args else True


def any_of(*args):
    return op("or", *args) if args else False


def eq(a, b):
    return op("eq", a, b)


def ge(a, b):
    return op("ge", a, b)


def le(a, b):
    return op("le", a, b)


def no(a):
    return op("not", a)


def within(value, low, high):
    return op("between", value, low, high)


def choose(condition, yes, otherwise):
    return op("if", condition, yes, otherwise)


def ctx(name):
    return {"op": "context", "name": name}


def var(name):
    return {"op": "var", "name": name}


def ref(name):
    return {"op": "ref", "name": name}


def length(suit):
    return {"op": "length", "suit": suit}


def honors(suit, ranks):
    return {"op": "honors", "suit": suit, "ranks": ranks}


def card(suit, rank):
    return ge(honors(suit, rank), 1)


def number_if(condition):
    return choose(condition, 1, 0)


def add(*values):
    return op("add", *values) if len(values) > 1 else values[0]


def bid(level, suit):
    return {"kind": "bid", "level": level, "strain": suit}


def fixed(call):
    return {"kind": "call", "call": call}


def cheapest(suits, low=1, high=7):
    return {"kind": "cheapest", "strains": list(suits) if isinstance(suits, tuple) else suits,
            "min_level": low, "max_level": high}


def legal(call):
    return {"op": "is_legal", "call": call}


def can_bid_suit(suit, low=1, high=7):
    return any_of(*(legal(str(level) + suit) for level in range(low, high + 1)))


def state(name):
    return eq({"op": "state"}, name)


def ours():
    return eq(ctx("event.by"), "ours")


def event(call):
    return all_of(ours(), eq(ctx("event.call"), call))


def support(suit, minimum=3):
    return ge(length(suit), minimum)


def longest(suit, candidates=SUITS, minimum=1):
    # Equal lengths follow the explicit S,H,D,C ordering, except major 4-4
    # responses which are overridden by a more specific rule below.
    index = candidates.index(suit)
    return all_of(ge(length(suit), minimum), *(
        op("gt" if i < index else "ge", length(suit), length(other))
        for i, other in enumerate(candidates) if other != suit
    ))


def feature_definitions():
    hcp = {"op": "hcp"}
    features = {"hcp": hcp, "nt_basic": {"op": "balanced"}}
    features["controls"] = add(*(add(op("mul", honors(suit, "A"), 2), honors(suit, "K")) for suit in SUITS))
    for suit in SUITS:
        features[f"good_{suit}"] = all_of(ge(length(suit), 5), ge(honors(suit, "AKQ"), 2))
        features[f"solid_{suit}"] = all_of(ge(length(suit), 6), eq(honors(suit, "AKQ"), 3))
        features[f"control_{suit}"] = any_of(card(suit, "A"), card(suit, "K"), le(length(suit), 1))
        features[f"stop_{suit}"] = any_of(
            card(suit, "A"), all_of(card(suit, "K"), ge(length(suit), 2)),
            all_of(card(suit, "Q"), ge(length(suit), 3)),
            all_of(eq(honors(suit, "JT"), 2), ge(length(suit), 4)),
        )
        extra = op("max", op("sub", length(suit), 4), 0)
        features[f"playing2_{suit}"] = choose(
            eq(honors(suit, "AKQ"), 3), op("mul", length(suit), 2),
            choose(eq(honors(suit, "AK"), 2), add(4, extra),
                   choose(eq(honors(suit, "AQ"), 2), add(3, extra),
                          choose(card(suit, "A"), 2,
                                 choose(all_of(card(suit, "K"), ge(length(suit), 2)), 1, 0)))),
        )
    features["playing_tricks2"] = add(*(ref(f"playing2_{suit}") for suit in SUITS))
    features["nt_extended"] = any_of(
        ref("nt_basic"),
        all_of(eq({"op": "shape"}, [5, 4, 2, 2]),
               any_of(ge(length("C"), 5), ge(length("D"), 5))),
        all_of(eq({"op": "shape"}, [6, 3, 2, 2]),
               any_of(eq(length("C"), 6), eq(length("D"), 6))),
    )
    features["nt_precision"] = any_of(
        ref("nt_basic"), all_of(eq({"op": "shape"}, [5, 4, 2, 2]),
                                any_of(eq(length("C"), 5), eq(length("D"), 5))),
    )
    features["am_captain"] = eq({"op": "seat"}, var("captain"))
    features["partner_min"] = ctx("partner_hcp_min")
    features["partner_max"] = ctx("partner_hcp_max")
    features["total_min"] = op("max", add(hcp, ref("partner_min")), ctx("partnership_hcp_min"))
    features["total_max"] = op("min", add(hcp, ref("partner_max")), ctx("partnership_hcp_max"))
    features["total_mid2"] = add(ref("total_min"), ref("total_max"))
    features["own_min"] = choose(ref("am_captain"), var("captain_hcp_min"), var("responder_hcp_min"))
    features["own_extra"] = all_of(ge(hcp, 8), ge(hcp, add(ref("own_min"), 2)))
    features["game_possible"] = ge(ref("total_max"), 25)
    features["game_invite_values"] = all_of(ref("game_possible"), any_of(
        ge(ref("total_min"), 23), all_of(ref("own_extra"), any_of(
            all_of(op("lt", ref("partner_max"), 40), ge(ref("total_mid2"), 46)),
            all_of(ge(ref("partner_min"), 11), ge(hcp, 10), ge(ref("controls"), 3))))))
    features["nt_slam_interest"] = all_of(ref("nt_extended"), ge(ref("total_max"), 33), ref("own_extra"),
        any_of(all_of(op("lt", ref("partner_max"), 40), ge(ref("total_mid2"), 62)),
               all_of(ge(hcp, 12), ge(ref("controls"), 4), ge(ref("total_min"), 28))))
    for suit_name in SUITS:
        partner_quality = choose(ref("am_captain"), var("responder_" + suit_name + "_support_quality"),
                                 var("captain_" + suit_name + "_support_quality"))
        features["support_min_" + suit_name] = op("max", ctx("partner_" + suit_name + "_min"),
                                                  choose(all_of(partner_quality, eq(honors(suit_name, "AKQ"), 3)), 4, 0))
        features["combined_" + suit_name] = op("max", add(length(suit_name), ref("support_min_" + suit_name)), ctx("partnership_" + suit_name + "_min"))
        features["fit_" + suit_name] = ge(ref("combined_" + suit_name), 8)
        shortage = any_of(*(le(length(side), 1) for side in SUITS if side != suit_name))
        short_bonus = op("max", *(choose(eq(length(side), 0), 3, choose(eq(length(side), 1), 2, 0))
                                  for side in SUITS if side != suit_name))
        fit_bonus = op("min", 2, op("max", 0, op("sub", ref("combined_" + suit_name), 8)))
        wasted = add(*(choose(le(ctx("partner_" + side + "_max"), 1),
                            add(op("mul", honors(side, "K"), 3), op("mul", honors(side, "Q"), 2), honors(side, "J")), 0)
                       for side in SUITS if side != suit_name))
        no_waste = any_of(*(all_of(le(ctx("partner_" + side + "_max"), 1),
                                   eq(honors(side, "KQJ"), 0)) for side in SUITS if side != suit_name))
        features["fit_value_" + suit_name] = op("sub", add(ref("total_min"), short_bonus, fit_bonus), wasted)
        features["shape_values_" + suit_name] = all_of(ge(hcp, 8), ge(ref("controls"), 3), any_of(
            all_of(ge(ref("combined_" + suit_name), 9), shortage),
            all_of(ge(length(suit_name), 6), ref("good_" + suit_name)),
            all_of(no_waste, ge(ref("controls"), 4))))
        features["slam_willing_" + suit_name] = all_of(ge(ref("controls"), 3),
            any_of(ref("own_extra"), ref("shape_values_" + suit_name), ge(ref("fit_value_" + suit_name), 31)))
        features["slam_possible_" + suit_name] = any_of(ge(ref("total_max"), 32), all_of(
            ge(ref("total_max"), 29), ref("shape_values_" + suit_name)))
        features["small_slam_values_" + suit_name] = all_of(ref("slam_possible_" + suit_name),
            ge(ref("fit_value_" + suit_name), 30))
    features["have_fit"] = any_of(*(all_of(eq(var("trump"), suit_name), ref("fit_" + suit_name)) for suit_name in SUITS))
    preferred = ("H", "S", "D", "C")
    for index, suit_name in enumerate(preferred):
        features["prefer_" + suit_name] = any_of(
            all_of(eq(var("trump"), suit_name), ref("fit_" + suit_name)),
            all_of(no(ref("have_fit")), ref("fit_" + suit_name), *(no(ref("fit_" + other)) for other in preferred[:index])),
        )
    features["keys"] = {"op": "keycards", "args": [var("trump")]}
    features["trump_q"] = ge(honors(var("trump"), "Q"), 1)
    features["excluded_keys"] = add(honors(var("trump"), "K"), *(
        choose(eq(var("void_suit"), suit), 0, honors(suit, "A")) for suit in SUITS
    ))
    features["enemy_stopped"] = choose(
        eq(ctx("opponent_bid_strain"), ""), True,
        choose(eq(ctx("opponent_bid_strain"), "NT"), True, any_of(*(
            all_of(eq(ctx("opponent_bid_strain"), suit), ref(f"stop_{suit}")) for suit in SUITS
        ))),
    )
    features["all_stopped"] = all_of(*(ref(f"stop_{suit}") for suit in SUITS))
    return features


class Compiler:
    def __init__(self, kind):
        self.precision = kind == "prec"
        self.kind = kind
        self.raw = {
            "schema_version": 2, "operator_version": 2,
            "name": f"{'精确1.1.1' if self.precision else 'CCBA2.2'} 工程移植 v5.5.4（公开配合与坚固套编码消歧）",
            "scope": "general", "features": feature_definitions(), "announcements": [], "rules": [],
            "protocol": {"initial": "idle", "parameters": {}, "states": {}},
            "fallback": {"kind": "error", "message": "No declared classic-policy completion"},
        }
        self.sequence = 0
        self.public_conditions = {}
        scalar = {
            "captain": ("string", ""), "open_suit": ("string", ""), "response_suit": ("string", ""),
            "trump": ("string", ""), "opening_kind": ("string", ""),
            "open_min": ("number", 0), "open_max": ("number", 40),
            "res_min": ("number", 0), "res_max": ("number", 40),
            "nt_level": ("number", 1), "transfer": ("string", ""),
            "nt_style": ("string", "strong"),
            "anchor": ("string", ""), "asker": ("string", ""),
            "answer_min": ("number", 0), "answer_q": ("bool", False),
            "invite_target": ("string", ""), "short_suit": ("string", ""),
            "rebid_suit": ("string", ""), "fourth_suit": ("string", ""),
            "help_suit": ("string", ""),
            "two_suit_a": ("string", ""), "two_suit_b": ("string", ""),
            "enemy_suit": ("string", ""),
            "void_suit": ("string", ""), "queen_high": ("bool", False),
            "captain_hcp_min": ("number", 0), "responder_hcp_min": ("number", 0),
            "captain_hcp_max": ("number", 40), "responder_hcp_max": ("number", 40),
            "double_kind": ("string", ""),
            "slam_active": ("bool", False), "slam_started": ("bool", False), "slam_solid": ("bool", False),
            "game_invited": ("bool", False), "nt_slam_invited": ("bool", False),
        }
        for seat_name in ("N", "E", "S", "W"):
            scalar["seat_" + seat_name + "_hcp_max"] = ("number", 40)
        for prefix in ("captain_", "responder_"):
            for suit_name in SUITS:
                scalar[prefix + suit_name + "_min"] = ("number", 0)
                scalar[prefix + suit_name + "_max"] = ("number", 13)
                for quality in ("support_quality", "support_denied", "control"):
                    scalar[prefix + suit_name + "_" + quality] = ("bool", False)
                for other in SUITS:
                    if other != suit_name:
                        scalar[prefix + suit_name + "_ge_" + other] = ("bool", False)
        for name, (kind_type, initial) in scalar.items():
            parameter = {"type": kind_type, "initial": initial}
            if name == "trump":
                parameter["values"] = ["", "C", "D", "H", "S"]
            self.raw["protocol"]["parameters"][name] = parameter
        for phase in (
            "idle", "major", "minor", "nt", "strong_club", "strong_two", "weak_two",
            "club_two", "three_suit", "preempt", "gambling", "gambling_correct", "opener_rebid",
            "natural", "competition", "stayman", "stayman_finish", "transfer",
            "transfer_finish", "minor_transfer", "minor_finish", "jacoby",
            "jacoby_finish", "inverted", "inverted_finish", "strong_negative",
            "strong_positive", "shape_ask", "shape_answer", "support_answer",
            "weak_ask", "weak_finish", "puppet", "puppet_finish", "rkcb", "rkcb_choose",
            "puppet_reveal", "quantitative", "gerber", "gerber_finish", "finished",
            "checkback", "checkback_relay", "checkback_invite", "checkback_force",
            "checkback_clubs", "checkback_club_finish",
            "responder_rebid", "fourth_answer", "raised_major", "raise_answer",
            "reverse_reply", "reverse_wait", "reverse_choose",
            "mini_ask", "mini_answer", "mini_choose", "simple_raise", "help_answer",
            "drury_answer", "drury_finish", "bergen_answer",
            "two_suit_select", "two_suit_minor", "two_suit_pick", "landy_answer",
            "nt_interference", "lebensohl_relay", "lebensohl_finish", "nt_cue_reply",
            "nt_doubled", "nt_escape", "nt_escape_finish",
            "precision_point_shape", "precision_shape_ask", "precision_shape_reply",
            "precision_four_ask", "precision_four_reply", "precision_club_ask",
            "precision_club_finish", "precision_club_relay", "precision_club_reveal",
            "precision_three_ask", "precision_three_finish", "precision_three_transfer",
            "queen_answer", "queen_choose", "king_answer", "king_choose", "void_answer", "void_choose",
            "smolen_reply", "minor_stayman", "minor_stayman_finish", "nt_minor_shape",
            "transfer_nt_choice", "negative_answer",
            "penalty_defense", "strong_4441_finish", "slam_cue",
        ):
            self.raw["protocol"]["states"][phase] = {"transitions": [], "otherwise": phase}

    def rule(self, name, condition, action, phase=None):
        self.sequence += 1
        self.raw["rules"].append({
            "id": f"{self.kind}.{name}.r{self.sequence}", "priority": 100000 - self.sequence,
            "when": all_of(state(phase), condition) if phase else condition, "action": action,
        })

    def transition(self, phase, name, condition, target, updates=None, forcing=None, allow_pass=False):
        if target == "finished" and forcing in (None, "none"):
            for level, terminal_goal in ((7, "grand_slam"), (6, "small_slam")):
                self.transition(phase, name + "_" + terminal_goal, all_of(condition, ours(),
                    eq(ctx("event.kind"), "BID"), eq(ctx("event.level"), level)), target, updates, terminal_goal, allow_pass)
            self.transition(phase, name + "_game", all_of(condition, ours(), eq(ctx("event.kind"), "BID"), any_of(
                all_of(eq(ctx("event.strain"), "NT"), ge(ctx("event.level"), 3)),
                all_of(op("in", ctx("event.strain"), ["H", "S"]), ge(ctx("event.level"), 4)),
                all_of(op("in", ctx("event.strain"), ["C", "D"]), ge(ctx("event.level"), 5)),
            )), target, updates, "game", allow_pass)
        values = dict(updates or {})
        if isinstance(values.get("short_suit"), str) and values["short_suit"] in SUITS:
            self.announce(f"{phase}.{name}.shortness.{len(self.raw['announcements'])}",
                          all_of(state(phase), condition), ["artificial"])
        if target in ("rkcb", "gerber", "void_answer"):
            forcing = "slam_try"
        elif target == "king_answer":
            forcing = "invite_grand"
        elif target == "quantitative":
            forcing = "invite_game" if values.get("invite_target") == "game" else "slam_try"
        elif phase == "checkback_invite" and name.startswith("invite_"):
            forcing = "invite_game"
        for source, destination in (("open_min", "captain_hcp_min"), ("res_min", "responder_hcp_min")):
            if source in values:
                values[destination] = op("max", var(destination), values[source])
        for source, destination in (("open_max", "captain_hcp_max"), ("res_max", "responder_hcp_max")):
            if source in values:
                values[destination] = op("min", var(destination), values[source])
        shown = values.get("response_suit")
        if shown in ("H", "S") and phase in ("stayman", "puppet", "transfer", "checkback_force"):
            minimum = 5 if phase == "puppet" else 3 if phase == "checkback_force" else 4
            if phase == "transfer":
                minimum = choose(all_of(eq(var("nt_level"), 1), eq(ctx("event.level"), 3)), 4, 2)
            key = "captain_" + shown + "_min"
            values[key] = op("max", var(key), minimum)
        trump = values.get("trump")
        if trump in SUITS and phase == "opener_rebid" and not name.startswith("slam_"):
            key = "captain_" + trump + "_min"
            values[key] = op("max", var(key), 4)
        if phase == "idle" and values.get("opening_kind") in ("michaels", "unusual_nt", "landy", "marshall", "leaping_michaels"):
            minimum = 4 if values["opening_kind"] in ("landy", "marshall") else 5
            for field in ("two_suit_a", "two_suit_b"):
                if values.get(field) in SUITS:
                    values["captain_" + values[field] + "_min"] = minimum
        if phase == "idle" and name == "natural_overcall":
            for suit_name in SUITS:
                values["captain_" + suit_name + "_min"] = choose(eq(ctx("event.strain"), suit_name), 5, 0)
            values["open_min"] = choose(eq(ctx("event.level"), 2), 11, 8)
            values["captain_hcp_min"] = op("max", var("captain_hcp_min"), values["open_min"])
        if phase == "competition" and name == "advance":
            supported = eq(ctx("event.strain"), var("open_suit"))
            advance_double = eq(var("opening_kind"), "double")
            first_reply = all_of(eq(ctx("our_bid_count"), choose(advance_double, 0, 1)), eq(ctx("forcing"), "none"),
                                 no(eq(ctx("event.actor"), var("captain"))))
            for suit_name in SUITS:
                key = "responder_" + suit_name + "_min"
                values[key] = op("max", var(key), choose(all_of(first_reply, eq(ctx("event.strain"), suit_name)),
                    choose(advance_double, 4, choose(supported, 3, 5)), 0))
            natural_minimum = choose(eq(var("opening_kind"), "strong_club"), 8,
                choose(eq(ctx("event.level"), 1), 6, choose(eq(ctx("event.level"), 2), 11, 12)))
            values["res_min"] = choose(all_of(first_reply, no(advance_double), no(eq(ctx("event.strain"), "NT"))),
                choose(le(var("responder_hcp_max"), 7 if self.precision else 5), 0,
                       choose(supported, 6, natural_minimum)), 0)
            values["responder_hcp_min"] = op("max", var("responder_hcp_min"), values["res_min"])
        if phase == "major" and name == "two_over_one_hearts":
            values["responder_H_min"] = 5
        if phase == "minor" and name.startswith("inverted_"):
            values["responder_" + name.removeprefix("inverted_") + "_min"] = 4
        if phase in ("precision_club_ask", "negative_answer"):
            for suit_name in ("H", "S"):
                values["captain_" + suit_name + "_min"] = op("max", var("captain_" + suit_name + "_min"),
                    choose(all_of(ours(), eq(ctx("event.strain"), suit_name),
                        eq(ctx("event.level"), 2) if phase == "precision_club_ask" else True), 4, 0))
        if name == "negative_double":
            values["double_kind"] = "negative"
            for suit_name, other in (("H", "S"), ("S", "H")):
                promised = all_of(no(eq(var("opening_kind"), "strong_club")),
                    no(eq(ctx("opponent_bid_strain"), suit_name)),
                    any_of(eq(ctx("opponent_bid_strain"), other), eq(var("open_suit"), other)))
                values["responder_" + suit_name + "_min"] = op("max", var("responder_" + suit_name + "_min"), choose(promised, 4, 0))
        elif name in ("takeout", "marshall_double"):
            values["double_kind"] = "takeout" if name == "takeout" else "two_suited"
        for key in tuple(values):
            if key.startswith(("captain_", "responder_")) and key.endswith("_min"):
                values[key] = op("max", var(key), values[key])
        previous = self.public_conditions.setdefault(phase, [])
        exclusive = all_of(condition, no(any_of(*previous))) if previous else condition
        previous.append(condition)
        self.raw["protocol"]["states"][phase]["transitions"].append({
            "id": f"{self.kind}.{phase}.{name}.t{len(previous)}", "when": exclusive, "to": target,
            "set": values, **({"forcing": forcing} if forcing is not None else {}),
            **({"allow_pass": True} if allow_pass else {}),
        })

    def announce(self, identifier, when, tags, shown=None):
        self.raw["announcements"].append({
            "id": identifier, "when": when, "tags": list(tags),
            **({"suit": shown} if shown is not None else {}),
        })

    def first_call(self, call, phase, low, high, shown="", opening_kind=""):
        initial = all_of(ours(), no({"op": "has_contract_bid"}), eq(ctx("event.call"), call))
        lengths = {}
        if phase == "three_suit":
            lengths = {"captain_" + suit_name + "_min": 4 for suit_name in ("H", "S", "C")}
        elif shown == "NT":
            lengths = {"captain_" + suit_name + "_min": 2 for suit_name in SUITS}
        elif shown in SUITS:
            minimum = 7 if phase == "preempt" else 6 if phase == "weak_two" else 5 if phase in ("major", "club_two") else 0 if self.precision else 3
            lengths["captain_" + shown + "_min"] = minimum
        self.transition("idle", "open_" + call, initial, phase, {
            "captain": ctx("event.actor"), "open_suit": shown,
            "open_min": low, "open_max": high, "opening_kind": opening_kind or phase,
            "nt_level": 2 if call == "2NT" else 1,
            **lengths,
        })

    def response(self, phase, call, target="opener_rebid", low=0, high=40, shown="", fit=False, forcing=None, only=True, **updates):
        values = {"res_min": low, "res_max": high, "response_suit": shown, **updates}
        if fit:
            values["trump"] = var("open_suit") if fit is True else fit
            minimum = 4 if target in ("jacoby", "mini_ask", "bergen_answer", "inverted") or "short_suit" in updates else 3
            for suit_name in SUITS:
                key = "responder_" + suit_name + "_min"
                values[key] = op("max", var(key), choose(eq(values["trump"], suit_name), minimum, 0))
        if shown in SUITS and not fit:
            minimum = 0
            if target == "transfer":
                minimum = 6 if call.startswith("4") else 5
            elif phase in ("strong_club", "strong_two", "club_two"):
                minimum = 5
            elif phase in ("minor", "major") and call.startswith("1"):
                minimum = 4
            elif phase == "minor" and call == "2C":
                minimum = 5 if self.precision else 4
            key = "responder_" + shown + "_min"
            values[key] = op("max", var(key), minimum)
        self.transition(phase, "reply_" + call, all_of(event(call), only), target, values, forcing)

    def game_invitation_updates(self):
        updates = {"game_invited": True}
        for owner, actor_is_owner in (
            ("captain", eq(ctx("event.actor"), var("captain"))),
            ("responder", no(eq(ctx("event.actor"), var("captain")))),
        ):
            other = "responder" if owner == "captain" else "captain"
            lower = op("max", 0, op("sub", 25, var(other + "_hcp_max")),
                op("min", op("sub", 23, var(other + "_hcp_min")), add(var(owner + "_hcp_min"), 2)))
            key = owner + "_hcp_min"
            updates[key] = op("max", var(key), choose(actor_is_owner, lower, 0))
        return updates

    def compile(self):
        self.openings()
        self.slam_cuebids()
        self.fit_conventions()
        self.precision_conventions()
        self.responses()
        self.nt_extensions()
        self.notrump()
        self.suit_rebids()
        self.checkback()
        self.rebids()
        self.slam_continuations()
        self.questions()
        self.manual_competition()
        self.competitive_conventions()
        self.competition()
        self.completion()
        # Public interference is explicit. Complex source subagreements not
        # represented by a dedicated state enter the declared natural policy.
        for phase in list(self.raw["protocol"]["states"]):
            if phase not in ("idle", "competition"):
                self.transition(phase, "opponent_interference",
                                all_of(eq(ctx("event.by"), "opponents"), no(eq(ctx("event.kind"), "P"))),
                                "competition")
        self.public_information()
        # Eliminate compilation-only unused states, never silently failed deals.
        states = self.raw["protocol"]["states"]
        reachable = {"idle"}
        while True:
            expanded = reachable | {transition["to"] for name in reachable for transition in states[name]["transitions"]}
            if expanded == reachable:
                break
            reachable = expanded
        removed = set(states) - reachable
        self.raw["protocol"]["states"] = {name: value for name, value in states.items() if name in reachable}
        self.raw["rules"] = [
            item for item in self.raw["rules"]
            if not any(_state_comparison(item["when"], name) for name in removed)
        ]
        self.raw["announcements"] = [
            item for item in self.raw["announcements"]
            if not any(_state_comparison(item["when"], name) for name in removed)
        ]
        return validate_system(self.raw)

    def public_information(self):
        facts = {}
        orders = {}

        def promise(phases, strain, minimum, condition=True, owner="actor", agreed=False, maximum=None):
            if isinstance(phases, str):
                phases = (phases,)
            for phase in phases:
                facts.setdefault(phase, []).append((strain, minimum, maximum, all_of(ours(), eq(ctx("event.kind"), "BID"), condition), owner, agreed))

        def order(phase, longer, shorter, condition, owner="captain"):
            orders.setdefault(phase, []).append((longer, shorter, all_of(ours(), condition), owner))

        def belongs(owner, prefix):
            captain = eq(ctx("event.actor"), var("captain"))
            return True if owner == prefix else False if owner != "actor" else captain if prefix == "captain" else no(captain)

        unopened = no({"op": "has_contract_bid"})
        for major in ("H", "S"):
            other = "S" if major == "H" else "H"
            order("idle", major, other, all_of(unopened, event("1" + major)))
            promise("idle", other, 0, all_of(unopened, event("2" + major)), owner="captain", maximum=3)
            for minor in (("D",) if self.precision else ("C", "D")):
                promise("idle", major, 0, all_of(unopened, event("1" + minor)), owner="captain", maximum=4)
        if self.precision:
            promise("idle", "C", 0, all_of(unopened, event("1D")), owner="captain", maximum=5)
            for suit in SUITS:
                promise("idle", suit, 0 if suit == "D" else 4, all_of(unopened, event("2D")),
                        owner="captain", maximum=1 if suit == "D" else 4 if suit in ("H", "S") else 5)
        else:
            order("idle", "D", "C", all_of(unopened, event("1D")))
            order("idle", "C", "D", all_of(unopened, event("1C")))
        for suit in SUITS:
            promise("idle", suit, 2, all_of(unopened, op("in", ctx("event.call"), ["1NT", "2NT"])),
                    owner="captain", maximum=5 if suit in ("H", "S") else 6)
        for major in ("H", "S"):
            promise("stayman", major, 0, op("in", ctx("event.strain"), ["D", "NT"]), owner="captain", maximum=3)
            promise("puppet", major, 0, eq(ctx("event.strain"), "NT"), owner="captain", maximum=3)
            promise("puppet", major, 0, eq(ctx("event.strain"), "D"), owner="captain", maximum=4)
        promise("stayman", "H", 0, eq(ctx("event.strain"), "S"), owner="captain", maximum=3)
        promise("puppet", "H", 0, eq(ctx("event.strain"), "S"), owner="captain", maximum=4)
        if self.precision:
            for suit in SUITS:
                promise("strong_club", suit, 2, op("in", ctx("event.call"), ["1NT", "2S"]), owner="responder", maximum=4)

        for major in ("H", "S"):
            same_opening = eq(var("open_suit"), major)
            promise("major", major, 3, all_of(same_opening, event("2" + major)), agreed=True)
            promise("major", major, 3, all_of(same_opening, event("2C"), ctx("event.passed_before_opening")), agreed=True)
            promise("major", major, 5, all_of(same_opening, event("4" + major)), agreed=True)
            promise("major", major, 5, all_of(eq(var("open_suit"), "S"), event("2H"))) if major == "H" else None
            promise("opener_rebid", major, 4, all_of(event("1" + major), no(eq(var("response_suit"), major))))
        promise("major", "C", 3, all_of(event("2C"), no(ctx("event.passed_before_opening"))))
        promise("major", "D", 4, event("2D"))
        promise("idle", "D", 2, all_of(no({"op": "has_contract_bid"}), event("1D")), owner="captain") if self.precision else None
        for strain in SUITS:
            promise("idle", strain, 8, all_of(no({"op": "has_contract_bid"}), event("4" + strain)), owner="captain")
            promise("minor", strain, 5, all_of(eq(var("open_suit"), strain), event("3" + strain)), agreed=True)
            if strain in ("H", "S", "D"):
                promise("minor", strain, 6, all_of(no(eq(var("open_suit"), strain)), event("2" + strain)))
            natural_rebid = no(op("in", "artificial", ctx("event.tags")))
            promise("opener_rebid", strain, 5, all_of(eq(var("open_suit"), strain), eq(ctx("event.strain"), strain), natural_rebid))
            promise("opener_rebid", strain, 4, all_of(no(eq(var("open_suit"), strain)),
                    eq(ctx("event.strain"), strain), natural_rebid))
            promise(("strong_positive", "strong_negative"), strain, 3 if strain in ("H", "S") else 0,
                    all_of(eq(var("response_suit"), strain), eq(ctx("event.strain"), strain)), agreed=strain in ("H", "S"))
            for phase in ("strong_positive", "strong_negative"):
                promise(phase, strain, 2,
                        all_of(eq(ctx("event.strain"), "NT"),
                            no(all_of(eq(var("response_suit"), "NT"), eq(ctx("event.call"), "2NT"))),
                            no(all_of(self.precision, phase == "strong_positive",
                                      op("in", var("response_suit"), ["C", "D"])))))
            if strain in ("H", "S"):
                promise(("checkback_force", "fourth_answer", "reverse_wait"), strain, 3,
                        all_of(eq(var("response_suit"), strain), eq(ctx("event.strain"), strain)), agreed=True)
                promise("checkback", strain, 6, all_of(eq(var("response_suit"), strain), event("2" + strain)))
            promise("checkback_invite", strain, 5, all_of(eq(var("response_suit"), strain), event("3" + strain)))
            promise(("responder_rebid", "reverse_reply"), strain, 6,
                    all_of(eq(var("response_suit"), strain), eq(ctx("event.strain"), strain)))
            partner_minimum = choose(eq(ctx("event.actor"), var("captain")),
                var("responder_" + strain + "_min"), var("captain_" + strain + "_min"))
            game_support = op("max", 0, op("min", choose(eq(var("opening_kind"), "double"), 4, 5), op("sub", 8, partner_minimum)))
            promise(("natural", "competition", "finished"), strain, game_support,
                    all_of(eq(ctx("event.strain"), strain), ge(ctx("event.level"), 4 if strain in ("H", "S") else 5),
                        no(op("in", "artificial", ctx("event.tags"))),
                        any_of(eq(var("trump"), ""), eq(var("trump"), strain))), agreed=True)
            promise("competition", strain, op("max", 0, op("min", 4, op("sub", 8, partner_minimum))),
                    all_of(eq(var("opening_kind"), "nt"), eq(ctx("event.actor"), var("captain")),
                        eq(ctx("forcing"), "one_round"), eq(ctx("event.strain"), strain)))

        for short, call in (("S", "2H"), ("C", "2NT"), ("D", "3C"), ("H", "3D")):
            for strain in SUITS:
                if strain != short:
                    promise("strong_club", strain, 4, event(call))
        for major in ("H", "S"):
            promise("precision_point_shape", major, 4, event("2" + major))
            promise("precision_shape_reply", major, 4, event("3" + major))
            promise("precision_four_reply", major, 4, eq(ctx("event.strain"), major))
        for call, pair in (("3C", ("C",)), ("3D", ("D", "H")), ("3H", ("H", "S")), ("3S", ("S", "D"))):
            for strain in pair:
                promise("precision_point_shape", strain, 4, event(call))
        promise("precision_shape_reply", "D", 4, event("3NT"))
        for strain in ("C", "D"):
            promise("precision_four_reply", strain, 4, eq(ctx("event.strain"), strain if strain != "C" else "NT"))
            promise("nt", strain, 4, all_of(eq(var("nt_level"), 2), event("3S")))
            promise("nt", strain, 4, all_of(eq(var("nt_level"), 1), no(eq(var("nt_style"), "weak")),
                    op("in", ctx("event.call"), ["3H", "3S"])))
            promise("nt", strain, 6, all_of(eq(var("nt_level"), 1), no(eq(var("nt_style"), "weak")), event("3" + strain)))
            promise("minor_stayman", strain, 4, event("4" + strain), agreed=True)
            promise("nt_minor_shape", strain, 4, event("4" + strain), agreed=True)
        promise("minor_finish", "D", 6, event("3D"))
        for major, other, transfer_call in (("H", "S", "2D"), ("S", "H", "2H")):
            promise("nt", major, 5, all_of(eq(var("nt_style"), "weak"), eq(var("nt_level"), 1), event("2" + major)))
            promise("nt_doubled", major, 5, event(transfer_call))
            promise("stayman_finish", major, 4, all_of(eq(var("response_suit"), "D"), event("3" + major)))
            promise("stayman_finish", other, 5, all_of(eq(var("response_suit"), "D"), event("3" + major)))
            promise("smolen_reply", major, 3, all_of(eq(var("transfer"), major), event("4" + major)), agreed=True)
            promise("puppet_finish", major, 4, any_of(event("3" + other), event("4D")))
            promise("puppet_reveal", major, 4, event("4" + major), agreed=True)
            for phase in ("stayman_finish", "transfer_finish"):
                promise(phase, major, op("max", 0, op("sub", 8, var("captain_" + major + "_min"))),
                        all_of(eq(var("response_suit"), major), op("in", ctx("event.call"), ["3" + major, "4" + major])),
                        owner="responder", agreed=True)
        promise("gambling_correct", "D", 7, event("4D"))
        for major in ("H", "S"):
            promise(("preempt", "weak_two", "three_suit", "club_two", "gambling"), major, 7, event("4" + major))
        promise("nt_doubled", "D", 5, event("2C"))
        for strain in SUITS:
            promise("nt_interference", strain, 5,
                    all_of(no(eq(var("enemy_suit"), strain)), op("in", ctx("event.level"), [2, 3]), eq(ctx("event.strain"), strain)))
        promise("lebensohl_finish", "D", 5, event("3D"))
        for minor in ("C", "D"):
            promise("two_suit_minor", minor, 5, event("3" + minor))

        for phase in dict.fromkeys((*facts, *orders)):
            entries = facts.get(phase, ())
            current_state = self.raw["protocol"]["states"][phase]
            self.transition(phase, "remember_public_information", all_of(ours(), eq(ctx("event.kind"), "BID")), current_state["otherwise"])
            for transition in current_state["transitions"]:
                assignments = transition["set"]
                for strain, minimum, maximum, condition, owner, agreed in entries:
                    for prefix in ("captain", "responder"):
                        key = prefix + "_" + strain + "_min"
                        matches = all_of(condition, belongs(owner, prefix))
                        assignments[key] = op("max", assignments.get(key, var(key)), choose(matches, minimum, 0))
                        if maximum is not None:
                            upper = prefix + "_" + strain + "_max"
                            assignments[upper] = op("min", assignments.get(upper, var(upper)), choose(matches, maximum, 13))
                    if agreed:
                        assignments["trump"] = choose(condition, strain, assignments.get("trump", var("trump")))
                for longer, shorter, condition, owner in orders.get(phase, ()):
                    for prefix in ("captain", "responder"):
                        key = prefix + "_" + longer + "_ge_" + shorter
                        assignments[key] = any_of(assignments.get(key, var(key)), all_of(condition, belongs(owner, prefix)))
        for phase, current_state in self.raw["protocol"]["states"].items():
            for transition in current_state["transitions"]:
                assignments = transition["set"]
                if phase in ("natural", "competition", "finished", "quantitative"):
                    accepts = all_of(ours(), eq(ctx("forcing"), "invite_game"),
                                     op("in", ctx("event.call"), ["3NT", "4H", "4S"]))
                    for owner, actor_is_owner in (
                        ("captain", eq(ctx("event.actor"), var("captain"))),
                        ("responder", no(eq(ctx("event.actor"), var("captain")))),
                    ):
                        other = "responder" if owner == "captain" else "captain"
                        key = owner + "_hcp_min"
                        assignments[key] = op("max", assignments.get(key, var(key)),
                            choose(all_of(accepts, actor_is_owner), op("max", 0, op("sub", 25, var(other + "_hcp_min"))), 0))
                short = assignments.get("short_suit")
                if isinstance(short, str) and short in SUITS:
                    for prefix in ("captain", "responder"):
                        key = prefix + "_" + short + "_max"
                        assignments[key] = op("min", assignments.get(key, var(key)),
                                              choose(all_of(ours(), belongs("actor", prefix)), 1, 13))
                        control = prefix + "_" + short + "_control"
                        assignments[control] = any_of(assignments.get(control, var(control)), all_of(ours(), belongs("actor", prefix)))

    def openings(self):
        hcp = ref("hcp")
        unopened = no({"op": "has_contract_bid"})
        third = eq({"op": "role"}, "partner")
        fourth = eq({"op": "role"}, "rho")
        nt_shape = ref("nt_precision" if self.precision else "nt_extended")
        if not self.precision:
            self.rule("open.strong_two", all_of(unopened, any_of(
                ge(hcp, 22), all_of(ge(hcp, 18), ge(ref("playing_tricks2"), 18)),
            )), fixed("2C"))
        self.rule("open.two_nt", all_of(unopened, within(hcp, 20, 21), ref("nt_extended")), fixed("2NT"))
        if self.precision:
            self.rule("open.strong_club", all_of(unopened, ge(hcp, 16)), fixed("1C"))
        self.rule("open.one_nt", all_of(unopened, within(hcp, 13 if self.precision else 15,
                                                       15 if self.precision else 17), nt_shape), fixed("1NT"))
        for suit in ("S", "H"):
            self.rule("open.fourth_weak_" + suit, all_of(
                unopened, fourth, within(hcp, 9, 12), longest(suit, minimum=6), ref(f"good_{suit}"),
                le(length("H" if suit == "S" else "S"), 3),
            ), fixed("2" + suit))
        if self.precision:
            three_suit = all_of(eq(length("S"), 4), eq(length("H"), 4),
                               le(length("D"), 1), ge(length("C"), 4))
            self.rule("open.precision_three_suit", all_of(unopened, within(hcp, 11, 15), three_suit), fixed("2D"))
            self.rule("open.precision_clubs", all_of(unopened, within(hcp, 11, 15), any_of(
                ge(length("C"), 6), all_of(eq(length("C"), 5), any_of(eq(length("H"), 4), eq(length("S"), 4))),
            )), fixed("2C"))
        for suit in ("S", "H"):
            self.rule("open.major_" + suit, all_of(unopened,
                ge(hcp, choose(third, 9, 11)), longest(suit, ("S", "H"), minimum=5),
            ), fixed("1" + suit))
        if self.precision:
            self.rule("open.precision_diamond", all_of(unopened, within(hcp, 11, 15), ge(length("D"), 2)), fixed("1D"))
        else:
            self.rule("open.diamond", all_of(unopened, ge(hcp, 12), ge(length("D"), 3),
                                            any_of(op("gt", length("D"), length("C")),
                                                   all_of(eq(length("D"), length("C")), ge(length("D"), 4)))), fixed("1D"))
            self.rule("open.club", all_of(unopened, ge(hcp, 12), ge(length("C"), 3)), fixed("1C"))
        for suit in ("D", "C"):
            self.rule("open.gambling_" + suit, all_of(unopened, within(hcp, 9, 16),
                within(length(suit), 7, 8), ref(f"solid_{suit}"),
                *(eq(honors(other, "AK"), 0) for other in SUITS if other != suit),
            ), fixed("3NT"))
        for suit in SUITS:
            for level, minimum in ((4, 8), (3, 7)):
                self.rule(f"open.preempt_{level}_{suit}", all_of(unopened, no(fourth), within(hcp, 5, 10),
                    longest(suit, minimum=minimum),
                    choose({"op": "vulnerable"}, ref(f"good_{suit}"), True),
                ), fixed(str(level) + suit))
        for suit in (("S", "H") if self.precision else ("S", "H", "D")):
            self.rule("open.weak_" + suit, all_of(unopened, no(fourth), within(hcp, 6, 10),
                longest(suit, minimum=6), le(length("H" if suit == "S" else "S"), 3),
            ), fixed("2" + suit))
        self.rule("open.pass", unopened, fixed("P"))
        self.transition("idle", "pass_before_opening", all_of(ours(), unopened, event("P")), "idle", {
            "seat_" + seat_name + "_hcp_max": op("min", var("seat_" + seat_name + "_hcp_max"),
                choose(eq(ctx("event.actor"), seat_name), 10 if self.precision else 11, 40))
            for seat_name in ("N", "E", "S", "W")
        })
        self.first_call("2NT", "nt", 20, 21, "NT")
        self.first_call("1NT", "nt", 13 if self.precision else 15, 15 if self.precision else 17, "NT")
        for item in self.raw["protocol"]["states"]["idle"]["transitions"]:
            if "open_2NT" in item["id"]:
                item["set"]["nt_style"] = "two"
            elif "open_1NT" in item["id"]:
                item["set"]["nt_style"] = "weak" if self.precision else "strong"
        if self.precision:
            self.first_call("1C", "strong_club", 16, 40)
            self.first_call("2C", "club_two", 11, 15, "C")
            self.first_call("2D", "three_suit", 11, 15, "")
        else:
            self.first_call("2C", "strong_two", 18, 40)
            self.first_call("1C", "minor", 12, 21, "C")
        self.first_call("1D", "minor", 11 if self.precision else 12, 15 if self.precision else 21, "D")
        for suit in ("H", "S"):
            self.first_call("1" + suit, "major", choose(eq(ctx("event.position"), 3), 9, 11),
                            15 if self.precision else 21, suit)
        for suit in (("H", "S") if self.precision else ("D", "H", "S")):
            self.first_call("2" + suit, "weak_two", choose(eq(ctx("event.position"), 4), 9, 6),
                            choose(eq(ctx("event.position"), 4), 12, 10), suit)
        for suit in SUITS:
            self.first_call("3" + suit, "preempt", 5, 10, suit)
            self.first_call("4" + suit, "preempt", 5, 10, suit)
        self.first_call("3NT", "gambling", 9, 16)
        for call, phase in (("1NT", "nt"), ("1C", "strong_club" if self.precision else "minor")):
            self.announce("opening_" + call, all_of(no({"op": "has_contract_bid"}), eq(ctx("event.call"), call)),
                          ("artificial", "strong_club") if phase == "strong_club" else
                          ("natural", "weak_nt" if self.precision else "strong_nt") if call == "1NT" else ("natural",),
                          "NT" if call == "1NT" else None if phase == "strong_club" else "C")
        self.announce("natural_nonclub_opening",
                      all_of(no({"op": "has_contract_bid"}), op("in", ctx("event.call"), ["1D", "1H", "1S"])),
                      ("natural",), ctx("event.strain"))

    def fit_conventions(self):
        hcp = ref("hcp")
        for major in ("H", "S"):
            c = eq(var("open_suit"), major)
            other = "S" if major == "H" else "H"
            self.rule("fit.drury_" + major, all_of(c, ctx("passed_before_opening"),
                      within(hcp, 9, 11), support(major)), fixed("2C"), "major")
            self.transition("major", "drury_" + major, all_of(
                event("2C"), c, ctx("event.passed_before_opening"),
            ), "drury_answer", {"trump": major, "res_min": 9, "res_max": 11}, "one_round")
            mini = "3" + other
            self.rule("fit.mini_splinter_" + major, all_of(c, support(major, 4), within(hcp, 10, 12),
                any_of(*(le(length(suit), 1) for suit in SUITS if suit != major))), fixed(mini), "major")
            self.response("major", mini, "mini_ask", 10, 12, fit=True, only=c)
            for short in ("C", "D", other):
                call = "3NT" if short == other else "4" + short
                self.rule(f"fit.splinter_{major}_{short}", all_of(
                    c, support(major, 4), within(hcp, 13, 15), le(length(short), 1),
                ), fixed(call), "major")
                self.response("major", call, "natural", 13, 15, fit=True, only=c,
                              short_suit=short, forcing="game")
            self.transition("major", "simple_raise_" + major, all_of(c, event("2" + major)),
                            "simple_raise", {"trump": major, "res_min": 6, "res_max": 10})
            for call, low, high in (("3C", 7, 9), ("3D", 10, 12)):
                self.response("major", call, "bergen_answer", low, high, fit=True, only=c)
            for side in SUITS:
                if side != major:
                    self.rule(f"jacoby.long_side_{major}_{side}", all_of(c, ge(hcp, 16), ge(ref("controls"), 5),
                              ge(length(side), 5), eq(honors(side, "KQ"), 2)), fixed("4" + side), "jacoby")
            for short in SUITS:
                if short == major:
                    continue
                self.rule(f"jacoby.source_short_{major}_{short}", all_of(c, le(length(short), 1), legal("3" + short)),
                          fixed("3" + short), "jacoby")
                self.transition("jacoby", f"source_short_{major}_{short}", all_of(c, event("3" + short)),
                                "natural", {"short_suit": short})
            self.rule("jacoby.source_maximum_" + major, all_of(c, ge(hcp, 14 if self.precision else 16),
                      ge(length(major), 6) if self.precision else True), fixed("3" + major), "jacoby")
            self.rule("jacoby.source_middle_" + major, all_of(c, any_of(ge(hcp, 14), ge(ref("controls"), 4))), fixed("3NT"), "jacoby")
            self.rule("jacoby.source_minimum_" + major, c, fixed("4" + major), "jacoby")
            self.transition("jacoby", "source_maximum_" + major, all_of(c, event("3" + major)), "slam_cue", {
                "open_min": 14 if self.precision else 16,
                "slam_active": True, "slam_started": True,
                **({"captain_" + major + "_min": 6} if self.precision else {}),
            }, "slam_try")
            self.transition("jacoby", "source_middle_" + major, all_of(c, event("3NT")), "natural", {"open_min": var("open_min"), "open_max": 15})
            self.transition("jacoby", "source_minimum_" + major, all_of(c, event("4" + major)), "finished", forcing="none")
        self.rule("mini.ask_shortness", True, {"kind": "step", "after": ctx("last_bid"), "count": 1}, "mini_ask")
        self.transition("mini_ask", "asked", all_of(ours(), eq(ctx("event.kind"), "BID")), "mini_answer", {
            "anchor": ctx("event.call"),
        }, "one_round")
        from bridge_evolve.core import ALL_BID_CALLS
        from bridge_evolve.systems.language import parse_call
        for major, anchor in (("H", "3NT"), ("S", "3S")):
            sides = [suit for suit in ("C", "D", "H", "S") if suit != major]
            for step, short in enumerate(sides, 1):
                self.rule(f"mini.short_{major}_{short}", all_of(eq(var("trump"), major), le(length(short), 1)),
                          {"kind": "step", "after": var("anchor"), "count": step}, "mini_answer")
                call = str(ALL_BID_CALLS[parse_call(anchor).ordering_index() + step])
                self.transition("mini_answer", f"short_{major}_{short}", all_of(eq(var("trump"), major), event(call)),
                                "mini_choose", {"short_suit": short})
        self.rule("mini.already_game", all_of(eq(ctx("last_bid_strain"), var("trump")), ge(ctx("last_bid_level"), 4)),
                  fixed("P"), "mini_choose")
        self.rule("mini.choose_game", True,
                  cheapest({"op": "list", "item_type": "string", "args": [var("trump")]}, 4, 5), "mini_choose")
        self.transition("mini_choose", "finished", all_of(ours(), eq(ctx("event.kind"), "BID")), "finished", forcing="none")
        for major in ("H", "S"):
            c = eq(var("trump"), major)
            self.rule("simple_raise.game_" + major, all_of(c, ge(ref("total_min"), 25)), fixed("4" + major), "simple_raise")
            for side in SUITS:
                if side == major:
                    continue
                self.rule(f"help.ask_{major}_{side}", all_of(c, ge(ref("total_mid2"), 46),
                    within(length(side), 3, 4), le(honors(side, "AKQ"), 1), can_bid_suit(side, 2, 3)),
                    cheapest([side], 2, 3), "simple_raise")
                self.transition("simple_raise", f"help_{major}_{side}", all_of(c, ours(), eq(ctx("event.strain"), side)),
                                "help_answer", {"help_suit": side}, "one_round")
            self.rule("simple_raise.invite_" + major, all_of(c, ref("game_invite_values")), fixed("3" + major), "simple_raise")
            self.transition("simple_raise", "invite_" + major, all_of(c, event("3" + major)),
                            "natural", self.game_invitation_updates(), "invite_game")
            self.rule("help.accept_" + major, all_of(c, any_of(ge(hcp, 9), le(length(var("help_suit")), 1),
                      ge(honors(var("help_suit"), "AKQ"), 2))), fixed("4" + major), "help_answer")
            self.rule("help.decline_" + major, c, fixed("3" + major), "help_answer")
            self.rule("bergen.accept_" + major, all_of(c, ge(ref("total_mid2"), 50)), fixed("4" + major), "bergen_answer")
            self.rule("bergen.decline_" + major, c, fixed("3" + major), "bergen_answer")
            self.rule("drury.game_" + major, all_of(c, ge(ref("total_mid2"), 50)), fixed("4" + major), "drury_answer")
            self.rule("drury.light_" + major, all_of(c, le(hcp, 10)), fixed("2" + major), "drury_answer")
            self.rule("drury.finish_" + major, c, fixed("2" + major), "drury_finish")
        self.rule("simple_raise.stop", True, fixed("P"), "simple_raise")
        self.rule("drury.genuine_opening", True, fixed("2D"), "drury_answer")
        self.transition("drury_answer", "genuine", event("2D"), "drury_finish", {"open_min": 11}, "one_round")
        self.transition("drury_answer", "natural", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")
        self.transition("drury_finish", "natural", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")
        for phase in ("simple_raise", "help_answer", "bergen_answer"):
            self.transition(phase, "natural", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")

    def precision_conventions(self):
        if not self.precision:
            return
        hcp = ref("hcp")
        self.rule("precision.point_shape_question", all_of(eq(var("response_suit"), "NT"), ge(hcp, 18),
                  ref("nt_extended"), legal("2C")), fixed("2C"), "strong_positive")
        self.rule("precision.fourteen_relay", all_of(eq(var("res_min"), 14), ge(hcp, 18), legal("2NT")),
                  fixed("2NT"), "strong_positive")
        self.transition("strong_positive", "point_shape", all_of(event("2C"), eq(var("response_suit"), "NT")),
                        "precision_point_shape", forcing="one_round")
        self.transition("strong_positive", "fourteen_relay", all_of(event("2NT"), eq(var("res_min"), 14)),
                        "precision_point_shape", forcing="one_round")
        for major in ("H", "S"):
            self.rule("precision.low_four_" + major, all_of(within(hcp, 8, 10), ge(length(major), 4), legal("2" + major)),
                      fixed("2" + major), "precision_point_shape")
            self.transition("precision_point_shape", "low_" + major, event("2" + major), "natural", {
                "res_min": 8, "res_max": 10, "response_suit": major,
            })
        self.rule("precision.low_no_major", all_of(within(hcp, 8, 10), legal("2D")), fixed("2D"), "precision_point_shape")
        self.transition("precision_point_shape", "low_no_major", event("2D"), "natural", {"res_min": 8, "res_max": 10})
        self.rule("precision.middle_4333", all_of(within(hcp, 11, 13), eq({"op": "shape"}, [4, 3, 3, 3]), legal("2NT")),
                  fixed("2NT"), "precision_point_shape")
        self.transition("precision_point_shape", "middle_4333", event("2NT"), "precision_four_ask", {"res_min": 11, "res_max": 13})
        for call, pair in (("3C", ("C", "")), ("3D", ("D", "H")), ("3H", ("H", "S")), ("3S", ("S", "D"))):
            shape = all_of(ge(length(pair[0]), 4), any_of(*(ge(length(suit), 4) for suit in SUITS if suit != "C"))) if not pair[1] else all_of(
                ge(length(pair[0]), 4), ge(length(pair[1]), 4))
            self.rule("precision.shape_pair_" + call, all_of(ge(hcp, 11), shape), fixed(call), "precision_point_shape")
            self.transition("precision_point_shape", "shape_pair_" + call, event(call), "precision_shape_ask", {
                "two_suit_a": pair[0], "two_suit_b": pair[1], "res_min": 11, "res_max": 40,
            })
        self.rule("precision.high_4333", True, fixed("3NT"), "precision_point_shape")
        self.transition("precision_point_shape", "high_4333", event("3NT"), "precision_four_ask", {
            "res_min": 14, "res_max": 40,
        })
        self.rule("precision.ask_other_four", all_of(eq(var("two_suit_a"), "C"), legal("3D")), fixed("3D"), "precision_shape_ask")
        self.transition("precision_shape_ask", "other_four", event("3D"), "precision_shape_reply", forcing="one_round")
        for suit, call in (("H", "3H"), ("S", "3S"), ("D", "3NT")):
            self.rule("precision.other_four_" + suit, ge(length(suit), 4), fixed(call), "precision_shape_reply")
            self.transition("precision_shape_reply", "other_four_" + suit, event(call), "natural", {"two_suit_b": suit})
        self.rule("precision.ask_single_four_low", legal("3C"), fixed("3C"), "precision_four_ask")
        self.rule("precision.ask_single_four_high", True, fixed("4C"), "precision_four_ask")
        self.transition("precision_four_ask", "asked", all_of(ours(), eq(ctx("event.kind"), "BID")),
                        "precision_four_reply", forcing="one_round")
        for suit in ("D", "H", "S", "C"):
            self.rule("precision.single_four_" + suit, ge(length(suit), 4),
                      cheapest([suit if suit != "C" else "NT"], 3, 4), "precision_four_reply")
            self.transition("precision_four_reply", "single_four_" + suit,
                            all_of(ours(), eq(ctx("event.strain"), suit if suit != "C" else "NT")), "natural", {
                "response_suit": suit,
            })
        for major in ("H", "S"):
            self.rule("precision.club_weak_major_" + major, all_of(le(hcp, 11), ge(length(major), 5)), fixed("2" + major), "club_two")
            self.response("club_two", "2" + major, "natural", 0, 11, major)
        self.rule("precision.club_inquiry", any_of(ge(hcp, 11), all_of(ge(hcp, 9), support("C"))), fixed("2D"), "club_two")
        self.response("club_two", "2D", "precision_club_ask", 9, 40, forcing="one_round")
        for major in ("H", "S"):
            self.rule("precision.club_four_" + major, ge(length(major), 4), fixed("2" + major), "precision_club_ask")
            self.transition("precision_club_ask", "four_" + major, event("2" + major), "precision_club_finish", {
                "response_suit": major,
            })
        self.rule("precision.club_solid_game", all_of(ge(hcp, 14), ref("solid_C"), ref("all_stopped")), fixed("3NT"), "precision_club_ask")
        for short in ("H", "S"):
            self.rule("precision.club_short_" + short, all_of(ge(hcp, 14), le(length(short), 1)), fixed("3" + short), "precision_club_ask")
            self.transition("precision_club_ask", "short_" + short, event("3" + short), "natural", {
                "open_min": 14, "captain_C_min": 6, "short_suit": short,
                "captain_H_max": 1 if short == "H" else 3, "captain_S_max": 1 if short == "S" else 3,
            })
        self.rule("precision.club_diamond_shape", all_of(ge(hcp, 14), any_of(ge(length("D"), 4), le(length("D"), 1))),
                  fixed("3D"), "precision_club_ask")
        self.transition("precision_club_ask", "diamond_shape", event("3D"), "precision_club_relay", {"open_min": 14}, "one_round")
        self.rule("precision.club_balanced_max", ge(hcp, 14), fixed("2NT"), "precision_club_ask")
        self.transition("precision_club_ask", "balanced_max", event("2NT"), "natural", {"open_min": 14}, "game")
        self.rule("precision.club_minimum", True, fixed("3C"), "precision_club_ask")
        self.transition("precision_club_ask", "minimum", event("3C"), "natural", {"open_min": 11, "open_max": 13})
        self.rule("precision.club_diamond_relay", True, fixed("3H"), "precision_club_relay")
        self.transition("precision_club_relay", "asked", event("3H"), "precision_club_reveal", forcing="one_round")
        self.rule("precision.club_four_diamonds", ge(length("D"), 4), fixed("3S"), "precision_club_reveal")
        self.rule("precision.club_short_diamonds", True, fixed("3NT"), "precision_club_reveal")
        self.transition("precision_club_reveal", "answered", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")
        for major in ("H", "S"):
            fit = all_of(eq(var("response_suit"), major), support(major, 4))
            self.rule("precision.club_fit_game_" + major, all_of(fit, ge(hcp, 12)), fixed("4" + major), "precision_club_finish")
            self.rule("precision.club_fit_invite_" + major, all_of(fit, ge(hcp, 10)), fixed("3" + major), "precision_club_finish")
        self.rule("precision.club_game_no_fit", all_of(ge(hcp, 12), ge(ref("total_min"), 25)), fixed("3NT"), "precision_club_finish")
        self.rule("precision.club_support", support("C"), fixed("3C"), "precision_club_finish")
        self.rule("precision.club_no_fit_invite", ge(ref("total_max"), 25), fixed("2NT"), "precision_club_finish")
        self.rule("precision.club_no_game", True, fixed("P"), "precision_club_finish")
        self.transition("precision_club_finish", "natural", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")
        self.rule("precision.three_suit_inquiry", ge(hcp, 13), fixed("3D"), "three_suit")
        self.response("three_suit", "3D", "precision_three_ask", 13, 40, forcing="game")
        self.rule("precision.three_suit_void_max", all_of(eq(length("D"), 0), ge(hcp, 14)), fixed("4D"), "precision_three_ask")
        self.rule("precision.three_suit_void_min", eq(length("D"), 0), fixed("4C"), "precision_three_ask")
        self.rule("precision.three_suit_single_honor", all_of(ge(hcp, 14), ge(honors("D", "AKQJ"), 1)), fixed("3NT"), "precision_three_ask")
        self.rule("precision.three_suit_single_max", ge(hcp, 14), fixed("3S"), "precision_three_ask")
        self.rule("precision.three_suit_single_min", True, fixed("3H"), "precision_three_ask")
        self.transition("precision_three_ask", "answered", all_of(ours(), eq(ctx("event.kind"), "BID")), "precision_three_finish")
        for major, call in (("H", "4D"), ("S", "4H")):
            self.rule("precision.three_suit_transfer_" + major, all_of(ge(length(major), 4), legal(call)), fixed(call), "precision_three_finish")
            self.transition("precision_three_finish", "transfer_" + major, event(call), "precision_three_transfer", {"trump": major})
        self.rule("precision.three_suit_club_game", True, fixed("5C"), "precision_three_finish")
        self.transition("precision_three_finish", "club_game", event("5C"), "finished", forcing="none")
        self.rule("precision.three_suit_accept", True, bid(4, var("trump")), "precision_three_transfer")
        self.transition("precision_three_transfer", "finished", all_of(ours(), eq(ctx("event.kind"), "BID")), "finished", forcing="none")

    def responses(self):
        hcp = ref("hcp")
        # Major-suit support, Jacoby, Bergen and passed-hand Drury.
        for suit in ("H", "S"):
            condition = eq(var("open_suit"), suit)
            other = "S" if suit == "H" else "H"
            self.rule("major.drury_" + suit, all_of(condition, ctx("passed_before_opening"),
                within(hcp, 9, 11), support(suit)), fixed("2C"), "major")
            self.rule("major.jacoby_" + suit, all_of(condition, ge(hcp, 13), support(suit, 4)), fixed("2NT"), "major")
            self.rule("major.bergen_limit_" + suit, all_of(condition, within(hcp, 10, 12), support(suit, 4)), fixed("3D"), "major")
            self.rule("major.bergen_constructive_" + suit, all_of(condition, within(hcp, 7, 9), support(suit, 4)), fixed("3C"), "major")
            self.rule("major.preemptive_game_" + suit, all_of(condition, support(suit, 5), within(hcp, 0, 6)), bid(4, suit), "major")
            self.rule("major.simple_raise_" + suit, all_of(condition, support(suit), within(hcp, 6, 10)), bid(2, suit), "major")
            self.rule("major.two_over_one_other_" + suit, all_of(condition, eq(suit, "S"), ge(hcp, 13), ge(length(other), 5)), fixed("2H"), "major")
            self.rule("major.two_over_one_diamonds_" + suit, all_of(condition, ge(hcp, 13), ge(length("D"), 4)), fixed("2D"), "major")
            self.rule("major.two_over_one_clubs_" + suit, all_of(condition, ge(hcp, 13), ge(length("C"), 3)), fixed("2C"), "major")
            self.rule("major.one_spade", all_of(condition, eq(suit, "H"), ge(hcp, 6), ge(length("S"), 4)), fixed("1S"), "major")
            self.rule("major.semi_forcing_nt_" + suit, all_of(condition, ge(hcp, 6)), fixed("1NT"), "major")
        self.response("major", "2NT", "jacoby", 13, 40, fit=True, forcing="game")
        self.response("major", "3D", "jacoby", 10, 12, fit=True)
        self.response("major", "3C", "jacoby", 7, 9, fit=True)
        self.response("major", "1NT", "opener_rebid", 6, 12)
        self.response("major", "1S", "opener_rebid", 6, 40, "S", forcing="one_round")
        self.transition("major", "two_over_one_hearts",
                        all_of(event("2H"), eq(var("open_suit"), "S")), "opener_rebid",
                        {"res_min": 13, "res_max": 40, "response_suit": "H"}, "game")
        for suit in ("H", "S"):
            self.response("major", "2" + suit, "natural", 6, 10, suit, fit=True)
            self.response("major", "4" + suit, "finished", 0, 6, suit, fit=True)
        self.transition("major", "drury", all_of(event("2C"), ctx("event.passed_before_opening")),
                        "natural", {"trump": var("open_suit"), "res_min": 9, "res_max": 11})
        for suit in ("C", "D"):
            self.response("major", "2" + suit, "opener_rebid", 13, 40, suit, forcing="game")

        for opening in ("C", "D"):
            c = eq(var("open_suit"), opening)
            min_major = 8 if self.precision else 5
            # Walsh: for non-game values major first; 4-4 majors hearts, 5-5 spades.
            self.rule("minor.four_four_hearts_" + opening,
                      all_of(c, ge(hcp, min_major), eq(length("H"), 4), eq(length("S"), 4)), fixed("1H"), "minor")
            for suit in ("S", "H"):
                self.rule("minor.major_" + opening + suit,
                          all_of(c, ge(hcp, min_major), longest(suit, ("S", "H"), minimum=4)), fixed("1" + suit), "minor")
            self.rule("minor.inverted_game_" + opening, all_of(c, ge(hcp, 13 if not self.precision else 14), support(opening, 4)), bid(2, opening), "minor")
            self.rule("minor.inverted_limit_" + opening, all_of(c, ge(hcp, 10 if not self.precision else 11), support(opening, 5)), bid(2, opening), "minor")
            if opening == "C":
                self.rule("minor.walsh_diamond", all_of(c, ge(hcp, 5), ge(length("D"), 4)), fixed("1D"), "minor")
            else:
                self.rule("minor.two_over_one_clubs", all_of(c, ge(hcp, 14 if self.precision else 13),
                                                            ge(length("C"), 5 if self.precision else 4)), fixed("2C"), "minor")
            self.rule("minor.nt_game_" + opening, all_of(c, ge(hcp, 14 if self.precision else 13)), fixed("3NT"), "minor")
            self.rule("minor.nt_invite_" + opening, all_of(c, ge(hcp, 12 if self.precision else 11)), fixed("2NT"), "minor")
            self.rule("minor.nt_part_" + opening, all_of(c, ge(hcp, 8 if self.precision else 6)), fixed("1NT"), "minor")
            for suit in ("S", "H", "D"):
                self.rule("minor.weak_jump_" + opening + suit, all_of(c, within(hcp, 4, 6),
                    longest(suit, minimum=6), legal("2" + suit), no(eq(suit, opening))), fixed("2" + suit), "minor")
            self.rule("minor.preemptive_raise_" + opening, all_of(c, support(opening, 5)), bid(3, opening), "minor")
        for suit in ("H", "S"):
            self.response("minor", "1" + suit, "opener_rebid", 8 if self.precision else 5, 40, suit, forcing="one_round")
        self.response("minor", "1D", "opener_rebid", 5, 40, "D", forcing="one_round")
        for suit in ("C", "D"):
            self.transition("minor", "inverted_" + suit, all_of(event("2" + suit), eq(var("open_suit"), suit)),
                            "inverted", {"trump": suit, "res_min": 11 if self.precision else 10, "res_max": 40}, "one_round")
        self.response("minor", "2C", "opener_rebid", 14 if self.precision else 13, 40, "C", forcing="game")
        for call, low, high in (("1NT", 8 if self.precision else 6, 11 if self.precision else 10),
                                ("2NT", 12 if self.precision else 11, 13 if self.precision else 12),
                                ("3NT", 14 if self.precision else 13, 40)):
            self.response("minor", call, "natural", low, high, "NT")
        for opening in ("C", "D"):
            self.response("minor", "3" + opening, "natural", 0, 10 if self.precision else 9,
                          fit=True, only=eq(var("open_suit"), opening))
            for suit in ("S", "H", "D"):
                if suit != opening:
                    self.response("minor", "2" + suit, "natural", 4, 6, suit,
                                  only=eq(var("open_suit"), opening), **{"responder_" + suit + "_min": 6})

        # Strong-club responses: shape, major/minor positives, balanced ranges.
        for short, call in (("S", "2H"), ("C", "2NT"), ("D", "3C"), ("H", "3D")):
            self.rule("strong_club.4441_" + short, all_of(ge(hcp, 8), eq(length(short), 1),
                *(eq(length(other), 4) for other in SUITS if other != short)), fixed(call), "strong_club")
            self.response("strong_club", call, "shape_ask", 8, 40, short_suit=short, forcing="game")
        for suit in SUITS:
            self.rule("strong_club.positive_" + suit, all_of(ge(hcp, 8), longest(suit, minimum=5)),
                      fixed(("1" if suit in ("H", "S") else "2") + suit), "strong_club")
            self.response("strong_club", ("1" if suit in ("H", "S") else "2") + suit,
                          "strong_positive", 8, 40, suit, forcing="game")
        self.rule("strong_club.balanced_14_15", all_of(within(hcp, 14, 15), ref("nt_basic")), fixed("2S"), "strong_club")
        self.response("strong_club", "2S", "strong_positive", 14, 15, "NT", forcing="game")
        self.rule("strong_club.balanced_positive", ge(hcp, 8), fixed("1NT"), "strong_club")
        self.response("strong_club", "1NT", "strong_positive", 8, 40, "NT", forcing="game")
        self.rule("strong_club.negative", le(hcp, 7), fixed("1D"), "strong_club")
        self.response("strong_club", "1D", "strong_negative", 0, 7)

        for suit in SUITS:
            call = ("2" if suit in ("H", "S") else "3") + suit
            self.rule("strong_two.positive_" + suit,
                      all_of(ge(hcp, 8), longest(suit, minimum=5),
                             any_of(ge(hcp, 10), ref(f"good_{suit}"), ge(length(suit), 6))),
                      fixed(call), "strong_two")
            self.response("strong_two", call, "strong_positive", 8, 40, suit, forcing="game")
        self.rule("strong_two.balanced_positive", all_of(ge(hcp, 8), ref("nt_basic")), fixed("2NT"), "strong_two")
        self.response("strong_two", "2NT", "strong_positive", 8, 40, "NT", forcing="game")
        self.rule("strong_two.negative_or_waiting", True, fixed("2D"), "strong_two")
        self.response("strong_two", "2D", "strong_negative", 0, 40, forcing="one_round")
        self.rule("weak_two.ogust", ge(hcp, 14), fixed("2NT"), "weak_two")
        self.response("weak_two", "2NT", "weak_ask", 14, 40, forcing="one_round")
        self.rule("club_two.inquiry", ge(hcp, 10), fixed("2D"), "club_two")
        self.response("club_two", "2D", "opener_rebid", 10, 40, forcing="one_round")
        self.rule("three_suit.inquiry", ge(hcp, 13), fixed("3D"), "three_suit")
        self.response("three_suit", "3D", "opener_rebid", 13, 40, forcing="game")
        for phase in ("preempt", "weak_two", "club_two", "three_suit"):
            for suit in ("H", "S"):
                self.rule(phase + ".major_game_" + suit, all_of(ge(hcp, 14), longest(suit, minimum=7), legal("4" + suit)),
                          fixed("4" + suit), phase)
            self.rule(phase + ".nt_game", all_of(ge(ref("total_min"), 25), ref("enemy_stopped"), legal("3NT")),
                      fixed("3NT"), phase)
        self.rule("gambling.pass", ref("all_stopped"), fixed("P"), "gambling")
        for suit in ("H", "S"):
            self.rule("gambling.major_game_" + suit,
                      all_of(ge(length(suit), 7), ge(hcp, 10), legal("4" + suit)), fixed("4" + suit), "gambling")
        self.rule("gambling.pass_or_correct", legal("4C"), fixed("4C"), "gambling")
        self.response("gambling", "4C", "gambling_correct", 0, 40, "C")
        self.rule("gambling.correct_clubs", ge(length("C"), 7), fixed("P"), "gambling_correct")
        self.rule("gambling.correct_diamonds", all_of(ge(length("D"), 7), legal("4D")),
                  fixed("4D"), "gambling_correct")
        self.transition("gambling_correct", "corrected", event("4D"), "finished")

    def nt_extensions(self):
        hcp = ref("hcp")
        strong = no(all_of(self.precision, eq(var("nt_style"), "weak")))
        for short in ("H", "S"):
            self.rule("nt.minor_shortness_" + short, all_of(strong, eq(var("nt_level"), 1), ge(hcp, 10),
                eq(length(short), 1), ge(length("C"), 4), ge(length("D"), 4), ge(add(length("C"), length("D")), 9)),
                fixed("3" + short), "nt")
            self.response("nt", "3" + short, "nt_minor_shape", 10, 40, only=all_of(strong, eq(var("nt_level"), 1)), short_suit=short, forcing="game")
        for minor in ("C", "D"):
            self.rule("nt.minor_invitation_" + minor, all_of(strong, eq(var("nt_level"), 1), within(hcp, 6, 9),
                      ge(length(minor), 6), ref("good_" + minor), le(length("H"), 3), le(length("S"), 3)),
                      fixed("3" + minor), "nt")
            self.response("nt", "3" + minor, "natural", 6, 9, minor, only=all_of(strong, eq(var("nt_level"), 1)))
        self.rule("two_nt.minor_stayman", all_of(eq(var("nt_level"), 2), ge(hcp, 5),
                  ge(length("C"), 4), ge(length("D"), 4), ge(add(length("C"), length("D")), 9)), fixed("3S"), "nt")
        self.response("nt", "3S", "minor_stayman", 5, 40, only=eq(var("nt_level"), 2), forcing="one_round")
        for minor in ("C", "D"):
            self.rule("minor_stayman.show_" + minor, ge(length(minor), 4), fixed("4" + minor), "minor_stayman")
            self.transition("minor_stayman", "show_" + minor, event("4" + minor), "minor_stayman_finish", {
                "trump": minor,
            })
            self.rule("nt_minor_shape.fit_" + minor, ge(length(minor), 4), fixed("4" + minor), "nt_minor_shape")
            self.transition("nt_minor_shape", "fit_" + minor, event("4" + minor), "natural", {"trump": minor})
        self.rule("minor_stayman.deny", True, fixed("3NT"), "minor_stayman")
        self.transition("minor_stayman", "deny", event("3NT"), "finished", forcing="none")
        self.rule("minor_stayman.game", ge(ref("total_min"), 29), bid(5, var("trump")), "minor_stayman_finish")
        self.rule("minor_stayman.stop", True, fixed("P"), "minor_stayman_finish")
        self.transition("minor_stayman_finish", "game", all_of(ours(), eq(ctx("event.kind"), "BID")), "finished", forcing="none")
        self.rule("nt_minor_shape.no_fit", True, fixed("3NT"), "nt_minor_shape")
        self.transition("nt_minor_shape", "no_fit", event("3NT"), "finished", forcing="none")
        for major, other in (("H", "S"), ("S", "H")):
            self.rule("smolen.four_" + major, all_of(strong, eq(var("response_suit"), "D"), ge(hcp, 10),
                      eq(length(major), 4), ge(length(other), 5)), fixed("3" + major), "stayman_finish")
            self.transition("stayman_finish", "smolen_" + major, all_of(event("3" + major), eq(var("response_suit"), "D")),
                            "smolen_reply", {"transfer": other}, "game")
            self.rule("smolen.accept_" + major, all_of(eq(var("transfer"), major), support(major)), fixed("4" + major), "smolen_reply")
            self.rule("transfer.superaccept_" + major, all_of(eq(var("transfer"), major), support(major, 4),
                      ge(hcp, var("open_max")), legal("3" + major)), fixed("3" + major), "transfer")
            self.rule("transfer.correct_nt_game_" + major, all_of(eq(var("transfer"), major), support(major),
                      no(eq({"op": "shape"}, [4, 3, 3, 3]))), fixed("4" + major), "transfer_nt_choice")
        self.rule("smolen.no_fit", True, fixed("3NT"), "smolen_reply")
        self.transition("smolen_reply", "finished", all_of(ours(), eq(ctx("event.kind"), "BID")), "finished", forcing="none")
        self.transition("transfer_finish", "offer_nt_game", event("3NT"), "transfer_nt_choice", forcing="none")
        self.rule("transfer.keep_nt_game", True, fixed("P"), "transfer_nt_choice")
        self.transition("transfer_nt_choice", "major_game", all_of(ours(), eq(ctx("event.kind"), "BID")), "finished", forcing="none")

    def notrump(self):
        hcp = ref("hcp")
        level = var("nt_level")
        weak = all_of(self.precision, eq(var("nt_style"), "weak"))
        threshold = choose(eq(level, 2), 5, choose(weak, 12, 10))
        declared_game_min = op("min", threshold, op("max", 0, op("sub", 25, var("captain_hcp_min"))))
        declared_invite_min = op("max", 6, op("sub", 25, var("captain_hcp_max")))
        invite = op("min", choose(weak, 10, 8), declared_invite_min)
        # Both 1NT and 2NT use their documented but different response structures.
        for suit, call in (("H", "4D"), ("S", "4H")):
            self.rule("nt.texas_" + suit, all_of(no(weak), eq(level, 1), ge(hcp, 10), longest(suit, ("S", "H"), minimum=6)),
                      fixed(call), "nt")
            self.response("nt", call, "transfer", 10, 40, suit, transfer=suit, only=no(weak))
        for suit, call in (("H", "2D"), ("S", "2H")):
            self.rule("nt.transfer_" + suit, all_of(no(weak), eq(level, 1), longest(suit, ("S", "H"), minimum=5)), fixed(call), "nt")
            self.response("nt", call, "transfer", 0, 40, suit, transfer=suit, only=no(weak))
        if self.precision:
            for suit in ("S", "H"):
                self.rule("nt.weak_major_" + suit, all_of(weak, eq(level, 1), le(hcp, 9), longest(suit, ("S", "H"), minimum=5)),
                          fixed("2" + suit), "nt")
                self.response("nt", "2" + suit, "finished", 0, 9, suit, only=weak)
        for suit, call in (("H", "3D"), ("S", "3H")):
            self.rule("two_nt.transfer_" + suit, all_of(eq(level, 2), ge(hcp, 4), longest(suit, ("S", "H"), minimum=5)), fixed(call), "nt")
            self.response("nt", call, "transfer", 4, 40, suit, transfer=suit)
        self.rule("nt.gerber", all_of(ref("nt_slam_interest"), ge(ref("total_min"), 33), eq(honors("C", "A"), 0),
                                     ref("nt_basic"), legal("4C")), fixed("4C"), "nt")
        self.response("nt", "4C", "gerber", op("max", 0, op("sub", 33, var("captain_hcp_max"))), 40, forcing="one_round",
                      nt_slam_invited=True)
        self.rule("nt.weak_invitation_stayman", all_of(weak, eq(level, 1), within(hcp, invite, op("sub", threshold, 1))),
              fixed("2C"), "nt")
        self.rule("nt.stayman", all_of(ge(hcp, choose(eq(level, 2), 5, invite)),
                                    any_of(ge(length("H"), 4), ge(length("S"), 4))),
                  bid(choose(eq(level, 2), 3, 2), choose(all_of(eq(level, 1), weak, ge(hcp, 12)), "D", "C")), "nt")
        self.response("nt", "2C", "stayman", 0, 40, forcing="one_round")
        if self.precision:
            self.response("nt", "2D", "stayman", 12, 40, forcing="game", invite_target="GF", only=weak)
        self.response("nt", "3C", "puppet", 5, 40, forcing="game")
        self.rule("nt.minor_transfer_strong", all_of(no(weak), eq(level, 1),
                  any_of(ge(length("C"), 6), ge(length("D"), 6)), le(hcp, 7)), fixed("2S"), "nt")
        self.response("nt", "2S", "minor_transfer", 0, 7, "C", only=no(weak))
        if self.precision:
            self.rule("nt.minor_transfer", all_of(weak, eq(level, 1), le(hcp, 9), any_of(ge(length("C"), 6), ge(length("D"), 6))),
                      fixed("2NT"), "nt")
            self.response("nt", "2NT", "minor_transfer", 0, 40, "C", only=weak)
        self.rule("nt.quantitative", all_of(ref("nt_basic"), ref("nt_slam_interest"), legal("4NT")), fixed("4NT"), "nt")
        self.response("nt", "4NT", "quantitative", op("max", 0, op("sub", 33, var("captain_hcp_max"))), 40,
                      nt_slam_invited=True)
        self.rule("nt.direct_game", ge(hcp, threshold), fixed("3NT"), "nt")
        self.rule("nt.direct_invite", all_of(no(weak), eq(level, 1), ge(hcp, invite)), fixed("2NT"), "nt")
        if not self.precision:
            self.response("nt", "2NT", "quantitative", declared_invite_min, 9, invite_target="game")
        self.response("nt", "3NT", "finished", declared_game_min, 30)
        self.response("nt", "2NT", "quantitative", declared_invite_min, 9, invite_target="game", only=no(weak))
        for phase in ("stayman", "puppet"):
            for suit in ("H", "S"):
                self.rule(phase + ".show_" + suit, ge(length(suit), 5 if phase == "puppet" else 4),
                          bid(choose(eq(level, 2), 3, 2), suit), phase)
            if phase == "puppet":
                self.rule("puppet.four_card_major", any_of(ge(length("H"), 4), ge(length("S"), 4)),
                          fixed("3D"), phase)
                self.transition("puppet", "four_card_major", event("3D"), "puppet_finish")
            denial = choose(all_of(weak, eq(var("invite_target"), "GF")), "NT", "D") if phase == "stayman" else "NT"
            self.rule(phase + ".deny", True, bid(choose(eq(level, 2), 3, 2), denial), phase)
            for suit in ("D", "H", "S", "NT"):
                for bid_level in (2, 3):
                    self.transition(phase, f"answer_{bid_level}_{suit}", event(str(bid_level) + suit),
                                    "stayman_finish", {"response_suit": suit})
        self.rule("transfer.accept", True, cheapest({"op": "list", "item_type": "string", "args": [var("transfer")]},
                                                   choose(ge(ctx("last_bid_level"), 4), 4, choose(eq(level, 2), 3, 2))), "transfer")
        for suit in ("H", "S"):
            for response_level in (2, 3, 4):
                self.transition("transfer", f"accepted_{response_level}_{suit}", event(str(response_level) + suit),
                                "transfer_finish", {"response_suit": suit})
        for phase in ("transfer_finish", "stayman_finish"):
            for suit in ("H", "S"):
                fit = all_of(eq(var("response_suit"), suit), ref("fit_" + suit))
                self.rule(phase + ".game_fit_" + suit, all_of(fit, ge(hcp, threshold), legal("4" + suit)), fixed("4" + suit), phase)
                self.rule(phase + ".invite_fit_" + suit, all_of(fit, ge(hcp, invite), legal("3" + suit)), fixed("3" + suit), phase)
            self.rule(phase + ".game_nt", all_of(ge(hcp, threshold), legal("3NT")), fixed("3NT"), phase)
            self.rule(phase + ".invite_nt", all_of(eq(level, 1), ge(hcp, invite), legal("2NT")), fixed("2NT"), phase)
            for suit in ("H", "S"):
                self.response(phase, "3" + suit, "natural", invite, choose(weak, 11, 9), suit, fit=suit)
                self.response(phase, "4" + suit, "finished", declared_game_min, 30, suit, fit=suit)
            self.response(phase, "2NT", "quantitative", invite, choose(weak, 11, 9), invite_target="game")
            self.response(phase, "3NT", "natural", declared_game_min, 30, "NT")
        self.rule("puppet.choose_both", all_of(ge(length("H"), 4), ge(length("S"), 4)),
                  fixed("4D"), "puppet_finish")
        self.rule("puppet.choose_hearts", ge(length("H"), 4), fixed("3S"), "puppet_finish")
        self.rule("puppet.choose_spades", ge(length("S"), 4), fixed("3H"), "puppet_finish")
        self.rule("puppet.no_major", True, fixed("3NT"), "puppet_finish")
        for call, requested in (("3S", "H"), ("3H", "S"), ("4D", "")):
            self.transition("puppet_finish", "choose_" + call, event(call), "puppet_reveal", {"transfer": requested})
        self.transition("puppet_finish", "stop", event("3NT"), "finished")
        for suit in ("H", "S"):
            self.rule("puppet.reveal_" + suit,
                      all_of(any_of(eq(var("transfer"), suit), eq(var("transfer"), "")), ge(length(suit), 4)),
                      fixed("4" + suit), "puppet_reveal")
        self.rule("puppet.deny_requested_major", legal("3NT"), fixed("3NT"), "puppet_reveal")
        self.transition("puppet_reveal", "finish", all_of(ours(), eq(ctx("event.kind"), "BID")), "finished")
        self.rule("minor_transfer.accept", True, fixed("3C"), "minor_transfer")
        self.response("minor_transfer", "3C", "minor_finish", 0, 40, "C")
        self.rule("minor_finish.correct_diamonds", all_of(op("gt", length("D"), length("C")), legal("3D")), fixed("3D"), "minor_finish")
        self.response("minor_finish", "3D", "finished", 0, 9, "D")
        self.rule("quantitative.grand", all_of(ge(ref("total_min"), 37), legal("7NT")), fixed("7NT"), "quantitative")
        self.rule("quantitative.small", all_of(ge(ref("total_min"), 33), legal("6NT")), fixed("6NT"), "quantitative")
        self.rule("quantitative.game", all_of(ge(ref("total_min"), 25), legal("3NT")), fixed("3NT"), "quantitative")

    def suit_rebids(self):
        hcp = ref("hcp")
        one_over_one = eq(ctx("last_bid_level"), 1)
        if self.precision:
            self.transition("opener_rebid", "xyz", all_of(
                event("1S"), eq(var("open_suit"), "D"), eq(var("response_suit"), "H"),
            ), "checkback", {"open_min": 11, "open_max": 15, "rebid_suit": "S"})
        for major in ("H", "S"):
            self.transition("opener_rebid", "raised_major_" + major, all_of(
                event("2" + major), eq(var("response_suit"), major), one_over_one,
            ), "raised_major", {"trump": major, "open_min": var("open_min"), "open_max": 15})
            self.rule("raise_inquiry.game_" + major, all_of(
                eq(var("trump"), major), ge(ref("total_min"), 25), legal("4" + major),
            ), fixed("4" + major), "raised_major")
            self.transition("raised_major", "game_" + major, event("4" + major), "finished", forcing="none")
        self.rule("raise_inquiry.ask", all_of(ref("game_invite_values"), legal("2NT")), fixed("2NT"), "raised_major")
        self.transition("raised_major", "asked", event("2NT"), "raise_answer", {
            "res_max": 40, **self.game_invitation_updates(),
        }, "one_round")
        self.rule("raise_inquiry.stop", True, fixed("P"), "raised_major")
        for suit in SUITS:
            self.rule("raise_inquiry.maximum_short_" + suit, all_of(
                ge(hcp, 14), no(eq(var("trump"), suit)), le(length(suit), 1), legal("4" + suit),
            ), fixed("4" + suit), "raise_answer")
            self.transition("raise_answer", "maximum_short_" + suit, all_of(
                event("4" + suit), no(eq(var("trump"), suit)),
            ), "natural", {"open_min": 14, "short_suit": suit})
        self.rule("raise_inquiry.maximum", ge(hcp, 14), bid(4, var("trump")), "raise_answer")
        self.transition("raise_answer", "maximum", all_of(ours(), eq(ctx("event.level"), 4)), "finished", forcing="none")
        for suit in ("C", "D"):
            self.rule("raise_inquiry.minimum_long_" + suit, all_of(
                eq(var("open_suit"), suit), ge(length(suit), 5), legal("3" + suit),
            ), fixed("3" + suit), "raise_answer")
        self.rule("raise_inquiry.minimum", True, bid(3, var("trump")), "raise_answer")
        self.transition("raise_answer", "minimum", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural", {
            "open_max": 13,
        })
        order = ("C", "D", "H", "S")
        for opening in order:
            for response in order:
                if response == opening:
                    continue
                for rebid in order:
                    if rebid == response:
                        continue
                    condition = all_of(eq(var("open_suit"), opening), eq(var("response_suit"), response))
                    reverse = rebid != opening and order.index(rebid) > order.index(opening)
                    self.transition("opener_rebid", f"suit_{opening}_{response}_{rebid}", all_of(
                        condition, event("2" + rebid), one_over_one,
                    ), "reverse_reply" if reverse else "responder_rebid", {
                        "rebid_suit": rebid,
                        **({"open_min": 14 if self.precision else 16} if reverse else {}),
                    }, "one_round" if reverse else None)
                    if rebid == opening:
                        artificial = "D" if opening == "C" else "S" if response == "H" else "H"
                    else:
                        artificial = next(suit for suit in order if suit not in (opening, response, rebid))
                    self.rule(f"fourth.ask_{opening}_{response}_{rebid}", all_of(
                        condition, eq(var("rebid_suit"), rebid), ge(ref("total_min"), 25),
                    ), cheapest([artificial], 1, 3), "responder_rebid")
                    self.transition("responder_rebid", f"fourth_{opening}_{response}_{rebid}", all_of(
                        condition, eq(var("rebid_suit"), rebid), ours(), eq(ctx("event.strain"), artificial),
                    ), "fourth_answer", {"fourth_suit": artificial, "res_min": op("sub", 25, var("open_min"))}, "game")
                    if reverse:
                        weak = le(hcp, 7 if not self.precision else 9)
                        self.rule(f"reverse.wait_{opening}_{response}_{rebid}", all_of(
                            condition, eq(var("rebid_suit"), rebid), weak, le(length(response), 5),
                        ), cheapest([artificial], 2, 2) if order.index(artificial) > order.index(rebid) else fixed("2NT"), "reverse_reply")
        self.transition("opener_rebid", "one_level_second_suit", all_of(
            ours(), eq(ctx("event.level"), 1), no(eq(ctx("event.strain"), "NT")),
        ), "responder_rebid", {"rebid_suit": ctx("event.strain")})
        self.transition("reverse_reply", "weak_relay", all_of(
            ours(), eq(ctx("event.kind"), "BID"), le(ctx("event.level"), 2),
            no(eq(ctx("event.strain"), var("response_suit"))),
        ), "reverse_wait", forcing="one_round")
        for phase in ("responder_rebid", "reverse_reply"):
            for major in ("H", "S"):
                self.rule(phase + ".invite_long_" + major, all_of(
                    eq(var("response_suit"), major), ge(length(major), 6), ref("game_invite_values"), legal("3" + major),
                ), fixed("3" + major), phase)
                self.rule(phase + ".weak_long_" + major, all_of(
                    eq(var("response_suit"), major), ge(length(major), 6), legal("2" + major),
                ), fixed("2" + major), phase)
                self.transition(phase, "weak_long_" + major, all_of(
                    event("2" + major), eq(var("response_suit"), major),
                ), "finished", forcing="none")
            self.rule(phase + ".nt_game", all_of(ge(ref("total_min"), 25), legal("3NT")), fixed("3NT"), phase)
            self.rule(phase + ".nt_invite", all_of(ref("game_invite_values"), legal("2NT")), fixed("2NT"), phase)
            self.transition(phase, "game_nt_invitation", event("2NT"), "quantitative",
                            {"invite_target": "game", **self.game_invitation_updates()}, "invite_game")
            for major in ("H", "S"):
                self.transition(phase, "game_long_invitation_" + major, all_of(event("3" + major),
                    eq(var("response_suit"), major)), "natural", self.game_invitation_updates(), "invite_game")
            for suit in ("C", "D"):
                self.rule(phase + ".weak_preference_" + suit, all_of(
                    eq(var("open_suit"), suit), legal("2" + suit),
                ), fixed("2" + suit), phase)
            self.rule(phase + ".stop", no(ctx("must_bid")), fixed("P"), phase)
            self.transition(phase, "natural", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")
        for major in ("H", "S"):
            self.rule("reverse.support_" + major, all_of(eq(var("response_suit"), major), support(major)),
                      fixed("3" + major), "reverse_wait")
        self.rule("reverse.relay_original", True,
                  cheapest({"op": "list", "item_type": "string", "args": [var("open_suit")]}, 3, 4), "reverse_wait")
        self.transition("reverse_wait", "answered", all_of(ours(), eq(ctx("event.kind"), "BID")), "reverse_choose")
        self.rule("reverse.strong_finish", all_of(ge(hcp, 8 if not self.precision else 10), legal("3NT")), fixed("3NT"), "reverse_choose")
        self.rule("reverse.weak_stop", True, fixed("P"), "reverse_choose")
        self.transition("reverse_choose", "finished", event("3NT"), "finished", forcing="none")
        for major in ("H", "S"):
            self.rule("fourth.support_" + major, all_of(eq(var("response_suit"), major), support(major)),
                      cheapest([major], 2, 4), "fourth_answer")
        for suit in SUITS:
            self.rule("fourth.stopped_" + suit, all_of(eq(var("fourth_suit"), suit), ref("stop_" + suit), legal("2NT")),
                      fixed("2NT"), "fourth_answer")
        self.rule("fourth.repeat", True,
                  cheapest({"op": "list", "item_type": "string", "args": [var("open_suit")]}, 3, 5), "fourth_answer")
        self.transition("fourth_answer", "answered", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")

    def checkback(self):
        hcp = ref("hcp")
        invitation = op("sub", 23, var("open_min"))
        game = op("sub", 25, var("open_min"))
        self.transition("opener_rebid", "two_way_checkback", event("1NT"), "checkback", {
            "open_min": var("captain_hcp_min"),
            "open_max": 12 if self.precision else 14,
        })
        self.rule("checkback.weak_clubs", any_of(all_of(op("lt", hcp, invitation), ge(length("C"), 6)),
              all_of(within(hcp, invitation, op("sub", game, 1)), ge(length("C"), 5), ge(length("D"), 5))),
                  fixed("2NT"), "checkback")
        self.response("checkback", "2NT", "checkback_clubs", 0, 40, "C", forcing="one_round")
        self.rule("checkback.club_relay", True, fixed("3C"), "checkback_clubs")
        self.transition("checkback_clubs", "accepted", event("3C"), "checkback_club_finish")
        self.rule("checkback.both_minors_invite", all_of(ge(hcp, invitation), ge(length("D"), 5), ge(length("C"), 5)),
                  fixed("3D"), "checkback_club_finish")
        self.transition("checkback_club_finish", "both_minors_invite", event("3D"), "natural", {
            "res_min": invitation, "res_max": op("sub", game, 1), "response_suit": "D",
        })
        self.rule("checkback.club_stop", True, fixed("P"), "checkback_club_finish")
        self.rule("checkback.invite_or_weak_diamonds", any_of(
            within(hcp, invitation, op("sub", game, 1)),
            all_of(op("lt", hcp, invitation), ge(length("D"), 5)),
        ), fixed("2C"), "checkback")
        self.transition("checkback", "diamond_relay", event("2C"), "checkback_relay", forcing="one_round")
        self.rule("checkback.diamond_relay", True, fixed("2D"), "checkback_relay")
        self.transition("checkback_relay", "accepted", event("2D"), "checkback_invite")
        self.rule("checkback.weak_diamond_stop", op("lt", hcp, invitation), fixed("P"), "checkback_invite")
        for suit in ("H", "S", "D", "C"):
            self.rule("checkback.invite_suit_" + suit, all_of(
                eq(var("response_suit"), suit), ge(length(suit), 5), legal("3" + suit),
            ), fixed("3" + suit), "checkback_invite")
            self.transition("checkback_invite", "invite_" + suit, event("3" + suit), "natural", {
                "res_min": invitation, "res_max": op("sub", game, 1), "response_suit": suit,
            })
        self.rule("checkback.invite_nt", True, fixed("2NT"), "checkback_invite")
        self.transition("checkback_invite", "invite_nt", event("2NT"), "quantitative", {
            "res_min": invitation, "res_max": op("sub", game, 1), "invite_target": "game",
        })
        self.rule("checkback.artificial_game_force", ge(hcp, game), fixed("2D"), "checkback")
        self.transition("checkback", "game_force", event("2D"), "checkback_force", {
            "res_min": game, "res_max": 40,
        }, "game")
        self.rule("checkback.weak_heart_preference", all_of(eq(var("response_suit"), "S"),
              op("lt", hcp, invitation), any_of(ge(length("H"), 4), all_of(eq(var("open_suit"), "H"), support("H")))),
              fixed("2H"), "checkback")
        for suit in ("H", "S"):
            self.rule("checkback.three_card_support_" + suit, all_of(
                eq(var("response_suit"), suit), support(suit),
            ), fixed("2" + suit), "checkback_force")
            self.transition("checkback_force", "fit_" + suit, event("2" + suit), "natural", {"trump": suit})
        self.rule("checkback.force_nt", True, fixed("2NT"), "checkback_force")
        self.transition("checkback_force", "nt", event("2NT"), "natural")
        for suit in ("H", "S"):
            self.rule("checkback.weak_major_" + suit, all_of(
                eq(var("response_suit"), suit), ge(length(suit), 6), legal("2" + suit),
            ), fixed("2" + suit), "checkback")
            self.transition("checkback", "weak_major_" + suit, event("2" + suit), "finished")
        self.rule("checkback.weak_stop", True, fixed("P"), "checkback")

    def rebids(self):
        hcp = ref("hcp")
        minor_positive = all_of(self.precision, op("in", var("response_suit"), ["C", "D"]))
        # Precision 1C-1NT: the six-step support inquiry and shape/point inquiry
        # share one answer phase, with their public question type recorded.
        if self.precision:
            asking = eq(var("response_suit"), "NT")
            for suit in SUITS:
                allowed = any_of(asking, eq(var("response_suit"), "C") if suit in ("D", "H", "S") else False,
                                 eq(var("response_suit"), "D") if suit in ("H", "S") else False)
                allowed = all_of(allowed, legal("2" + suit))
                self.rule("strong_positive.solid_request_cues_" + suit,
                          all_of(allowed, ref("solid_" + suit), ge(length(suit), 7), legal("3" + suit)), fixed("3" + suit), "strong_positive")
                self.transition("strong_positive", "solid_request_cues_" + suit,
                                all_of(allowed, event("3" + suit)), "slam_cue",
                                {"trump": suit, "captain_" + suit + "_min": 7, "slam_active": True,
                                 "slam_started": True, "slam_solid": True}, "slam_try")
            self.rule("strong_positive.balanced_minimum_stop",
                      all_of(asking, within(hcp, 16, 17), ref("nt_extended"), legal("3NT")),
                      fixed("3NT"), "strong_positive")
            for requested, call in (("S", "2S"), ("H", "2H"), ("D", "2D"), ("C", "2NT")):
                self.rule("strong_positive.support_inquiry_" + requested,
                          all_of(asking, longest(requested, minimum=5), legal(call)),
                          fixed(call), "strong_positive")
                self.transition("strong_positive", "support_inquiry_" + requested,
                                all_of(event(call), eq(var("response_suit"), "NT")), "support_answer",
                                {"transfer": requested, "anchor": call, "invite_target": "six-step",
                                 "captain_" + requested + "_min": 5},
                                "one_round")
            self.rule("strong_positive.shape_inquiry", all_of(asking, legal("2C")),
                      fixed("2C"), "strong_positive")
            self.transition("strong_positive", "point_shape_inquiry",
                            all_of(event("2C"), eq(var("response_suit"), "NT")), "support_answer",
                            {"invite_target": "point-shape"}, "one_round")
            for requested, anchor in (("D", "2D"), ("H", "2H"), ("S", "2S"), ("C", "2NT")):
                has_support = any_of(support(requested, 4), all_of(support(requested, 3), ge(honors(requested, "AKQ"), 1)))
                for step, low, high, fits in (
                    (1, 8, 10, False), (2, 8, 10, True), (3, 11, 13, False),
                    (4, 11, 13, True), (5, 16, 40, False), (6, 16, 40, True),
                ):
                    self.rule(f"support.six_step_{requested}_{step}", all_of(
                        eq(var("invite_target"), "six-step"), eq(var("transfer"), requested),
                        within(hcp, low, high), has_support if fits else no(has_support),
                    ), {"kind": "step", "after": var("anchor"), "count": step}, "support_answer")
                    from bridge_evolve.core import ALL_BID_CALLS
                    from bridge_evolve.systems.language import parse_call
                    response_call = str(ALL_BID_CALLS[parse_call(anchor).ordering_index() + step])
                    self.transition("support_answer", f"six_step_{requested}_{step}", all_of(
                        eq(var("invite_target"), "six-step"), eq(var("transfer"), requested),
                        event(response_call),
                    ), "natural", {
                        "res_min": low, "res_max": high, "response_suit": requested,
                        "trump": requested if fits else "",
                        **({"responder_" + requested + "_min": 3, "responder_" + requested + "_support_quality": True}
                           if fits else {"responder_" + requested + "_max": op("min", var("responder_" + requested + "_max"), 3),
                                         "responder_" + requested + "_support_denied": True}),
                    })
            for suit in ("H", "S"):
                self.rule("support.shape_low_" + suit, all_of(
                    eq(var("invite_target"), "point-shape"), within(hcp, 8, 10), ge(length(suit), 4),
                ), fixed("2" + suit), "support_answer")
                self.transition("support_answer", "shape_low_" + suit,
                                all_of(eq(var("invite_target"), "point-shape"), event("2" + suit)),
                                "natural", {"res_min": 8, "res_max": 10, "response_suit": suit,
                                            "responder_" + suit + "_min": 4})
            self.rule("support.shape_low_no_major", all_of(eq(var("invite_target"), "point-shape"),
                                                         le(hcp, 10)), fixed("2D"), "support_answer")
            self.transition("support_answer", "shape_low_no_major",
                            all_of(eq(var("invite_target"), "point-shape"), event("2D")),
                            "natural", {"res_min": 8, "res_max": 10})
            self.rule("support.shape_mid", all_of(eq(var("invite_target"), "point-shape"),
                                                 within(hcp, 11, 13)), fixed("2NT"), "support_answer")
            self.transition("support_answer", "shape_mid",
                            all_of(eq(var("invite_target"), "point-shape"), event("2NT")),
                            "natural", {"res_min": 11, "res_max": 13})
            self.rule("support.shape_high", all_of(eq(var("invite_target"), "point-shape"),
                                                  ge(hcp, 16)), fixed("3NT"), "support_answer")
            self.transition("support_answer", "shape_high",
                            all_of(eq(var("invite_target"), "point-shape"), event("3NT")),
                            "natural", {"res_min": 16, "res_max": 40})
        if self.precision:
            for short in ("C", "D"):
                shape = all_of(eq(var("response_suit"), short), eq(length(short), 1),
                               *(eq(length(suit), 4) for suit in SUITS if suit != short))
                self.rule("strong_positive.4441_" + short, all_of(shape, within(hcp, 16, 18), legal("3NT")),
                          fixed("3NT"), "strong_positive")
                bounds = {f"captain_{suit}_{end}": 1 if suit == short else 4 for suit in SUITS for end in ("min", "max")}
                self.transition("strong_positive", "4441_" + short,
                                all_of(event("3NT"), eq(var("response_suit"), short)), "strong_4441_finish",
                                {"open_min": 16, "open_max": 18, **bounds}, "one_round")
            for major in ("H", "S"):
                self.rule("strong_4441.fit_" + major, all_of(support(major, 4), legal("4" + major)),
                          fixed("4" + major), "strong_4441_finish")
                self.transition("strong_4441_finish", "fit_" + major, event("4" + major), "finished",
                                {"trump": major, "responder_" + major + "_min": 4})
            self.rule("strong_4441.accept_nt", True, fixed("P"), "strong_4441_finish")
            self.transition("strong_4441_finish", "accept_nt", event("P"), "finished", forcing="none", allow_pass=True)
        # Special strong openings retain their own ranges before natural completion.
        for phase in ("strong_negative", "strong_positive"):
            for suit in ("H", "S"):
                self.rule(phase + ".fit_" + suit, all_of(eq(var("response_suit"), suit), support(suit)),
                          cheapest([suit], 2, 4), phase)
                for level in (2, 3, 4):
                    self.transition(phase, f"fit_{level}" + suit,
                                    all_of(event(str(level) + suit), eq(var("response_suit"), suit)),
                                    "natural", {"trump": suit})
            self.rule(phase + ".balanced_game",
                      all_of(ref("nt_extended"), ge(ref("total_min"), 25), legal("3NT"),
                             no(minor_positive) if phase == "strong_positive" else True), fixed("3NT"), phase)
            self.rule(phase + ".strong_nt_25", all_of(ref("nt_extended"), ge(hcp, 25), legal("3NT"),
                      no(minor_positive) if phase == "strong_positive" else True), fixed("3NT"), phase)
            self.rule(phase + ".strong_nt_22", all_of(ref("nt_extended"), ge(hcp, 22), legal("2NT")), fixed("2NT"), phase)
            self.rule(phase + ".rebid_nt", all_of(ref("nt_extended"), ge(hcp, 16), legal("1NT")), fixed("1NT"), phase)
            for suit in SUITS:
                self.rule(phase + ".longest_" + suit, longest(suit, minimum=5), cheapest([suit], 1, 4), phase)
            if self.precision and phase == "strong_positive":
                self.rule("strong_positive.minor_wait", all_of(minor_positive, *(le(length(suit), 4) for suit in SUITS),
                          op("lt", length(var("response_suit")), 4), legal("2NT")), fixed("2NT"), phase)
            for suit in ("H", "S", "C", "D"):
                self.rule(phase + ".four_card_suit_" + suit,
                          all_of(ge(length(suit), 4), can_bid_suit(suit, 1, 3),
                                 any_of(no(minor_positive), eq(var("response_suit"), suit)) if phase == "strong_positive" else True),
                          cheapest([suit], 1, 3), phase)
            for level in (1, 2, 3):
                if self.precision and phase == "strong_positive" and level in (2, 3):
                    self.transition(phase, "minor_wait_nt_" + str(level), all_of(event(str(level) + "NT"), minor_positive),
                                    "natural", {"open_suit": "NT", "open_min": 16, "open_max": 40, "nt_level": level},
                                    "one_round" if level == 2 else None)
                self.transition(phase, f"rebid_{level}NT", event(str(level) + "NT"),
                                "natural" if level == 3 else "nt", {
                                    "open_suit": "NT", "nt_level": level,
                                    "nt_style": "strong" if level == 1 else "two",
                                    "open_min": var("open_min") if level == 3 else (16, 22)[level - 1],
                                    "open_max": 40 if level == 3 else (21, 24)[level - 1],
                                })
            for suit in SUITS:
                for level in (1, 2, 3, 4):
                    self.transition(phase, f"rebid_{level}{suit}", event(str(level) + suit), "natural",
                                    {"open_suit": suit, "open_min": 16 if self.precision else 18, "open_max": 40,
                                     "captain_" + suit + "_min": choose(all_of(minor_positive, no(eq(var("response_suit"), suit))), 5, 4)
                                     if phase == "strong_positive" else 4})

        for phase in ("opener_rebid", "strong_positive"):
            # One-level major support is established before strength is spent.
            for suit in ("H", "S"):
                self.rule(phase + ".raise_game_" + suit, all_of(eq(var("response_suit"), suit),
                    support(suit, 4), ge(ref("total_min"), 25), legal("4" + suit)), fixed("4" + suit), phase)
                self.rule(phase + ".raise_limit_" + suit, all_of(eq(var("response_suit"), suit),
                    support(suit, 4), ge(hcp, 16), legal("3" + suit)), fixed("3" + suit), phase)
                self.rule(phase + ".raise_" + suit, all_of(eq(var("response_suit"), suit),
                    support(suit, 4), legal("2" + suit)), fixed("2" + suit), phase)
            for suit in ("S", "H"):
                minimum = choose(all_of(minor_positive, no(eq(var("response_suit"), suit))), 5, 4) if phase == "strong_positive" else 4
                self.rule(phase + ".new_major_" + suit, all_of(ge(length(suit), minimum),
                    no(eq(var("response_suit"), suit)), legal("1" + suit),
                    no(all_of(eq(var("open_suit"), "C"), eq(var("response_suit"), "D"), ref("nt_basic")))),
                    fixed("1" + suit), phase)
            self.rule(phase + ".nt_18_19", all_of(within(hcp, 18, 19), ref("nt_extended"), legal("2NT")), fixed("2NT"), phase)
            self.rule(phase + ".nt_minimum", all_of(ref("nt_extended"), legal("1NT"),
                      within(hcp, 11, 12) if self.precision and phase == "opener_rebid" else True), fixed("1NT"), phase)
            for suit in SUITS:
                self.rule(phase + ".six_card_rebid_" + suit, all_of(eq(var("open_suit"), suit), ge(length(suit), 6)),
                          cheapest([suit], 2, 4), phase)
                lower_suits = list(("C", "D", "H", "S")[:("C", "D", "H", "S").index(suit)])
                minimum = choose(all_of(minor_positive, no(eq(var("response_suit"), suit))), 5, 4) if phase == "strong_positive" else 4
                self.rule(phase + ".second_suit_" + suit, all_of(no(eq(var("open_suit"), suit)), ge(length(suit), minimum),
                         any_of(no(op("in", var("open_suit"), lower_suits)) if lower_suits else True,
                             ge(hcp, 14 if self.precision else 16), eq(ctx("forcing"), "game"))),
                          cheapest([suit], 2, 3), phase)
            for suit in SUITS:
                self.rule(phase + ".five_card_repeat_" + suit,
                          all_of(eq(var("open_suit"), suit), ge(length(suit), 5), can_bid_suit(suit, 2, 4)),
                          cheapest([suit], 2, 4), phase)
        # Rebid meanings are public and inferred from the actual call, not rule trace.
        for phase in ("opener_rebid",):
            for suit in ("H", "S"):
                for level, low, high in ((2, 11, 15), (3, 16, 17), (4, 18, 21)):
                    self.transition(phase, f"support_{level}{suit}",
                        all_of(event(str(level) + suit), eq(var("response_suit"), suit)), "natural",
                        {"trump": suit, "open_min": 16 if level == 3 else var("open_min"),
                         "open_max": op("min", var("open_max"), op("sub", 24, var("responder_hcp_min"))) if level == 3 else high})
            for call, low, high in (("1NT", 11 if self.precision else 12, 12 if self.precision else 14),
                                    ("2NT", 18, 19), ("3NT", 20, 24)):
                self.transition(phase, "rebid_" + call, event(call), "natural",
                                {"open_min": low, "open_max": high, "nt_level": 1})
            self.transition(phase, "natural_rebid", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")

        # Inverted raises and Jacoby relay answers communicate features/ranges.
        for phase in ("jacoby", "inverted"):
            for suit in SUITS:
                self.rule(phase + ".short_" + suit, all_of(no(eq(suit, var("open_suit"))),
                    le(length(suit), 1), legal("3" + suit)), fixed("3" + suit), phase)
            self.rule(phase + ".high_limit_nt", all_of(ge(hcp, 16), ref("nt_extended"), legal("3NT")), fixed("3NT"), phase)
            self.rule(phase + ".minimum_suit", True,
                      cheapest({"op": "list", "item_type": "string", "args": [var("open_suit")]}, 3, 4), phase)
            for suit in SUITS:
                self.transition(phase, "short_" + suit,
                    all_of(event("3" + suit), no(eq(var("open_suit"), suit))), "natural",
                    {"short_suit": suit})
            self.transition(phase, "answer", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")
        # Precision 4441 positive: the short suit is known before the asking call.
        self.rule("shape.ask_points", True, {"kind": "step", "after": ctx("last_bid"), "count": 1}, "shape_ask")
        self.transition("shape_ask", "asked", all_of(ours(), eq(ctx("event.kind"), "BID")), "shape_answer",
                        {"anchor": ctx("event.call")}, "one_round")
        self.rule("shape.answer_low", le(hcp, 10), {"kind": "step", "after": var("anchor"), "count": 1}, "shape_answer")
        self.rule("shape.answer_mid", le(hcp, 13), {"kind": "step", "after": var("anchor"), "count": 2}, "shape_answer")
        self.rule("shape.answer_high", ge(hcp, 14), {"kind": "step", "after": var("anchor"), "count": 3}, "shape_answer")
        self.transition("shape_answer", "answered", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural",
                        {"res_min": 8, "res_max": 40})
        # Ogust 2NT inquiry, exact documented ordinal answers.
        for call, condition, low, high in (
            ("3NT", any_of(*(all_of(eq(var("open_suit"), suit), ref(f"solid_{suit}")) for suit in SUITS)), 9, 10),
            ("3S", all_of(ge(hcp, 9), any_of(*(all_of(eq(var("open_suit"), suit), ref(f"good_{suit}")) for suit in SUITS))), 9, 10),
            ("3H", ge(hcp, 9), 9, 10),
            ("3D", any_of(*(all_of(eq(var("open_suit"), suit), ref(f"good_{suit}")) for suit in SUITS)), 6, 8),
            ("3C", True, 6, 8),
        ):
            self.rule("weak.ask_" + call, condition, fixed(call), "weak_ask")
            self.transition("weak_ask", "answer_" + call, event(call), "natural",
                            {"open_min": low, "open_max": choose(eq(ctx("opening_position"), 4), 12, high)})

    def slam_cuebids(self):
        from bridge_evolve.systems.language import parse_call
        order = ("C", "D", "H", "S")
        phases = ("natural", "finished", "jacoby_finish", "inverted_finish", "stayman_finish",
                  "transfer_finish", "puppet_finish", "precision_club_finish", "opener_rebid",
                  "responder_rebid", "reverse_choose", "mini_choose", "smolen_reply", "nt_minor_shape")

        def fit_updates(trump):
            updates = {"trump": trump, "slam_active": True, "slam_started": True}
            for owner, actor_is_owner in (
                ("captain", eq(ctx("event.actor"), var("captain"))),
                ("responder", no(eq(ctx("event.actor"), var("captain")))),
            ):
                other = "responder" if owner == "captain" else "captain"
                key = owner + "_" + trump + "_min"
                updates[key] = op("max", var(key), choose(actor_is_owner,
                    op("max", 0, op("sub", 8, var(other + "_" + trump + "_min"))), 0))
            return updates

        for phase in ("natural", "finished"):
            nt_question = all_of(eq(var("trump"), ""), eq(ctx("last_bid_strain"), "NT"),
                                 eq(ctx("last_bid_by"), "ours"))
            self.rule("slam_controls.quantitative_" + phase, all_of(
                nt_question, ref("nt_slam_interest"), no(var("nt_slam_invited")),
                no(eq(ctx("forcing"), "one_round")), legal("4NT")), fixed("4NT"), phase)
            updates = {"invite_target": "slam", "slam_started": True, "nt_slam_invited": True}
            for owner, actor_is_owner in (("captain", eq(ctx("event.actor"), var("captain"))),
                                         ("responder", no(eq(ctx("event.actor"), var("captain"))))):
                other = "responder" if owner == "captain" else "captain"
                key = owner + "_hcp_min"
                updates[key] = op("max", var(key), choose(actor_is_owner,
                    op("max", 0, op("sub", 33, var(other + "_hcp_max"))), 0))
            self.transition(phase, "quantitative_slam", all_of(nt_question, event("4NT")),
                            "quantitative", updates, "slam_try")
        for phase in (*phases, "slam_cue"):
            self.transition(phase, "control_rkcb", all_of(op("in", var("trump"), list(order)), event("4NT")), "rkcb",
                            {"anchor": "4NT", "asker": ctx("event.actor"), "slam_active": True,
                             "slam_started": True}, "slam_try")
        for trump in order:
            game = 4 if trump in ("H", "S") else 5
            c = eq(var("trump"), trump)
            known_trump = all_of(c, ge(ctx("agreed_trump_min"), 8), ref("fit_" + trump))
            interest = all_of(ref("slam_possible_" + trump), ref("slam_willing_" + trump))
            covered = all_of(*(any_of(ref("control_" + side),
                var("captain_" + side + "_control"), var("responder_" + side + "_control"))
                for side in order if side != trump))
            key_floor = 2 if trump in ("H", "S") else 3 if trump == "D" else 4
            retreat_safe = (True if trump == "S" else ref("small_slam_values_" + trump) if trump == "C"
                            else any_of(ref("trump_q"), ref("small_slam_values_" + trump)))
            safe_question = all_of(covered, ge(ref("keys"), key_floor),
                ref("trump_q") if trump == "C" else True,
                interest, retreat_safe, legal("4NT"))
            self.raw["features"]["slam_interest_" + trump] = interest
            self.raw["features"]["safe_keycards_" + trump] = safe_question
            exclusion_ready = all_of(ge(length(trump), 6), interest, ref("small_slam_values_" + trump), ge(ref("keys"), 2),
                any_of(*(all_of(eq(length(side), 0), legal("4S" if side == "S" else "5" + side))
                         for side in order if side != trump))) if trump in ("H", "S") else False
            self.raw["features"]["exclusion_ready_" + trump] = exclusion_ready
            for phase in (*phases, "slam_cue"):
                eligible = all_of(c, ref("fit_" + trump), ref("safe_keycards_" + trump),
                                  no(ref("exclusion_ready_" + trump)),
                                  no(eq(ctx("forcing"), "one_round")), eq(ctx("last_bid_by"), "ours"))
                self.rule(f"slam_controls.ask_{phase}_{trump}", eligible, fixed("4NT"), phase)
            for phase in phases:
                forcing = any_of(eq(ctx("forcing"), "game"), all_of(eq(ctx("goal"), "game"), no(ctx("goal_met"))))
                partner_length = choose(eq(ctx("event.actor"), var("captain")),
                                        var("responder_" + trump + "_min"), var("captain_" + trump + "_min"))
                # Ordinary opener rebids share this call: new suits promise four, repeats five.
                required_partner = choose(eq(var("open_suit"), trump), 3, 4)
                private_fit_encoding = ge(ctx("partner_" + trump + "_min"), required_partner) if phase == "opener_rebid" else True
                public_fit_encoding = ge(partner_length, required_partner) if phase == "opener_rebid" else True
                for level in range(2, game):
                    call = str(level) + trump
                    discover = all_of(no(ref("have_fit")), ref("fit_" + trump),
                        ge(ctx("partner_" + trump + "_min"), 2 if trump in ("C", "D") else 3),
                        any_of(eq(var("open_suit"), trump), eq(var("response_suit"), trump)),
                        forcing, private_fit_encoding, legal(call))
                    self.rule(f"slam_controls.fit_{phase}_{call}", discover, fixed(call), phase)
                    self.transition(phase, "slam_fit_" + call, all_of(op("lt", ctx("agreed_trump_min"), 8), forcing, event(call),
                        public_fit_encoding,
                        ge(partner_length, 2 if trump in ("C", "D") else 3),
                        any_of(eq(var("open_suit"), trump), eq(var("response_suit"), trump))),
                        "slam_cue", fit_updates(trump), "slam_try")
                    self.rule(f"slam_controls.confirm_{phase}_{call}", all_of(c, ref("fit_" + trump),
                        op("lt", ctx("agreed_trump_min"), 8), forcing, private_fit_encoding, legal(call)), fixed(call), phase)
                entry = all_of(known_trump, ref("prefer_" + trump), ref("slam_interest_" + trump), no(var("slam_started")),
                               no(ref("exclusion_ready_" + trump)),
                               eq(ctx("last_bid_by"), "ours"), no(eq(ctx("forcing"), "one_round")),
                               any_of(eq(ctx("forcing"), "game"), all_of(eq(ctx("goal"), "game"), no(ctx("goal_met")))))
                if game == 4:
                    self.rule(f"slam_controls.invite_{phase}_{trump}", all_of(entry, legal("3" + trump)),
                              fixed("3" + trump), phase)
                for side in order:
                    if side == trump:
                        continue
                    ceiling = game if order.index(side) < order.index(trump) else game - 1
                    for level in range(3, game + 1):
                        call = str(level) + side
                        if parse_call(call).ordering_index() >= parse_call(str(game) + trump).ordering_index():
                            continue
                        self.rule(f"slam_controls.start_{phase}_{trump}_{call}",
                                  all_of(entry, ref("control_" + side), legal(call)), fixed(call), phase)
                self.transition(phase, "slam_invite_" + trump,
                    all_of(c, forcing, ours(), eq(ctx("event.strain"), trump), within(ctx("event.level"), 2, game - 1)),
                    "slam_cue", fit_updates(trump), "slam_try")
                for side in order:
                    if side == trump:
                        continue
                    ceiling = game if order.index(side) < order.index(trump) else game - 1
                    updates = fit_updates(trump)
                    for owner, actor_is_owner in (
                        ("captain", eq(ctx("event.actor"), var("captain"))),
                        ("responder", no(eq(ctx("event.actor"), var("captain")))),
                    ):
                        control = owner + "_" + side + "_control"
                        updates[control] = any_of(var(control), actor_is_owner)
                    condition = all_of(c, ge(ctx("agreed_trump_min"), 8), forcing, ours(), eq(ctx("event.kind"), "BID"),
                                       eq(ctx("event.strain"), side), within(ctx("event.level"), 3, ceiling))
                    self.transition(phase, f"slam_control_{trump}_{side}", condition, "slam_cue", updates, "slam_try")
                    self.announce(f"control.{phase}.{trump}.{side}", all_of(state(phase), condition), ["artificial"])
            for side in order:
                if side == trump:
                    continue
                already = any_of(var("captain_" + side + "_control"), var("responder_" + side + "_control"))
                ceiling = game if order.index(side) < order.index(trump) else game - 1
                for level in range(3, game + 1):
                    call = str(level) + side
                    if parse_call(call).ordering_index() >= parse_call(str(game) + trump).ordering_index():
                        continue
                    self.rule(f"slam_controls.reply_{trump}_{call}",
                              all_of(c, no(already), no(ref("exclusion_ready_" + trump)), ref("control_" + side), legal(call)),
                              fixed(call), "slam_cue")
                updates = {}
                for owner, actor_is_owner in (("captain", eq(ctx("event.actor"), var("captain"))),
                                             ("responder", no(eq(ctx("event.actor"), var("captain"))))):
                    field = owner + "_" + side + "_control"
                    updates[field] = any_of(var(field), actor_is_owner)
                condition = all_of(c, ours(), eq(ctx("event.kind"), "BID"),
                                   eq(ctx("event.strain"), side), within(ctx("event.level"), 3, ceiling))
                self.transition("slam_cue", f"control_{trump}_{side}", condition, "slam_cue", updates, "slam_try")
                self.announce(f"control.reply.{trump}.{side}", all_of(state("slam_cue"), condition), ["artificial"])
            self.rule("slam_controls.signoff_" + trump, all_of(c, no(ref("exclusion_ready_" + trump)), legal(str(game) + trump)),
                      fixed(str(game) + trump), "slam_cue")
            self.rule("slam_controls.accept_game_" + trump, all_of(c, eq(ctx("last_bid_strain"), trump),
                      no(ref("exclusion_ready_" + trump)), ge(ctx("last_bid_level"), game), no(ctx("must_bid"))), fixed("P"), "slam_cue")
            self.transition("slam_cue", "return_game_" + trump, all_of(c, event(str(game) + trump)),
                            "finished", {"slam_active": False}, "game")
            self.transition("slam_cue", "accept_game_" + trump, all_of(c, event("P")),
                            "finished", {"slam_active": False}, "none", allow_pass=True)

    def slam_continuations(self):
        from bridge_evolve.core import ALL_BID_CALLS
        from bridge_evolve.systems.language import parse_call
        order = ("C", "D", "H", "S")
        combined = add(ref("keys"), var("answer_min"))
        have_queen = any_of(ref("trump_q"), var("answer_q"))
        for major in ("H", "S"):
            c = eq(var("trump"), major)
            for void in order:
                if void == major:
                    continue
                anchor = "4S" if void == "S" else "5" + void
                for phase in ("natural", "slam_cue"):
                    self.rule(f"exclusion.ask_{phase}_{major}_{void}", all_of(
                        c, ge(length(major), 6), eq(length(void), 0), ref("exclusion_ready_" + major),
                        ge(ref("keys"), 2), legal(anchor),
                    ), fixed(anchor), phase)
                    self.transition(phase, f"exclusion_{major}_{void}", all_of(c, event(anchor)), "void_answer", {
                        "void_suit": void, "anchor": anchor, "asker": ctx("event.actor"),
                    }, "one_round")
                score2 = add(op("mul", ref("excluded_keys"), 2), number_if(ref("trump_q")))
                for step, low, high, minimum, queen in (
                    (1, 0, 1, 0, False), (2, 2, 2, 1, False),
                    (3, 3, 3, 1, True), (4, 4, 4, 2, False), (5, 5, 5, 2, True),
                ):
                    self.rule(f"exclusion.answer_{major}_{void}_{step}", all_of(c, eq(var("void_suit"), void),
                        within(score2, low, high)), {"kind": "step", "after": var("anchor"), "count": step}, "void_answer")
                    call = str(ALL_BID_CALLS[parse_call(anchor).ordering_index() + step])
                    self.transition("void_answer", f"answer_{major}_{void}_{step}", all_of(c, eq(var("void_suit"), void), event(call)),
                                    "void_choose", {"answer_min": minimum, "answer_q": queen})
            self.rule("exclusion.direct_grand_" + major, all_of(c, ge(ref("excluded_keys"), 3)), fixed("7" + major), "void_answer")
            self.transition("void_answer", "grand_" + major, all_of(c, event("7" + major)), "finished", forcing="none")
            excluded_total = add(ref("excluded_keys"), var("answer_min"))
            self.rule("exclusion.grand_" + major, all_of(c, ge(excluded_total, 4), have_queen,
                      ge(ref("total_min"), 35), legal("7" + major)), fixed("7" + major), "void_choose")
            self.rule("exclusion.small_" + major, all_of(c, ge(excluded_total, 3), have_queen,
                      legal("6" + major)), fixed("6" + major), "void_choose")
            self.rule("exclusion.already_trump_" + major, all_of(c, eq(ctx("last_bid_strain"), major)), fixed("P"), "void_choose")
            self.rule("exclusion.retreat_" + major, c, cheapest([major], 5, 6), "void_choose")
            for answer in ("5C", "5D"):
                question = next(str(call) for call in ALL_BID_CALLS if call.ordering_index() > parse_call(answer).ordering_index()
                                and call.level == 5 and call.strain.value in order and call.strain.value != major)
                high = parse_call(question).ordering_index() > parse_call("5" + major).ordering_index()
                self.rule(f"queen.ask_{major}_{answer}", all_of(c, eq(ctx("last_bid"), answer),
                    no(ref("trump_q")), no(var("answer_q")), ref("small_slam_values_" + major),
                    ge(combined, 5 if high else 4), legal(question)),
                    fixed(question), "rkcb_choose")
                self.transition("rkcb_choose", f"queen_{major}_{answer}", all_of(c, event(question)), "queen_answer", {
                    "anchor": question, "queen_high": high,
                }, "one_round")
            for side in order:
                if side == major:
                    continue
                available = any_of(legal("5" + side), all_of(order.index(side) < order.index(major), legal("6" + side)))
                self.rule(f"queen.show_king_{major}_{side}", all_of(c, ref("trump_q"), card(side, "K"), available),
                          cheapest([side], 5, 6), "queen_answer")
                self.transition("queen_answer", f"king_{major}_{side}", all_of(c, ours(), eq(ctx("event.strain"), side)),
                                "queen_choose", {"answer_q": True})
            self.rule("queen.deny_low_" + major, all_of(c, no(ref("trump_q")), legal("5" + major)), fixed("5" + major), "queen_answer")
            self.rule("queen.deny_high_" + major, all_of(c, no(ref("trump_q"))), fixed("6" + major), "queen_answer")
            self.rule("queen.no_king_high_" + major, all_of(c, var("queen_high"), legal("5NT")), fixed("5NT"), "queen_answer")
            self.rule("queen.no_king_low_" + major, c, fixed("6" + major), "queen_answer")
            self.transition("queen_answer", "deny_low_" + major, all_of(c, event("5" + major)), "queen_choose", {"answer_q": False})
            self.transition("queen_answer", "six_" + major, all_of(c, event("6" + major)), "queen_choose", {
                "answer_q": no(var("queen_high")),
            })
            self.transition("queen_answer", "no_king_" + major, all_of(c, event("5NT")), "queen_choose", {"answer_q": True})
            for phase in ("rkcb_choose", "queen_choose"):
                self.rule(phase + ".ask_side_kings_" + major, all_of(c, ge(combined, 5), have_queen,
                          ge(ref("total_min"), 35), legal("5NT")), fixed("5NT"), phase)
                self.transition(phase, "side_kings_" + major, all_of(c, event("5NT")), "king_answer", forcing="one_round")
            self.rule("queen.choose_grand_" + major, all_of(c, ge(combined, 5), have_queen,
                      ge(ref("total_min"), 37), legal("7" + major)), fixed("7" + major), "queen_choose")
            self.rule("queen.choose_small_" + major, all_of(c, ge(combined, 4), have_queen,
                      legal("6" + major)), fixed("6" + major), "queen_choose")
            self.rule("queen.already_trump_" + major, all_of(c, eq(ctx("last_bid_strain"), major)), fixed("P"), "queen_choose")
            self.rule("queen.retreat_" + major, c, cheapest([major], 5, 6), "queen_choose")
            self.rule("kings.extra_winners_" + major, all_of(c, ge(ref("playing_tricks2"), 24), legal("7" + major)),
                      fixed("7" + major), "king_answer")
            for side in order[:order.index(major)]:
                self.rule(f"kings.show_{major}_{side}", all_of(c, card(side, "K"), legal("6" + side)),
                          fixed("6" + side), "king_answer")
            self.rule("kings.none_below_trump_" + major, c, fixed("6" + major), "king_answer")
            self.rule("kings.choose_grand_" + major, all_of(c, ge(ref("total_min"), 37), legal("7" + major)),
                      fixed("7" + major), "king_choose")
            self.rule("kings.already_trump_" + major, all_of(c, eq(ctx("last_bid_strain"), major)), fixed("P"), "king_choose")
            self.rule("kings.choose_small_" + major, c, fixed("6" + major), "king_choose")
        self.transition("king_answer", "answered", all_of(ours(), eq(ctx("event.kind"), "BID")), "king_choose")
        for phase in ("queen_choose", "king_choose", "void_choose"):
            self.transition(phase, "finished", all_of(ours(), eq(ctx("event.kind"), "BID")), "finished", forcing="none")

    def questions(self):
        # Request RKCB only when a five-level retreat is still meaningful.
        eligible = all_of(ref("have_fit"), any_of(*(all_of(eq(var("trump"), suit),
                          ref("safe_keycards_" + suit), no(ref("exclusion_ready_" + suit))) for suit in ("H", "S"))),
                          eq(ctx("last_bid_by"), "ours"), no(eq(ctx("forcing"), "one_round")))
        # These rules precede the generic completion, but not explicit protocol answers.
        for phase in ("natural",):
            self.rule("slam.ask_keycards", eligible, fixed("4NT"), phase)
            self.transition(phase, "rkcb_asked", event("4NT"), "rkcb",
                            {"anchor": "4NT", "asker": ctx("event.actor")}, "one_round")
        for label, condition, count, minimum, queen in (
            ("14", op("in", ref("keys"), [1, 4]), 1, 1, False),
            ("03", op("in", ref("keys"), [0, 3]), 2, 0, False),
            ("25_no_q", all_of(op("in", ref("keys"), [2, 5]), no(ref("trump_q"))), 3, 2, False),
            ("25_with_q", all_of(op("in", ref("keys"), [2, 5]), ref("trump_q")), 4, 2, True),
        ):
            call = ("5C", "5D", "5H", "5S")[count - 1]
            self.rule("slam.answer_" + label, condition,
                      {"kind": "step", "after": var("anchor"), "count": count}, "rkcb")
            self.transition("rkcb", "answer_" + label, event(call), "rkcb_choose",
                            {"answer_min": minimum, "answer_q": queen})
        self.rule("slam.grand", all_of(ge(add(ref("keys"), var("answer_min")), 5),
                                      any_of(ref("trump_q"), var("answer_q")),
                                      ge(ref("total_min"), 37)),
                  bid(7, var("trump")), "rkcb_choose")
        self.rule("slam.small", all_of(ge(add(ref("keys"), var("answer_min")), 4),
                                      any_of(ref("trump_q"), var("answer_q")),
                                      any_of(*(all_of(eq(var("trump"), suit), ref("small_slam_values_" + suit)) for suit in SUITS))),
                  bid(6, var("trump")), "rkcb_choose")
        self.rule("slam.already_at_retreat", all_of(eq(ctx("last_bid_strain"), var("trump")),
                                                   ge(ctx("last_bid_level"), 5)), fixed("P"), "rkcb_choose")
        self.rule("slam.retreat", True, cheapest({"op": "list", "item_type": "string", "args": [var("trump")]}, 5, 6), "rkcb_choose")
        self.transition("rkcb_choose", "finished", all_of(ours(), eq(ctx("event.kind"), "BID")), "finished")
        aces = add(*(honors(suit, "A") for suit in SUITS))
        for call, values in (("4D", [0, 4]), ("4H", [1]), ("4S", [2]), ("4NT", [3])):
            self.rule("gerber.answer_" + call, op("in", aces, values), fixed(call), "gerber")
            self.transition("gerber", "answered_" + call, event(call), "gerber_finish", {"answer_min": min(values)})
        self.rule("gerber.grand", all_of(eq(add(aces, var("answer_min")), 4), ge(ref("total_min"), 37), legal("7NT")), fixed("7NT"), "gerber_finish")
        self.rule("gerber.small", all_of(ge(add(aces, var("answer_min")), 3), ge(ref("total_min"), 33), legal("6NT")), fixed("6NT"), "gerber_finish")
        self.rule("gerber.retreat", True, cheapest(["NT"], 4, 6), "gerber_finish")
        self.transition("gerber_finish", "finished", all_of(ours(), eq(ctx("event.kind"), "BID")), "finished")

    def manual_competition(self):
        hcp = ref("hcp")
        first = all_of(eq(var("captain"), ""), {"op": "has_contract_bid"})
        strong_club = all_of(first, eq(ctx("opening_strain"), "C"), eq(ctx("last_bid"), "1C"),
                            {"op": "announced", "which": "opening", "tag": "strong_club"})
        self.rule("marshall.double_majors", all_of(strong_club, ge(hcp, 8), ge(length("H"), 4),
                  ge(length("S"), 4), ge(add(length("H"), length("S")), 9), legal("X")), fixed("X"))
        for call, minimum, total in (("2NT", 5, 10), ("1NT", 4, 9)):
            self.rule("marshall.minors_" + call, all_of(strong_club, ge(hcp, 8), ge(length("C"), minimum),
                      ge(length("D"), minimum), ge(add(length("C"), length("D")), total), legal(call)), fixed(call))
            self.transition("idle", "marshall_" + call, all_of(event(call), eq(ctx("opening_strain"), "C"),
                            eq(ctx("last_bid"), "1C"),
                            {"op": "announced", "which": "opening", "tag": "strong_club"}), "two_suit_select", {
                "captain": ctx("event.actor"), "opening_kind": "marshall", "open_min": 8, "open_max": 40,
                "two_suit_a": "C", "two_suit_b": "D", "enemy_suit": "C",
            }, "one_round")
        self.transition("idle", "marshall_double", all_of(event("X"), eq(ctx("opening_strain"), "C"),
                        eq(ctx("last_bid"), "1C"),
                        {"op": "announced", "which": "opening", "tag": "strong_club"}), "two_suit_select", {
            "captain": ctx("event.actor"), "opening_kind": "marshall", "open_min": 8, "open_max": 40,
            "two_suit_a": "H", "two_suit_b": "S", "enemy_suit": "C",
        }, "one_round")
        responding = all_of(no(ref("am_captain")), no(eq(var("captain"), "")))
        strong_response = eq(var("opening_kind"), "strong_club")
        for major in ("H", "S"):
            self.rule("competition.constructive_raise_" + major, all_of(responding, eq(var("open_suit"), major),
                      support(major), ge(hcp, 6), can_bid_suit(major, 2, 3)), cheapest([major], 2, 3), "competition")
        for suit in SUITS:
            for level in (1, 2, 3):
                minimum = 6 if level == 1 else 11 if level == 2 else 12
                self.rule(f"competition.natural_response_{level}{suit}", all_of(responding, ge(length(suit), 5),
                    no(eq(ctx("opponent_bid_strain"), suit)), ge(hcp, choose(strong_response, 8, minimum)),
                    legal(str(level) + suit)), fixed(str(level) + suit), "competition")
        negative = all_of(responding, eq(ctx("our_bid_count"), 1), eq(ctx("opener"), var("captain")),
                          legal("X"), le(ctx("last_bid_level"), 3),
                          ge(hcp, choose(strong_response, 5, choose(eq(ctx("last_bid_level"), 1), 6,
                              choose(eq(ctx("last_bid_level"), 2), 8, 10)))),
                          any_of(strong_response, *(all_of(no(eq(ctx("opponent_bid_strain"), major)), no(eq(var("open_suit"), major)),
                                    ge(length(major), 4)) for major in ("H", "S"))))
        self.rule("competition.negative_double", negative, fixed("X"), "competition")
        self.transition("competition", "negative_double", all_of(event("X"), no(eq(ctx("event.actor"), var("captain")))),
                        "negative_answer", {"res_min": choose(strong_response, 5, choose(eq(ctx("last_bid_level"), 1), 6,
                            choose(eq(ctx("last_bid_level"), 2), 8, 10))), "res_max": 40}, "one_round")
        for major in ("H", "S"):
            self.rule("negative.show_" + major, all_of(no(eq(ctx("opponent_bid_strain"), major)),
                      ge(length(major), 4), can_bid_suit(major)), cheapest([major]), "negative_answer")
        self.rule("negative.stopped_nt", all_of(ref("enemy_stopped"), legal("2NT")), fixed("2NT"), "negative_answer")
        self.rule("negative.original_suit", True,
                  cheapest({"op": "list", "item_type": "string", "args": [choose(eq(var("open_suit"), ""), "C", var("open_suit"))]}),
                  "negative_answer")
        self.transition("negative_answer", "answered", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural", {
            "response_suit": ctx("event.strain"),
        })
        for level, minimum, maximum in ((3, 13, 40), (2, 11, 12), (1, 6, 10)):
            self.rule(f"competition.natural_nt_{level}", all_of(responding, ge(hcp, minimum), ref("enemy_stopped"),
                      ge(ref("total_min"), 25 if level == 3 else 23) if level > 1 else True,
                      legal(str(level) + "NT")), fixed(str(level) + "NT"), "competition")
        for opening, major in (("H", "S"), ("S", "H")):
            for minor in ("C", "D"):
                self.rule(f"defense.leaping_{opening}_{minor}", all_of(first, eq(ctx("opponent_bid_strain"), opening),
                    eq(ctx("last_bid_level"), 2), ge(hcp, 17), ge(length(major), 5), ge(length(minor), 5), legal("4" + minor)),
                    fixed("4" + minor))
                self.transition("idle", f"leaping_{opening}_{minor}", all_of(event("4" + minor),
                                eq(ctx("opponent_bid_strain"), opening), eq(ctx("last_bid_level"), 2)), "two_suit_select", {
                    "captain": ctx("event.actor"), "opening_kind": "leaping_michaels", "open_min": 17, "open_max": 40,
                    "two_suit_a": major, "two_suit_b": minor, "enemy_suit": opening,
                }, "game")

    def competitive_conventions(self):
        hcp = ref("hcp")
        enemy = ctx("opponent_bid_strain")
        first = all_of(eq(var("captain"), ""), {"op": "has_contract_bid"})
        natural_club = no({"op": "announced", "which": "opening", "tag": "strong_club"})
        order = ("C", "D", "H", "S")
        for opening in order:
            at_one = all_of(first, eq(enemy, opening), eq(ctx("last_bid_level"), 1),
                            natural_club if opening == "C" else True)
            major = "S" if opening == "H" else "H"
            both_majors = opening in ("C", "D")
            self.rule("defense.michaels_" + opening, all_of(at_one, ge(hcp, 10 if both_majors else 12),
                ge(length("H" if both_majors else major), 5),
                ge(length("S"), 5) if both_majors else any_of(ge(length("C"), 5), ge(length("D"), 5)),
                legal("2" + opening)), fixed("2" + opening))
            self.transition("idle", "michaels_" + opening, all_of(event("2" + opening), eq(enemy, opening),
                            eq(ctx("last_bid_level"), 1)), "two_suit_select", {
                "captain": ctx("event.actor"), "opening_kind": "michaels", "open_min": 10 if both_majors else 12,
                "open_max": 40, "two_suit_a": "H" if both_majors else major,
                "two_suit_b": "S" if both_majors else "", "enemy_suit": opening,
            }, "one_round")
            low_suits = [suit for suit in order if suit != opening][:2]
            self.rule("defense.unusual_nt_" + opening, all_of(at_one, ge(hcp, 12),
                *(ge(length(suit), 5) for suit in low_suits), legal("2NT")), fixed("2NT"))
            self.transition("idle", "unusual_nt_" + opening, all_of(event("2NT"), eq(enemy, opening),
                            eq(ctx("last_bid_level"), 1)), "two_suit_select", {
                "captain": ctx("event.actor"), "opening_kind": "unusual_nt", "open_min": 12, "open_max": 40,
                "two_suit_a": low_suits[0], "two_suit_b": low_suits[1], "enemy_suit": opening,
            }, "one_round")
        natural_nt_opening = all_of(eq(ctx("opening_strain"), "NT"), eq(ctx("last_bid_level"), 1),
                       {"op": "announced", "which": "opening", "tag": "natural"})
        landy = all_of(first, eq(enemy, "NT"), natural_nt_opening)
        self.rule("defense.landy", all_of(landy, ge(hcp, choose({"op": "vulnerable"}, 12, 11)),
                  ge(length("H"), 4), ge(length("S"), 4), ge(add(length("H"), length("S")), 9)), fixed("2C"))
        self.transition("idle", "landy", all_of(event("2C"), eq(enemy, "NT"), natural_nt_opening), "two_suit_select", {
            "captain": ctx("event.actor"), "opening_kind": "landy", "open_min": 11, "open_max": 40,
            "two_suit_a": "H", "two_suit_b": "S", "enemy_suit": "NT",
        }, "one_round")
        self.rule("two_suit.ask_unknown_minor", all_of(eq(var("two_suit_b"), ""),
                  le(length(var("two_suit_a")), 2), legal("2NT")), fixed("2NT"), "two_suit_select")
        self.transition("two_suit_select", "ask_minor", all_of(event("2NT"), eq(var("two_suit_b"), "")),
                        "two_suit_minor", forcing="one_round")
        self.rule("landy.ask_equal_majors", all_of(eq(var("opening_kind"), "landy"),
                  eq(length("H"), length("S")), legal("2D")), fixed("2D"), "two_suit_select")
        self.transition("two_suit_select", "landy_ask", all_of(event("2D"), eq(var("opening_kind"), "landy")),
                        "landy_answer", forcing="one_round")
        for suit in ("C", "D"):
            self.rule("two_suit.show_minor_" + suit, longest(suit, ("C", "D")), fixed("3" + suit), "two_suit_minor")
            self.transition("two_suit_minor", "minor_" + suit, event("3" + suit), "two_suit_pick", {"two_suit_b": suit})
        for major in ("H", "S"):
            self.rule("landy.answer_" + major, longest(major, ("H", "S")), fixed("2" + major), "landy_answer")
            self.transition("landy_answer", "major_" + major, event("2" + major), "natural", {"open_suit": major, "trump": major})
        for phase in ("two_suit_select", "two_suit_pick"):
            self.rule(phase + ".already_selected", all_of(
                eq(ctx("last_bid_strain"), var("two_suit_b")),
                ge(length(var("two_suit_b")), length(var("two_suit_a"))), no(ctx("must_bid")),
            ), fixed("P"), phase)
            for suit in order:
                known = any_of(eq(var("two_suit_a"), suit), eq(var("two_suit_b"), suit))
                better = all_of(known, *(choose(
                    any_of(eq(var("two_suit_a"), other), eq(var("two_suit_b"), other)),
                    ge(length(suit), length(other)), True,
                ) for other in order if other != suit))
                self.rule(phase + ".game_" + suit, all_of(known, ge(length(suit), 3),
                    ge(ref("total_min"), 25 if suit in ("H", "S") else 29), legal(("4" if suit in ("H", "S") else "5") + suit)),
                    fixed(("4" if suit in ("H", "S") else "5") + suit), phase)
                self.rule(phase + ".choose_" + suit, all_of(better, can_bid_suit(suit)), cheapest([suit]), phase)
                for level in range(2, 6):
                    self.transition(phase, f"choose_{level}{suit}", event(str(level) + suit), "natural", {
                        "open_suit": suit, "response_suit": suit, "trump": suit, "res_min": 0, "res_max": 40,
                    })
            self.rule(phase + ".already_preferred", no(ctx("must_bid")), fixed("P"), phase)
        immediate_nt_interference = all_of(eq(ctx("last_actor"), var("captain")),
            eq(ctx("last_call"), ctx("last_bid")), eq(ctx("last_bid_strain"), "NT"))
        self.transition("nt", "doubled_escape", all_of(immediate_nt_interference, eq(ctx("event.by"), "opponents"),
                        eq(ctx("event.kind"), "X")), "nt_doubled", forcing="none")
        self.transition("nt", "lebensohl_entry", all_of(eq(ctx("event.by"), "opponents"),
                        immediate_nt_interference, eq(ctx("event.kind"), "BID"), eq(ctx("event.level"), 2)), "nt_interference", {
            "enemy_suit": ctx("event.strain"),
        }, "none")
        game = choose(eq(var("nt_style"), "weak"), 12, 10)
        for suit in SUITS:
            self.rule("lebensohl.weak_two_" + suit, all_of(no(eq(var("enemy_suit"), suit)),
                ge(length(suit), 5), op("lt", hcp, game), legal("2" + suit)), fixed("2" + suit), "nt_interference")
        self.rule("lebensohl.slow", all_of(legal("2NT"), any_of(
            all_of(ge(hcp, game), ref("enemy_stopped")),
            all_of(op("lt", hcp, game), any_of(ge(length("C"), 5), ge(length("D"), 5))),
        )), fixed("2NT"), "nt_interference")
        self.transition("nt_interference", "relay", event("2NT"), "lebensohl_relay", forcing="one_round")
        self.rule("lebensohl.accept_relay", True, fixed("3C"), "lebensohl_relay")
        self.transition("lebensohl_relay", "accepted", event("3C"), "lebensohl_finish")
        for suit in SUITS:
            self.rule("lebensohl.game_suit_" + suit, all_of(no(eq(var("enemy_suit"), suit)),
                ge(hcp, game), ge(length(suit), 5), legal("3" + suit)), fixed("3" + suit), "nt_interference")
        self.rule("lebensohl.direct_cue", all_of(ge(hcp, game), any_of(ge(length("H"), 4), ge(length("S"), 4))),
                  cheapest({"op": "list", "item_type": "string", "args": [var("enemy_suit")]}, 3, 3), "nt_interference")
        self.rule("lebensohl.direct_nt", all_of(ge(hcp, game), ge(ref("total_min"), 25), legal("3NT")), fixed("3NT"), "nt_interference")
        self.rule("lebensohl.takeout", all_of(ge(hcp, op("sub", game, 2)), legal("X")), fixed("X"), "nt_interference")
        self.transition("nt_interference", "takeout_response", event("X"), "competition", {
            "res_min": op("sub", game, 2), "res_max": 40, "double_kind": "takeout",
        }, "one_round")
        self.rule("lebensohl.pass", True, fixed("P"), "nt_interference")
        self.rule("lebensohl.slow_game", all_of(ge(hcp, game), ge(ref("total_min"), 25), legal("3NT")), fixed("3NT"), "lebensohl_finish")
        self.rule("lebensohl.weak_diamonds", all_of(ge(length("D"), 5), legal("3D")), fixed("3D"), "lebensohl_finish")
        self.rule("lebensohl.weak_clubs", True, fixed("P"), "lebensohl_finish")
        for phase in ("nt_interference", "lebensohl_finish"):
            self.transition(phase, "game_nt", event("3NT"), "finished", forcing="none")
            self.transition(phase, "natural_suit", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural", {
                "response_suit": ctx("event.strain"), "res_min": choose(all_of(phase == "nt_interference",
                    eq(ctx("event.level"), 3), no(eq(ctx("event.strain"), var("enemy_suit")))), game, 0), "res_max": 40,
            })
        self.rule("nt_escape.redouble_clubs", all_of(legal("XX"), any_of(ge(length("C"), 6),
                  all_of(le(hcp, 4), ge(length("C"), 4)))), fixed("XX"), "nt_doubled")
        self.transition("nt_doubled", "redoubled", event("XX"), "nt_escape", {"transfer": "C"}, "one_round")
        for suit, call in (("S", "2H"), ("H", "2D"), ("D", "2C")):
            self.rule("nt_escape.transfer_" + suit, ge(length(suit), 5), fixed(call), "nt_doubled")
            self.transition("nt_doubled", "transfer_" + suit, event(call), "nt_escape", {"transfer": suit}, "one_round")
        self.rule("nt_escape.stand", True, fixed("P"), "nt_doubled")
        self.rule("nt_escape.accept", True,
                  cheapest({"op": "list", "item_type": "string", "args": [var("transfer")]}, 2, 3), "nt_escape")
        self.transition("nt_escape", "accepted", all_of(ours(), eq(ctx("event.kind"), "BID")), "nt_escape_finish")
        self.rule("nt_escape.finish", True, fixed("P"), "nt_escape_finish")

    def competition(self):
        hcp = ref("hcp")
        not_captain = eq(var("captain"), "")
        enemy_suit = ctx("opponent_bid_strain")
        first_intervention = all_of(not_captain, {"op": "has_contract_bid"})
        artificial_club = all_of(eq(ctx("opening_strain"), "C"), eq(ctx("last_bid"), "1C"),
                     {"op": "announced", "which": "opening", "tag": "strong_club"})
        natural_nt_opening = all_of(eq(ctx("opening_strain"), "NT"), eq(ctx("last_bid_level"), 1),
                       {"op": "announced", "which": "opening", "tag": "natural"})
        self.rule("defense.penalty_notrump_opening", all_of(first_intervention, ge(hcp, 18),
              eq(enemy_suit, "NT"), eq(ctx("last_bid_level"), 1),
              {"op": "announced", "which": "last_bid", "tag": "natural"}, legal("X")), fixed("X"))
        self.transition("idle", "penalty_notrump", all_of(event("X"), eq(ctx("last_bid_strain"), "NT"),
                eq(ctx("last_bid_level"), 1), {"op": "announced", "which": "last_bid", "tag": "natural"}),
                "penalty_defense", {"captain": ctx("event.actor"), "open_min": 18, "open_max": 40,
                            "opening_kind": "penalty", "double_kind": "penalty"})
        self.rule("defense.strong_club_double", all_of(first_intervention, eq(ctx("last_bid"), "1C"),
            {"op": "announced", "which": "opening", "tag": "strong_club"},
            ge(hcp, choose({"op": "vulnerable"}, 11, 8)), ge(length("H"), 5), ge(length("S"), 4), legal("X")), fixed("X"))
        self.rule("defense.nt_overcall", all_of(first_intervention, no(artificial_club), within(hcp, 15, 18),
            ref("nt_extended"), ref("enemy_stopped"), legal("1NT")), fixed("1NT"))
        self.rule("defense.takeout_double", all_of(first_intervention, ge(hcp, 12), legal("X"),
            no(artificial_club), no(eq(enemy_suit, "NT")), le(ctx("last_bid_level"), 3),
            any_of(ge(hcp, 18), all_of(no(eq(enemy_suit, "NT")),
                *(choose(eq(enemy_suit, suit), le(length(suit), 2), ge(length(suit), 3)) for suit in SUITS))),
        ), fixed("X"))
        for suit in SUITS:
            self.rule("defense.natural_overcall_" + suit, all_of(first_intervention,
                no(eq(enemy_suit, suit)), ge(hcp, 8), ge(length(suit), 5), legal("1" + suit)),
                fixed("1" + suit))
            self.rule("defense.two_level_overcall_" + suit, all_of(first_intervention,
                no(eq(enemy_suit, suit)), ge(hcp, 11), ge(length(suit), 5),
                no(natural_nt_opening) if suit == "C" else True,
                ref(f"good_{suit}"), legal("2" + suit)), fixed("2" + suit))
        self.rule("defense.natural_clubs_over_nt", all_of(first_intervention, natural_nt_opening,
                  ge(hcp, 8), ge(length("C"), 6), ref("good_C"), legal("3C")), fixed("3C"))
        self.rule("defense.no_qualifying_entry", first_intervention, fixed("P"))
        self.transition("idle", "takeout", event("X"), "competition", {
            "captain": ctx("event.actor"), "open_min": 12, "open_max": 40, "opening_kind": "double",
        })
        self.transition("idle", "nt_overcall", event("1NT"), "nt", {
            "captain": ctx("event.actor"), "open_min": 15, "open_max": 18, "nt_level": 1,
            "open_suit": "NT", "opening_kind": "nt",
            "nt_style": "strong",
        })
        self.transition("idle", "natural_overcall", all_of(ours(), eq(ctx("event.kind"), "BID")), "competition", {
            "captain": ctx("event.actor"), "open_suit": ctx("event.strain"),
            "open_min": 8, "open_max": 40, "opening_kind": "overcall",
        })
        self.rule("competition.redouble_strength", all_of(ge(hcp, 10), legal("XX"),
              eq(ctx("our_bid_count"), 1), eq(ctx("opener"), var("captain")),
              no(ref("am_captain")), eq(ctx("last_bid_by"), "ours"), eq(ctx("last_bid_level"), 1),
                  no(ref("have_fit")), op("in", ctx("opponent_double_kind"), ["takeout", "negative", "two_suited"])), fixed("XX"), "competition")
        self.transition("competition", "strength_redouble", event("XX"), "natural", {"res_min": 10, "double_kind": "strength"})
        self.rule("penalty_defense.pass", True, fixed("P"), "penalty_defense")
        # Advancing a takeout double chooses the longest unbid suit without
        # passing an artificial double merely because the captain showed none.
        for suit in SUITS:
            self.rule("competition.advance_double_" + suit, all_of(eq(var("opening_kind"), "double"),
                no(ref("am_captain")), no(eq(enemy_suit, suit)), longest(suit, minimum=4),
                can_bid_suit(suit)),
                cheapest([suit], 1, 7), "competition")
        for suit in ("H", "S", "D", "C"):
            self.rule("competition.support_" + suit, all_of(eq(var("open_suit"), suit), support(suit),
                ge(hcp, 6), no(ref("am_captain")), legal("2" + suit)), fixed("2" + suit), "competition")
        self.transition("competition", "advance", all_of(ours(), eq(ctx("event.kind"), "BID"),
                        no(eq(ctx("event.actor"), var("captain")))), "natural",
                        {"res_min": 0, "res_max": 40, "response_suit": ctx("event.strain")})
        self.rule("competition.no_constructive_first_response", all_of(no(ref("am_captain")),
            eq(ctx("our_bid_count"), choose(eq(var("opening_kind"), "double"), 0, 1)), no(ctx("must_bid"))), fixed("P"), "competition")

    def completion(self):
        for phase in ("major", "minor"):
            maximum = 7 if self.precision and phase == "minor" else 5
            self.rule(phase + ".no_constructive_reply", le(ref("hcp"), maximum), fixed("P"), phase)
            self.transition(phase, "no_constructive_reply", event("P"), "finished",
                            {"res_max": maximum}, "none", allow_pass=True)
        for level, goal in ((7, "grand_slam"), (6, "small_slam")):
            committed = eq(ctx("forcing"), goal) if level == 7 else op("in", ctx("forcing"), ["small_slam", "invite_grand"])
            self.rule(f"nt.complete_{level}NT", all_of(committed, legal(str(level) + "NT")), fixed(str(level) + "NT"), "nt")
        self.rule("nt.complete_game_force", all_of(eq(ctx("forcing"), "game"), legal("3NT")), fixed("3NT"), "nt")
        self.rule("nt.no_declared_response", no(ctx("must_bid")), fixed("P"), "nt")
        phases = list(self.raw["protocol"]["states"])
        for phase in phases:
            if phase in ("idle", "major", "minor", "nt", "transfer", "stayman", "puppet", "rkcb", "rkcb_choose",
                         "gerber", "gerber_finish", "shape_ask", "shape_answer", "weak_ask", "gambling_correct",
                         "strong_club", "strong_two", "strong_negative", "strong_positive",
                         "jacoby", "inverted", "opener_rebid", "checkback", "checkback_relay",
                         "checkback_invite", "checkback_force", "checkback_clubs", "checkback_club_finish",
                         "responder_rebid", "fourth_answer", "raised_major", "raise_answer",
                         "reverse_reply", "reverse_wait", "reverse_choose", "mini_ask", "mini_answer", "mini_choose",
                         "simple_raise", "help_answer", "drury_answer", "drury_finish", "bergen_answer", "penalty_defense"):
                continue
            reached = all_of(eq(ctx("last_bid_by"), "ours"), any_of(
                all_of(eq(ctx("last_bid_strain"), "NT"), ge(ctx("last_bid_level"), 3)),
                all_of(op("in", ctx("last_bid_strain"), ["H", "S"]), ge(ctx("last_bid_level"), 4)),
                all_of(op("in", ctx("last_bid_strain"), ["C", "D"]), ge(ctx("last_bid_level"), 5)),
            ))
            self.rule(phase + ".stop_completed", all_of(reached, no(ctx("must_bid"))), fixed("P"), phase)
            if phase == "finished":
                self.rule("finished.stop_settled", no(ctx("must_bid")), fixed("P"), phase)
            if phase == "quantitative":
                self.rule("quantitative.decline_slam", all_of(eq(ctx("goal"), "slam_try"), op("lt", ref("total_min"), 33)), fixed("P"), phase)
                self.transition(phase, "decline_slam", event("P"), "finished", forcing="none", allow_pass=True)
            self.rule(phase + ".decline_invitation", all_of(eq(ctx("forcing"), "invite_game"),
                      op("lt", ref("total_min"), 25)), fixed("P"), phase)
            for level, goal in ((7, "grand_slam"), (6, "small_slam")):
                committed = eq(ctx("forcing"), goal) if level == 7 else op("in", ctx("forcing"), ["small_slam", "invite_grand"])
                for suit_name in ("H", "S", "D", "C"):
                    self.rule(f"{phase}.committed_{level}{suit_name}", all_of(committed, ref("prefer_" + suit_name), legal(str(level) + suit_name)),
                              fixed(str(level) + suit_name), phase)
                self.rule(f"{phase}.committed_{level}NT", all_of(committed, legal(str(level) + "NT")), fixed(str(level) + "NT"), phase)
            for suit in ("H", "S", "D", "C"):
                game_level = 4 if suit in ("H", "S") else 5
                self.rule(phase + ".natural_game_" + suit,
                    all_of(ref("prefer_" + suit), any_of(eq(var("trump"), ""), eq(var("trump"), suit)),
                           any_of(ge(ref("total_min"), 25 if suit in ("H", "S") else 29),
                                               eq(ctx("forcing"), "game")),
                           legal(str(game_level) + suit)), fixed(str(game_level) + suit), phase)
            self.rule(phase + ".natural_nt_game", all_of(ge(ref("total_min"), 25), ref("enemy_stopped"), legal("3NT")), fixed("3NT"), phase)
            self.rule(phase + ".answer_completed_game", all_of(reached, eq(ctx("forcing"), "one_round")), fixed("P"), phase)
            self.transition(phase, "answer_completed_game", all_of(event("P"), reached, eq(ctx("forcing"), "one_round")),
                            "finished", forcing="none", allow_pass=True)
            for suit in ("H", "S"):
                self.rule(phase + ".natural_invite_" + suit, all_of(
                    ref("prefer_" + suit), eq(var("trump"), suit), eq(ctx("last_bid_by"), "ours"),
                    eq(ctx("last_bid_strain"), suit), le(ctx("last_bid_level"), 2), no(ctx("must_bid")), ref("game_invite_values"),
                    legal("3" + suit)), fixed("3" + suit), phase)
            self.rule(phase + ".natural_nt_invite", all_of(ref("game_invite_values"), ref("nt_extended"),
                      no(ctx("must_bid")), no(ref("have_fit")), eq(ctx("last_bid_by"), "ours"),
                      ref("enemy_stopped"), legal("2NT")), fixed("2NT"), phase)
            for suit in ("H", "S", "D", "C"):
                self.rule(phase + ".stop_partscore_" + suit, all_of(ref("prefer_" + suit), no(ctx("must_bid")),
                    eq(ctx("last_bid_by"), "ours"), eq(ctx("last_bid_strain"), suit)), fixed("P"), phase)
                self.rule(phase + ".choose_partscore_" + suit, all_of(ref("prefer_" + suit), no(ctx("must_bid")),
                          can_bid_suit(suit, 2, 3)), cheapest([suit], 2, 3), phase)
            self.rule(phase + ".natural_stop", no(ctx("must_bid")), fixed("P"), phase)
            self.rule(phase + ".forcing_nt", all_of(eq(ctx("forcing"), "game"), ref("enemy_stopped"), legal("3NT")), fixed("3NT"), phase)
            for suit in ("H", "S", "D", "C"):
                self.rule(phase + ".one_round_fit_" + suit, all_of(op("in", ctx("forcing"), ["one_round", "slam_try"]),
                          ref("prefer_" + suit), can_bid_suit(suit, 2, 3)), cheapest([suit], 2, 3), phase)
            for suit in SUITS:
                game_level = 4 if suit in ("H", "S") else 5
                self.rule(phase + ".forcing_solo_game_" + suit, all_of(eq(ctx("forcing"), "game"),
                          any_of(eq(var("trump"), ""), eq(var("trump"), suit)),
                          longest(suit, minimum=5), legal(str(game_level) + suit)), fixed(str(game_level) + suit), phase)
                self.rule(phase + ".forcing_one_round_" + suit, all_of(
                    op("in", ctx("forcing"), ["one_round", "slam_try"]), longest(suit, minimum=4), can_bid_suit(suit, 1, 3)),
                    cheapest([suit], 1, 3), phase)
            self.rule(phase + ".defend_above_goal", all_of(ctx("must_bid"), eq(ctx("last_bid_by"), "opponents"),
                      ge(ctx("last_bid_level"), ctx("goal_level"))), fixed("P"), phase)
            self.transition(phase, "defend_above_goal", all_of(event("P"), eq(ctx("last_bid_by"), "opponents")),
                            "finished", forcing="none", allow_pass=True)
            for level, goal in ((7, "grand_slam"), (6, "small_slam")):
                self.transition(phase, "reached_" + goal, all_of(ours(), eq(ctx("event.kind"), "BID"), eq(ctx("event.level"), level)),
                                "finished", forcing=goal)
            own_game = all_of(ours(), eq(ctx("event.kind"), "BID"), any_of(
                all_of(eq(ctx("event.strain"), "NT"), ge(ctx("event.level"), 3)),
                all_of(op("in", ctx("event.strain"), ["H", "S"]), ge(ctx("event.level"), 4)),
                all_of(op("in", ctx("event.strain"), ["C", "D"]), ge(ctx("event.level"), 5)),
            ), no(op("in", ctx("forcing"), ["small_slam", "invite_grand", "grand_slam"])))
            self.transition(phase, "own_game_reached", own_game, "finished", forcing="game")
            for suit in ("H", "S"):
                self.transition(phase, "major_invitation_" + suit, all_of(event("3" + suit), eq(var("trump"), suit),
                    eq(ctx("last_bid_by"), "ours"), eq(ctx("last_bid_strain"), suit), le(ctx("last_bid_level"), 2),
                    op("in", ctx("forcing"), ["none", "invite_game"])), "natural",
                    self.game_invitation_updates(), forcing="invite_game")
            self.transition(phase, "nt_invitation", all_of(event("2NT"),
                            eq(ctx("last_bid_by"), "ours"), op("in", ctx("forcing"), ["none", "invite_game"])),
                            "quantitative", {"invite_target": "game", **self.game_invitation_updates()}, "invite_game")
            self.transition(phase, "continue", all_of(ours(), eq(ctx("event.kind"), "BID")), "natural")


def _state_comparison(expression, name):
    if isinstance(expression, dict):
        if expression.get("op") == "eq" and expression.get("args") == [{"op": "state"}, name]:
            return True
        return any(_state_comparison(value, name) for value in expression.values())
    if isinstance(expression, list):
        return any(_state_comparison(value, name) for value in expression)
    return False


def build_classic(kind: str) -> SystemDefinition:
    if kind not in SOURCE_HASHES:
        raise ValueError("Classic kind must be ccba or prec.")
    return Compiler(kind).compile()


def translation_manifest(systems: dict[str, SystemDefinition], sources: Path | None) -> dict:
    definitions = (
        ("opening_and_response", {"ccba": [(83, 127), (529, 558)], "prec": [(95, 143), (585, 609), (900, 930)]},
         ("open.", "major.", "minor.", "strong_club.", "strong_two."), "test_manual_opening_families_and_strong_club_exception"),
        ("checkback_xyz", {"ccba": [(128, 140), (182, 194), (576, 609)], "prec": [(631, 662), (948, 968)]},
         ("checkback.",), "test_two_way_checkback_invitation_is_a_partnership_relay"),
        ("fourth_suit_and_reverses", {"ccba": [(196, 307)], "prec": [(664, 790)]},
         ("fourth.", "reverse.", "responder_rebid."), "test_fourth_suit_game_force_is_not_a_natural_four_card_suit"),
        ("fit_showing", {"ccba": [(717, 773)], "prec": [(1089, 1146)]},
         ("fit.", "mini.", "help.", "drury.", "bergen.", "jacoby.", "raise_inquiry."), "test_mini_splinter_sends_shortness_only_after_the_public_question"),
        ("precision_shape_relays", {"prec": [(243, 415), (1565, 1693)]},
         ("precision.", "support.", "shape."), "test_precision_point_shape_relay_preserves_two_four_card_suits"),
        ("notrump", {"ccba": [(909, 1030), (1232, 1322)], "prec": [(1305, 1420), (1760, 1845)]},
         ("nt.", "two_nt.", "smolen.", "minor_stayman.", "transfer.", "puppet.", "stayman."), "test_smolen_keeps_the_five_card_major_with_the_nt_opener"),
        ("notrump_interference", {"ccba": [(1031, 1162)], "prec": [(1421, 1563)]},
         ("lebensohl.", "nt_escape.", "landy."), "test_lebensohl_weak_diamonds_and_stopped_game_use_the_forced_club_relay"),
        ("slam", {"ccba": [(1342, 1372)], "prec": [(1861, 1891)]},
         ("slam.", "slam_controls.", "queen.", "kings.", "exclusion.", "gerber."), "test_rkcb_queen_question_and_side_king_answer_are_public_continuations"),
        ("competitive", {"ccba": [(1373, 1430)], "prec": [(417, 579), (1892, 1951)]},
         ("defense.", "marshall.", "competition.", "negative.", "two_suit_"), "test_michaels_and_unusual_nt_have_distinct_public_two_suit_meanings"),
    )
    originals = {kind: read_manual(sources / f"{kind}.docx") for kind in systems} if sources else {}
    modules = []
    for name, ranges, prefixes, scenario in definitions:
        modules.append({
            "module": name, "source_ranges": ranges,
            "source_clauses": {kind: [{"paragraph": number, "text": text} for number, text in originals.get(kind, ())
                                     if text and any(low <= number <= high for low, high in spans)]
                               for kind, spans in ranges.items()},
            "rule_ids": {kind: [rule["id"] for rule in system.to_data()["rules"]
                                if rule["id"].split(".", 1)[1].startswith(prefixes)] for kind, system in systems.items()},
            "scenario_test": "tests/test_classics.py::ClassicTranslationTests::" + scenario,
            "status": "executable-with-explicit-engineering-decisions",
        })
    return {
        "schema_version": 1, "profile_version": PROFILE_VERSION,
        "source_sha256": SOURCE_HASHES, "source_documents_verified": sources is not None,
        "source_paragraph_counts": {kind: len(records) for kind, records in originals.items()},
        "mapping_scope": "章节/协议与代表性场景映射；不等于每条原文的形式等价证明。JSON IR是全部实际决策语义。",
        "partnership_state": {
            "support_inquiries": "精确1C-1NT后的2D/H/S/NT支持问叫，按当前发送规则公开问叫花色5+；六级答档同时公开点力档、支持至少Qxx（含Axx/Kxx）或xxxx、三张下界及已定将牌。无支持档保存否定及至多三张；答叫字面花色不是自然长套。",
            "support_private_refinement": "支持Qxx-or-better/xxxx是公开的并集保证；仅当本人持该套AKQ三张大牌时，才能在本人私有推断中排除三张带大牌分支，推得搭档至少四张；不将这一私有结论写回公共账本。",
            "slam_controls": "试探看联手可能上界而非30点HCP下界：上界32+，或有配合牌型价值且上界29+；本人控制值至少3，并比自己公开下界多2点、或有九张配合单缺/六张好将牌/无浪费短套控制等价值。未知40上界本身不够触发。逼局后八张私有可保证配合允许低位回原花色定将并保留探索空间；新花控制不保证自然长套，绝不使用DDS。",
            "slam_safety": "优先确认边花控制，再进入关键张；高花至少两关键张，方片至少三、草花至少四且有将牌Q。红桃/方片若最高答档会越过五阶退路，须本人有将牌Q以排除该答档，或已具备小满贯配合价值；草花和高阶排除问叫也要求有可接受六阶的价值。黑桃允许先问后安全停5S，不能把可能性试探逼成六阶。七张以上AKQ独立套要求扣叫，既有Q/K后续保留。",
            "trump_reselection": "未真正形成八张配合时，逼局方可低位显示另一门八张配合并重定将牌；控制叫前先公开确认当前八张配合，避免自然低花被误解成旧高花的控制/排除问叫。已定另一个将牌时通用终局不能抢用其5C/5D等排除问叫保留叫品；换将要先明确公开，而不是暗中改变解释。",
            "fit_encoding": "开叫人自然再叫与配合探索共享叫品时，必须取共同保证：新套/加叫同伴花色通常仅保证四张，原套重叫保证五张；伙伴公开下界不足以合成八张时，不把同一叫品解码为已确认八张配合。人工控制叫不附加自然长套保证。精确强阳后的三阶坚固七张套邀扣只在二阶同花色仍合法时成立；应叫2S抬高叫阶后，被迫的3H等不是七张坚固套编码。",
            "slam_commitment": "普通RKCB后至少四关键张和将牌Q，且私有配合评价达到30才选择小满贯；配合评价=本人HCP+搭档公开下界，加本人最长短套的缺门3/单张2与最多2个超过八张的将牌，减本人落在搭档公开单缺的K/Q/J点。这个工程评价不写入公共HCP，也不冒称50%成功率模型；大满贯仍保留全部关键张/Q及37点等原专用门槛。",
            "game_invitation": "自然邀请要求联手上界可达25，且下界23+、或本人比公开下界多2点并满足有限区间中值23/搭档公开11+且本人10+及3控制值。未知40不直接取中值。非低限分支可在下界21等条件邀请；邀请的公共下界取所有发送分支的共同保证，禁止写成已知联手25。三阶高花邀请传递邀局义务和共同点力保证；未定将的自然NT也可在花色重叫后用2NT邀请。接受方仍可低限Pass，不能凭上界单独强迫进局。",
            "late_strong_club_double": "强梅花的低点力特殊双高加倍仅紧接最后一个1C叫品；对方已负应叫或有后续后，不能仅凭最初强梅花标签输出8点X却被公开解释成12点普通技术X。第一介入和公开解释使用相同最近叫品条件。",
            "nt_slam_invitation": "有限NT范围以可能上界33+、区间中值31+且本人比公开下界多2点为定量邀请；未知上界还须本人12+、4控制值、已知下界28+。问叫/邀请只公开可保证的33减搭档上界等共同下界，不虚报31点联手保证。接受6NT/7NT保留33/37点下界门槛。",
            "quantitative_before_gerber": "均型NT试探在联手下界不足33、但上界可达33时走4NT定量邀贯，不因本人没有草花A而优先进入Gerber。Gerber入口须联手下界33+，用于已有小满贯点力时核查A；其后的4NT退路仍是止叫，不误改成新邀请。定量4NT后的开叫人用本人HCP加搭档公开下界判断33/37点接受门槛，低限可以拒绝。",
            "artificial_game_boundary": "已公开标记为人工的Splinter/控制叫，不能仅按字面3NT/4H等清除逼局目标。历史冻结IR不补入新的标签；新的公开标签与发送、解释一起冻结。",
            "first_response_boundary": "高花/低花开叫的第一应叫不使用通用自然兜底去占用二盖一等保留叫品。没有符合已声明应叫/弱跳/加叫条件时，CCBA及高花开叫的0-5点、精确低花开叫的0-7点明确Pass，并公开对应点力上界；不把5点八张草花兜底为承诺13点的2C。",
            "first_response_exits": "低花开叫后的弱跳新花明确为4-6点六张套并退出第一应叫阶段；阻击加叫记录原低花五张支持并退出。不能让开叫人下一轮被误当成应叫人，再输出已不合法的一阶高花。",
            "marshall_context": "马歇尔双套只解释仍紧接强1C的干扰；对方已经作出1D负应叫等后，不能仍凭最早的强梅花标签把后来的2NT错解成双低花。公开解释与实际发送使用相同的最近叫牌条件。",
            "public_bounds": "分别记录captain/responder的HCP与C/D/H/S张数下界，取max单调更新；只来自公开叫品的声明，不读取命中私有规则或搭档手牌。",
            "natural_contracts": "自然叫品须有实际套长保证；自然强开叫再叫保守保证4张，原套自然重叫保守保证5张，均取同一公开叫品全部发送分支的共同保证，不能从私有最长套/六张套分支写入5/6。支持、牌型接力、无将和竞争叫品的保证投影到同一套长账本。",
            "fit_contracts": "转移/斯台曼后的自然邀局和成局支持必须由本人实长加搭档公开下界保证至少8张；支持后确认将牌，并补足由公开历史可推得的本人支持下界。人工问答不按字面套长解释。",
            "hcp_intervals": "HCP同时记录上下界；下界取max、上界取min，明确拒绝空区间。每个座位的开叫前Pass上界独立保存，角色确定后与开叫/应叫范围取交集。",
            "pass_limits": "仅限全桌尚未开叫时的Pass：精确11+HCP的开叫分支覆盖全部牌型，故Pass最多10；CCBA12+覆盖全部牌型，故最多11。高花可11点、第三家可9点轻开，不反推所有11点牌都Pass；竞争/埋伏Pass不自动限点。",
            "joint_bounds": "partnership_hcp_min及partnership_C/D/H/S_min为双方已声明下界之和；套长上界取双方上界之和且最多13。agreed_trump_min/max为已选将牌的联手区间。",
            "suit_intervals": "每手四套min/max及套间不少于关系按总张数13传播，公开过程不读取任何手牌；空牌型域、区间放宽或遗忘关系均报错。1D开叫排除5张高花；精确1D还排除6张草花。NT、弱二、斯台曼否定答叫和单缺显示保留各自可证明的界。",
            "suit_relations": "captain/responder_<长套>_ge_<短套>为公开保证。自然1S保证S不少于H，自然1H保证H不少于S；CCBA低花开叫保存自然低花长短关系，精确短方片不冒称D长于C。",
            "after_call_trace": "新账本另存public_context_after，显示本叫品解释及传播后的公开区间；private_trace.inference仍是决策前本人估计。旧冻结记录不回填。",
            "joint_hcp_max": "联手HCP上界为双方角色上界与对应座位Pass上界取交集后的和，最多40；未有非平凡上界时用40，不把区间上端当成已知实力。",
            "private_inference": "本人实际HCP加搭档公开HCP区间，再与公开联手区间相交；套长使用本人实际长度加搭档公开下界。这些估计只存在本人策略及私有trace，不共享给搭档。",
            "natural_nt_gate": "无额外明示约定时，自然3NT至少需要联手点力下界25；竞争分支不能只因应叫人13点就进局。精确2C无配合后续和莱本索尔自然3NT同样检查联手下界。区间上界可用于可能性判断，不能单独触发进局。人工3NT问答和已声明逼局单独处理。",
            "goals": ["none", "one_round", "invite_game", "game", "slam_try", "small_slam", "invite_grand", "grand_slam"],
            "goal_semantics": "邀请可拒绝；one_round与slam_try要求下一次回答但不承诺六阶；game要求成局、small_slam六阶、invite_grand至少六阶、grand_slam七阶。goal记录目标，forcing记录当前义务，goal_met表示是否已到达。",
            "interference": "普通干扰不清除配合、下界或未完成目标；一轮回答和较低声明不撤销更高承诺。空间已被敌方越过且无法完成时，使用显式转防守规则。",
            "notrump_interference": "莱本索尔和无将被加倍后的应叫人逃叫协议，只接受紧接captain的NT叫品之后的敌方干扰；应叫人已经Pass后由另一敌家晚轮介入时，进入普通竞争，不把captain续叫错记为responder的点力。",
            "notrump_takeout_reply": "无将受干扰后技术X：强无将应叫人8+、弱无将10+；明确要求captain回答一轮并转普通竞争。captain自然新套答叫保证4张，或已有八张配合时保证补足八张的共同长度，不把captain叫品解释为responder点力。",
            "game_force_new_suit": "已经二盖一逼局后，开叫人可自然再叫实际4+张的新套，不再要求非逼局逆叫的额外HCP；这种再叫只表达套长，不自动把原公开点力下界提高到逆叫门槛。未逼局时原逆叫条件保留。",
            "completion": "专用协议优先；逼局先保留低位配合及满贯探索空间，邀请按区间可能性和自身价值，承诺成局/满贯按各自验收条件。低限可拒绝，不从另一手牌或DDS获知结果。",
            "doubles": "否定性X限开叫后的首次应叫，1/2/3阶至少6/8/10HCP（强梅花示弱X为5+）；实力XX限一阶开叫刚遭技术/否定/双套X、10+且尚未定配合，不对已选高阶定约盲目XX。自然1NT开叫的惩罚X为18+；不对人工NT或任意高阶NT凭16点加倍。",
            "audit": "逐步牌后检查公开下界<=真实HCP<=公开上界、私有区间包含真实联手HCP；下界不倒退、上界不放宽，Pass座位上界也核对真实性。套长下界不超过实际手牌。完整手牌仅由牌后审计读取。",
        },
        "modules": modules,
        "assumptions": {
            "limits": "逆叫门槛CCBA16+、精确14+；加叫2NT问叫高限14+；弱二高限9-10；Jacoby高限16+，中限14-15或4控制。",
            "invitation": "无明示边界时，以本人HCP加搭档公开点力下界达到23作为邀请依据；成局门槛高花/NT25、低花29。已发邀请可接受或拒绝，不把邀请当逼局。",
            "good_suit": "至少5张且AKQ中至少两张；半坚固采用同一保守口径；坚固套至少6张且含AKQ。",
            "stopper": "A，或Kx，或Qxx，或JTxx及以上；未定义的半止张不当作完整止张。",
            "help_suit": "帮张为边花3-4张且AKQ至多一张；应叫方9+HCP、有单缺或该花色AKQ两张时接受，否则退回三阶将牌。",
            "mini_splinter": "加一级问单缺；边花依C/D/H/S顺序加级，跳过将牌；多门短套取首门。",
            "exclusion_conflict": "正文遗漏1.5关键张档且与附录不一致；采用附录五档0-0.5/1/1.5/2/2.5，依次加1-5级；3+直接大满贯。缺门A不计，Q计半张。",
            "slam_ambiguity": "0/3、1/4均先使用下界，不从搭档私牌推断高档；问K需已知所有关键张及将牌Q，35+点才继续，37+点才按点力选择大满贯。",
            "unusual_nt": "未展开的特殊2NT按两个最低未叫花色各5张、12+HCP实现；Landy为54+双高花，无局11+/有局12+。",
            "residual_continuations": "未逐条展开的多轮干扰、控制扣叫和终局选择，使用IR中明示的自然竞争/继续/停止规则；不声称手册逐条无损移植。",
            "strong_four_card_rebid": "prec.docx第108段的四张例外仅用于1C-1D示弱后的自然再叫；第321/333/335段中，1C-2C/2D后的自然新套须5+，不能泛化该例外。16-18点4441且短套为同伴低花时按第325/338段用3NT显示牌型并保留搭档选高花的后续；其它无五张新套、无四张支持的正应叫进程用2NT等待，不冒称22-24点或均型。ccba.docx第1179段2C-2D后的自然套仍允许真实四张，不叫单张。",
            "natural_repeat_fallback": "没有更具体可执行后续时，只允许重叫实际5+张的原套；5431等弱牌未必能逆叫，故同一原套重叫的公共保证是5而非命中六张套分支时的6。首次竞争应叫未满足约定条件且无逼叫义务时明确Pass，不经通用兜底冒充自然正应叫。",
        },
        "excluded": ["首攻、打牌信号、裁判纠正和赛事体系准入；本阶段只执行叫牌。"],
    }


def export_classics(directory: Path, *, sources: Path | None = None, compact: bool = False) -> dict[str, str]:
    if sources is not None:
        for kind, expected in SOURCE_HASHES.items():
            path = sources / f"{kind}.docx"
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Source document changed: {path}; review the migration before rebuilding.")
    directory.mkdir(parents=True, exist_ok=not compact)
    hashes = {}
    definitions = {}
    original_hashes = {}
    sharing = {}
    for kind in SOURCE_HASHES:
        system = build_classic(kind)
        original_hashes[kind] = system.content_hash
        if compact:
            from bridge_evolve.systems.compact import compact_system, format_system
            original_features = len(system.features)
            system = compact_system(system)
            sharing[kind] = len(system.features) - original_features
        definitions[kind] = system
        (directory / f"{kind}.json").write_text(
            format_system(system) if compact else json.dumps(system.to_data(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf8",
        )
        (directory / f"{kind}.md").write_text(render_system(system), encoding="utf8")
        hashes[kind] = system.content_hash
    translated = translation_manifest(definitions, sources)
    translation_path = directory / "translation.json"
    translation_path.write_text(json.dumps(translated, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    migration = {
        "schema_version": 2, "profile_version": PROFILE_VERSION,
        "status": "executable-manual-protocols-with-explicit-assumptions-not-formally-equivalent",
        "source_sha256": SOURCE_HASHES, "source_documents_verified": sources is not None,
        "source_versions": {"ccba": "2.2 (2019-10)", "prec": "1.1.1 (2021-01)"},
        "systems": hashes,
        "translation_manifest": translation_path.name,
        "translation_sha256": hashlib.sha256(translation_path.read_bytes()).hexdigest(),
        "precedence": "正文优先于附录；具体协议优先于通用自然续叫；同一条件重叠按导出的IR优先级。",
        "implemented": [
            "CCBA自然低花/五张高花，正文11点高花和第三家轻开；精确强梅花/短方片/2C/2D开叫",
            "CCBA15-17与精确13-15无将及不同应叫；强梅花示弱后强无将使用自然转移而非弱无将约定",
            "第一应叫、Walsh高花优先、二盖一、反加叫、Bergen、Jacoby、passed-hand Drury",
            "强梅花示弱/自然正应叫/4441问点力、1C-1NT的六级支持问叫与点力询问",
            "Stayman、Jacoby/Texas高花转移、低花转移、2NT傀儡高花询问",
            "弱二Ogust、赌博3NT修正、自然邀局/选局、定量无将满贯",
            "双路重询、精确XYZ、第四花色逼局、逆叫弱牌出口与加叫后2NT询问",
            "Mini-Splinter/Splinter、Bergen、Jacoby高低限、帮张邀请和逆朱瑞回答",
            "精确4333与44牌型接力、2C-2D及2D-3D问叫、Smolen和低花斯台曼",
            "Gerber、1430关键张、后续问Q/问K、缺门关键张及明确满贯/退路",
            "Michaels、特殊2NT、Landy、马歇、跳迈克尔斯、莱本索尔和被加倍后的逃叫",
            "自然争叫、技术性/否定性加倍及应叫、无将争叫、实力再加倍",
        ],
        "compressed_or_replaced": [
            "罕见的多轮干扰、部分控制扣叫和未展开的终局选择使用明示自然续叫，不声称逐条形式等价",
            "原文模糊词和缺门关键张冲突采用translation.json中的暂定、可复现评估口径",
            "首攻和打牌信号完全排除在叫牌实验之外",
        ],
        "deterministic_definitions": {
            "good_suit": "至少5张且AKQ中至少两张；坚固套至少6张且含AKQ。",
            "stopper": "A，或Kx及以上，或Qxx及以上，或JTxx及以上。",
            "playing_tricks2": "半墩单位：AKQ计2×套长；AK计4+max(长度-4,0)；AQ计3+max(长度-4,0)；单A计2；Kx及以上计1；其余0。各套相加，不是DDS。",
            "natural_targets": "承诺高花/3NT仍用25点下界、低花29；邀局改看可达25的上界与23中值/本人额外价值，逼局先保留已知配合和满贯试探空间。详细公式见translation.json与IR。",
            "partnership_bounds": "公开账本单调保留双方HCP和各套长度下界；人工花色不等于所持花色；联手下界和当前牌手的私有估计分开记录。",
            "calling_goals": "none/one_round/invite_game/game/slam_try/small_slam/invite_grand/grand_slam；高承诺不会被一轮回答或较低声明清除。",
            "partner_ranges": "只由公开叫品/阶段确定，不读取搭档私牌；不确定的0/3、1/4关键张以较低值处理。",
            "hcp_upper_bounds": "开叫/应叫上界与开叫前Pass上界取交集；没有保证的上限保持未知40。本人私有估计记录上下界，不把上界或区间中值当成保证可成局。",
            "suit_ties": "普通等长依S,H,D,C；4-4高花首次应叫优先H，5-5优先S。",
            "fallback": "根兜底为error；各阶段停止均为明示自然停止规则，不把非法或未定义动作修成Pass。",
        },
        "interpretation": "成绩仅适用于这些带明示推断的可执行翻译版本；不代表原版人类搭档实力，也未证明逐条无损移植。",
        "validation_history": {
            "prior_attempt": "results/ccba-prec-1000-v1/run.json",
            "status": "invalid run stopped at board 608; no ranking or summary was published",
            "correction": "Gambling 3NT pass-or-correct now has a dedicated 4C/4D protocol instead of the generic low-level rebid path.",
            "data_policy": "The original 1000-board test is consumed for debugging; final conclusions require a new 1000-board holdout.",
        },
    }
    if compact:
        migration["representation"] = {
            "kind": "shared-expressions-v1", "language": "existing-v2-features-ref",
            "source_systems": original_hashes, "added_features": sharing,
            "verification": "Expanding the added references restores the exact original system data.",
            "layout": "two-space structural indentation; short expressions inline",
        }
    (directory / "migration.json").write_text(json.dumps(migration, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return hashes
