import copy
import hashlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from test_board_viewer import BOARD, ROOT, viewer, write_smoke_run
from regression_fixtures import regression_cases


class AuditConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        systems = self.project / "systems"
        systems.mkdir()
        for name in ("general-v2.json", "pass-v2.json"):
            (systems / name).write_bytes((ROOT / "systems" / name).read_bytes())
        self.state = viewer.ViewerState(self.project)
        self.addCleanup(self.state.executor.shutdown, wait=True)
        self.a, self.b = self.state.systems

    def request(self, **updates):
        return {"board": copy.deepcopy(BOARD), "system_a": self.a, "system_b": self.b, **updates}

    def finish(self, job):
        self.state.executor.submit(lambda: None).result(timeout=120)
        return self.state.audit_job(job["id"])

    def test_hands_accept_lowercase_ten_empty_voids_and_reject_bad_cards(self):
        lower = {seat: hand.lower().replace("t", "10") for seat, hand in BOARD["hands"].items()}
        record = viewer.custom_deal({**BOARD, "hands": lower})
        self.assertEqual(record["hands"], BOARD["hands"])
        self.assertEqual(record["tables"], [])
        self.assertIsNone(record["imp"])
        for hands in ({**BOARD["hands"], "E": BOARD["hands"]["N"]},
                      {**BOARD["hands"], "N": "AKQ.JT9.KQ32.87"},
                      {**BOARD["hands"], "N": "AKZ.JT9.KQ32.876"}):
            with self.subTest(hands=hands), self.assertRaisesRegex(ValueError, "校验失败"):
                viewer.custom_deal({**BOARD, "hands": hands})

    def test_pbn_seat_rotation_and_full_metadata_json_are_preserved(self):
        board = viewer.parse_board(BOARD)
        pbn = viewer.deal_to_pbn(board.deal, viewer.Seat.EAST)
        record = viewer.custom_deal({"format": "pbn", "text": pbn, "dealer": "W", "vulnerability": "both"})
        self.assertEqual(record["hands"], BOARD["hands"])
        self.assertEqual((record["dealer"], record["vulnerability"]), ("W", "both"))
        tagged = f'[Dealer "E"]\n[Vulnerable "NS"]\n[Deal "{pbn}"]'
        record = viewer.custom_deal({"format": "pbn", "text": tagged, "dealer": "N", "vulnerability": "none"})
        self.assertEqual((record["dealer"], record["vulnerability"]), ("E", "ns"))
        record = viewer.custom_deal({"format": "json", "text": json.dumps({**BOARD, "dealer": "S"})})
        self.assertEqual(record["dealer"], "S")
        for payload in ({"format": "pbn", "text": 12}, {"format": "json", "text": "[]"},
                        {"format": "hands", "hands": {}}, {"format": "unsupported"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                viewer.custom_deal(payload)

    def test_seeded_deals_are_complete_and_reproducible_without_global_rng(self):
        payload = {"format": "random", "seed": 20261029, "dealer": "E", "vulnerability": "ew"}
        first = self.state.create_deal(payload, preview=True)["record"]
        second = self.state.create_deal(payload, preview=True)["record"]
        self.assertEqual(first["hands"], second["hands"])
        self.assertNotEqual(first["hands"], self.state.create_deal({**payload, "seed": 20261030}, preview=True)["record"]["hands"])
        self.assertEqual(sum(summary["hcp"] for summary in first["hand_summary"].values()), 40)
        self.assertFalse(self.state.workspace.exists())
        for seed in (-1, True, 1.5, "12", 2**53):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                self.state.create_deal({**payload, "seed": seed})

    def test_custom_library_is_persistent_and_copies_do_not_overwrite(self):
        first = self.state.create_deal({**BOARD, "label": "custom <not markup>"})["record"]
        second = self.state.create_deal(BOARD)["record"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual((first["number"], second["number"]), (1, 2))
        restored = viewer.ViewerState(self.project)
        self.addCleanup(restored.executor.shutdown, wait=True)
        records = restored.bundle("custom-deals")["boards"]
        self.assertEqual({record["number"] for record in records}, {1, 2})
        self.assertEqual(next(record for record in records if record["id"] == first["id"])["label"], "custom <not markup>")
        self.assertEqual(restored.catalog()["runs"][0]["purpose"], "custom-deals")
        with self.assertRaises(ValueError):
            self.state.create_deal({**BOARD, "label": "x" * 161})

    def test_real_dds_audit_is_persistent_downloadable_and_can_be_reopened(self):
        job = self.finish(self.state.start_audit(self.request()))
        self.assertEqual(job["status"], "completed", job)
        report = self.state.audit_report(job["id"])
        self.assertEqual(report["oracle"]["oracle_kind"], "dds")
        self.assertTrue(report["dds_verification"]["fresh_crosscheck"])
        self.assertEqual(sum(len(row) for row in report["dds_table"].values()), 20)
        self.assertEqual(report["release_acceptance"], "not_established")
        self.assertEqual(report["metrics"]["tables"], 2)
        self.assertTrue(all(check["status"] in ("passed", "not-applicable", "warning") for check in report["checks"]))
        with patch.object(viewer, "DDSAdapter", side_effect=AssertionError("reopening must not solve")):
            restored = viewer.ViewerState(self.project)
            self.addCleanup(restored.executor.shutdown, wait=True)
            bundle = restored.bundle(job["id"])
        self.assertEqual(bundle["single_audit"], report)
        self.assertIsInstance(bundle["boards"][0]["imp"], int)
        self.assertTrue(all(step["public_explanation"]["basis"] == "frozen-public-protocol"
                            for table in bundle["boards"][0]["tables"] for step in table["auction"]))
        for format in ("json", "md", "zip"):
            contents, content_type, filename = self.state.audit_download(job["id"], format)
            self.assertTrue(contents)
            self.assertTrue(filename.endswith("." + format))
            if format == "md":
                self.assertIn("不是独立保留测试", contents.decode())
                self.assertIn("不等于全面语义正确", contents.decode())
            if format == "zip":
                with zipfile.ZipFile(io.BytesIO(contents)) as archive:
                    names = archive.namelist()
                    self.assertIn(job["id"] + "/board.bridge.json", names)
                    for identity in (self.a, self.b):
                        self.assertIn(job["id"] + f"/systems/{identity}.json", names)
        with self.assertRaises(ValueError):
            self.state.audit_download(job["id"], "html")

    def test_dds_unavailable_is_a_failed_report_with_null_scores_and_can_retry(self):
        with patch.object(viewer, "DDSAdapter", side_effect=viewer.DDSUnavailableError("missing test DDS")):
            job = self.finish(self.state.start_audit(self.request()))
        self.assertEqual(job["status"], "failed")
        report = self.state.audit_report(job["id"])
        self.assertIn("DDS不可用", report["error"])
        self.assertEqual(report["status"], "failed")
        record = viewer.read_rows(self.state.workspace / "audits" / job["id"] / "board.bridge.json")[0]
        self.assertIsNone(record["imp"])
        self.assertTrue(all(table["score_ns"] is None for table in record["tables"]))
        self.assertIn("未计分", self.state.audit_download(job["id"], "md")[0].decode())
        next_job = self.finish(self.state.start_audit(self.request()))
        self.assertNotEqual(next_job["id"], job["id"])
        self.assertEqual(next_job["status"], "completed", next_job)
        self.assertEqual(self.state.audit_job(job["id"])["status"], "failed")

    def test_execution_exception_is_logged_and_preserved_not_pass_or_zero(self):
        with patch.object(self.state, "compare", side_effect=RuntimeError("policy failure")):
            with self.assertLogs("bridge.board_viewer", level="ERROR"):
                job = self.finish(self.state.start_audit(self.request()))
        self.assertEqual(job["status"], "failed")
        self.assertIn("policy failure", self.state.audit_report(job["id"])["error"])
        self.assertFalse((self.state.workspace / "audits" / job["id"] / "board.bridge.json").exists())
        self.assertIn("执行错误", self.state.audit_download(job["id"], "md")[0].decode())

    def test_unsound_bounds_are_not_hidden_by_successful_dds_scoring(self):
        def unsound(*args):
            raise ValueError("Unsound public interval: test counterexample")
        with patch.object(viewer, "bounds_checker", return_value=unsound):
            job = self.finish(self.state.start_audit(self.request()))
        report = self.state.audit_report(job["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(report["oracle"]["oracle_kind"], "dds")
        self.assertEqual(report["engineering_checks_status"], "failed")
        self.assertEqual(sum(check["status"] == "failed" for check in report["checks"]), 2)
        self.assertIn("Unsound public interval", report["error"])

    def test_source_drift_requires_service_restart_and_preserves_a_failure_report(self):
        with patch.object(viewer, "source_snapshot", return_value={"changed.py": "new-hash"}):
            with self.assertLogs("bridge.board_viewer", level="ERROR"):
                job = self.finish(self.state.start_audit(self.request()))
        self.assertEqual(job["status"], "failed")
        self.assertIn("重启显示器", self.state.audit_report(job["id"])["error"])

    def test_async_deduplication_and_bounded_queue(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with patch.object(self.state, "execute_audit", side_effect=lambda *args: release.wait(timeout=10)):
            job = self.state.start_audit(self.request())
            self.assertEqual(self.state.start_audit(self.request())["id"], job["id"])
            self.assertEqual(len(self.state.audit_history()["jobs"]), 1)
            for number in range(2, 9):
                self.state.start_audit(self.request(board={**BOARD, "number": number}))
            with self.assertRaisesRegex(ValueError, "8"):
                self.state.start_audit(self.request(board={**BOARD, "number": 9}))
            release.set()
            self.state.executor.submit(lambda: None).result(timeout=15)

    def test_restart_marks_unfinished_jobs_interrupted_not_completed(self):
        release = threading.Event()
        with patch.object(self.state, "execute_audit", side_effect=lambda *args: release.wait(timeout=10)):
            job = self.state.start_audit(self.request())
            restored = viewer.ViewerState(self.project)
            self.addCleanup(restored.executor.shutdown, wait=True)
            interrupted = restored.audit_job(job["id"])
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertIn("未完成", interrupted["error"])
            self.assertEqual(restored.catalog()["runs"], [])
            with self.assertRaisesRegex(ValueError, "中断"):
                restored.audit_report(job["id"])
            release.set()

    def test_registered_source_is_resolved_on_server_without_mutating_frozen_files(self):
        directory = self.project / "results/fixture-smoke-1"
        run = write_smoke_run(directory)
        before = {path.relative_to(directory): hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.rglob("*") if path.is_file()}
        state = viewer.ViewerState(self.project)
        self.addCleanup(state.executor.shutdown, wait=True)
        source = state.bundle("fixture-smoke-1")["boards"][0]
        request = {"source_run": "fixture-smoke-1", "board_id": source["id"], "board": {"invalid": "must be ignored"},
                   "system_a": run["system_a"]["hash"], "system_b": run["system_b"]["hash"], "fresh_dds": False}
        job = state.start_audit(request)
        state.executor.submit(lambda: None).result(timeout=120)
        report = state.audit_report(job["id"])
        self.assertTrue(report["source_comparison"]["same_systems"])
        self.assertEqual(report["source"]["board_id"], source["id"])
        self.assertFalse(report["dds_verification"]["fresh_crosscheck"])
        after = {path.relative_to(directory): hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        with self.assertRaisesRegex(ValueError, "找不到"):
            state.start_audit({**request, "board_id": "wrong"})
        with self.assertRaisesRegex(ValueError, "Unknown"):
            state.start_audit({**request, "source_run": "../outside"})

    def test_known_artificial_3nt_defect_is_reported_even_when_engine_checks_pass(self):
        identities = []
        for name in ("ccba", "prec"):
            system = viewer.load_system(ROOT / f"systems/classics-v5.3-compact/{name}.json")
            path = self.project / "systems" / f"{system.content_hash}.json"
            path.write_text(json.dumps(system.to_data()))
            self.state.systems[system.content_hash] = {"path": path, "system": system}
            identities.append(system.content_hash)
        original = regression_cases("artificial_splinter_3nt")[0]
        job = self.finish(self.state.start_audit(self.request(board=original["board"], system_a=identities[0], system_b=identities[1])))
        self.assertEqual(job["status"], "completed", job)
        report = self.state.audit_report(job["id"])
        self.assertEqual(report["engineering_checks_status"], "passed")
        self.assertEqual(report["status"], "completed-with-findings")
        self.assertIn("artificial-splinter-terminal-3nt", [finding["id"] for finding in report["findings"]])
        self.assertGreater(report["metrics"]["bound_checked_decisions"], 0)

    def test_tampered_evidence_and_report_are_rejected_on_reopen(self):
        job = self.finish(self.state.start_audit(self.request()))
        directory = self.state.workspace / "audits" / job["id"]
        markdown = directory / "report.md"
        original_markdown = markdown.read_bytes()
        markdown.write_bytes(original_markdown + b" ")
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.state.audit_download(job["id"], "zip")
        markdown.write_bytes(original_markdown)
        record_path = directory / "board.bridge.json"
        original = record_path.read_bytes()
        record_path.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.state.bundle(job["id"])
        record_path.write_bytes(original)
        report_path = directory / "audit-report.json"
        report_path.write_bytes(report_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.state.audit_report(job["id"])

    def test_bad_requests_do_not_create_success_shaped_jobs(self):
        for payload in ([], self.request(system_a="missing"), self.request(fresh_dds="false"),
                        self.request(board={**BOARD, "dealer": "Q"})):
            with self.subTest(payload=payload), self.assertRaises((ValueError, KeyError)):
                self.state.start_audit(payload)
        self.assertFalse(self.state.jobs)
        for identifier in ("../../outside", "audit-../input", "audit-" + "0" * 32):
            with self.assertRaises(ValueError):
                self.state.audit_job(identifier)


class AuditConsoleHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = Path(cls.temporary.name)
        systems = cls.project / "systems"
        systems.mkdir()
        for name in ("general-v2.json", "pass-v2.json"):
            (systems / name).write_bytes((ROOT / "systems" / name).read_bytes())
        cls.state = viewer.ViewerState(cls.project)
        cls.server = viewer.ViewerServer(("127.0.0.1", 0), cls.state)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()
        cls.temporary.cleanup()

    def request(self, method, path, body=None, headers=None, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=120)
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers or {})
        response = connection.getresponse()
        status, contents, response_headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return (status, contents, response_headers) if raw else (status, json.loads(contents))

    def test_custom_endpoints_preview_save_and_reload(self):
        status, result = self.request("POST", "/api/deals/preview", BOARD)
        self.assertEqual(status, 200)
        self.assertEqual(result["record"]["hands"], BOARD["hands"])
        status, result = self.request("POST", "/api/deals", {**BOARD, "label": "HTTP custom"})
        self.assertEqual(status, 200)
        status, library = self.request("GET", "/api/runs/custom-deals")
        self.assertEqual(status, 200)
        self.assertIn(result["record"]["id"], [record["id"] for record in library["boards"]])

    def test_job_submission_progress_report_download_and_retry(self):
        a, b = self.state.systems
        status, job = self.request("POST", "/api/audits", {"board": BOARD, "system_a": a, "system_b": b})
        self.assertEqual(status, 202)
        self.assertIn(job["status"], ("queued", "running", "completed"))
        self.state.executor.submit(lambda: None).result(timeout=120)
        status, finished = self.request("GET", "/api/audits/" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(finished["status"], "completed", finished)
        self.assertEqual(finished["report"]["oracle"]["oracle_kind"], "dds")
        status, contents, headers = self.request("GET", f"/api/audits/{job['id']}/report?format=zip", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(contents)))
        status, retry = self.request("POST", f"/api/audits/{job['id']}/retry", {})
        self.assertEqual(status, 202)
        self.assertNotEqual(retry["id"], job["id"])
        self.state.executor.submit(lambda: None).result(timeout=120)
        self.assertEqual(self.state.audit_job(retry["id"])["status"], "completed")
        path = self.state.workspace / "audits" / job["id"] / "input.json"
        changed = json.loads(path.read_text())
        changed["board"]["dealer"] = "W"
        path.write_text(json.dumps(changed))
        status, result = self.request("POST", f"/api/audits/{job['id']}/retry", {})
        self.assertEqual(status, 422)
        self.assertIn("输入哈希", result["error"])

    def test_new_endpoints_preserve_origin_input_and_asset_boundaries(self):
        for path in ("/api/deals", "/api/audits"):
            self.assertEqual(self.request("POST", path, BOARD, {"Origin": "https://not-local.example"})[0], 403)
            self.assertEqual(self.request("POST", path, [BOARD])[0], 422)
        self.assertEqual(self.request("GET", "/api/audits/audit-../input")[0], 404)
        self.assertEqual(self.request("GET", "/.runtime/board-viewer/audits")[0], 404)
        status, contents, headers = self.request("GET", "/review-filters.mjs", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn(b"matchesAdvancedFilters", contents)


if __name__ == "__main__":
    unittest.main()
