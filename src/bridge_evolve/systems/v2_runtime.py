"""Reconstruct public meaning and protocol memory without receiving a Deal."""

from __future__ import annotations

from dataclasses import dataclass, field

from bridge_evolve.core import (
    ALL_BID_CALLS, Call, CallKind, Seat, Strain, Vulnerability,
    apply_call, initial_auction, legal_calls,
)
from bridge_evolve.systems.language import Action, Expression, SystemDefinition, SystemError, Value, parse_call
from bridge_evolve.systems.runtime import Evaluation, Observation, PublicView, boolean, number
from bridge_evolve.systems.v2_language import CONTEXT_TYPES, FORCING_STATES
from bridge_evolve.systems.shape_bounds import propagate_suit_bounds


def partner(seat: Seat) -> Seat:
    return seat.next().next()


def suit(value: Value, *, allow_nt: bool = False, allow_empty: bool = False) -> str:
    allowed = ("C", "D", "H", "S") + (("NT",) if allow_nt else ()) + (("",) if allow_empty else ())
    if not isinstance(value, str) or value not in allowed:
        raise SystemError(f"Expected a defined suit from {allowed}, got {value!r}.")
    return value


def goal_level(goal: str, strain: str) -> int:
    if goal == "grand_slam":
        return 7
    if goal in ("small_slam", "invite_grand"):
        return 6
    if goal in ("invite_game", "game", "slam_try"):
        return 4 if strain in ("H", "S") else 5 if strain in ("C", "D") else 3
    return 0


def obligated(forcing: str, target: Seat | None, actor: Seat) -> bool:
    return forcing in ("game", "small_slam", "invite_grand", "grand_slam") or (
        forcing in ("one_round", "slam_try") and target == actor
    )


@dataclass(frozen=True)
class PublicEvent:
    seat: Seat
    call: Call
    known: bool
    tags: tuple[str, ...] = ()
    shown_suit: str = ""


@dataclass
class Frame:
    state: str
    parameters: dict[str, Value] = field(default_factory=dict)
    forcing: str = "none"
    target: Seat | None = None
    goal: str = "none"


