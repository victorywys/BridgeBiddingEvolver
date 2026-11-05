import copy
import fcntl
import io
import json
import tempfile
import http.client
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from bridge_evolve.cli import main
from bridge_evolve.evolution.config import normalize_config, save_json
from bridge_evolve.evolution.controller import Controller, initialize, register_system, run, status
from bridge_evolve.evolution.execution import Evaluator, EvolutionStopped, column_summary, evaluate_pair, paired_gain
from bridge_evolve.evolution.proposals import agent_packet, apply_proposal, evidence_packet, numeric_proposals
from bridge_evolve.evolution.inspection import overview, pair_export
from bridge_evolve.systems import load_system, validate_system


def toy(name, threshold=None):
    return {
        "schema_version": 2, "operator_version": 2, "name": name, "scope": "general",
        "features": {}, "announcements": [],
        "protocol": {"initial": "idle", "parameters": {},
                     "states": {"idle": {"transitions": [], "otherwise": "idle"}}},
        "rules": [] if threshold is None else [{
            "id": "toy.invite.open", "priority": 1,
            "when": {"op": "and", "args": [
                {"op": "ge", "args": [{"op": "hcp"}, threshold]},
                {"op": "is_legal", "call": "1NT"}]},
            "action": {"kind": "call", "call": "1NT"}}],
        "fallback": {"kind": "call", "call": "P"},
    }


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.paths = []
        for name, threshold in (("A", 10), ("B", None)):
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(toy(name, threshold)))
            self.paths.append(str(path))
        self.config = {"profile": "smoke", "source": "numbers", "baselines": self.paths,
                       "modules": ["toy.invite"], "workers": 1, "random_seed": 9321,
                       "boards": {"diagnosis": 2, "screen": 2, "full": 3, "promotion": 3},
                       "max_seconds": 300, "max_pairs": 1000, "resamples": 100}

    def initialize(self, **updates):
        return initialize({**self.config, **updates}, self.project / ("run-" + str(len(list(self.project.glob("run-*"))))), self.project)

    def test_initialization_does_not_prereserve_deals_and_freezes_both_baselines(self):
        directory = self.initialize()
        state = status(directory)
        self.assertEqual(state["rounds"], {})
        self.assertEqual(state["status"], "ready")
        self.assertFalse((directory / "rounds").exists())
        self.assertEqual(len(state["champions"]), 2)
        self.assertEqual(len(state["system_files"]), 4)
        cfg = json.loads((directory / "config.json").read_text())
        self.assertEqual(cfg["goal_boards"], 10000)
        self.assertFalse(cfg["automatic_goal_check"])

    def test_every_round_draws_new_common_deals_and_resume_reuses_its_exact_draw(self):
        directory = self.initialize()
        controller = Controller(directory)
        evaluator = Evaluator(directory, controller.config, controller.state, controller.checkpoint, lambda: None)
        self.addCleanup(evaluator.close)
        systems = list(controller.state["champions"].values())
        first, boards1 = evaluator._round_data("first", systems, 3)
        again, repeated = evaluator._round_data("first", systems, 3)
        second, boards2 = evaluator._round_data("second", systems, 3)
        self.assertEqual(boards1, repeated)
        self.assertEqual(first["seed"], again["seed"])
        self.assertNotEqual(first["seed"], second["seed"])
        self.assertFalse({row["family_id"] for row in boards1} & {row["family_id"] for row in boards2})
        with self.assertRaisesRegex(ValueError, "Resume round"):
            evaluator._round_data("first", systems, 4)

    def test_atomic_ir_edits_do_not_modify_the_parent_or_allow_external_targets(self):
        system = load_system(Path(self.paths[0]))
        packet = evidence_packet(system, [], ["toy.invite"])
        proposal = numeric_proposals(system, packet)["proposals"][0]
        original = system.to_data()
        candidate = apply_proposal(system, packet, proposal)
        self.assertNotEqual(candidate.content_hash, system.content_hash)
        self.assertEqual(system.to_data(), original)
        for replacement in (
            {**proposal, "parent_hash": "stale"},
            {**proposal, "edits": [{"kind": "rule", "id": "outside", "field": "when", "value": True}]},
            {**proposal, "edits": [{"kind": [], "id": "bad", "field": "when", "value": True}]},
            {**proposal, "issue_ids": [{}]},
        ):
            with self.subTest(proposal=replacement), self.assertRaises(ValueError):
                apply_proposal(system, packet, replacement)

    def test_large_scenario_ledgers_stay_complete_without_overflowing_model_context(self):
        system = load_system(Path(self.paths[0]))
        records = [{
            "test_only": True, "dealer": "N", "vulnerability": "none", "hands": {},
            "imp": 0, "tables": [{"systems": {"NS": {"hash": system.content_hash}}, "auction": [{
                "seat": "N", "call": "P", "private_trace": {"rule_id": "toy.invite.open"},
                "public_context": {"fixture_case": index}}]}],
        } for index in range(10000)]
        with patch("bridge_evolve.evolution.proposals.agent_board", return_value={}), \
             patch("bridge_evolve.evolution.proposals.agent_decision", return_value={}):
            ledger = evidence_packet(system, records, ["toy.invite"])
        compact = agent_packet(ledger)
        self.assertEqual(ledger["total_groups"], 10000)
        self.assertEqual(len(ledger["deferred_groups"]), 9992)
        self.assertEqual(compact["deferred_group_count"], 9992)
        self.assertEqual(len(compact["deferred_groups"]), 32)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False).encode()), 160000)
        self.assertEqual(compact["selected_groups"], ledger["selected_groups"])
        ledger["previous_attempts"] = [{"hypothesis": "x" * 50000} for _ in range(6)]
        compact = agent_packet(ledger)
        self.assertEqual(len(ledger["previous_attempts"]), 6)
        self.assertGreater(compact["deferred_previous_attempts"], 0)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False).encode()), 160000)

    def test_paired_statistics_keep_common_indices_and_difference_of_objectives(self):
        values = [1, -2, 4, 0, 3]
        first, second = column_summary([values, values], seed=2, alpha=.025, resamples=200)
        self.assertEqual(first, second)
        result = {"scores": {"C": {"B1": [2, 3], "B2": [4, 5]},
                             "I": {"B1": [1, 2], "B2": [3, 4]}}}
        gain = paired_gain(result, "C", "I", ["B1", "B2"], seed=1, alpha=.005, resamples=100)
        self.assertEqual((gain["mean"], gain["lower"], gain["opponent_deltas"]), (1, 1, [1, 1]))
        for columns in ([], [[]], [[1], [1, 2]], [[True]], [[float("nan")]]):
            with self.subTest(columns=columns), self.assertRaises(ValueError):
                column_summary(columns, seed=1, alpha=.025, resamples=100)

    def test_real_dds_three_generation_number_source_loop_and_resume(self):
        directory = self.initialize()
        result = run(directory)
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["generation"], 3)
        self.assertEqual(result["goal"]["status"], "not_checked")
        self.assertEqual(result["agent_requests"], 0)
        self.assertGreater(result["evaluated_pairs"], 0)
        seeds = [value["seed"] for value in result["rounds"].values()]
        self.assertEqual(len(seeds), len(set(seeds)))
        self.assertTrue((directory / "report.md").is_file())
        detail = overview(directory, details=True)
        self.assertEqual(len(detail["generations"]), 3)
        first_round = next(row for row in detail["rounds"] if row.get("pairs"))
        first_pair = first_round["pairs"][0]
        archive = pair_export(directory, first_round["id"], first_pair["candidate"], first_pair["opponent"], limit=1)
        from test_board_viewer import viewer
        imported = viewer.import_file(archive, "evolution-pair.zip")
        self.assertEqual(imported["statistics"]["boards"], 1)
        self.assertTrue(imported["boards"][0]["tables"][0]["auction"])
        before = result["evaluated_pairs"]
        replayed = run(directory)
        self.assertEqual(replayed["evaluated_pairs"], before)
        self.assertEqual([value["seed"] for value in replayed["rounds"].values()], seeds)

    def test_agent_budget_does_not_reset_or_fall_back_to_numeric_proposals(self):
        directory = self.initialize(source="agent", max_agent_requests=1)
        with patch("bridge_evolve.evolution.controller.LocalAgent") as client:
            client.return_value.propose.return_value = {"analysis": [], "proposals": [], "source": "agent"}
            first = run(directory)
            second = run(directory)
        self.assertEqual(first["status"], "budget_exhausted")
        self.assertEqual(second["agent_requests"], 1)
        self.assertEqual(client.return_value.propose.call_count, 1)
        self.assertFalse(any(generation["candidates"] for generation in second["generations"].values()))

    def test_cancel_before_start_and_tampered_config_are_explicit(self):
        directory = self.initialize()
        (directory / "cancel-requested").touch()
        cancelled = run(directory)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["rounds"], {})
        cfg_path = directory / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["max_pairs"] += 1
        cfg_path.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(ValueError, "differs"):
            Controller(directory)

    def test_artifact_tampering_is_not_accepted_on_resume(self):
        directory = self.initialize()
        controller = Controller(directory)
        path = directory / "analysis/example.json"
        controller.artifact(path, {"parent_hash": "fixture"})
        path.write_text('{"parent_hash":"altered"}')
        with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
            controller.read_artifact(path)

    def test_cli_is_registered_and_requires_real_configuration(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["evolve", "status", "--run", str(self.initialize())]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ready")
        for update in ({"workers": True}, {"generations": 0}, {"source": "fake"}, {"boards": {"test": 10000}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                normalize_config({**self.config, **update}, self.project)

    def test_local_agent_is_text_only_and_does_not_expose_its_key(self):
        from bridge_evolve.evolution.proposals import LocalAgent
        packet = {"parent_hash": "parent", "selected_groups": [], "rules": [], "transitions": []}
        body = {"output": [{"type": "message", "content": [{"type": "output_text",
                 "text": '{"analysis":[],"proposals":[]}'}]}], "usage": {"input_tokens": 10, "output_tokens": 5}}

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, _):
                return json.dumps(body).encode()

        with patch("bridge_evolve.evolution.proposals._gateway_key", return_value="unit-only-key"), \
                patch("bridge_evolve.evolution.proposals.build_opener") as opener:
            opener.return_value.open.return_value = Response()
            result = LocalAgent(self.project, "unit-model", 10).propose(packet)
        payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(payload["tools"], [])
        self.assertFalse(payload["store"])
        self.assertEqual(result["source"], "agent")
        self.assertNotIn("unit-only-key", json.dumps(result))

    def test_original_champions_do_not_waste_a_10000_board_check(self):
        directory = self.initialize(generations=1)
        result = run(directory)
        controller = Controller(directory)
        anchors = [value["hash"] for value in controller.config["baselines"]]
        controller.state["champions"] = dict(zip(("A", "B"), anchors, strict=True))
        controller.verify_goal(force=True)
        self.assertEqual(controller.state["goal"]["status"], "not_checked")
        self.assertEqual(controller.state["goal_checks"], 0)
        self.assertNotIn("goal-10000", controller.state["rounds"])

    def test_strategy_findings_preserve_real_scores_and_remain_visible_to_analysis(self):
        systems = {load_system(Path(path)).content_hash: load_system(Path(path)) for path in self.paths}
        a, b = systems
        record = {"test_only": True, "dds": {"oracle": {"oracle_kind": "fixture"}}, "imp": 3, "point_difference": 100,
                  "audit_result": {"status": "completed-with-findings", "errors": []},
                  "tables": [{"id": "table1", "auction": [{"seat": "N", "system_hash": a}]}]}
        finding = {"id": "precision-shortness-terminal-suit", "table": "table1", "turn": 0,
                   "description": "A diagnostic strategy weakness, not an execution or information violation."}
        engine = Mock()
        engine.batch_board.return_value = (record, [], [finding])
        with patch("bridge_evolve.evolution.execution._worker_engine", engine), \
             patch("bridge_evolve.evolution.execution._worker_systems", str(self.project)), \
             patch("bridge_evolve.evolution.execution.frozen_system", side_effect=lambda _, identity: systems[identity]):
            actual = evaluate_pair((a, b, {"index": 0}, 1))
        self.assertEqual(actual["imp"], 3)
        self.assertEqual(actual["audit_result"]["status"], "completed-with-findings")
        self.assertEqual(actual["audit_result"]["errors"], [])
        self.assertEqual(actual["audit_result"]["findings"][0]["system_hash"], a)

    def test_real_execution_errors_invalidate_candidates_and_never_award_opponent_crash_wins(self):
        broken = toy("intentional-error-fixture")
        broken["fallback"] = {"kind": "error", "message": "intentional evolution ownership fixture"}
        path = self.project / "broken.json"
        path.write_text(json.dumps(broken))
        bad = load_system(path)
        for owner in ("candidate", "anchor"):
            with self.subTest(owner=owner):
                directory = self.initialize(baselines=self.paths if owner == "candidate" else [str(path), self.paths[1]])
                controller = Controller(directory)
                controller.state["test_only"] = True
                candidate = bad if owner == "candidate" else load_system(Path(self.paths[0]))
                identity = register_system(directory, controller.state, candidate)
                evaluator = Evaluator(directory, controller.config, controller.state, controller.checkpoint, lambda: None)
                try:
                    result = evaluator.evaluate("intentional-hard-error", [identity], 1)
                finally:
                    evaluator.close()
                self.assertEqual(result["status"][identity], "invalid" if owner == "candidate" else "blocked")
                self.assertNotIn(identity, result["objective"])
                self.assertTrue(result["errors"][identity])
                self.assertTrue(all(error.get("system_hash") == bad.content_hash for error in result["errors"][identity]))

    def test_dead_controllers_are_interrupted_but_live_file_locks_remain_running(self):
        directory = self.initialize()
        state = status(directory)
        state["status"] = "running"
        save_json(directory / "state.json", state)
        self.assertEqual(status(directory)["status"], "interrupted")
        with (directory / "controller.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(status(directory)["status"], "running")
        self.assertEqual(status(directory)["status"], "interrupted")

    def test_run_reloads_the_latest_checkpoint_after_acquiring_the_lock(self):
        directory = self.initialize()
        controller = Controller(directory)
        saved = status(directory)
        saved.update(agent_requests=7, test_only=True, oracle={"oracle_kind": "fixture"})
        save_json(directory / "state.json", saved)
        with patch("bridge_evolve.evolution.controller.Evaluator"), \
             patch.object(controller, "generation", side_effect=EvolutionStopped("unit-test stop before scoring")):
            result = controller.run()
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["agent_requests"], 7)
        self.assertTrue(result["test_only"])

    def test_goal_requires_10000_paired_boards_and_corrected_20000_resample_statistics(self):
        directory = self.initialize()
        controller = Controller(directory)
        parent = controller.system(controller.state["champions"]["A"])
        packet = evidence_packet(parent, [], ["toy.invite"])
        candidate = apply_proposal(parent, packet, numeric_proposals(parent, packet)["proposals"][0])
        selected = register_system(directory, controller.state, candidate)
        controller.state.update(test_only=True, oracle={"oracle_kind": "fixture"})
        controller.state["champions"]["A"] = selected
        anchors = [value["hash"] for value in controller.config["baselines"]]
        result = {"status": {selected: "checked"}, "anchors": anchors, "seed": 1,
                  "means": {selected: {anchor: 1 for anchor in anchors}}, "objective": {selected: 1},
                  "scores": {selected: {anchor: [1] * 10000 for anchor in anchors}}}
        controller.evaluator = Mock()
        controller.evaluator.evaluate.return_value = result
        with patch.object(controller, "round", return_value=result), \
             patch("bridge_evolve.evolution.controller.column_summary", wraps=column_summary) as statistics:
            controller.verify_goal()
        controller.evaluator.evaluate.assert_called_once_with("goal-10000", [selected], 10000)
        self.assertEqual(statistics.call_args.kwargs["alpha"], .025)
        self.assertEqual(statistics.call_args.kwargs["resamples"], 20000)
        self.assertEqual(controller.state["goal"]["status"], "target_met")
        self.assertEqual(controller.state["goal_checks"], 1)
        self.assertTrue(controller.state["test_only"])

    def test_interrupted_promotion_resumes_its_allocated_attempt_without_double_spending(self):
        directory = self.initialize(generations=1, max_promotions=2)

        class ScriptedEvaluator:
            paused = False

            def __init__(self, directory, config, state, checkpoint, stop):
                self.directory, self.config, self.state = directory, config, state
                state.update(test_only=True, oracle={"oracle_kind": "fixture"})

            def close(self):
                pass

            def records(self, *_):
                return iter([])

            def evaluate(self, identity, systems, count):
                if "promotion" in identity and not ScriptedEvaluator.paused:
                    ScriptedEvaluator.paused = True
                    raise EvolutionStopped("unit-test interruption after allocating confirmation")
                anchors = [value["hash"] for value in self.config["baselines"]]
                means, scores = {}, {}
                for candidate in systems:
                    raw = json.loads((self.directory / "systems" / f"{candidate}.json").read_text())
                    threshold = raw["rules"][0]["when"]["args"][0]["args"][1] if raw["rules"] else 10
                    value = threshold - 10
                    scores[candidate] = {anchor: [value] * count for anchor in anchors}
                    means[candidate] = {anchor: value for anchor in anchors}
                return {"id": identity, "seed": 1, "anchors": anchors, "scores": scores, "means": means,
                        "objective": {candidate: next(iter(value.values())) for candidate, value in means.items()},
                        "status": {candidate: "checked" for candidate in systems}, "errors": {candidate: [] for candidate in systems}}

        with patch("bridge_evolve.evolution.controller.Evaluator", ScriptedEvaluator):
            first = run(directory)
            self.assertEqual(first["status"], "budget_exhausted")
            self.assertEqual(first["promotion_attempts"], 1)
            self.assertEqual(first["generations"]["1"]["promotions"]["A"]["status"], "allocated")
            second = run(directory)
        self.assertEqual(second["status"], "completed", second.get("error"))
        self.assertEqual(second["promotion_attempts"], 1)
        self.assertEqual(second["generations"]["1"]["promotions"]["A"]["status"], "promoted")
        self.assertTrue(second["test_only"])


class EvolutionHTTPTests(unittest.TestCase):
    def test_initialized_and_abandoned_queued_experiments_can_be_started_after_restart(self):
        from test_board_viewer import ROOT, viewer
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "systems").mkdir()
            paths = [project / "systems" / name for name in ("general-v2.json", "pass-v2.json")]
            for path in paths:
                path.write_bytes((ROOT / "systems" / path.name).read_bytes())
            directory = initialize({"profile": "smoke", "source": "numbers", "baselines": [str(path) for path in paths]}, project=project)
            state = viewer.ViewerState(project)
            self.addCleanup(state.executor.shutdown)
            self.assertEqual(state.evolution_detail(directory.name)["status"], "ready")
            saved = status(directory)
            saved["status"] = "queued"
            save_json(directory / "state.json", saved)
            self.assertEqual(state.evolution_detail(directory.name)["status"], "interrupted")
            future = Future()
            with patch.object(state.executor, "submit", return_value=future):
                resumed = state.resume_evolution(directory.name)
            self.assertEqual(resumed["status"], "queued")
            self.assertFalse((directory / "rounds").exists())
            future.cancel()
            state.runtime_snapshot = {}
            with self.assertRaisesRegex(ValueError, "重启"):
                state.resume_evolution(directory.name)
            a, b = state.systems
            with self.assertRaisesRegex(ValueError, "重启"):
                state.start_evolution({"system_a": a, "system_b": b, "source": "numbers"})

    def test_ui_factory_status_pair_import_and_archive_use_existing_history_management(self):
        from test_board_viewer import ROOT, viewer
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "systems").mkdir()
            for name in ("general-v2.json", "pass-v2.json"):
                (project / "systems" / name).write_bytes((ROOT / "systems" / name).read_bytes())
            state = viewer.ViewerState(project)
            server = viewer.ViewerServer(("127.0.0.1", 0), state)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()

            def request(method, path, body=None, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=180)
                connection.request(method, path, json.dumps(body) if body is not None else None, headers or {})
                response = connection.getresponse()
                code, contents = response.status, response.read()
                connection.close()
                return code, contents

            try:
                a, b = state.systems
                code, response = request("POST", "/api/evolutions", {
                    "system_a": a, "system_b": b, "profile": "smoke", "source": "numbers", "generations": 1, "workers": 1})
                self.assertEqual(code, 202, response)
                identifier = json.loads(response)["id"]
                state.executor.submit(lambda: None).result(timeout=180)
                code, contents = request("GET", "/api/evolutions/" + identifier)
                detail = json.loads(contents)
                self.assertEqual(code, 200)
                self.assertEqual(detail["status"], "completed", detail)
                self.assertEqual(detail["generation"], 1)
                row = next(row for row in detail["rounds"] if row.get("pairs"))
                pair = row["pairs"][0]
                code, archive = request("GET", f"/api/evolutions/{identifier}/rounds/{row['id']}/pairs/{pair['candidate']}/{pair['opponent']}")
                self.assertEqual(code, 200)
                self.assertEqual(len(viewer.import_file(archive, "pair.zip")["boards"]), 16)
                self.assertIn(identifier, [item["id"] for item in state.library_entries()["entries"]])
                state.organize_library({"ids": [identifier], "action": "trash", "confirm": True})
                self.assertEqual(state.evolution_history()["experiments"], [])
                code, contents = request("GET", "/api/evolutions/" + identifier)
                self.assertEqual(code, 422)
                self.assertIn("回收站", json.loads(contents)["error"])
                code, _ = request("GET", "/api/evolutions", headers={"Origin": "https://not-approved.invalid"})
                self.assertEqual(code, 403)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
