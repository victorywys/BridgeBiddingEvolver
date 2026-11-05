"""Exact marginal suit bounds from public constraints on a 13-card hand."""

from collections.abc import Mapping
from functools import lru_cache

from bridge_evolve.systems.language import SystemError, Value, integer

SUITS = ("S", "H", "D", "C")
SHAPES = tuple(
    (spades, hearts, diamonds, 13 - spades - hearts - diamonds)
    for spades in range(14)
    for hearts in range(14 - spades)
    for diamonds in range(14 - spades - hearts)
)


@lru_cache(maxsize=8192)
def shape_range(
    minimum: tuple[int, ...], maximum: tuple[int, ...], relations: tuple[tuple[int, int], ...] = (),
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if (len(minimum) != 4 or len(maximum) != 4
            or any(type(low) is not int or type(high) is not int or not 0 <= low <= high <= 13
                   for low, high in zip(minimum, maximum, strict=True))
            or any(left not in range(4) or right not in range(4) for left, right in relations)):
        raise SystemError(f"Invalid suit intervals or relations: {minimum}, {maximum}, {relations}.")
    possible = [
        shape for shape in SHAPES
        if all(low <= length <= high for length, low, high in zip(shape, minimum, maximum, strict=True))
        and all(shape[left] >= shape[right] for left, right in relations)
    ]
    if not possible:
        raise SystemError(f"Suit constraints admit no 13-card hand: {minimum}, {maximum}, {relations}.")
    return (tuple(min(shape[index] for shape in possible) for index in range(4)),
            tuple(max(shape[index] for shape in possible) for index in range(4)))


def suit_constraints(
    parameters: Mapping[str, Value], prefix: str,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[tuple[int, int], ...]]:
    minimum = tuple(integer(parameters.get(prefix + suit + "_min", 0)) for suit in SUITS)
    maximum = tuple(integer(parameters.get(prefix + suit + "_max", 13)) for suit in SUITS)
    relations = []
    for left, left_suit in enumerate(SUITS):
        for right, right_suit in enumerate(SUITS):
            if left == right:
                continue
            value = parameters.get(prefix + left_suit + "_ge_" + right_suit, False)
            if type(value) is not bool:
                raise SystemError("Public suit-order guarantees must be boolean.")
            if value:
                relations.append((left, right))
    return minimum, maximum, tuple(relations)


def propagate_suit_bounds(parameters: dict[str, Value], previous: Mapping[str, Value]) -> None:
    for owner in ("captain", "responder"):
        prefix = owner + "_"
        keys = tuple(prefix + suit + ending for suit in SUITS for ending in ("_min", "_max"))
        if not all(key in parameters for key in keys):
            continue
        for suit in SUITS:
            low, high = prefix + suit + "_min", prefix + suit + "_max"
            if integer(parameters[low]) < integer(previous[low]) or integer(parameters[high]) > integer(previous[high]):
                raise SystemError(f"Public suit interval weakened for {owner}/{suit}.")
        for left in SUITS:
            for right in SUITS:
                key = prefix + left + "_ge_" + right
                if left != right and previous.get(key, False) and not parameters.get(key, False):
                    raise SystemError(f"Public suit relation weakened: {key}.")
        minimum, maximum = shape_range(*suit_constraints(parameters, prefix))
        for index, suit in enumerate(SUITS):
            parameters[prefix + suit + "_min"] = minimum[index]
            parameters[prefix + suit + "_max"] = maximum[index]
