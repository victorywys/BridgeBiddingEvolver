import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.cli import main
from bridge_evolve.evolution.config import blank_system
from bridge_evolve.evolution.controller import Controller, initialize, run, status
from bridge_evolve.evolution.execution import Evaluator, EvolutionStopped
from bridge_evolve.evolution.proposals import evidence_packet
from bridge_evolve.evolution.models import ModelClient
from bridge_evolve.evolution.research_tools import ResearchTools
from test_research import BOARD, response
from contextlib import redirect_stderr, redirect_stdout


class EvaluationQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        baselines = []
        for name in ("anchor-A", "anchor-B"):
            raw = blank_system().to_data()
            raw["name"] = name
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(raw))
            baselines.append(str(path))
        self.config = {"profile": "smoke", "source": "research", "baselines": baselines, "origins": ["blank"],
                       "generations": 1, "workers": 1, "boards": {"diagnosis": 2, "validity": 2, "screen": 2, "full": 2, "promotion": 2},
                       "max_agent_requests": 20, "max_agent_tokens": 2000000, "max_seconds": 600,
                       "evaluation_seconds": 120, "research_seconds": 240, "max_pairs": 1000, "resamples": 100}
        self.directory = initialize(self.config, self.project / "experiment", self.project)
        self.controller = Controller(self.directory)
        self.parent = self.controller.state["champions"]["Z"]
        self.packet = evidence_packet(self.controller.system(self.parent), [], ["*"])
        self.raw = blank_system().to_data()
        self.raw["name"] = "queue-test-protocol"
        self.raw["rules"] = [{"id": "queue.open", "priority": 1,
            "when": {"op": "not", "args": [{"op": "has_contract_bid"}]}, "action": {"kind": "call", "call": "1C"}}]
        self.tools = ResearchTools(self.controller, "g001-Z", self.parent, self.packet, self.raw, None)

    def publish(self):
        case = self.tools.execute("case", "add_cases", {"cases": [{"name": "opening", "board": BOARD}]})
        case_id = case["cases"][0]["case_id"]
        self.tools.execute("tests", "write_tests", {"tests": [{
            "name": "actual-opening", "case_id": case_id, "mode": "decision", "changed_from_parent": True,
            "assertions": [{"path": "/call", "op": "eq", "value": "1C"}]}]})
        published = self.tools.execute("publish", "publish_candidate", {"hypothesis": "自然开叫与空白父本行为不同。"})
        self.assertTrue(published["ok"], published)
        return published

    def test_published_candidate_finishes_without_another_model_request_and_resume_is_idempotent(self):
        published = self.publish()
        job_id = published["evaluation"]["id"]
        self.assertEqual(self.controller.state["evaluation_queue"][job_id]["status"], "queued")
        with patch("bridge_evolve.evolution.research.ModelClient", side_effect=AssertionError("Model initialized")), \
             patch("bridge_evolve.evolution.models.ModelClient", side_effect=AssertionError("Model initialized")):
            result = run(self.directory, evaluation_only=True)
            job = result["evaluation_queue"][job_id]
            self.assertNotIn(job["status"], ("queued", "running", "budget_blocked", "budget_exhausted"))
            self.assertEqual(result["status"], "completed", result.get("error"))
            self.assertEqual(result["agent_requests"], 0)
            self.assertEqual(result["promotion_attempts"], 0)
            self.assertEqual(result["goal"]["status"], "not_checked")
            self.assertEqual(set(job["rounds"]), {"validity", "screen", "full"})
            used = result["evaluated_pairs"]
            rounds = copy.deepcopy(result["rounds"])
            again = run(self.directory, evaluation_only=True)
        self.assertEqual(again["evaluated_pairs"], used)
        self.assertEqual(again["rounds"], rounds)
        self.assertEqual(job["remaining_pairs"], 0)
        self.assertEqual(job["remaining_seconds"], 0)

    def test_same_candidate_occurrences_share_one_queue_and_one_allocation(self):
        published = self.publish()
        first = self.controller.evaluations.jobs[published["evaluation"]["id"]]
        repeated = self.controller.evaluations.enqueue(published["release"], lineage="Z", generation=2)
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(len(self.controller.evaluations.jobs), 1)
        self.assertEqual(len(repeated["occurrences"]), 2)
        self.assertEqual(self.controller.evaluations.held_seconds(), self.config["evaluation_seconds"])

    def test_budget_shortage_retains_a_visible_intent_without_consuming_models_or_pairs(self):
        self.controller.config["max_pairs"] = 1
        published = self.publish()
        job = self.controller.evaluations.jobs[published["evaluation"]["id"]]
        self.assertEqual(job["status"], "budget_blocked")
        self.assertEqual(job["remaining_pairs"], 0)
        self.assertEqual(job["remaining_seconds"], 0)
        self.assertEqual(self.controller.state["evaluated_pairs"], 0)
        self.assertEqual(self.controller.state["agent_requests"], 0)
        self.assertTrue((self.directory / published["release"]).is_file())

    def test_expired_crash_time_lease_is_not_silently_refunded(self):
        published = self.publish()
        job = self.controller.evaluations.jobs[published["evaluation"]["id"]]
        job.update(status="running", lease_updated_at="2000-01-01T00:00:00+00:00")
        self.controller.checkpoint()
        result = run(self.directory, evaluation_only=True)
        saved = result["evaluation_queue"][job["id"]]
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(saved["status"], "budget_exhausted")
        self.assertEqual(saved["remaining_pairs"], 0)
        self.assertEqual(saved["spent_seconds"], saved["reserved_seconds"])
        self.assertEqual(result["agent_requests"], 0)

    def test_model_timeout_cannot_consume_reserved_evaluation_time(self):
        blocked = ModelClient(self.controller.config, self.controller.state, self.controller.checkpoint,
                              lambda: None, request_timeout=lambda: 0)
        with patch("bridge_evolve.evolution.models._gateway_key", side_effect=AssertionError("Credentials requested")):
            with self.assertRaisesRegex(EvolutionStopped, "unreserved research time"):
                blocked.request([], [], label="no-window")
        self.assertEqual(self.controller.state["agent_requests"], 0)
        client = ModelClient(self.controller.config, self.controller.state, self.controller.checkpoint,
                             lambda: None, request_timeout=lambda: 2)
        client.key = "test-key"
        with patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response(text="ok")).encode())
            client.request([], [], label="bounded-window")
        self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 2)

    def test_pair_reservations_protect_queue_and_retries_do_not_charge_twice(self):
        published = self.publish()
        job = self.controller.evaluations.jobs[published["evaluation"]["id"]]
        self.controller.config["max_pairs"] = job["reserved_pairs"] + 2
        evaluator = Evaluator(self.directory, self.controller.config, self.controller.state,
                              self.controller.checkpoint, self.controller.stop)
        self.addCleanup(evaluator.close)
        evaluator.reserve_evaluation(job["id"], job["reserved_pairs"])
        self.assertTrue(evaluator.reserve_pair("lab-1"))
        self.assertTrue(evaluator.reserve_pair("lab-2"))
        with self.assertRaises(EvolutionStopped):
            evaluator.reserve_pair("lab-3")
        evaluator.active_reservation = job["id"]
        self.assertTrue(evaluator.reserve_pair("reserved-pair"))
        before = self.controller.state["evaluated_pairs"]
        self.assertFalse(evaluator.reserve_pair("reserved-pair"))
        self.assertEqual(self.controller.state["evaluated_pairs"], before)
        self.assertEqual(evaluator.remaining_evaluation_pairs(job["id"]), job["reserved_pairs"] - 1)

    def test_interrupt_after_completed_round_reuses_exact_draw_cache_and_budget(self):
        published = self.publish()
        original = Controller.round
        interrupted = False

        def stop_after_round(controller, identity, systems, stage):
            nonlocal interrupted
            result = original(controller, identity, systems, stage)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return result

        with patch.object(Controller, "round", stop_after_round):
            first = run(self.directory, evaluation_only=True)
        self.assertEqual(first["status"], "interrupted")
        job = first["evaluation_queue"][published["evaluation"]["id"]]
        validity = job["rounds"]["validity"]
        original_draw = copy.deepcopy(first["rounds"][validity])
        completed = run(self.directory, evaluation_only=True)
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(completed["rounds"][validity], original_draw)
        self.assertEqual(completed["evaluated_pairs"], job["reserved_pairs"])
        self.assertFalse((self.directory / "rounds" / validity / "logs-2").exists())

    def test_later_research_budget_error_does_not_starve_a_published_candidate(self):
        count = 0

        def reply(*_args, **_kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return response(("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.raw}]}))
            if count == 2:
                return response(("case", "add_cases", {"cases": [{"name": "opening", "board": BOARD}]}))
            if count == 3:
                case_id = canonical_hash({"call": "case", "index": 0})[:16]
                return response(("tests", "write_tests", {"tests": [{"name": "opening", "case_id": case_id, "mode": "decision",
                    "changed_from_parent": True, "assertions": [{"path": "/call", "op": "eq", "value": "1C"}]}]}))
            if count == 4:
                return response(("publish", "publish_candidate", {"hypothesis": "完整开叫候选。"}))
            raise EvolutionStopped("Test-only later model budget exhaustion")

        with patch("bridge_evolve.evolution.research.ModelClient") as factory:
            factory.return_value.request.side_effect = reply
            result = run(self.directory)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(len(result["evaluation_queue"]), 1)
        job = next(iter(result["evaluation_queue"].values()))
        self.assertIn("outcome", job)
        self.assertEqual(set(job["rounds"]), {"validity", "screen", "full"})
        self.assertEqual(result["promotion_attempts"], 0)

    def test_portable_release_import_preserves_provenance_and_deduplicates_before_evaluation(self):
        published = self.publish()
        release = self.controller.read_artifact(self.directory / published["release"])
        folder = self.project / "published"
        folder.mkdir()
        for identity in (release["hash"], release["parent"]):
            (folder / f"{identity}.json").write_text(json.dumps(self.controller.system(identity).to_data()))
        snapshots = []
        for position in range(2):
            data = {**release, "origin": f"g{position + 1:03d}-Z"}
            filename = f"release-{position}.json"
            (folder / filename).write_text(json.dumps(data))
            snapshots.append({"hash": release["hash"], "parent": release["parent"], "release": filename,
                "source_run": self.directory.name, "source_engine": self.controller.state["engine_hash"],
                "source_model": self.controller.config["model"]})
        index = folder / "index.json"
        index.write_text(json.dumps({"schema": "bridge-published-research-archive/v1",
            "source_baselines": self.controller.config["baselines"], "snapshots": snapshots}))
        before = (self.directory / "state.json").read_bytes()
        imported = initialize({**self.config, "evaluation_sources": [str(index)]}, self.project / "evaluation-only", self.project)
        saved = status(imported)
        self.assertEqual(len(saved["evaluation_queue"]), 1)
        job = next(iter(saved["evaluation_queue"].values()))
        self.assertEqual(len(job["occurrences"]), 2)
        self.assertEqual(saved["agent_requests"], 0)
        self.assertFalse(saved["rounds"])
        self.assertEqual((self.directory / "state.json").read_bytes(), before)
        result = run(imported, evaluation_only=True)
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["agent_requests"], 0)

    def test_persistent_evaluation_only_configuration_never_starts_research_on_resume(self):
        self.publish()
        config = {**self.config, "evaluation_only": True}
        directory = initialize(config, self.project / "permanent-evaluation", self.project)
        with patch("bridge_evolve.evolution.research.ModelClient", side_effect=AssertionError("Model initialized")):
            result = run(directory)
        self.assertEqual(result["status"], "failed")
        self.assertIn("No frozen candidate evaluations", result["error"])
        self.assertEqual(result["agent_requests"], 0)

    def test_a_stopped_evaluation_run_can_be_a_new_release_source_without_copying_scores(self):
        self.publish()
        completed = run(self.directory, evaluation_only=True)
        original = (self.directory / "state.json").read_bytes()
        imported = initialize({**self.config, "evaluation_only": True, "evaluation_sources": [str(self.directory)]},
                              self.project / "evaluation-transfer", self.project)
        state = status(imported)
        self.assertEqual(len(state["evaluation_queue"]), 1)
        self.assertEqual(state["rounds"], {})
        self.assertEqual(state["agent_requests"], 0)
        self.assertEqual(state["promotion_attempts"], 0)
        with patch("bridge_evolve.evolution.research.ModelClient", side_effect=AssertionError("Model initialized")):
            result = run(imported)
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertNotEqual([row["seed"] for row in result["rounds"].values()],
                            [row["seed"] for row in completed["rounds"].values()])
        self.assertEqual((self.directory / "state.json").read_bytes(), original)

    def test_existing_evaluation_cli_cannot_silently_override_frozen_model_configuration(self):
        error = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(error):
            code = main(["evolve", "evaluate", "--run", str(self.directory), "--model", "different-model"])
        self.assertEqual(code, 1)
        self.assertIn("not both", error.getvalue())

    def test_malformed_imported_regression_contract_is_explicitly_invalid_not_a_fake_score(self):
        published = self.publish()
        release = self.controller.read_artifact(self.directory / published["release"])
        release["test_definitions"] = [{"name": "missing-fields"}]
        path = self.directory / "malformed-release.json"
        self.controller.artifact(path, release)
        job = self.controller.evaluations.enqueue(path.name, lineage="Z")
        result = run(self.directory, evaluation_only=True)
        self.assertEqual(result["evaluation_queue"][job["id"]]["status"], "invalid")
        self.assertEqual(result["evaluation_queue"][job["id"]]["tests"]["failed"], 1)
        self.assertFalse(result["evaluation_queue"][job["id"]]["rounds"])

    def test_imported_old_baseline_branch_faults_are_archived_not_reactivated_under_new_policy(self):
        self.publish()
        baseline = self.controller.config["baselines"][0]["hash"]
        self.controller.state["comparison_issues"] = {"old": {"round": "old", "boards": 1, "error_count": 1,
            "errors": [{"stage": "auction", "type": "SystemError", "table": "table1", "actor": "E",
                        "system_hash": baseline, "message": "Unimplemented protocol branch: old missing branch"}]}}
        self.controller.checkpoint()
        before = (self.directory / "state.json").read_bytes()
        directory = initialize({**self.config, "evaluation_only": True, "evaluation_sources": [str(self.directory)]},
                               self.project / "archived-old-opponent-issues", self.project)
        imported = status(directory)
        self.assertFalse(imported.get("comparison_issues"))
        self.assertIn("old", imported["baseline_policy_revision"]["historical_comparison_issues"])
        completed = run(directory)
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertEqual(completed["agent_requests"], 0)
        self.assertEqual((self.directory / "state.json").read_bytes(), before)

    def test_metadata_recovery_preserves_original_scores_engine_and_pre_recovery_state(self):
        published = self.publish()
        completed = run(self.directory, evaluation_only=True)
        self.assertEqual(completed["status"], "completed")
        baseline = self.controller.config["baselines"][0]["hash"]
        completed.update(status="blocked", error="Historical imported issue only", comparison_issues={
            "old": {"round": "old-source-round", "boards": 1, "error_count": 1, "errors": [
                {"stage": "auction", "type": "SystemError", "table": "table1", "actor": "E",
                 "system_hash": baseline, "message": "Unimplemented protocol branch: historical only"}]}})
        from bridge_evolve.evolution.config import save_json
        from bridge_evolve.evolution.faults import recover_completed_reassessment
        save_json(self.directory / "state.json", completed)
        original = (self.directory / "state.json").read_bytes()
        outcomes = copy.deepcopy(completed["evaluation_queue"])
        rounds = copy.deepcopy(completed["rounds"])
        recovery = recover_completed_reassessment(self.directory, reason="Fixture metadata-only recovery.")
        fixed = status(self.directory)
        self.assertEqual(fixed["status"], "completed")
        self.assertFalse(fixed.get("comparison_issues"))
        self.assertEqual(fixed["engine_hash"], completed["engine_hash"])
        self.assertEqual(fixed["config_hash"], completed["config_hash"])
        self.assertEqual(fixed["evaluation_queue"], outcomes)
        self.assertEqual(fixed["rounds"], rounds)
        self.assertEqual((self.directory / recovery["before_state"]).read_bytes(), original)
        self.assertEqual(fixed["agent_requests"], 0)
        self.assertEqual(recover_completed_reassessment(self.directory, reason="Idempotent fixture."), recovery)
        self.assertEqual(len(status(self.directory)["recoveries"]), 1)


if __name__ == "__main__":
    unittest.main()
