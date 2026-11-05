"""Pure decisions over immutable, player-visible observations."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
from typing import TYPE_CHECKING

from bridge_evolve.core import (
    AuctionState, Call, CallKind, Card, Hand, Seat, Vulnerability,
    apply_call, initial_auction, legal_calls,
)
from bridge_evolve.systems.language import (
    Action, Expression, SystemDefinition, SystemError, Value, parse_call,
)

if TYPE_CHECKING:
    from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld

ROLES = ("dealer", "lho", "partner", "rho")


def replay(dealer: Seat, history: tuple[Call, ...]) -> AuctionState:
    auction = initial_auction(dealer)
    for call in history:
        auction = apply_call(auction, call)
    return auction


@dataclass(frozen=True, slots=True)
class Observation:
    hand: Hand
    seat: Seat
    dealer: Seat
    vulnerability: Vulnerability
    history: tuple[Call, ...]
    public_systems: tuple[str, ...] = ()
    public_definitions: tuple[tuple[str, SystemDefinition], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.hand, Hand) or not isinstance(self.hand.cards, tuple)
            or any(not isinstance(card, Card) for card in self.hand.cards)
        ):
            raise SystemError("Observation requires an immutable, validated Hand.")
        Hand(self.hand.cards)
        if not isinstance(self.seat, Seat) or not isinstance(self.dealer, Seat):
            raise SystemError("Observation seat and dealer must be Seat values.")
        if not isinstance(self.vulnerability, Vulnerability):
            raise SystemError("Observation vulnerability must be a Vulnerability value.")
        if not isinstance(self.history, tuple) or any(not isinstance(call, Call) for call in self.history):
            raise SystemError("Observation history must be a tuple of legal calls.")
        if not isinstance(self.public_systems, tuple) or any(
            not isinstance(spec, str) for spec in self.public_systems
        ):
            raise SystemError("Public systems must contain only static specification strings.")
        if not isinstance(self.public_definitions, tuple):
            raise SystemError("Public definitions must be immutable.")
        sides = []
        for item in self.public_definitions:
            if not isinstance(item, tuple) or len(item) != 2:
                raise SystemError("Public definitions require (NS/EW, SystemDefinition) pairs.")
            side, definition = item
            if side not in ("NS", "EW") or not isinstance(definition, SystemDefinition):
                raise SystemError("Invalid public definition.")
            sides.append(side)
        if len(set(sides)) != len(sides):
            raise SystemError("Duplicate partnership public definition.")
        auction = replay(self.dealer, self.history)
        if auction.ended or auction.next_seat != self.seat:
            raise SystemError("Observation must describe the current player of an unfinished auction.")

    @property
    def role(self) -> str:
        seat = self.dealer
        for role in ROLES:
            if seat == self.seat:
                return role
            seat = seat.next()
        raise SystemError("Unable to determine public role.")

    @property
    def legal_actions(self) -> tuple[Call, ...]:
        return legal_calls(replay(self.dealer, self.history))


@dataclass(frozen=True, slots=True)
class PublicView:
    seat: Seat
    dealer: Seat
    vulnerability: Vulnerability
    history: tuple[Call, ...]

    @property
    def hand(self) -> Hand:
        raise SystemError("A public protocol evaluation cannot access any private hand.")

    @property
    def legal_actions(self) -> tuple[Call, ...]:
        return legal_calls(replay(self.dealer, self.history))


def hand_hcp(hand: Hand) -> int:
    return sum({"A": 4, "K": 3, "Q": 2, "J": 1}.get(card.rank, 0) for card in hand.cards)


def hand_shape(hand: Hand) -> tuple[int, ...]:
    return tuple(sorted((sum(card.suit == suit for card in hand.cards) for suit in "CDHS"), reverse=True))


BALANCED_SHAPES = ((4, 3, 3, 3), (4, 4, 3, 2), (5, 3, 3, 2))


def number(value: Value) -> int:
    if type(value) is not int:
        raise SystemError("Internal expression type mismatch: expected number.")
    return value


def boolean(value: Value) -> bool:
    if type(value) is not bool:
        raise SystemError("Internal expression type mismatch: expected boolean.")
    return value


class Evaluation:
    def __init__(self, observation: Observation | PublicView, state: str, public: PublicContext | None = None):
        self.observation = observation
        self.state = state
        self.values: dict[Expression, Value] = {}
        self.public = public

    def evaluate(self, expression: Expression) -> Value:
        if expression not in self.values:
            self.values[expression] = self._evaluate(expression)
        return self.values[expression]

    def _evaluate(self, expression: Expression) -> Value:
        op = expression.op
        observation = self.observation
        parameters = dict(expression.parameters)
        parameters.update((name, self.evaluate(child)) for name, child in expression.bindings)
        if op == "if":
            branch = 1 if boolean(self.evaluate(expression.args[0])) else 2
            return self.evaluate(expression.args[branch])
        if op == "list":
            return tuple(self.evaluate(child) for child in expression.args)
        if self.public is not None:
            from bridge_evolve.systems.v2_runtime import evaluate_extension
            extension = evaluate_extension(expression, self)
            if extension is not None:
                return extension
        if op == "literal":
            return expression.literal
        if op == "ref":
            return self.evaluate(expression.args[0])
        if op == "hcp":
            return hand_hcp(observation.hand)
        if op == "controls":
            return sum({"A": 2, "K": 1}.get(card.rank, 0) for card in observation.hand.cards)
        if op == "shape":
            return hand_shape(observation.hand)
        if op == "balanced":
            return hand_shape(observation.hand) in BALANCED_SHAPES
        if op == "seat":
            return observation.seat.value
        if op == "role":
            if not isinstance(observation, Observation):
                raise SystemError("Player-relative role is unavailable in public transitions.")
            return observation.role
        if op == "state":
            return self.state
        if op == "vulnerable":
            return observation.vulnerability.is_vulnerable(observation.seat)
        if op == "previously_passed":
            seat = observation.dealer
            for call in observation.history:
                if seat == observation.seat and call.kind == CallKind.PASS:
                    return True
                seat = seat.next()
            return False
        if op == "has_contract_bid":
            return any(call.kind == CallKind.BID for call in observation.history)
        if op == "is_legal":
            return parse_call(parameters["call"]) in observation.legal_actions
        if op == "has":
            return any(card.rank + card.suit == parameters["card"] for card in observation.hand.cards)
        if op in ("length", "honors", "control"):
            if parameters["suit"] not in ("C", "D", "H", "S"):
                raise SystemError(f"Hand feature requires a defined C/D/H/S suit, got {parameters['suit']!r}.")
            ranks = {card.rank for card in observation.hand.cards if card.suit == parameters["suit"]}
            if op == "length":
                return len(ranks)
            if op == "honors":
                selected = parameters["ranks"]
                if not isinstance(selected, str):
                    raise SystemError("Internal honors rank type mismatch.")
                return len(ranks & set(selected))
            round_number = number(parameters["round"])
            high = bool(ranks & ({"A"} if round_number == 1 else {"A", "K"}))
            return high or (boolean(parameters["shortage"]) and len(ranks) < round_number)
        if op == "and":
            return all(boolean(self.evaluate(arg)) for arg in expression.args)
        if op == "or":
            return any(boolean(self.evaluate(arg)) for arg in expression.args)
        args = tuple(self.evaluate(arg) for arg in expression.args)
        if op == "not":
            return not boolean(args[0])
        if op == "eq":
            return args[0] == args[1]
        if op == "ne":
            return args[0] != args[1]
        if op == "in":
            options = args[1]
            if not isinstance(options, tuple):
                raise SystemError("Internal membership type mismatch.")
            return args[0] in options
        numbers = tuple(number(arg) for arg in args)
        if op == "between":
            return numbers[1] <= numbers[0] <= numbers[2]
        if op == "lt":
            return numbers[0] < numbers[1]
        if op == "le":
            return numbers[0] <= numbers[1]
        if op == "gt":
            return numbers[0] > numbers[1]
        if op == "ge":
            return numbers[0] >= numbers[1]
        if op == "add":
            return sum(numbers)
        if op == "sub":
            return numbers[0] - numbers[1]
        if op == "mul":
            product = 1
            for value in numbers:
                product *= value
            return product
        if op == "min":
            return min(numbers)
        if op == "max":
            return max(numbers)
        raise SystemError(f"No semantics for operator: {op}")


@dataclass(frozen=True)
class DecisionTrace:
    rule_id: str
    state: str
    checked_rules: tuple[tuple[str, bool], ...]
    values: tuple[tuple[Expression, Value], ...]
    public_context: tuple[tuple[str, Value], ...] = ()


@dataclass(frozen=True)
class Decision:
    call: Call
    trace: DecisionTrace


def decide(system: SystemDefinition, observation: Observation) -> Decision:
    world = None
    if system.version == 2 or observation.public_definitions:
        from bridge_evolve.systems.v2_runtime import build_world
        world = build_world(system, observation)
    return _decide_and_advance(system, observation, world)


def _decide_and_advance(
    system: SystemDefinition, observation: Observation, world: PublicWorld | None,
) -> Decision:
    if world is not None:
        from bridge_evolve.systems.v2_runtime import PublicContext
        expected = dict(observation.public_definitions)
        side = observation.seat.partnership.value
        if side in expected and expected[side].content_hash != system.content_hash:
            raise SystemError("The executing system differs from its public partnership definition.")
        expected[side] = system
        if (world.dealer != observation.dealer or world.vulnerability != observation.vulnerability
                or world.auction.calls != observation.history
                or {side: definition.content_hash for side, definition in world.definitions.items()}
                != {side: definition.content_hash for side, definition in expected.items()}):
            raise SystemError("Incremental public state does not match the current observation.")
        public = PublicContext(world, observation.seat.partnership.value, observation.seat)
        state = public.frame.state
        evaluation = Evaluation(observation, state, public)
    else:
        state = system.state_after(observation.history)
        evaluation = Evaluation(observation, state)
    checked: list[tuple[str, bool]] = []
    selected = system.fallback
    identifier = "fallback"
    ordered = sorted(system.rules, key=lambda rule: rule.priority, reverse=True)
    for _, group in groupby(ordered, key=lambda rule: rule.priority):
        hits = []
        for rule in group:
            matched = boolean(evaluation.evaluate(rule.when))
            checked.append((rule.id, matched))
            if matched:
                hits.append(rule)
        if len(hits) > 1:
            raise SystemError(f"Conflicting highest-priority rules: {[rule.id for rule in hits]}")
        if hits:
            selected, identifier = hits[0].call, hits[0].id
            break
    if isinstance(selected, Action):
        from bridge_evolve.systems.v2_runtime import resolve_action
        selected = resolve_action(selected, evaluation)
    if selected not in observation.legal_actions:
        raise SystemError(f"Rule {identifier} emitted illegal call {selected}; no fallback was substituted.")
    context = ()
    if world is not None and evaluation.public is not None:
        context = evaluation.public.snapshot()
        world.step(selected)
    return Decision(
        selected,
        DecisionTrace(identifier, state, tuple(checked), tuple(evaluation.values.items()), context),
    )
