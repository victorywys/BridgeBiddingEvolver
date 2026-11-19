import copy
import json
import tempfile
import unittest
from pathlib import Path

from bridge_evolve.evaluation.record_log import decode_records, encode_records, iter_records
from bridge_evolve.evolution.config import blank_system
from bridge_evolve.evolution.controller import Controller, initialize, register_system
from bridge_evolve.evolution.execution import Evaluator
from bridge_evolve.evolution.faults import adjudicate_baseline, score_contributions, validate_adjudication
from bridge_evolve.systems import validate_system


class BaselineFaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        baselines = []
        for name in ("broken-baseline", "normal-baseline"):
            raw = blank_system().to_data()
            raw["name"] = name
            if name == "broken-baseline":
                raw["fallback"] = {"kind": "error", "message": "No declared classic-policy completion"}
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(raw))
            baselines.append(str(path))
        self.config = {"profile": "smoke", "source": "research", "origins": ["blank"], "baselines": baselines,
                       "workers": 1, "max_seconds": 300, "max_pairs": 1000, "resamples": 100,
                       "boards": {"diagnosis": 2, "validity": 2, "screen": 2, "full": 2, "promotion": 2}}

    def evaluator(self, **updates):
        directory = initialize({**self.config, **updates}, self.project / f"run-{len(list(self.project.glob('run-*')))}",
                               self.project)
        controller = Controller(directory)
        evaluator = Evaluator(directory, controller.config, controller.state, controller.checkpoint, controller.stop)
        self.addCleanup(evaluator.close)
        return controller, evaluator

    def test_baseline_branch_is_a_visible_forfeit_and_no_longer_blocks_a_real_round(self):
        controller, evaluator = self.evaluator()
        identity = controller.state["champions"]["Z"]
        result = evaluator.evaluate("baseline-faults", [identity], 2)
        self.assertEqual(controller.config["baseline_fault_policy"], "baseline-forfeit-24")
        self.assertEqual(result["status"][identity], "checked")
        self.assertEqual(result["errors"][identity], [])
        broken, normal = result["anchors"]
        self.assertEqual(result["scores"][identity][broken], [24, 24])
        self.assertEqual(result["scores"][identity][normal], [0, 0])
        self.assertEqual(result["objective"][identity], 12)
        self.assertEqual(result["score_components"][identity][broken]["forfeit_pairs"], 2)
        records = list(iter_records(controller.directory / result["logs"][identity + "/" + broken]))
        for record in records:
            self.assertEqual(record["audit_result"]["status"], "adjudicated")
            self.assertTrue(record["adjudication"]["errors"])
            self.assertIsNone(record["tables"][0]["contract"])
            self.assertIsNone(record["point_difference"])
            self.assertEqual(decode_records(encode_records(iter([record])))[0], record)
        used = controller.state["evaluated_pairs"]
        repeated = evaluator.evaluate("baseline-faults", [identity], 2)
        self.assertEqual(repeated, result)
        self.assertEqual(controller.state["evaluated_pairs"], used)
        evaluator.config["baseline_fault_policy"] = "strict"
        with self.assertRaisesRegex(ValueError, "frozen baseline scoring policy"):
            evaluator.evaluate("baseline-faults", [identity], 2)

    def test_candidate_branch_errors_are_not_forgiven_by_the_opponent_policy(self):
        controller, evaluator = self.evaluator()
        broken = blank_system().to_data()
        broken["name"] = "broken-candidate"
        broken["fallback"] = {"kind": "error", "message": "Candidate missing branch"}
        identity = register_system(controller.directory, controller.state, validate_system(broken))
        result = evaluator.evaluate("candidate-faults", [identity], 1)
        self.assertNotEqual(result["status"][identity], "checked")
        self.assertNotIn(identity, result["objective"])
        self.assertTrue(result["errors"][identity])
        self.assertTrue(any(error.get("system_hash") == identity for error in result["errors"][identity]))

    def test_strict_policy_retains_the_original_invalid_score_behavior(self):
        controller, evaluator = self.evaluator(baseline_fault_policy="strict")
        identity = controller.state["champions"]["Z"]
        result = evaluator.evaluate("strict-baseline", [identity], 1)
        self.assertEqual(result["status"][identity], "blocked")
        self.assertNotIn(identity, result["objective"])
        self.assertIsNone(result["scores"][identity][result["anchors"][0]][0])

    def record(self):
        error = {"stage": "auction", "type": "SystemError", "table": "table1", "actor": "E",
                 "system_hash": "opponent", "message": "Unimplemented protocol branch: declared missing"}
        return {"number": 1, "imp": None, "point_difference": None, "dds": {"oracle": {"oracle_kind": "dds"}},
                "audit_result": {"status": "failed", "errors": [error]},
                "tables": [{"id": "table1", "error": error, "systems": {
                    "NS": {"hash": "candidate"}, "EW": {"hash": "opponent"}}},
                    {"id": "table2", "systems": {"NS": {"hash": "opponent"}, "EW": {"hash": "candidate"}}}]}

    def test_unknown_ownership_dds_errors_and_wrong_actor_are_not_adjudicated(self):
        for changes in ({"system_hash": None}, {"system_hash": "candidate"}, {"actor": "N"},
                        {"stage": "dds"}, {"message": "Some unrelated runtime failure"}):
            record = self.record()
            record["audit_result"]["errors"][0].update(changes)
            actual = adjudicate_baseline(record, "candidate", "opponent", "baseline-forfeit-24")
            self.assertIsNone(actual["imp"])
            self.assertNotIn("adjudication", actual)
        mixed = self.record()
        mixed["audit_result"]["errors"].append({"stage": "dds", "message": "Oracle failed"})
        self.assertNotIn("adjudication", adjudicate_baseline(mixed, "candidate", "opponent", "baseline-forfeit-24"))
        with self.assertRaises(ValueError):
            validate_adjudication({"adjudication": {"awarded_imp": 24}})

    def test_faulted_and_played_score_contributions_sum_to_the_actual_parent_gain(self):
        result = {"boards": 2, "anchors": ["A"], "baseline_fault_policy": "baseline-forfeit-24",
                  "score_components": {"new": {"A": {"played_imp_total": -2, "forfeit_imp_total": 24, "forfeit_pairs": 1}},
                                       "old": {"A": {"played_imp_total": 6, "forfeit_imp_total": 0, "forfeit_pairs": 0}}}}
        parts = score_contributions(result, "new", "old")
        self.assertEqual(parts["played_contribution"], -4)
        self.assertEqual(parts["forfeit_contribution"], 12)
        self.assertEqual(parts["total_delta"], 8)
        record = self.record()
        original = copy.deepcopy(record)
        decided = adjudicate_baseline(record, "candidate", "opponent", "baseline-forfeit-24")
        self.assertEqual(record, original)
        decided["adjudication"]["errors"][0]["system_hash"] = "candidate"
        with self.assertRaises(ValueError):
            validate_adjudication(decided)

    def test_new_policy_continuation_does_not_inherit_legacy_branch_faults_as_permanent_blockers(self):
        controller, _ = self.evaluator(baseline_fault_policy="strict")
        baseline = controller.config["baselines"][0]["hash"]
        controller.state["comparison_issues"] = {"old-branch": {"round": "legacy", "boards": 1, "error_count": 1,
            "errors": [{"stage": "auction", "type": "SystemError", "table": "table1", "actor": "E",
                        "system_hash": baseline, "message": "Unimplemented protocol branch: declared missing"}]}}
        controller.checkpoint()
        original = (controller.directory / "state.json").read_bytes()
        directory = initialize({**self.config, "champion_seed_run": str(controller.directory),
                                "baseline_fault_policy": "baseline-forfeit-24"},
                               self.project / "new-policy-continuation", self.project)
        next_controller = Controller(directory)
        self.assertFalse(next_controller.state.get("comparison_issues"))
        revision = next_controller.state["baseline_policy_revision"]
        self.assertEqual(revision["previous"], "strict")
        self.assertEqual(revision["current"], "baseline-forfeit-24")
        self.assertIn("old-branch", revision["historical_comparison_issues"])
        self.assertEqual(next_controller.state["rounds"], {})
        self.assertEqual(next_controller.state["agent_requests"], 0)
        self.assertEqual((controller.directory / "state.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
