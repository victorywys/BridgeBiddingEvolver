import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_board_viewer import BOARD, ROOT, viewer, write_smoke_run


class LibraryAndLazyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        (self.project / "systems").mkdir()
        for name in ("general-v2.json", "pass-v2.json"):
            (self.project / "systems" / name).write_bytes((ROOT / "systems" / name).read_bytes())
        self.state = viewer.ViewerState(self.project)
        self.addCleanup(self.state.executor.shutdown, wait=True)
        self.a, self.b = self.state.systems

    def batch(self, **updates):
        job = self.state.start_batch({"count": 2, "seed": 20261102, "system_a": self.a, "system_b": self.b, **updates})
        self.state.executor.submit(lambda: None).result(timeout=60)
        result = self.state.audit_job(job["id"])
        self.assertEqual(result["status"], "completed", result)
        return result

    def test_rename_category_trash_and_restore_persist_without_touching_evidence(self):
        job = self.batch()
        directory = self.state.workspace / "audits" / job["id"]
        hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir() if path.is_file()}
        index = self.state.run_index(job["id"])
        self.state.organize_library({"ids": [job["id"]], "action": "update", "name": '满贯专项 <fixture>', "category": "测试"})
        entry = next(row for row in self.state.library_entries()["entries"] if row["id"] == job["id"])
        self.assertEqual((entry["name"], entry["category"]), ('满贯专项 <fixture>', "测试"))
        self.assertEqual(self.state.run_index(job["id"])["name"], entry["name"])
        self.assertEqual(self.state.run_index(job["id"])["version"], index["version"])
        self.assertEqual(self.state.audit_history()["jobs"][0]["label"], entry["name"])
        self.state.organize_library({"ids": [job["id"]], "action": "trash", "confirm": True})
        self.assertNotIn(job["id"], [row["id"] for row in self.state.catalog()["runs"]])
        self.assertNotIn(job["id"], [row["id"] for row in self.state.audit_history()["jobs"]])
        for load in (self.state.bundle, self.state.run_index):
            with self.assertRaisesRegex(ValueError, "回收站"):
                load(job["id"])
        with self.assertRaisesRegex(ValueError, "回收站"):
            self.state.run_board(job["id"], index["boards"][0]["id"])
        restored = viewer.ViewerState(self.project)
        self.addCleanup(restored.executor.shutdown, wait=True)
        entry = next(row for row in restored.library_entries()["entries"] if row["id"] == job["id"])
        self.assertTrue(entry["trashed_at"])
        restored.organize_library({"ids": [job["id"]], "action": "restore"})
        self.assertEqual(restored.run_index(job["id"])["name"], '满贯专项 <fixture>')
        self.assertEqual(hashes, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in directory.iterdir() if path.is_file()})

    def test_bulk_classification_is_atomic_and_running_tasks_cannot_be_trashed(self):
        first, second = self.batch(), self.batch(seed=10)
        identifiers = [first["id"], second["id"]]
        self.state.organize_library({"ids": identifiers, "action": "update", "category": "待复查"})
        self.assertEqual({row["category"] for row in self.state.library_entries()["entries"]}, {"待复查"})
        with self.assertRaises(ValueError):
            self.state.organize_library({"ids": [first["id"], "../outside"], "action": "trash", "confirm": True})
        self.assertNotIn("trashed_at", self.state.library[first["id"]])
        self.state.jobs[second["id"]]["status"] = "running"
        with self.assertRaisesRegex(ValueError, "正在执行"):
            self.state.organize_library({"ids": identifiers, "action": "trash", "confirm": True})
        self.assertNotIn("trashed_at", self.state.library[first["id"]])
        self.state.jobs[second["id"]]["status"] = "completed"
        self.state.organize_library({"ids": identifiers, "action": "trash", "confirm": True})
        self.assertTrue(all(row["trashed_at"] for row in self.state.library_entries()["entries"]))
        self.state.organize_library({"ids": identifiers, "action": "restore"})
        self.assertTrue(all(not row["trashed_at"] for row in self.state.library_entries()["entries"]))

    def test_frozen_runs_can_be_organized_and_recovered_without_modifying_results(self):
        directory = self.project / "results/fixture-smoke-1"
        manifest = write_smoke_run(directory)
        self.state.runs[directory.name] = {"path": directory, "manifest": manifest}
        original = (directory / "run.json").read_bytes()
        self.state.organize_library({"ids": [directory.name], "action": "update", "category": "历史", "name": "旧版对照"})
        self.assertEqual(self.state.catalog()["runs"][0]["name"], "旧版对照")
        self.state.organize_library({"ids": [directory.name], "action": "trash", "confirm": True})
        self.assertIsNone(self.state.catalog()["default_run"])
        self.assertEqual(self.state.catalog()["runs"], [])
        self.state.organize_library({"ids": [directory.name], "action": "restore"})
        self.assertEqual((directory / "run.json").read_bytes(), original)
        self.assertEqual(self.state.catalog()["runs"][0]["category"], "历史")

    def test_invalid_management_and_malformed_persisted_metadata_are_explicit_errors(self):
        job = self.batch()
        invalid = [
            {"ids": [], "action": "trash", "confirm": True},
            {"ids": [job["id"]], "action": "trash"},
            {"ids": [job["id"], job["id"]], "action": "restore"},
            {"ids": [job["id"]], "action": "update", "name": ""},
            {"ids": [job["id"]], "action": "update", "category": 3},
            {"ids": [job["id"]], "action": "update", "path": "/tmp/outside"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.state.organize_library(payload)
        (self.state.workspace / "library.json").write_text('{"invalid": {"name": 3}}')
        with self.assertRaisesRegex(ValueError, "整理信息"):
            viewer.ViewerState(self.project)

    def test_lightweight_index_avoids_full_bundle_and_preserves_every_call_and_filter_field(self):
        job = self.batch()
        with patch.object(self.state, "bundle", side_effect=AssertionError("must not load full bundle")), \
                patch.object(viewer, "load_system", side_effect=AssertionError("must reuse registered definitions")):
            index = self.state.run_index(job["id"])
        full = self.state.bundle(job["id"])
        self.assertTrue(index["details_on_demand"])
        self.assertEqual(index["statistics"], full["statistics"])
        self.assertEqual(index["evaluation"]["summary"], full["evaluation"]["summary"])
        for small, complete in zip(index["boards"], full["boards"], strict=True):
            self.assertTrue(small["details_pending"])
            for key in ("hands", "hand_summary", "dealer", "vulnerability", "difference", "imp", "point_difference"):
                self.assertEqual(small[key], complete[key])
            self.assertNotIn("dds", small)
            for left, right in zip(small["tables"], complete["tables"], strict=True):
                self.assertEqual(left["systems"], right["systems"])
                self.assertEqual(left["slam_diagnostics"], right["slam_diagnostics"])
                self.assertEqual(left["auction"], [{key: step[key] for key in ("seat", "call")} for step in right["auction"]])
                self.assertTrue(all("public_context" not in step and "private_trace" not in step for step in left["auction"]))
                self.assertIn(right["auction"][0]["public_explanation"]["summary"], small["search_text"])
            detail = self.state.run_board(job["id"], small["id"], index["version"])
            self.assertEqual(detail["record"], complete)
        self.assertLess(len(json.dumps(index)), len(json.dumps(full)) * .5)

    def test_details_exports_and_human_reference_resolution_read_only_selected_boards(self):
        job = self.batch()
        index = self.state.run_index(job["id"])
        ids = [row["id"] for row in index["boards"]]
        full = self.state.bundle(job["id"])
        with patch.object(self.state, "bundle", side_effect=AssertionError("unexpected full reload")):
            record, source = self.state.resolve_review({"source_run": job["id"], "board_id": ids[1]})
            self.assertEqual(record, full["boards"][1])
            self.assertEqual(source["number"], 2)
            exported, _, _ = self.state.export_selected(job["id"], {"ids": ids, "version": index["version"],
                "format": "json", "reviews": {ids[1]: {"status": "flagged", "note": "人类标记"}}})
        decoded = json.loads(exported)
        self.assertEqual(len(decoded["boards"]), 2)
        self.assertTrue(decoded["boards"][0]["tables"][0]["auction"][0]["private_trace"])
        self.assertTrue(decoded["boards"][0]["dds"])
        self.assertEqual(decoded["boards"][1]["review"]["note"], "人类标记")
        pbn, _, _ = self.state.export_selected(job["id"], {"ids": ids, "version": index["version"], "format": "pbn"})
        self.assertEqual(len(viewer.import_file(pbn, "selection.pbn")["boards"]), 2)
        compact, _, _ = self.state.export_selected(job["id"], {"ids": ids, "version": index["version"], "format": "compact"})
        imported = viewer.import_file(compact, "selection.compact.jsonl",
                                      public_definitions={identity: value["system"] for identity, value in self.state.systems.items()})
        self.assertEqual([row["tables"] for row in imported["boards"]], [row["tables"] for row in full["boards"]])
        agent, _, _ = self.state.export_selected(job["id"], {"ids": ids, "version": index["version"], "format": "agent"})
        self.assertNotIn("public_context", agent.decode())
        with self.assertRaisesRegex(ValueError, "精简分析视图"):
            viewer.import_file(agent, "selection.agent.jsonl")
        archive, _, _ = self.state.audit_download(job["id"], "zip")
        restored = viewer.import_file(archive, "audit.zip")
        self.assertEqual([row["tables"] for row in restored["boards"]], [row["tables"] for row in full["boards"]])
        with self.assertRaisesRegex(ValueError, "已变化"):
            self.state.run_board(job["id"], ids[0], "stale")
        for payload in ({"ids": [ids[0]], "version": "stale", "format": "json"},
                        {"ids": ["missing"], "version": index["version"], "format": "json"},
                        {"ids": ids, "version": index["version"], "format": "outside"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.state.export_selected(job["id"], payload)

    def test_compact_cache_still_rejects_tampered_evidence_and_respects_bounded_memory(self):
        jobs = [self.batch(count=1, seed=seed) for seed in range(3)]
        for job in jobs:
            self.state.run_index(job["id"])
        self.assertEqual(len(self.state.index_cache), 2)
        job = jobs[-1]
        index = self.state.run_index(job["id"])
        path = self.state.workspace / "audits" / job["id"] / "boards.bridge.jsonl"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.state.run_board(job["id"], index["boards"][0]["id"], index["version"])
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.state.run_index(job["id"])

    def test_single_board_pretty_json_and_failed_boards_remain_browsable(self):
        job = self.state.start_audit({"board": BOARD,
            "system_a": self.a, "system_b": self.b, "fresh_dds": False})
        self.state.executor.submit(lambda: None).result(timeout=60)
        index = self.state.run_index(job["id"])
        self.assertEqual(self.state.run_board(job["id"], index["boards"][0]["id"])["record"],
                         self.state.bundle(job["id"])["boards"][0])
        with patch.object(viewer, "audit_checks", return_value=(
            [{"id": "table1-bounds", "status": "failed", "detail": "fixture error"}], [])):
            failed = self.batch(count=1)
        compact = self.state.run_index(failed["id"])["boards"][0]
        self.assertIsNone(compact["imp"])
        self.assertEqual(compact["audit_result"]["errors"][0]["message"], "fixture error")
        self.assertTrue(compact["tables"][0]["auction"])


class LibraryAndLazyHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = Path(cls.temporary.name)
        (cls.project / "systems").mkdir()
        for name in ("general-v2.json", "pass-v2.json"):
            (cls.project / "systems" / name).write_bytes((ROOT / "systems" / name).read_bytes())
        cls.state = viewer.ViewerState(cls.project)
        a, b = cls.state.systems
        cls.job = cls.state.start_batch({"count": 2, "seed": 5, "system_a": a, "system_b": b})
        cls.state.executor.submit(lambda: None).result(timeout=60)
        cls.server = viewer.ViewerServer(("127.0.0.1", 0), cls.state)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers or {})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_management_lazy_detail_export_and_origin_boundaries(self):
        status, library = self.request("GET", "/api/library")
        self.assertEqual(status, 200)
        self.assertIn(self.job["id"], [row["id"] for row in library["entries"]])
        status, index = self.request("GET", f"/api/runs/{self.job['id']}/index")
        self.assertEqual(status, 200)
        board_id = index["boards"][0]["id"]
        status, detail = self.request("GET", f"/api/runs/{self.job['id']}/boards/{board_id}?version={index['version']}")
        self.assertEqual(status, 200)
        self.assertIn("public_context", detail["record"]["tables"][0]["auction"][0])
        status, exported = self.request("POST", f"/api/runs/{self.job['id']}/export",
            {"ids": [board_id], "version": index["version"], "format": "json"})
        self.assertEqual(status, 200)
        self.assertEqual(exported["boards"][0], detail["record"])
        status, summary = self.request("GET", f"/api/runs/{self.job['id']}/boards/{board_id}?view=summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["schema"], "bridge-agent-board/v1")
        self.assertNotIn("public_context", json.dumps(summary))
        status, decision = self.request("GET", f"/api/runs/{self.job['id']}/boards/{board_id}?view=decision&table=1&turn=1")
        self.assertEqual(status, 200)
        self.assertEqual(decision["decision"], detail["record"]["tables"][0]["auction"][0])
        self.assertNotIn("dds", decision)
        status, statistics = self.request("GET", f"/api/audits/{self.job['id']}/records")
        self.assertEqual(status, 200)
        self.assertEqual(statistics["records"], 2)
        self.assertEqual(statistics["task_status"], "completed")
        self.assertEqual(statistics["schema"], "bridge-agent-statistics/v1")
        status, selected = self.request("GET", f"/api/audits/{self.job['id']}/records?view=summary&board=2")
        self.assertEqual(status, 200)
        self.assertEqual(selected["number"], 2)
        for method, path, payload in (("GET", "/api/library", None),
                                     ("GET", f"/api/runs/{self.job['id']}/index", None),
                                     ("POST", "/api/library", {"ids": [self.job["id"]], "action": "trash", "confirm": True})):
            status, _ = self.request(method, path, payload, {"Origin": "https://untrusted.invalid"})
            self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/library", {"ids": [self.job["id"]], "action": "trash", "confirm": True})
        self.assertEqual(status, 200)
        status, error = self.request("GET", f"/api/runs/{self.job['id']}/index")
        self.assertEqual(status, 422)
        self.assertIn("回收站", error["error"])
        status, _ = self.request("POST", "/api/library", {"ids": [self.job["id"]], "action": "restore"})
        self.assertEqual(status, 200)
