import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_evolve.evolution.config import blank_system, normalize_config
from bridge_evolve.evolution.controller import Controller, initialize, register_system, run
from bridge_evolve.evolution.execution import Evaluator
from bridge_evolve.evolution.goals import ForcingPassGoal
from bridge_evolve.evolution.inspection import overview
from bridge_evolve.evolution.research_tools import ResearchTools
from bridge_evolve.systems import validate_system
from test_research import BOARD


def op(name, *args):
    return {"op": name, "args": list(args)}


def context(name):
    return {"op": "context", "name": name}


def forcing_fixture():
    raw = blank_system().to_data()
    raw["name"] = "Forcing Pass validity fixture, not an experiment seed"
    raw["protocol"]["states"] = {
        "idle": {"otherwise": "idle", "transitions": [{
            "id": "opening_pass", "when": op("and", op("eq", context("event.by"), "ours"),
                op("eq", context("event.call"), "P"), op("not", {"op": "has_contract_bid"})),
            "to": "reply", "set": {}, "forcing": "one_round"}]},
        "reply": {"otherwise": "reply", "transitions": [{
            "id": "response", "when": op("and", op("eq", context("event.by"), "ours"),
                op("ne", context("event.call"), "P")), "to": "finished", "set": {}}]},
        "finished": {"otherwise": "finished", "transitions": []},
    }
    raw["rules"] = [
        {"id": "required_double", "priority": 20,
         "when": op("and", context("must_bid"), {"op": "is_legal", "call": "X"}),
         "action": {"kind": "call", "call": "X"}},
        {"id": "required_bid", "priority": 10, "when": context("must_bid"),
         "action": {"kind": "cheapest", "strains": ["C", "D", "H", "S", "NT"], "min_level": 1, "max_level": 7}},
    ]
    return raw


class ForcingPassEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        baselines = []
        for name in ("fixture-anchor-A", "fixture-anchor-B"):
            raw = blank_system().to_data()
            raw["name"] = name
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(raw))
            baselines.append(str(path))
        self.config = {"profile": "smoke", "source": "research", "origins": ["blank"], "baselines": baselines,
                       "protocol_goal": "forcing-pass", "selection_policy": "parent-improvement",
                       "allow_promotion": True, "automatic_goal_check": False, "workers": 1, "generations": 2,
                       "boards": {"diagnosis": 2, "validity": 2, "screen": 2, "full": 2, "promotion": 2},
                       "growth_boards": 2, "growth_seconds": 30, "max_seconds": 300,
                       "max_pairs": 1000, "evaluation_seconds": 60, "resamples": 100}
        self.directory = initialize(self.config, self.project / "run", self.project)
        self.controller = Controller(self.directory)
        self.parent = self.controller.state["champions"]["Z"]

    def tools(self, raw):
        tools = ResearchTools(self.controller, "g001-Z", self.parent, {"selected_groups": []}, raw, None)
        case = tools.execute("case", "add_cases", {"cases": [{"name": "natural", "board": BOARD}]})["cases"][0]["case_id"]
        tools.execute("tests", "write_tests", {"tests": [{
            "name": "natural-change", "case_id": case, "mode": "auction", "changed_from_parent": True,
            "assertions": [{"path": "/auction/0/call", "op": "eq", "value": "P"}],
        }]})
        return tools

    def test_seed_is_exactly_all_pass_but_not_a_goal_compliant_release(self):
        seed = self.controller.system(self.parent)
        self.assertEqual(len(seed.rules), 0)
        self.assertEqual(str(seed.fallback.call), "P")
        self.assertEqual(ForcingPassGoal().check(seed)["status"], "failed")
        self.assertFalse(self.controller.state["rounds"])
        self.assertEqual(self.controller.config["selection_policy"], "parent-improvement")

    def test_published_goal_parent_can_continue_with_rebased_tests_and_remains_in_growth_comparisons(self):
        tools = self.tools(forcing_fixture())
        released = tools.execute("published-goal-parent", "publish_candidate", {"hypothesis": "Goal-valid test parent."})
        self.assertTrue(released["ok"], released)
        identity = released["hash"]
        self.controller.state["champions"]["Z"] = identity
        self.controller.state["generations"]["1"] = {"status": "completed", "parents": {"Z": self.parent},
            "candidates": {"Z-0": {"hash": identity, "release": released["release"], "research_session": "g001-Z"}},
            "promotions": {}}
        self.controller.checkpoint()
        original = (self.directory / "state.json").read_bytes()
        directory = initialize({**self.config, "champion_seed_run": str(self.directory)},
                               self.project / "continued-goal-parent", self.project)
        controller = Controller(directory)
        self.assertEqual(controller.state["champions"]["Z"], identity)
        self.assertEqual(controller.development_versions(), [self.parent, identity])
        self.assertEqual(controller.state["research_seeds"]["Z"]["goal_validation"]["status"], "passed")
        from bridge_evolve.evolution.proposals import evidence_packet
        from bridge_evolve.evolution.research import ResearchSession
        session = ResearchSession(controller, 1, "Z", identity, evidence_packet(controller.system(identity), [], ["*"]))
        self.assertNotIn("changed_from_parent", session.tools.data["tests"][0])
        result = session.tools.test_results()
        self.assertEqual((result["passed"], result["failed"], result["change_witnesses"]), (1, 0, 0))
        self.assertEqual(result["protocol_witnesses"], 0)
        self.assertEqual((self.directory / "state.json").read_bytes(), original)
        self.assertEqual(controller.state["agent_requests"], 0)

    def test_an_incomplete_all_pass_parent_cannot_masquerade_as_a_published_goal_continuation(self):
        with self.assertRaisesRegex(ValueError, "does not satisfy the protocol goal"):
            initialize({**self.config, "champion_seed_run": str(self.directory)},
                       self.project / "invalid-goal-parent", self.project)
        self.assertEqual(self.controller.state["agent_requests"], 0)

    def test_public_obligation_covers_all_dealers_positions_and_legal_interference(self):
        result = ForcingPassGoal().check(validate_system(forcing_fixture()))
        self.assertEqual((result["status"], result["probes"], result["passed"]), ("passed", 288, 288))
        fake = blank_system().to_data()
        fake["announcements"] = [{"id": "label_only", "when": True, "tags": ["forcing_pass"]}]
        self.assertEqual(ForcingPassGoal().check(validate_system(fake))["status"], "failed")
        escape = forcing_fixture()
        escape["protocol"]["states"]["reply"]["transitions"].append({
            "id": "allow_pass_escape", "when": op("and", op("eq", context("event.by"), "ours"),
                op("eq", context("event.call"), "P")), "to": "finished", "set": {}, "allow_pass": True})
        self.assertEqual(ForcingPassGoal().check(validate_system(escape))["status"], "failed")

    def test_natural_pass_and_response_witness_is_replayed_by_the_evaluation_queue(self):
        tools = self.tools(forcing_fixture())
        published = tools.execute("publish", "publish_candidate", {"hypothesis": "Validity fixture only."})
        self.assertTrue(published["ok"], published)
        self.assertEqual(published["tests"]["protocol_witnesses"], 1)
        with patch("bridge_evolve.evolution.research.ModelClient", side_effect=AssertionError("No model allowed")):
            result = run(self.directory, evaluation_only=True)
        self.assertEqual(result["status"], "completed", result.get("error"))
        job = result["evaluation_queue"][published["evaluation"]["id"]]
        self.assertEqual(job["tests"]["protocol_witnesses"], 1)
        self.assertEqual(job["protocol_goal"]["status"], "passed")
        self.assertGreater(job["outcome"]["protocol_coverage"]["natural_partner_response_families"], 0)
        self.assertEqual(result["agent_requests"], 0)
        self.assertEqual(overview(self.directory)["protocol_goal"], "forcing-pass")

    def test_dead_opening_pass_semantics_without_a_natural_witness_cannot_publish(self):
        raw = forcing_fixture()
        raw["rules"].insert(0, {"id": "never_open_pass", "priority": 50,
            "when": op("not", {"op": "has_contract_bid"}), "action": {"kind": "call", "call": "1C"}})
        tools = self.tools(raw)
        tools.data["tests"][0]["assertions"][0]["value"] = "1C"
        result = tools.execute("dead-pass-publish", "publish_candidate", {"hypothesis": "Must be rejected."})
        self.assertFalse(result["ok"])
        self.assertIn("opening-Pass/partner-response", result["error"])
        self.assertEqual(len(tools.data["releases"]), 0)

    def paired_result(self, candidate):
        anchors = [value["hash"] for value in self.controller.config["baselines"]]
        return {"id": "fresh-parent-comparison", "seed": 17, "boards": 2, "anchors": anchors,
                "status": {candidate: "checked", self.parent: "checked"}, "errors": {candidate: [], self.parent: []},
                "scores": {self.parent: {anchor: [-8, -2] for anchor in anchors},
                           candidate: {anchors[0]: [-7, 3], anchors[1]: [-9, -3]}}}

    def test_parent_only_upgrade_ignores_mature_strength_significance_and_single_anchor_guard(self):
        candidate = register_system(self.directory, self.controller.state, validate_system(forcing_fixture()))
        generation = {"promotions": {}, "candidates": {}}
        entry = {"lineage": "Z", "hash": candidate, "parent": self.parent}
        with patch.object(self.controller, "round", return_value=self.paired_result(candidate)):
            self.controller.confirm_candidate(1, generation, entry)
        promotion = next(iter(generation["promotions"].values()))
        self.assertEqual(promotion["status"], "promoted")
        self.assertFalse(promotion["global_confirmed"])
        self.assertLessEqual(promotion["statistics"]["lower"], 0)
        self.assertLess(min(promotion["statistics"]["opponent_deltas"]), 0)
        self.assertEqual(self.controller.state["champions"]["Z"], candidate)
        self.controller.state["champions"]["Z"] = self.parent
        self.controller.state["promotion_attempts"] = 0
        self.controller.config["selection_policy"] = "confirmed"
        control = {"promotions": {}, "candidates": {}}
        with patch.object(self.controller, "round", return_value=self.paired_result(candidate)):
            self.controller.confirm_candidate(2, control, {"lineage": "Z", "hash": candidate, "parent": self.parent})
        self.assertEqual(next(iter(control["promotions"].values()))["status"], "not_confirmed")

    def test_growth_replays_versions_on_common_fresh_boards_and_resume_does_not_repeat_pairs(self):
        candidate = register_system(self.directory, self.controller.state, validate_system(forcing_fixture()))
        self.controller.state["champions"]["Z"] = candidate
        self.controller.state["generations"]["1"] = {"status": "completed", "parents": {"Z": self.parent},
            "candidates": {}, "promotions": {"Z": {"status": "promoted", "candidate": candidate, "parent": self.parent}}}
        evaluator = Evaluator(self.directory, self.controller.config, self.controller.state,
                              self.controller.checkpoint, self.controller.stop)
        self.addCleanup(evaluator.close)
        self.controller.evaluator = evaluator
        before = self.controller.model_time_window()
        self.controller.reserve_growth(1)
        self.assertGreater(before - self.controller.model_time_window(), 29)
        self.controller.verify_growth(1)
        job = self.controller.state["growth_checks"]["1"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["versions"], [self.parent, candidate])
        self.assertEqual(len(job["curve"]), 2)
        self.assertIn("against_previous", job["curve"][1])
        self.assertEqual(self.controller.growth_seconds_held(), 0)
        used = self.controller.state["evaluated_pairs"]
        self.controller.verify_growth(1)
        self.assertEqual(self.controller.state["evaluated_pairs"], used)
        view = overview(self.directory)
        self.assertEqual(view["growth_checks"][0]["versions"], [self.parent, candidate])

    def test_development_goal_and_inherited_system_shortcuts_are_rejected(self):
        for change in ({"origins": ["existing"]}, {"automatic_goal_check": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_config({**self.config, **change}, self.project)
        with self.assertRaisesRegex(ValueError, "not global confirmation"):
            run(self.directory, goal_only=True)

    def test_equal_parent_performance_is_not_a_development_upgrade(self):
        candidate = register_system(self.directory, self.controller.state, validate_system(forcing_fixture()))
        result = self.paired_result(candidate)
        result["scores"][candidate] = {key: list(values) for key, values in result["scores"][self.parent].items()}
        generation = {"promotions": {}, "candidates": {}}
        with patch.object(self.controller, "round", return_value=result):
            self.controller.confirm_candidate(1, generation, {"lineage": "Z", "hash": candidate, "parent": self.parent})
        self.assertEqual(next(iter(generation["promotions"].values()))["status"], "not_confirmed")
        self.assertEqual(self.controller.state["champions"]["Z"], self.parent)

    def test_growth_keeps_negative_shared_block_differences_instead_of_forcing_a_rising_curve(self):
        candidate = register_system(self.directory, self.controller.state, validate_system(forcing_fixture()))
        self.controller.state["champions"]["Z"] = candidate
        self.controller.state["generations"]["1"] = {"status": "completed", "parents": {"Z": self.parent},
            "candidates": {}, "promotions": {"Z": {"status": "promoted", "candidate": candidate, "parent": self.parent}}}
        evaluator = Evaluator(self.directory, self.controller.config, self.controller.state,
                              self.controller.checkpoint, self.controller.stop)
        self.addCleanup(evaluator.close)
        self.controller.evaluator = evaluator
        self.controller.reserve_growth(1)
        result = self.paired_result(candidate)
        result["scores"][candidate] = {key: [-9, -3] for key in result["anchors"]}
        result["objective"] = {self.parent: -5, candidate: -6}
        result["means"] = {identity: {key: value for key in result["anchors"]}
                           for identity, value in result["objective"].items()}
        with patch.object(evaluator, "evaluate", return_value=result):
            self.controller.verify_growth(1)
        curve = self.controller.state["growth_checks"]["1"]["curve"]
        self.assertEqual(curve[1]["delta_to_seed"], -1)
        self.assertEqual(curve[1]["against_previous"]["mean"], -1)
        self.assertEqual(self.controller.state["growth_checks"]["1"]["status"], "completed")

    def test_growth_budget_shortage_retains_an_explicit_failure(self):
        self.controller.config["max_pairs"] = 1
        evaluator = Evaluator(self.directory, self.controller.config, self.controller.state,
                              self.controller.checkpoint, self.controller.stop)
        self.addCleanup(evaluator.close)
        self.controller.evaluator = evaluator
        from bridge_evolve.evolution.execution import EvolutionStopped
        with self.assertRaises(EvolutionStopped):
            self.controller.reserve_growth(1)
        job = self.controller.state["growth_checks"]["1"]
        self.assertEqual(job["status"], "budget_blocked")
        self.assertEqual(job["remaining_seconds"], 0)
        self.assertEqual(self.controller.state["evaluated_pairs"], 0)


if __name__ == "__main__":
    unittest.main()
