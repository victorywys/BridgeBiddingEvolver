import unittest
from concurrent.futures import ThreadPoolExecutor

from bridge_evolve.core import Seat, Vulnerability
from bridge_evolve.oracle import DDSAdapter, DDTable, deal_from_pbn
from test_oracle import PUBLISHED_FIXTURES


class DDSParTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = DDSAdapter()

    def test_upstream_published_dealer_par_scores_and_sacrifices(self):
        for index, dealer, vulnerability, expected, strain, level, doubling, tricks in (
            (0, Seat.NORTH, Vulnerability.NONE, -110, "S", 2, 1, 8),
            (1, Seat.EAST, Vulnerability.NS, 100, "S", 4, 2, 9),
            (2, Seat.NORTH, Vulnerability.NONE, -300, "H", 5, 2, 9),
        ):
            table = DDTable(PUBLISHED_FIXTURES[index][1])
            result = self.adapter.par(table, dealer, vulnerability)
            self.assertEqual(result["score_ns"], expected)
            self.assertEqual(len(result["contracts"]), 1)
            contract = result["contracts"][0]
            self.assertEqual((contract["strain"], contract["level"], contract["doubling"], contract["tricks"]),
                             (strain, level, doubling, tricks))

    def test_all_vulnerabilities_dealers_and_thread_serialization(self):
        table = self.adapter.solve(deal_from_pbn(PUBLISHED_FIXTURES[0][0]))
        cases = [(dealer, vulnerability) for dealer in Seat for vulnerability in Vulnerability]
        with ThreadPoolExecutor(max_workers=4) as executor:
            actual = list(executor.map(lambda pair: self.adapter.par(table, *pair), cases))
        expected = [self.adapter.par(table, *pair) for pair in cases]
        self.assertEqual(actual, expected)

    def test_makeable_levels_convert_all_twenty_entries_and_preserve_zero(self):
        table = DDTable(((0, 6, 7, 12, 13),) * 4)
        levels = table.makeable_levels()
        self.assertEqual(levels["N"], {"C": 0, "D": 0, "H": 1, "S": 6, "NT": 7})
        self.assertEqual(sum(len(row) for row in levels.values()), 20)
        diagnostics = self.adapter.diagnostics(DDTable(PUBLISHED_FIXTURES[0][1]), Seat.NORTH, Vulnerability.NONE)
        self.assertEqual(diagnostics["par"]["score_ns"], -110)
        self.assertEqual(diagnostics["makeable_levels"]["E"]["S"], 2)

    def test_all_pass_par_is_explicit_and_input_types_are_strict(self):
        result = self.adapter.par(DDTable(((6,) * 5,) * 4), Seat.WEST, Vulnerability.BOTH)
        self.assertEqual(result["score_ns"], 0)
        self.assertTrue(result["passed_out"])
        self.assertEqual(result["contracts"], [])
        with self.assertRaises(TypeError):
            self.adapter.par(DDTable(((6,) * 5,) * 4), "N", Vulnerability.NONE)


if __name__ == "__main__":
    unittest.main()
