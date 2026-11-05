"""Strict inputs, general four-player auctions, and the frozen local 1NT mode."""

from __future__ import annotations

from dataclasses import dataclass

from bridge_evolve.core import (
    Call, Card, Contract, Deal, Hand, Seat, Vulnerability,
    apply_call, final_contract, initial_auction,
)
from bridge_evolve.systems.language import SystemDefinition, SystemError, object_fields, parse_call, text, thaw, validate_system
from bridge_evolve.systems.render import render_call, render_expression, render_system
from bridge_evolve.systems.runtime import (
    BALANCED_SHAPES, Decision, Observation, decide, hand_hcp, hand_shape,
)
from bridge_evolve.systems.shape_bounds import SUITS, shape_range, suit_constraints


def parse_hand(value: object) -> Hand:
    sections = text(value).split(".")
    if len(sections) != 4:
        raise SystemError("Hand notation is S.H.D.C, with T for ten and - for a void.")
    cards = []
    for suit, ranks in zip("SHDC", sections, strict=True):
        if ranks == "-":
            continue
        if not ranks:
            raise SystemError("Use - rather than an empty suit.")
        cards.extend(Card(rank, suit) for rank in ranks)
    return Hand(tuple(cards))


def parse_observation(value: object) -> Observation:
    fields = object_fields(
        value, {"hand", "seat", "dealer", "vulnerability", "history"}, {"public_systems", "public_definitions"},
    )
    history = fields["history"]
    public = fields.get("public_systems", [])
    if not isinstance(history, list) or not isinstance(public, list):
        raise SystemError("history and public_systems must be lists.")
    definitions = fields.get("public_definitions", {})
    if not isinstance(definitions, dict) or any(side not in ("NS", "EW") for side in definitions):
        raise SystemError("public_definitions must map NS/EW to static system IR objects.")
    return Observation(
        parse_hand(fields["hand"]), Seat(text(fields["seat"])), Seat(text(fields["dealer"])),
        Vulnerability(text(fields["vulnerability"])), tuple(parse_call(call) for call in history),
        tuple(text(spec) for spec in public),
        tuple((side, validate_system(raw)) for side, raw in definitions.items()),
    )


@dataclass(frozen=True)
class Board:
    deal: Deal
    dealer: Seat
    vulnerability: Vulnerability


def parse_board(value: object) -> Board:
    fields = object_fields(value, {"dealer", "vulnerability", "hands"})
    hands = object_fields(fields["hands"], {"N", "E", "S", "W"})
    return Board(
        Deal(*(parse_hand(hands[seat]) for seat in "NESW")),
        Seat(text(fields["dealer"])), Vulnerability(text(fields["vulnerability"])),
    )


def protocol_record(snapshot: tuple) -> dict[str, object]:
    fields = {"state", "var.captain", "agreed_trump", "agreed_trump_min", "agreed_trump_max", "partnership_hcp_min", "partnership_hcp_max", "forcing", "must_bid",
              "goal", "goal_level", "goal_met", "double_kind", "opponent_double_kind"}
    fields.update("partnership_" + suit + "_min" for suit in "CDHS")
    fields.update("partnership_" + suit + "_max" for suit in "CDHS")
    return {name: thaw(value) for name, value in snapshot if name in fields or (
        name.startswith(("var.captain_", "var.responder_", "var.seat_"))
        and (name.endswith(("_min", "_max", "_support_quality", "_support_denied", "_control")) or "_ge_" in name)
    ) or name in ("var.slam_active", "var.slam_started", "var.slam_solid", "var.game_invited", "var.nt_slam_invited")
    }


