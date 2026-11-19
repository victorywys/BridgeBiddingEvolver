"""Finite, executable module contracts over the unchanged general v2 engine."""

from __future__ import annotations

import copy
import json
import logging
import random
from collections import Counter
from pathlib import Path

from bridge_evolve.core import Deal, Hand, Seat, Vulnerability, full_deck
from bridge_evolve.evaluation.data import board_data, canonical_hash, family_hash
from bridge_evolve.evolution.feedback import short_text
from bridge_evolve.systems import SystemError, load_system, validate_system
from bridge_evolve.systems.inputs import Board, parse_board, run_auction
from bridge_evolve.systems.navigation import PublicFilter, system_index
from bridge_evolve.systems.runtime import hand_hcp, hand_shape

LOGGER = logging.getLogger(__name__)
PREFIX = "nt15."
ACTIVE = PREFIX + "active"
SCHEMA = "bridge-module-contract/v1"
BANDS = ((0, 6), (7, 7), (8, 8), (9, 9), (10, 10), (11, 15), (16, 25))
SHAPES = ("balanced", "major_H", "major_S", "both_majors", "long_C", "long_D")


def _guard(states):
    return {"op": "in", "args": [{"op": "state"}, sorted(states)]}


def _state_literals(value, mapping):
    if isinstance(value, dict):
        result = {key: _state_literals(child, mapping) for key, child in value.items()}
        args = result.get("args", [])
        if result.get("op") in ("eq", "ne", "in") and len(args) == 2:
            for position in (0, 1):
                if args[position] in ({"op": "state"}, {"op": "context", "name": "state"}):
                    other = 1 - position
                    literal = args[other]
                    if isinstance(literal, str):
                        args[other] = mapping.get(literal, literal)
                    elif isinstance(literal, list):
                        args[other] = [mapping.get(item, item) if isinstance(item, str) else item for item in literal]
        if result.get("op") == "ref":
            result["name"] = PREFIX + result["name"]
        return result
    if isinstance(value, list):
        return [_state_literals(child, mapping) for child in value]
    return value


def isolate_nt(system):
    """Route only natural 1NT openings to an equivalent, isolated state copy."""
    if system.version != 2 or system.initial != "idle":
        raise SystemError("The NT seed requires a general v2 system with an idle opening state.")
    raw = system.to_data()
    if any(name.startswith(PREFIX) for name in raw["features"]) or any(
            name.startswith(PREFIX) for name in raw["protocol"]["states"]):
        raise SystemError("The source already uses the NT module namespace.")
    states = raw["protocol"]["states"]
    if "nt" not in states or not {"finished", "outside"} <= states.keys():
        raise SystemError("The seed must declare nt, finished and outside protocol states.")
    internal, pending = set(), ["nt"]
    while pending:
        name = pending.pop()
        if name in internal or name in ("finished", "outside"):
            continue
        internal.add(name)
        pending.extend(item["to"] for item in states[name]["transitions"])
    mapping = {name: PREFIX + name for name in internal}
    result = copy.deepcopy(raw)
    result["name"] = system.name + " · isolated natural 1NT research anchor"
    result["features"].update({PREFIX + name: _state_literals(value, mapping) for name, value in raw["features"].items()})
    for name in sorted(internal):
        node = _state_literals(states[name], mapping)
        node["otherwise"] = mapping.get(node["otherwise"], node["otherwise"])
        for transition in node["transitions"]:
            transition["id"] = PREFIX + transition["id"]
            transition["to"] = mapping.get(transition["to"], transition["to"])
        result["protocol"]["states"][mapping[name]] = node
    module_states = set(mapping.values())
    for rule, compiled in zip(raw["rules"], system.rules, strict=True):
        if any(PublicFilter(name).value(compiled.when) is not False for name in internal):
            clone = _state_literals(rule, mapping)
            clone["id"] = PREFIX + rule["id"]
            clone["when"] = {"op": "and", "args": [_guard(module_states), clone["when"]]}
            result["rules"].append(clone)
    for announcement, compiled in zip(raw["announcements"], system.announcements, strict=True):
        if any(PublicFilter(name).value(compiled.when) is not False for name in internal):
            clone = _state_literals(announcement, mapping)
            clone["id"] = PREFIX + announcement["id"]
            clone["when"] = {"op": "and", "args": [_guard(module_states), clone["when"]]}
            result["announcements"].append(clone)
    for collection in ("rules", "announcements"):
        for item in result[collection][:len(raw[collection])]:
            item["when"] = {"op": "and", "args": [
                {"op": "not", "args": [{"op": "var", "name": ACTIVE}]}, item["when"]]}
    opening = next((item for item in result["protocol"]["states"]["idle"]["transitions"] if item["id"] == "open_nt"), None)
    if opening is None:
        raise SystemError("The seed needs an explicit open_nt transition; an arbitrary opening is not inferred.")
    routed = copy.deepcopy(opening)
    routed.update(id="open_isolated_1nt", to=PREFIX + "nt")
    routed["when"] = {"op": "and", "args": [opening["when"],
        {"op": "eq", "args": [{"op": "context", "name": "event.call"}, "1NT"]}]}
    opening["when"] = {"op": "and", "args": [opening["when"],
        {"op": "ne", "args": [{"op": "context", "name": "event.call"}, "1NT"]}]}
    result["protocol"]["states"]["idle"]["transitions"].append(routed)
    parameters = result["protocol"]["parameters"]
    parameters[ACTIVE] = {"type": "bool", "initial": False}
    parameters["captain"] = {"type": "string", "initial": "", "values": ["", "N", "E", "S", "W"]}
    for owner in ("captain", "responder"):
        parameters[owner + "_hcp_min"] = {"type": "number", "initial": 0}
        parameters[owner + "_hcp_max"] = {"type": "number", "initial": 40}
        for suit in "SHDC":
            parameters[owner + "_" + suit + "_min"] = {"type": "number", "initial": 0}
            parameters[owner + "_" + suit + "_max"] = {"type": "number", "initial": 13}
    routed["set"].update(captain={"op": "context", "name": "event.actor"},
                         captain_hcp_min=15, captain_hcp_max=17)
    routed["set"][ACTIVE] = True
    for state_name in module_states:
        for transition in result["protocol"]["states"][state_name]["transitions"]:
            if transition["to"] in ("finished", "outside"):
                transition["set"][ACTIVE] = False
    for suit in "SHDC":
        routed["set"]["captain_" + suit + "_min"] = 2
        routed["set"]["captain_" + suit + "_max"] = 5 if suit in "SH" else 6
    result["rules"].append({"id": PREFIX + "explicit_nonforcing_exit", "priority": -100,
        "when": {"op": "and", "args": [_guard(module_states), {"op": "not", "args": [{"op": "context", "name": "must_bid"}]}]},
        "action": {"kind": "call", "call": "P"}})
    return validate_system(result)


