import json
import tempfile
import unittest
from pathlib import Path

from bridge_evolve.evolution.config import blank_system
from bridge_evolve.evolution.controller import Controller, initialize
from bridge_evolve.evolution.inspection import overview, readable_report
from bridge_evolve.evolution.presentation import changes_between
from bridge_evolve.evolution.research_tools import ResearchTools
from bridge_evolve.systems import validate_system
from test_research import BOARD


class ResearchPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        baselines = []
        for name in ("A", "B"):
            raw = blank_system().to_data()
            raw["name"] = name
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(raw))
            baselines.append(str(path))
        self.directory = initialize({"profile": "smoke", "source": "research", "origins": ["blank"],
            "baselines": baselines, "max_seconds": 300, "max_pairs": 1000, "evaluation_seconds": 30},
            self.project / "run", self.project)
        self.controller = Controller(self.directory)
        self.parent = self.controller.state["champions"]["Z"]

    def publish(self, call="1C", name="first"):
        raw = blank_system().to_data()
        raw["name"] = "Human-readable version " + name
        raw["rules"] = [{"id": "opening", "priority": 1, "when": {"op": "not", "args": [{"op": "has_contract_bid"}]},
                         "action": {"kind": "call", "call": call}}]
        tools = ResearchTools(self.controller, "g001-Z", self.parent, {"selected_groups": []}, raw, None)
        tools.data["system"] = raw
        case = tools.execute("case-" + name, "add_cases", {"cases": [{"name": "natural", "board": BOARD}]})["cases"][0]["case_id"]
        tools.execute("tests-" + name, "write_tests", {"tests": [{"name": "opening", "case_id": case,
            "mode": "decision", "changed_from_parent": True,
            "assertions": [{"path": "/call", "op": "eq", "value": call}]}]})
        result = tools.execute("publish-" + name, "publish_candidate", {"hypothesis": "Author's unverified +100 IMP claim."})
        self.assertTrue(result["ok"], result)
        return result

    def test_report_separates_current_parent_latest_candidate_author_intent_and_actual_conclusions(self):
        first = self.publish()
        second = self.publish("1D", "second")
        s = self.controller.state
        s["champions"]["Z"] = first["hash"]
        s["generations"]["1"] = {"parents": {"Z": self.parent}, "status": "completed", "candidates": {},
            "promotions": {"Z": {"status": "promoted", "candidate": first["hash"], "parent": self.parent,
                "global_confirmed": False, "statistics": {"mean": .25, "boards": 512}}}}
        job = s["evaluation_queue"][second["evaluation"]["id"]]
        job.update(status="comparison_blocked", outcome={"status": "comparison_blocked", "error_count": 1,
            "errors": [{"board": 4, "message": "Unimplemented protocol branch: fixture"}]})
        self.controller.checkpoint()
        original = (self.directory / "state.json").read_bytes()
        view = overview(self.directory, details=True)
        report = view["research_summary"]
        self.assertEqual(report["current"], ["V1"])
        self.assertEqual(report["latest"], "V2")
        self.assertEqual(report["blocked_count"], 1)
        self.assertIn("不是全局确认", report["versions"][1]["decision"])
        self.assertIn("未判负", report["versions"][2]["decision"])
        self.assertEqual(report["versions"][1]["evidence"]["gain"], .25)
        self.assertNotEqual(report["versions"][1]["evidence"]["gain"], 100)
        self.assertEqual(report["versions"][1]["author_intent"], "Author's unverified +100 IMP claim.")
        self.assertTrue(report["versions"][1]["opening"])
        change = report["versions"][1]["changes"]["items"][0]
        self.assertEqual(change["before"], "原来没有此分支")
        self.assertIn("1C", change["after"])
        markdown = readable_report(self.directory).decode()
        for title in ("结论先读", "体系是什么", "作者设计意图", "相对亲本修改了什么", "实际证据与裁决"):
            self.assertIn(title, markdown)
        self.assertEqual((self.directory / "state.json").read_bytes(), original)

    def test_resolved_before_after_changes_do_not_report_array_reordering_as_redesign(self):
        first = blank_system().to_data()
        first["rules"] = [{"id": "first", "priority": 1, "when": True, "action": {"kind": "call", "call": "1C"}},
                          {"id": "second", "priority": 2, "when": False, "action": {"kind": "call", "call": "1D"}}]
        before = validate_system(first)
        first["rules"].reverse()
        self.assertEqual(changes_between(validate_system(first), before)["total"], 0)
        first["rules"][0]["action"]["call"] = "1H"
        result = changes_between(validate_system(first), before)
        self.assertEqual(result["total"], 1)
        self.assertIn("1D", result["items"][0]["before"])
        self.assertIn("1H", result["items"][0]["after"])

    def test_old_frozen_runs_without_new_policy_fields_are_labelled_strict_not_rescored(self):
        self.controller.config.pop("baseline_fault_policy")
        from bridge_evolve.evolution.presentation import research_summary
        view = research_summary(self.directory, self.controller.state, self.controller.config)
        self.assertEqual(view["baseline_fault_policy"], "strict")
        self.assertIn("不会篡改", view["baseline_fault_explanation"])
        self.assertEqual(view["versions"][0]["rules"], 0)
        self.assertEqual(view["versions"][0]["fallback"], "P")


if __name__ == "__main__":
    unittest.main()
