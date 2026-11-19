import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_evolve.core import Seat
from bridge_evolve.evaluation.data import board_data, family_hash, generate_dataset, load_dataset
from bridge_evolve.evolution.config import blank_system, normalize_config
from bridge_evolve.evolution.controller import Controller, initialize, run
from bridge_evolve.evolution.execution import Evaluator
from bridge_evolve.evolution.inspection import overview
from bridge_evolve.evolution.research_tools import ResearchTools
from bridge_evolve.evolution.sampling import check_cohort, conditional_source, descriptor, normalize_sampling
from bridge_evolve.systems.inputs import parse_board
from bridge_evolve.systems.runtime import hand_hcp
from test_research import BOARD


class ConditionalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        paths = []
        for name in ("local-anchor-A", "local-anchor-B"):
            raw = blank_system().to_data()
            raw["name"] = name
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(raw))
            paths.append(str(path))
        self.sampling = {"name": "North balanced 15-17", "dealer": "N", "constraints": {"N": {"hcp": [15, 17]}},
                         "hand_conditions": {"N": {"op": "balanced"}}, "attempt_limit": 100000}
        self.config = {"profile": "smoke", "source": "research", "baselines": paths, "origins": ["blank"],
                       "generations": 1, "workers": 1, "development_sampling": self.sampling,
                       "allow_promotion": False, "automatic_goal_check": False, "max_seconds": 300,
                       "evaluation_seconds": 120, "max_pairs": 1000, "resamples": 100,
                       "boards": {"diagnosis": 2, "validity": 2, "screen": 2, "full": 2, "promotion": 2}}
        self.directory = initialize(self.config, self.project / "run", self.project)
        self.controller = Controller(self.directory)

    def evaluator(self):
        evaluator = Evaluator(self.directory, self.controller.config, self.controller.state,
                              self.controller.checkpoint, self.controller.stop)
        self.addCleanup(evaluator.close)
        return evaluator

    def publish(self):
        parent = self.controller.state["champions"]["Z"]
        raw = blank_system().to_data()
        raw["name"] = "local-test-protocol"
        raw["rules"] = [{"id": "local.open", "priority": 1, "when": {"op": "not", "args": [{"op": "has_contract_bid"}]},
                         "action": {"kind": "call", "call": "1C"}}]
        tools = ResearchTools(self.controller, "g001-Z", parent, {"selected_groups": []}, raw, None)
        case = tools.execute("case", "add_cases", {"cases": [{"name": "opening", "board": BOARD}]})["cases"][0]["case_id"]
        tools.execute("tests", "write_tests", {"tests": [{"name": "natural-opening", "case_id": case,
            "mode": "auction", "changed_from_parent": True, "assertions": [
                {"path": "/auction/0/call", "op": "eq", "value": "1C"}]}]})
        release = tools.execute("publish", "publish_candidate", {"hypothesis": "Fixture natural action change."})
        self.assertTrue(release["ok"], release)
        return release

    def test_conditional_draws_match_real_hands_are_shared_fresh_and_resume_without_redrawing(self):
        evaluator = self.evaluator()
        systems = list(self.controller.state["champions"].values())
        first, rows = evaluator._round_data("local-first", systems, 6)
        repeated, replayed = evaluator._round_data("local-first", systems, 6)
        second, newer = evaluator._round_data("local-second", systems, 6)
        self.assertEqual(rows, replayed)
        self.assertEqual(first["seed"], repeated["seed"])
        self.assertNotEqual(first["seed"], second["seed"])
        self.assertFalse({row["family_id"] for row in rows} & {row["family_id"] for row in newer})
        for index, row in enumerate(rows):
            check_cohort(self.controller.config["development_sampling"], row, index)
            self.assertEqual(row["board"]["dealer"], "N")
            self.assertTrue(15 <= hand_hcp(parse_board(row["board"]).deal.hand(Seat.NORTH)) <= 17)
        manifest, _ = load_dataset(self.directory / first["dataset"])
        self.assertEqual(manifest["sampling"]["schema"], descriptor(self.controller.config["development_sampling"])["schema"])
        self.assertEqual(manifest["sampling"]["draw"]["accepted"], 6)
        self.controller.config["development_sampling"]["name"] = "changed-after-freeze"
        with self.assertRaisesRegex(ValueError, "frozen conditional"):
            evaluator._round_data("local-first", systems, 6)

    def test_cohort_membership_is_checked_even_if_board_hashes_are_recomputed(self):
        evaluator = self.evaluator()
        saved, _ = evaluator._round_data("tamper", list(self.controller.state["champions"].values()), 2)
        manifest_path = self.directory / saved["dataset"]
        manifest = json.loads(manifest_path.read_text())
        boards_path = manifest_path.parent / manifest["boards"]
        rows = [json.loads(line) for line in boards_path.read_text().splitlines()]
        board = parse_board(rows[0]["board"])
        weak = min((seat for seat in Seat if seat != Seat.NORTH), key=lambda seat: hand_hcp(board.deal.hand(seat)))
        hands = rows[0]["board"]["hands"]
        hands["N"], hands[weak.value] = hands[weak.value], hands["N"]
        rows[0]["family_id"] = family_hash(parse_board(rows[0]["board"]).deal)
        boards_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        manifest["boards_sha256"] = hashlib.sha256(boards_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "frozen ranges"):
            evaluator._round_data("tamper", list(self.controller.state["champions"].values()), 2)

    def test_bad_conditions_and_global_promotion_or_goal_claims_are_rejected(self):
        for change in ({"allow_promotion": True}, {"automatic_goal_check": True}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "Conditional development"):
                normalize_config({**self.config, **change}, self.project)
        for change in ({"seed": 4}, {"hand_conditions": {"N": {"op": "state"}}},
                       {"constraints": {}, "hand_conditions": {}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_sampling({**self.sampling, **change})
        with self.assertRaisesRegex(ValueError, "uniform evaluation"):
            run(self.directory, goal_only=True)
        self.assertEqual(self.controller.state["goal_checks"], 0)

    def test_exhausted_sampling_keeps_partial_evidence_and_never_relaxes_conditions(self):
        spec = normalize_sampling({"name": "impossible-test", "constraints": {"N": {"hcp": [40, 40]}}, "attempt_limit": 2})
        description = {**descriptor(spec), "draw": {}}
        target = self.project / "failed-draw"
        with self.assertRaisesRegex(ValueError, "未放宽"):
            generate_dataset(target, count=1, seed=731, split="validation", sampling=description,
                             source=conditional_source(spec, 1, 731, lambda: None, description["draw"]))
        self.assertTrue((target / "boards.jsonl").is_file())
        self.assertFalse((target / "manifest.json").exists())

    def test_no_model_queue_uses_only_conditional_rounds_and_retains_paired_statistics(self):
        release = self.publish()
        with patch("bridge_evolve.evolution.research.ModelClient", side_effect=AssertionError("No model allowed")):
            result = run(self.directory, evaluation_only=True)
            job = result["evaluation_queue"][release["evaluation"]["id"]]
            self.assertEqual(result["status"], "completed", result.get("error"))
            self.assertEqual(result["agent_requests"], 0)
            self.assertEqual(result["promotion_attempts"], 0)
            self.assertEqual(job["outcome"]["evaluation_scope"], "conditional")
            self.assertAlmostEqual(job["outcome"]["statistics"]["mean_imp"], job["outcome"]["full_delta"])
            self.assertEqual(job["outcome"]["statistics"]["boards"], 2)
            self.assertEqual(len(job["outcome"]["statistics"]["ci95"]), 2)
            for saved in result["rounds"].values():
                self.assertEqual(saved["evaluation_scope"], "conditional")
                metadata, records = load_dataset(self.directory / saved["dataset"])
                self.assertEqual(metadata["sampling"]["definition"], self.controller.config["development_sampling"])
                self.assertEqual(len(records), 2)
            used = result["evaluated_pairs"]
            again = run(self.directory, evaluation_only=True)
            self.assertEqual(again["evaluated_pairs"], used)
        view = overview(self.directory, details=True)
        self.assertEqual(view["development_sampling"]["name"], self.sampling["name"])
        self.assertTrue(all(row["evaluation_scope"] == "conditional" for row in view["rounds"]))

    def test_queue_identity_includes_the_frozen_sampling_distribution(self):
        release = self.publish()
        old = release["evaluation"]["id"]
        self.controller.config["development_sampling"] = copy.deepcopy(self.controller.config["development_sampling"])
        self.controller.config["development_sampling"]["constraints"]["N"]["hcp"] = [16, 17]
        new = self.controller.evaluations.enqueue(release["release"], lineage="Z", generation=2)
        self.assertNotEqual(old, new["id"])
        self.assertEqual(len(self.controller.evaluations.jobs), 2)


if __name__ == "__main__":
    unittest.main()
