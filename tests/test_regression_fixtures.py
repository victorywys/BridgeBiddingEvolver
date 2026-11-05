import unittest

from bridge_evolve.systems.inputs import parse_board
from regression_fixtures import V4_HASHES, frozen_v4, regression_cases


class RegressionFixtureTests(unittest.TestCase):
    def test_consumed_boards_are_complete_and_do_not_need_experiment_archives(self):
        sizes = {"weak_minor_exits": 4, "late_strong_club_double": 3,
                 "artificial_splinter_3nt": 1, "minor_fit_reselection": 2}
        for group, count in sizes.items():
            cases = regression_cases(group)
            self.assertEqual(len(cases), count)
            for case in cases:
                with self.subTest(group=group, index=case["source_index"]):
                    board = parse_board(case["board"])
                    self.assertEqual(len(board.deal.north.cards), 13)

    def test_regression_cases_do_not_share_mutable_state(self):
        cases = regression_cases("weak_minor_exits")
        cases[0]["board"]["dealer"] = "W"
        self.assertEqual(regression_cases("weak_minor_exits")[0]["board"]["dealer"], "E")

    def test_frozen_definitions_retain_the_original_semantic_hashes(self):
        for name, expected in V4_HASHES.items():
            with self.subTest(system=name):
                self.assertEqual(frozen_v4(name).content_hash, expected)


if __name__ == "__main__":
    unittest.main()
