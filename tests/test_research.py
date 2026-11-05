import copy
import json
import tempfile
import unittest
import io
import http.client
import threading
from contextlib import redirect_stdout
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from bridge_evolve.cli import main
from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evolution.config import ROOT, DEFAULT_MODEL, blank_system, normalize_config
from bridge_evolve.evolution.controller import Controller, initialize, register_system, run, status
from bridge_evolve.evolution.execution import Evaluator, EvolutionStopped
from bridge_evolve.evolution.models import ModelClient
from bridge_evolve.evolution.inspection import overview, research_detail, frozen_system_export, timeline
from bridge_evolve.evolution.proposals import evidence_packet
from bridge_evolve.evolution.research import RESEARCH_PROMPT, ResearchSession
from bridge_evolve.evolution.research_tools import ResearchTools, edit_document
from bridge_evolve.systems import validate_system


BOARD = json.loads((ROOT / "examples/general-board.json").read_text())


def response(*calls, text=None, usage=None):
    output = [{"type": "function_call", "call_id": identity, "name": name, "arguments": json.dumps(arguments)}
              for identity, name, arguments in calls]
    if text is not None:
        output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    return {"model": DEFAULT_MODEL, "output": output, "usage": usage or {"input_tokens": 30, "output_tokens": 20}}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        paths = []
        for name in ("anchor-A", "anchor-B"):
            raw = blank_system().to_data()
            raw["name"] = name
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(raw))
            paths.append(str(path))
        self.config = {"profile": "smoke", "source": "research", "baselines": paths, "origins": ["blank"],
                       "generations": 1, "workers": 1, "boards": {"diagnosis": 2, "validity": 2, "screen": 2, "full": 2, "promotion": 2},
                       "max_agent_requests": 20, "max_agent_tokens": 2000000, "max_output_tokens": 4096,
                       "max_seconds": 300, "research_seconds": 240, "max_pairs": 1000, "resamples": 100}
        self.directory = initialize(self.config, self.project / "experiment", self.project)
        self.controller = Controller(self.directory)
        self.controller.state["test_only"] = True
        self.parent = self.controller.state["champions"]["Z"]
        self.packet = evidence_packet(self.controller.system(self.parent), [], ["*"])
        self.client = Mock()
        self.tools = ResearchTools(self.controller, "g001-Z", self.parent, self.packet, blank_system().to_data(), self.client)

    def case(self):
        result = self.tools.execute("case", "add_cases", {"cases": [{"name": "regression", "board": BOARD}]})
        self.assertTrue(result["ok"], result)
        return result["cases"][0]["case_id"]

    def opening_system(self, call="1C"):
        raw = blank_system().to_data()
        raw["name"] = "original-test-protocol"
        raw["features"] = {"enough": {"op": "ge", "args": [{"op": "hcp"}, 16]}}
        raw["rules"] = [{"id": "original.open", "priority": 900, "when": {"op": "and", "args": [
            {"op": "not", "args": [{"op": "has_contract_bid"}]}, {"op": "ref", "name": "enough"},
            {"op": "is_legal", "call": call}]}, "action": {"kind": "call", "call": call}}]
        return raw

    def authored_tests(self, case_id, call="1C"):
        return [{"name": "opening", "case_id": case_id, "mode": "decision", "prefix": [],
                 "assertions": [{"path": "/call", "op": "eq", "value": call}], "changed_from_parent": True}]

    def test_astra_and_origins_are_explicit_and_old_baselines_remain_frozen(self):
        config = normalize_config({**self.config, "origins": ["existing", "blank"]}, self.project)
        self.assertEqual(config["model"], "gpt-6-astra")
        self.assertEqual(config["plateau_limit"], 0)
        self.assertEqual(config["origins"], ["existing", "blank"])
        self.assertEqual(status(self.directory)["rounds"], {})
        self.assertEqual(len(status(self.directory)["system_files"]), 6)
        self.assertIn("ALL bidding meanings", RESEARCH_PROMPT)
        self.assertNotIn("Return JSON only", RESEARCH_PROMPT)
        for update in ({"max_research_turns": True}, {"max_tool_calls": 0}, {"origins": ["blank", "blank"]},
                       {"max_agent_tokens": 999}, {"reasoning_effort": "automatic-cheap-fallback"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                normalize_config({**self.config, **update}, self.project)

    def test_cli_model_override_is_frozen_without_silent_substitution(self):
        config_path = self.project / "config.json"
        config_path.write_text(json.dumps({**self.config, "model": "explicit-old-choice"}))
        output = self.project / "cli-experiment"
        with redirect_stdout(io.StringIO()):
            code = main(["evolve", "init", "--config", str(config_path), "--output", str(output), "--model", DEFAULT_MODEL])
        self.assertEqual(code, 0)
        frozen = json.loads((output / "config.json").read_text())
        self.assertEqual(frozen["model"], DEFAULT_MODEL)
        self.assertEqual(frozen["source"], "research")

    def test_full_document_edits_are_atomic_and_allow_structure_and_more_than_eight_changes(self):
        raw = blank_system().to_data()
        edits = [{"op": "add", "path": f"/features/f{index}", "value": index} for index in range(20)]
        changed = edit_document(raw, edits)
        self.assertEqual(len(changed["features"]), 20)
        self.assertEqual(raw["features"], {})
        changed = edit_document(changed, [{"op": "add", "path": "/features/a~1b", "value": 7},
                                         {"op": "test", "path": "/features/a~1b", "value": 7},
                                         {"op": "remove", "path": "/features/f0"}])
        self.assertEqual(changed["features"]["a/b"], 7)
        self.assertNotIn("f0", changed["features"])
        with self.assertRaises(ValueError):
            edit_document(raw, [{"op": "add", "path": "/features/new", "value": True},
                                {"op": "remove", "path": "/features/missing"}])
        self.assertEqual(raw["features"], {})
        full = json.loads((ROOT / "systems/general-v2.json").read_text())
        result = self.tools.execute("rewrite", "edit_system", {"edits": [{"op": "replace", "path": "", "value": full}]})
        self.assertTrue(result["ok"], result)
        self.assertGreater(len(self.tools.system().states), 2)

    def test_navigation_reads_dependencies_compares_parent_and_tracks_edits(self):
        raw = self.opening_system()
        self.tools.execute("rewrite-navigation", "edit_system", {"edits": [{"op": "replace", "path": "", "value": raw}]})
        found = self.tools.execute("find-opening", "navigate_system", {
            "request": {"view": "search", "kind": "rule", "call": "1C"}})
        self.assertTrue(found["ok"], found)
        self.assertEqual(found["matches"]["items"][0]["key"], "rule:original.open")
        read = self.tools.execute("read-opening", "navigate_system", {
            "request": {"view": "read", "keys": ["rule:original.open"]}})
        self.assertTrue(read["dependencies"]["complete_in_response"])
        self.assertIn("feature:enough", {item["key"] for item in read["definitions"]["items"]})
        diff = self.tools.execute("diff-opening", "navigate_system", {
            "request": {"view": "diff", "kind": "rule"}, "compare_to": "parent"})
        self.assertEqual(diff["changes"]["items"][0]["change"], "added")
        self.tools.execute("change-opening", "edit_system", {
            "edits": [{"op": "replace", "path": "/rules/0/action/call", "value": "1D"}]})
        changed = self.tools.execute("find-new-opening", "navigate_system", {
            "request": {"view": "search", "kind": "rule", "call": "1D"}})
        self.assertNotEqual(found["system_hash"], changed["system_hash"])
        self.assertEqual(changed["matches"]["total"], 1)
        self.assertFalse(self.tools.execute("bad-navigation", "navigate_system", {
            "request": {"view": "catalog"}, "snapshot": "../../host"})["ok"])

    def test_research_intro_is_scoped_and_evidence_has_direct_pointers(self):
        session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet)
        intro = json.loads(session.data["messages"][1]["content"])
        self.assertEqual(intro["navigation"]["inventory"]["tests"], 0)
        self.assertEqual(intro["navigation"]["start"]["view"], "catalog")
        self.assertIn("not a scan of the whole IR", RESEARCH_PROMPT)
        self.tools.packet["selected_groups"] = [{"id": "sample", "rule_id": "original.open",
            "occurrences": 1, "imp_sum": -1, "examples": [{"one": 1}]}]
        result = self.tools.execute("direct-evidence", "read_evidence", {
            "view": "packet", "pointer": "/selected_groups/0/examples/0"})
        self.assertEqual(result["value"], {"one": 1})

    def test_intermediate_invalid_rules_are_acknowledged_but_non_general_releases_are_rejected(self):
        edited = self.tools.execute("temporary-null", "edit_system", {"edits": [{"op": "replace", "path": "/rules", "value": None}]})
        self.assertTrue(edited["ok"], edited)
        self.assertIsNone(edited["rules"])
        self.assertFalse(self.tools.execute("invalid-null", "validate_system", {})["ok"])
        local = json.loads((ROOT / "systems/1nt-v1.json").read_text())
        self.tools.execute("local-system", "edit_system", {"edits": [{"op": "replace", "path": "", "value": local}]})
        self.assertFalse(self.tools.execute("not-general-v2", "validate_system", {})["ok"])

    def test_actual_failures_return_to_researcher_and_fixed_tests_gate_publication(self):
        case_id = self.case()
        self.tools.execute("rewrite", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.opening_system()}]})
        self.tools.execute("tests-wrong", "write_tests", {"tests": self.authored_tests(case_id, "1S")})
        failed = self.tools.execute("run-wrong", "run_tests", {})
        self.assertEqual(failed["failed"], 1)
        rejected = self.tools.execute("publish-wrong", "publish_candidate", {"hypothesis": "test-only intentional failing release"})
        self.assertFalse(rejected["ok"])
        self.assertEqual(self.tools.data["releases"], [])
        self.tools.execute("repair", "edit_system", {"edits": [{"op": "replace", "path": "/rules/0/action/call", "value": "1S"},
            {"op": "replace", "path": "/rules/0/when/args/2/call", "value": "1S"}]})
        passed = self.tools.execute("run-fixed", "run_tests", {})
        self.assertEqual((passed["passed"], passed["failed"], passed["change_witnesses"]), (1, 0, 1))
        published = self.tools.execute("publish-fixed", "publish_candidate", {"hypothesis": "test-only coherent original opening"})
        self.assertTrue(published["ok"], published)
        frozen = self.controller.system(published["hash"]).to_data()
        self.tools.execute("later-edit", "edit_system", {"edits": [{"op": "replace", "path": "/rules/0/priority", "value": 901}]})
        self.assertEqual(self.controller.system(published["hash"]).to_data(), frozen)
        release = self.controller.read_artifact(self.directory / published["release"])
        self.assertEqual(release["tests"]["passed"], 1)
        self.assertEqual(release["test_definitions"], self.authored_tests(case_id, "1S"))
        self.assertEqual(release["cases"][case_id]["board"], BOARD)

    def test_tool_replay_cannot_apply_an_array_insertion_twice(self):
        arguments = {"edits": [{"op": "add", "path": "/rules/-", "value": self.opening_system()["rules"][0]}]}
        first = self.tools.execute("stable-id", "edit_system", arguments)
        restored = ResearchTools(self.controller, "g001-Z", self.parent, self.packet, {}, self.client)
        repeated = restored.execute("stable-id", "edit_system", arguments)
        self.assertEqual(first, repeated)
        self.assertEqual(len(restored.data["system"]["rules"]), 1)
        with self.assertRaisesRegex(ValueError, "different arguments"):
            restored.execute("stable-id", "edit_system", {"edits": [{"op": "remove", "path": "/rules/0"}]})

    def test_workspace_reads_are_frozen_snapshots_without_self_referential_tool_results(self):
        root = self.tools.execute("root", "read_workspace", {"pointer": "", "limit": 100})
        self.assertTrue(root["ok"])
        self.assertEqual(root["operations"], 1)
        listing = self.tools.execute("operations", "read_workspace", {"pointer": "/operations", "limit": 100})
        self.assertTrue(listing["ok"])
        old_notes = self.tools.execute("read-notes", "read_workspace", {"pointer": "/notes"})
        self.assertEqual(old_notes["value"], [])
        self.tools.execute("append-note", "research_note", {"text": "读取历史不能反过来引用或修改实时工作区。"})
        serialized = json.dumps(self.tools.data, ensure_ascii=False)
        self.assertTrue(serialized)
        self.assertEqual(self.tools.data["operations"]["read-notes"]["result"]["value"], [])
        restored = ResearchTools(self.controller, "g001-Z", self.parent, self.packet, {}, self.client)
        self.assertEqual(restored.execute("root", "read_workspace", {"pointer": "", "limit": 100}), root)
        self.assertEqual(len(restored.data["notes"]), 1)

    def test_persistence_failures_are_not_misreported_as_agent_argument_errors(self):
        with patch("bridge_evolve.evolution.research.ModelClient") as factory:
            factory.return_value.request.return_value = response(("note", "research_note", {"text": "fixture"}))
            session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet)
            with patch.object(session.tools, "execute", side_effect=ValueError("fixture persistence integrity failure")):
                with self.assertRaisesRegex(ValueError, "persistence integrity"):
                    session.run()

    def test_constrained_cases_and_hand_predicates_are_real_complete_deals(self):
        self.tools.data["system"]["features"] = {"unused_public_parameter": {"op": "var", "name": "not_declared_here"}}
        self.tools.save()
        generated = self.tools.execute("generate", "generate_cases", {
            "sampling": {"count": 2, "seed": 98123, "dealer": "N", "constraints": {"N": {"hcp": [15, 20]}}},
            "seat": "N", "hand_condition": {"op": "ge", "args": [{"op": "length", "suit": "S"}, 5]}})
        self.assertTrue(generated["ok"], generated)
        from bridge_evolve.systems.runtime import hand_hcp
        from bridge_evolve.systems.inputs import parse_board
        for case in generated["cases"]:
            board = parse_board(case["board"])
            hand = board.deal.hand(next(seat for seat in type(board.dealer) if seat.value == "N"))
            self.assertTrue(15 <= hand_hcp(hand) <= 20)
            self.assertGreaterEqual(sum(card.suit == "S" for card in hand.cards), 5)
        self.assertEqual(self.tools.data["lab_boards"], 2)

    def test_unfinished_rounds_host_paths_and_hidden_information_are_not_authorized(self):
        self.controller.state["rounds"]["unseen"] = {"status": "running", "boards": 10000}
        self.assertFalse(self.tools.execute("unseen", "read_evidence", {"view": "records", "round_id": "unseen"})["ok"])
        self.assertFalse(self.tools.execute("host", "read_reference", {"topic": "../../credentials"})["ok"])
        self.assertFalse(self.tools.execute("shell", "execute_shell", {"command": "echo not-authorized"})["ok"])
        hidden = self.opening_system()
        hidden["rules"][0]["when"] = {"op": "partner_hand"}
        self.tools.execute("bad-policy", "edit_system", {"edits": [{"op": "replace", "path": "", "value": hidden}]})
        self.assertFalse(self.tools.execute("validate-hidden", "validate_system", {})["ok"])
        self.assertEqual(self.tools.data["releases"], [])

    def test_advisory_reviewer_uses_the_same_pinned_client_and_has_no_tools(self):
        self.client.request.return_value = response(text="检查问叫和回答的边界，当前没有实际测试证明。")
        result = self.tools.execute("review", "consult", {"question": "检查此协议的退出是否完整。", "pointers": ["/fallback"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "gpt-6-astra")
        self.assertEqual(self.client.request.call_args.args[1], [])
        self.assertIn("review", self.client.request.call_args.kwargs["label"])
        self.assertEqual(self.tools.data["notes"][-1]["kind"], "review")

    def test_strict_real_dds_trial_reports_action_changes_without_promoting(self):
        self.tools.execute("open", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.opening_system("1NT")}]})
        evaluator = Evaluator(self.directory, self.controller.config, self.controller.state, self.controller.checkpoint, self.controller.stop)
        self.addCleanup(evaluator.close)
        self.controller.evaluator = evaluator
        outcome = self.tools.execute("real-trial", "trial", {"boards": 2})
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["status"], "checked")
        self.assertEqual(self.controller.state["oracle"]["oracle_kind"], "dds")
        self.assertEqual(self.controller.state["promotion_attempts"], 0)
        before = self.controller.state["evaluated_pairs"]
        repeated = self.tools.execute("real-trial", "trial", {"boards": 2})
        self.assertEqual(outcome, repeated)
        self.assertEqual(self.controller.state["evaluated_pairs"], before)

    def test_persistent_session_replays_saved_work_and_resumes_a_real_repair_loop(self):
        case_id = canonical_case = canonical_hash({"call": "case", "index": 0})[:16]
        wrong = self.opening_system("1C")
        scripted = [
            response(("case", "add_cases", {"cases": [{"name": "regression", "board": BOARD}]})),
            response(("rewrite", "edit_system", {"edits": [{"op": "replace", "path": "", "value": wrong}]}),
                     ("tests", "write_tests", {"tests": self.authored_tests(case_id, "1S")}),
                     ("failures", "run_tests", {})),
            EvolutionStopped("unit-test interruption after a real failing test"),
        ]
        with patch("bridge_evolve.evolution.research.ModelClient") as factory:
            factory.return_value.request.side_effect = scripted
            session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet)
            with self.assertRaises(EvolutionStopped):
                session.run()
        restored = self.controller.read_artifact(self.directory / "research/g001-Z/workspace.json")
        self.assertEqual(restored["last_tests"]["failed"], 1)
        self.assertEqual(list(restored["cases"]), [canonical_case])
        repair = response(("repair", "edit_system", {"edits": [
            {"op": "replace", "path": "/rules/0/action/call", "value": "1S"},
            {"op": "replace", "path": "/rules/0/when/args/2/call", "value": "1S"}]}),
            ("pass", "run_tests", {}))
        with patch("bridge_evolve.evolution.research.ModelClient") as factory:
            factory.return_value.request.side_effect = [
                repair, response(("release", "publish_candidate", {"hypothesis": "真实修复后提交整个体系的测试夹具"})),
                response(("done", "finish_research", {"summary": "已实际运行失败测试、修复并复测通过；未宣称晋级。"})),
            ]
            result = ResearchSession(self.controller, 1, "Z", self.parent, self.packet).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["releases"]), 1)
        self.assertEqual(result["releases"][0]["tests"]["failed"], 0)
        self.assertEqual(result["releases"][0]["tests"]["change_witnesses"], 1)
        self.assertEqual(self.controller.state["research_sessions"]["g001-Z"]["turns"], 5)
        self.assertNotIn("execute_shell", [tool["name"] for tool in factory.return_value.request.call_args.args[1]])
        detailed = research_detail(self.directory, "g001-Z", offset=0, limit=3)
        self.assertEqual(len(detailed["events"]), 3)
        self.assertEqual(detailed["model"], DEFAULT_MODEL)
        self.assertIn("ALL bidding meanings", detailed["prompt"])
        self.assertEqual(detailed["tests"]["failed"], 0)
        tool = research_detail(self.directory, "g001-Z", tool_call="repair")
        self.assertEqual(tool["arguments"]["edits"][0]["path"], "/rules/0/action/call")
        self.assertTrue(tool["result"]["ok"])
        exported = json.loads(frozen_system_export(self.directory, result["releases"][0]["hash"], "json"))
        self.assertEqual(exported["rules"][0]["action"]["call"], "1S")
        detail = overview(self.directory, details=True)
        self.assertEqual(detail["model"], DEFAULT_MODEL)
        self.assertEqual(detail["budgets"]["max_agent_requests"], self.config["max_agent_requests"])
        self.assertEqual(len(detail["research_sessions"]), 1)

    def test_publishing_requires_a_real_behavior_change_witness_not_a_renaming(self):
        case_id = self.case()
        self.tools.execute("rename", "edit_system", {"edits": [{"op": "replace", "path": "/name", "value": "renamed-only"}]})
        self.assertFalse(self.tools.execute("publish-name", "publish_candidate", {"hypothesis": "only a name"})["ok"])
        self.tools.execute("open", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.opening_system()}]})
        tests = self.authored_tests(case_id)
        tests[0].pop("changed_from_parent")
        self.tools.execute("tests", "write_tests", {"tests": tests})
        self.assertFalse(self.tools.execute("publish-no-witness", "publish_candidate", {"hypothesis": "missing witness"})["ok"])
        self.tools.execute("witness", "write_tests", {"tests": self.authored_tests(case_id)})
        self.assertTrue(self.tools.execute("publish-with-witness", "publish_candidate", {"hypothesis": "actual action change"})["ok"])

    def test_tool_and_release_budgets_are_real_not_rule_complexity_penalties(self):
        self.controller.config["max_tool_calls"] = 1
        self.assertTrue(self.tools.execute("one", "research_note", {"text": "有限工具预算测试"})["ok"])
        with self.assertRaisesRegex(ValueError, "tool-call budget"):
            self.tools.execute("two", "read_system", {})
        self.assertEqual(len(self.tools.data["operations"]), 1)

    def test_unfinished_research_branch_survives_into_the_next_generation(self):
        self.controller.config["max_research_turns"] = 1
        edited = self.opening_system("1S")
        with patch("bridge_evolve.evolution.research.ModelClient") as factory:
            factory.return_value.request.return_value = response(
                ("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": edited}]}))
            first = ResearchSession(self.controller, 1, "Z", self.parent, self.packet).run()
        self.assertEqual(first["status"], "needs_more_research")
        self.assertEqual(first["releases"], [])
        second = ResearchSession(self.controller, 2, "Z", self.parent, self.packet)
        self.assertEqual(second.tools.data["system"], edited)
        self.assertEqual(second.data["continued_from"], "research/g001-Z/workspace.json")
        self.assertEqual(second.data["turns"], 0)
        self.assertTrue(self.controller.state["test_only"])

    def test_explicit_new_experiment_can_continue_frozen_work_without_copying_scores_or_old_budget(self):
        edited = self.opening_system("1S")
        self.controller.state["agent_requests"] = 7
        self.controller.checkpoint()
        with patch("bridge_evolve.evolution.research.ModelClient") as factory:
            factory.return_value.request.return_value = response(
                ("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": edited}]}))
            self.controller.config["max_research_turns"] = 1
            ResearchSession(self.controller, 1, "Z", self.parent, self.packet).run()
        old_usage = status(self.directory)["agent_requests"]
        self.assertEqual(old_usage, 7)
        new_directory = initialize({**self.config, "research_seed_run": str(self.directory)},
                                   self.project / "continued", self.project)
        next_controller = Controller(new_directory)
        self.assertEqual(next_controller.state["rounds"], {})
        self.assertEqual(next_controller.state["agent_requests"], 0)
        self.assertEqual(next_controller.state["promotion_attempts"], 0)
        continued = ResearchSession(next_controller, 1, "Z", self.parent, self.packet)
        self.assertEqual(continued.tools.data["system"], edited)
        self.assertEqual(continued.data["continued_from"]["run"], self.directory.name)
        self.assertEqual(status(self.directory)["agent_requests"], old_usage)
        seed = next_controller.state["research_seeds"]["Z"]
        self.assertEqual(seed["source_model"], DEFAULT_MODEL)
        (new_directory / seed["path"]).write_text("{}")
        with self.assertRaisesRegex(ValueError, "input identity"):
            Controller(new_directory)

    def test_published_champion_warm_start_uses_the_winner_and_rebases_regression_witnesses(self):
        case_id = self.case()
        self.tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.opening_system("1S")}]})
        self.tools.execute("tests", "write_tests", {"tests": self.authored_tests(case_id, "1S")})
        published = self.tools.execute("publish", "publish_candidate", {"hypothesis": "test-only published champion"})
        self.assertTrue(published["ok"], published)
        champion = published["hash"]
        self.controller.state["champions"]["Z"] = champion
        self.controller.state["agent_requests"] = 7
        self.controller.state["generations"]["1"] = {
            "status": "completed", "candidates": {"Z-0": {"hash": champion, "release": published["release"],
                                                         "research_session": "g001-Z"}},
            "promotions": {"Z": {"status": "promoted", "candidate": champion, "parent": self.parent}},
        }
        self.controller.checkpoint()
        original_bytes = (self.directory / "state.json").read_bytes()
        new_directory = initialize({**self.config, "origins": ["existing", "blank"], "champion_seed_run": str(self.directory)},
                                   self.project / "warm-start", self.project)
        state = status(new_directory)
        self.assertEqual(next(iter(state["champions"])), "Z")
        self.assertEqual(state["champions"]["Z"], champion)
        self.assertEqual(state["initial_champions"]["Z"], champion)
        self.assertEqual(state["origin_hashes"]["Z"], self.parent)
        self.assertEqual(state["rounds"], {})
        self.assertEqual((state["agent_requests"], state["promotion_attempts"]), (0, 0))
        self.assertEqual((self.directory / "state.json").read_bytes(), original_bytes)
        next_controller = Controller(new_directory)
        packet = evidence_packet(next_controller.system(champion), [], ["*"])
        session = ResearchSession(next_controller, 1, "Z", champion, packet)
        self.assertEqual(session.tools.system().content_hash, champion)
        self.assertEqual(session.tools.data["tests"][0]["assertions"][0]["value"], "1S")
        self.assertNotIn("changed_from_parent", session.tools.data["tests"][0])
        self.assertEqual(session.tools.test_results()["passed"], 1)
        self.assertEqual(session.tools.test_results()["change_witnesses"], 0)
        summary = overview(new_directory, details=True)
        current = next(row for row in summary["current_systems"] if row["lineage"] == "Z")
        self.assertEqual(current["hash"], champion)
        self.assertEqual(current["rules"], 1)
        self.assertEqual(current["source"]["kind"], "champion")
        self.assertNotIn("latest_result", current)
        with self.assertRaisesRegex(ValueError, "either"):
            normalize_config({**self.config, "champion_seed_run": str(self.directory), "research_seed_run": str(self.directory)}, self.project)
        (self.directory / "systems" / f"{champion}.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "frozen identity"):
            initialize({**self.config, "champion_seed_run": str(self.directory)}, self.project / "tampered-warm-start", self.project)

    def test_explicit_repaired_benchmarks_preserve_paid_work_without_copying_old_scores(self):
        prototype = validate_system(self.opening_system("1C"))
        prototype_hash = register_system(self.directory, self.controller.state, prototype)
        self.controller.state["champions"]["Z"] = prototype_hash
        self.controller.state["agent_requests"] = 7
        self.controller.state["status"] = "failed"
        self.controller.state["comparison_issues"] = {"old": {
            "round": "old-diagnosis", "boards": 2, "error_count": 1,
            "errors": [{"message": "test-only old reference defect"}]}}
        source_anchors = [row["hash"] for row in self.controller.config["baselines"]]
        self.controller.state["champions"].update(A=source_anchors[0], B=source_anchors[1])
        for lineage, parent in (("Z", prototype_hash), ("A", source_anchors[0])):
            session = ResearchSession(self.controller, 2, lineage, parent,
                                      evidence_packet(self.controller.system(parent), [], ["*"]))
            generated = session.tools.execute("saved-case", "add_cases", {"cases": [{"name": "saved", "board": BOARD}]})
            case_id = generated["cases"][0]["case_id"]
            session.tools.execute("saved-edit", "edit_system", {
                "edits": [{"op": "replace", "path": "", "value": self.opening_system("1S")}]})
            session.tools.execute("saved-tests", "write_tests", {"tests": self.authored_tests(case_id, "1S")})
            session.tools.execute("run-saved", "run_tests", {})
        self.controller.checkpoint()
        source_state = (self.directory / "state.json").read_bytes()
        repaired = blank_system().to_data()
        repaired["name"] = "repaired-anchor-A-fixture"
        path = self.project / "repaired-A.json"
        path.write_text(json.dumps(repaired))
        config = {**self.config, "origins": ["existing", "blank"],
                  "baselines": [str(path), self.config["baselines"][1]], "research_seed_run": str(self.directory)}
        with self.assertRaisesRegex(ValueError, "benchmark_revision_reason"):
            initialize(config, self.project / "implicit-reference-change", self.project)
        continued = initialize({**config, "benchmark_revision_reason": "test-only explicit corrected public contract"},
                               self.project / "explicit-reference-change", self.project)
        controller = Controller(continued)
        state = controller.state
        self.assertEqual(state["champions"]["Z"], prototype_hash)
        self.assertNotEqual(state["champions"]["A"], source_anchors[0])
        self.assertEqual(state["rounds"], {})
        self.assertEqual(state["agent_requests"], 0)
        self.assertFalse(state.get("comparison_issues"))
        self.assertTrue(state["benchmark_revision"]["historical_comparison_issues"])
        self.assertEqual((self.directory / "state.json").read_bytes(), source_state)
        for lineage in ("Z", "A"):
            parent = state["champions"][lineage]
            restored = ResearchSession(controller, 1, lineage, parent,
                                       evidence_packet(controller.system(parent), [], ["*"]))
            self.assertEqual(restored.tools.system().to_data()["rules"][0]["action"]["call"], "1S")
            self.assertEqual(restored.tools.test_results()["passed"], 1)
            self.assertEqual(len(restored.tools.data["cases"]), 1)
            if lineage == "A":
                self.assertNotIn("changed_from_parent", restored.tools.data["tests"][0])
                self.assertEqual(state["research_seeds"]["A"]["source_parent"], source_anchors[0])
            else:
                self.assertTrue(restored.tools.data["tests"][0]["changed_from_parent"])

    def test_imported_initial_seed_does_not_replace_a_later_promoted_system(self):
        case_id = self.case()
        old = self.opening_system("1C")
        old_system = validate_system(old)
        old_hash = register_system(self.directory, self.controller.state, old_system)
        seed_path = self.directory / "seed.json"
        seed_path.write_text(json.dumps({"workspace": {"system": old, "tests": [], "cases": {}, "notes": []}}))
        self.controller.state["research_seeds"] = {"Z": {
            "parent": old_hash, "path": "seed.json", "source_run": "source-fixture", "source_session": "g001-Z"}}
        new = validate_system(self.opening_system("1S"))
        new_hash = register_system(self.directory, self.controller.state, new)
        release_path = self.directory / "research/new-release.json"
        self.controller.artifact(release_path, {"hash": new_hash, "hypothesis": "test-only next champion",
            "test_definitions": self.authored_tests(case_id, "1S"), "cases": {case_id: self.tools.data["cases"][case_id]}})
        self.controller.state["generations"]["1"] = {"candidates": {
            "Z-0": {"hash": new_hash, "release": "research/new-release.json"}}}
        session = ResearchSession(self.controller, 2, "Z", new_hash, evidence_packet(new, [], ["*"]))
        self.assertEqual(session.tools.system().content_hash, new_hash)
        self.assertEqual(session.tools.test_results()["passed"], 1)
        self.assertNotIn("changed_from_parent", session.tools.data["tests"][0])
        self.assertEqual(session.data["continued_from"]["champion"], new_hash)

    def test_live_milestones_are_durable_idempotent_and_track_stale_draft_tests(self):
        self.tools.execute("note", "research_note", {"text": "本轮先验证整套协议的实际行为变化。"})
        self.tools.execute("rewrite", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.opening_system()}]})
        case_id = self.case()
        self.tools.execute("tests", "write_tests", {"tests": self.authored_tests(case_id)})
        self.tools.execute("run", "run_tests", {})
        self.assertTrue(self.tools.summary()["tests_current"])
        self.tools.execute("change", "edit_system", {"edits": [{"op": "replace", "path": "/rules/0/priority", "value": 901}]})
        self.assertFalse(self.tools.summary()["tests_current"])
        first = timeline(self.directory, offset=0, limit=2)
        self.assertEqual([row["kind"] for row in first["events"]], ["hypothesis", "design_change"])
        before = first["total"]
        self.tools.execute("note", "research_note", {"text": "本轮先验证整套协议的实际行为变化。"})
        self.assertEqual(timeline(self.directory)["total"], before)
        rows = list(status(self.directory)["milestones"].values())
        self.assertTrue(all(row["timestamp"] for row in rows))
        test = next(row for row in rows if row["kind"] == "tests")
        self.assertEqual(test["tests"]["passed"], 1)
        self.assertEqual(test["tests"]["change_witnesses"], 1)
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            timeline(self.directory, offset=-1)

    def test_live_model_request_is_visible_while_awaiting_the_gateway(self):
        client = ModelClient(self.controller.config, self.controller.state, self.controller.checkpoint, self.controller.stop)
        def network(request, **_):
            current = overview(self.directory, details=True)
            self.assertEqual(current["activity"]["kind"], "model")
            self.assertEqual(current["active_request"]["request"], 1)
            self.assertEqual(current["active_request"]["status"], "reserved")
            self.assertTrue(current["active_request"]["started_at"])
            return ModelBudgetTests.Response(response(text="status test"))
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = network
            client.request([], [], label="g001-Z:turn:1")
        self.assertEqual(self.controller.state["model_requests"][0]["status"], "received")
        self.assertTrue(self.controller.state["model_requests"][0]["finished_at"])
        self.assertNotIn("active_request", overview(self.directory))

    def test_fixed_opponent_failure_allows_research_but_never_manufactures_fitness_or_promotion(self):
        broken = blank_system().to_data()
        broken["name"] = "intentional-fixed-opponent-failure-fixture"
        broken["fallback"] = {"kind": "error", "message": "test-only fixed opponent error"}
        path = self.project / "broken-anchor.json"
        path.write_text(json.dumps(broken))
        directory = initialize({**self.config, "baselines": [self.config["baselines"][0], str(path)]},
                               self.project / "blocked-research", self.project)
        with patch.object(Controller, "proposals", return_value={"source": "research", "proposals": [], "analysis": []}) as researcher:
            result = run(directory)
        self.assertEqual(researcher.call_count, 1)
        packet = researcher.call_args.args[-1]
        self.assertEqual(packet["diagnosis_status"], "blocked")
        self.assertTrue(packet["diagnosis_errors"])
        self.assertIn("block formal ranking", packet["comparison_warning"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["generation"], 1)
        self.assertEqual(result["promotion_attempts"], 0)
        self.assertEqual(result["champions"]["Z"], self.parent)
        self.assertTrue(result["comparison_issues"])
        summary = json.loads((directory / "rounds/g001-diagnosis/summary.json").read_text())
        self.assertNotIn(self.parent, summary["objective"])
        self.assertTrue(any(value is None for column in summary["scores"][self.parent].values() for value in column))
        controller = Controller(directory)
        controller.verify_goal(force=True)
        self.assertEqual(controller.state["goal"]["status"], "blocked")
        self.assertEqual(controller.state["goal_checks"], 0)
        self.assertNotIn("goal-10000", controller.state["rounds"])
        continued = initialize({**self.config, "baselines": [self.config["baselines"][0], str(path)],
                               "champion_seed_run": str(directory)}, self.project / "blocked-continuation", self.project)
        carried = status(continued)
        self.assertTrue(carried["comparison_issues"])
        self.assertEqual(carried["rounds"], {})
        self.assertEqual(carried["agent_requests"], 0)
        self.assertEqual(status(directory)["evaluated_pairs"], result["evaluated_pairs"])

    def test_research_controller_evaluates_tied_alternatives_and_does_not_call_them_failures(self):
        self.controller.config["origins"] = ["blank"]
        releases = []
        for number in range(2):
            raw = blank_system().to_data()
            raw["rules"] = [{"id": f"test.unreachable.{number}", "priority": 1, "when": False,
                             "action": {"kind": "call", "call": "1C"}}]
            system = validate_system(raw)
            identity = register_system(self.directory, self.controller.state, system)
            path = self.directory / "research" / f"test-release-{number}.json"
            self.controller.artifact(path, {"parent": self.parent, "hash": identity, "tests": {"tests": 1, "passed": 1, "failed": 0}})
            releases.append({"parent_hash": self.parent, "snapshot_hash": identity, "release": str(path.relative_to(self.directory)),
                             "hypothesis": "test-only tied no-action-change candidate"})
        self.controller.checkpoint()
        with patch.object(Controller, "proposals", return_value={"source": "research", "research_session": "test-only",
                                                                 "proposals": releases, "analysis": []}):
            result = run(self.directory)
        self.assertEqual(result["status"], "completed", result.get("error"))
        entries = list(result["generations"]["1"]["candidates"].values())
        self.assertEqual([entry["status"] for entry in entries], ["insufficient_coverage", "insufficient_coverage"])
        self.assertEqual(len(result["rounds"]["g001-full"]["systems"]), 3)
        self.assertTrue(all(entry["full_delta"] == 0 for entry in entries))


class ModelBudgetTests(unittest.TestCase):
    class Response:
        def __init__(self, value):
            self.value = value
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self, _):
            return json.dumps(self.value).encode()

    def setUp(self):
        self.state = {"agent_requests": 0}
        self.config = {"model": DEFAULT_MODEL, "project": str(ROOT), "max_agent_requests": 1,
                       "max_agent_tokens": 100000, "max_output_tokens": 4096, "max_context_bytes": 100000,
                       "reasoning_effort": "high", "agent_timeout": 30}
        self.checkpoint = Mock()

    def test_request_is_charged_before_network_and_actual_tokens_settle_the_reservation(self):
        def open_request(request, **_):
            self.assertEqual(self.state["agent_requests"], 1)
            self.assertGreater(self.state["model_usage"]["charged_tokens"], 4096)
            payload = json.loads(request.data)
            self.assertEqual(payload["model"], "gpt-6-astra")
            self.assertFalse(payload["store"])
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["reasoning"]["effort"], "high")
            return self.Response(response(text="完成", usage={"input_tokens": 30, "output_tokens": 20}))
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = open_request
            client = ModelClient(self.config, self.state, self.checkpoint, lambda: None)
            client.request([{"role": "user", "content": "test"}], [], label="test")
            with self.assertRaisesRegex(EvolutionStopped, "request budget"):
                client.request([], [], label="denied")
        self.assertEqual(self.state["model_usage"]["charged_tokens"], 50)
        self.assertEqual(self.state["model_usage"]["unreported_requests"], 0)
        self.assertNotIn("unit-only-secret", json.dumps(self.state))

    def test_provider_failure_keeps_budget_spent_and_never_switches_models(self):
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("http://local", 503, "unavailable", {}, None)
            client = ModelClient(self.config, self.state, self.checkpoint, lambda: None)
            with self.assertRaisesRegex(RuntimeError, "no fallback"):
                client.request([], [], label="failure")
            restored = ModelClient(self.config, copy.deepcopy(self.state), self.checkpoint, lambda: None)
            with self.assertRaises(EvolutionStopped):
                restored.request([], [], label="resume")
        self.assertEqual(self.state["agent_requests"], 1)
        self.assertGreater(self.state["model_usage"]["charged_tokens"], 4096)
        self.assertEqual(self.state["model_usage"]["unreported_requests"], 1)
        self.assertEqual(self.state["model_requests"][0]["model"], "gpt-6-astra")

    def test_legacy_preallocated_request_cannot_be_charged_twice_or_reused(self):
        self.state["agent_requests"] = 1
        client = ModelClient(self.config, self.state, self.checkpoint, lambda: None)
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.return_value = self.Response(response(text="legacy reply"))
            client.request([], [], label="legacy", reserved_request=1)
            self.assertEqual(self.state["agent_requests"], 1)
            with self.assertRaisesRegex(ValueError, "already-used"):
                client.request([], [], label="duplicate", reserved_request=1)
        self.assertEqual(self.state["model_usage"]["charged_tokens"], 50)

    def test_token_reservation_denies_network_and_missing_usage_is_not_silently_free(self):
        config = {**self.config, "max_agent_tokens": 1000}
        with patch("bridge_evolve.evolution.models.build_opener") as opener:
            with self.assertRaisesRegex(EvolutionStopped, "token budget"):
                ModelClient(config, self.state, self.checkpoint, lambda: None).request([], [], label="denied")
            opener.assert_not_called()
        self.assertEqual(self.state["agent_requests"], 0)
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.return_value = self.Response({"output": [], "model": DEFAULT_MODEL})
            ModelClient(self.config, self.state, self.checkpoint, lambda: None).request([], [], label="unreported")
        self.assertGreater(self.state["model_usage"]["charged_tokens"], 4096)
        self.assertTrue(self.state["model_requests"][0]["usage_unreported"])

    def test_gateway_cannot_silently_substitute_a_cheaper_model(self):
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            value = {**response(text="wrong model"), "model": "gpt-5.3-codex"}
            opener.return_value.open.return_value = self.Response(value)
            with self.assertRaisesRegex(RuntimeError, "different model"):
                ModelClient(self.config, self.state, self.checkpoint, lambda: None).request([], [], label="model-mismatch")
        self.assertEqual(self.state["model_requests"][0]["status"], "model_mismatch")
        self.assertEqual(self.state["model_usage"]["charged_tokens"], 50)

    def test_transient_503_retries_the_same_model_with_every_attempt_and_unknown_usage_charged(self):
        config = {**self.config, "max_agent_requests": 3, "model_retries": 2, "model_retry_delay": 0}
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = [
                HTTPError("http://local", 503, "temporary", {}, None),
                self.Response(response(text="recovered"))]
            result = ModelClient(config, self.state, self.checkpoint, lambda: None).request([], [], label="retry-test")
        self.assertEqual(result["model"], DEFAULT_MODEL)
        self.assertEqual(self.state["agent_requests"], 2)
        self.assertEqual([row["status"] for row in self.state["model_requests"]], ["error", "received"])
        first = self.state["model_requests"][0]
        self.assertEqual(self.state["model_usage"]["charged_tokens"], first["reserved_tokens"] + 50)
        self.assertEqual(self.state["model_usage"]["unreported_requests"], 1)
        self.assertEqual(self.state["activity"]["kind"], "research")
        self.assertEqual(len(self.state["milestones"]), 1)
        self.assertTrue(all(row["model"] == DEFAULT_MODEL for row in self.state["model_requests"]))

    def test_gateway_retries_are_finite_and_nontransient_errors_do_not_retry(self):
        config = {**self.config, "max_agent_requests": 3, "model_retries": 2, "model_retry_delay": 0}
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = [HTTPError("http://local", 503, "temporary", {}, None) for _ in range(3)]
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                ModelClient(config, self.state, self.checkpoint, lambda: None).request([], [], label="finite")
        self.assertEqual(self.state["agent_requests"], 3)
        self.assertEqual(len(self.state["milestones"]), 2)
        self.assertEqual(self.state["model_usage"]["unreported_requests"], 3)
        state = {"agent_requests": 0}
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("http://local", 400, "bad request", {}, None)
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                ModelClient(config, state, self.checkpoint, lambda: None).request([], [], label="do-not-retry")
        self.assertEqual(state["agent_requests"], 1)