def private_inference(observation: Observation, snapshot: tuple) -> dict[str, object]:
    values = dict(snapshot)
    suit_min = {suit: max(sum(card.suit == suit for card in observation.hand.cards) + int(values.get("partner_" + suit + "_min", 0)),
                         int(values.get("partnership_" + suit + "_min", 0))) for suit in "CDHS"}
    result = {
        "partnership_hcp_min": max(hand_hcp(observation.hand) + int(values.get("partner_hcp_min", 0)),
                                   int(values.get("partnership_hcp_min", 0))),
        "partnership_hcp_max": min(hand_hcp(observation.hand) + int(values.get("partner_hcp_max", 40)),
                       int(values.get("partnership_hcp_max", 40))),
        "suit_min": suit_min,
    }
    if all("var.captain_" + suit + "_max" in values for suit in SUITS):
        owner = "responder" if values.get("var.captain") == observation.seat.value else "captain"
        minimum, maximum, relations = suit_constraints(values, f"var.{owner}_")
        minimum = tuple(max(low, 4 if values.get(f"var.{owner}_{suit}_support_quality") and sum(
            card.rank in "AKQ" and card.suit == suit for card in observation.hand.cards) == 3 else 0)
                        for suit, low in zip(SUITS, minimum, strict=True))
        own = tuple(sum(card.suit == suit for card in observation.hand.cards) for suit in SUITS)
        lower, upper = shape_range(minimum, tuple(min(high, 13 - length) for high, length in zip(maximum, own, strict=True)), relations)
        suit_max = {}
        for index, suit in enumerate(SUITS):
            suit_min[suit] = max(own[index] + lower[index], int(values.get("partnership_" + suit + "_min", 0)))
            suit_max[suit] = min(own[index] + upper[index], int(values.get("partnership_" + suit + "_max", 13)))
            if suit_min[suit] > suit_max[suit]:
                raise SystemError(f"Inconsistent private partnership suit interval for {suit}.")
        result["suit_max"] = suit_max
    return result


def decision_data(system: SystemDefinition, observation: Observation, decision: Decision, trace: bool) -> dict[str, object]:
    result: dict[str, object] = {
        "system_hash": system.content_hash,
        "seat": observation.seat.value,
        "state": decision.trace.state,
        "call": str(decision.call),
        "legal_calls": [str(call) for call in observation.legal_actions],
        "public_meaning": render_call(system, decision.call),
    }
    if trace:
        result["private_trace"] = {
            "rule_id": decision.trace.rule_id,
            "checked_rules": [
                {"id": identifier, "matched": matched} for identifier, matched in decision.trace.checked_rules
            ],
            "values": [
                {"expression": expression.to_data(), "description": render_expression(expression), "value": thaw(value)}
                for expression, value in decision.trace.values
            ],
        }
    if system.version == 2:
        result["public_context"] = {name: thaw(value) for name, value in decision.trace.public_context}
        if trace:
            result["private_trace"]["inference"] = private_inference(observation, decision.trace.public_context)
    return result


def parse_prefix(value: object) -> tuple[Call, ...]:
    if not isinstance(value, list):
        raise SystemError("An auction prefix must be a list of canonical call strings.")
    return tuple(parse_call(item) for item in value)


