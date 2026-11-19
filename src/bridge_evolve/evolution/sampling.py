"""Frozen, candidate-independent development cohorts using the existing deal sampler."""

from bridge_evolve.core import Seat
from bridge_evolve.systems.inputs import parse_board, parse_prefix, run_auction
from bridge_evolve.systems.language import PRIMITIVES, SystemError, compile_expression, object_fields, source_at, source_pointer, text
from bridge_evolve.systems.runtime import Evaluation, Observation, hand_hcp
from bridge_evolve.systems.v2_language import PRIVATE_OPERATORS

SCHEMA = "bridge-conditional-development/v1"
PUBLIC_OPERATORS = (set(PRIMITIVES) - PRIVATE_OPERATORS) | {"context", "var", "announced", "is_legal"}


class ReferenceAuctionError(SystemError):
    def __init__(self, error, board, definitions):
        from bridge_evolve.evaluation.data import board_data
        super().__init__(str(error), actor=error.actor, system_hash=error.system_hash, pointer=error.pointer)
        self.board = board_data(board)
        self.systems = {side: system.content_hash for side, system in definitions.items()}


def hand_conditions(raw):
    if not isinstance(raw, dict) or set(raw) - set("NESW"):
        raise ValueError("hand_conditions must map N/E/S/W to boolean hand expressions.")
    compiled = {}
    for seat, value in raw.items():
        with source_at(source_pointer("/development_sampling/hand_conditions", seat)):
            expression = compile_expression(value, version=2)
            pending = [expression]
            while pending:
                node = pending.pop()
                if node.op in PUBLIC_OPERATORS:
                    raise ValueError("Sampling conditions may use hands, not auction/context or candidate features.")
                pending.extend(node.args)
                pending.extend(child for _, child in node.bindings)
            if expression.kind != "bool":
                raise ValueError("A sampling hand condition must be boolean.")
            compiled[seat] = expression
    return compiled


def normalize_sampling(raw):
    from bridge_evolve.evolution.execution import audit_module
    fields = object_fields(raw, {"name", "constraints"},
                           {"dealer", "vulnerability", "attempt_limit", "hand_conditions", "auction"},
                           pointer="/development_sampling")
    name = text(fields["name"], pointer="/development_sampling/name")
    if len(name) > 160:
        raise ValueError("The conditional development cohort name must contain at most 160 characters.")
    spec = audit_module().sampling_config({
        **{key: fields[key] for key in ("constraints", "dealer", "vulnerability", "attempt_limit") if key in fields},
        "count": 1, "seed": 0, "attempt_limit": fields.get("attempt_limit", 1000000),
    }, max_count=100000)
    conditions = hand_conditions(fields.get("hand_conditions", {}))
    auction = fields.get("auction")
    if auction is not None:
        auction = object_fields(auction, {"reference", "opponent"}, {"side", "prefix", "rule_ids", "state"})
        if any(not isinstance(auction[key], str) or not auction[key] for key in ("reference", "opponent")):
            raise ValueError("An auction cohort requires frozen reference/opponent identities.")
        if auction.get("side", "NS") not in ("NS", "EW"):
            raise ValueError("Auction sampling side must be NS or EW.")
        if "prefix" in auction:
            parse_prefix(auction["prefix"])
        if "rule_ids" in auction and (not isinstance(auction["rule_ids"], list) or not auction["rule_ids"]
                or any(not isinstance(value, str) or not value for value in auction["rule_ids"])):
            raise ValueError("Auction rule_ids must be a nonempty list of rule IDs from the frozen reference.")
        if "state" in auction and (not isinstance(auction["state"], str) or not auction["state"]):
            raise ValueError("Auction state must be a nonempty state name.")
        if not auction.get("prefix") and not auction.get("rule_ids") and not auction.get("state"):
            raise ValueError("An auction cohort needs a natural prefix, rule_ids or state selector.")
    if auction is None and not conditions and all(interval == [0, 40 if field == "hcp" else 13]
                              for values in spec["constraints"].values() for field, interval in values.items()):
        raise ValueError("Conditional development must actually constrain the hand distribution.")
    return {"name": name, **{key: spec[key] for key in ("constraints", "dealer", "vulnerability", "attempt_limit")},
            "hand_conditions": {seat: expression.to_data() for seat, expression in conditions.items()},
            **({"auction": auction} if auction is not None else {})}


