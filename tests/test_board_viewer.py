import copy
import hashlib
import importlib.util
import json
import io
import sys
import tempfile
import threading
import unittest
import http.client
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("board_viewer", ROOT / "tools/board_viewer.py")
viewer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = viewer
SPEC.loader.exec_module(viewer)

BOARD = {"dealer": "N", "vulnerability": "none", "hands": {
    "N": "AKQ.JT9.KQ32.876", "E": "654.876.876.AKQJ", "S": "JT987.AKQ2.AJ.T9", "W": "32.543.T954.5432",
}}


def sample_run():
    run = {"status": "completed", "completed_boards": 1, "experiment_id": "test",
           "system_a": {"name": "A", "hash": "a"}, "system_b": {"name": "B", "hash": "b"}}
    deals = [{"index": 0, "family_id": "family", "board": copy.deepcopy(BOARD)}]
    scores = [{"index": 0, "family_id": "family", "imp_a": 6, "point_difference_a": 250}]
    auctions = []
    for name, call, score in (("table1", "3NT", 400), ("table2", "2NT", 150)):
        summary = {"contract": {"level": int(call[0]), "strain": "NT", "declarer": "N", "doubling": 1},
                   "tricks": 9, "score_ns": score}
        scores[0][name] = summary
        calls = ("1NT", "P", call, "P", "P", "P")
        auctions.append({"index": 0, "table": name, "systems": {"NS": "a" if name == "table1" else "b",
                                                               "EW": "b" if name == "table1" else "a"},
                         "auction": [{"seat": ("N", "E", "S", "W")[index % 4], "call": item,
                                      "private_trace": {"rule_id": "test.rule", "features": []}} for index, item in enumerate(calls)],
                         **summary})
    return run, deals, scores, auctions


def write_smoke_run(directory):
    directory.mkdir(parents=True)
    run, deals, scores, auctions = sample_run()
    run.update(experiment_id="fixture-smoke-1", boards=1, tables=2, dataset_split="validation", source_hash="1" * 64)
    identities = {}
    for role, old_hash, name in (("system_a", "a", "general-v2.json"), ("system_b", "b", "pass-v2.json")):
        system = viewer.load_system(ROOT / "systems" / name)
        run[role] = {"name": system.name, "hash": system.content_hash}
        identities[old_hash] = system.content_hash
        path = directory / "systems" / f"{system.content_hash}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(system.to_data()))
    for auction in auctions:
        auction["systems"] = {side: identities[content_hash] for side, content_hash in auction["systems"].items()}
    for name, records in (("dataset-boards.jsonl", deals), ("boards.jsonl", scores), ("auctions.jsonl", auctions)):
        (directory / name).write_text("\n".join(map(json.dumps, records)) + "\n")
    summary = {"boards": 1, "tables": 2, "total_imp": 6, "mean_imp": 6.0, "ci95": [6.0, 6.0],
               "a_wins": 1, "b_wins": 0, "ties": 0, "invalid_boards": 0, "deleted_or_replaced_boards": 0}
    (directory / "summary.json").write_text(json.dumps(summary))
    (directory / "semantic-contracts.json").write_text(json.dumps({"status": "passed", "probe_counts": {"consistent": 2}}))
    files = ("dataset-boards.jsonl", "boards.jsonl", "auctions.jsonl", "summary.json")
    run["artifacts_sha256"] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in files}
    run["dataset_hash"] = run["artifacts_sha256"]["dataset-boards.jsonl"]
    (directory / "run.json").write_text(json.dumps(run))
    audit = {"status": "completed-with-findings", "smoke_status": "passed", "release_acceptance": "not_established",
             "boards": 1, "tables": 2, "source_hash": run["source_hash"], "dataset_hash": run["dataset_hash"],
             "current_systems": {"ccba": identities["a"], "prec": identities["b"]},
             "metrics": {"strict_semantic_contracts": {"consistent": 2}, "bound_checked_decisions": 12},
             "findings": [{"id": "release-validation-gap", "description": "Full validation is not complete."}],
             "evidence_sha256": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                                 for name in (*files, "run.json", "semantic-contracts.json")}}
    (directory / "smoke-audit.json").write_text(json.dumps(audit))
    return run


