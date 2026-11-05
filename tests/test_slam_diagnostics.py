import copy
import unittest

from bridge_evolve.evaluation.slams import summarize_games, summarize_slams, table_slam_diagnostics
from bridge_evolve.oracle import DDSAdapter, DDTable
from bridge_evolve.oracle.diagnostics import validate_diagnostics
from bridge_evolve.core import Seat, Vulnerability
from test_oracle import PUBLISHED_FIXTURES


class SlamDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.table = DDTable(((6, 12, 12, 13, 11), (7, 7, 7, 7, 7), (6, 12, 12, 13, 11), (7, 7, 7, 7, 7)))

    def played(self, level=4, strain="S", declarer="N", tricks=13):
        return {"id": "table1", "systems": {"NS": {"hash": "a"}, "EW": {"hash": "b"}},
                "contract": {"level": level, "strain": strain, "declarer": declarer, "doubling": 1},
                "tricks": tricks, "score_ns": None}

    def test_small_and_grand_opportunities_do_not_count_as_called(self):
        result = table_slam_diagnostics(self.played(), self.table)
        self.assertFalse(result["called"])
        self.assertEqual(result["small_not_bid"], ["NS"])
        self.assertEqual(result["grand_not_bid"], ["NS"])
        result = table_slam_diagnostics(self.played(6), self.table)
        self.assertTrue(result["made"])
        self.assertEqual(result["small_not_bid"], [])
        self.assertEqual(result["grand_not_bid"], ["NS"])
        self.assertEqual(table_slam_diagnostics(self.played(7), self.table)["grand_not_bid"], [])

    def test_failed_slam_with_a_makeable_alternative_is_not_an_uncalled_slam(self):
        result = table_slam_diagnostics(self.played(6, "NT", tricks=11), self.table)
        self.assertTrue(result["called"])
        self.assertFalse(result["made"])
        self.assertEqual(result["small_not_bid"], [])
        self.assertEqual(result["small_wrong_contract"], ["NS"])

    def test_opponent_declares_and_passed_out_do_not_hide_available_slams(self):
        result = table_slam_diagnostics(self.played(3, "NT", "E", 7), self.table)
        self.assertEqual(result["small_not_bid"], ["NS"])
        result = table_slam_diagnostics({"contract": {"passed_out": True}, "tricks": None}, self.table)
        self.assertEqual(result["grand_not_bid"], ["NS"])

    def test_summary_separates_table_counts_partnership_opportunities_and_families(self):
        first = self.played(6)
        second = self.played()
        second.update(id="table2", systems={"NS": {"hash": "b"}, "EW": {"hash": "a"}})
        record = {"number": 1, "index": 0, "family_id": "family", "dds": {"table": self.table.to_dict()},
                  "tables": [first, second]}
        summary, cases = summarize_slams([record], {"a": "A", "b": "B"})
        self.assertEqual(summary["called"], {"small": 1, "grand": 0, "total": 1})
        self.assertEqual(summary["made"]["total"], 1)
        self.assertEqual(summary["unique_families_with_small_slam"], 1)
        self.assertEqual(summary["opportunities"]["small_available"], 2)
        self.assertEqual(summary["opportunities"]["small_not_bid"], 1)
        self.assertEqual(summary["opportunities"]["grand_not_bid"], 2)
        self.assertEqual(len(cases), 3)

    def test_strict_dds_payload_rejects_changed_levels_par_score_and_context(self):
        table = DDTable(PUBLISHED_FIXTURES[0][1])
        diagnostics = DDSAdapter().diagnostics(table, Seat.NORTH, Vulnerability.NONE)
        self.assertEqual(validate_diagnostics(diagnostics, "N", "none"), diagnostics)
        for field in ("level", "score", "dealer", "vulnerability"):
            altered = copy.deepcopy(diagnostics)
            if field == "level":
                altered["makeable_levels"]["N"]["C"] = 7
            elif field == "score":
                altered["par"]["score_ns"] = 0
            else:
                altered[field] = "W" if field == "dealer" else "both"
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_diagnostics(altered, "N", "none")

    def test_game_diagnostics_separate_partscore_misses_from_failed_game_contracts(self):
        first = self.played(2)
        first["auction"] = [{"public_context": {"forcing": "none"}, "public_context_after": {"forcing": "invite_game"}}]
        second = self.played(3, "NT", tricks=8)
        second.update(id="table2", systems={"NS": {"hash": "b"}, "EW": {"hash": "a"}}, auction=[])
        record = {"number": 1, "index": 0, "family_id": "family", "dds": {"table": self.table.to_dict()},
                  "tables": [first, second]}
        summary, cases = summarize_games([record], {"a": "A", "b": "B"})
        self.assertEqual((summary["called"], summary["made"], summary["failed"]), (1, 0, 1))
        self.assertEqual(summary["opportunities"], {"available": 2, "not_bid": 1, "wrong_contract": 1})
        self.assertEqual(summary["unique_families_with_game"], 1)
        self.assertEqual(summary["signals"]["game_invitations"], 1)
        self.assertEqual({item["kind"] for item in cases}, {"not_bid", "wrong_contract"})


if __name__ == "__main__":
    unittest.main()