def matches_hands(board, conditions):
    for name, expression in conditions.items():
        seat = Seat(name)
        observation = Observation(board.deal.hand(seat), seat, seat, board.vulnerability, ())
        if not Evaluation(observation, "sampling").evaluate(expression):
            return False
    return True


def matches_auction(board, spec, system):
    if "auction" not in spec:
        return True
    if system is None:
        raise ValueError("An auction cohort requires a trusted frozen-system resolver.")
    target = spec["auction"]
    reference, opponent = system(target["reference"]), system(target["opponent"])
    side = target.get("side", "NS")
    ns, ew = (reference, opponent) if side == "NS" else (opponent, reference)
    try:
        actual = run_auction(ns, ew, board, compact=True, trace=True)
    except SystemError as error:
        raise ReferenceAuctionError(error, board, {"NS": ns, "EW": ew}) from error
    steps = actual["auction"]
    prefix = target.get("prefix", [])
    if [step["call"] for step in steps[:len(prefix)]] != prefix:
        return False
    own = [step for step in steps if Seat(step["seat"]).partnership.value == side]
    if "rule_ids" in target and not any(step["private_trace"]["rule_id"] in target["rule_ids"] for step in own):
        return False
    return "state" not in target or any(step["state"] == target["state"] for step in own)


def conditional_source(spec, count, seed, stop, draw, system=None):
    from bridge_evolve.evolution.execution import audit_module
    module = audit_module()
    config = module.sampling_config({
        **{key: spec[key] for key in ("constraints", "dealer", "vulnerability", "attempt_limit")},
        "count": count, "seed": seed,
    }, max_count=100000)
    conditions = hand_conditions(spec["hand_conditions"])

    def progress(attempts, accepted):
        draw.update(attempts=attempts, accepted=accepted)

    return module.sampled_boards(config, progress=progress, cancelled=lambda: stop() or False,
                                 accept=lambda board: matches_hands(board, conditions) and matches_auction(board, spec, system))


def check_cohort(spec, raw, index, system=None):
    board = parse_board(raw["board"])
    if spec["dealer"] != "cycle" and board.dealer.value != spec["dealer"]:
        raise ValueError("A conditional board has the wrong frozen dealer.")
    expected_dealer = list(Seat)[index % 4]
    if spec["dealer"] == "cycle" and board.dealer != expected_dealer:
        raise ValueError("A conditional board violates the dealer cycle.")
    from bridge_evolve.core import Vulnerability
    expected_vulnerability = (list(Vulnerability)[((index // 4) + index) % 4].value
                              if spec["vulnerability"] == "cycle" else spec["vulnerability"])
    if board.vulnerability.value != expected_vulnerability:
        raise ValueError("A conditional board violates the frozen vulnerability schedule.")
    for name, values in spec["constraints"].items():
        hand = board.deal.hand(Seat(name))
        actual = {"hcp": hand_hcp(hand), **{suit: sum(card.suit == suit for card in hand.cards) for suit in "SHDC"}}
        if any(not low <= actual[field] <= high for field, (low, high) in values.items()):
            raise ValueError(f"A conditional board violates {name}'s frozen ranges.")
    if not matches_hands(board, hand_conditions(spec["hand_conditions"])):
        raise ValueError("A conditional board violates its frozen hand predicates.")
    if not matches_auction(board, spec, system):
        raise ValueError("A conditional board does not naturally reach its frozen auction target.")


def descriptor(spec):
    return {"schema": SCHEMA, "definition": spec}