def run_auction(
    ns: SystemDefinition, ew: SystemDefinition, board: Board,
    *, prefix: tuple[Call, ...] = (), trace: bool = False, compact: bool = False,
) -> dict[str, object]:
    from bridge_evolve.systems.runtime import _decide_and_advance
    from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld
    if ns.scope != "general" or ew.scope != "general":
        raise SystemError("General auctions require two scope=general systems; local experiments remain separate.")
    if not isinstance(prefix, tuple):
        raise SystemError("Auction prefix must be immutable.")
    definitions = {"NS": ns, "EW": ew}
    public = tuple(render_system(system) for system in (ns, ew))
    world = PublicWorld(definitions, board.dealer, board.vulnerability)
    entries: list[dict[str, object]] = []

    def record_after(entry: dict, seat: Seat) -> None:
        side = seat.partnership.value
        if trace and "captain_S_max" in world.frames[side].parameters:
            entry["public_context_after"] = protocol_record(PublicContext(world, side, seat).snapshot())

    for call in prefix:
        seat = world.auction.next_seat
        world.step(call)
        entries.append({
            "seat": seat.value, "call": str(call), "source": "prefix",
            "public_meaning": render_call(definitions[seat.partnership.value], call),
        })
        record_after(entries[-1], seat)
    while not world.auction.ended:
        seat = world.auction.next_seat
        system = definitions[seat.partnership.value]
        observation = Observation(
            board.deal.hand(seat), seat, board.dealer, board.vulnerability,
            world.auction.calls, public, tuple(definitions.items()),
        )
        try:
            decision = _decide_and_advance(system, observation, world)
        except SystemError as error:
            if error.system_hash is not None or error.actor is not None:
                raise
            raise SystemError(str(error), actor=seat.value, system_hash=system.content_hash) from error
        if compact:
            entry = {
                "system_hash": system.content_hash, "seat": seat.value,
                "call": str(decision.call), "state": decision.trace.state,
            }
            if trace:
                entry["public_context"] = protocol_record(decision.trace.public_context)
                entry["private_trace"] = {
                    "rule_id": decision.trace.rule_id,
                    "features": [
                        {"expression": expression.to_data(), "value": thaw(value)}
                        for expression, value in decision.trace.values
                        if expression.op in ("hcp", "length", "honors", "control", "keycards", "shape", "balanced", "var", "context")
                    ],
                    "inference": private_inference(observation, decision.trace.public_context),
                }
        else:
            entry = decision_data(system, observation, decision, trace)
        entry["source"] = "policy"
        entries.append(entry)
        record_after(entry, seat)
    contract = final_contract(world.auction)
    return {
        "mode": "general", "systems": {"NS": ns.content_hash, "EW": ew.content_hash},
        "dealer": board.dealer.value, "vulnerability": board.vulnerability.value,
        "auction": entries,
        "partnership_contexts": {side: protocol_record(PublicContext(world, side, Seat.NORTH if side == "NS" else Seat.EAST).snapshot())
                     for side, definition in definitions.items() if definition.version == 2},
        "contract": {
            "level": contract.level, "strain": contract.strain.value,
            "declarer": contract.declarer.value, "doubling": contract.doubling,
        } if isinstance(contract, Contract) else {"passed_out": True},
        "evaluation": "not_evaluated",
        "note": "Single-table bidding only; no DDS, trick prediction, score, or paired IMP evaluation.",
    }


def run_uncontested(system: SystemDefinition, board: Board, *, trace: bool = False) -> dict[str, object]:
    if system.scope != "uncontested-1nt":
        raise SystemError("auction run currently supports only scope=uncontested-1nt.")
    if board.dealer != Seat.NORTH:
        raise SystemError("The current local experiment fixes N as dealer and 1NT opener.")
    if not 15 <= hand_hcp(board.deal.north) <= 17 or hand_shape(board.deal.north) not in BALANCED_SHAPES:
        raise SystemError("Board is outside the fixed 15-17 HCP, 4333/4432/5332 opening distribution.")
    public_specification = render_system(system)
    auction = initial_auction(board.dealer)
    decisions: list[dict[str, object]] = []
    while not auction.ended:
        seat = auction.next_seat
        if seat in (Seat.EAST, Seat.WEST):
            call = Call.pass_call()
            entry: dict[str, object] = {
                "seat": seat.value, "call": "P", "public_meaning": "本局部实验固定对手始终Pass。",
            }
        else:
            observation = Observation(
                board.deal.hand(seat), seat, board.dealer, board.vulnerability,
                auction.calls, (public_specification,),
            )
            decision = decide(system, observation)
            call = decision.call
            if not auction.calls and str(call) != "1NT":
                raise SystemError("The system did not produce the required fixed 1NT opening.")
            entry = decision_data(system, observation, decision, trace)
        decisions.append(entry)
        auction = apply_call(auction, call)
    contract = final_contract(auction)
    if not isinstance(contract, Contract):
        raise SystemError("A local 1NT auction unexpectedly passed out.")
    return {
        "mode": "uncontested-1nt",
        "system_hash": system.content_hash,
        "dealer": board.dealer.value,
        "vulnerability": board.vulnerability.value,
        "auction": decisions,
        "contract": {
            "level": contract.level, "strain": contract.strain.value,
            "declarer": contract.declarer.value, "doubling": contract.doubling,
        },
        "evaluation": "not_evaluated",
        "note": "Only bidding was simulated. No DDS, trick prediction, score, or strength claim.",
    }
