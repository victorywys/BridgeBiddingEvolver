import copy
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge_evolve.evolution.config import ROOT, blank_system
from bridge_evolve.evolution.controller import Controller, initialize, run, status
from bridge_evolve.evolution.director import PLAN, DirectedResearch, structured_request
from bridge_evolve.evolution.execution import Evaluator, EvolutionStopped
from bridge_evolve.evolution.inspection import overview, research_detail
from bridge_evolve.evolution.memory import working_set
from bridge_evolve.evolution.proposals import evidence_packet
from bridge_evolve.evolution.research import ResearchSession
from bridge_evolve.evolution.repairs import RepairQueue, completion_system, record_incident
from bridge_evolve.evolution.research_tools import ResearchTools
from bridge_evolve.systems import validate_system
from test_research import BOARD, response


IDEA = {"title": "人工信息分区", "hypothesis": "改变信息分区可改善后续选择", "mechanism": "给搭档多一次描述机会",
        "implementation": "完整修改发送/接收并验证边界", "falsification": "自然路径不可达或新牌收益退化",
        "focus_keys": ["fallback"]}


class DirectedResearchTests(unittest.TestCase):
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
        self.config = {"profile": "smoke", "source": "research", "origins": ["blank"], "baselines": baselines,
            "research_architecture": "director", "hypotheses_per_unit": 2, "repair_attempts": 0,
            "generations": 1, "workers": 1, "boards": {"diagnosis": 2, "validity": 2, "screen": 2, "full": 2, "promotion": 2},
            "max_research_turns": 40, "max_agent_requests": 60, "max_agent_tokens": 2000000,
            "max_seconds": 300, "evaluation_seconds": 30, "max_pairs": 1000, "max_releases": 2,
            "max_context_bytes": 100000, "resamples": 100, "allow_promotion": False, "automatic_goal_check": False}
        self.directory = initialize(self.config, self.project / "run", self.project)
        self.controller = Controller(self.directory)
        self.parent = self.controller.state["champions"]["Z"]

    def worker_system(self, call="1C"):
        raw = blank_system().to_data()
        raw["rules"] = [{"id": "test.open", "priority": 1,
                         "when": {"op": "not", "args": [{"op": "has_contract_bid"}]},
                         "action": {"kind": "call", "call": call}}]
        return raw

    def test_structured_result_uses_frozen_output_limit_including_reasoning(self):
        arguments = {"summary": "两项独立假设", "ideas": [IDEA]}
        for limit in (4096, 16384):
            with self.subTest(max_output_tokens=limit), patch("bridge_evolve.evolution.director.ModelClient") as client:
                self.controller.config["max_output_tokens"] = limit
                client.return_value.request.return_value = response(("plan", PLAN["name"], arguments))
                result = structured_request(self.controller, self.directory / f"plan-{limit}.json",
                                            "Plan research.", {}, PLAN, "g001-Z:director:plan")
                self.assertEqual(result, arguments)
                self.assertEqual(client.return_value.request.call_args.kwargs["output_tokens"], limit)

    def test_incomplete_structured_result_is_preserved_without_reissue_or_acceptance(self):
        complete = {"summary": "可解析但未完成的计划", "ideas": [IDEA]}
        cases = (("incomplete", '{"summary":"truncated'), ("completed", json.dumps(complete)))
        for index, (status, arguments) in enumerate(cases):
            with self.subTest(response_status=status), patch("bridge_evolve.evolution.director.ModelClient") as client:
                partial = response(("plan", PLAN["name"], complete))
                partial.update(status=status, incomplete_details={"reason": "max_output_tokens"})
                partial["output"][0].update(status="incomplete", arguments=arguments)
                client.return_value.request.return_value = partial
                path = self.directory / f"incomplete-plan-{index}.json"
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, "incomplete structured result.*max_output_tokens"):
                        structured_request(self.controller, path, "Plan research.", {}, PLAN, "g001-Z:director:plan")
                client.return_value.request.assert_called_once()
                self.assertEqual(self.controller.read_artifact(path), partial)

    def test_director_receives_real_evidence_before_deciding_and_jobs_are_isolated_and_recoverable(self):
        phases = {}
        reviews = []

        def request(messages, tools, **kwargs):
            name = tools[0]["name"]
            if name == "submit_research_plan":
                self.assertNotIn("edit_system", [tool["name"] for tool in tools])
                return response(("plan", name, {"summary": "两项独立假设", "ideas": [IDEA, {**IDEA, "title": "另一种编码"}]}))
            if name == "review_implementation":
                content = json.loads(messages[1]["content"])
                report = content["trusted_implementation_report"]
                self.assertTrue(report["actual_tests"]["passed"])
                self.assertIn("full_delta", report["releases"][0]["development"])
                reviews.append(report)
                return response(("review", name, {"decision": "continue", "learning": "按真实新牌结果记录，不预设正收益。"}))
            label = kwargs["label"].split(":")[0]
            phase = phases.get(label, 0)
            phases[label] = phase + 1
            call = "1C" if label.endswith("h01") else "1D"
            if phase == 0:
                # The second implementer also starts from the frozen parent, not the first worker's draft.
                context = json.loads(messages[1]["content"])
                self.assertEqual(context["working_rule_count"], 0)
                self.assertEqual(context["implementation_assignment"]["turn_budget"], 20 if label.endswith("h01") else 36)
                return response((label + "-edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.worker_system(call)}]}))
            if phase == 1:
                return response((label + "-case", "add_cases", {"cases": [{"name": "natural", "board": BOARD}]}))
            if phase == 2:
                from bridge_evolve.evaluation.data import canonical_hash
                case = canonical_hash({"call": label + "-case", "index": 0})[:16]
                return response((label + "-tests", "write_tests", {"tests": [{
                    "name": "opening", "case_id": case, "mode": "auction", "changed_from_parent": True,
                    "assertions": [{"path": "/auction/0/call", "op": "eq", "value": call}]}]}))
            if phase == 3:
                return response((label + "-publish", "publish_candidate", {"hypothesis": "Fixture complete change."}))
            self.fail("An implementer kept polling after publication instead of returning to its director.")

        with patch("bridge_evolve.evolution.director.ModelClient") as director, patch("bridge_evolve.evolution.research.ModelClient") as workers:
            director.return_value.request.side_effect = request
            workers.return_value.request.side_effect = request
            result = run(self.directory)
            self.assertEqual(result["status"], "completed", result.get("error"))
            self.assertEqual(len(reviews), 2)
            unit = result["research_units"]["g001-Z"]
            self.assertEqual(unit["worker_turns"], 8)
            self.assertEqual([job["status"] for job in unit["jobs"]], ["reported", "reported"])
            self.assertTrue(all("真实回归" in job["report"]["summary"] for job in unit["jobs"]))
            self.assertTrue(all(not job["report"]["summary"].startswith("{") for job in unit["jobs"]))
            self.assertEqual(set(result["research_sessions"]), {"g001-Z-h01", "g001-Z-h02"})
            again = run(self.directory)
            self.assertEqual(again["evaluated_pairs"], result["evaluated_pairs"])
            self.assertEqual(len(reviews), 2)
        view = overview(self.directory, details=True)
        self.assertEqual(view["workflow"]["architecture"], "director")
        self.assertEqual(len(view["workflow"]["units"][0]["jobs"]), 2)
        details = research_detail(self.directory, "g001-Z-h01")
        self.assertEqual(details["role"], "implementer")

    def test_compaction_retains_actual_ir_current_stage_and_evaluation_without_fixed_restart(self):
        packet = evidence_packet(self.controller.system(self.parent), [], ["*"])
        session = ResearchSession(self.controller, 1, "Z", self.parent, packet,
            assignment={**IDEA, "job_id": "g001-Z-h01", "turn_budget": 40}, identity="g001-Z-h01")
        session.tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.worker_system()}]})
        session.tools.execute("read", "navigate_system", {"request": {"view": "read", "keys": ["rule:test.open"], "dependencies": False}})
        session.tools.execute("stage", "update_implementation_state", {
            "stage": "testing", "verified": "已实现发送", "blocker": "", "next_action": "运行已编写回归",
            "stage_design": "空进程：全部手牌分区→1C；公开承诺未限定；同伴依公开协议接续。待检验干扰与覆盖。"})
        session.tools.data["last_development_evaluation"] = {"status": "no_full_gain", "candidate": "fixture"}
        session.compact("Test context reset.")
        context = json.loads(session.data["messages"][1]["content"])
        self.assertEqual(context["working_set"]["nodes"][0]["data"]["id"], "test.open")
        self.assertEqual(context["implementation_state"]["next_action"], "运行已编写回归")
        self.assertIn("全部手牌分区→1C", context["implementation_state"]["stage_design"])
        self.assertEqual(research_detail(self.directory, session.identity)["implementation_state"]["stage_design"],
                         context["implementation_state"]["stage_design"])
        self.assertEqual(context["development_evaluation"]["status"], "no_full_gain")
        self.assertNotIn("start", context["navigation"])
        session.tools.execute("change", "edit_system", {"edits": [{"op": "replace", "path": "/rules/0/action/call", "value": "1D"}]})
        retained = working_set(session.tools, 20000)
        self.assertTrue(retained["nodes"][0]["changed_since_read"])
        self.assertEqual(retained["nodes"][0]["data"]["action"]["call"], "1D")

    def test_repeated_information_is_detected_but_new_definitions_reset_the_streak(self):
        tools = ResearchTools(self.controller, "g001-Z-h01", self.parent, {"selected_groups": []}, self.worker_system(), None)
        tools.data["assignment"] = IDEA
        args = {"request": {"view": "read", "keys": ["rule:test.open"], "dependencies": False}}
        for index in range(14):
            tools.execute("read-" + str(index), "navigate_system", args)
        self.assertEqual(tools.data["research_memory"]["no_information_streak"], 13)
        tools.execute("new", "navigate_system", {"request": {"view": "read", "keys": ["fallback"], "dependencies": False}})
        self.assertEqual(tools.data["research_memory"]["no_information_streak"], 0)

    def test_dependency_changes_invalidate_an_unchanged_rule_and_deleted_nodes_are_not_restored(self):
        raw = self.worker_system()
        raw["features"]["sufficient"] = {"op": "ge", "args": [{"op": "hcp"}, 10]}
        raw["rules"][0]["when"] = {"op": "ref", "name": "sufficient"}
        tools = ResearchTools(self.controller, "g001-Z-h01", self.parent, {"selected_groups": []}, raw, None)
        read = tools.execute("read-dependent-rule", "navigate_system", {"request": {
            "view": "read", "keys": ["rule:test.open"], "dependencies": False}})
        self.assertTrue(read["ok"], read)
        tools.execute("changed-dependency", "edit_system", {"edits": [
            {"op": "replace", "path": "/features/sufficient/args/1", "value": 15}]})
        memory = working_set(tools, 12000)
        self.assertEqual(memory["nodes"][0]["data"], raw["rules"][0])
        self.assertTrue(memory["nodes"][0]["changed_since_read"])
        self.assertEqual(memory["unloaded_dependencies"], ["feature:sufficient"])
        tools.execute("remove-known-rule", "edit_system", {"edits": [{"op": "remove", "path": "/rules/0"}]})
        memory = working_set(tools, 12000)
        self.assertEqual(memory["nodes"], [])
        self.assertIn("rule:test.open", memory["invalidated"])

    def test_director_can_stop_without_forcing_a_candidate_or_fake_positive_result(self):
        self.controller.state["generations"]["1"] = {"status": "running", "parents": {"Z": self.parent}, "candidates": {}, "promotions": {}}
        self.controller.evaluator = Evaluator(self.directory, self.controller.config, self.controller.state,
                                            self.controller.checkpoint, self.controller.stop)
        self.addCleanup(self.controller.evaluator.close)
        replies = [response(("plan", "submit_research_plan", {"summary": "检验表达能力", "ideas": [IDEA]})),
                   response(("review", "review_implementation", {"decision": "stop", "learning": "当前算子不能表达该要求，保留失败。"}))]
        with patch("bridge_evolve.evolution.director.ModelClient") as director, patch("bridge_evolve.evolution.research.ModelClient") as worker:
            director.return_value.request.side_effect = replies
            worker.return_value.request.return_value = response(("finish", "finish_research", {"summary": "需要可信引擎新算子，未发布。"}))
            result = DirectedResearch(self.controller, 1, "Z", self.parent,
                                     evidence_packet(self.controller.system(self.parent), [], ["*"])).run()
        self.assertEqual(result["proposals"], [])
        self.assertEqual(self.controller.state["research_units"]["g001-Z"]["jobs"][0]["report"]["releases"], [])

    def test_interrupted_director_feedback_resumes_without_restarting_the_completed_worker(self):
        requests = []

        def direct(messages, tools, **kwargs):
            requests.append(tools[0]["name"])
            if tools[0]["name"] == "submit_research_plan":
                return response(("plan", "submit_research_plan", {"summary": "单一假设", "ideas": [IDEA]}))
            raise KeyboardInterrupt

        with patch("bridge_evolve.evolution.director.ModelClient") as director, patch("bridge_evolve.evolution.research.ModelClient") as worker:
            director.return_value.request.side_effect = direct
            worker.return_value.request.return_value = response(("finish", "finish_research", {"summary": "不可表达，返回具体阻碍。"}))
            interrupted = run(self.directory)
            self.assertEqual(interrupted["status"], "interrupted")
            used = interrupted["evaluated_pairs"]
            worker_count = worker.return_value.request.call_count
            director.return_value.request.side_effect = None
            director.return_value.request.return_value = response(("review", "review_implementation", {
                "decision": "stop", "learning": "需要可信引擎扩展，保留失败而非重复启动。"}))
            finished = run(self.directory)
            self.assertEqual(finished["status"], "completed", finished.get("error"))
            self.assertEqual(finished["evaluated_pairs"], used)
            self.assertEqual(worker.return_value.request.call_count, worker_count)
            self.assertEqual(requests.count("submit_research_plan"), 1)

    def test_old_peer_workspaces_are_not_loaded_into_a_new_implementation_assignment(self):
        packet = evidence_packet(self.controller.system(self.parent), [], ["*"])
        first = ResearchSession(self.controller, 1, "Z", self.parent, packet,
            assignment={**IDEA, "job_id": "g001-Z-h01", "turn_budget": 20}, identity="g001-Z-h01")
        first.tools.execute("edit-peer", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.worker_system()}]})
        second = ResearchSession(self.controller, 1, "Z", self.parent, packet,
            assignment={**IDEA, "job_id": "g001-Z-h02", "turn_budget": 20}, identity="g001-Z-h02")
        self.assertEqual(second.tools.system().content_hash, self.parent)
        self.assertEqual(len(second.tools.data["system"]["rules"]), 0)
        third = ResearchSession(self.controller, 1, "Z", self.parent, packet,
            assignment={**IDEA, "job_id": "g001-Z-h03", "turn_budget": 20, "continue_from": "g001-Z-h01"},
            identity="g001-Z-h03")
        self.assertEqual(third.tools.system().content_hash, first.tools.system().content_hash)
        self.assertTrue(third.data["continued_from"]["explicit_director_handoff"])

    def test_low_information_loop_returns_to_director_instead_of_spending_all_turns(self):
        packet = evidence_packet(self.controller.system(self.parent), [], ["*"])
        index = 0

        def repeat(*args, **kwargs):
            nonlocal index
            index += 1
            return response((f"same-{index}", "navigate_system", {"request": {"view": "read", "keys": ["fallback"]}}))

        with patch("bridge_evolve.evolution.research.ModelClient") as worker:
            worker.return_value.request.side_effect = repeat
            session = ResearchSession(self.controller, 1, "Z", self.parent, packet,
                assignment={**IDEA, "job_id": "g001-Z-h01", "turn_budget": 40}, identity="g001-Z-h01")
            result = session.run()
        self.assertEqual(result["status"], "stalled")
        self.assertEqual(session.data["turns"], 13)
        self.assertEqual(result["releases"], [])

    def test_standby_model_failure_is_reported_without_rewriting_scores_or_baselines(self):
        self.controller.config["repair_attempts"] = 1
        anchors = [item["hash"] for item in self.controller.config["baselines"]]
        record = {**BOARD, "tables": [{"id": "table1", "systems": {
            "NS": {"hash": self.parent}, "EW": {"hash": anchors[0]}}}],
            "audit_result": {"errors": [{"stage": "dds", "message": "Fixture native solver failure",
                                         "type": "RuntimeError"}]}}
        record_incident(self.controller, record, self.parent, anchors[0], "fixture-failure")
        original_systems = copy.deepcopy(self.controller.state["system_files"])
        with patch("bridge_evolve.evolution.director.ModelClient") as model:
            model.return_value.request.side_effect = RuntimeError("Fixture repair gateway unavailable")
            RepairQueue(self.controller).process()
        job = next(iter(self.controller.state["repair_jobs"].values()))
        self.assertEqual(job["status"], "requires_review")
        self.assertIn("gateway unavailable", job["repair_error"])
        self.assertEqual(self.controller.state["system_files"], original_systems)
        self.assertNotIn("candidate_path", job)