class PublicWorld:
    def __init__(self, definitions: dict[str, SystemDefinition], dealer: Seat, vulnerability: Vulnerability):
        self.definitions = definitions
        self.dealer = dealer
        self.vulnerability = vulnerability
        self.auction = initial_auction(dealer)
        self.events: list[PublicEvent] = []
        self.frames = {
            side: Frame(system.initial, {parameter.name: parameter.initial for parameter in system.parameters})
            for side, system in definitions.items()
        }

    def evaluation(self, side: str, event: PublicEvent) -> Evaluation:
        seat = Seat.NORTH if side == "NS" else Seat.EAST
        view = PublicView(seat, self.dealer, self.vulnerability, self.auction.calls)
        public = PublicContext(self, side, seat, event)
        return Evaluation(view, self.frames[side].state, public)

    def step(self, call: Call) -> None:
        updated = apply_call(self.auction, call)
        actor = self.auction.next_seat
        actor_side = actor.partnership.value
        event = PublicEvent(actor, call, actor_side in self.definitions)
        if event.known:
            tags = set()
            shown = set()
            evaluation = self.evaluation(actor_side, event)
            for announcement in self.definitions[actor_side].announcements:
                if boolean(evaluation.evaluate(announcement.when)):
                    tags.update(announcement.tags)
                    if announcement.suit is not None:
                        shown.add(suit(evaluation.evaluate(announcement.suit), allow_nt=True, allow_empty=True))
            if len(shown) > 1:
                raise SystemError("Conflicting publicly declared suits for the same call.",
                                  actor=actor.value, system_hash=self.definitions[actor_side].content_hash)
            for exclusive in ({"natural", "artificial"}, {"weak_nt", "strong_nt"}):
                if exclusive <= tags:
                    raise SystemError(f"Conflicting public announcement tags: {sorted(exclusive)}",
                                      actor=actor.value, system_hash=self.definitions[actor_side].content_hash)
            event = PublicEvent(actor, call, True, tuple(sorted(tags)), next(iter(shown), ""))
        updates = {}
        for side, system in self.definitions.items():
            frame = self.frames[side]
            if system.version == 1:
                state = next(item for item in system.states if item.name == frame.state)
                updates[side] = Frame(dict(state.on).get(str(call), state.otherwise))
                continue
            state = next(item for item in system.states if item.name == frame.state)
            evaluation = self.evaluation(side, event)
            hits = [item for item in state.transitions if boolean(evaluation.evaluate(item.when))]
            if len(hits) > 1:
                raise SystemError(f"Conflicting public transitions: {[item.id for item in hits]}",
                                  actor=actor.value, system_hash=system.content_hash)
            transition = hits[0] if hits else None
            required = actor_side == side and obligated(frame.forcing, frame.target, actor)
            if required and call.kind == CallKind.PASS and not (transition and transition.allow_pass):
                raise SystemError(f"Protocol forcing violation: {actor.value} cannot Pass under {frame.forcing}.",
                                  actor=actor.value, system_hash=system.content_hash)
            forcing, target = frame.forcing, frame.target
            goal = frame.goal
            if forcing in ("one_round", "slam_try", "invite_game") and target == actor:
                forcing, target = "none", None
            elif target == actor:
                target = None
            parameters = dict(frame.parameters)
            if transition is not None:
                parameters.update((name, evaluation.evaluate(expr)) for name, expr in transition.assignments)
                if transition.forcing is not None:
                    requested = transition.forcing
                    if requested in ("one_round", "slam_try", "invite_game", "invite_grand") and actor_side != side:
                        raise SystemError("A one-round force must be initiated by a call from that partnership.",
                                          actor=actor.value, system_hash=system.content_hash)
                    if requested in ("one_round", "slam_try"):
                        forcing = forcing if forcing in ("game", "small_slam", "invite_grand", "grand_slam") else requested
                        target = partner(actor)
                        if FORCING_STATES.index(goal) < FORCING_STATES.index(requested):
                            goal = requested
                    elif requested == "none":
                        forcing, target = "none", None
                    else:
                        preserve = forcing in ("game", "small_slam", "invite_grand", "grand_slam") and (
                            goal_level(forcing, "NT") > goal_level(requested, "NT")
                        )
                        if not preserve:
                            forcing, goal = requested, requested
                            target = partner(actor) if requested in ("invite_game", "invite_grand") else None
            for parameter in system.parameters:
                value = parameters[parameter.name]
                if parameter.domain and value not in parameter.domain:
                    raise SystemError(f"Public parameter {parameter.name} is outside its declared domain.",
                                      actor=actor.value, system_hash=system.content_hash)
            for owner in ("captain", "responder"):
                lower = number(parameters.get(owner + "_hcp_min", 0))
                upper = number(parameters.get(owner + "_hcp_max", 40))
                captain = parameters.get("captain", "")
                if captain:
                    seat = Seat(captain) if owner == "captain" else partner(Seat(captain))
                    upper = min(upper, number(parameters.get("seat_" + seat.value + "_hcp_max", 40)))
                if not 0 <= lower <= upper <= 40:
                    raise SystemError(f"Inconsistent public HCP interval for {owner}: [{lower}, {upper}].",
                                      actor=actor.value, system_hash=system.content_hash)
            try:
                propagate_suit_bounds(parameters, frame.parameters)
            except SystemError as error:
                raise SystemError(str(error), actor=actor.value, system_hash=system.content_hash) from error
            if (forcing in ("game", "small_slam", "invite_grand", "grand_slam") and actor_side == side
                    and call.kind == CallKind.BID and "artificial" not in event.tags):
                minimum = goal_level(forcing, call.strain.value)
                if call.level is not None and call.level >= minimum:
                    forcing = "one_round" if target is not None else "none"
            updates[side] = Frame(
                transition.target if transition else state.otherwise, parameters, forcing, target, goal,
            )
        if updated.ended:
            pending = [side for side, frame in updates.items() if frame.forcing != "none"]
            if pending:
                raise SystemError(f"Auction ended with unresolved protocol forcing: {pending}", actor=actor.value,
                                  system_hash=self.definitions[pending[0]].content_hash if len(pending) == 1 else None)
        self.frames = updates
        self.events.append(event)
        self.auction = updated


