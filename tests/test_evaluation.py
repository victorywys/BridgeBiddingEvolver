import hashlib
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bridge_evolve.core import Deal, Hand, Seat, Strain, Vulnerability, full_deck
from bridge_evolve.evaluation.cache import DDSCache
from bridge_evolve.evaluation.data import (
    BoardRecord, board_data, family_hash, generate_dataset, load_dataset,
)
from bridge_evolve.evaluation.experiment import evaluate, paired_board, table_score
from bridge_evolve.evaluation.statistics import paired_summary
from bridge_evolve.oracle import DDTable, DDSAdapter, DDSUnavailableError
from bridge_evolve.systems import load_system, validate_system
from bridge_evolve.systems.inputs import Board


class DatasetTests(unittest.TestCase):
    def test_unconditional_generation_is_reproducible_complete_and_balanced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = generate_dataset(root / "a", count=32, seed=2026, split="test")
            b = generate_dataset(root / "b", count=32, seed=2026, split="test")
            manifest, records = load_dataset(a)
            other, repeated = load_dataset(b)
            self.assertEqual(manifest, other)
            self.assertEqual(records, repeated)
            self.assertEqual(len(records), 32)
            self.assertEqual(set(manifest["distribution"].values()), {2})
            for record in records:
                cards = [card for seat in Seat for card in record.board.deal.hand(seat).cards]
                self.assertEqual(len(set(cards)), 52)
            with self.assertRaises(FileExistsError):
                generate_dataset(root / "a", count=32, seed=2026, split="test")

    def test_dataset_corruption_is_detected_and_rotations_share_a_family(self):
        deck = full_deck()
        deal = Deal(*(Hand(deck[start:start + 13]) for start in (0, 13, 26, 39)))
        rotated = Deal(deal.west, deal.north, deal.east, deal.south)
        self.assertEqual(family_hash(deal), family_hash(rotated))
        with tempfile.TemporaryDirectory() as directory:
            manifest = generate_dataset(Path(directory) / "data", count=2, seed=1, split="train")
            path = manifest.parent / "boards.jsonl"
            path.write_text(path.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "hash"):
                load_dataset(manifest)

    def test_consumed_test_data_cannot_be_reused_as_an_independent_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = generate_dataset(root / "data", count=1, seed=1, split="test")
            (dataset.parent / "consumed.json").write_text('{"reason":"inspected"}')
            config = {
                "experiment_id": "must-reject", "dataset": str(dataset),
                "system_a": "unread-a.json", "system_b": "unread-b.json",
                "dds_cache": "unused.sqlite", "output": str(root / "run"),
                "bootstrap_seed": 1, "bootstrap_resamples": 100,
            }
            path = root / "config.json"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "consumed"):
                evaluate(path)
            self.assertFalse((root / "run").exists())

    def test_changed_translation_assumptions_are_rejected_before_a_formal_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = generate_dataset(root / "data", count=1, seed=1, split="train")
            system = Path(__file__).resolve().parents[1] / "systems/pass-v2.json"
            content_hash = load_system(system).content_hash
            (root / "translation.json").write_text('{}')
            (root / "migration.json").write_text(json.dumps({
                "systems": {"ccba": content_hash, "prec": content_hash},
                "translation_manifest": "translation.json", "translation_sha256": "0" * 64,
            }))
            config = {
                "experiment_id": "must-reject-changed-translation", "dataset": str(dataset),
                "system_a": str(system), "system_b": str(system), "dds_cache": str(root / "cache.sqlite"),
                "output": str(root / "run"), "bootstrap_seed": 1, "bootstrap_resamples": 100,
                "migration_manifest": "migration.json",
            }
            path = root / "config.json"
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "Translation manifest content hash"):
                evaluate(path)
            self.assertFalse((root / "run").exists())


