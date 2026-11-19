import copy
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import test_expanded_research as expanded
from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evolution.config import normalize_config
from bridge_evolve.evolution.controller import Controller, frozen_workspace, initialize
from bridge_evolve.evolution.director import DirectedResearch, StructuredRequestLimit, director_evidence, structured_request
from bridge_evolve.evolution.execution import EvolutionStopped
from bridge_evolve.evolution.inspection import overview, research_evidence_export
from bridge_evolve.evolution.manager import CampaignManager, DECISION, DeliveryBlocked, import_management
from bridge_evolve.evolution.memory import working_set
from bridge_evolve.evolution.models import ModelRequestError, request_body, token_reservation
from bridge_evolve.evolution.proposals import evidence_packet
from bridge_evolve.evolution.research import ResearchSession
from bridge_evolve.evolution.research_tools import ResearchTools
from bridge_evolve.evolution.repairs import RepairQueue, import_incidents, record_reference_incident
from bridge_evolve.evolution.sampling import ReferenceAuctionError
from bridge_evolve.systems.inputs import parse_board
from bridge_evolve.systems.language import SystemError
from test_directed_research import IDEA
from test_research import BOARD, response


class CampaignManagerTests(unittest.TestCase):
    system = expanded.ExpandedResearchTests.system

    def setUp(self):
        expanded.ExpandedResearchTests.setUp(self)
        self.config.update(campaign_manager={"turns": 1, "seconds": 60, "max_missed_deliveries": 2},
                           repair_attempts=0, hypotheses_per_unit=1, generations=3)
        self.directory = initialize(self.config, self.project / "managed", self.project)
        self.controller = Controller(self.directory)
        self.parent = self.controller.state["champions"]["Z"]
        self.packet = evidence_packet(self.controller.system(self.parent), [], ["*"])
        self.manager = CampaignManager(self.controller)
        self.job = {"id": "g001-Z-h01", "idea": IDEA}
        self.controller.state["generations"]["1"] = {
            "status": "running", "parents": {"Z": self.parent}, "candidates": {}, "promotions": {}}
        self.controller.checkpoint()

    def worker(self):
        return ResearchSession(self.controller, 1, "Z", self.parent, self.packet, identity=self.job["id"],
            assignment={**IDEA, "job_id": self.job["id"], "turn_budget": 40, "seconds_budget": 200})

    def decision(self, action="continue", deliverable="draft", tasks=(), updates=()):
        value = {
            "summary": "Evidence determines delivery.", "decision": action,
            "instruction": "Complete the specified public-stage slice and supply real evidence.",
            "deliverable": deliverable, "tasks": list(tasks), "remaining_gaps": ["Missing interference exit."]}
        if updates:
            value["task_updates"] = list(updates)
        return response(("management", DECISION["name"], value))

    def stage(self):
        return {"stage": "implementing", "verified": "saved IR", "blocker": "", "next_action": "test the slice",
                "stage_design": "Empty auction: all hands -> 1C; partner continuation and exit require tests."}

    def test_deadline_saves_all_pending_outputs_and_resumes_without_reexecuting_edits(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.side_effect = [self.decision(), self.decision(deliverable="tests")]
            self.assertTrue(self.manager.prepare(session, self.job))
            with patch.object(session.client, "request") as request:
                request.return_value = response(
                    ("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.system()}]}),
                    ("stage", "update_implementation_state", self.stage()))
                session.run()
                request.assert_called_once()
            self.assertEqual(session.data["status"], "awaiting_management")
            self.assertFalse(session.tools.data["finished"])
            self.assertEqual(len(session.tools.data["operations"]), 2)
            self.assertTrue(all(row["status"] == "completed" for row in session.tools.data["operations"].values()))
            self.assertTrue(self.manager.review(session, self.job))
        branch = self.manager.branch(session.identity)
        row = self.manager.data["reviews"][branch["reviews"][-1]]
        self.assertTrue(row["receipt"]["met"])
        snapshot = json.loads(research_evidence_export(self.directory, session.identity,
            pointer="/management/checkpoints/" + row["id"]))
        self.assertEqual(snapshot["system"], self.system())
        self.controller = Controller(self.directory)
        session = self.worker()
        with patch.object(session.client, "request") as request:
            request.return_value = response(("note", "research_note", {"text": "New question."}))
            session.run()
            request.assert_called_once()
        self.assertEqual(session.data["turns"], 2)
        self.assertEqual(len(session.tools.data["operations"]), 3)
        self.assertEqual(sum(row["name"] == "edit_system" for row in session.tools.data["operations"].values()), 1)
        session.compact("Retain the active management contract.")
        self.assertEqual(json.loads(session.data["messages"][1]["content"])["management_contract"]["deliverable"], "tests")

    def test_completed_worker_is_reviewed_without_faking_delivery_or_rebilling_on_replay(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.side_effect = [self.decision(deliverable="tests"),
                                                         self.decision("stop", "tests")]
            self.manager.prepare(session, self.job)
            session.tools.execute("finish", "finish_research", {"summary": "Claimed done without actual tests."})
            session.save()
            self.manager.finish(session, self.job)
            self.manager.finish(session, self.job)
            self.assertEqual(director.return_value.request.call_count, 2)
        self.assertEqual(self.manager.branch(session.identity)["status"], "needs_followup")
        self.assertFalse(self.manager.branch(session.identity)["last_receipt"]["met"])
        self.assertEqual(len(session.tools.data["releases"]), 0)

    def test_interrupted_contract_application_reuses_deadline_and_unstarted_tasks_return_to_queue(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision()
            self.manager.prepare(session, self.job)
        branch = self.manager.branch(session.identity)
        row = self.manager.data["reviews"][branch["reviews"][-1]]
        deadline = branch["contract"]["deadline_at"]
        row["applied"] = False
        branch["pending_review"] = row["id"]
        self.manager.data["tasks"]["unstarted"] = {
            "id": "unstarted", "status": "assigned", "unit": "g001-Z", "session": "g001-Z-h02",
            "priority": 50, "lineage": "Z", "idea": IDEA}
        self.controller.checkpoint()
        self.controller = Controller(self.directory)
        self.manager = CampaignManager(self.controller)
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            self.manager.prepare(session, self.job)
            director.return_value.request.assert_not_called()
        self.assertEqual(self.manager.branch(session.identity)["contract"]["deadline_at"], deadline)
        self.manager.return_unstarted([{"management_task": "unstarted", "status": "cancelled"}])
        self.assertEqual(self.manager.data["tasks"]["unstarted"]["status"], "pending")
        self.assertNotIn("session", self.manager.data["tasks"]["unstarted"])

    def test_elapsed_time_deadline_survives_restart_without_another_worker_request(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision()
            self.manager.prepare(session, self.job)
        deadline = "2000-01-01T00:00:00+00:00"
        self.manager.branch(session.identity)["contract"]["deadline_at"] = deadline
        self.controller.checkpoint()
        self.controller = Controller(self.directory)
        session = self.worker()
        with patch("bridge_evolve.evolution.research.ModelClient") as worker:
            session.run()
            worker.return_value.request.assert_not_called()
        self.assertEqual(session.data["turns"], 0)
        self.assertEqual(session.data["status"], "awaiting_management")
        self.assertEqual(self.controller.state["management"]["branches"][session.identity]["contract"]["deadline_at"], deadline)

    def test_old_passed_tests_and_tool_ok_do_not_forge_current_validation(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision(deliverable="tests")
            self.manager.prepare(session, self.job)
        tools = session.tools
        goal = next(iter(self.manager.data["deliveries"].values()))
        self.manager.data["tasks"]["after-tests"] = {
            "id": "after-tests", "lineage": "Z", "priority": 50, "idea": IDEA,
            "delivery_id": goal["id"], "status": "pending", "requires": ["tests"]}
        self.manager.reconcile()
        self.assertEqual(self.manager.data["tasks"]["after-tests"]["status"], "waiting_evidence")
        tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.system()}]})
        added = tools.execute("case", "add_cases", {"cases": [{"name": "natural", "board": BOARD}]})
        case_id = added["cases"][0]["case_id"]
        tools.execute("tests", "write_tests", {"tests": [{
            "name": "opening", "case_id": case_id, "mode": "auction",
            "assertions": [{"path": "/auction/0/call", "op": "eq", "value": "1C"}]}]})
        tools.execute("run", "run_tests", {})
        contract = self.manager.branch(session.identity)["contract"]
        receipt = self.manager.receipt(session, contract)
        self.assertTrue(receipt["deliveries"]["tests"])
        self.manager.reconcile()
        self.assertEqual(self.manager.data["tasks"]["after-tests"]["status"], "pending")
        baseline = {**contract, **{key: receipt[key] for key in
                    ("operations", "document_hash", "tests_hash", "tests_current")}}
        tools.execute("repeat", "run_tests", {})
        self.assertFalse(self.manager.receipt(session, baseline)["deliveries"]["tests"])
        tools.execute("change", "edit_system", {"edits": [
            {"op": "replace", "path": "/rules/0/action/call", "value": "1D"}]})
        self.manager.reconcile()
        self.assertEqual(self.manager.data["tasks"]["after-tests"]["status"], "waiting_evidence")
        self.assertFalse(self.manager.receipt(session, baseline)["tests_current"])
        result = tools.execute("failure", "run_tests", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["failed"], 1)
        receipt = self.manager.receipt(session, baseline)
        self.assertFalse(receipt["deliveries"]["tests"])
        self.assertTrue(receipt["deliveries"]["counterexample"])

    def test_repeated_misses_forbid_extensions_and_idempotently_dispatch_real_followup(self):
        session = self.worker()
        followup = {"lineage": "Z", "priority": 95,
                    "idea": {**IDEA, "title": "Small missing exit", "continue_from": session.identity}}
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.side_effect = [
                self.decision(deliverable="tests"),
                self.decision(deliverable="tests"),
                self.decision("redirect", "tests"),
                self.decision("continue", "tests"),
                self.decision("pause", "tests", [followup])]
            self.assertTrue(self.manager.prepare(session, self.job))
            session.tools.execute("note1", "research_note", {"text": "No measured result."})
            self.assertTrue(self.manager.review(session, self.job))
            session.tools.execute("note2", "research_note", {"text": "Still no measured result."})
            self.assertFalse(self.manager.review(session, self.job))
            self.assertEqual(director.return_value.request.call_count, 5)
        branch = self.manager.branch(session.identity)
        self.assertEqual(branch["missed_deliveries"], 2)
        self.assertEqual(session.data["status"], "managed_pause")
        self.assertFalse(session.tools.data["finished"])
        row = self.manager.data["reviews"][branch["reviews"][-1]]
        self.assertEqual(row["allowed_decisions"], ["pause", "stop"])
        notices = len(session.data["management_notices"])
        row["applied"] = False
        branch["pending_review"] = row["id"]
        self.controller.checkpoint()
        self.controller = Controller(self.directory)
        self.manager = CampaignManager(self.controller)
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            self.assertFalse(self.manager.prepare(session, self.job))
            director.return_value.request.assert_not_called()
        self.assertEqual(len(session.data["management_notices"]), notices)
        self.assertEqual(len(self.manager.data["tasks"]), 1)
        claimed = self.manager.claim("Z", "g002-Z")
        self.assertEqual(claimed[0]["idea"]["continue_from"], session.identity)
        self.assertEqual(self.manager.claim("Z", "g002-Z"), claimed)
        self.assertEqual(self.manager.claim("Z", "g003-Z"), [])
        view = overview(self.directory, details=True)["workflow"]["management"]
        self.assertEqual(view["tasks"][0]["status"], "assigned")
        self.assertIn("Missing interference exit.", view["remaining_gaps"])

    def test_directed_worker_yields_to_maintenance_and_global_queued_job_is_actually_started(self):
        expanded.ExpandedResearchTests.evaluator(self)
        queued = {"lineage": "Z", "priority": 90, "idea": {**IDEA, "title": "Queued missing slice"},
                  "id": "queued-test", "status": "pending", "created_at": "2026-01-01T00:00:00+00:00"}
        self.manager.data["tasks"][queued["id"]] = queued
        self.controller.checkpoint()
        with patch("bridge_evolve.evolution.director.ModelClient") as director, \
             patch("bridge_evolve.evolution.research.ModelClient") as worker, \
             patch.object(self.controller, "process_repairs") as repairs, \
             patch.object(self.controller, "process_evaluations") as evaluations:
            director.return_value.request.side_effect = [
                self.decision(deliverable="tests"), self.decision("pause", "tests"),
                response(("review", "review_implementation", {"decision": "continue", "learning": "No fabricated delivery."}))]
            worker.return_value.request.return_value = response(("note", "research_note", {"text": "No delivery yet."}))
            DirectedResearch(self.controller, 1, "Z", self.parent, self.packet).run()
        self.assertEqual(worker.return_value.request.call_count, 1)
        self.assertGreaterEqual(repairs.call_count, 2)
        evaluations.assert_called_once()
        labels = [call.kwargs["label"] for call in director.return_value.request.call_args_list]
        self.assertTrue(all(":manager:" in label for label in labels[:2]), labels)
        self.assertTrue(labels[-1].endswith(":director:review:1"), labels)
        self.assertFalse(any(label.endswith(":plan") for label in labels), labels)
        task = self.manager.data["tasks"][queued["id"]]
        self.assertEqual(task["session"], "g001-Z-h01")
        self.assertEqual(task["status"], "paused")
        unit = self.controller.state["research_units"]["g001-Z"]
        self.assertEqual(unit["jobs"][0]["idea"]["title"], queued["idea"]["title"])
        self.assertEqual(unit["jobs"][0]["report"]["worker_status"], "managed_pause")
        self.assertEqual(unit["jobs"][0]["report"]["releases"], [])

    def test_config_and_continuation_preserve_real_pending_work_not_old_deadlines(self):
        for limits in ({"turns": 0, "seconds": 60, "max_missed_deliveries": 2},
                       {"turns": True, "seconds": 60, "max_missed_deliveries": 2}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                normalize_config({**self.config, "campaign_manager": limits}, self.project)
        with self.assertRaises(ValueError):
            normalize_config({**self.config, "research_budget_mode": "episode"}, self.project)
        source = copy.deepcopy(self.controller.state)
        source["research_sessions"]["g001-Z-h01"] = {"releases": 0}
        source["management"]["tasks"]["pending"] = {
            "id": "pending", "status": "assigned", "lineage": "Z", "priority": 99,
            "session": "g001-Z-h01", "idea": IDEA}
        target = {"research_branches": {"imported-g001-Z-h01": {}}}
        import_management(source, target)
        self.assertEqual(target["management"]["tasks"]["pending"]["status"], "pending")
        self.assertEqual(target["management"]["tasks"]["pending"]["idea"]["continue_from"], "imported-g001-Z-h01")
        self.assertEqual(target["management"]["branches"], {})

    def test_manager_gateway_failure_preserves_pending_review_instead_of_finishing_the_worker(self):
        expanded.ExpandedResearchTests.evaluator(self)
        self.manager.data["tasks"]["queued"] = {"id": "queued", "status": "pending", "priority": 90,
                                                "lineage": "Z", "idea": IDEA}
        with patch("bridge_evolve.evolution.director.ModelClient") as director, \
             patch("bridge_evolve.evolution.research.ModelClient") as worker:
            director.return_value.request.side_effect = [self.decision(deliverable="tests"),
                                                         ModelRequestError("manager transport failed")]
            worker.return_value.request.return_value = response(("note", "research_note", {"text": "No result yet."}))
            with self.assertRaisesRegex(ModelRequestError, "manager transport failed"):
                DirectedResearch(self.controller, 1, "Z", self.parent, self.packet).run()
        metadata = self.controller.state["research_sessions"]["g001-Z-h01"]
        workspace = self.controller.read_artifact(self.directory / metadata["workspace"])
        self.assertFalse(workspace["finished"])
        self.assertEqual(metadata["status"], "awaiting_management")
        self.assertIn("pending_review", self.manager.branch("g001-Z-h01"))

    def test_invalid_saved_draft_is_evidence_not_a_query_error_retry_loop(self):
        session = self.worker()
        raw = self.system()
        raw["protocol"]["states"]["idle"]["otherwise"] = "missing"
        result = session.tools.execute("invalid", "edit_system", {
            "edits": [{"op": "replace", "path": "", "value": raw}]})
        self.assertTrue(result["ok"])
        document = canonical_hash(raw)
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.side_effect = [
                response(("read-invalid", "inspect_research_evidence", {
                    "view": "protocol", "session": session.identity, "candidate": document})),
                self.decision("redirect", "draft")]
            self.assertTrue(self.manager.prepare(session, self.job))
            self.assertEqual(director.return_value.request.call_count, 2)
            messages = director.return_value.request.call_args.args[0]
            output = json.loads(next(item["output"] for item in messages
                                     if item.get("type") == "function_call_output"))
            self.assertEqual(output["status"], "invalid_draft")
            self.assertIsNone(output["evidence_source"]["system_hash"])
            self.assertEqual(output["compile_error"]["pointer"], "/protocol/states/idle/otherwise")
        self.assertFalse(self.controller.state.get("maintenance_events"))
        with self.assertRaisesRegex(ValueError, "saved session draft"):
            director_evidence(self.controller, {"view": "protocol", "session": session.identity, "candidate": "0" * 64})

    def test_manager_itself_cannot_poll_forever_or_disguise_its_timeout_as_delivery(self):
        session = self.worker()
        counter = iter(range(10))

        def polling(*args, **kwargs):
            return response(("query-" + str(next(counter)), "inspect_research_evidence", {"view": "portfolio"}))

        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.side_effect = polling
            self.assertFalse(self.manager.prepare(session, self.job))
            self.assertEqual(director.return_value.request.call_count, 6)
        row = next(iter(self.manager.data["reviews"].values()))
        self.assertEqual(row["decision_source"], "host_review_limit")
        self.assertIn("6-request", row["error"])
        self.assertFalse(row["receipt"]["met"])
        self.assertEqual(session.data["status"], "managed_pause")
        self.assertEqual(session.tools.data["releases"], [])
        path = self.directory / "expired-manager.json"
        self.controller.artifact(path.with_suffix(".conversation.json"), {
            "step": 0, "recovering": False, "started_at": "2000-01-01T00:00:00+00:00",
            "messages": [{"role": "system", "content": "Manage."}, {"role": "user", "content": "Decide."}]})
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            with self.assertRaisesRegex(StructuredRequestLimit, "1-second"):
                structured_request(self.controller, path, "Manage.", {}, DECISION, "campaign:manager:expired",
                                   max_steps=6, max_seconds=1)
            director.return_value.request.assert_not_called()

    def test_continuation_does_not_overwrite_a_retained_branch_with_reused_session_id(self):
        session = self.worker()
        session.tools.execute("edit", "edit_system", {"edits": [
            {"op": "replace", "path": "", "value": self.system("1C")}]})
        session.save()
        retained = ResearchTools(self.controller, "retained-old", self.parent, self.packet, self.system("1D"), None)
        old_id = "imported-" + session.identity
        self.controller.state["research_branches"] = {old_id: {
            "lineage": "Z", "parent": self.parent,
            "workspace": retained.path.relative_to(self.directory).as_posix()}}
        self.manager.data["tasks"]["continuing"] = {"id": "continuing", "status": "pending", "priority": 90,
            "lineage": "Z", "idea": {**IDEA, "continue_from": session.identity}}
        self.controller.state["status"] = "cancelled"
        self.controller.checkpoint()
        target = initialize({**self.config, "champion_seed_run": str(self.directory)},
                            self.project / "collision-handoff", self.project)
        resumed = Controller(target)
        mapping = resumed.state["research_branch_imports"][self.controller.state["id"]]
        current_id = mapping[session.identity]
        self.assertNotEqual(current_id, old_id)
        self.assertEqual(resumed.state["research_branches"][old_id]["hash"], canonical_hash(self.system("1D")))
        self.assertEqual(resumed.state["research_branches"][current_id]["hash"], canonical_hash(self.system("1C")))
        self.assertEqual(resumed.state["management"]["tasks"]["continuing"]["idea"]["continue_from"], current_id)

    def test_review_clock_survives_first_request_failure_and_expired_cached_decision_is_still_used(self):
        path = self.directory / "interrupted-review.json"
        arguments = (path, "Manage.", {}, DECISION, "campaign:manager:interrupted")
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.side_effect = ModelRequestError("Interrupted first request.")
            with self.assertRaises(ModelRequestError):
                structured_request(self.controller, *arguments, max_steps=6, max_seconds=1)
        saved = self.controller.read_artifact(path.with_suffix(".conversation.json"))
        self.assertIsNotNone(saved)
        later = datetime.fromisoformat(saved["started_at"]) + timedelta(seconds=5)
        with (patch("bridge_evolve.evolution.director.datetime") as clock,
              patch("bridge_evolve.evolution.director.timestamp", return_value=later.isoformat()),
              patch("bridge_evolve.evolution.director.ModelClient") as director):
            clock.now.return_value = later
            clock.fromisoformat.side_effect = datetime.fromisoformat
            with self.assertRaises(StructuredRequestLimit):
                structured_request(Controller(self.directory), *arguments, max_steps=6, max_seconds=1)
            director.return_value.request.assert_not_called()
            self.controller.artifact(path, self.decision())
            result = structured_request(Controller(self.directory), *arguments, max_steps=6, max_seconds=1)
            self.assertEqual(result["decision"], "continue")
            director.return_value.request.assert_not_called()

    def test_new_management_task_cannot_be_hidden_by_an_inherited_review_id(self):
        session = self.worker()
        legacy_id = session.identity + "-r0001-task-01"
        old_idea = {**IDEA, "title": "Inherited broad stage"}
        self.manager.data["tasks"][legacy_id] = {
            "id": legacy_id, "status": "pending", "priority": 50, "lineage": "Z", "idea": old_idea,
            "fingerprint": canonical_hash({"lineage": "Z", "idea": old_idea})}
        task = {"lineage": "Z", "priority": 100, "idea": {**IDEA, "title": "New smaller delivery"}}
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision(tasks=[task])
            self.manager.prepare(session, self.job)
            self.manager.prepare(session, self.job)
            director.return_value.request.assert_called_once()
        self.assertEqual(len(self.manager.data["tasks"]), 2)
        self.assertEqual(self.manager.data["tasks"][legacy_id]["idea"], old_idea)
        new_id = self.controller.state["id"] + "-" + legacy_id
        self.assertEqual(self.manager.data["tasks"][new_id]["idea"], task["idea"])
        self.assertEqual(self.manager.claim("Z", "g002-Z")[0]["id"], new_id)

    def test_continuation_recovers_legacy_collision_from_saved_decision_not_a_new_model_claim(self):
        source = copy.deepcopy(self.controller.state)
        session = "g001-Z-h01"
        review_id = session + "-r0001"
        legacy_id = review_id + "-task-01"
        old_idea = {**IDEA, "title": "Earlier imported work"}
        new_idea = {**IDEA, "title": "Actual split assignment", "continue_from": session}
        assignment = {"lineage": "Z", "priority": 100, "idea": new_idea}
        fingerprint = canonical_hash({"lineage": "Z", "idea": new_idea})
        source["research_sessions"][session] = {"releases": 0}
        source["management"]["tasks"][legacy_id] = {
            "id": legacy_id, "status": "pending", "priority": 50, "lineage": "Z", "idea": old_idea,
            "fingerprint": canonical_hash({"lineage": "Z", "idea": old_idea}), "historical_task": legacy_id}
        source["management"]["reviews"][review_id] = {
            "applied": True, "session": session, "created_at": "2026-01-01T00:00:00+00:00",
            "decision": {"tasks": [assignment]}}
        target = {"research_branches": {"imported-" + session: {}}}
        import_management(source, target)
        recovered_id = source["id"] + "-" + legacy_id
        recovered = target["management"]["tasks"][recovered_id]
        self.assertEqual(recovered["idea"]["title"], new_idea["title"])
        self.assertEqual(recovered["idea"]["continue_from"], "imported-" + session)
        self.assertEqual(recovered["recovered_from_review"], review_id)
        self.assertEqual(target["management"]["tasks"][legacy_id]["idea"], old_idea)
        self.assertEqual(len(source["management"]["tasks"]), 1)
        source["management"]["tasks"][recovered_id] = {
            **assignment, "id": recovered_id, "fingerprint": fingerprint, "status": "delivered"}
        import_management(source, target)
        self.assertNotIn(recovered_id, target["management"]["tasks"])

    def test_backlog_deferral_is_applied_and_preserved_not_just_written_in_manager_prose(self):
        session = self.worker()
        self.manager.data["tasks"]["old"] = {
            "id": "old", "lineage": "Z", "status": "pending", "priority": 100, "idea": IDEA}
        task = {"lineage": "Z", "priority": 90, "idea": {**IDEA, "title": "Small urgent prerequisite"}}
        updates = [{"task_id": "old", "status": "paused", "priority": 20,
                    "reason": "Blocked duplicate must not run ahead of its prerequisite."}]
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision(tasks=[task], updates=updates)
            self.manager.prepare(session, self.job)
            branch = self.manager.branch(session.identity)
            row = self.manager.data["reviews"][branch["reviews"][-1]]
            row["applied"] = False
            branch["pending_review"] = row["id"]
            self.manager.prepare(session, self.job)
            director.return_value.request.assert_called_once()
        self.assertEqual(self.manager.claim("Z", "g002-Z")[0]["idea"]["title"], task["idea"]["title"])
        self.assertEqual(self.manager.data["tasks"]["old"]["status"], "paused")
        self.assertEqual(self.manager.data["tasks"]["old"]["last_management_action"]["reason"], updates[0]["reason"])
        target = {"id": "next-managed-run", "research_branches": {
            session.identity: copy.deepcopy(self.controller.state["research_sessions"][session.identity])}}
        import_management(self.controller.state, target)
        self.assertEqual(target["management"]["tasks"]["old"]["status"], "paused")

    def test_paused_executor_keeps_delivery_and_claims_a_saved_work_handoff_before_new_planning(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision("pause", "tests")
            self.assertFalse(self.manager.prepare(session, self.job))
        goal = next(iter(self.manager.data["deliveries"].values()))
        claimed = self.manager.claim("Z", "g002-Z")
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["delivery_id"], goal["id"])
        self.assertEqual(claimed[0]["idea"]["continue_from"], session.identity)
        self.assertIn("Do not restart", claimed[0]["idea"]["implementation"])
        self.assertEqual(goal["owner"], "research_team")
        claimed[0]["status"] = "paused"
        goal["attempts"].extend(["second-executor", "third-executor"])
        self.manager.recover()
        self.assertEqual(goal["status"], "blocked_delivery")
        self.controller.evaluator = Mock()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            with self.assertRaises(DeliveryBlocked):
                DirectedResearch(self.controller, 3, "Z", self.parent, self.packet).run()
            director.return_value.request.assert_not_called()
        self.assertEqual(goal["status"], "blocked_delivery")

    def test_invalid_draft_raw_reads_survive_legacy_handoff_but_changed_nodes_are_not_reused(self):
        session = self.worker()
        broken = self.system()
        broken["rules"][0]["action"]["call"] = "1Z"
        session.tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": broken}]})
        for index, pointer in enumerate(("/name", "/rules/0")):
            self.assertTrue(session.tools.execute("read" + str(index), "read_system", {"pointer": pointer})["ok"])
        memory = working_set(session.tools, 20000)
        self.assertIn("compilation_error", memory)
        self.assertEqual(len(memory["raw_reads"]), 2)
        session.tools.data["research_memory"].pop("raw_reads")
        carried = frozen_workspace(self.directory, self.controller.state, session.tools.data)
        self.assertEqual(len(carried["research_memory"]["raw_reads"]), 2)
        session.tools.data["research_memory"] = carried["research_memory"]
        session.tools.execute("rename", "edit_system", {"edits": [{"op": "replace", "path": "/name", "value": "Changed"}]})
        current = working_set(session.tools, 20000)
        self.assertEqual([item["pointer"] for item in current["raw_reads"]], ["/rules/0"])
        self.assertTrue(current["invalidated"])
        session.tools.save()
        session.save()
        continued = ResearchSession(self.controller, 2, "Z", self.parent, self.packet, identity="g002-Z-h01",
            assignment={**IDEA, "continue_from": session.identity, "turn_budget": 40, "seconds_budget": 200})
        self.assertIn("1Z", json.dumps(working_set(continued.tools, 20000)))

    def test_delivery_closure_rejects_empty_claim_and_accepts_a_real_replayable_negative_result(self):
        session = self.worker()
        invalid = self.decision()
        value = json.loads(invalid["output"][0]["arguments"])
        value["delivery_outcome"] = {"status": "delivered", "reason": "No actual evidence."}
        invalid["output"][0]["arguments"] = json.dumps(value)
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.side_effect = [invalid, self.decision(deliverable="counterexample")]
            self.manager.prepare(session, self.job)
            self.assertEqual(director.return_value.request.call_count, 2)
        goal = next(iter(self.manager.data["deliveries"].values()))
        self.assertEqual(goal["status"], "active")
        tools = session.tools
        tools.execute("edit", "edit_system", {"edits": [{"op": "replace", "path": "", "value": self.system()}]})
        case_id = tools.execute("case", "add_cases", {"cases": [{"name": "natural", "board": BOARD}]})["cases"][0]["case_id"]
        tools.execute("tests", "write_tests", {"tests": [{
            "name": "falsified-opening", "case_id": case_id, "mode": "auction",
            "assertions": [{"path": "/auction/0/call", "op": "eq", "value": "1D"}]}]})
        self.assertEqual(tools.execute("run", "run_tests", {})["failed"], 1)
        final = self.decision("stop", "counterexample")
        value = json.loads(final["output"][0]["arguments"])
        value["delivery_outcome"] = {"status": "falsified", "reason": "Saved natural opening regression reproduces the mismatch."}
        final["output"][0]["arguments"] = json.dumps(value)
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = final
            self.manager.data["tasks"]["redundant-continuation"] = {
                "id": "redundant-continuation", "lineage": "Z", "priority": 50, "idea": IDEA,
                "status": "pending", "delivery_id": goal["id"]}
            self.manager.finish(session, self.job)
        self.assertEqual(goal["status"], "falsified")
        self.assertTrue(goal["last_receipt"]["deliveries"]["counterexample"])
        self.assertEqual(self.manager.data["tasks"]["redundant-continuation"]["status"], "cancelled")
        self.assertEqual(self.manager.claim("Z", "g002-Z"), [])

    def test_engineering_blocks_only_until_verified_revision_is_active_and_prioritizes_its_delivery(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision(deliverable="tests")
            self.manager.prepare(session, self.job)
        baseline = self.controller.config["baselines"][0]["hash"]
        error = ReferenceAuctionError(SystemError("Unimplemented protocol branch: fixture", actor="E", system_hash=baseline),
            parse_board(BOARD), {"NS": self.controller.system(self.parent), "EW": self.controller.system(baseline)})
        recorded = record_reference_incident(self.controller, error, session.identity, "failed-reference")
        repair_id = recorded["jobs"][0]
        goal = next(iter(self.manager.data["deliveries"].values()))
        session.data["management_reason"] = "engineering_blocker"
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            self.assertFalse(self.manager.review(session, self.job))
            director.return_value.request.assert_not_called()
        self.assertEqual(goal["blocked_by"], [repair_id])
        self.assertEqual(self.manager.branch(session.identity)["missed_deliveries"], 0)
        job = self.controller.state["repair_jobs"][repair_id]
        job["status"] = "accepted_for_next_run"
        self.manager.reconcile()
        self.assertEqual(goal["status"], "blocked_engineering")
        job["status"] = "pending"
        self.controller.state["repair_jobs"]["repair-other"] = {
            "id": "repair-other", "kind": "framework", "status": "pending"}
        self.controller.config["repair_attempts"] = 1
        order = []
        def deferred(_controller, item):
            order.append(item["id"])
            item["status"] = "deferred"
        with patch("bridge_evolve.evolution.maintenance.repair", side_effect=deferred):
            RepairQueue(self.controller).process()
        self.assertEqual(order[0], repair_id)
        job.update(status="resolved_by_revision", validation={"status": "passed", "replayed_tables": 1})
        resumed = self.manager.claim("Z", "g002-Z")
        self.assertEqual(resumed[0]["idea"]["continue_from"], session.identity)
        self.assertEqual(resumed[0]["delivery_id"], goal["id"])

    def test_query_engine_failure_keeps_exact_incidents_and_does_not_misroute_design_errors(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision()
            self.manager.prepare(session, self.job)
        with patch.object(session.tools, "do_read_workspace", side_effect=KeyError("internal-result-key")):
            first = session.tools.execute("query1", "read_workspace", {"pointer": "/notes"})
            self.manager.reconcile()
            goal = next(iter(self.manager.data["deliveries"].values()))
            self.assertFalse(goal["blocked_by"])
            session.tools.execute("query2", "read_workspace", {"pointer": "/notes"})
        self.manager.reconcile()
        self.assertEqual(goal["status"], "blocked_engineering")
        self.assertEqual(len(self.controller.state["repair_jobs"]), 1)
        repair_id = first["repair"]["jobs"][0]
        self.assertEqual(self.controller.state["repair_jobs"][repair_id]["occurrences"], 2)
        with patch.object(session.tools, "do_read_system", side_effect=SystemError("Undefined candidate state", pointer="/protocol/states/missing")):
            result = session.tools.execute("design", "read_system", {"pointer": "/protocol"})
        self.assertNotIn("repair", result)
        session.save()
        self.controller.checkpoint()
        target = Controller(initialize(self.config, self.project / "imported-incidents", self.project))
        import_incidents(target, self.directory)
        imported = target.state["repair_jobs"][repair_id]
        self.assertEqual(imported["occurrences"], 2)
        evidence = target.read_artifact(target.directory / imported["evidence"][0])
        self.assertEqual(evidence["arguments"], {"pointer": "/notes"})
        self.assertEqual(evidence["workspace"]["system"], session.tools.data["system"])

    def test_closeout_reservation_stops_worker_and_exit_records_latest_evidence_without_ghosts(self):
        session = self.worker()
        self.job["management_task"] = "main-delivery"
        self.manager.data["tasks"]["main-delivery"] = {
            "id": "main-delivery", "status": "assigned", "lineage": "Z", "priority": 99,
            "idea": IDEA, "session": session.identity, "unit": "g001-Z"}
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision()
            self.manager.prepare(session, self.job)
        self.controller.config["max_agent_requests"] = 1
        with patch.object(session.client, "request") as request:
            session.run()
            request.assert_not_called()
        self.assertEqual(session.data["management_reason"], "closeout_budget")
        reserve = self.manager.branch(session.identity)["closeout_reserve"]
        with patch("bridge_evolve.evolution.models._gateway_key") as key:
            with self.assertRaises(EvolutionStopped):
                session.client.request([], [], label=session.identity + ":review")
            key.assert_not_called()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision("pause", "tests")
            self.manager.review(session, self.job)
            call = director.return_value.request.call_args
            self.assertEqual(len(call.args[1]), 1)
            self.assertLessEqual(token_reservation(request_body(self.controller.config, call.args[0], call.args[1]),
                                                  self.controller.config["max_output_tokens"]), reserve["tokens"])
        session.data["status"] = self.controller.state["research_sessions"][session.identity]["status"] = "running"
        session.save()
        self.manager.data["tasks"]["main-delivery"]["status"] = "assigned"
        self.job.update(status="implementing", session=session.identity)
        self.controller.state.setdefault("research_units", {})["g001-Z"] = {
            "status": "implementing", "jobs": [self.job], "worker_turns": 0}
        self.controller.state.update(status="budget_exhausted", error="Fixture resource boundary")
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            self.manager.finalize()
            self.manager.finalize()
            director.return_value.request.assert_not_called()
        goal = next(iter(self.manager.data["deliveries"].values()))
        self.assertEqual(goal["status"], "blocked_budget")
        self.assertEqual(self.controller.state["research_sessions"][session.identity]["status"], "blocked_budget")
        self.assertNotEqual(self.manager.data["tasks"]["main-delivery"]["status"], "assigned")
        saved = [item for item in self.manager.data["checkpoints"].values() if item.get("source") == "host_run_stop"]
        self.assertEqual(len(saved), 1)
        exported = json.loads(research_evidence_export(self.directory, session.identity,
            pointer="/management/checkpoints/" + saved[0]["id"]))
        self.assertEqual(exported["receipt"]["document_hash"], canonical_hash(session.tools.data["system"]))
        self.assertEqual(self.controller.state["research_units"]["g001-Z"]["status"], "budget_exhausted")

    def test_controller_budget_exit_invokes_delivery_finalization_without_model_requests(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision()
            self.manager.prepare(session, self.job)
        branch = self.manager.branch(session.identity)
        pending = self.manager.data["reviews"][branch["reviews"][-1]]
        pending["applied"] = False
        pending.pop("decision")
        branch["pending_review"] = pending["id"]
        session.data["status"] = "running"
        session.save()
        with patch("bridge_evolve.evolution.controller.Evaluator"), \
             patch.object(self.controller, "generation", side_effect=EvolutionStopped("Fixture exhausted research allocation")), \
             patch("bridge_evolve.evolution.director.ModelClient") as director:
            state = self.controller.run()
            director.return_value.request.assert_not_called()
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["research_sessions"][session.identity]["status"], "blocked_budget")
        goal = next(iter(state["management"]["deliveries"].values()))
        self.assertEqual(goal["status"], "blocked_budget")
        self.assertTrue(any(item.get("source") == "host_run_stop"
                            for item in state["management"]["checkpoints"].values()))
        self.assertEqual(state["management"]["reviews"][pending["id"]]["interruption"]["status"], "budget_exhausted")

    def test_initial_management_reserves_implementation_and_closure_before_spending(self):
        session = self.worker()
        self.controller.config["max_agent_requests"] = 2
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            self.assertFalse(self.manager.prepare(session, self.job))
            director.return_value.request.assert_not_called()
        goal = next(iter(self.manager.data["deliveries"].values()))
        self.assertEqual(goal["status"], "blocked_budget")
        self.assertEqual(self.controller.state["agent_requests"], 0)

    def test_resuming_paused_work_continues_actual_saved_session_not_its_old_input(self):
        session = self.worker()
        self.manager.data["tasks"]["paused"] = {
            "id": "paused", "lineage": "Z", "status": "paused", "priority": 10,
            "session": session.identity, "unit": "g000-Z",
            "idea": {**IDEA, "base_candidate": self.parent}}
        updates = [{"task_id": "paused", "status": "pending", "priority": 100,
                    "reason": "Resume the saved draft after its prerequisite."}]
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision(updates=updates)
            self.manager.prepare(session, self.job)
        task = self.manager.claim("Z", "g002-Z")[0]
        self.assertEqual(task["idea"]["continue_from"], session.identity)
        self.assertNotIn("base_candidate", task["idea"])
        self.assertNotIn("session", task)
        self.assertEqual(task["priority"], 100)