class RepairPermissionTests(unittest.TestCase):
    def original(self):
        raw = blank_system().to_data()
        raw["name"] = "Repairable baseline"
        raw["rules"] = [{"id": "existing.finish", "priority": 20, "when": {"op": "has_contract_bid"},
                         "action": {"kind": "call", "call": "P"}}]
        raw["fallback"] = {"kind": "error", "message": "No declared classic-policy completion"}
        return raw

    def proposal(self):
        return {"basis": "Fixture declared natural opening; not a strength claim.", "rules": [{
            "id": "completion.open", "when": {"op": "and", "args": [
                {"op": "eq", "args": [{"op": "state"}, "idle"]}, {"op": "not", "args": [{"op": "has_contract_bid"}]}]},
            "action": {"kind": "call", "call": "1C"}}]}

    def test_completion_cannot_override_old_rules_public_protocol_or_insert_a_pass_fallback(self):
        original = self.original()
        result = completion_system(original, self.proposal(), "idle", "test").to_data()
        self.assertEqual(result["rules"][0], original["rules"][0])
        self.assertLess(result["rules"][-1]["priority"], original["rules"][0]["priority"])
        self.assertEqual(result["protocol"], validate_system(original).to_data()["protocol"])
        for mutation in ("priority", "unguarded", "pass"):
            proposal = self.proposal()
            if mutation == "priority":
                proposal["rules"][0]["priority"] = 9999
            elif mutation == "unguarded":
                proposal["rules"][0]["when"] = True
            else:
                proposal["rules"][0]["action"]["call"] = "P"
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, TypeError)):
                completion_system(original, proposal, "idle", "test")

    def test_real_baseline_fault_creates_deduplicated_work_then_independent_reviewed_export_not_hot_deploy(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            paths = []
            for name, raw in (("bad", self.original()), ("other", blank_system().to_data())):
                path = project / f"{name}.json"
                path.write_text(json.dumps(raw))
                paths.append(str(path))
            directory = initialize({"profile": "smoke", "source": "research", "origins": ["blank"],
                "baselines": paths, "research_architecture": "director", "repair_attempts": 1, "workers": 1,
                "max_agent_requests": 10, "max_seconds": 300, "max_pairs": 100, "resamples": 100}, project / "run", project)
            controller = Controller(directory)
            evaluator = Evaluator(directory, controller.config, controller.state, controller.checkpoint, controller.stop)
            evaluator.incident_callback = lambda record, candidate, opponent, identity: record_incident(
                controller, record, candidate, opponent, identity)
            try:
                result = evaluator.evaluate("fault-event", [controller.state["champions"]["Z"]], 2)
            finally:
                evaluator.close()
            self.assertEqual(set(result["status"].values()), {"checked"})
            self.assertEqual(len(controller.state["repair_jobs"]), 1)
            job = next(iter(controller.state["repair_jobs"].values()))
            self.assertGreater(job["occurrences"], 1)
            baseline_hash = controller.config["baselines"][0]["hash"]
            with patch("bridge_evolve.evolution.director.ModelClient") as model:
                model.return_value.request.side_effect = [
                    response(("proposal", "propose_baseline_completion", self.proposal())),
                    response(("review", "review_baseline_completion", {"approve": True, "reason": "Fixture basis and preservation checks passed."}))]
                RepairQueue(controller).process()
            self.assertEqual(job["status"], "accepted_for_next_run", job.get("error"))
            self.assertGreater(job["validation"]["replayed_tables"], 0)
            self.assertEqual(controller.config["baselines"][0]["hash"], baseline_hash)
            self.assertEqual(controller.system(baseline_hash).fallback.kind, "error")
            self.assertTrue((directory / job["candidate_path"]).is_file())
            self.assertNotEqual(job["candidate"], baseline_hash)

    def test_framework_incidents_generate_a_diagnosed_revision_request_without_running_host_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            baselines = []
            for name in ("a", "b"):
                raw = blank_system().to_data()
                raw["name"] = name
                path = project / f"{name}.json"
                path.write_text(json.dumps(raw))
                baselines.append(str(path))
            directory = initialize({"profile": "smoke", "source": "research", "origins": ["blank"],
                "baselines": baselines, "research_architecture": "director", "repair_attempts": 1,
                "max_agent_requests": 10, "max_seconds": 300}, project / "run", project)
            controller = Controller(directory)
            a, b = [item["hash"] for item in controller.config["baselines"]]
            record = {**BOARD, "tables": [{"id": "table1", "systems": {"NS": {"hash": a}, "EW": {"hash": b}}}],
                      "audit_result": {"errors": [{"stage": "dds", "message": "Fixture oracle fault", "type": "RuntimeError"}]}}
            record_incident(controller, record, a, b, "fixture")
            with patch("bridge_evolve.evolution.director.ModelClient") as model:
                model.return_value.request.return_value = response(("diagnose", "request_engine_revision", {
                    "diagnosis": "Fixture verified oracle error, not candidate loss.",
                    "repair_plan": "Isolated engineering revision and solver provenance check.",
                    "required_tests": ["Real DDS availability and exact input reproduction"]}))
                RepairQueue(controller).process()
            job = next(iter(controller.state["repair_jobs"].values()))
            self.assertEqual(job["status"], "engine_revision_proposed")
            self.assertNotIn("candidate_path", job)
            self.assertEqual(controller.config["baselines"][0]["hash"], a)


class ActiveMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        raw = blank_system().to_data()
        raw.update(name="Existing illegal rule", rules=[
            {"id": "open", "priority": 20, "when": {"op": "not", "args": [{"op": "has_contract_bid"}]},
             "action": {"kind": "call", "call": "1C"}},
            {"id": "existing.finish", "priority": 10, "when": {"op": "has_contract_bid"},
             "action": {"kind": "call", "call": "1C"}}],
             fallback={"kind": "error", "message": "No declared classic-policy completion"})
        paths = []
        for name, definition in (("bad", raw), ("other", blank_system().to_data())):
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(definition))
            paths.append(str(path))
        self.directory = initialize({"profile": "smoke", "source": "research", "origins": ["blank"],
            "baselines": paths, "research_architecture": "director", "research_budget_mode": "campaign",
            "repair_attempts": 1, "workers": 1, "max_agent_requests": 20, "max_agent_tokens": 2000000,
            "max_context_bytes": 100000, "max_seconds": 300, "automatic_goal_check": False,
            "repair_continuation_mode": "evaluation", "model": "offline-test-only",
            "maintenance_budget": {"max_requests": 40, "max_tokens": 2000000, "max_seconds": 300}},
            self.project / "run", self.project)
        self.controller = Controller(self.directory)
        self.baseline = self.controller.config["baselines"][0]["hash"]
        self.parent = self.controller.state["champions"]["Z"]

    def incident(self):
        from bridge_evolve.evolution.maintenance import replay_board
        layout = {"NS": self.parent, "EW": self.baseline}
        result = replay_board({side: self.controller.system(identity) for side, identity in layout.items()}, BOARD)
        self.assertEqual(result["status"], "failed")
        error = {**result["error"], "table": "table1", "stage": "auction"}
        record = {**BOARD, "tables": [{"id": "table1", "systems": {
            side: {"hash": identity} for side, identity in layout.items()}, "error": error}],
            "audit_result": {"errors": [error]}}
        record_incident(self.controller, record, self.parent, self.baseline, "real-counterexample")
        job = next(iter(self.controller.state["repair_jobs"].values()))
        self.controller.artifact(self.directory / "repairs" / job["id"] / "regression-boards.json",
                                 [{"index": 0, "board": BOARD}])
        self.controller.checkpoint()
        return job, record

    def test_engine_file_discovery_paginates_real_paths_searches_lines_and_rejects_escape(self):
        from bridge_evolve.evolution.engine_maintenance import EngineWorkspace
        for directory in ("src", "tests", "tools"):
            (self.project / directory).mkdir()
        (self.project / "src/actual_module.py").write_text("def distinctive_marker():\n    return 17\n")
        (self.project / "src/second.py").write_text("distinctive_marker = 9\n")
        (self.project / "pyproject.toml").write_text("[project]\nname='discovery-fixture'\nversion='0.0.0'\n")
        job = {"id": "repair-" + "d" * 20}
        with patch("bridge_evolve.evolution.engine_maintenance.ROOT", self.project):
            workspace = EngineWorkspace(self.controller, job)
        first = workspace.execute("list_engine_files", {"glob": "src/*.py", "limit": 1}, "list")
        second = workspace.execute("list_engine_files", {
            "glob": "src/*.py", "offset": first["next_offset"], "limit": 1}, "list-next")
        self.assertEqual(first["value"] + second["value"], ["src/actual_module.py", "src/second.py"])
        result = workspace.execute("search_engine_files", {
            "query": "distinctive_marker", "glob": "src/*.py", "limit": 1}, "search")
        found = result["value"][0]
        self.assertEqual((found["path"], found["line"]), ("src/actual_module.py", 1))
        read = workspace.execute("read_engine_file", {"path": found["path"], "line_start": found["line"]}, "read-found")
        self.assertIn("distinctive_marker", json.dumps(read))
        (self.project / "outside.py").write_text("not authorized for the isolated agent")
        (workspace.root / "src/escape.py").symlink_to(self.project / "outside.py")
        for index, path in enumerate(("../outside.py", "src/escape.py")):
            denied = workspace.execute("read_engine_file", {"path": path}, f"escape-{index}")
            self.assertFalse(denied["ok"])
        self.assertNotIn("src/escape.py", workspace.files())

    def test_complete_engine_bundle_reads_all_references_in_an_isolated_process_and_detects_asset_loss(self):
        import subprocess
        import sys
        from bridge_evolve.evolution.config import engine_identity
        from bridge_evolve.evolution.runtime import freeze_engine, validate_runtime
        root = self.project / "complete-engine"
        identity = freeze_engine(ROOT, root)
        self.assertEqual(freeze_engine(ROOT, root), identity)
        code = (
            "from bridge_evolve.evolution.runtime import REFERENCES,validate_runtime\n"
            "from bridge_evolve.evolution.config import ROOT,engine_identity\n"
            "from bridge_evolve.evolution.research_tools import ResearchTools\n"
            "from types import SimpleNamespace\n"
            "validate_runtime(ROOT)\n"
            "tools=object.__new__(ResearchTools)\n"
            "tools.controller=SimpleNamespace(config={})\n"
            "for topic in REFERENCES:\n"
            " assert tools.do_read_reference({'topic':topic})['value'],topic\n"
            "print(engine_identity())\n")
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=root,
            env={**os.environ, "PYTHONPATH": str(root / "src")}, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), identity)
        (root / "docs/08-general-protocols-v2.md").unlink()
        with self.assertRaises(FileNotFoundError):
            validate_runtime(root)
        self.assertNotEqual(engine_identity(root), identity)

    def test_new_accepted_base_preserves_old_engineering_work_as_a_proposal_in_a_fresh_branch(self):
        from bridge_evolve.evolution.config import engine_identity
        from bridge_evolve.evolution.engine_maintenance import EngineWorkspace
        for directory in ("src", "tests", "tools"):
            (self.project / directory).mkdir()
        for name in ("board_viewer.py", "verify_classics.py"):
            (self.project / "tools" / name).write_text("")
        (self.project / "src/work.py").write_text("def answer():\n    return 1\n")
        (self.project / "pyproject.toml").write_text("[project]\nname='rebase-fixture'\nversion='0.0.0'\n")
        job = {"id": "repair-" + "c" * 20}
        with patch("bridge_evolve.evolution.engine_maintenance.ROOT", self.project):
            old = EngineWorkspace(self.controller, job)
        edit = {"path": "src/work.py", "old": "return 1", "new": "return 2"}
        self.assertTrue(old.execute("edit_engine_file", edit, "unaccepted-edit")["ok"])
        base = self.project / "accepted-base"
        shutil.copytree(old.original, base, symlinks=True)
        (base / "src/other_fix.py").write_text("another_fix = True\n")
        self.controller.state["repair_engine"] = {
            "path": str(base), "hash": engine_identity(base), "repair": "accepted-other"}
        fresh = EngineWorkspace(self.controller, job)
        self.assertNotEqual(fresh.root, old.root)
        self.assertIn("return 2", (old.root / "src/work.py").read_text())
        self.assertIn("return 1", (fresh.root / "src/work.py").read_text())
        self.assertIn("another_fix", (fresh.root / "src/other_fix.py").read_text())
        reference = fresh.data["prior_branch"]["evidence"]["details_ref"]
        retained = fresh.execute("read_engine_result", {
            "result_id": reference["result_id"], "pointer": "/changes/src~1work.py/after"}, "retained-edit")
        self.assertIn("return 2", retained["text"])
        self.assertTrue(fresh.execute("edit_engine_file", edit, "ported-edit")["ok"])
        self.assertIn("another_fix", (fresh.root / "src/other_fix.py").read_text())

    def test_fatal_framework_error_is_dispatched_before_failed_run_can_handoff_and_recovery_is_bounded(self):
        from bridge_evolve.evolution.handoff import prepare
        self.controller.config["auto_continue_repairs"] = True

        def finish_repairs():
            for job in self.controller.state.get("repair_jobs", {}).values():
                if job["kind"] == "framework":
                    job.update(status="accepted_engine_revision", engine_hash="a" * 64, engine_path=str(ROOT))
                    self.controller.state["repair_engine"] = {
                        "hash": job["engine_hash"], "path": str(ROOT), "repair": job["id"]}

        with patch.object(self.controller, "load_frozen_state"), patch.object(
                self.controller, "generation", side_effect=FileNotFoundError("required runtime reference missing")), patch.object(
                self.controller, "process_repairs", side_effect=finish_repairs):
            result = self.controller.run()
        self.assertEqual(result["status"], "continued", result.get("error"))
        incident = result["recovery_incident"]
        job = result["repair_jobs"][incident]
        self.assertEqual(job["error"]["type"], "FileNotFoundError")
        self.assertEqual(result["continuation"]["recovery"]["incident"], incident)
        evidence = self.controller.read_artifact(self.directory / job["evidence"][0])
        self.assertTrue(evidence["source_hints"])
        self.controller.state.update(status="failed", continuation=None,
            recovery_history=[{"incident": "repair-" + "b" * 19 + str(index), "fingerprint": job["fingerprint"]}
                              for index in range(3)])
        self.assertIsNone(prepare(self.controller))
        self.assertIn("three", self.controller.state["recovery_blocked"]["reason"])
        self.controller.state["recovery_history"] = []
        job["status"] = "requires_engine_revision"
        self.assertIsNone(prepare(self.controller))
        job["status"] = "accepted_engine_revision"
        self.controller.state["repair_engine"] = {"path": str(ROOT), "hash": "c" * 64, "repair": "unrelated",
                                                "covers": ["unrelated"]}
        self.assertIsNone(prepare(self.controller))

    def test_illegal_and_public_guarantee_faults_are_maintained_but_never_forfeited(self):
        from bridge_evolve.evolution.faults import baseline_branch_fault
        job, record = self.incident()
        self.assertEqual(job["kind"], "baseline-policy")
        error = record["audit_result"]["errors"][0]
        self.assertFalse(baseline_branch_fault(record, error, self.parent, self.baseline))
        record["audit_result"]["errors"][0] = {**error, "stage": "checks",
            "message": "Unsound public bound EW/var.responder_D_min=6, actual=4, rule=fixture.public."}
        record_incident(self.controller, record, self.parent, self.baseline, "public-counterexample")
        self.assertEqual(len(self.controller.state["repair_jobs"]), 2)
        self.assertTrue(all(row["kind"] == "baseline-policy" for row in self.controller.state["repair_jobs"].values()))

    def test_queries_old_rule_edit_failed_submission_and_review_feedback_reach_executable_delivery(self):
        from bridge_evolve.evolution.maintenance import RepairTools
        job, _ = self.incident()
        proposal = {"summary": "修正已开叫后的旧规则", "basis": "自然开叫之后无新增牌情应停叫", "remaining_issues": "不声称全空间强度证明"}
        regression = {"name": "原非法重复开叫应停叫", "case_id": "incident-0", "mode": "decision",
            "prefix": ["P", "1C", "P"], "assertions": [{"path": "/call", "op": "eq", "value": "P"}]}
        with patch("bridge_evolve.evolution.director.ModelClient") as model:
            model.return_value.request.side_effect = [
                response(("original", "replay_incidents", {"snapshot": "parent"})),
                response(("read", "navigate_system", {"request": {
                    "view": "read", "keys": ["rule:existing.finish"], "dependencies": True, "transitive": True}})),
                response(("too-early", "submit_repair", proposal)),
                response(("edit", "edit_system", {"edits": [
                    {"op": "replace", "path": "/rules/1/action", "value": {"kind": "call", "call": "P"}}]})),
                response(("first-regression", "write_tests", {"tests": [regression]})),
                response(("submit", "submit_repair", proposal)),
                response(("independent-read", "navigate_system", {"snapshot": "parent",
                    "request": {"view": "read", "keys": ["rule:existing.finish"]}})),
                response(("review-return", "review_repair", {"approve": False, "reason": "补对手开叫后的停叫边界。"})),
                response(("test", "write_tests", {"tests": [regression, {**regression, "name": "对手开叫后的停叫边界",
                    "prefix": ["1D", "P", "P"]}]})),
                response(("resubmit", "submit_repair", proposal)),
                response(("review-accept", "review_repair", {"approve": True, "reason": "修复原有非法规则，公开协议及正常开叫均保持。"}))]
            RepairQueue(self.controller).process()
            used = model.return_value.request.call_count
            RepairQueue(self.controller).process()
            self.assertEqual(model.return_value.request.call_count, used)
        self.assertEqual(job["status"], "accepted_for_next_run", job.get("repair_error"))
        self.assertEqual(job["iteration"], 2)
        self.assertEqual(job["validation"]["status"], "passed")
        self.assertEqual(job["validation"]["authored_tests"]["passed"], 2)
        self.assertEqual(self.controller.config["baselines"][0]["hash"], self.baseline)
        self.assertEqual(self.controller.system(self.baseline).to_data()["rules"][1]["action"]["call"], "1C")
        workspace = RepairTools(self.controller, job)
        self.assertEqual(workspace.system().to_data()["rules"][1]["action"]["call"], "P")
        self.assertGreater(workspace.do_replay_incidents({})["total"], 0)
        self.assertTrue(self.controller.state["maintenance_events"])

    def test_completed_and_interrupted_audits_return_control_to_the_maintainer(self):
        from bridge_evolve.evolution.maintenance import RepairTools
        job, _ = self.incident()
        job["status"] = "proposing"
        workspace = RepairTools(self.controller, job)
        workspace.data["system"]["rules"][1]["action"]["call"] = "P"
        workspace.data["tests"] = [{"name": "停叫边界", "case_id": "incident-0", "mode": "decision",
            "prefix": ["P", "1C", "P"], "assertions": [{"path": "/call", "op": "eq", "value": "1D"}]}]
        failed = workspace.audit()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(job["status"], "proposing")
        self.assertEqual(job["validation"], failed)
        workspace.data["tests"][0]["assertions"][0]["value"] = "P"
        passed = workspace.audit()
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(job["status"], "proposing")
        saved = json.loads((self.directory / "state.json").read_text())["repair_jobs"][job["id"]]
        self.assertEqual(saved["status"], "proposing")
        self.assertEqual(saved["validation"], passed)
        workspace.data["tests"][0]["name"] = "取消后的工作稿"
        with patch.object(workspace, "replay_evidence", side_effect=EvolutionStopped("Explicit cancellation")):
            with self.assertRaises(EvolutionStopped):
                workspace.audit()
        self.assertEqual(job["status"], "proposing")
        self.assertEqual(job["validation"], passed)

    def test_failed_reference_sampling_saves_the_actual_board_and_routes_to_maintenance(self):
        from bridge_evolve.evolution.sampling import matches_auction
        from bridge_evolve.evolution.research_tools import ResearchTools
        tools = ResearchTools(self.controller, "g001-Z", self.parent,
                              {"selected_groups": [], "total_groups": 0}, blank_system().to_data(), None)
        from bridge_evolve.systems.inputs import parse_board
        target = {"auction": {"reference": self.parent, "opponent": self.baseline, "prefix": ["P"]}}
        with patch.object(tools, "do_generate_cases", side_effect=lambda *args: matches_auction(
                parse_board(BOARD), target, self.controller.system)):
            result = tools.execute("reference-error", "generate_cases", {"sampling": {"count": 1}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["repair"]["owner"], "maintenance")
        job = next(iter(self.controller.state["repair_jobs"].values()))
        evidence = self.controller.read_artifact(self.directory / job["evidence"][0])
        self.assertEqual(evidence["board"], BOARD)

    def test_new_engine_imports_real_incidents_and_two_hops_preserve_drafts_and_remaining_resources(self):
        from bridge_evolve.evolution.handoff import initialize_maintenance, prepare
        from bridge_evolve.evolution.controller import register_system
        from bridge_evolve.evolution.maintenance import RepairTools
        job, _ = self.incident()
        repair = RepairTools(self.controller, job)
        complete_result = {"saved_evidence": "x" * 10000}
        reference = repair.full_result(complete_result)
        repair.save()
        workspace = ResearchTools(self.controller, "g001-Z-h01", self.parent,
                                  {"selected_groups": [], "total_groups": 0}, blank_system().to_data(), None)
        workspace.data["cases"]["saved-board"] = {"name": "必须继承的原牌", "board": BOARD}
        workspace.save()
        self.controller.state["research_sessions"]["g001-Z-h01"] = {
            "lineage": "Z", "parent": self.parent,
            "workspace": workspace.path.relative_to(self.directory).as_posix()}
        self.controller.state.update(status="blocked", spent_seconds=20, maintenance_seconds=7,
            model_requests=[
                {"request": 1, "label": "g001-Z-h01:turn:1", "budget": "research",
                 "status": "received", "usage": {"input_tokens": 100, "output_tokens": 20}},
                {"request": 2, "label": "repair:fixture", "budget": "maintenance",
                 "status": "received", "usage": {"input_tokens": 30, "output_tokens": 20}}],
            agent_requests=2)
        self.controller.checkpoint()
        before = (self.directory / "state.json").read_bytes()
        directory = initialize_maintenance(self.directory, output=self.project / "maintenance")
        self.assertEqual((self.directory / "state.json").read_bytes(), before)
        maintained = Controller(directory)
        self.assertEqual(maintained.config["max_agent_requests"], self.controller.config["max_agent_requests"] - 1)
        self.assertEqual(maintained.config["max_agent_tokens"], 2000000 - 120)
        self.assertEqual(maintained.config["maintenance_budget"]["max_tokens"], 2000000 - 50)
        self.assertTrue(maintained.state["repair_jobs"])
        copied = maintained.directory / "repairs" / job["id"] / "results" / f"{reference['result_id']}.json"
        self.assertEqual(maintained.read_artifact(copied), complete_result)
        self.assertIn("imported-g001-Z-h01", maintained.state["research_branches"])
        fixed = maintained.system(self.baseline).to_data()
        fixed["rules"][1]["action"]["call"] = "P"
        candidate = validate_system(fixed)
        register_system(directory, maintained.state, candidate)
        target = directory / "repaired.json"
        maintained.artifact(target, candidate.to_data())
        maintained.state.update(status="completed", repair_versions={
            self.baseline: {"hash": candidate.content_hash, "path": str(target), "repair": "fixture"}})
        maintained.checkpoint()
        child = prepare(maintained)
        successor = Controller(Path(child["path"]))
        self.assertEqual(successor.config["baselines"][0]["hash"], candidate.content_hash)
        self.assertEqual(successor.state["evaluated_pairs"], 0)
        self.assertEqual(successor.state["benchmark_revision"]["source_run"], directory.name)
        retained = successor.state["research_branches"]["imported-g001-Z-h01"]
        draft = successor.read_artifact(successor.directory / retained["workspace"])
        self.assertEqual(draft["cases"]["saved-board"]["board"], BOARD)
        self.assertEqual(prepare(maintained)["path"], child["path"])
        with patch("bridge_evolve.evolution.director.ModelClient") as model:
            RepairQueue(successor).process()
            model.assert_not_called()
        self.assertEqual({row["status"] for row in successor.state["repair_jobs"].values()}, {"resolved_by_revision"})
        migrated = Controller(initialize_maintenance(directory, mode="evaluation"))
        self.assertEqual(migrated.config["baselines"][0]["hash"], candidate.content_hash)

    def test_incremental_replay_preserves_cross_step_public_monotonicity(self):
        from bridge_evolve.evolution.execution import audit_module
        from bridge_evolve.systems.inputs import parse_board
        from bridge_evolve.systems.language import SystemError
        checker = audit_module().bounds_checker()
        board = parse_board(BOARD)
        first = {"seat": "N", "call": "P", "public_context": {
            "var.captain": "N", "var.captain_hcp_min": 0, "var.captain_hcp_max": 39}}
        second = {"seat": "S", "call": "P", "public_context": {
            **first["public_context"], "var.captain_hcp_max": 40}}
        with self.assertRaisesRegex(SystemError, "upper bound increased"):
            checker([first, second], board)
        previous = {}
        self.assertEqual(checker([first], board, previous=previous), 1)
        with self.assertRaisesRegex(SystemError, "upper bound increased"):
            checker([second], board, previous=previous)
        self.assertEqual(checker([second], board), 1)

    def test_engine_migration_retains_isolated_edits_original_reproducer_and_complete_results(self):
        from bridge_evolve.evolution.engine_maintenance import EngineWorkspace
        from bridge_evolve.evolution.handoff import initialize_maintenance
        for name in ("src", "tests", "tools"):
            (self.project / name).mkdir()
        (self.project / "src/recoverable_engine.py").write_text("def answer():\n    return 1\n")
        (self.project / "pyproject.toml").write_text("[project]\nname='recovery-fixture'\nversion='0.0.0'\n")
        record_incident(self.controller, {**BOARD, "tables": [], "audit_result": {"errors": [
            {"stage": "execution", "message": "Recoverable engineering fixture", "type": "RuntimeError"}]}},
            self.parent, self.baseline, "engineering-recovery")
        job = next(iter(self.controller.state["repair_jobs"].values()))
        with patch("bridge_evolve.evolution.engine_maintenance.ROOT", self.project):
            workspace = EngineWorkspace(self.controller, job)
        edit = {"path": "src/recoverable_engine.py", "old": "return 1", "new": "return 2"}
        result = workspace.execute("edit_engine_file", edit, "saved-edit")
        self.assertTrue(result["ok"], result)
        regression = ("import unittest\nfrom recoverable_engine import answer\n"
                      "class RecoveryTest(unittest.TestCase):\n"
                      "    def test_answer(self):\n        self.assertEqual(answer(), 2)\n")
        workspace.execute("edit_engine_file", {"path": "tests/test_recoverable_engine.py",
            "old": "", "new": regression}, "saved-regression")
        complete = {"details": "complete-result-" * 1000}
        reference = workspace.feedback(complete, limit=1000)["details_ref"]
        self.controller.state["status"] = "blocked"
        job.update(iteration=2, review={"approve": False, "reason": "保留测试并继续修订"})
        self.controller.checkpoint()
        directory = initialize_maintenance(self.directory, output=self.project / "engine-continuation")
        successor = Controller(directory)
        retained = successor.state["repair_jobs"][job["id"]]
        resumed = EngineWorkspace(successor, retained)
        self.assertEqual(resumed.data["operations"], workspace.data["operations"])
        self.assertEqual(retained["review"], job["review"])
        self.assertEqual(retained["iteration"], 2)
        self.assertEqual(resumed.execute("edit_engine_file", edit, "saved-edit"), result)
        self.assertIn("return 2", (resumed.root / edit["path"]).read_text())
        self.assertIn("return 1", (resumed.original / edit["path"]).read_text())
        self.assertEqual(successor.read_artifact(resumed.directory / f"result-{reference['result_id']}.json"), complete)
        selector = "test_recoverable_engine.RecoveryTest.test_answer"
        self.assertEqual(resumed.run_tests([selector])["returncode"], 0)
        self.assertNotEqual(resumed.run_tests([selector], original=True)["returncode"], 0)
        resumed.execute("edit_engine_file", {**edit, "old": "return 2", "new": "return 3"}, "new-edit")
        self.assertIn("return 2", (workspace.root / edit["path"]).read_text())

    def test_parallel_repair_regressions_preserve_full_natural_results_and_order(self):
        from bridge_evolve.evolution.maintenance import regression_comparisons
        original = self.controller.system(self.baseline)
        raw = original.to_data()
        raw["rules"][1]["action"]["call"] = "P"
        revised = validate_system(raw)
        peer = self.controller.system(self.parent)
        layouts = {"NS": ({"NS": original, "EW": peer}, {"NS": revised, "EW": peer}),
                   "EW": ({"NS": peer, "EW": original}, {"NS": peer, "EW": revised})}
        boards = [{"index": 0, "board": BOARD}, {"index": 1, "board": {**BOARD, "dealer": "E"}}]
        serial = list(regression_comparisons(layouts, boards, workers=1, stop=lambda: None))
        parallel = list(regression_comparisons(layouts, boards, workers=2, stop=lambda: None))
        self.assertEqual(parallel, serial)
        self.assertEqual(len(parallel), 4)
        self.assertTrue(all(before["status"] == "failed" and after["status"] == "passed"
                            for _, _, before, after in parallel))

        def stopped():
            raise EvolutionStopped("Explicit cancellation")

        with self.assertRaises(EvolutionStopped):
            list(regression_comparisons(layouts, boards, workers=2, stop=stopped))

    def test_all_incident_tables_run_in_parallel_without_losing_versions_or_original_boards(self):
        from bridge_evolve.evolution.maintenance import RepairTools
        job, record = self.incident()
        second = copy.deepcopy(record)
        second["dealer"] = "E"
        second["tables"].append({**copy.deepcopy(second["tables"][0]), "id": "table2"})
        second["tables"][1]["systems"] = {
            "NS": {"hash": self.baseline}, "EW": {"hash": self.parent}}
        record_incident(self.controller, second, self.parent, self.baseline, "second-counterexample")
        workspace = RepairTools(self.controller, job)
        workspace.data["system"]["rules"][1]["action"]["call"] = "P"
        with patch.object(workspace, "system", wraps=workspace.system) as compile_system:
            serial = list(workspace.replay_evidence())
            compile_system.assert_called_once_with()
        with patch.dict(self.controller.config, workers=2):
            parallel = list(workspace.replay_evidence())
        self.assertEqual(serial, parallel)
        self.assertEqual(len(parallel), 3)
        self.assertEqual([row[2] for row in parallel], ["table1", "table1", "table2"])
        self.assertTrue(all(row[-1]["status"] == "passed" for row in parallel))
        self.assertEqual(job["validation_progress"], {
            "phase": "incidents", "completed_tables": 3, "total_tables": 3, "workers": 2})

    def test_framework_maintainer_delivers_isolated_code_that_fails_before_and_passes_after(self):
        from bridge_evolve.evolution.handoff import prepare, remaining_config, run_engine_handoff
        config = remaining_config(self.directory, mode="evaluation")
        baselines = []
        for name in ("engine-anchor-A", "engine-anchor-B"):
            raw = blank_system().to_data()
            raw["name"] = name
            path = self.project / f"{name}.json"
            path.write_text(json.dumps(raw))
            baselines.append(str(path))
        config.update(baselines=baselines, resamples=100,
            benchmark_revision_reason="Standalone engine test uses valid opponents; the invalid source fixture is retained.",
            boards={name: 2 for name in ("diagnosis", "validity", "screen", "full", "promotion")})
        self.directory = initialize(config, self.project / "engine-run", self.project)
        self.controller = Controller(self.directory)
        self.baseline = self.controller.config["baselines"][0]["hash"]
        self.parent = self.controller.state["champions"]["Z"]
        candidate = blank_system().to_data()
        candidate["rules"] = [{"id": "handoff.open", "priority": 1,
            "when": {"op": "not", "args": [{"op": "has_contract_bid"}]}, "action": {"kind": "call", "call": "1C"}}]
        tools = ResearchTools(self.controller, "g001-Z", self.parent,
            evidence_packet(self.controller.system(self.parent), [], ["*"]), candidate, None)
        case = tools.execute("case", "add_cases", {"cases": [{"name": "handoff", "board": BOARD}]})
        tools.execute("tests", "write_tests", {"tests": [{"name": "real-opening",
            "case_id": case["cases"][0]["case_id"], "mode": "decision", "changed_from_parent": True,
            "assertions": [{"path": "/call", "op": "eq", "value": "1C"}]}]})
        published = tools.execute("publish", "publish_candidate", {"hypothesis": "Exercise the repaired engine's actual evaluation queue."})
        self.assertTrue(published["ok"], published)
        shutil.copytree(ROOT / "src", self.project / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.project / "tests").mkdir()
        (self.project / "tools").mkdir()
        for name in ("board_viewer.py", "verify_classics.py"):
            shutil.copyfile(ROOT / "tools" / name, self.project / "tools" / name)
        from bridge_evolve.evolution.runtime import asset_hashes
        for name in asset_hashes(ROOT):
            target = self.project / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        (self.project / "pyproject.toml").write_text("[project]\nname='maintenance-fixture'\nversion='0.0.0'\n")
        source = self.project / "src" / "bridge_evolve" / "maintenance_fixture.py"
        source.write_text("def quota(limit):\n    return limit - 1\n")
        record_incident(self.controller, {**BOARD, "tables": [], "audit_result": {"errors": [
            {"stage": "execution", "message": "Quota consumes one extra request.", "type": "RuntimeError"}]}},
            self.parent, self.baseline, "engine-counterexample")
        job = next(iter(self.controller.state["repair_jobs"].values()))
        regression = ("import unittest\nfrom bridge_evolve.maintenance_fixture import quota\n"
                      "class RepairTest(unittest.TestCase):\n"
                      "    def test_exact_quota(self):\n"
                      "        self.assertEqual(quota(0), 0)\n"
                      "        self.assertEqual(quota(3), 3)\n")
        selector = "test_maintenance_fixture.RepairTest.test_exact_quota"
        with patch("bridge_evolve.evolution.engine_maintenance.ROOT", self.project), patch(
                "bridge_evolve.evolution.director.ModelClient") as model:
            model.return_value.request.side_effect = [
                response(("read", "read_engine_file", {"path": "src/bridge_evolve/maintenance_fixture.py"})),
                response(("edit", "edit_engine_file", {"path": "src/bridge_evolve/maintenance_fixture.py",
                    "old": "return limit - 1", "new": "return limit"})),
                response(("regression", "edit_engine_file", {"path": "tests/test_maintenance_fixture.py",
                    "old": "", "new": regression})),
                response(("run", "run_engine_tests", {"selectors": [selector]})),
                response(("deliver", "submit_engine_revision", {"diagnosis": "Off-by-one in request quota",
                    "summary": "Correct exact quota, including zero", "reproduction_test": selector})),
                response(("review", "review_engine_revision", {"approve": True, "reason": "Original reproducer fails; fixed code passes."}))]
            RepairQueue(self.controller).process()
        self.assertEqual(job["status"], "accepted_engine_revision", job.get("repair_error"))
        self.assertIn("return limit - 1", source.read_text())
        self.assertIn("return limit\n", (Path(job["engine_path"]) / "src/bridge_evolve/maintenance_fixture.py").read_text())
        saved = self.controller.read_artifact(self.directory / job["workspace"])
        self.assertNotEqual(saved["original_reproduction"]["returncode"], 0)
        self.assertEqual(saved["tests"][-1]["returncode"], 0)
        self.controller.state["status"] = "completed"
        self.controller.checkpoint()
        handoff = prepare(self.controller)
        with patch.dict("os.environ", {"BRIDGE_DDS_LIBRARY": os.environ.get(
                "BRIDGE_DDS_LIBRARY", str(ROOT / ".runtime/dds/libdds.so"))}):
            result = run_engine_handoff(handoff)
        self.assertEqual(result["id"], handoff["id"])
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["engine_hash"], self.controller.state["repair_engine"]["hash"])
        self.assertEqual(result["agent_requests"], 0)
        self.assertGreater(result["evaluated_pairs"], 0)