@dataclass(frozen=True)
class PublicContext:
    world: PublicWorld
    side: str
    seat: Seat
    event: PublicEvent | None = None

    @property
    def frame(self) -> Frame:
        return self.world.frames[self.side]

    def hcp_upper(self, seat: Seat) -> int:
        parameters = self.frame.parameters
        maximum = number(parameters.get("seat_" + seat.value + "_hcp_max", 40))
        captain = parameters.get("captain", "")
        if captain:
            prefix = "captain_" if captain == seat.value else "responder_"
            maximum = min(maximum, number(parameters.get(prefix + "hcp_max", 40)))
        return maximum

    def selected_event(self, which: str) -> PublicEvent | None:
        bids = [event for event in self.world.events if event.call.kind == CallKind.BID]
        if which == "last_call":
            return self.world.events[-1] if self.world.events else None
        if which == "opening":
            return bids[0] if bids else None
        if which == "last_bid":
            return bids[-1] if bids else None
        if which in ("partner", "partner_call"):
            source = self.world.events if which == "partner_call" else bids
            events = [event for event in source if event.seat == partner(self.seat)]
        elif which in ("opponents", "opponent_call"):
            source = self.world.events if which == "opponent_call" else bids
            events = [event for event in source if event.seat.partnership.value != self.side]
        else:
            raise SystemError(f"Unknown public event selector: {which}")
        return events[-1] if events else None

    def value(self, name: str) -> Value:
        if name == "state":
            return self.frame.state
        events = self.world.events
        bids = [event for event in events if event.call.kind == CallKind.BID]
        opening = bids[0] if bids else None
        opener = opening.seat if opening else None
        our_bids = [event for event in bids if event.seat.partnership.value == self.side]
        our_opener = our_bids[0].seat if our_bids else None
        if name == "our_bid_count":
            return len(our_bids)
        if name == "last_bid_by":
            return ("ours" if bids[-1].seat.partnership.value == self.side else "opponents") if bids else ""
        interventions = [
            event for event in events
            if opener is not None and event.seat.partnership != opener.partnership
            and event.call.kind != CallKind.PASS
        ]
        overcaller = interventions[0].seat if interventions else None
        roles = {
            "opener": opener, "responder": partner(opener) if opener else None,
            "overcaller": overcaller, "advancer": partner(overcaller) if overcaller else None,
            "our_opener": our_opener, "our_responder": partner(our_opener) if our_opener else None,
        }
        if name in roles:
            seat = roles[name]
            return seat.value if seat is not None else ""
        if name == "dealer":
            return self.world.dealer.value
        if name == "vulnerability":
            return self.world.vulnerability.value
        if name == "own_vulnerable":
            return self.world.vulnerability.is_vulnerable(self.seat)
        if name == "opponents_vulnerable":
            return self.world.vulnerability.is_vulnerable(self.seat.next())
        if name == "partner_seat":
            return partner(self.seat).value
        if name == "dynamic_role":
            return next((role for role in ("opener", "responder", "overcaller", "advancer")
                         if roles[role] == self.seat), "unopened" if opener is None else "other")
        if name == "opening_position":
            if opener is None:
                return 0
            order = [self.world.dealer]
            for _ in range(3):
                order.append(order[-1].next())
            return order.index(opener) + 1
        if name == "opening_strain":
            return opening.call.strain.value if opening and opening.call.strain else ""
        if name == "last_call":
            return str(events[-1].call) if events else ""
        if name == "last_actor":
            return events[-1].seat.value if events else ""
        if name in ("last_bid", "last_bid_strain", "last_bid_level", "last_bid_actor"):
            last = bids[-1] if bids else None
            if name == "last_bid_level":
                return last.call.level if last and last.call.level is not None else 0
            if name == "last_bid":
                return str(last.call) if last else ""
            if name == "last_bid_actor":
                return last.seat.value if last else ""
            return last.call.strain.value if last and last.call.strain else ""
        if name in ("partner_bid_strain", "opponent_bid_strain", "partner_shown_suit", "opponent_shown_suit"):
            selector = "partner" if name.startswith("partner") else "opponents"
            event = self.selected_event(selector)
            if name.endswith("shown_suit"):
                if event is not None and not event.known:
                    raise SystemError("Cannot query shown suit without that side's public definition.")
                return event.shown_suit if event else ""
            return event.call.strain.value if event and event.call.strain else ""
        if name == "unbid_suits":
            used = {event.call.strain.value for event in bids if event.call.strain}
            return tuple(suit for suit in ("C", "D", "H", "S") if suit not in used)
        if name == "passed_before_opening":
            for event in events:
                if event.call.kind == CallKind.BID:
                    break
                if event.seat == self.seat and event.call.kind == CallKind.PASS:
                    return True
            return False
        if name == "agreed_trump":
            return self.frame.parameters.get("trump", "")
        if name == "partnership_hcp_min":
            return sum(number(self.frame.parameters.get(prefix + "hcp_min", 0)) for prefix in ("captain_", "responder_"))
        if name == "partnership_hcp_max":
            seat = Seat.NORTH if self.side == "NS" else Seat.EAST
            return min(40, self.hcp_upper(seat) + self.hcp_upper(partner(seat)))
        if name == "partner_hcp_max":
            return self.hcp_upper(partner(self.seat))
        if name.startswith("partnership_") and name.endswith("_min"):
            strain = name.removeprefix("partnership_").removesuffix("_min")
            return sum(number(self.frame.parameters.get(prefix + strain + "_min", 0)) for prefix in ("captain_", "responder_"))
        if name.startswith("partner_") and name.endswith("_min"):
            prefix = "responder_" if self.frame.parameters.get("captain", "") == self.seat.value else "captain_"
            return self.frame.parameters.get(prefix + name.removeprefix("partner_"), 0)
        if name.startswith("partnership_") and name.endswith("_max"):
            strain = name.removeprefix("partnership_").removesuffix("_max")
            return min(13, sum(number(self.frame.parameters.get(prefix + strain + "_max", 13))
                               for prefix in ("captain_", "responder_")))
        if name.startswith("partner_") and name.endswith("_max"):
            prefix = "responder_" if self.frame.parameters.get("captain", "") == self.seat.value else "captain_"
            return self.frame.parameters.get(prefix + name.removeprefix("partner_"), 13)
        if name == "agreed_trump_min":
            trump = str(self.frame.parameters.get("trump", ""))
            return sum(number(self.frame.parameters.get(prefix + trump + "_min", 0)) for prefix in ("captain_", "responder_")) if trump else 0
        if name == "agreed_trump_max":
            trump = str(self.frame.parameters.get("trump", ""))
            return self.value("partnership_" + trump + "_max") if trump else 0
        if name in ("double_kind", "opponent_double_kind"):
            other = "EW" if self.side == "NS" else "NS"
            frame = self.world.frames.get(other) if name == "opponent_double_kind" else self.frame
            return frame.parameters.get("double_kind", "") if frame else ""
        if name == "forcing":
            return self.frame.forcing
        if name == "goal":
            return self.frame.goal
        if name == "goal_level":
            return goal_level(self.frame.goal, str(self.frame.parameters.get("trump", "NT")))
        if name == "goal_met":
            if not our_bids:
                return self.frame.goal == "none"
            last = our_bids[-1].call
            minimum = goal_level(self.frame.goal, last.strain.value)
            return minimum == 0 or last.level >= minimum
        if name == "must_bid":
            return obligated(self.frame.forcing, self.frame.target, self.seat)
        if name.startswith("event."):
            event = self.event
            if event is None:
                raise SystemError(f"{name} is available only while interpreting a public call event.")
            order = [self.world.dealer]
            for _ in range(3):
                order.append(order[-1].next())
            passed = False
            for previous in events:
                if previous.call.kind == CallKind.BID:
                    break
                if previous.seat == event.seat and previous.call.kind == CallKind.PASS:
                    passed = True
            values: dict[str, Value] = {
                "event.actor": event.seat.value, "event.call": str(event.call),
                "event.strain": event.call.strain.value if event.call.strain else "",
                "event.level": event.call.level if event.call.level is not None else 0,
                "event.kind": event.call.kind.value,
                "event.by": "ours" if event.seat.partnership.value == self.side else "opponents",
                "event.tags": event.tags,
                "event.position": order.index(event.seat) + 1,
                "event.passed_before_opening": passed,
            }
            return values[name]
        raise SystemError(f"Unknown context value: {name}")

    def snapshot(self) -> tuple[tuple[str, Value], ...]:
        unavailable = []
        for key, selector in (("partner_shown_suit", "partner"), ("opponent_shown_suit", "opponents")):
            event = self.selected_event(selector)
            if event is not None and not event.known:
                unavailable.append(key)
        values = [
            (name, self.value(name)) for name in CONTEXT_TYPES
            if not name.startswith("event.") and name not in unavailable
        ]
        values.extend((f"var.{name}", value) for name, value in self.frame.parameters.items())
        values.append(("unavailable", tuple(unavailable)))
        return tuple(values)


