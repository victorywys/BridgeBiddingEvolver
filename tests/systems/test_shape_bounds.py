import unittest

from bridge_evolve.core import Seat, Vulnerability
from bridge_evolve.systems import validate_system
from bridge_evolve.systems.language import SystemError, parse_call
from bridge_evolve.systems.shape_bounds import SHAPES, SUITS, shape_range
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld
from systems.test_v2 import ctx, op, raw_system, transition


class ShapeBoundsTests(unittest.TestCase):
    def test_all_560_physical_shapes_and_unknown_bounds(self):
        self.assertEqual(len(SHAPES), 560)
        self.assertEqual(len(set(SHAPES)), 560)
        self.assertTrue(all(sum(shape) == 13 for shape in SHAPES))
        self.assertEqual(shape_range((0,) * 4, (13,) * 4), ((0,) * 4, (13,) * 4))

    def test_order_and_thirteen_cards_propagate_both_directions(self):
        self.assertEqual(shape_range((5, 4, 0, 0), (13,) * 4, ((0, 1),)),
                         ((5, 4, 0, 0), (9, 6, 4, 4)))
        self.assertEqual(shape_range((5, 6, 0, 0), (13,) * 4, ((0, 1),)),
                         ((6, 6, 0, 0), (7, 6, 1, 1)))

    def test_opposite_orders_mean_equality_not_an_arbitrary_tie(self):
        low, high = shape_range((5, 4, 0, 0), (13,) * 4, ((0, 1), (1, 0)))
        self.assertEqual(low, (5, 5, 0, 0))
        self.assertEqual(high, (6, 6, 3, 3))

    def test_impossible_intervals_and_shapes_are_explicit_errors(self):
        for low, high, relations in (
            ((7, 7, 0, 0), (13,) * 4, ()),
            ((5, 4, 0, 0), (4, 13, 13, 13), ()),
            ((4, 0, 4, 4), (13, 1, 13, 13), ((1, 0),)),
        ):
            with self.subTest(low=low), self.assertRaises(SystemError):
                shape_range(low, high, relations)

    def shape_system(self, second):
        raw = raw_system()
        parameters = raw["protocol"]["parameters"]
        parameters["captain"] = {"type": "string", "initial": "N"}
        parameters["captain_S_ge_H"] = {"type": "bool", "initial": True}
        for suit in SUITS:
            for ending, initial in (("min", 0), ("max", 13)):
                parameters[f"captain_{suit}_{ending}"] = {"type": "number", "initial": initial}
        raw["protocol"]["states"]["idle"]["transitions"] = [
            transition("open", op("eq", ctx("event.call"), "1S"),
                       set={"captain_S_min": 5, "captain_H_min": 4}),
            transition("tighten", op("eq", ctx("event.call"), "2C"), set=second),
        ]
        return validate_system(raw)

    def test_public_reconstruction_propagates_and_persists_without_a_hand(self):
        world = PublicWorld({"NS": self.shape_system({"captain_H_min": 6})}, Seat.NORTH, Vulnerability.NONE)
        for call in ("1S", "P", "2C"):
            world.step(parse_call(call))
        values = dict(PublicContext(world, "NS", Seat.NORTH).snapshot())
        self.assertEqual(values["var.captain_S_min"], 6)
        self.assertEqual(values["var.captain_H_max"], 6)
        self.assertEqual(values["partner_S_max"], 13)
        self.assertEqual(PublicContext(world, "NS", Seat.SOUTH).value("partner_S_max"), 7)
        self.assertEqual(values["partnership_S_max"], 13)

    def test_public_intervals_and_relations_cannot_be_forgotten(self):
        for second in ({"captain_S_min": 4}, {"captain_H_max": 13}, {"captain_S_ge_H": False}):
            with self.subTest(second=second):
                world = PublicWorld({"NS": self.shape_system(second)}, Seat.NORTH, Vulnerability.NONE)
                for call in ("1S", "P"):
                    world.step(parse_call(call))
                with self.assertRaisesRegex(SystemError, "weakened"):
                    world.step(parse_call("2C"))

    def test_player_relative_upper_bounds_are_not_public_transition_inputs(self):
        raw = raw_system()
        raw["protocol"]["states"]["idle"]["transitions"] = [
            transition("invalid", op("ge", ctx("partner_S_max"), 0)),
        ]
        with self.assertRaisesRegex(SystemError, "not available"):
            validate_system(raw)