def _shape_class(hand, category):
    lengths = {suit: sum(card.suit == suit for card in hand.cards) for suit in "SHDC"}
    if category == "balanced":
        return hand_shape(hand) in ((4, 3, 3, 3), (4, 4, 3, 2), (5, 3, 3, 2))
    if category == "both_majors":
        return min(lengths["S"], lengths["H"]) >= 4
    return lengths[category[-1]] >= (6 if category.startswith("long_") else 5)


def nt_probe_cases(seed=618203):
    missing = {(low, high, shape) for low, high in BANDS for shape in SHAPES}
    rng, cases, families = random.Random(seed), [], set()
    attempts = 0
    while missing and attempts < 200000:
        attempts += 1
        cards = list(full_deck())
        rng.shuffle(cards)
        hands = [Hand(tuple(cards[index:index + 13])) for index in (0, 13, 26, 39)]
        if not 15 <= hand_hcp(hands[0]) <= 17 or hand_shape(hands[0]) not in ((4, 3, 3, 3), (4, 4, 3, 2), (5, 3, 3, 2)):
            continue
        match = next((key for key in sorted(missing) if key[0] <= hand_hcp(hands[2]) <= key[1]
                      and _shape_class(hands[2], key[2])), None)
        if match is None:
            continue
        deal = Deal(*hands)
        family = family_hash(deal)
        if family in families:
            continue
        families.add(family)
        missing.remove(match)
        rotation = len(cases) % 4
        rotated = hands[-rotation:] + hands[:-rotation] if rotation else hands
        board = Board(Deal(*rotated), list(Seat)[rotation], list(Vulnerability)[len(cases) % 4])
        cases.append({"id": f"hcp{match[0]}-{match[1]}-{match[2]}",
                      "board": board_data(board), "responder_hcp_band": list(match[:2]), "shape_class": match[2],
                      "require_game": match[0] >= 10, "source": "stratified consumed diagnostic probe"})
    if missing:
        raise SystemError(f"Contract probe generation exhausted {attempts} attempts; missing groups: {sorted(missing)}")
    return cases, attempts