def build_world(system: SystemDefinition, observation: Observation) -> PublicWorld:
    definitions = dict(observation.public_definitions)
    side = observation.seat.partnership.value
    if side in definitions and definitions[side].content_hash != system.content_hash:
        raise SystemError("The executing system differs from its public partnership definition.")
    definitions[side] = system
    world = PublicWorld(definitions, observation.dealer, observation.vulnerability)
    for call in observation.history:
        world.step(call)
    return world


def public_context(system: SystemDefinition, observation: Observation) -> dict[str, object]:
    world = build_world(system, observation)
    context = PublicContext(world, observation.seat.partnership.value, observation.seat)
    from bridge_evolve.systems.language import thaw
    return {"state": context.frame.state, "values": {name: thaw(value) for name, value in context.snapshot()}}


def evaluate_extension(expression: Expression, evaluation: Evaluation) -> Value | None:
    context = evaluation.public
    if context is None:
        raise SystemError("v2 expressions require a reconstructed public context.")
    parameters = dict(expression.parameters)
    if expression.op == "context":
        return context.value(str(parameters["name"]))
    if expression.op == "var":
        return context.frame.parameters[str(parameters["name"])]
    if expression.op == "announced":
        event = context.selected_event(str(parameters["which"]))
        if event is None:
            return False
        if not event.known:
            raise SystemError("Cannot query a call's tags without that side's public definition.")
        return parameters["tag"] in event.tags
    if expression.op == "keycards":
        trump = suit(evaluation.evaluate(expression.args[0]))
        return sum(card.rank == "A" or (card.suit == trump and card.rank == "K")
                   for card in evaluation.observation.hand.cards)
    return None


