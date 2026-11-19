import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import test_research
from bridge_evolve.evaluation.data import board_data, canonical_hash, load_dataset
from bridge_evolve.evolution.config import blank_system
from bridge_evolve.evolution.controller import Controller, initialize, register_system
from bridge_evolve.evolution.costs import budget_usage, resource_summary
from bridge_evolve.evolution.director import EVIDENCE, PLAN, REVIEW, DirectedResearch, director_evidence, structured_request
from bridge_evolve.evolution.execution import Evaluator, EvolutionStopped
from bridge_evolve.evolution.models import ModelClient, request_body
from bridge_evolve.evolution.proposals import evidence_packet
from bridge_evolve.evolution.research import ResearchSession
from bridge_evolve.evolution.research_tools import ResearchTools, merge_documents, validate_arguments
from bridge_evolve.evolution.sampling import check_cohort
from bridge_evolve.systems import validate_system
from test_directed_research import IDEA
from test_research import BOARD, response


class ExpandedResearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        paths = []
        for name in ("expanded-A", "expanded-B"):
            raw = blank_system().to_data()
            raw["name"] = name
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(raw))
            paths.append(str(path))
        self.config = {
            "profile": "smoke", "source": "research", "research_architecture": "director",
            "research_budget_mode": "campaign", "origins": ["blank"], "baselines": paths,
            "hypotheses_per_unit": 2, "repair_attempts": 10, "generations": 1, "workers": 1,
            "boards": {"diagnosis": 2, "validity": 2, "screen": 2, "full": 2, "promotion": 2},
            "max_agent_requests": 60, "max_agent_tokens": 5000000, "max_output_tokens": 16384,
            "max_research_turns": 40, "max_tool_calls": 100, "max_context_bytes": 800000,
            "research_seconds": 200, "evaluation_seconds": 30, "max_seconds": 300,
            "max_pairs": 1000, "max_lab_boards": 10000, "max_trial_boards": 256,
            "resamples": 100, "allow_promotion": True, "selection_policy": "parent-improvement",
            "automatic_goal_check": False, "model_retries": 0,
            "maintenance_budget": {"max_requests": 20, "max_tokens": 2000000, "max_seconds": 120},
        }
        self.directory = initialize(self.config, self.project / "run", self.project)
        self.controller = Controller(self.directory)
        self.parent = self.controller.state["champions"]["Z"]
        self.packet = evidence_packet(self.controller.system(self.parent), [], ["*"])

    def system(self, call="1C"):
        raw = blank_system().to_data()
        raw["rules"] = [{"id": "test.open", "priority": 1,
                         "when": {"op": "not", "args": [{"op": "has_contract_bid"}]},
                         "action": {"kind": "call", "call": call}}]
        return raw

    def evaluator(self):
        evaluator = Evaluator(self.directory, self.controller.config, self.controller.state,
                              self.controller.checkpoint, self.controller.stop)
        self.addCleanup(evaluator.close)
        self.controller.evaluator = evaluator
        return evaluator

    def test_incomplete_plan_is_preserved_and_corrections_spend_only_maintenance(self):
        self.controller.config["max_agent_requests"] = 1
        partial = response(("plan", PLAN["name"], {}))
        partial.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        partial["output"][0]["arguments"] = '{"summary":"unfinished'
        good = response(("plan", PLAN["name"], {"summary": "机制研究", "ideas": [IDEA]}))
        for item in (partial, good):
            item["copilot_usage"] = {"total_nano_aiu": 1000000000}
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = [test_research.ModelBudgetTests.Response(partial), test_research.ModelBudgetTests.Response(good),
                                                    test_research.ModelBudgetTests.Response(response(text="normal research"))]
            path = self.directory / "director" / "plan.json"
            path.parent.mkdir()
            result = structured_request(self.controller, path, "Plan.", {}, PLAN, "g001-Z:director:plan")
            self.assertEqual(result["summary"], "机制研究")
            self.assertEqual(self.controller.read_artifact(path), partial)
            self.assertEqual(structured_request(self.controller, path, "Plan.", {}, PLAN, "g001-Z:director:plan"), result)
            self.assertEqual(opener.return_value.open.call_count, 2)
            self.assertEqual(budget_usage(self.controller.state, "research")["requests"], 0)
            self.assertEqual(budget_usage(self.controller.state, "maintenance")["requests"], 2)
            client = ModelClient(self.controller.config, self.controller.state, self.controller.checkpoint, self.controller.stop)
            client.request([], [], label="normal")
            self.assertEqual(budget_usage(self.controller.state, "research")["requests"], 1)
            self.assertEqual(self.controller.state["agent_requests"], 3)
            self.assertEqual(resource_summary(self.controller.state)["models"]["known_ai_credits"], 2)

    def test_transport_failure_is_not_free_or_charged_to_idea_budget(self):
        self.controller.config["max_agent_requests"] = 1
        client = ModelClient(self.controller.config, self.controller.state, self.controller.checkpoint, self.controller.stop)
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = [
                HTTPError("http://local", 500, "gateway fault", {}, None),
                test_research.ModelBudgetTests.Response(response(text="research continues")),
            ]
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                client.request([], [], label="transport")
            client.request([], [], label="normal")
            with self.assertRaises(EvolutionStopped):
                client.request([], [], label="research exhausted")
        self.assertEqual(budget_usage(self.controller.state, "research")["requests"], 1)
        maintenance = budget_usage(self.controller.state, "maintenance")
        self.assertEqual(maintenance["requests"], 1)
        self.assertGreater(maintenance["charged_tokens"], 4096)
        self.assertEqual(self.controller.state["agent_requests"], 2)
        self.assertEqual(self.controller.state["model_usage"]["unreported_requests"], 1)

    def test_interface_failure_and_its_correction_do_not_consume_research_turns(self):
        values = [
            response(("invalid", "navigate_system", {"request": {"view": "search", "keys": ["fallback"]}})),
            response(("finish", "finish_research", {"summary": "接口恢复已完成，未发布或捏造成绩。"})),
        ]
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = [test_research.ModelBudgetTests.Response(item) for item in values]
            session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet,
                                      assignment={**IDEA, "job_id": "g001-Z-h01", "turn_budget": 40},
                                      identity="g001-Z-h01")
            result = session.run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(session.data["turns"], 2)
        self.assertEqual(self.controller.state["research_sessions"]["g001-Z-h01"]["research_turns"], 0)
        self.assertEqual(budget_usage(self.controller.state, "maintenance")["requests"], 2)
        self.assertEqual(budget_usage(self.controller.state, "research")["requests"], 0)
        self.assertEqual(len(self.controller.state["maintenance_events"]), 1)

    def test_director_can_query_evidence_without_edit_privileges_or_18kb_truncation(self):
        def request(messages, tools, **kwargs):
            self.assertGreater(len(messages[1]["content"]), 18000)
            names = [item["name"] for item in tools]
            self.assertNotIn("edit_system", names)
            self.assertIn(EVIDENCE["name"], names)
            if len(messages) == 2:
                return response(("query", EVIDENCE["name"], {"view": "portfolio"}))
            self.assertIn("continuable_branches", messages[-1]["output"])
            return response(("plan", PLAN["name"], {"summary": "充分查证后提出机制", "ideas": [IDEA]}))

        with patch("bridge_evolve.evolution.director.ModelClient") as client:
            client.return_value.request.side_effect = request
            path = self.directory / "plan.json"
            result = structured_request(self.controller, path, "Plan.", {"material": "x" * 40000},
                                        PLAN, "g001-Z:director:plan")
            self.assertEqual(len(result["ideas"]), 1)
            self.assertEqual(client.return_value.request.call_count, 2)
            self.assertEqual(structured_request(self.controller, path, "Plan.", {}, PLAN, "g001-Z:director:plan"), result)
            self.assertEqual(client.return_value.request.call_count, 2)

    def test_director_protocol_evidence_reads_saved_drafts_without_freezing_or_changing_cached_summaries(self):
        identity = "g001-Z-h01"
        session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet,
                                  assignment={**IDEA, "job_id": identity, "turn_budget": 40}, identity=identity)
        raw = self.system()
        state = raw["protocol"]["initial"]
        raw["protocol"]["states"][state]["transitions"] = [{"id": "unused", "when": False, "to": state, "set": {}}]
        session.tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": raw}]})
        document = canonical_hash(raw)
        draft = session.tools.system().content_hash
        self.assertNotEqual(document, draft)
        frozen_files = copy.deepcopy(self.controller.state["system_files"])
        workspace_bytes = session.tools.path.read_bytes()
        result = director_evidence(self.controller, {
            "view": "protocol", "session": identity, "candidate": document, "query": "missing-branch"})
        self.assertEqual(result["hash"], draft)
        self.assertEqual(result["evidence_source"], {
            "kind": "draft", "session": identity, "hash": draft, "document_hash": document, "system_hash": draft})
        self.assertEqual(result["branches"], [])
        legacy = director_evidence(self.controller, {"view": "protocol", "session": identity, "candidate": draft})
        self.assertEqual(legacy["evidence_source"], result["evidence_source"])
        with self.assertRaisesRegex(ValueError, "registered frozen candidate"):
            director_evidence(self.controller, {"view": "protocol", "candidate": document})
        complete = director_evidence(self.controller, {"view": "protocol", "session": identity})
        self.assertGreater(len(complete["branches"]), 0)
        self.assertIn("not validation", complete["evidence_scope"])
        self.assertEqual(self.controller.state["research_sessions"][identity]["working"]["document_hash"], document)
        self.assertEqual(session.tools.path.read_bytes(), workspace_bytes)
        self.assertEqual(self.controller.state["system_files"], frozen_files)
        self.assertEqual(self.controller.state["agent_requests"], 0)
        self.assertEqual(session.tools.data["releases"], [])

        self.controller.state["research_branches"] = {
            "imported-h01": {"workspace": self.controller.state["research_sessions"][identity]["workspace"]}}
        imported = director_evidence(self.controller, {"view": "protocol", "session": "imported-h01"})
        self.assertEqual(imported["hash"], draft)
        self.assertEqual(imported["evidence_source"]["session"], "imported-h01")
        queries = director_evidence(self.controller, {"view": "portfolio"})["protocol_queries"]
        for selected in (identity, "imported-h01"):
            query = queries[selected]
            self.assertNotIn("candidate", query)
            validate_arguments(query, EVIDENCE["parameters"])
            self.assertEqual(director_evidence(self.controller, query)["evidence_source"]["session"], selected)
        frozen = director_evidence(self.controller, {"view": "protocol", "candidate": self.parent})
        self.assertEqual(frozen["hash"], self.parent)
        self.assertEqual(frozen["evidence_source"], {"kind": "frozen", "hash": self.parent})

    def test_workspace_document_query_does_not_spend_maintenance_or_require_a_retry(self):
        identity = "g001-Z-h01"
        session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet,
                                  assignment={**IDEA, "job_id": identity, "turn_budget": 40}, identity=identity)
        raw = self.system()
        state = raw["protocol"]["initial"]
        raw["protocol"]["states"][state]["transitions"] = [{"id": "unused", "when": False, "to": state, "set": {}}]
        session.tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": raw}]})
        document = canonical_hash(raw)
        self.assertNotEqual(document, session.tools.system().content_hash)
        expected = {"summary": "使用实际草稿完成查证", "ideas": [IDEA]}
        values = [
            response(("draft", EVIDENCE["name"], {"view": "protocol", "session": identity, "candidate": document})),
            response(("plan", PLAN["name"], expected)),
        ]
        context = DirectedResearch(self.controller, 1, "Z", self.parent, self.packet).context()
        self.assertEqual(context["protocol_queries"][identity], {"view": "protocol", "session": identity})
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = [test_research.ModelBudgetTests.Response(item) for item in values]
            path = self.directory / "document-query-plan.json"
            result = structured_request(self.controller, path, "Plan.", context, PLAN, "g001-Z:director:plan")
            self.assertEqual(result, expected)
            self.assertEqual(structured_request(self.controller, path, "Plan.", context, PLAN, "g001-Z:director:plan"), result)
            self.assertEqual(opener.return_value.open.call_count, 2)
        self.assertEqual(budget_usage(self.controller.state, "research")["requests"], 2)
        self.assertEqual(budget_usage(self.controller.state, "maintenance")["requests"], 0)
        self.assertFalse(self.controller.state.get("maintenance_events"))
        conversation = self.controller.read_artifact(path.with_suffix(".conversation.json"))
        output = json.loads(conversation["messages"][-1]["output"])
        self.assertEqual(output["evidence_source"]["document_hash"], document)
        self.assertEqual(session.tools.data["releases"], [])

    def test_director_protocol_still_rejects_stale_versions_and_malformed_candidate_parameters(self):
        identity = "g001-Z-h01"
        session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet,
                                  assignment={**IDEA, "job_id": identity, "turn_budget": 40}, identity=identity)
        raw = self.system()
        state = raw["protocol"]["initial"]
        raw["protocol"]["states"][state]["transitions"] = [{"id": "unused", "when": False, "to": state, "set": {}}]
        session.tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": raw}]})
        old_versions = (canonical_hash(raw), session.tools.system().content_hash, self.parent)
        self.assertNotEqual(old_versions[0], old_versions[1])
        session.tools.execute("change", "edit_system", {
            "edits": [{"op": "replace", "path": "/rules/0/action/call", "value": "1D"}]})
        workspace_bytes = session.tools.path.read_bytes()
        for candidate in old_versions:
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, "No other version was substituted"):
                director_evidence(self.controller, {"view": "protocol", "session": identity, "candidate": candidate})
        self.assertEqual(session.tools.path.read_bytes(), workspace_bytes)
        self.assertEqual(session.tools.data["releases"], [])
        for candidate in ("\u306b", "auto", "\u200d", " ", "a" * 66):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_arguments({"view": "protocol", "session": identity, "candidate": candidate}, EVIDENCE["parameters"])

    def test_human_draft_review_pins_unpublished_snapshots_and_compares_the_real_parent_without_scoring(self):
        from test_board_viewer import viewer
        directory = initialize(self.config, self.project / ".runtime/evolution" / ("evo-" + "a" * 32), self.project)
        controller = Controller(directory)
        parent = controller.state["champions"]["Z"]
        session = ResearchSession(controller, 1, "Z", parent, self.packet,
                                  assignment={**IDEA, "job_id": "g001-Z-h01", "turn_budget": 40}, identity="g001-Z-h01")
        session.tools.execute("first", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.system()}]})
        frozen_files = copy.deepcopy(controller.state["system_files"])
        state = viewer.ViewerState(self.project)
        self.addCleanup(state.executor.shutdown, wait=True)
        payload = {"evolution": directory.name, "source": {"kind": "draft", "session": session.identity},
                   "side": "NS", "request": {"call": "1C"}, "compare_to": "parent"}
        before = state.protocol_review(payload)
        self.assertEqual(before["source"]["kind"], "draft")
        self.assertEqual(before["selected"]["status"], "defined")
        self.assertEqual(before["selected"]["explanation"]["basis"], "draft-public-protocol")
        self.assertEqual(before["comparison"]["selected"]["status"], "unused")
        payload["source"]["snapshot"] = before["source"]["snapshot"]
        session.tools.execute("second", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.system("1D")}]})
        pinned = state.protocol_review(payload)
        self.assertEqual(pinned["source"]["hash"], before["source"]["hash"])
        self.assertEqual(pinned["selected"]["status"], "defined")
        payload["source"].pop("snapshot")
        updated = state.protocol_review(payload)
        self.assertNotEqual(updated["source"]["hash"], before["source"]["hash"])
        self.assertEqual(updated["selected"]["status"], "unused")
        self.assertEqual(controller.state["system_files"], frozen_files)
        self.assertEqual(controller.state["agent_requests"], 0)
        self.assertEqual(controller.state["evaluated_pairs"], 0)
        payload["source"]["snapshot"] = before["source"]["snapshot"]
        state.protocol_snapshots.clear()
        with self.assertRaisesRegex(ValueError, "没有替换"):
            state.protocol_review(payload)

    def test_invalid_director_evidence_references_recover_using_only_maintenance_and_replay_without_rebilling(self):
        identity = "g001-Z-h01"
        session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet,
                                  assignment={**IDEA, "job_id": identity, "turn_budget": 40}, identity=identity)
        session.tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.system()}]})
        missing = "6e9d121d0a34fa631a3310da5ffeac91d5433e0d22ce288dec6b8ecc76134162"
        queries = [
            ({"view": "protocol", "candidate": missing, "session": identity}, False),
            ({"view": "protocol", "candidate": missing}, False),
            ({"view": "protocol", "session": "unknown"}, False),
            ({"view": "progress", "session": "unknown"}, False),
            ({"view": "portfolio"}, True),
        ]
        for index, (query, missing_call_id) in enumerate(queries):
            with self.subTest(query=query):
                before_research = budget_usage(self.controller.state, "research")["requests"]
                before_maintenance = budget_usage(self.controller.state, "maintenance")["requests"]
                bad = response(("bad", EVIDENCE["name"], query))
                if missing_call_id:
                    bad["output"][0].pop("call_id")
                good = response(("draft", EVIDENCE["name"], {"view": "protocol", "session": identity}))
                definition = REVIEW if index == 0 else PLAN
                expected = ({"decision": "continue", "learning": "纠正版本引用后继续反馈"} if index == 0
                            else {"summary": "纠正版本引用后继续研究", "ideas": [IDEA]})
                plan = response(("decision", definition["name"], expected))
                for item in (bad, good, plan):
                    item["copilot_usage"] = {"total_nano_aiu": 1000000000}
                with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
                     patch("bridge_evolve.evolution.models.build_opener") as opener:
                    opener.return_value.open.side_effect = [
                        test_research.ModelBudgetTests.Response(item) for item in (bad, good, plan)]
                    path = self.directory / f"evidence-plan-{index}.json"
                    label = f"g001-Z:director:review:{index}"
                    result = structured_request(self.controller, path, "Review evidence.", {}, definition, label)
                    self.assertEqual(result, expected)
                    self.assertEqual(structured_request(self.controller, path, "Review evidence.", {}, definition, label), result)
                    self.assertEqual(opener.return_value.open.call_count, 3)
                self.assertEqual(self.controller.read_artifact(path), bad)
                event = self.controller.state["maintenance_events"][label + ":format"]
                self.assertEqual(event["response"], path.name)
                self.assertEqual(budget_usage(self.controller.state, "research")["requests"], before_research + 1)
                self.assertEqual(budget_usage(self.controller.state, "maintenance")["requests"], before_maintenance + 2)
        self.assertEqual(resource_summary(self.controller.state)["models"]["known_ai_credits"], 3 * len(queries))
        self.assertEqual(self.controller.state["agent_requests"], 3 * len(queries))
        self.assertEqual(session.tools.data["releases"], [])

    def test_trusted_evidence_corruption_is_not_hidden_as_an_interface_correction(self):
        identity = "g001-Z-h01"
        session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet,
                                  assignment={**IDEA, "job_id": identity, "turn_budget": 40}, identity=identity)
        for index, kind in enumerate(("frozen", "draft")):
            with self.subTest(kind=kind):
                if kind == "frozen":
                    candidate = register_system(self.directory, self.controller.state, validate_system(self.system("1D")))
                    target = self.directory / "systems" / f"{candidate}.json"
                    target.write_bytes(target.read_bytes() + b" ")
                    query = {"view": "protocol", "candidate": candidate}
                else:
                    session.tools.path.unlink()
                    query = {"view": "protocol", "session": identity}
                value = response(("evidence", EVIDENCE["name"], query))
                with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
                     patch("bridge_evolve.evolution.models.build_opener") as opener:
                    opener.return_value.open.return_value = test_research.ModelBudgetTests.Response(value)
                    with self.assertRaisesRegex(RuntimeError, "Trusted .* evidence"):
                        structured_request(self.controller, self.directory / f"corrupt-{index}.json",
                                           "Plan.", {}, PLAN, f"g001-Z:director:corrupt:{index}")
                    self.assertEqual(opener.return_value.open.call_count, 1)
                self.assertEqual(budget_usage(self.controller.state, "maintenance")["requests"], 0)
                self.assertFalse(self.controller.state.get("maintenance_events"))

    def test_campaign_tasks_do_not_split_or_consume_each_others_turn_allowance(self):
        self.evaluator()
        self.controller.state["generations"]["1"] = {
            "parents": {"Z": self.parent}, "candidates": {}, "promotions": {}, "status": "research"}
        assignments = []

        def request(messages, tools, **kwargs):
            name = tools[0]["name"]
            if name == PLAN["name"]:
                return response(("plan", name, {"summary": "两项独立机制", "ideas": [IDEA, {**IDEA, "title": "第二机制"}]}))
            if name == "review_implementation":
                return response(("review", name, {"decision": "continue", "learning": "未产出不伪造结果。"}))
            assignment = json.loads(messages[1]["content"])["implementation_assignment"]
            assignments.append(assignment["turn_budget"])
            return response(("finish-" + assignment["job_id"], "finish_research", {"summary": "机制被反例否定，保留结论。"}))

        with patch("bridge_evolve.evolution.director.ModelClient") as director, \
             patch("bridge_evolve.evolution.research.ModelClient") as workers:
            director.return_value.request.side_effect = request
            workers.return_value.request.side_effect = request
            DirectedResearch(self.controller, 1, "Z", self.parent, self.packet).run()
        self.assertEqual(assignments, [40, 40])

    def test_targeted_fresh_dds_batches_reach_coverage_without_using_case_budget(self):
        parent = register_system(self.directory, self.controller.state, validate_system(self.system("1C")))
        self.controller.state["champions"]["Z"] = parent
        self.controller.checkpoint()
        evaluator = self.evaluator()
        tools = ResearchTools(self.controller, "g001-Z-h01", parent, self.packet, self.system("1D"), None)
        tools.data["lab_boards"] = 9999
        target = {"constraints": {"N": {"hcp": [12, 18]}}, "dealer": "N", "vulnerability": "none",
                  "auction": {"reference": "parent", "prefix": ["1C"], "rule_ids": ["test.open"], "side": "NS"}}
        generated = tools.execute("case", "generate_cases", {"sampling": {"count": 1}, "cohort": target})
        self.assertTrue(generated["ok"], generated)
        self.assertEqual(tools.data["lab_boards"], 10000)
        result = tools.execute("trial", "trial", {"boards": 2, "max_boards": 4,
                                                 "min_changed_families": 4, "sampling": target})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["boards"], 4)
        self.assertEqual(result["effect_coverage"]["changed_families"], 4)
        self.assertEqual(result["effect_coverage"]["status"], "sufficient")
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(tools.data["lab_boards"], 10000)
        self.assertEqual(tools.data["trial_boards"], 4)
        self.assertEqual(result["evaluation_scope"], "conditional")
        self.assertIn("NOT global", result["scope"])
        for identity in result["rounds"]:
            saved = self.controller.state["rounds"][identity]
            manifest = json.loads((self.directory / saved["dataset"]).read_text())
            self.assertEqual(manifest["count"], 2)
            self.assertEqual(manifest["sampling"]["definition"]["auction"]["reference"], parent)
            _, boards = load_dataset(self.directory / saved["dataset"])
            for index, board in enumerate(boards):
                check_cohort(manifest["sampling"]["definition"], {"board": board_data(board.board)},
                             index, self.controller.system)
        units = self.controller.state["evaluated_pairs"]
        again = tools.execute("trial", "trial", {"boards": 2, "max_boards": 4,
                                                "min_changed_families": 4, "sampling": target})
        self.assertEqual(again, result)
        self.assertEqual(self.controller.state["evaluated_pairs"], units)
        self.assertEqual(evaluator.config["development_sampling"], None)

    def test_generated_cases_can_exceed_the_old_1000_board_request_cap(self):
        tools = ResearchTools(self.controller, "g001-Z-h01", self.parent, self.packet, blank_system().to_data(), None)
        result = tools.execute("large-generation", "generate_cases", {"sampling": {"count": 1001}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(tools.data["cases"]), 1001)
        self.assertEqual(tools.data["lab_boards"], 1001)
        self.assertEqual(tools.data.get("trial_boards", 0), 0)
        self.assertEqual(self.controller.state["agent_requests"], 0)

    def test_two_candidates_can_replace_the_current_winner_in_the_same_unit(self):
        candidates = [register_system(self.directory, self.controller.state, validate_system(self.system(call)))
                      for call in ("1C", "1D")]
        self.controller.state["generations"]["1"] = {
            "parents": {"Z": self.parent}, "candidates": {}, "promotions": {}, "status": "research"}
        for candidate in candidates:
            self.controller.state["candidate_archive"][candidate] = {
                "hash": candidate, "parent": self.parent, "lineage": "Z", "release": "fixture-release.json"}
            self.controller.state.setdefault("evaluation_queue", {})[candidate] = {
                "candidate": candidate, "status": "development_complete"}
            self.controller.queue_challenge(candidate, "Z", number=1)
        actual_comparisons = []

        def round_result(identity, systems, stage):
            actual_comparisons.append(list(systems))
            candidate, incumbent = systems
            anchors = [row["hash"] for row in self.controller.config["baselines"]]
            return {"id": identity, "seed": 123, "boards": 2, "anchors": anchors,
                    "status": {candidate: "checked", incumbent: "checked"},
                    "scores": {candidate: {anchor: [2, 2] for anchor in anchors},
                               incumbent: {anchor: [0, 0] for anchor in anchors}}}

        with patch.object(self.controller, "round", side_effect=round_result) as evaluate:
            self.controller.process_challenges()
            self.assertEqual(actual_comparisons, [[candidates[0], self.parent], [candidates[1], candidates[0]]])
            self.assertEqual(self.controller.state["champions"]["Z"], candidates[1])
            self.assertEqual(len(self.controller.state["champion_history"]), 2)
            self.assertEqual(len(self.controller.state["generations"]["1"]["promotions"]), 2)
            self.controller.process_challenges()
            self.assertEqual(evaluate.call_count, 2)
            self.controller.state["champions"]["Z"] = candidates[0]
            repeated = self.controller.queue_challenge(candidates[1], "Z", number=1)
            self.assertEqual(repeated["parent"], candidates[0])
            self.assertEqual(repeated["status"], "promoted")
            self.controller.process_challenges()
            self.assertEqual(evaluate.call_count, 2)
            self.controller.state["champions"]["Z"] = candidates[1]
            self.controller.checkpoint()
        self.assertEqual(Controller(self.directory).state["champions"]["Z"], candidates[1])

    def test_three_way_merge_is_atomic_and_keeps_distinct_semantic_changes(self):
        base = blank_system().to_data()
        ours = copy.deepcopy(base)
        ours["features"]["our_feature"] = 1
        theirs = copy.deepcopy(base)
        theirs["features"]["their_feature"] = 2
        merged, conflicts = merge_documents(base, ours, theirs)
        self.assertEqual(conflicts, [])
        self.assertEqual(merged["features"], {"our_feature": 1, "their_feature": 2})
        tools = ResearchTools(self.controller, "g001-Z-h01", self.parent, self.packet, ours, None)
        candidate = register_system(self.directory, self.controller.state, validate_system(theirs))
        result = tools.execute("merge", "merge_candidate", {"candidate": candidate, "base": self.parent})
        self.assertTrue(result["merged"], result)
        self.assertEqual(tools.data["system"]["features"], merged["features"])
        conflict = copy.deepcopy(theirs)
        conflict["features"]["our_feature"] = 9
        other = register_system(self.directory, self.controller.state, validate_system(conflict))
        before = copy.deepcopy(tools.data["system"])
        result = tools.execute("conflict", "merge_candidate", {"candidate": other, "base": self.parent})
        self.assertFalse(result["merged"])
        self.assertIn("/features/our_feature", result["conflicts"])
        self.assertEqual(tools.data["system"], before)
        self.assertEqual(tools.data["releases"], [])

    def test_large_context_and_full_working_set_are_not_forced_back_to_old_caps(self):
        raw = blank_system().to_data()
        raw["features"] = {f"known_{index}": index for index in range(70)}
        parent = register_system(self.directory, self.controller.state, validate_system(raw))
        session = ResearchSession(self.controller, 1, "Z", parent, self.packet,
                                  assignment={**IDEA, "job_id": "g001-Z-h01", "turn_budget": 40},
                                  identity="g001-Z-h01")
        intro = json.loads(session.data["messages"][1]["content"])
        self.assertGreater(intro["working_set"]["retained_nodes"], 32)
        session.data["messages"].append({"role": "user", "content": "evidence " * 30000})
        self.assertGreater(len(request_body(self.controller.config, session.data["messages"], session.definitions)), 175000)
        with patch.object(session.client, "request", return_value=response(
                ("finish", "finish_research", {"summary": "未丢弃大上下文，明确结束。"}))):
            session.run()
        self.assertEqual(session.data["context_compactions"], 0)

    def test_engine_revision_imports_unfinished_branches_and_candidates_without_old_scores(self):
        session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet,
                                  assignment={**IDEA, "job_id": "g001-Z-h01", "turn_budget": 40},
                                  identity="g001-Z-h01")
        session.tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.system()}]})
        case = session.tools.execute("case", "add_cases", {"cases": [{"name": "natural", "board": BOARD}]})["cases"][0]["case_id"]
        session.tools.execute("tests", "write_tests", {"tests": [{
            "name": "changed opening", "case_id": case, "mode": "auction", "changed_from_parent": True,
            "assertions": [{"path": "/auction/0/call", "op": "eq", "value": "1C"}]}]})
        published = session.tools.execute("publish", "publish_candidate", {"hypothesis": "自然改变开叫"})
        self.assertTrue(published["ok"], published)
        draft = ResearchSession(self.controller, 1, "Z", self.parent, self.packet,
                                assignment={**IDEA, "job_id": "g001-Z-h02", "turn_budget": 40},
                                identity="g001-Z-h02")
        draft.tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.system("1D")}]})
        self.controller.state["status"] = "cancelled"
        self.controller.checkpoint()
        continued = initialize({**self.config, "champion_seed_run": str(self.directory)},
                               self.project / "continued", self.project)
        controller = Controller(continued)
        self.assertEqual(controller.state["champions"]["Z"], self.parent)
        self.assertEqual(controller.state["rounds"], {})
        self.assertEqual(controller.state["agent_requests"], 0)
        self.assertIn(published["hash"], controller.state["candidate_archive"])
        self.assertIn("imported-g001-Z-h02", controller.state["research_branches"])
        next_session = ResearchSession(controller, 1, "Z", self.parent, self.packet,
            assignment={**IDEA, "continue_from": "imported-g001-Z-h02", "job_id": "g001-Z-h01", "turn_budget": 40},
            identity="g001-Z-h01")
        self.assertEqual(next_session.tools.system().content_hash, validate_system(self.system("1D")).content_hash)

    def test_dynamic_plan_limits_invalid_assignments_and_last_review_use_maintenance(self):
        self.evaluator()
        self.controller.config["hypotheses_per_unit"] = 1
        self.controller.state["generations"]["1"] = {
            "parents": {"Z": self.parent}, "candidates": {}, "promotions": {}, "status": "research"}
        invalid = response(("plan", PLAN["name"], {
            "summary": "超出声明上限", "ideas": [IDEA, {**IDEA, "title": "额外假设"}]}))
        values = [
            invalid,
            response(("plan", PLAN["name"], {"summary": "不能猜任务名", "ideas": [{**IDEA, "continue_from": "unknown"}]})),
            response(("plan", PLAN["name"], {"summary": "修正为已有接口", "ideas": [IDEA]})),
            response(("finish", "finish_research", {"summary": "本机制被反例否定。"})),
            response(("review", "review_implementation", {"decision": "continue", "learning": "不能增加未声明任务", "next_idea": IDEA})),
            response(("review", "review_implementation", {"decision": "continue", "learning": "完整保留真实失败与结论。"})),
        ]
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = [test_research.ModelBudgetTests.Response(item) for item in values]
            result = DirectedResearch(self.controller, 1, "Z", self.parent, self.packet).run()
        self.assertEqual(result["research_status"], "completed")
        self.assertEqual(len(self.controller.state["research_units"]["g001-Z"]["jobs"]), 1)
        self.assertEqual(budget_usage(self.controller.state, "research")["requests"], 1)
        self.assertEqual(budget_usage(self.controller.state, "maintenance")["requests"], 5)
        self.assertEqual(self.controller.read_artifact(self.directory / "director/g001-Z/plan.json"), invalid)

    def test_bad_output_packet_executes_no_partial_tools_and_is_recovered(self):
        broken = response(
            ("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.system()}]}),
            ("broken", "run_tests", {}))
        broken["output"][1].pop("call_id")
        values = [broken, response(("finish", "finish_research", {"summary": "坏包未执行任何部分工具。"}))]
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = [test_research.ModelBudgetTests.Response(item) for item in values]
            session = ResearchSession(self.controller, 1, "Z", self.parent, self.packet,
                                      assignment={**IDEA, "job_id": "g001-Z-h01", "turn_budget": 40},
                                      identity="g001-Z-h01")
            session.run()
        self.assertEqual(session.tools.system().content_hash, self.parent)
        self.assertNotIn("edit", session.tools.data["operations"])
        self.assertEqual(budget_usage(self.controller.state, "research")["requests"], 0)
        self.assertEqual(budget_usage(self.controller.state, "maintenance")["requests"], 2)

    def test_worker_gateway_failure_preserves_work_and_does_not_cancel_other_ideas(self):
        self.evaluator()
        self.controller.state["generations"]["1"] = {
            "parents": {"Z": self.parent}, "candidates": {}, "promotions": {}, "status": "research"}
        values = [
            test_research.ModelBudgetTests.Response(response(("plan", PLAN["name"], {
                "summary": "两个独立机制", "ideas": [IDEA, {**IDEA, "title": "下一机制"}]}))),
            HTTPError("http://local", 500, "gateway fault", {}, None),
            test_research.ModelBudgetTests.Response(response(("review", "review_implementation", {
                "decision": "continue", "learning": "接口故障不是机制被否定。"}))),
            test_research.ModelBudgetTests.Response(response(("finish", "finish_research", {"summary": "第二机制可正常研究。"}))),
            test_research.ModelBudgetTests.Response(response(("review", "review_implementation", {
                "decision": "continue", "learning": "按实际结果继续。"}))),
        ]
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = values
            result = DirectedResearch(self.controller, 1, "Z", self.parent, self.packet).run()
        self.assertEqual(result["research_status"], "completed")
        jobs = self.controller.state["research_units"]["g001-Z"]["jobs"]
        self.assertEqual(jobs[0]["report"]["worker_status"], "blocked_harness")
        self.assertEqual(jobs[1]["report"]["worker_status"], "completed")
        self.assertEqual(budget_usage(self.controller.state, "maintenance")["requests"], 1)
        self.assertEqual(budget_usage(self.controller.state, "research")["requests"], 4)

    def test_true_research_budget_exhaustion_is_not_mislabeled_as_harness_failure(self):
        self.evaluator()
        self.controller.config["max_agent_requests"] = 1
        self.controller.state["generations"]["1"] = {
            "parents": {"Z": self.parent}, "candidates": {}, "promotions": {}, "status": "research"}
        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.return_value = test_research.ModelBudgetTests.Response(response(
                ("plan", PLAN["name"], {"summary": "正常机制", "ideas": [IDEA]})))
            with self.assertRaisesRegex(EvolutionStopped, "Model request budget exhausted"):
                DirectedResearch(self.controller, 1, "Z", self.parent, self.packet).run()
            self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertNotEqual(self.controller.state["research_units"]["g001-Z"]["status"], "blocked_harness")
        self.assertEqual(budget_usage(self.controller.state, "maintenance")["requests"], 0)

    def test_trial_caps_at_remaining_independent_allowance_without_losing_completed_batches(self):
        self.evaluator()
        self.controller.config["max_trial_boards"] = 3
        tools = ResearchTools(self.controller, "g001-Z-h01", self.parent, self.packet, self.system(), None)
        result = tools.execute("trial", "trial", {
            "boards": 2, "max_boards": 8, "min_changed_families": 100, "uniform": True})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["boards"], 3)
        self.assertEqual(len(result["rounds"]), 2)
        self.assertEqual(tools.data["trial_boards"], 3)
        self.assertEqual(result["effect_coverage"]["status"], "insufficient")
        self.assertTrue(result["effect_coverage"]["limited_by_remaining_trial_budget"])
        self.assertEqual(result["effect_coverage"]["requested_maximum_boards"], 8)

    def test_growth_time_reservation_does_not_consume_the_maintenance_pool(self):
        self.evaluator()
        self.controller.config.update(growth_boards=2, growth_seconds=10)
        self.controller.previous_seconds = 295
        self.controller.state["maintenance_seconds"] = 120
        self.controller.reserve_growth(1)
        self.assertEqual(self.controller.state["growth_checks"]["1"]["reserved_seconds"], 10)


if __name__ == "__main__":
    unittest.main()
