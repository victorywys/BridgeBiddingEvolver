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

from test_board_viewer import BOARD, ROOT, viewer


class HumanAuctionTests(unittest.TestCase):
    def test_complete_prefix_and_ap_use_bridge_legality_not_policy_forcing(self):
        complete = viewer.human_auction({"board": BOARD, "calls": "1♠ Pass 2♣ P 4S AP"})
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["contract"], {"level": 4, "strain": "S", "declarer": "N", "doubling": 1})
        self.assertEqual(complete["calls"][-3:], ["P", "P", "P"])
        prefix = viewer.human_auction({"board": {**BOARD, "dealer": "E"}, "calls": ["1NT", "P"]})
        self.assertFalse(prefix["complete"])
        self.assertIsNone(prefix["contract"])
        self.assertEqual(prefix["next_seat"], "W")
        self.assertEqual([step["seat"] for step in prefix["auction"]], ["E", "S"])
        self.assertTrue(viewer.human_auction({"board": BOARD, "calls": "1C P 2D AP"})["complete"])

    def test_illegal_sequences_and_calls_after_completion_fail_explicitly(self):
        for calls in ("1S P 1C", "X", "P P P P 1C", "AP P", "8NT", [None]):
            with self.subTest(calls=calls), self.assertRaises(ValueError):
                viewer.human_auction({"board": BOARD, "calls": calls})


class SamplingTests(unittest.TestCase):
    def config(self, **updates):
        return viewer.sampling_config({"count": 5, "seed": 20261102, **updates})

    def test_optional_seed_is_generated_only_on_submission_and_zero_stays_zero(self):
        with patch.object(viewer.random, "SystemRandom") as entropy:
            entropy.return_value.randrange.return_value = 83150764881293
            for payload in ({"count": 1}, {"count": 1, "seed": None}, {"count": 1, "seed": ""}, {"count": 1, "seed": "  "}):
                with self.subTest(payload=payload):
                    self.assertEqual(viewer.sampling_config(payload)["seed"], 83150764881293)
                    self.assertIsNone(viewer.sampling_config(payload, generate_seed=False)["seed"])
            entropy.return_value.randrange.assert_called_with(2**53)
            self.assertEqual(entropy.return_value.randrange.call_count, 4)
            self.assertEqual(self.config(seed=0)["seed"], 0)
            self.assertEqual(entropy.return_value.randrange.call_count, 4)

    def test_reproducible_complete_deals_respect_all_seat_ranges(self):
        config = self.config(dealer="E", vulnerability="both", constraints={
            "N": {"hcp": [12, 20], "S": [4, 8]},
            "E": {"H": [0, 4]}, "S": {"D": [2, 6]}, "W": {"C": [2, 6]},
        })
        first = list(viewer.sampled_boards(config))
        second = list(viewer.sampled_boards(config))
        self.assertEqual(first, second)
        self.assertEqual(len({record["family_id"] for record in first}), 5)
        for record in first:
            summary = viewer.hand_summary(record["board"])["hand_summary"]
            self.assertEqual((record["board"]["dealer"], record["board"]["vulnerability"]), ("E", "both"))
            for seat in viewer.SEATS:
                values = {"hcp": summary[seat]["hcp"], **dict(zip(viewer.SUITS, summary[seat]["shape"]))}
                for field, (low, high) in config["constraints"][seat].items():
                    self.assertLessEqual(low, values[field])
                    self.assertLessEqual(values[field], high)

    def test_impossible_and_malformed_constraints_are_rejected_before_sampling(self):
        invalid = [
            {"count": 0}, {"count": 1001}, {"seed": True}, {"seed": 1.5}, {"seed": "42"},
            {"seed": -1}, {"seed": 2**53}, {"attempt_limit": 1}, {"dealer": "Q"},
            {"constraints": {"Q": {}}}, {"constraints": {"N": {"hcp": [10, 9]}}},
            {"constraints": {"N": {"S": [8, 13], "H": [8, 13]}}},
            {"constraints": {seat: {"hcp": [11, 40]} for seat in "NESW"}},
            {"constraints": {"N": {"S": [7, 13]}, "E": {"S": [7, 13]}}},
        ]
        for updates in invalid:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.config(**updates)

    def test_exhaustion_and_cancellation_never_relax_constraints(self):
        config = self.config(count=1, attempt_limit=3, constraints={"N": {"hcp": [11, 40]}})
        with patch.object(viewer.random.Random, "shuffle", return_value=None):
            with self.assertRaisesRegex(ValueError, "只找到0/1"):
                list(viewer.sampled_boards(config))
        with self.assertRaises(viewer.AuditCancelled):
            list(viewer.sampled_boards(self.config(), cancelled=lambda: True))