def resolve_action(action: Action, evaluation: Evaluation) -> Call:
    if action.kind == "call" and action.call is not None:
        return action.call
    if action.kind == "error":
        raise SystemError(f"Unimplemented protocol branch: {action.message}")
    values = {name: evaluation.evaluate(expression) for name, expression in action.expressions}
    if action.kind == "bid":
        return Call.bid(number(values["level"]), Strain(suit(values["strain"], allow_nt=True)))
    if action.kind == "step":
        after = parse_call(values["after"])
        count = number(values["count"])
        if after.kind != CallKind.BID or count < 1:
            raise SystemError("Step action requires a contract bid anchor and a positive count.")
        index = after.ordering_index() + count
        if index >= len(ALL_BID_CALLS):
            raise SystemError("Step action exceeds 7NT.")
        return ALL_BID_CALLS[index]
    if action.kind == "cheapest":
        choices = values["strains"]
        minimum, maximum = number(values["min_level"]), number(values["max_level"])
        if not isinstance(choices, tuple) or not 1 <= minimum <= maximum <= 7:
            raise SystemError("Invalid cheapest-action strains or level interval.")
        strains = {suit(value, allow_nt=True) for value in choices}
        for call in evaluation.observation.legal_actions:
            if (call.kind == CallKind.BID and call.strain is not None and call.strain.value in strains
                    and call.level is not None and minimum <= call.level <= maximum):
                return call
        raise SystemError("No legal contract bid satisfies the cheapest-action constraints.")
    raise SystemError(f"Invalid action: {action.kind}")