def build_nt_contract(source: Path, output: Path):
    original = load_system(source)
    anchor = isolate_nt(original)
    cases, attempts = nt_probe_cases()
    output.mkdir(parents=True, exist_ok=False)
    (output / "anchor.json").write_text(json.dumps(anchor.to_data(), ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    contract = {"schema": SCHEMA, "name": "Natural 15-17 1NT complete continuation pilot",
        "anchor": "anchor.json", "anchor_hash": anchor.content_hash, "source_system_hash": original.content_hash,
        "state_prefix": PREFIX, "root": PREFIX + "nt", "exits": ["finished", "outside"],
        "entry": {"call": "1NT", "opener_hcp": [15, 17], "shapes": ["4333", "4432", "5332"]},
        "cases": cases, "generator_seed": 618203, "generation_attempts": attempts,
        "coverage_scope": "42 finite HCP/shape strata, including 7/8/9/10 boundaries; not exhaustive hand coverage or independent fitness evidence"}
    target = output / "contract.json"
    target.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return target


class ModuleContract:
    def __init__(self, data, anchor):
        if (not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("anchor_hash") != anchor.content_hash
                or data.get("state_prefix") != PREFIX
                or not data.get("root", "").startswith(data["state_prefix"])
                or not isinstance(data.get("cases"), list) or not data["cases"]):
            raise SystemError("Module contract schema, anchor or finite case domain is invalid.")
        self.data, self.anchor = copy.deepcopy(data), anchor
        self.prefix = data["state_prefix"]
        self.anchor_index = system_index(anchor)
        self.exterior = [state.name for state in anchor.states if not state.name.startswith(self.prefix)]
        if data["root"] not in {state.name for state in anchor.states} or not set(data["exits"]) <= set(self.exterior):
            raise SystemError("The contract must name a declared root and exterior exits.")
        if data.get("entry") != {"call": "1NT", "opener_hcp": [15, 17], "shapes": ["4333", "4432", "5332"]}:
            raise SystemError("This contract implementation requires the declared natural 15-17 balanced 1NT entry.")
        identities = set()
        groups = set()
        for case in data["cases"]:
            board = parse_board(case["board"])
            if not isinstance(case.get("id"), str) or case["id"] in identities or type(case.get("require_game")) is not bool:
                raise SystemError("Contract cases need unique IDs and explicit game-interface assertions.")
            identities.add(case["id"])
            hand = board.deal.hand(board.dealer)
            if not 15 <= hand_hcp(hand) <= 17 or hand_shape(hand) not in ((4, 3, 3, 3), (4, 4, 3, 2), (5, 3, 3, 2)):
                raise SystemError("A contract probe is outside the frozen opening distribution.")
            band, shape = case.get("responder_hcp_band"), case.get("shape_class")
            if (not isinstance(band, list) or any(type(value) is not int for value in band)
                    or tuple(band) not in BANDS or shape not in SHAPES):
                raise SystemError("Contract probe HCP/shape strata must be declared precisely.")
            responder = board.deal.hand(board.dealer.next().next())
            if not band[0] <= hand_hcp(responder) <= band[1] or not _shape_class(responder, shape):
                raise SystemError("A contract probe does not match its claimed finite category.")
            if case["require_game"] != (band[0] >= 10):
                raise SystemError("The positive-response interface was weakened in a diagnostic probe.")
            groups.add((band[0], band[1], shape))
        if groups != {(low, high, shape) for low, high in BANDS for shape in SHAPES}:
            raise SystemError("The finite module contract is missing a required HCP/shape stratum.")

    def cases(self):
        identity = canonical_hash(self.data)
        return [{**copy.deepcopy(case), "case_id": canonical_hash({"contract": identity, "case": case["id"]})[:16],
                 "side": Seat(case["board"]["dealer"]).partnership.value} for case in self.data["cases"]]

    def scope_errors(self, candidate):
        raw, initial = candidate.to_data(), self.anchor.to_data()
        errors = []
        for key in ("schema_version", "operator_version", "scope", "fallback"):
            if raw[key] != initial[key]:
                errors.append("Frozen exterior field changed: " + key)
        if raw["protocol"]["initial"] != initial["protocol"]["initial"]:
            errors.append("Frozen opening state changed.")
        if not self.data["root"] in raw["protocol"]["states"]:
            errors.append("Declared module root was removed.")
        for name, parameter in initial["protocol"]["parameters"].items():
            if raw["protocol"]["parameters"].get(name) != parameter:
                errors.append("Frozen parameter definition changed: " + name)
        for name in raw["protocol"]["parameters"].keys() - initial["protocol"]["parameters"].keys():
            if not name.startswith(self.prefix):
                errors.append("New parameter needs an isolated module namespace: " + name)
        index = system_index(candidate)
        for key, node in self.anchor_index.nodes.items():
            mutable = node.state.startswith(self.prefix) or node.kind == "feature" and node.identifier.startswith(self.prefix)
            if node.kind in ("rule", "announcement"):
                mutable = all(PublicFilter(state).value(self.anchor_index.expressions[key]) is False for state in self.exterior)
            if not mutable and (key not in index.nodes or index.fingerprint(key) != self.anchor_index.fingerprint(key)):
                errors.append("Frozen exterior semantics changed: " + key)
        exterior = set(self.exterior)
        for state in candidate.states:
            if state.name not in exterior and not state.name.startswith(self.prefix):
                errors.append("New state is outside the module namespace: " + state.name)
            if state.name.startswith(self.prefix):
                if not state.otherwise.startswith(self.prefix):
                    errors.append("Module otherwise must stay internal; exterior exits need an explicit transition.")
                for target in [state.otherwise, *(item.target for item in state.transitions)]:
                    if not target.startswith(self.prefix) and target not in self.data["exits"]:
                        errors.append("Undeclared module exit: " + target)
                for transition in state.transitions:
                    if transition.target in self.data["exits"]:
                        value = dict(transition.assignments).get(ACTIVE)
                        if value is None or value.op != "literal" or value.literal is not False:
                            errors.append("An exterior exit must clear the module's public active flag.")
        for key, node in index.nodes.items():
            if node.kind in ("rule", "announcement") and (
                    key not in self.anchor_index.nodes or index.fingerprint(key) != self.anchor_index.fingerprint(key)):
                if any(PublicFilter(state).value(index.expressions[key]) is not False for state in exterior):
                    errors.append("Modified rule/announcement can escape the module: " + key)
            if node.kind == "feature" and key not in self.anchor_index.nodes and not node.identifier.startswith(self.prefix):
                errors.append("New helper needs the module namespace: " + key)
        return errors

    def check(self, candidate, stop=lambda: None):
        from bridge_evolve.evolution.execution import audit_module
        errors = self.scope_errors(candidate)
        passed, counts, visited, interference_count = 0, Counter(), set(), 0
        source_keys = []
        pass_system = validate_system({"schema_version": 2, "operator_version": 2, "name": "Contract passive opponent",
            "scope": "general", "features": {}, "announcements": [], "rules": [],
            "fallback": {"kind": "call", "call": "P"},
            "protocol": {"initial": "idle", "parameters": {}, "states": {"idle": {"transitions": [], "otherwise": "idle"}}}})
        if not errors:
            for case in self.data["cases"]:
                stop()
                board = parse_board(case["board"])
                own_side = board.dealer.partnership.value
                try:
                    players = (candidate, pass_system) if own_side == "NS" else (pass_system, candidate)
                    result = run_auction(*players, board, trace=True, compact=True)
                    entries = result["auction"]
                    if entries[0]["call"] != "1NT":
                        raise SystemError("The naturally reached frozen opening is not 1NT.")
                    for entry in entries:
                        if Seat(entry["seat"]).partnership.value != own_side:
                            continue
                        if entry["state"].startswith(self.prefix):
                            visited.add(entry["state"])
                            rule = entry["private_trace"]["rule_id"]
                            source_keys.append(rule)
                            if rule == "fallback":
                                raise SystemError("A module decision fell through to the whole-system fallback.")
                    after = result["partnership_contexts"][own_side]
                    if after["state"] != "finished":
                        raise SystemError("An uncontested module case escaped without its declared completed exit.")
                    audit_module().bounds_checker()(entries, board)
                    if case["require_game"]:
                        contract = result["contract"]
                        game = 3 if contract.get("strain") == "NT" else 4 if contract.get("strain") in ("H", "S") else 5
                        if contract.get("level", 0) < game:
                            raise SystemError("The positive-response interface did not complete a game-level contract.")
                    passed += 1
                    counts[case["shape_class"]] += 1
                except (SystemError, ValueError) as error:
                    LOGGER.warning("Module contract case %s failed: %s", case["id"], short_text(error))
                    errors.append({"case": case["id"], "type": type(error).__name__, "error": short_text(error)})
            from bridge_evolve.systems.language import parse_call
            from bridge_evolve.systems.v2_runtime import PublicWorld
            for interference in ("X", "2C", "2D", "2H", "2S"):
                stop()
                interference_count += 1
                try:
                    world = PublicWorld({"NS": candidate, "EW": pass_system}, Seat.NORTH, Vulnerability.NONE)
                    world.step(parse_call("1NT"))
                    world.step(parse_call(interference))
                    if world.frames["NS"].state not in self.data["exits"]:
                        raise SystemError("Interference did not follow the declared exterior handoff.")
                except (SystemError, ValueError) as error:
                    LOGGER.warning("Counterfactual interference probe %s failed: %s", interference, short_text(error))
                    errors.append({"counterfactual_prefix": ["1NT", interference], "type": type(error).__name__,
                                   "error": short_text(error)})
        return {"schema": "bridge-module-contract-result/v1", "contract_hash": canonical_hash(self.data),
                "system_hash": candidate.content_hash, "status": "passed" if not errors else "failed",
                "cases": len(self.data["cases"]), "passed": passed, "failures": errors[:12], "failure_count": len(errors),
                "covered_shape_groups": dict(counts), "visited_module_states": sorted(visited),
                "selected_rules": sorted(set(source_keys)), "counterfactual_interference_probes": interference_count,
                "scope": self.data["coverage_scope"]}