class ResearchHTTPTests(unittest.TestCase):
    def test_model_budget_research_tool_and_frozen_system_endpoints(self):
        from test_board_viewer import viewer
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "systems").mkdir()
            paths = [project / "systems" / name for name in ("general-v2.json", "pass-v2.json")]
            for path in paths:
                path.write_bytes((ROOT / "systems" / path.name).read_bytes())
            directory = initialize({"source": "research", "profile": "smoke", "origins": ["blank"],
                "baselines": [str(path) for path in paths]}, project=project)
            controller = Controller(directory)
            controller.state["test_only"] = True
            parent = controller.state["champions"]["Z"]
            packet = evidence_packet(controller.system(parent), [], ["*"])
            with patch("bridge_evolve.evolution.research.ModelClient") as factory:
                factory.return_value.request.side_effect = [
                    response(("note", "research_note", {"text": "已保存实际研究记录；此处为HTTP测试夹具，不是模型成绩。"})),
                    response(("done", "finish_research", {"summary": "HTTP接口验证，不宣称改进。"}))]
                ResearchSession(controller, 1, "Z", parent, packet).run()
            state = viewer.ViewerState(project)
            self.addCleanup(state.executor.shutdown)
            server = viewer.ViewerServer(("127.0.0.1", 0), state)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()

            def request(path):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
                connection.request("GET", path)
                response = connection.getresponse()
                code, body = response.status, response.read()
                connection.close()
                return code, body

            try:
                with patch("bridge_evolve.evolution.models.available_models", return_value=[{"id": DEFAULT_MODEL, "name": "GPT-6 Astra"}]):
                    code, body = request("/api/evolution-models")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["default"], DEFAULT_MODEL)
                prefix = "/api/evolutions/" + directory.name
                code, body = request(prefix + "/research/g001-Z?offset=0&limit=1")
                self.assertEqual(code, 200, body)
                detail = json.loads(body)
                self.assertEqual(len(detail["events"]), 1)
                self.assertEqual(detail["model"], DEFAULT_MODEL)
                self.assertEqual(detail["notes"][0]["kind"], "research")
                code, body = request(prefix + "/timeline?offset=0&limit=2")
                self.assertEqual(code, 200, body)
                events = json.loads(body)
                self.assertEqual(len(events["events"]), 2)
                self.assertEqual(events["events"][0]["kind"], "research_started")
                self.assertEqual(events["events"][1]["kind"], "hypothesis")
                code, _ = request(prefix + "/timeline?offset=-1")
                self.assertEqual(code, 422)
                code, body = request(prefix + "/research/g001-Z?tool_call=note")
                self.assertEqual(code, 200)
                self.assertIn("HTTP", json.loads(body)["arguments"]["text"])
                code, body = request(prefix + f"/systems/{parent}.json")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["rules"], [])
                future = Future()
                a, b = state.systems
                with patch.object(state.executor, "submit", return_value=future):
                    created = state.start_evolution({"source": "numbers", "system_a": a, "system_b": b,
                        "profile": "smoke", "model": DEFAULT_MODEL, "max_agent_requests": 7,
                        "max_agent_tokens": 500000, "max_research_turns": 9})
                self.assertEqual(created["model"], DEFAULT_MODEL)
                self.assertEqual(created["budgets"]["max_agent_requests"], 7)
                self.assertEqual(created["budgets"]["max_agent_tokens"], 500000)
                self.assertEqual(created["budgets"]["max_research_turns"], 9)
                future.cancel()
                state.organize_library({"ids": [directory.name], "action": "trash", "confirm": True})
                code, _ = request(prefix + "/research/g001-Z")
                self.assertEqual(code, 422)
                code, _ = request(prefix + "/timeline")
                self.assertEqual(code, 422)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
