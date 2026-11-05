import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest.mock import patch

from test_board_viewer import ROOT, viewer


class ParallelAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        directory = self.project / "systems"
        directory.mkdir()
        for name in ("general-v2.json", "pass-v2.json"):
            (directory / name).write_bytes((ROOT / "systems" / name).read_bytes())
        self.state = viewer.ViewerState(self.project)
        self.addCleanup(self.state.executor.shutdown, wait=True)
        self.a, self.b = self.state.systems

    def request(self, **updates):
        return {"count": 4, "seed": 20261102, "system_a": self.a, "system_b": self.b, **updates}

    def finish(self, job):
        self.state.executor.submit(lambda: None).result(timeout=180)
        return self.state.audit_job(job["id"])

    def test_native_dds_starts_on_a_distinct_thread_before_bidding_finishes(self):
        dds_started, auction_started = threading.Event(), threading.Event()
        original_solve, original_auction = viewer.DDSAdapter.solve, viewer.run_auction
        threads = {}

        def solve(adapter, deal):
            threads["dds"] = threading.get_ident()
            dds_started.set()
            if not auction_started.wait(timeout=5):
                raise AssertionError("Bidding was serialized behind DDS.")
            return original_solve(adapter, deal)

        def auction(*args, **kwargs):
            threads["auction"] = threading.get_ident()
            if not dds_started.wait(timeout=5):
                raise AssertionError("DDS was not started alongside bidding.")
            auction_started.set()
            return original_auction(*args, **kwargs)

        raw = next(viewer.sampled_boards(viewer.sampling_config(self.request(count=1))))
        with patch.object(viewer.DDSAdapter, "solve", solve), patch.object(viewer, "run_auction", auction):
            result = self.state.compare({"board": raw["board"], "system_a": self.a, "system_b": self.b})
        self.assertNotEqual(threads["dds"], threads["auction"])
        self.assertEqual(result["oracle"]["oracle_kind"], "dds")
        self.assertIsInstance(result["record"]["imp"], int)

    def test_spawned_workers_match_every_serial_record_and_preserve_input_order(self):
        observed = []

        class ObservedPool(ProcessPoolExecutor):
            def shutdown(self, *args, **kwargs):
                if self._processes is not None:
                    observed.extend(self._processes)
                return super().shutdown(*args, **kwargs)

        serial = self.finish(self.state.start_batch(self.request(workers=1)))
        with patch.object(viewer, "ProcessPoolExecutor", ObservedPool):
            parallel = self.finish(self.state.start_batch(self.request(workers=2)))
        self.assertEqual((serial["status"], parallel["status"]), ("completed", "completed"))
        self.assertEqual(len(set(observed)), 2)
        left, right = self.state.bundle(serial["id"]), self.state.bundle(parallel["id"])
        self.assertEqual([row["number"] for row in right["boards"]], [1, 2, 3, 4])
        for before, after in zip(left["boards"], right["boards"], strict=True):
            for key in ("hands", "dealer", "vulnerability", "tables", "dds", "imp", "point_difference"):
                with self.subTest(board=after["number"], key=key):
                    self.assertEqual(before[key], after[key])
            self.assertEqual({key: value for key, value in before["audit_result"].items() if key != "dds_verification"},
                             {key: value for key, value in after["audit_result"].items() if key != "dds_verification"})
        report = self.state.audit_report(parallel["id"])
        self.assertEqual(report["execution"], {"workers": 2, "dds_thread_overlap": True,
                         "start_method": "spawn", "max_in_flight": 4})
        self.assertEqual(report["summary"], self.state.audit_report(serial["id"])["summary"])
        self.assertEqual(report["metrics"]["boards"], 4)
        self.assertEqual(report["metrics"]["tables"], 8)
        self.assertEqual(report["metrics"]["dds_cache_hits"], 4)
        self.assertEqual(report["metrics"]["dds_cache_solves"], 0)
        self.assertEqual(report["metrics"]["fresh_dds_crosschecks"], 4)
        self.assertTrue(all(check["status"] == "passed" for row in right["boards"] for check in row["audit_result"]["checks"]
                            if check["id"].endswith("-determinism") or check["id"] == "fresh-dds"))

    def test_spawned_board_error_does_not_abort_later_deals_or_supply_zero_imp(self):
        request = self.request(dealer="N", workers=2)
        raws = list(viewer.sampled_boards(viewer.sampling_config(request)))
        first_hcp = viewer.hand_summary(raws[0]["board"])["hand_summary"]["N"]["hcp"]
        raw = copy.deepcopy(self.state.systems[self.b]["system"].to_data())
        raw["name"] = "Fixture selective policy failure"
        raw["rules"] = [{"id": "selective-error", "priority": 1,
                         "when": {"op": "and", "args": [
                             {"op": "eq", "args": [{"op": "role"}, "dealer"]},
                             {"op": "eq", "args": [{"op": "hcp"}, first_hcp]}]},
                         "action": {"kind": "error", "message": "fixture per-board worker error"}}]
        system = viewer.validate_system(raw)
        path = self.project / "systems/selective-error.json"
        viewer.save_document(path, system.to_data())
        self.state.systems[system.content_hash] = {"system": system, "path": path}
        job = self.finish(self.state.start_batch({**request, "system_a": system.content_hash}))
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["completed_boards"], 4)
        rows = self.state.bundle(job["id"])["boards"]
        self.assertEqual(rows[0]["audit_result"]["status"], "failed")
        self.assertIsNone(rows[0]["imp"])
        self.assertIn("fixture per-board worker error", rows[0]["audit_result"]["errors"][0]["message"])
        self.assertTrue(any(row["audit_result"]["status"] == "checked" for row in rows[1:]))
        self.assertEqual(job["valid_boards"] + job["failed_boards"], 4)

    def test_controller_cancellation_cancels_bounded_pending_work(self):
        submitted = threading.Event()
        cancelled = threading.Event()
        pools = []

        class PendingPool:
            def __init__(self, **options):
                self.options = options
                self.futures = []
                self.shutdown_options = None
                pools.append(self)

            def submit(self, *args):
                future = Future()
                self.futures.append(future)
                if len(self.futures) == 4:
                    submitted.set()
                return future

            def shutdown(self, **options):
                self.shutdown_options = options

        request = {**self.request(count=20), "workers": 2}
        generated = list(viewer.sampled_boards(viewer.sampling_config(request)))
        definitions = {identity: entry["system"] for identity, entry in self.state.systems.items()}
        result = []

        def consume():
            try:
                list(self.state.batch_results(generated, request, definitions, self.project, cancelled.is_set))
            except viewer.AuditCancelled:
                result.append("cancelled")

        with patch.object(viewer, "ProcessPoolExecutor", PendingPool):
            worker = threading.Thread(target=consume)
            worker.start()
            self.assertTrue(submitted.wait(timeout=5))
            cancelled.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["cancelled"])
        self.assertEqual(len(pools[0].futures), 4)
        self.assertTrue(all(future.cancelled() for future in pools[0].futures))
        self.assertEqual(pools[0].options["mp_context"].get_start_method(), "spawn")
        self.assertEqual(pools[0].shutdown_options, {"wait": True, "cancel_futures": True})

    def test_worker_process_crash_is_a_failed_task_not_a_fake_board_result(self):
        class CrashedPool:
            def __init__(self, **options):
                pass

            def submit(self, *args):
                future = Future()
                future.set_exception(BrokenProcessPool("fixture worker crash"))
                return future

            def shutdown(self, **options):
                pass

        with patch.object(viewer, "ProcessPoolExecutor", CrashedPool):
            with self.assertLogs("bridge.board_viewer", level="ERROR"):
                job = self.finish(self.state.start_batch(self.request(workers=2)))
        self.assertEqual((job["status"], job["completed_boards"]), ("failed", 0))
        report = self.state.audit_report(job["id"])
        self.assertIn("fixture worker crash", report["error"])
        self.assertEqual(report["board_failures"], [])
        self.assertNotIn(job["id"], [run["id"] for run in self.state.catalog()["runs"]])

    def test_workers_are_validated_bounded_and_frozen_into_the_input(self):
        with patch.object(viewer.os, "cpu_count", return_value=8):
            self.assertEqual(viewer.sampling_config(self.request(count=63))["workers"], 1)
            self.assertEqual(viewer.sampling_config(self.request(count=64))["workers"], 2)
        self.assertEqual(viewer.sampling_config(self.request(count=1, workers=4))["workers"], 1)
        for value in (True, 0, 5, "2", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                viewer.sampling_config(self.request(workers=value))
        job = self.finish(self.state.start_batch(self.request(count=1, workers=1)))
        saved = json.loads((self.state.workspace / "audits" / job["id"] / "input.json").read_text())
        self.assertEqual((job["workers"], saved["workers"]), (1, 1))