class DirectedWorkflowHTTPTests(unittest.TestCase):
    def test_isolated_worker_evidence_and_verified_repair_export_have_real_http_routes(self):
        from test_board_viewer import viewer
        from concurrent.futures import Future
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "systems").mkdir()
            paths = []
            for name in ("general-v2", "pass-v2"):
                raw = blank_system().to_data()
                raw["name"] = name
                path = project / "systems" / f"{name}.json"
                path.write_text(json.dumps(raw))
                paths.append(str(path))
            directory = initialize({"profile": "smoke", "source": "research", "origins": ["blank"],
                "baselines": paths, "research_architecture": "director", "max_seconds": 300,
                "selection_policy": "parent-improvement", "automatic_goal_check": False, "growth_boards": 2}, project=project)
            controller = Controller(directory)
            controller.state["test_only"] = True
            parent = controller.state["champions"]["Z"]
            with patch("bridge_evolve.evolution.research.ModelClient") as model:
                model.return_value.request.return_value = response(("done", "finish_research", {"summary": "HTTP fixture, no real model."}))
                ResearchSession(controller, 1, "Z", parent, evidence_packet(controller.system(parent), [], ["*"]),
                    assignment={**IDEA, "job_id": "g001-Z-h01", "turn_budget": 10}, identity="g001-Z-h01").run()
            repair_id = "repair-" + "a" * 20
            path = directory / "repairs" / repair_id / "candidate.json"
            path.parent.mkdir(parents=True)
            controller.artifact(path, controller.system(parent).to_data())
            controller.state["repair_jobs"] = {repair_id: {"id": repair_id, "status": "accepted_for_next_run",
                "kind": "baseline-completion", "candidate_path": path.relative_to(directory).as_posix(),
                "error": {"message": "HTTP fixture"}, "evidence": [], "next_action": "新冻结实验复测",
                "validation": {"status": "passed", "regression_tables": 512, "original_valid_tables": 412,
                    "new_failures": [], "pre_existing_failures": [
                        {"after_status": "failed" if index < 3 else "passed", "details": "x" * 1000}
                        for index in range(100)]}}}
            controller.checkpoint()
            state = viewer.ViewerState(project)
            self.addCleanup(state.executor.shutdown)
            server = viewer.ViewerServer(("127.0.0.1", 0), state)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def get(path):
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
                client.request("GET", path)
                response = client.getresponse()
                result = response.status, response.read()
                client.close()
                return result

            try:
                base = "/api/evolutions/" + directory.name
                code, body = get(base + "/research/g001-Z-h01")
                self.assertEqual(code, 200, body)
                self.assertEqual(json.loads(body)["role"], "implementer")
                code, body = get(base + "/research/g001-Z-h01/evidence?pointer=%2Fassignment")
                self.assertEqual(code, 200, body)
                self.assertEqual(json.loads(body)["job_id"], "g001-Z-h01")
                code, body = get(base + "/repairs/" + repair_id + "/candidate")
                self.assertEqual(code, 200, body)
                self.assertEqual(json.loads(body)["rules"], [])
                code, body = get(base + "/repairs/" + repair_id)
                self.assertEqual(code, 200, body)
                self.assertEqual(json.loads(body)["next_action"], "新冻结实验复测")
                self.assertEqual(len(json.loads(body)["validation"]["pre_existing_failures"]), 100)
                code, body = get(base)
                self.assertEqual(code, 200, body)
                validation = json.loads(body)["workflow"]["repairs"][0]["validation"]
                self.assertEqual(validation["regression_tables"], 512)
                self.assertEqual(validation["new_failures_count"], 0)
                self.assertEqual(validation["pre_existing_failures_count"], 100)
                self.assertEqual(validation["remaining_pre_existing_failures_count"], 3)
                controller.state["repair_jobs"][repair_id]["status"] = "rejected"
                controller.checkpoint()
                self.assertEqual(get(base + "/repairs/" + repair_id + "/candidate")[0], 422)
                future = Future()
                a, b = list(state.systems)[:2]
                with patch.object(state.executor, "submit", return_value=future):
                    created = state.start_evolution({"source": "research", "profile": "smoke",
                        "system_a": a, "system_b": b, "origins": ["blank"], "research_architecture": "director",
                        "hypotheses_per_unit": 2, "repair_attempts": 1, "champion_seed_run": directory.name,
                        "research_budget_mode": "campaign", "max_trial_boards": 4096, "max_promotions": 200,
                        "maintenance_budget": {"max_requests": 100, "max_tokens": 2000000, "max_seconds": 600}})
                self.assertEqual(created["workflow"]["architecture"], "director")
                self.assertEqual(created["workflow"]["hypotheses_per_unit"], 2)
                self.assertEqual(created["selection_policy"], "parent-improvement")
                saved_config = json.loads((state.evolution_directory(created["id"]) / "config.json").read_text())
                self.assertEqual(saved_config["growth_boards"], 2)
                self.assertFalse(saved_config["automatic_goal_check"])
                self.assertEqual(created["workflow"]["budget_mode"], "campaign")
                self.assertEqual(saved_config["max_trial_boards"], 4096)
                self.assertEqual(saved_config["max_promotions"], 200)
                self.assertEqual(created["budgets"]["maintenance_budget"]["max_tokens"], 2000000)
                self.assertEqual(created["resources"]["budget_pools"]["research"]["requests"], 0)
                future.cancel()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
