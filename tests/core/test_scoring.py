import unittest

from bridge_evolve.core.auction import Contract
from bridge_evolve.core.scoring import score_contract, score_ns, signed_imp
from bridge_evolve.core.types import Seat, Strain, Vulnerability


class DuplicateScoringTests(unittest.TestCase):
    def test_fixed_scoring_cases_for_m1_acceptance(self) -> None:
        cases = (
            (Contract(4, Strain.SPADES, Seat.NORTH), 10, 420, 620),
            (Contract(3, Strain.NOTRUMP, Seat.NORTH), 9, 400, 600),
            (Contract(2, Strain.HEARTS, Seat.NORTH), 10, 170, 170),
            (Contract(2, Strain.HEARTS, Seat.NORTH, doubling=2), 8, 470, 670),
            (Contract(4, Strain.SPADES, Seat.NORTH, doubling=2), 7, -500, -800),
            (Contract(4, Strain.SPADES, Seat.NORTH, doubling=4), 7, -1000, -1600),
            (Contract(6, Strain.NOTRUMP, Seat.NORTH), 12, 990, 1440),
            (Contract(7, Strain.NOTRUMP, Seat.NORTH), 13, 1520, 2220),
        )
        for contract, tricks, not_vul, vul in cases:
            with self.subTest(contract=contract, vulnerable=False):
                self.assertEqual(
                    score_contract(contract, tricks=tricks, declarer_vulnerable=False), not_vul
                )
            with self.subTest(contract=contract, vulnerable=True):
                self.assertEqual(
                    score_contract(contract, tricks=tricks, declarer_vulnerable=True), vul
                )

    def test_ns_and_ew_scores_are_exact_opposites(self) -> None:
        contract_ns = Contract(level=4, strain=Strain.SPADES, declarer=Seat.NORTH)
        contract_ew = Contract(level=4, strain=Strain.SPADES, declarer=Seat.EAST)
        ns_score = score_ns(contract_ns, tricks=10, vulnerability=Vulnerability.NONE)
        ew_score = score_ns(contract_ew, tricks=10, vulnerability=Vulnerability.NONE)
        self.assertEqual(ns_score, -ew_score)


class SignedImpTests(unittest.TestCase):
    def test_imp_boundaries_and_sign(self) -> None:
        boundaries = (
            (0, 0),
            (19, 0),
            (20, 1),
            (49, 1),
            (50, 2),
            (89, 2),
            (90, 3),
            (129, 3),
            (130, 4),
            (169, 4),
            (170, 5),
            (219, 5),
            (220, 6),
            (269, 6),
            (270, 7),
            (319, 7),
            (320, 8),
            (369, 8),
            (370, 9),
            (429, 9),
            (430, 10),
            (499, 10),
            (500, 11),
            (599, 11),
            (600, 12),
            (749, 12),
            (750, 13),
            (899, 13),
            (900, 14),
            (1099, 14),
            (1100, 15),
            (1299, 15),
            (1300, 16),
            (1499, 16),
            (1500, 17),
            (1749, 17),
            (1750, 18),
            (1999, 18),
            (2000, 19),
            (2249, 19),
            (2250, 20),
            (2499, 20),
            (2500, 21),
            (2999, 21),
            (3000, 22),
            (3499, 22),
            (3500, 23),
            (3999, 23),
            (4000, 24),
            (5000, 24),
        )
        for point_diff, expected in boundaries:
            with self.subTest(point_diff=point_diff):
                self.assertEqual(signed_imp(point_diff), expected)
                self.assertEqual(signed_imp(-point_diff), -expected)

    def test_signed_imp_is_odd_function(self) -> None:
        for value in range(-5000, 5001, 37):
            with self.subTest(value=value):
                self.assertEqual(signed_imp(-value), -signed_imp(value))


if __name__ == "__main__":
    unittest.main()