def write_audited_run(directory):
    run = write_smoke_run(directory)
    run["experiment_id"] = directory.name
    (directory / "run.json").write_text(json.dumps(run))
    (directory / "smoke-audit.json").unlink()
    checker = directory / "supplemental-check.py"
    checker.write_text("# Fixture audit checker.\n")
    table = viewer.read_rows(directory / "auctions.jsonl")[0]
    case = {"index": 0, "board_number": 1, "family_id": "family", "board": copy.deepcopy(BOARD), "table": "table1",
            "history": [step["call"] for step in table["auction"]],
            **{key: table[key] for key in ("contract", "tricks", "score_ns")}}
    audit = {"status": "completed-with-findings", "engineering_checks_status": "passed",
             "semantic_review_status": "completed-with-findings", "release_acceptance": "blocked_by_semantic_findings",
             "source_hash": run["source_hash"], "dataset_hash": run["dataset_hash"],
             "systems": {"ccba": run["system_a"]["hash"], "prec": run["system_b"]["hash"]},
             "summary": json.loads((directory / "summary.json").read_text()),
             "metrics": {"boards": 1, "tables": 2, "strict_semantic_contracts": 2, "bound_checked_decisions": 12},
             "findings": [{"id": "artificial-splinter-terminal-3nt", "severity": "high",
                           "description": "An artificial response was passed.",
                           "affected_tables": 1, "affected_unique_families": 1, "cases": [case]}],
             "checker_sha256": hashlib.sha256(checker.read_bytes()).hexdigest()}
    files = ("run.json", "dataset-boards.jsonl", "boards.jsonl", "auctions.jsonl", "summary.json", "semantic-contracts.json",
             *(f"systems/{run[role]['hash']}.json" for role in ("system_a", "system_b")))
    audit["evidence_sha256"] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in files}
    (directory / "audit-report.json").write_text(json.dumps(audit))
    return run