class ReviewWorkflowTests(unittest.TestCase):
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

    def finish(self, job):
        self.state.executor.submit(lambda: None).result(timeout=180)
        return self.state.audit_job(job["id"])

    def request(self, **updates):
        return {"count": 2, "seed": 20261102, "system_a": self.a, "system_b": self.b, **updates}

    def replace_report(self, identifier, report):
        path = self.state.workspace / "audits" / identifier / "audit-report.json"
        viewer.save_document(path, report)
        self.state.update_job(identifier, report_sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def replace_rows(self, identifier, rows):
        directory = self.state.workspace / "audits" / identifier
        path = directory / "boards.bridge.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        report = json.loads((directory / "audit-report.json").read_text())
        report["evidence_sha256"]["boards.bridge.jsonl"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.replace_report(identifier, report)

    def test_human_revisions_persist_without_mutating_frozen_board_or_tables(self):
        original = copy.deepcopy(BOARD)
        payload = {"board": BOARD, "table": "table1", "calls": "1NT P 3NT AP", "label": "人类建议",
                   "note": "<not executable>"}
        first = self.state.save_human(payload)
        second = self.state.save_human({**payload, "calls": "1NT P 4NT"})
        self.assertEqual(BOARD, original)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["previous_id"], first["id"])
        self.assertEqual(second["revision"], 2)
        self.assertFalse(second["complete"])
        self.assertIsNone(second["contract"])
        restored = viewer.ViewerState(self.project)
        self.addCleanup(restored.executor.shutdown, wait=True)
        queried = restored.human_references({"board": {**BOARD, "number": 99}})
        self.assertEqual([row["id"] for row in queried["references"]], [second["id"], first["id"]])
        exported = [json.loads(line) for line in restored.human_export().splitlines()]
        self.assertEqual({row["id"] for row in exported}, {first["id"], second["id"]})
        self.assertTrue(all(row["schema"] == "bridge-human-auction/v1" for row in exported))
        self.assertFalse(any("private_trace" in step for row in exported for step in row["auction"]))
        with self.assertRaisesRegex(ValueError, "人类参考JSONL"):
            viewer.import_file(restored.human_export(), "human-auctions.jsonl")

    def test_reference_slot_and_revision_are_bound_to_actual_system_context(self):
        record = viewer.normalize_record({**BOARD, "tables": [
            {"systems": {"NS": {"hash": self.a, "name": "A"}, "EW": {"hash": self.b, "name": "B"}}},
            {"systems": {"NS": {"hash": self.b, "name": "B"}, "EW": {"hash": self.a, "name": "A"}}},
        ]}, 0, "test")
        first = self.state.save_human({"board": record, "table": "table1", "calls": "1C P"})
        other = self.state.save_human({"board": record, "table": "table2", "calls": "1D P"})
        self.assertNotEqual(first["slot"], other["slot"])
        self.assertEqual((first["revision"], other["revision"]), (1, 1))
        self.assertEqual(first["systems"], record["tables"][0]["systems"])

    def test_reference_tampering_empty_and_bad_fields_do_not_supply_a_fake_standard(self):
        for payload in ({"calls": ""}, {"calls": "1C", "table": "outside"}, {"calls": "1C", "label": ""},
                        {"calls": "1C", "note": 5}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.state.save_human({"board": BOARD, **payload})
        saved = self.state.save_human({"board": BOARD, "calls": "1C P"})
        path = self.state.workspace / "human" / saved["deal_key"] / (saved["id"] + ".json")
        raw = json.loads(path.read_text())
        raw["calls"] = ["7NT"]
        path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "身份"):
            self.state.human_references({"board": BOARD})
        with self.assertRaisesRegex(ValueError, "哈希"):
            self.state.human_export()

    def test_real_dds_batch_persists_every_deal_report_and_complete_zip(self):
        constraints = {"N": {"hcp": [12, 20], "S": [4, 8]}}
        job = self.finish(self.state.start_batch(self.request(constraints=constraints, label="two real boards")))
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["completed_boards"], 2)
        self.assertEqual(job["accepted_boards"], 2)
        report = self.state.audit_report(job["id"])
        self.assertEqual(report["oracle"]["oracle_kind"], "dds")
        self.assertEqual(report["metrics"]["strict_semantic_contracts"], 0)
        self.assertEqual(report["metrics"]["tables"], 4)
        self.assertEqual(report["summary"]["deleted_or_replaced_boards"], 0)
        self.assertEqual(report["sampling"]["constraints"]["N"]["hcp"], [12, 20])
        bundle = self.state.bundle(job["id"])
        self.assertEqual(bundle["purpose"], "batch-audit")
        self.assertEqual(bundle["systems"]["A"]["hash"], self.a)
        self.assertEqual(bundle["systems"]["B"]["name"], self.state.systems[self.b]["system"].name)
        self.assertEqual(len(bundle["boards"]), 2)
        self.assertEqual(bundle["evaluation"]["audit"]["games"], report["games"])
        for record in bundle["boards"]:
            self.assertGreaterEqual(record["hand_summary"]["N"]["hcp"], 12)
            self.assertEqual(len(record["tables"]), 2)
            self.assertEqual(len(record["dds"]["table"]), 4)
        contents, _, _ = self.state.audit_download(job["id"], "zip")
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            self.assertIn(job["id"] + "/boards.bridge.jsonl", archive.namelist())
            self.assertIn(job["id"] + "/dataset-boards.jsonl", archive.namelist())
            self.assertIsNone(archive.testzip())
        restored = viewer.ViewerState(self.project)
        self.addCleanup(restored.executor.shutdown, wait=True)
        self.assertEqual(restored.bundle(job["id"])["boards"], bundle["boards"])
        catalog = next(run for run in restored.catalog()["runs"] if run["id"] == job["id"])
        self.assertEqual((catalog["boards"], catalog["purpose"]), (2, "batch-audit"))

    def test_completed_batch_reuses_exact_saved_meanings_and_one_verified_bundle(self):
        job = self.finish(self.state.start_batch(self.request()))
        path = self.state.workspace / "audits" / job["id"] / "boards.bridge.jsonl"
        expected = viewer.read_rows(path)
        with patch.object(viewer, "explain_public_auction", side_effect=AssertionError("must not replay saved explanations")):
            with patch.object(self.state, "_load_audit_bundle", wraps=self.state._load_audit_bundle) as load:
                first = self.state.bundle(job["id"])
                second = self.state.bundle(job["id"])
        self.assertIs(first, second)
        self.assertEqual(load.call_count, 1)
        self.assertEqual([row["tables"] for row in first["boards"]], [row["tables"] for row in expected])
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.state.bundle(job["id"])

    def test_completed_bundle_cache_is_bounded_and_invalidates_changed_reports(self):
        first = self.finish(self.state.start_batch(self.request(count=1)))
        second = self.finish(self.state.start_batch(self.request(count=1)))
        with patch.object(self.state, "_load_audit_bundle", wraps=self.state._load_audit_bundle) as load:
            self.state.bundle(first["id"])
            self.state.bundle(second["id"])
            self.state.bundle(first["id"])
            self.assertEqual(load.call_count, 3)
            report = self.state.audit_report(first["id"])
            report["inspection_note"] = "fixture metadata revision"
            self.replace_report(first["id"], report)
            updated = self.state.bundle(first["id"])
        self.assertEqual(load.call_count, 4)
        self.assertEqual(updated["batch_audit"]["inspection_note"], "fixture metadata revision")
        self.assertEqual(self.state.audit_cache[0], first["id"])

    def test_legacy_missing_meaning_replays_only_the_affected_table(self):
        job = self.finish(self.state.start_batch(self.request(count=1)))
        rows = viewer.read_rows(self.state.workspace / "audits" / job["id"] / "boards.bridge.jsonl")
        expected = copy.deepcopy(rows[0]["tables"])
        rows[0]["tables"][0]["auction"][0].pop("public_explanation")
        self.replace_rows(job["id"], rows)
        with patch.object(viewer, "explain_public_auction", wraps=viewer.explain_public_auction) as replay:
            bundle = self.state.bundle(job["id"])
        self.assertEqual(replay.call_count, 1)
        self.assertEqual(bundle["boards"][0]["tables"], expected)

    def test_malformed_saved_explanations_and_unhashed_artifacts_are_not_trusted(self):
        job = self.finish(self.state.start_batch(self.request(count=1)))
        original = viewer.read_rows(self.state.workspace / "audits" / job["id"] / "boards.bridge.jsonl")
        self.state.bundle(job["id"])
        for field, invalid in (("schema_version", True), ("summary", 3), ("notes", "not a list"), ("warnings", [{}])):
            rows = copy.deepcopy(original)
            rows[0]["tables"][0]["auction"][0]["public_explanation"][field] = invalid
            self.replace_rows(job["id"], rows)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "saved public explanation"):
                self.state.bundle(job["id"])
        self.replace_rows(job["id"], original)
        report = self.state.audit_report(job["id"])
        for key in ("boards.bridge.jsonl", f"systems/{self.a}.json"):
            changed = copy.deepcopy(report)
            changed["evidence_sha256"].pop(key)
            self.replace_report(job["id"], changed)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "evidence is missing"):
                self.state.bundle(job["id"])

    def test_imported_saved_wording_is_still_rebuilt_from_public_evidence(self):
        job = self.finish(self.state.start_batch(self.request(count=1)))
        record = copy.deepcopy(self.state.bundle(job["id"])["boards"][0])
        record["tables"][0]["auction"][0]["public_explanation"]["summary"] = "FORGED WORDING"
        contents = json.dumps({"schema": viewer.SCHEMA, "boards": [record]}).encode()
        definitions = {identity: entry["system"] for identity, entry in self.state.systems.items()}
        with patch.object(viewer, "explain_public_auction", wraps=viewer.explain_public_auction) as replay:
            imported = viewer.import_file(contents, "untrusted.json", public_definitions=definitions)
        self.assertEqual(replay.call_count, 2)
        self.assertNotEqual(imported["boards"][0]["tables"][0]["auction"][0]["public_explanation"]["summary"], "FORGED WORDING")

    def test_failed_sampling_preserves_explicit_error_and_never_registers_a_complete_run(self):
        with patch.object(viewer.random.Random, "shuffle", return_value=None):
            with self.assertLogs("bridge.board_viewer", level="ERROR"):
                job = self.finish(self.state.start_batch(self.request(count=1, attempt_limit=3,
                    constraints={"N": {"hcp": [11, 40]}})))
        self.assertEqual(job["status"], "failed")
        self.assertIn("只找到0/1", self.state.audit_report(job["id"])["error"])
        self.assertNotIn(job["id"], [run["id"] for run in self.state.catalog()["runs"]])
        with self.assertRaises(ValueError):
            self.state.bundle(job["id"])

    def test_completed_batch_does_not_hide_registered_semantic_findings(self):
        found = {"id": "artificial-splinter-terminal-3nt", "description": "Fixture targeted finding",
                 "table": "table1", "turn": 0}
        with patch.object(viewer, "audit_checks", return_value=([{"status": "passed", "detail": "fixture"}], [found])):
            job = self.finish(self.state.start_batch(self.request(count=1)))
        self.assertEqual(job["status"], "completed")
        audit = self.state.bundle(job["id"])["evaluation"]["audit"]
        self.assertEqual(audit["status"], "completed-with-findings")
        self.assertEqual(audit["semantic_review_status"], "completed-with-findings")
        self.assertEqual(len(audit["findings"]), 1)

    def test_missing_dds_keeps_all_boards_without_zero_scores_or_fake_summary(self):
        with patch.object(viewer, "DDSAdapter", side_effect=viewer.DDSUnavailableError("not available")):
            with self.assertLogs("bridge.board_viewer", level="ERROR"):
                job = self.finish(self.state.start_batch(self.request()))
        self.assertEqual(job["status"], "completed")
        self.assertEqual((job["completed_boards"], job["valid_boards"], job["failed_boards"]), (2, 0, 2))
        report = self.state.audit_report(job["id"])
        self.assertEqual(report["status"], "completed-with-findings")
        self.assertIsNone(report["summary"])
        self.assertEqual(len(report["board_failures"]), 2)
        rows = self.state.bundle(job["id"])["boards"]
        for row in rows:
            self.assertIsNone(row["imp"])
            self.assertEqual(row["audit_result"]["status"], "failed")
            self.assertIn("not available", row["audit_result"]["errors"][0]["message"])
            self.assertTrue(all(table["score_ns"] is None for table in row["tables"]))

    def test_first_table_error_preserves_other_table_and_continues_later_boards(self):
        original = viewer.run_auction
        calls = 0

        def fail_first(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("fixture policy failure <not markup>")
            return original(*args, **kwargs)

        with patch.object(viewer, "run_auction", side_effect=fail_first):
            with self.assertLogs("bridge.board_viewer", level="ERROR"):
                job = self.finish(self.state.start_batch(self.request()))
        self.assertEqual((job["status"], job["completed_boards"], job["valid_boards"], job["failed_boards"]), ("completed", 2, 1, 1))
        self.assertEqual(calls, 7)
        bundle = self.state.bundle(job["id"])
        failed, good = bundle["boards"]
        self.assertEqual(failed["tables"][0]["auction"], [])
        self.assertIsNone(failed["tables"][0]["contract"])
        self.assertIsNone(failed["tables"][0]["score_ns"])
        self.assertTrue(failed["tables"][1]["auction"])
        self.assertIsInstance(failed["tables"][1]["score_ns"], int)
        self.assertIsNone(failed["imp"])
        self.assertIsNone(failed["point_difference"])
        self.assertEqual(failed["audit_result"]["errors"][0]["table"], "table1")
        self.assertEqual(failed["audit_result"]["errors"][0]["stage"], "auction")
        self.assertEqual(good["audit_result"]["status"], "checked")
        report = self.state.audit_report(job["id"])
        self.assertEqual(report["engineering_checks_status"], "failed")
        self.assertEqual(report["release_acceptance"], "blocked_by_board_failures")
        self.assertEqual(report["summary"]["boards"], 1)
        self.assertEqual(report["summary"]["audited_boards"], 2)
        self.assertEqual(report["summary"]["invalid_boards"], 1)
        self.assertEqual(report["summary"]["total_imp"], good["imp"])
        self.assertEqual(report["slam"]["tables"], 2)
        self.assertEqual(report["board_failures"][0]["board_number"], 1)
        self.assertEqual(bundle["evaluation"]["audit"]["engineering_checks_status"], "failed")

    def test_check_failure_keeps_original_scores_but_excludes_pair_after_restart(self):
        failed_check = {"id": "table1-bounds", "status": "failed", "detail": "fixture unsound suit bound"}
        with patch.object(viewer, "audit_checks", side_effect=[([failed_check], []), ([], [])]):
            job = self.finish(self.state.start_batch(self.request()))
        self.assertEqual((job["valid_boards"], job["failed_boards"]), (1, 1))
        restored = viewer.ViewerState(self.project)
        self.addCleanup(restored.executor.shutdown, wait=True)
        failed, good = restored.bundle(job["id"])["boards"]
        self.assertTrue(all(table["score_ns"] is not None for table in failed["tables"]))
        self.assertIsNone(failed["imp"])
        self.assertIsNone(failed["point_difference"])
        self.assertEqual(failed["audit_result"]["checks"], [failed_check])
        self.assertEqual(failed["audit_result"]["errors"][0]["message"], failed_check["detail"])
        self.assertIsNotNone(good["imp"])
        self.assertEqual(restored.audit_report(job["id"])["summary"]["boards"], 1)

    def test_all_board_execution_errors_are_browsable_without_a_score_summary(self):
        with patch.object(self.state, "compare", side_effect=ValueError("fixture board execution failure")):
            with self.assertLogs("bridge.board_viewer", level="ERROR"):
                job = self.finish(self.state.start_batch(self.request()))
        self.assertEqual((job["status"], job["completed_boards"], job["valid_boards"], job["failed_boards"]), ("completed", 2, 0, 2))
        report = self.state.audit_report(job["id"])
        self.assertIsNone(report["summary"])
        self.assertEqual(len(report["board_failures"]), 2)
        bundle = self.state.bundle(job["id"])
        self.assertEqual(len(bundle["boards"]), 2)
        self.assertIsNone(bundle["evaluation"]["summary"])
        self.assertTrue(all(row["imp"] is None for row in bundle["boards"]))
        self.assertTrue(all(not table["auction"] for row in bundle["boards"] for table in row["tables"]))
        self.assertIn(job["id"], [run["id"] for run in self.state.catalog()["runs"]])

    def test_board_cancellation_and_storage_failure_are_not_recoverable_board_errors(self):
        with patch.object(self.state, "compare", side_effect=viewer.AuditCancelled("fixture cancellation")):
            job = self.finish(self.state.start_batch(self.request()))
        self.assertEqual((job["status"], job["completed_boards"]), ("cancelled", 0))
        self.assertEqual(self.state.audit_report(job["id"])["board_failures"], [])
        original = Path.open

        def fail_storage(path, mode="r", *args, **kwargs):
            if path.name == "boards.bridge.jsonl" and mode == "x":
                raise OSError("fixture disk full")
            return original(path, mode, *args, **kwargs)

        with patch.object(Path, "open", fail_storage):
            with self.assertLogs("bridge.board_viewer", level="ERROR"):
                job = self.finish(self.state.start_batch(self.request()))
        self.assertEqual(job["status"], "failed")
        self.assertIn("fixture disk full", self.state.audit_report(job["id"])["error"])
        self.assertNotIn(job["id"], [run["id"] for run in self.state.catalog()["runs"]])

    def test_failed_pair_errors_survive_json_and_pbn_without_reconstructing_zero_imp(self):
        detail = {"stage": "checks", "table": "table1", "type": "ValueError",
                  "message": 'fixture invalid score <script> "quoted" \\ path\nsecond line'}
        record = viewer.normalize_record({**BOARD, "tables": [
            {"auction": ["1C", "P", "P", "P"], "score_ns": 0},
            {"auction": ["P", "P", "P", "P"], "score_ns": 0}],
            "audit_result": {"status": "failed", "errors": [detail]}}, 0, "test")
        self.assertIsNone(record["imp"])
        self.assertEqual(viewer.statistics([record])["same_score_different_auction"], 0)
        contents = json.dumps(viewer.pack([record], "failed", "test")).encode()
        for restored in (viewer.import_file(contents, "failed.json"),
                         viewer.import_file(viewer.export_pbn([record]).encode(), "failed.pbn")):
            with self.subTest(source=restored["source"]):
                row = restored["boards"][0]
                self.assertEqual(row["audit_result"]["errors"], [detail])
                self.assertIsNone(row["imp"])
                self.assertIsNone(row["point_difference"])
                self.assertEqual([table["score_ns"] for table in row["tables"]], [0, 0])
        failed_table = viewer.normalize_record({**BOARD, "tables": [{"auction": [], "error": detail}],
            "audit_result": {"status": "failed", "errors": [detail]}}, 0, "test")
        restored = viewer.import_file(viewer.export_pbn([failed_table]).encode(), "empty-error.pbn")["boards"][0]
        self.assertEqual(restored["tables"][0]["auction"], [])
        self.assertEqual(restored["tables"][0]["error"], detail)
        with self.assertRaisesRegex(ValueError, "requires a failed"):
            viewer.normalize_record({**BOARD, "tables": [{"error": detail}]}, 0, "test")

    def test_malformed_board_failure_metadata_is_rejected(self):
        invalid = [None, {"status": "checked", "errors": [{"stage": "checks", "message": "failure"}]},
                   {"status": "failed", "errors": []}, {"status": "failed", "errors": ["not an object"]},
                   {"status": "failed", "errors": [{"stage": "checks", "message": 3}]}]
        for result in invalid:
            with self.subTest(result=result), self.assertRaises(ValueError):
                viewer.normalize_record({**BOARD, "audit_result": result}, 0, "test")

    def test_queued_batch_cancel_is_durable_and_not_a_complete_run(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.state.executor.submit(lambda: release.wait(timeout=10))
        job = self.state.start_batch(self.request())
        self.state.cancel_audit(job["id"])
        release.set()
        cancelled = self.finish(job)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["completed_boards"], 0)
        self.assertEqual(self.state.audit_report(job["id"])["status"], "cancelled")
        self.assertNotIn(job["id"], [run["id"] for run in self.state.catalog()["runs"]])


class ReviewWorkflowHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = Path(cls.temporary.name)
        directory = cls.project / "systems"
        directory.mkdir()
        for name in ("general-v2.json", "pass-v2.json"):
            (directory / name).write_bytes((ROOT / "systems" / name).read_bytes())
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
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=180)
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers or {})
        response = connection.getresponse()
        status, contents, response_headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return (status, contents, response_headers) if raw else (status, json.loads(contents))

    def test_human_preview_save_query_and_dedicated_ndjson_export(self):
        status, result = self.request("POST", "/api/human-auctions/preview", {"board": BOARD, "calls": "1NT P"})
        self.assertEqual(status, 200)
        self.assertFalse(result["complete"])
        status, saved = self.request("POST", "/api/human-auctions", {"board": BOARD, "calls": "1NT P 3NT AP"})
        self.assertEqual(status, 200)
        status, result = self.request("POST", "/api/human-auctions/query", {"board": BOARD})
        self.assertEqual(status, 200)
        self.assertIn(saved["id"], [row["id"] for row in result["references"]])
        status, contents, headers = self.request("GET", "/api/human-auctions/export", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(saved["id"], [json.loads(line)["id"] for line in contents.splitlines()])

    def test_batch_creation_result_and_retry_preserve_conditions(self):
        a, b = self.state.systems
        payload = {"count": 2, "seed": 2026110210, "system_a": a, "system_b": b,
                   "constraints": {"S": {"hcp": [8, 18], "H": [3, 7]}}, "dealer": "W", "vulnerability": "ns"}
        status, normalized = self.request("POST", "/api/batches/validate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(normalized["sampling"]["constraints"]["S"]["H"], [3, 7])
        status, job = self.request("POST", "/api/batches", payload)
        self.assertEqual(status, 202)
        self.state.executor.submit(lambda: None).result(timeout=180)
        status, result = self.request("GET", "/api/audits/" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "completed", result)
        status, bundle = self.request("GET", "/api/runs/" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(len(bundle["boards"]), 2)
        for record in bundle["boards"]:
            self.assertEqual((record["dealer"], record["vulnerability"]), ("W", "ns"))
            self.assertGreaterEqual(record["hand_summary"]["S"]["hcp"], 8)
        status, retry = self.request("POST", f"/api/audits/{job['id']}/retry", {})
        self.assertEqual(status, 202)
        self.assertEqual(retry["count"], 2)
        self.state.executor.submit(lambda: None).result(timeout=180)
        repeated = self.state.bundle(retry["id"])
        self.assertEqual([board["hands"] for board in bundle["boards"]], [board["hands"] for board in repeated["boards"]])

    def test_blank_seed_preview_then_submission_records_actual_seed_and_retry_reuses_it(self):
        a, b = self.state.systems
        payload = {"count": 1, "system_a": a, "system_b": b}
        status, preview = self.request("POST", "/api/batches/validate", payload)
        self.assertEqual(status, 200)
        self.assertIsNone(preview["sampling"]["seed"])
        status, job = self.request("POST", "/api/batches", payload)
        self.assertEqual(status, 202)
        self.assertIs(type(job["seed"]), int)
        self.assertTrue(0 <= job["seed"] < 2**53)
        self.state.executor.submit(lambda: None).result(timeout=180)
        status, finished = self.request("GET", "/api/audits/" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["report"]["sampling"]["seed"], job["seed"])
        status, retry = self.request("POST", f"/api/audits/{job['id']}/retry", {})
        self.assertEqual(status, 202)
        self.assertEqual(retry["seed"], job["seed"])
        self.state.executor.submit(lambda: None).result(timeout=180)
        self.assertEqual(self.state.bundle(job["id"])["boards"][0]["hands"],
                         self.state.bundle(retry["id"])["boards"][0]["hands"])

    def test_batch_cancel_and_new_endpoints_keep_origin_and_path_boundaries(self):
        a, b = self.state.systems
        release = threading.Event()
        self.addCleanup(release.set)
        self.state.executor.submit(lambda: release.wait(timeout=10))
        status, job = self.request("POST", "/api/batches", {"count": 2, "seed": 6, "system_a": a, "system_b": b})
        self.assertEqual(status, 202)
        status, result = self.request("POST", f"/api/audits/{job['id']}/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["cancel_requested"])
        release.set()
        self.state.executor.submit(lambda: None).result(timeout=180)
        status, result = self.request("GET", "/api/audits/" + job["id"])
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "cancelled")
        for path in ("/api/batches", "/api/human-auctions", "/api/human-auctions/query"):
            self.assertEqual(self.request("POST", path, {}, {"Origin": "https://not-local.example"})[0], 403)
            self.assertEqual(self.request("POST", path, [BOARD])[0], 422)
        self.assertEqual(self.request("GET", "/.runtime/board-viewer/human")[0], 404)
        status, contents, headers = self.request("GET", "/auction-stats.mjs", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn(b"buildAuctionStatistics", contents)


if __name__ == "__main__":
    unittest.main()