class PairedScoringTests(unittest.TestCase):
    def setUp(self):
        deck = full_deck()
        self.board = Board(Deal(*(Hand(deck[start:start + 13]) for start in (0, 13, 26, 39))),
                           Seat.NORTH, Vulnerability.NONE)

    def test_real_declarer_axis_doubling_and_ns_view_are_used(self):
        values = ((7, 8, 9, 10, 11), (6, 7, 8, 9, 10),
                  (5, 6, 7, 8, 9), (4, 5, 6, 7, 8))
        table = DDTable(values)
        result = {"contract": {"level": 3, "strain": "NT", "declarer": "E", "doubling": 1}}
        self.assertEqual(table_score(result, self.board, table), (-430, 10))
        result = {"contract": {"level": 2, "strain": "H", "declarer": "N", "doubling": 2}}
        self.assertEqual(table_score(result, self.board, table), (570, 9))
        self.assertEqual(table_score({"contract": {"passed_out": True}}, self.board, table), (0, None))

    def test_two_tables_form_one_sample_and_swapping_systems_negates_imp(self):
        table = DDTable(((10, 10, 10, 10, 10),) * 4)
        a = {"contract": {"level": 4, "strain": "S", "declarer": "N", "doubling": 1}, "auction": []}
        b = {"contract": {"level": 2, "strain": "H", "declarer": "N", "doubling": 1}, "auction": []}
        with patch("bridge_evolve.evaluation.experiment.run_auction", side_effect=[a, b]):
            result = paired_board("A", "B", self.board, table)
        self.assertEqual(result["point_difference_a"], 250)
        self.assertEqual(result["imp_a"], 6)
        with patch("bridge_evolve.evaluation.experiment.run_auction", side_effect=[b, a]):
            reversed_result = paired_board("B", "A", self.board, table)
        self.assertEqual(reversed_result["imp_a"], -result["imp_a"])

    def test_bootstrap_is_paired_deterministic_and_does_not_double_sample_size(self):
        a = paired_summary([0, 6, -6, 0], seed=10, resamples=200)
        self.assertEqual(a, paired_summary([0, 6, -6, 0], seed=10, resamples=200))
        self.assertEqual((a["boards"], a["tables"], a["mean_imp"]), (4, 8, 0))
        self.assertLess(a["ci95"][0], 0)
        self.assertGreater(a["ci95"][1], 0)
        self.assertEqual(paired_summary([0] * 10, seed=1, resamples=100)["ci95"], [0, 0])

    def test_fixture_provenance_cannot_create_a_formal_cache(self):
        class Provenance:
            def to_dict(self):
                return {"oracle_kind": "fixture"}

        class FixtureAdapter:
            provenance = Provenance()

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "fixture"):
                DDSCache(Path(directory) / "cache.sqlite", FixtureAdapter())

    def test_cache_hit_equals_a_fresh_real_solve_on_an_independently_exact_deal(self):
        try:
            adapter = DDSAdapter()
        except DDSUnavailableError:
            if os.environ.get("BRIDGE_DDS_REQUIRED") == "1":
                raise
            self.skipTest("Real DDS is unavailable; mandatory-real host run separately required.")
        # Each player holds all thirteen cards of one suit. NS can make all
        # tricks with C/H trumps, EW with D/S; the opening leader cashes 13 at NT.
        expected = DDTable(((13, 0, 13, 0, 0), (0, 13, 0, 13, 0),
                            (13, 0, 13, 0, 0), (0, 13, 0, 13, 0)))
        with tempfile.TemporaryDirectory() as directory:
            with DDSCache(Path(directory) / "tables.sqlite", adapter) as cache:
                actual = cache.solve(self.board.deal)
                self.assertEqual(actual, expected)
                self.assertEqual(cache.solve(self.board.deal), actual)
                self.assertEqual(adapter.solve(self.board.deal), actual)
                self.assertEqual((cache.solves, cache.hits), (1, 1))

    def test_concurrent_identical_real_solves_commit_one_consistent_cache_entry(self):
        try:
            adapter = DDSAdapter()
        except DDSUnavailableError:
            if os.environ.get("BRIDGE_DDS_REQUIRED") == "1":
                raise
            self.skipTest("Real DDS is unavailable; mandatory-real host run separately required.")
        expected = adapter.solve(self.board.deal)
        barrier = threading.Barrier(2)
        original = adapter.solve

        def simultaneous_miss(deal):
            barrier.wait(timeout=5)
            return original(deal)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tables.sqlite"

            def solve():
                with DDSCache(path, adapter) as cache:
                    return cache.solve(self.board.deal), cache.solves

            with patch.object(adapter, "solve", side_effect=simultaneous_miss):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(solve) for _ in range(2)]
                    results = [future.result(timeout=15) for future in futures]
            self.assertEqual(results, [(expected, 1), (expected, 1)])
            with DDSCache(path, adapter) as cache:
                self.assertEqual(cache.connection.execute("SELECT COUNT(*) FROM dd_tables").fetchone()[0], 1)
                self.assertEqual(cache.solve(self.board.deal), expected)
                self.assertEqual(cache.hits, 1)

    def test_failed_board_is_recorded_and_never_replaced_with_pass_or_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = generate_dataset(root / "data", count=2, seed=1, split="train")
            raw = {
                "schema_version": 2, "operator_version": 2, "name": "test pass", "scope": "general",
                "features": {}, "announcements": [], "rules": [],
                "protocol": {"initial": "idle", "parameters": {},
                             "states": {"idle": {"transitions": [], "otherwise": "idle"}}},
                "fallback": {"kind": "call", "call": "P"},
            }
            system = root / "system.json"
            system.write_text(json.dumps(raw))
            config = {
                "experiment_id": "failure-test", "dataset": str(dataset),
                "system_a": str(system), "system_b": str(system), "dds_cache": str(root / "cache.sqlite"),
                "output": str(root / "run"), "bootstrap_seed": 1, "bootstrap_resamples": 100,
            }
            path = root / "config.json"
            path.write_text(json.dumps(config))

            class Provenance:
                def to_dict(self):
                    return {"oracle_kind": "dds", "version": "test-only", "source_commit": "not-real"}

            class BrokenAdapter:
                provenance = Provenance()

                def solve(self, deal):
                    raise RuntimeError("deliberate native solver failure")

            with patch("bridge_evolve.evaluation.experiment.DDSAdapter", return_value=BrokenAdapter()):
                with self.assertRaisesRegex(RuntimeError, "deliberate"):
                    evaluate(path)
            run = json.loads((root / "run/run.json").read_text())
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["completed_boards"], 0)
            self.assertEqual(run["errors"][0]["board"], 0)
            self.assertFalse((root / "run/summary.json").exists())


if __name__ == "__main__":
    unittest.main()