class BoardViewerTests(unittest.TestCase):
    def test_run_join_preserves_after_call_intervals_and_suit_relations(self):
        run, deals, scores, auctions = sample_run()
        after = {"state": "natural", "var.captain": "N", "var.captain_S_min": 5,
                 "var.captain_H_max": 4, "var.captain_S_ge_H": True, "partnership_S_max": 13}
        auctions[0]["auction"][0]["public_context_after"] = after
        bundle = viewer.assemble_run(run, deals, scores, auctions, "shape-test")
        self.assertEqual(bundle["boards"][0]["tables"][0]["auction"][0]["public_context_after"], after)
        self.assertNotIn("public_context_after", bundle["boards"][0]["tables"][1]["auction"][0])

    def test_development_audit_retains_findings_without_claiming_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "fixture-audit-1"
            write_audited_run(directory)
            bundle = viewer.load_run(directory)
            self.assertEqual(bundle["evaluation"]["purpose"], "development-audit")
            audit = bundle["evaluation"]["audit"]
            self.assertEqual(audit["status"], "completed-with-findings")
            self.assertEqual(audit["engineering_checks_status"], "passed")
            self.assertEqual(audit["release_acceptance"], "blocked_by_semantic_findings")
            self.assertEqual(audit["metrics"]["strict_semantic_contracts"], 2)
            self.assertEqual(audit["findings"][0]["cases"][0]["board_number"], 1)
            self.assertNotIn("smoke_status", audit)

    def test_development_audit_identity_counts_and_findings_are_verified(self):
        changes = (
            lambda audit: audit.update(source_hash="2" * 64),
            lambda audit: audit["summary"].update(total_imp=0),
            lambda audit: audit["metrics"].update(bound_checked_decisions=11),
            lambda audit: audit["metrics"].update(strict_semantic_contracts=3),
            lambda audit: audit["findings"][0]["cases"][0].update(board_number=2),
            lambda audit: audit["findings"][0]["cases"][0].update(history=["P"] * 4),
            lambda audit: audit["findings"][0]["cases"].__setitem__(0, "invalid"),
            lambda audit: audit["findings"][0].update(affected_tables=2),
            lambda audit: audit.update(status="passed"),
        )
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "fixture-audit-1"
                write_audited_run(directory)
                path = directory / "audit-report.json"
                audit = json.loads(path.read_text())
                change(audit)
                path.write_text(json.dumps(audit))
                with self.assertRaises(ValueError):
                    viewer.load_run(directory)

    def test_development_audit_checks_nested_frozen_ir_and_checker_hashes(self):
        for filename in ("frozen-system", "supplemental-check.py"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "fixture-audit-1"
                run = write_audited_run(directory)
                path = directory / ("systems/" + run["system_a"]["hash"] + ".json" if filename == "frozen-system" else filename)
                path.write_text("{}")
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    viewer.load_run(directory)

    def test_development_audit_rejects_unidentified_nested_evidence(self):
        for filename in ("../outside.json", "systems/unknown.json", "other/file.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "fixture-audit-1"
                write_audited_run(directory)
                path = directory / "audit-report.json"
                audit = json.loads(path.read_text())
                audit["evidence_sha256"][filename] = "1" * 64
                path.write_text(json.dumps(audit))
                with self.assertRaisesRegex(ValueError, "identified frozen system"):
                    viewer.load_run(directory)

    def test_smoke_summary_and_audit_match_the_actual_scored_boards(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "fixture-smoke-1"
            write_smoke_run(directory)
            bundle = viewer.load_run(directory)
            evaluation = bundle["evaluation"]
            self.assertEqual(evaluation["purpose"], "development-smoke")
            self.assertEqual(evaluation["split"], "validation")
            self.assertEqual(evaluation["summary"]["total_imp"], sum(board["imp"] for board in bundle["boards"]))
            self.assertEqual(evaluation["summary"]["mean_imp"], 6)
            self.assertEqual(evaluation["summary"]["ci95"], [6, 6])
            self.assertEqual(evaluation["audit"]["smoke_status"], "passed")
            self.assertEqual(evaluation["audit"]["release_acceptance"], "not_established")
            self.assertEqual(evaluation["audit"]["metrics"]["bound_checked_decisions"], 12)

    def test_changed_summary_and_audit_evidence_are_rejected(self):
        for filename in ("summary.json", "semantic-contracts.json"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary) / "fixture-smoke-1"
                write_smoke_run(directory)
                (directory / filename).write_text("{}")
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    viewer.load_run(directory)

    def test_smoke_audit_from_another_source_is_not_displayed_as_passed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "fixture-smoke-1"
            write_smoke_run(directory)
            path = directory / "smoke-audit.json"
            audit = json.loads(path.read_text())
            audit["source_hash"] = "2" * 64
            path.write_text(json.dumps(audit))
            with self.assertRaisesRegex(ValueError, "does not match the frozen run identity"):
                viewer.load_run(directory)

    def test_nonfinite_summary_cannot_be_rendered_as_a_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "fixture-smoke-1"
            run = write_smoke_run(directory)
            path = directory / "summary.json"
            summary = json.loads(path.read_text())
            summary["ci95"][0] = float("nan")
            path.write_text(json.dumps(summary))
            run["artifacts_sha256"]["summary.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
            (directory / "run.json").write_text(json.dumps(run))
            with self.assertRaisesRegex(ValueError, "invalid mean or confidence interval"):
                viewer.load_run(directory)

    def test_older_smoke_without_an_audit_does_not_invent_a_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "fixture-smoke-1"
            write_smoke_run(directory)
            (directory / "smoke-audit.json").unlink()
            evaluation = viewer.load_run(directory)["evaluation"]
            self.assertEqual(evaluation["summary"]["boards"], 1)
            self.assertNotIn("audit", evaluation)

    def test_run_join_preserves_hands_scores_system_sides_and_private_trace(self):
        bundle = viewer.assemble_run(*sample_run(), "test-source")
        record = bundle["boards"][0]
        self.assertEqual(record["number"], 1)
        self.assertEqual(record["index"], 0)
        self.assertEqual(record["hands"], BOARD["hands"])
        self.assertEqual(record["hand_summary"]["N"]["hcp"], 15)
        self.assertEqual(record["difference"], {"auction": True, "contract": True, "score": True, "first_turn": 2})
        self.assertEqual(record["tables"][1]["systems"]["EW"]["name"], "A")
        self.assertEqual(record["tables"][0]["auction"][0]["private_trace"]["rule_id"], "test.rule")

    def test_mismatched_missing_or_duplicate_records_are_not_silently_dropped(self):
        run, deals, scores, auctions = sample_run()
        with self.assertRaises(ValueError):
            viewer.assemble_run(run, deals, scores, auctions[:1], "test")
        with self.assertRaises(ValueError):
            viewer.assemble_run(run, deals, scores, auctions + auctions[:1], "test")
        scores[0]["family_id"] = "different-family"
        with self.assertRaises(ValueError):
            viewer.assemble_run(run, deals, scores, auctions, "test")

    def test_frozen_file_tampering_is_rejected(self):
        run, deals, scores, auctions = sample_run()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run["artifacts_sha256"] = {"boards.jsonl": "0" * 64}
            (root / "run.json").write_text(json.dumps(run))
            for name, records in (("dataset-boards.jsonl", deals), ("boards.jsonl", scores), ("auctions.jsonl", auctions)):
                (root / name).write_text("\n".join(map(json.dumps, records)))
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                viewer.load_run(root)

    def test_json_roundtrip_preserves_complete_deal_and_both_auctions(self):
        original = viewer.assemble_run(*sample_run(), "test")
        repeated = viewer.import_file(json.dumps(original).encode(), "sample.bridge.json")
        self.assertEqual(repeated["boards"][0]["hands"], original["boards"][0]["hands"])
        self.assertEqual(repeated["boards"][0]["tables"], original["boards"][0]["tables"])
        self.assertEqual(repeated["statistics"], original["statistics"])

    def test_json_roundtrip_preserves_public_ledger_and_private_inference(self):
        original = viewer.assemble_run(*sample_run(), "test")
        step = original["boards"][0]["tables"][0]["auction"][0]
        step["public_context"] = {"goal": "game", "forcing": "game", "partnership_hcp_min": 25, "partnership_hcp_max": 32,
                                  "agreed_trump": "S", "agreed_trump_min": 8}
        step["private_trace"]["inference"] = {"partnership_hcp_min": 27, "partnership_hcp_max": 30, "suit_min": {"S": 9}}
        repeated = viewer.import_file(json.dumps(original).encode(), "state.bridge.json")
        restored = repeated["boards"][0]["tables"][0]["auction"][0]
        self.assertEqual(restored["public_context"], step["public_context"])
        self.assertEqual(restored["private_trace"]["inference"], step["private_trace"]["inference"])

    def test_pbn_roundtrip_preserves_seats_vulnerability_and_two_tables(self):
        original = viewer.assemble_run(*sample_run(), "test")
        original["boards"][0]["vulnerability"] = "both"
        pbn = viewer.export_pbn(original["boards"])
        repeated = viewer.import_file(pbn.encode(), "sample.pbn")
        record = repeated["boards"][0]
        self.assertEqual(record["hands"], BOARD["hands"])
        self.assertEqual(record["vulnerability"], "both")
        self.assertEqual(len(record["tables"]), 2)
        self.assertEqual([table["id"] for table in record["tables"]], ["table1", "table2"])
        self.assertEqual(record["tables"][0]["contract"]["declarer"], "N")
        self.assertEqual(record["tables"][1]["score_ns"], 150)
        self.assertEqual(record["imp"], 6)

    def test_plain_board_json_and_jsonl_import_without_inventing_results(self):
        for content in (json.dumps(BOARD), json.dumps({"index": 3, "board": BOARD}) + "\n"):
            bundle = viewer.import_file(content.encode(), "board.jsonl")
            self.assertEqual(bundle["boards"][0]["tables"], [])
            self.assertIsNone(bundle["boards"][0]["imp"])
        with self.assertRaisesRegex(ValueError, "hands"):
            viewer.import_file(b'{"index":1,"imp_a":4}', "boards.jsonl")

    def test_import_rejects_illegal_calls_inconsistent_declarer_and_duplicate_cards(self):
        bundle = viewer.assemble_run(*sample_run(), "test")
        record = bundle["boards"][0]
        record["tables"][0]["contract"]["declarer"] = "S"
        with self.assertRaisesRegex(ValueError, "declarer"):
            viewer.import_file(json.dumps(bundle).encode(), "review.json")
        record["tables"][0]["contract"]["declarer"] = "N"
        record["tables"][0]["auction"][2]["call"] = "1C"
        with self.assertRaises(ValueError):
            viewer.import_file(json.dumps(bundle).encode(), "review.json")
        with self.assertRaises(ValueError):
            viewer.import_file(json.dumps({**BOARD, "hands": {**BOARD["hands"], "E": BOARD["hands"]["N"]}}).encode(), "board.json")

    def test_missing_real_dds_returns_unscored_bidding_not_mock_scores(self):
        state = viewer.ViewerState(ROOT)
        registered = next(key for key, value in state.systems.items() if value["path"].name == "pass-v2.json")
        with patch.object(viewer, "DDSAdapter", side_effect=viewer.DDSUnavailableError("not installed for this test")):
            result = state.compare({"board": {**BOARD, "number": 9, "index": 8}, "system_a": registered, "system_b": registered})
        self.assertEqual(result["oracle"]["oracle_kind"], "unavailable")
        self.assertIsNone(result["record"]["imp"])
        self.assertEqual((result["record"]["number"], result["record"]["index"]), (9, 8))
        self.assertTrue(all(table["score_ns"] is None for table in result["record"]["tables"]))

    def test_untrusted_display_fields_cannot_supply_markup_as_numbers(self):
        for key in ("index", "number", "imp", "point_difference"):
            raw = {**BOARD, key: '<img src=x onerror="alert(1)">'}
            with self.subTest(key=key), self.assertRaises(ValueError):
                viewer.import_file(json.dumps(raw).encode(), "untrusted.json")
        with self.assertRaisesRegex(ValueError, "review"):
            viewer.import_file(json.dumps({**BOARD, "review": {"status": "<script>"}}).encode(), "review.json")

    def test_native_run_zip_import_rejects_missing_or_duplicate_files(self):
        run, deals, scores, auctions = sample_run()
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("frozen/run.json", json.dumps(run))
            for name, records in (("dataset-boards.jsonl", deals), ("boards.jsonl", scores), ("auctions.jsonl", auctions)):
                archive.writestr("frozen/" + name, "\n".join(map(json.dumps, records)))
        imported = viewer.import_file(content.getvalue(), "run.zip")
        self.assertEqual(imported["boards"][0]["imp"], 6)
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("run.json", json.dumps(run))
        with self.assertRaises(ValueError):
            viewer.import_file(content.getvalue(), "missing.zip")


class BoardViewerHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = viewer.ViewerServer(("127.0.0.1", 0), viewer.ViewerState(ROOT))
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        status, contents = response.status, response.read()
        connection.close()
        return status, json.loads(contents)

    def test_catalog_registers_completed_runs_and_general_systems(self):
        status, result = self.request("GET", "/api/catalog")
        self.assertEqual(status, 200)
        self.assertTrue(result["systems"])
        self.assertTrue(all(item["status"] == "completed" for item in result["runs"]))
        self.assertNotIn("ccba-prec-1000-v1", [item["id"] for item in result["runs"]])

    def test_smoke_http_result_and_frozen_only_systems_are_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = write_smoke_run(project / "results" / "fixture-smoke-1")
            with patch.object(self.server, "state", viewer.ViewerState(project)):
                status, catalog = self.request("GET", "/api/catalog")
                self.assertEqual(status, 200)
                self.assertEqual(catalog["runs"][0]["purpose"], "development-smoke")
                self.assertTrue(all(system["path"].startswith("results/") for system in catalog["systems"]))
                status, bundle = self.request("GET", "/api/runs/fixture-smoke-1")
                self.assertEqual(status, 200)
                self.assertEqual(bundle["evaluation"]["summary"]["total_imp"], 6)
                self.assertEqual(bundle["evaluation"]["audit"]["smoke_status"], "passed")
                status, specification = self.request("GET", "/api/systems/" + run["system_a"]["hash"])
                self.assertEqual(status, 200)
                self.assertEqual(specification["hash"], run["system_a"]["hash"])
                self.assertIn(run["system_a"]["name"], specification["specification"])

    def test_development_audit_is_available_through_the_existing_catalog_and_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            write_audited_run(project / "results" / "fixture-audit-1")
            with patch.object(self.server, "state", viewer.ViewerState(project)):
                status, catalog = self.request("GET", "/api/catalog")
                self.assertEqual(status, 200)
                self.assertEqual(catalog["runs"][0]["purpose"], "development-audit")
                status, bundle = self.request("GET", "/api/runs/fixture-audit-1")
                self.assertEqual(status, 200)
                self.assertEqual(bundle["evaluation"]["audit"]["semantic_review_status"], "completed-with-findings")
                self.assertEqual(bundle["evaluation"]["audit"]["findings"][0]["affected_tables"], 1)

    def test_system_query_http_uses_frozen_ir_without_loading_full_specification(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = write_smoke_run(project / "results" / "fixture-smoke-1")
            with patch.object(self.server, "state", viewer.ViewerState(project)), patch.object(
                    viewer, "render_system", side_effect=AssertionError("Full specification requested")):
                path = "/api/systems/" + run["system_a"]["hash"] + "/query"
                status, result = self.request("POST", path, json.dumps({"request": {"view": "catalog"}}))
                self.assertEqual(status, 200)
                self.assertEqual(result["system_hash"], run["system_a"]["hash"])
                status, result = self.request("POST", path, json.dumps({"request": {
                    "view": "auction", "history": ["1S", "P", "3S", "P"], "call": "4NT"},
                    "public_systems": {"EW": run["system_b"]["hash"]}}))
                self.assertEqual(status, 200, result)
                self.assertEqual(result["explanation"]["purpose"], "体系明确标记的人工叫品")
                self.assertEqual(result["public_meaning"]["state_after"], "keycards")
                self.assertIn("artificial", result["public_meaning"]["tags"])
                self.assertEqual(result["explanation"]["basis"], "frozen-public-protocol")
                status, result = self.request("POST", path, json.dumps({"request": {"view": "read", "keys": ["missing"]}}))
                self.assertEqual(status, 422)
                self.assertIn("Unknown semantic node", result["error"])

    def test_cross_origin_requests_and_private_paths_are_rejected(self):
        status, result = self.request("POST", "/api/import?filename=test.json", json.dumps(BOARD), {"Origin": "https://example.invalid"})
        self.assertEqual(status, 403)
        status, result = self.request("GET", "/.runtime/copilot-api/gateway-client-key")
        self.assertEqual(status, 404)

    def test_loopback_forwarded_ports_are_not_bound_to_the_server_port(self):
        for authority in ("localhost:18765", "127.0.0.1:28765", "[::1]:38765"):
            with self.subTest(authority=authority):
                status, result = self.request("POST", "/api/import?filename=forwarded.json", json.dumps(BOARD),
                    {"Host": authority, "Origin": "http://" + authority, "Sec-Fetch-Site": "same-origin"})
                self.assertEqual(status, 200)
                self.assertEqual(result["boards"][0]["hands"], BOARD["hands"])

    def test_host_rewriting_proxy_requires_an_explicit_trusted_origin(self):
        origin = "http://localhost:18765"
        status, _ = self.request("POST", "/api/import?filename=forwarded.json", json.dumps(BOARD), {"Origin": origin})
        self.assertEqual(status, 403)
        with patch.object(self.server, "allowed_origins", frozenset({viewer.origin_identity(origin)})):
            status, result = self.request("POST", "/api/import?filename=forwarded.json", json.dumps(BOARD), {"Origin": origin})
            self.assertEqual(status, 200)
            self.assertEqual(result["boards"][0]["hands"], BOARD["hands"])
            status, _ = self.request("GET", "/api/catalog", headers={"Host": "attacker.invalid", "Origin": origin})
            self.assertEqual(status, 403)

    def test_public_https_proxy_is_allowed_only_when_explicitly_configured(self):
        headers = {"Host": "bridge.example.test", "Origin": "https://bridge.example.test", "Sec-Fetch-Site": "same-origin"}
        status, _ = self.request("POST", "/api/import?filename=proxy.json", json.dumps(BOARD), headers)
        self.assertEqual(status, 403)
        with patch.object(self.server, "allowed_origins", frozenset({viewer.origin_identity("https://bridge.example.test")})):
            status, result = self.request("POST", "/api/import?filename=proxy.json", json.dumps(BOARD), headers)
            self.assertEqual(status, 200)
            self.assertEqual(result["boards"][0]["hands"], BOARD["hands"])
            status, _ = self.request("GET", "/api/catalog", headers={"Host": "bridge.example.test"})
            self.assertEqual(status, 200)

    def test_cross_origin_ports_malformed_hosts_and_null_origins_are_rejected(self):
        cases = (
            {"Host": "localhost:18765", "Origin": "http://localhost:28765"},
            {"Host": "localhost.evil.invalid:18765"}, {"Host": "evil.invalid"},
            {"Host": "localhost:99999"}, {"Host": "localhost:0"}, {"Host": "user@localhost:18765"},
            {"Host": "localhost:18765/path"}, {"Host": "localhost:18765", "Origin": "null"},
            {"Host": "localhost:18765", "Origin": "http://localhost:18765", "Sec-Fetch-Site": "cross-site"},
        )
        for headers in cases:
            with self.subTest(headers=headers):
                status, _ = self.request("GET", "/api/catalog", headers=headers)
                self.assertEqual(status, 403)

    def test_origin_configuration_rejects_wildcards_credentials_and_paths(self):
        for value in ("*", "http://*.example.test", "https://user:password@example.test", "http://localhost:8765/path",
                      "http://localhost:99999", "http://localhost:0", "http://localhost:8765?x=1", "http://localhost:8765#x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                viewer.origin_identity(value)
        self.assertEqual(viewer.origin_identity("HTTPS://Bridge.Example.Test:443/"), ("https", "bridge.example.test", 443))

    def test_http_import_returns_validated_complete_deal(self):
        status, result = self.request("POST", "/api/import?filename=test.json", json.dumps(BOARD))
        self.assertEqual(status, 200)
        self.assertEqual(result["boards"][0]["hands"], BOARD["hands"])
        status, result = self.request("POST", "/api/import?filename=test.json", '{"hands":{}}')
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()