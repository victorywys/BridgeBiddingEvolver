import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import test_campaign_manager as campaign
import test_expanded_research as expanded
import test_research as research
from bridge_evolve.evolution.controller import Controller, initialize
from bridge_evolve.evolution.config import blank_system, normalize_config
from bridge_evolve.evolution.delivery import (
    DeliveryDeadline, DeliveryWindow, RETURN_CHOICE, RETURN_SOLUTION, SolutionReturned,
)
from bridge_evolve.evolution.director import structured_request
from bridge_evolve.evolution.manager import CampaignManager
from bridge_evolve.evolution.models import ModelClient, request_body, token_reservation
from bridge_evolve.evolution.inspection import solution_export
from bridge_evolve.evolution.engine_maintenance import EngineWorkspace, repair_engine
from bridge_evolve.evolution.repairs import RepairQueue, record_incident
from bridge_evolve.evolution.proposals import evidence_packet
from bridge_evolve.evolution.research_tools import tool
from bridge_evolve.evolution.context import ContextCapacityError, assemble_context, latest_interaction
from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evolution.memory import working_set
from test_research import response


SOLUTION = {
    "proposal": "Empty auction: use 1C for the existing opening partition; retain the current public commitments and partner continuation.",
    "basis": "The saved draft defines this partition; no new fitness evidence is claimed.",
    "limitations": "The continuation needs authored boundary tests and a natural whole-auction witness.",
    "next_action": "Add the natural witness and check current sender/receiver guarantees.",
}


class TimeboxedDeliveryTests(unittest.TestCase):
    system = campaign.CampaignManagerTests.system
    worker = campaign.CampaignManagerTests.worker
    decision = campaign.CampaignManagerTests.decision

    def setUp(self):
        expanded.ExpandedResearchTests.setUp(self)
        self.config.update(campaign_manager={
            "turns": 3, "seconds": 900, "max_missed_deliveries": 2, "solution_return": True},
            repair_attempts=1, hypotheses_per_unit=1, generations=3, max_seconds=10000)
        self.directory = initialize(self.config, self.project / "timeboxed", self.project)
        self.controller = Controller(self.directory)
        self.parent = self.controller.state["champions"]["Z"]
        self.packet = evidence_packet(self.controller.system(self.parent), [], ["*"])
        self.manager = CampaignManager(self.controller)
        self.job = {"id": "g001-Z-h01", "idea": campaign.IDEA}
        self.controller.state["generations"]["1"] = {
            "status": "running", "parents": {"Z": self.parent}, "candidates": {}, "promotions": {}}
        self.controller.checkpoint()

    def prepare(self):
        session = self.worker()
        with patch("bridge_evolve.evolution.director.ModelClient") as client:
            self.assertTrue(self.manager.prepare(session, self.job))
            client.assert_not_called()
        return session

    def test_each_request_counts_down_and_last_slot_only_returns_a_real_qualified_solution(self):
        session = self.prepare()
        now = [datetime.now(timezone.utc)]
        replies = iter([
            response(("read-1", "read_system", {"pointer": "/name"})),
            response(("read-2", "read_system", {"pointer": "/name"})),
            response(("return", "return_solution", SOLUTION)),
        ])

        def request(*_args, **_kwargs):
            now[0] += timedelta(seconds=5)
            return next(replies)

        with patch("bridge_evolve.evolution.delivery.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda *_: now[0]
            with patch.object(session.client, "request", side_effect=request) as model:
                result = session.run()
        countdowns = [json.loads(call.args[0][-1]["content"])["delivery_countdown"] for call in model.call_args_list]
        self.assertEqual([row["turns_remaining"] for row in countdowns], [3, 2, 1])
        self.assertGreater(countdowns[0]["seconds_remaining"], countdowns[1]["seconds_remaining"])
        self.assertGreater(countdowns[1]["seconds_remaining"], countdowns[2]["seconds_remaining"])
        self.assertEqual(model.call_args_list[-1].args[1], [RETURN_SOLUTION])
        self.assertEqual(model.call_args_list[-1].kwargs["tool_choice"], RETURN_CHOICE)
        self.assertEqual(result["solution_return"]["solution"], SOLUTION)
        self.assertEqual(session.data["status"], "awaiting_management")
        self.assertFalse(session.tools.data["finished"])
        self.assertEqual(session.tools.data["releases"], [])
        returned = next(iter(self.controller.state["solution_deliveries"].values()))
        value = self.controller.read_artifact(self.directory / returned["result"]["path"])
        self.assertEqual(value["turns_used"], 3)
        self.assertEqual(len(value["countdown_history"]), 3)
        checkpoint = self.controller.read_artifact(self.directory / value["evidence"]["checkpoint"]["path"])
        self.assertEqual(checkpoint["system"], session.tools.data["system"])
        with patch("bridge_evolve.evolution.director.ModelClient") as director:
            director.return_value.request.return_value = self.decision("pause")
            self.assertFalse(self.manager.review(session, self.job))
        self.assertEqual(self.manager.branch(session.identity)["missed_deliveries"], 0)
        self.assertEqual(self.manager.data["deliveries"][self.manager.branch(session.identity)["delivery_id"]]["latest_solution"]["solution"], SOLUTION)

    def test_near_deadline_returns_before_exploring(self):
        session = self.prepare()
        self.manager.branch(session.identity)["contract"]["deadline_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat()
        with patch.object(session.client, "request", return_value=response(("return", "return_solution", SOLUTION))) as model:
            session.run()
        model.assert_called_once()
        self.assertEqual(model.call_args.args[1], [RETURN_SOLUTION])
        self.assertEqual(model.call_args.kwargs["tool_choice"], RETURN_CHOICE)
        self.assertEqual(session.delivery_window.data["return_reason"], "time_closeout")

    def test_returned_immature_proposal_really_updates_the_draft_but_cannot_publish(self):
        from bridge_evolve.evolution.controller import frozen_workspace
        session = self.prepare()
        solution = {**SOLUTION, "provisional_patch": {
            "document_hash": canonical_hash(session.tools.data["system"]),
            "edits": [{"op": "replace", "path": "/fallback", "value": {"kind": "call", "call": "1C"}}],
            "falsification": "Reject if a natural continuation emits an illegal 1C."}}
        with patch.object(session.client, "request", return_value=response(("return-patch", "return_solution", solution))):
            result = session.run()
        self.assertEqual(session.tools.data["system"]["fallback"]["call"], "1C")
        self.assertEqual(result["solution_return"]["provisional_application"]["status"], "applied_provisional")
        self.assertEqual(len(session.tools.data["provisional_proposals"]), 1)
        returned = self.controller.read_artifact(self.directory / result["solution_return"]["result"]["path"])
        checkpoint = self.controller.read_artifact(self.directory / returned["evidence"]["checkpoint"]["path"])
        self.assertEqual(checkpoint["provisional_proposals"], session.tools.data["provisional_proposals"])
        frozen = frozen_workspace(self.directory, self.controller.state, session.tools.data)
        self.assertEqual(frozen["provisional_proposals"], session.tools.data["provisional_proposals"])
        self.assertEqual(session.tools.data["releases"], [])
        published = session.tools.execute("reject-unverified", "publish_candidate", {"hypothesis": SOLUTION["proposal"]})
        self.assertFalse(published["ok"])
        self.assertEqual(session.tools.data["releases"], [])

    def test_stale_and_partially_invalid_proposals_return_explicit_rejections_without_partial_edits(self):
        session = self.prepare()
        before = copy.deepcopy(session.tools.data["system"])
        for label, digest, edits in (
            ("stale", "f" * 64, [{"op": "replace", "path": "/name", "value": "Do not apply"}]),
            ("invalid", canonical_hash(before), [
                {"op": "replace", "path": "/name", "value": "Do not partly apply"},
                {"op": "replace", "path": "/missing/member", "value": "invalid"}]),
        ):
            solution = {**SOLUTION, "provisional_patch": {
                "document_hash": digest, "edits": edits, "falsification": "A concrete failure is retained."}}
            result = session.tools.provisional_patch(solution, label)
            self.assertEqual(result["status"], "not_applied", result)
            self.assertTrue(result["error"])
            self.assertEqual(session.tools.data["system"], before)

    def test_crash_after_provisional_write_can_replay_the_return_without_reapplying_the_patch(self):
        session = self.prepare()
        window = DeliveryWindow(self.controller, self.directory / "proposal-replay.delivery.json",
                                "proposal-replay", session=session.identity)
        session.tools.delivery_window = window
        solution = {**SOLUTION, "provisional_patch": {
            "document_hash": canonical_hash(session.tools.data["system"]),
            "edits": [{"op": "add", "path": "/rules/-", "value": {
                "id": "provisional-once", "priority": 1, "when": {"op": "not", "args": [{"op": "has_contract_bid"}]},
                "action": {"kind": "call", "call": "1C"}}}],
            "falsification": "Natural tests must confirm the new partition."}}
        with patch.object(window, "complete", side_effect=RuntimeError("simulated host interruption")):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                session.tools.do_return_solution(solution)
        workspace = self.controller.state["research_sessions"][session.identity]["workspace"]
        session.tools.data = self.controller.read_artifact(self.directory / workspace)
        returned = session.tools.do_return_solution(solution)
        self.assertEqual(returned["provisional_application"]["status"], "applied_provisional")
        self.assertEqual([rule["id"] for rule in session.tools.data["system"]["rules"]].count("provisional-once"), 1)
        self.assertEqual(len(session.tools.data["provisional_proposals"]), 1)

    def test_missing_reference_becomes_an_immediately_blocking_persisted_incident(self):
        session = self.prepare()
        with patch("bridge_evolve.evolution.research_tools.ROOT", self.project):
            result = session.tools.execute("missing-reference", "read_reference", {"topic": "protocol"})
        self.assertFalse(result["ok"])
        incident = self.controller.state["repair_jobs"][result["repair"]["jobs"][0]]
        self.assertEqual(result["failure_kind"], "framework")
        self.assertEqual(incident["error"]["type"], "FileNotFoundError")
        self.assertTrue(incident["blocked_research"][0]["blocking"])
        self.assertEqual(session.tools.data["operations"]["missing-reference"]["status"], "completed")
        self.assertEqual(self.controller.state["recovery_incident"], incident["id"])
        recovery = {"source_run": "source-fixture", "incident": incident["id"], "status": "running",
                    "expected_engine": self.controller.state["engine_hash"]}
        self.controller.state.update(recovery=recovery, recovery_history=[copy.deepcopy(recovery)])
        resumed = session.tools.execute("recovered-reference", "read_reference", {"topic": "protocol"})
        self.assertTrue(resumed["ok"])
        receipt = self.controller.read_artifact(self.directory / "recovery-receipt.json")
        self.assertEqual((receipt["status"], receipt["tool"]), ("research_resumed", "read_reference"))
        incident["status"] = "resolved_by_revision"
        with patch("bridge_evolve.evolution.research_tools.ROOT", self.project):
            repeated = session.tools.execute("regressed-reference", "read_reference", {"topic": "protocol"})
        reopened = self.controller.state["repair_jobs"][repeated["repair"]["jobs"][0]]
        self.assertNotEqual(reopened["id"], incident["id"])
        self.assertEqual(reopened["fingerprint"], incident["id"])
        self.assertEqual(incident["status"], "resolved_by_revision")

    def test_new_engineering_blocker_still_returns_a_concrete_solution_before_waiting(self):
        session = self.prepare()
        replies = [response(("missing-reference", "read_reference", {"topic": "protocol"})),
                   response(("return-despite-blocker", "return_solution", SOLUTION))]
        with patch("bridge_evolve.evolution.research_tools.ROOT", self.project), patch.object(
                session.client, "request", side_effect=replies) as model:
            result = session.run()
        self.assertEqual(result["solution_return"]["solution"], SOLUTION)
        self.assertEqual(session.delivery_window.data["return_reason"], "engineering_blocker")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(model.call_args.args[1], [RETURN_SOLUTION])

    def test_provisional_workspace_recovers_both_sides_of_the_file_checkpoint_crash_gap(self):
        class ProcessCrash(BaseException):
            pass

        session = self.prepare()
        window_path = self.directory / "crash-gap.delivery.json"
        window = DeliveryWindow(self.controller, window_path, "crash-gap", session=session.identity)
        session.tools.delivery_window = window
        before = copy.deepcopy(session.tools.data["system"])
        solution = {**SOLUTION, "provisional_patch": {
            "document_hash": canonical_hash(before),
            "edits": [{"op": "add", "path": "/rules/-", "value": {
                "id": "durable-proposal", "priority": 1, "when": {"op": "not", "args": [{"op": "has_contract_bid"}]},
                "action": {"kind": "call", "call": "1C"}}}],
            "falsification": "Natural tests must verify the draft."}}
        key = session.tools.path.relative_to(self.directory).as_posix()
        for stage in ("before", "after"):
            controller = session.tools.controller
            original_artifact, original_checkpoint = controller.artifact, controller.checkpoint

            def interrupted_artifact(path, value):
                if path == session.tools.path:
                    raise ProcessCrash()
                return original_artifact(path, value)

            def interrupted_checkpoint():
                pending = controller.state.get("workspace_writes", {}).get(key)
                if pending and controller.state["artifacts"].get(key) == pending["after"]:
                    raise ProcessCrash()
                return original_checkpoint()

            method, replacement = ("artifact", interrupted_artifact) if stage == "before" else (
                "checkpoint", interrupted_checkpoint)
            with self.subTest(stage=stage), patch.object(controller, method, side_effect=replacement):
                with self.assertRaises(ProcessCrash):
                    session.tools.do_return_solution(solution)
            fresh = Controller(self.directory)
            if stage == "after":
                actual = session.tools.path.read_bytes()
                session.tools.path.write_text("{}")
                with self.assertRaisesRegex(ValueError, "neither side"):
                    fresh.read_artifact(session.tools.path)
                session.tools.path.write_bytes(actual)
            session.tools.data = fresh.read_artifact(session.tools.path)
            self.assertEqual(fresh.state["workspace_recoveries"][-1]["outcome"],
                             "not_written" if stage == "before" else "committed")
            if stage == "before":
                self.assertEqual(session.tools.data["system"], before)
            session.tools.controller = self.controller = fresh
            session.tools.delivery_window = DeliveryWindow(fresh, window_path, "crash-gap", session=session.identity)
        returned = session.tools.do_return_solution(solution)
        self.assertEqual(returned["provisional_application"]["status"], "applied_provisional")
        self.assertEqual([rule["id"] for rule in session.tools.data["system"]["rules"]].count("durable-proposal"), 1)

    def test_ready_engine_forces_a_real_solution_return_before_the_safe_handoff(self):
        from bridge_evolve.evolution.handoff import EngineRevisionReady
        session = self.prepare()
        self.controller.state.update(status="running", repair_engine={"hash": "e" * 64, "repair": "ready"})
        with patch.object(session.client, "request", return_value=response(("return-before-handoff", "return_solution", SOLUTION))) as model:
            result = session.run()
        self.assertEqual(result["solution_return"]["solution"], SOLUTION)
        self.assertEqual(session.delivery_window.data["return_reason"], "engine_handoff")
        self.assertEqual(model.call_args.args[1], [RETURN_SOLUTION])
        model.assert_called_once()
        with self.assertRaises(EngineRevisionReady):
            self.controller.stop()

    def test_missing_ir_members_are_input_errors_and_consultation_reads_the_correct_workspace(self):
        session = self.prepare()
        for index, pointer in enumerate(("/protocol/parameters/gquant", "/rules/999999")):
            result = session.tools.execute(f"bad-pointer-{index}", "read_system", {"pointer": pointer})
            self.assertFalse(result["ok"])
            self.assertNotIn("incident", result)
        self.assertEqual(self.controller.state.get("repair_jobs", {}), {})
        reply = response(text="The workspace is visible; missing nodes are explicit.")
        with patch("bridge_evolve.evolution.models.ModelClient.request", return_value=reply) as request:
            result = session.tools.execute("workspace-review", "consult", {
                "question": "Inspect the actual workspace", "pointers": ["/tests", "/workspace/cases", "/name", "/missing"]})
        self.assertTrue(result["ok"], result)
        evidence = json.loads(request.call_args.args[0][1]["content"])
        self.assertEqual(evidence["workspace"]["/tests"]["value"], session.tools.data["tests"])
        self.assertEqual(evidence["workspace"]["/workspace/cases"]["value"], session.tools.data["cases"])
        self.assertEqual(evidence["system"]["/name"]["value"], session.tools.data["system"]["name"])
        self.assertIn("/missing", result["pointer_errors"])

    def test_small_allocation_pays_for_a_solution_instead_of_initial_management_and_zero_delivery(self):
        session = self.prepare()
        self.controller.config["max_agent_requests"] = 1
        with patch.object(session.client, "request", return_value=response(("return", "return_solution", SOLUTION))) as model:
            session.run()
        model.assert_called_once()
        self.assertEqual(model.call_args.args[1], [RETURN_SOLUTION])
        self.assertEqual(model.call_args.kwargs["tool_choice"], RETURN_CHOICE)
        self.assertEqual(session.delivery_window.data["return_reason"], "resource_closeout")

    def test_retained_large_history_is_focused_to_allow_real_work_before_returning(self):
        session = self.prepare()
        self.controller.config.update(max_agent_tokens=78804, max_context_bytes=262144, max_output_tokens=8192)
        session.data["messages"].append({"role": "user", "content": "old saved context " * 8000})
        with patch.object(session.client, "request", side_effect=[
                response(("read", "read_system", {"pointer": "/name"})),
                response(("return", "return_solution", SOLUTION))]) as model:
            session.run()
        self.assertNotIn("tool_choice", model.call_args_list[0].kwargs)
        self.assertEqual(session.data["delivery_requests"]["1"]["context_mode"], "budget_focus")
        self.assertNotIn("old saved context", json.dumps(model.call_args_list[0].args[0]))
        actual_input = self.controller.read_artifact(session.path.parent / "request-0001-input.json")
        self.assertEqual(actual_input["messages"], model.call_args_list[0].args[0])

    def test_budget_focus_delivers_nested_raw_result_in_the_actual_next_http_request_and_allows_editing(self):
        session = self.prepare()
        self.controller.config.update(max_agent_tokens=78804, max_context_bytes=262144, max_output_tokens=8192)
        raw = self.system()
        state = raw["protocol"]["initial"]
        expected = {"transitions": [{"id": "repairable.receiver", "when": {"op": "and", "args": [
            {"op": "not", "args": [{"op": "has_contract_bid"}]}, False]},
            "to": "missing_receiver", "set": {}}], "otherwise": state}
        raw["protocol"]["states"][state] = copy.deepcopy(expected)
        session.tools.execute("seed", "edit_system", {"edits": [{"op": "replace", "path": "", "value": raw}]})
        session.data["messages"].append({"role": "user", "content": "old context " * 12000})
        replies = [
            response(("source", "read_system", {"pointer": f"/protocol/states/{state}"})),
            response(("repair", "edit_system", {"edits": [{
                "op": "replace", "path": f"/protocol/states/{state}/transitions/0/to", "value": state}]})),
            response(("return", "return_solution", SOLUTION)),
        ]
        bodies = []

        def send(request, **_kwargs):
            body = json.loads(request.data)
            bodies.append(body)
            if len(bodies) == 2:
                delivered = next(item for item in body["input"] if item.get("call_id") == "source"
                                 and item.get("type") == "function_call_output")
                self.assertEqual(json.loads(delivered["output"])["value"], expected)
            reply = replies[len(bodies) - 1]
            reply["usage"] = {"input_tokens": 1000, "output_tokens": 100}
            return research.ModelBudgetTests.Response(reply)

        with patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.side_effect = send
            session.run()
        self.assertEqual(len(bodies), 3)
        self.assertEqual(session.data["delivery_requests"]["2"]["context_mode"], "budget_focus")
        self.assertEqual(session.tools.data["system"]["protocol"]["states"][state]["transitions"][0]["to"], state)
        self.assertTrue(session.tools.data["research_memory"]["context_deliveries"]["source"]["full_tool_result"])
        receipt = self.controller.read_artifact(session.path.parent / "request-0002-input.json")
        self.assertEqual(receipt["messages"], bodies[1]["input"])
        self.assertEqual(receipt["context"]["body_sha256"],
                         hashlib.sha256(request_body(self.controller.config, bodies[1]["input"], session.definitions)).hexdigest())
        self.assertEqual(session.data["context_requests"]["2"]["status"], "received")

    def test_compaction_and_solution_closeout_keep_the_latest_native_tool_result(self):
        session = self.prepare()
        result = session.tools.execute("read", "read_system", {"pointer": "/protocol"})
        session.data["messages"].extend([
            {"type": "function_call", "name": "read_system", "arguments": '{"pointer":"/protocol"}', "call_id": "read"},
            {"type": "function_call_output", "call_id": "read", "output": json.dumps(result)}])
        session.compact("Keep the pending protocol repair " * 10000)
        compacted = next(message for message in session.data["messages"] if message.get("type") == "function_call_output")
        self.assertEqual(json.loads(compacted["output"]), result)
        window = DeliveryWindow(self.controller, self.directory / "context-window.json", "context:clock")
        closing = window.return_messages(session.solution_context(), tail=latest_interaction(session.data["messages"]),
                                         required=("assignment", "management_contract"))
        delivered = next(message for message in closing if message.get("type") == "function_call_output")
        self.assertEqual(json.loads(delivered["output"]), result)
        payload = json.loads(closing[1]["content"])
        self.assertEqual(payload["context_delivery"]["latest_tool_interaction"], "full")
        self.assertEqual(payload["management_contract"], self.manager.branch(session.identity)["contract"])

    def test_versioned_read_reuse_normalizes_defaults_and_preserves_page_progress(self):
        session = self.prepare()
        session.tools.data["system"]["features"] = {"a": 1, "b": 2, "c": 3, "d": 4}
        first = session.tools.execute("page-one", "read_system", {"pointer": "/features", "limit": 2})
        self.assertEqual(first["next_offset"], 2)
        with patch.object(session.tools, "do_read_system", side_effect=AssertionError("Unchanged page was queried again")):
            reused = session.tools.execute("same-page", "read_system", {
                "snapshot": "working", "pointer": "/features", "offset": 0, "limit": 2})
        self.assertEqual(reused, first)
        self.assertTrue(session.tools.data["operations"]["same-page"]["cache_hit"])
        second = session.tools.execute("page-two", "read_system", {"pointer": "/features", "offset": 2, "limit": 2})
        self.assertEqual(second["value"], {"c": 3, "d": 4})
        self.assertIsNone(second["next_offset"])
        session.tools.execute("change", "edit_system", {"edits": [{"op": "replace", "path": "/features/a", "value": 9}]})
        changed = session.tools.execute("new-page", "read_system", {"pointer": "/features", "limit": 2})
        self.assertEqual(changed["value"], {"a": 9, "b": 2})
        self.assertFalse(session.tools.data["operations"]["new-page"]["cache_hit"])
        session.tools.execute("reorder", "edit_system", {"edits": [{
            "op": "replace", "path": "/features", "value": {"c": 3, "d": 4, "a": 9, "b": 2}}]})
        retained = working_set(session.tools, session.tools.result_limit)
        self.assertFalse(any(row["pointer"] == "/features" for row in retained["raw_reads"]))
        reordered = session.tools.execute("reordered-page", "read_system", {"pointer": "/features", "limit": 2})
        self.assertEqual(reordered["value"], {"c": 3, "d": 4})
        self.assertFalse(session.tools.data["operations"]["reordered-page"]["cache_hit"])
        remainder = session.tools.execute("reordered-next", "read_system", {
            "pointer": "/features", "offset": reordered["next_offset"], "limit": 2})
        self.assertEqual({**reordered["value"], **remainder["value"]}, session.tools.data["system"]["features"])

    def test_latest_source_precedes_saved_progress_while_the_task_contract_remains_intact(self):
        session = self.prepare()
        result = session.tools.execute("source", "read_system", {"pointer": "/protocol"})
        session.data["messages"].extend([
            {"type": "function_call", "name": "read_system", "arguments": '{"pointer":"/protocol"}', "call_id": "source"},
            {"type": "function_call_output", "call_id": "source", "output": json.dumps(result)}])
        progress = {"stage": "draft", "next_action": "Retain the source and verify the receiver.",
                    "stage_design": {"notes": "Previously saved public-stage design. " * 200}}
        session.tools.data["implementation_state"] = progress
        context = {"assignment": session.assignment, "implementation_state": progress,
                   "working_set": {"system_document_hash": result["workspace_hash"], "state": progress,
                                   "raw_reads": [], "nodes": []}}
        core = session.context_messages({"assignment": session.assignment})
        maximum = len(request_body(self.controller.config, core, session.definitions)) + 1024
        focused = session.context_messages(context, maximum=maximum)
        payload = json.loads(focused[1]["content"])
        self.assertEqual(payload["management_contract"], self.manager.branch(session.identity)["contract"])
        self.assertEqual(payload["context_delivery"]["implementation_state"], "not_delivered")
        self.assertEqual(payload["context_delivery"]["implementation_state_ref"]["workspace_pointer"], "/implementation_state")
        self.assertNotIn("implementation_state", payload)
        self.assertNotIn("state", payload["working_set"])
        delivered = next(item for item in focused if item.get("type") == "function_call_output")
        self.assertEqual(json.loads(delivered["output"]), result)
        self.assertLessEqual(len(request_body(self.controller.config, focused, session.definitions)), maximum)
        recovered = session.tools.execute("recover-progress", "read_workspace", {"pointer": "/implementation_state"})
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["value"], progress)
        roomy = json.loads(session.context_messages(context)[1]["content"])
        self.assertEqual(roomy["implementation_state"], progress)
        self.assertNotIn("state", roomy["working_set"])

    def test_large_raw_read_is_retained_and_unfittable_closeout_is_explicit_not_a_fake_delivery(self):
        session = self.prepare()
        session.tools.data["system"]["features"] = {f"feature_{i}": "source " * 70 for i in range(60)}
        result = session.tools.execute("large-read", "read_system", {"pointer": "/features", "limit": 100})
        self.assertGreater(len(json.dumps(result)), 12000)
        saved = session.tools.data["research_memory"]["raw_reads"]
        self.assertEqual(next(reversed(saved.values()))["page"]["value"], result["value"])
        tail = [
            {"type": "function_call", "name": "read_system", "arguments": '{"pointer":"/features","limit":100}', "call_id": "large-read"},
            {"type": "function_call_output", "call_id": "large-read", "output": json.dumps(result)}]
        window = DeliveryWindow(self.controller, self.directory / "large-context-window.json", "context:large")
        closing = window.return_messages({}, tail=tail,
            archive_tail=lambda messages: session.tools.full_result({"messages": messages}))
        payload = json.loads(closing[1]["content"])["context_delivery"]
        self.assertEqual(payload["latest_tool_interaction"], "not_delivered")
        self.assertFalse(any(message.get("type") == "function_call_output" for message in closing))
        ref = payload["tool_result_not_delivered"]["details_ref"]
        restored = session.tools.execute("recover-full", "read_result", {
            "result_id": ref["result_id"], "pointer": "/messages/1/output", "text_limit": 8})
        self.assertEqual(restored["value"], json.dumps(result)[:8])
        self.assertLessEqual(len(request_body(self.controller.config, closing, [RETURN_SOLUTION], tool_choice=RETURN_CHOICE)), 18000)

    def test_interrupted_run_tests_can_return_solution_without_claiming_a_full_result(self):
        session = self.prepare()
        replies = [
            response(("interrupted-tests", "run_tests", {}), ("unstarted", "run_tests", {})),
            response(("return", "return_solution", SOLUTION)),
        ]
        with patch.object(session.client, "request", side_effect=replies) as model, \
             patch.object(session.tools, "do_run_tests", side_effect=DeliveryDeadline("test deadline")) as tests:
            result = session.run()
        tests.assert_called_once()
        self.assertEqual(model.call_count, 2)
        self.assertEqual(result["solution_return"]["solution"], SOLUTION)
        self.assertEqual(session.data["pending_calls"], [])
        self.assertEqual(session.data["context_requests"]["2"]["status"], "received")
        operation = session.tools.data["operations"]["interrupted-tests"]
        self.assertEqual(operation["status"], "started")
        self.assertNotIn("result", operation)
        self.assertNotIn("unstarted", session.tools.data["operations"])
        self.assertNotIn("last_tests", session.tools.data)
        outputs = {item["call_id"]: json.loads(item["output"])
                   for item in model.call_args_list[1].args[0]
                   if item.get("type") == "function_call_output"}
        self.assertTrue(outputs["interrupted-tests"]["deferred_for_solution_return"])
        receipt = session.tools.data["research_memory"]["context_deliveries"]["interrupted-tests"]
        self.assertEqual(receipt, {"request": 2, "sha256": canonical_hash(outputs["interrupted-tests"]),
                                   "full_tool_result": False})
        saved = self.controller.read_artifact(session.tools.path)
        self.assertEqual(saved["operations"]["interrupted-tests"], operation)
        self.assertEqual(saved["research_memory"]["context_deliveries"]["interrupted-tests"], receipt)
        self.assertEqual(session.tools.data["releases"], [])

    def test_context_receipts_require_a_completed_matching_result(self):
        session = self.prepare()
        success = {"ok": True, "value": "complete"}
        failure = {"ok": False, "error": "real test error"}
        rows = [
            ("success", {"status": "completed", "result": success}, success, True),
            ("failure", {"status": "completed", "result": failure}, failure, True),
            ("shortened", {"status": "completed", "result": success}, {"ok": True}, False),
            ("started", {"status": "started"}, None, False),
            ("partial", {"status": "started", "result": success}, success, False),
            ("missing", {"status": "completed"}, None, False),
        ]
        messages = []
        for call_id, operation, output, _expected in rows:
            session.tools.data["operations"][call_id] = {"name": "run_tests", "arguments": {}, **operation}
            messages.extend([
                {"type": "function_call", "name": "run_tests", "arguments": "{}", "call_id": call_id},
                {"type": "function_call_output", "call_id": call_id, "output": json.dumps(output)},
            ])
        before = copy.deepcopy(session.tools.data["operations"])
        session.record_request_context(messages, session.definitions, None)
        session.receive_request_context()
        first = copy.deepcopy(session.tools.data["research_memory"]["context_deliveries"])
        session.receive_request_context()
        self.assertEqual(session.tools.data["operations"], before)
        self.assertEqual(session.tools.data["research_memory"]["context_deliveries"], first)
        for call_id, _operation, output, expected in rows:
            self.assertEqual(first[call_id], {"request": 1, "sha256": canonical_hash(output),
                                             "full_tool_result": expected})
        session.tools.data["operations"]["started"].update(status="completed", result=success)
        session.data["turns"] += 1
        session.record_request_context([
            {"type": "function_call_output", "call_id": "started", "output": json.dumps(success)},
        ], session.definitions, None)
        session.receive_request_context()
        self.assertEqual(session.tools.data["research_memory"]["context_deliveries"]["started"], {
            "request": 2, "sha256": canonical_hash(success), "full_tool_result": True})

    def test_failed_transport_does_not_mark_prepared_context_as_received(self):
        session = self.prepare()
        with patch.object(session.client, "request", side_effect=OSError("interrupted transport")):
            with self.assertRaisesRegex(OSError, "interrupted transport"):
                session.run()
        self.assertEqual(session.data["context_requests"]["1"]["status"], "prepared")
        self.assertFalse(session.tools.data["research_memory"].get("context_deliveries"))

    def test_latest_interaction_pairs_replayed_call_ids_with_the_recent_call_group(self):
        call = {"type": "function_call", "name": "read_system", "arguments": "{}", "call_id": "read"}
        output = {"type": "function_call_output", "call_id": "read", "output": '{"old":true}'}
        current = [call, {**call, "call_id": "other"},
                   {**output, "output": '{"current":true}'}, {**output, "call_id": "other"}]
        messages = [call, output, {"role": "user", "content": "Recovered work"}, *current]
        self.assertEqual(latest_interaction(messages), current)

    def test_zero_or_negative_context_allowance_is_not_replaced_by_the_full_budget(self):
        for maximum in (0, -1):
            with self.subTest(maximum=maximum):
                with self.assertRaises(ContextCapacityError):
                    assemble_context(self.controller.config, "Return the current solution.", {}, [],
                                     maximum=maximum)

    def test_saved_response_recovery_finalizes_the_prepared_context_without_a_second_request(self):
        session = self.prepare()
        write_artifact = self.controller.artifact

        def interrupted_write(path, value):
            result = write_artifact(path, value)
            if path.name == "response-0001.json":
                raise OSError("interrupted after persisting the response")
            return result

        with patch.object(session.client, "request", return_value=response(("return", "return_solution", SOLUTION))):
            with patch.object(self.controller, "artifact", side_effect=interrupted_write):
                with self.assertRaisesRegex(OSError, "persisting the response"):
                    session.run()
        resumed = self.worker()
        self.assertEqual(resumed.data["context_requests"]["1"]["status"], "prepared")
        with patch.object(resumed.client, "request", side_effect=AssertionError("An archived reply was requested twice")):
            result = resumed.run()
        self.assertEqual(result["solution_return"]["solution"], SOLUTION)
        self.assertEqual(resumed.data["context_requests"]["1"]["status"], "received")

    def test_return_reserve_still_funds_larger_escaped_work_discovered_later(self):
        window = DeliveryWindow(self.controller, self.directory / "reserve-window.json", "reserve:clock")
        initial = window.hold_return_request(window.return_messages({"proposal": "small saved scheme"}), "research")["tokens"]
        large = window.return_messages({"stage_design": '\\"' * 20000, "current_choice": "1C " * 5000})
        window.closing = True
        body = request_body(self.controller.config, [*large, window.reminder(0)], [RETURN_SOLUTION], tool_choice=RETURN_CHOICE)
        self.assertLessEqual(len(body), self.controller.config["max_context_bytes"])
        self.assertLessEqual(token_reservation(body, self.controller.config["max_output_tokens"]), initial)

    def test_solution_mode_rejects_nonboolean_configuration_instead_of_silently_disabling(self):
        for invalid in (None, "true", 1):
            with self.subTest(value=invalid):
                raw = {**self.config, "campaign_manager": {**self.config["campaign_manager"], "solution_return": invalid}}
                with self.assertRaisesRegex(ValueError, "solution_return must be boolean"):
                    normalize_config(raw, self.project)

    def test_imported_delivery_receives_a_retained_receipt_before_any_new_worker_starts(self):
        session = self.prepare()
        self.controller.state["status"] = "blocked"
        self.controller.checkpoint()
        config = {**self.config, "champion_seed_run": str(self.directory), "repair_seed_run": str(self.directory)}
        successor = Controller(initialize(config, self.project / "successor", self.project))
        successor.process_repairs()
        manager = CampaignManager(successor)
        goal = next(iter(manager.data["deliveries"].values()))
        receipt = goal["last_receipt"]
        self.assertEqual(receipt["evidence_scope"], "retained_snapshot")
        self.assertEqual(receipt["new_tool_calls"], 0)
        self.assertEqual(receipt["operations"], 0)
        self.assertEqual(receipt["releases"], 0)
        self.assertFalse(any(receipt["deliveries"].values()))
        self.assertEqual(successor.state["agent_requests"], 0)
        self.assertEqual(receipt["document_hash"], campaign.canonical_hash(session.tools.data["system"]))

    def test_cached_paid_return_is_recovered_after_deadline_without_a_new_request_or_resetting_the_clock(self):
        session = self.prepare()
        deadline = self.manager.branch(session.identity)["contract"]["deadline_at"]
        with patch.object(session.client, "request", return_value=response(("return", "return_solution", SOLUTION))) as model:
            with patch.object(session.tools, "execute", side_effect=OSError("interrupted after saved response")):
                with self.assertRaises(OSError):
                    session.run()
            model.assert_called_once()
        resumed = self.worker()
        with patch("bridge_evolve.evolution.delivery.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(seconds=1000)
            with patch.object(resumed.client, "request") as model:
                result = resumed.run()
                model.assert_not_called()
        self.assertEqual(result["solution_return"]["solution"], SOLUTION)
        self.assertEqual(resumed.delivery_window.data["deadline_at"], deadline)
        reference = resumed.delivery_window.data["result"]
        first = (self.directory / reference["path"]).read_bytes()
        resumed.run()
        self.assertEqual((self.directory / reference["path"]).read_bytes(), first)
        self.assertEqual(len(self.controller.state["solution_deliveries"]), 1)

    def test_consultation_uses_a_slot_and_cannot_take_the_return_slot(self):
        session = self.prepare()
        replies = [
            response(("consult", "consult", {"question": "Check the opening partition", "pointers": ["/name"]})),
            response(text="Keep the current opening and test its public guarantees."),
            response(("return", "return_solution", SOLUTION)),
        ]
        with patch.object(session.client, "request", side_effect=replies) as model:
            session.run()
        self.assertEqual(len(model.call_args_list), 3)
        self.assertTrue(model.call_args_list[1].kwargs["label"].endswith(":review"))
        self.assertEqual([json.loads(call.args[0][-1]["content"])["delivery_countdown"]["turns_remaining"]
                          for call in model.call_args_list], [3, 2, 1])
        self.assertEqual(session.delivery_window.data["requests"], 3)
        self.assertEqual(model.call_args_list[-1].args[1], [RETURN_SOLUTION])

    def test_engineering_period_returns_a_partial_solution_without_bypassing_submission_validation(self):
        definition = tool("submit_repair", "Submit an implemented and verified repair.",
                          {"summary": {"type": "string"}}, ("summary",))
        validator = Mock(side_effect=AssertionError("A partial solution must not enter acceptance."))
        self.controller.config["campaign_manager"]["turns"] = 1
        path = self.directory / "repairs" / "period-one.json"
        with patch("bridge_evolve.evolution.director.ModelClient") as client:
            client.return_value.request.return_value = response(("return", "return_solution", SOLUTION))
            with self.assertRaises(SolutionReturned) as returned:
                structured_request(self.controller, path, "Repair the observed failure.", {"incident": "exact failure"},
                    definition, "repair:repair-test:engine:1", validator=validator,
                    solution_evidence=lambda: {"changes": [], "tests": [], "acceptance": "not_reviewed"})
        validator.assert_not_called()
        self.assertEqual(returned.exception.value["solution"], SOLUTION)
        self.assertEqual(returned.exception.value["evidence"]["tests"], [])
        self.assertFalse(path.with_suffix(".resolved.json").exists())
        self.assertEqual(client.return_value.request.call_args.args[1], [RETURN_SOLUTION])
        self.assertEqual(client.return_value.request.call_args.kwargs["tool_choice"], RETURN_CHOICE)

    def test_deadline_scopes_clip_network_and_engineering_tool_time_without_poisoning_the_controller(self):
        path = self.directory / "window.json"
        window = DeliveryWindow(self.controller, path, "test:clock", session=self.job["id"],
            deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=180)).isoformat())
        outside = self.controller.model_time_window()
        with window.active():
            self.assertGreater(window.timeout(), 59)
            self.assertLessEqual(self.controller.model_time_window(), 60)
            self.assertLessEqual(self.controller.maintenance_time_window(), 60)
            window.closing = True
            self.assertGreater(self.controller.model_time_window(), 179)
            window.data["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            with self.assertRaises(DeliveryDeadline):
                self.controller.stop()
        self.assertGreater(self.controller.model_time_window(), outside - 1)

    def test_actual_network_payload_forces_the_return_function_and_uses_remaining_timeout(self):
        self.controller.config["agent_timeout"] = 5000
        window = DeliveryWindow(self.controller, self.directory / "network-window.json", "network:clock",
            deadline_at=(datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat())
        window.closing = True
        client = ModelClient(self.controller.config, self.controller.state, self.controller.checkpoint,
                             self.controller.stop, request_timeout=self.controller.model_time_window)
        with window.active(), patch("bridge_evolve.evolution.models._gateway_key", return_value="unit-only-secret"), \
             patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.return_value = research.ModelBudgetTests.Response(
                response(("return", "return_solution", SOLUTION)))
            client.request([window.reminder(0)], [RETURN_SOLUTION], label="network:return", tool_choice=RETURN_CHOICE)
        payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(payload["tool_choice"], RETURN_CHOICE)
        self.assertEqual(payload["tools"], [RETURN_SOLUTION])
        self.assertGreater(opener.return_value.open.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(opener.return_value.open.call_args.kwargs["timeout"], 40)
        self.assertEqual(self.controller.state["model_requests"][-1]["tool_choice"], RETURN_CHOICE)

    def test_invalid_last_response_fails_explicitly_without_an_extra_round_or_fake_scheme(self):
        session = self.prepare()
        self.controller.config["max_agent_requests"] = 1
        with patch.object(session.client, "request", return_value=response(text="Need more research.")) as model:
            with self.assertRaisesRegex(RuntimeError, "did not call return_solution"):
                session.run()
        model.assert_called_once()
        self.assertEqual(session.delivery_window.data["status"], "failed")
        self.assertEqual(self.controller.state.get("solution_deliveries", {}), {})
        self.assertTrue(session.data["responses"])

    def test_download_is_the_exact_immutable_return_and_rejects_modified_content(self):
        session = self.prepare()
        with patch.object(session.client, "request", return_value=response(("return", "return_solution", SOLUTION))):
            session.run()
        identity = session.delivery_window.data["id"]
        path = self.directory / session.delivery_window.data["result"]["path"]
        self.assertEqual(solution_export(self.directory, identity), path.read_bytes())
        path.write_text('{"schema":"tampered"}')
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            solution_export(self.directory, identity)

    def test_engineer_returns_each_period_and_continues_the_same_unaccepted_source(self):
        for name in ("src", "tests", "tools"):
            (self.project / name).mkdir()
        (self.project / "src/period_fixture.py").write_text("def answer():\n    return 1\n")
        (self.project / "pyproject.toml").write_text("[project]\nname='period-fixture'\nversion='0.0.0'\n")
        baseline = self.controller.config["baselines"][0]["hash"]
        record_incident(self.controller, {**research.BOARD, "tables": [], "audit_result": {"errors": [
            {"stage": "execution", "message": "A bounded engineering period", "type": "RuntimeError"}]}},
            self.parent, baseline, "period-counterexample")
        job = next(iter(self.controller.state["repair_jobs"].values()))
        with patch("bridge_evolve.evolution.engine_maintenance.ROOT", self.project):
            workspace = EngineWorkspace(self.controller, job)
        self.assertTrue(workspace.execute("edit_engine_file", {
            "path": "src/period_fixture.py", "old": "return 1", "new": "return 2"}, "saved-edit")["ok"])
        self.controller.config["campaign_manager"]["turns"] = 1
        with patch("bridge_evolve.evolution.director.ModelClient") as client:
            client.return_value.request.side_effect = [
                response(("period-1", "return_solution", SOLUTION)), response(("period-2", "return_solution", SOLUTION))]
            repair_engine(self.controller, job)
            self.assertEqual(job["status"], "solution_returned")
            self.assertEqual(job["iteration"], 2)
            repair_engine(self.controller, job)
        self.assertEqual(job["status"], "solution_returned")
        self.assertEqual(job["iteration"], 3)
        self.assertEqual(len(set(job["solution_returns"])), 2)
        self.assertNotIn("repair_engine", self.controller.state)
        recovered = EngineWorkspace(self.controller, job)
        self.assertEqual(recovered.data["changes"], workspace.data["changes"])
        self.assertIn("return 2", (recovered.root / "src/period_fixture.py").read_text())

    def test_fatal_repair_continues_after_period_return_through_tests_review_and_safe_handoff(self):
        for name in ("src", "tests", "tools"):
            (self.project / name).mkdir()
        for name in ("board_viewer.py", "verify_classics.py"):
            (self.project / "tools" / name).write_text("")
        (self.project / "src/period_fixture.py").write_text("def answer():\n    return 1\n")
        (self.project / "pyproject.toml").write_text("[project]\nname='period-fixture'\nversion='0.0.0'\n")
        regression = ("import unittest\nfrom period_fixture import answer\n"
                      "class PeriodTests(unittest.TestCase):\n"
                      "    def test_answer(self):\n        self.assertEqual(answer(), 2)\n")
        selector = "test_period_fixture.PeriodTests.test_answer"
        proposal = {"diagnosis": "The original answer is wrong.", "summary": "Return the corrected answer.",
                    "reproduction_test": selector}
        solution = {**SOLUTION, "proposal": "The isolated answer now returns 2; verify before acceptance."}
        replies = [
            response(("edit", "edit_engine_file", {
                "path": "src/period_fixture.py", "old": "return 1", "new": "return 2"})),
            response(("test", "edit_engine_file", {
                "path": "tests/test_period_fixture.py", "old": "", "new": regression})),
            response(("return", "return_solution", solution)),
            response(("verify", "run_engine_tests", {"selectors": [selector]})),
            response(("submit", "submit_engine_revision", proposal)),
            response(("review", "review_engine_revision", {"approve": True, "reason": "Actual original failure and corrected regression confirmed."})),
        ]
        with patch("bridge_evolve.evolution.engine_maintenance.ROOT", self.project), \
             patch.object(self.controller, "load_frozen_state"), \
             patch.object(self.controller, "generation", side_effect=KeyError("result")), \
             patch("bridge_evolve.evolution.director.ModelClient") as client:
            client.return_value.request.side_effect = replies
            result = self.controller.run()
        self.assertEqual(client.return_value.request.call_count, 6)
        self.assertEqual(result["status"], "continued", result.get("error"))
        job = result["repair_jobs"][result["recovery_incident"]]
        self.assertEqual(job["status"], "accepted_engine_revision", job.get("repair_error"))
        self.assertEqual(len(job["solution_returns"]), 1)
        self.assertEqual(job["iteration"], 2)
        self.assertEqual(job["period_supervision"]["dispatches"], 2)
        self.assertEqual(job["period_supervision"]["status"], "accepted_engine_revision")
        workspace = self.controller.read_artifact(self.directory / job["workspace"])
        self.assertEqual(workspace["tests"][-1]["returncode"], 0)
        self.assertNotEqual(workspace["original_reproduction"]["returncode"], 0)
        self.assertIn("AssertionError: 1 != 2", workspace["original_reproduction"]["stderr"])
        self.assertTrue(job["review"]["approve"])
        self.assertEqual(result["continuation"]["engine_revision"]["hash"], job["engine_hash"])
        self.assertIn(job["id"], result["continuation"]["engine_revision"]["covers"])
        self.assertIn("return 1", (self.project / "src/period_fixture.py").read_text())

    def test_engineering_supervision_stops_identical_unimplemented_periods_without_claiming_acceptance(self):
        baseline = self.controller.config["baselines"][0]["hash"]
        record_incident(self.controller, {**research.BOARD, "tables": [], "audit_result": {"errors": [
            {"stage": "execution", "message": "No engineering progress", "type": "RuntimeError"}]}},
            self.parent, baseline, "stalled-engineering")
        job = next(iter(self.controller.state["repair_jobs"].values()))

        def partial(_controller, current):
            current.update(status="solution_returned", iteration=current.get("iteration", 1) + 1,
                           latest_solution=SOLUTION)

        with patch("bridge_evolve.evolution.maintenance.repair", side_effect=partial) as repair:
            self.controller.process_repairs()
        self.assertEqual(repair.call_count, 2)
        self.assertEqual(job["status"], "solution_returned")
        self.assertEqual(job["period_supervision"]["status"], "blocked")
        self.assertEqual(job["period_supervision"]["stalled_periods"], 2)
        self.assertIn("没有新增", job["repair_error"])
        self.assertNotIn("repair_engine", self.controller.state)
        self.assertNotIn("continuation", self.controller.state)

    def test_repair_only_retains_budget_blocked_partial_work_instead_of_reporting_completion(self):
        from bridge_evolve.evolution.costs import budget_usage
        baseline = self.controller.config["baselines"][0]["hash"]
        record_incident(self.controller, {**research.BOARD, "tables": [], "audit_result": {"errors": [
            {"stage": "execution", "message": "Last maintenance request", "type": "RuntimeError"}]}},
            self.parent, baseline, "budget-blocked-engineering")
        self.controller.config["maintenance_budget"]["max_requests"] = 1
        before = budget_usage(self.controller.state, "research")

        def partial(_controller, job):
            job.update(status="solution_returned", iteration=2, latest_solution=SOLUTION)
            self.controller.state["model_requests"] = [{
                "request": 1, "budget": "maintenance", "status": "received",
                "usage": {"input_tokens": 10, "output_tokens": 10}}]
            self.controller.state["agent_requests"] = 1

        with patch.object(self.controller, "load_frozen_state"), \
             patch("bridge_evolve.evolution.maintenance.repair", side_effect=partial) as repair:
            result = self.controller.run(repair_only=True)
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(result["status"], "blocked", result.get("error"))
        self.assertEqual(result["blocked_component"], "maintenance")
        job = next(iter(result["repair_jobs"].values()))
        self.assertEqual(job["period_supervision"]["status"], "blocked")
        self.assertIn("独立维护余额不足", job["repair_error"])
        self.assertEqual(budget_usage(self.controller.state, "research"), before)
        self.assertNotIn("continuation", result)

    def test_research_tool_budget_cannot_prevent_its_final_solution_return(self):
        session = self.prepare()
        self.controller.config["max_tool_calls"] = 1
        with patch.object(session.client, "request", side_effect=[
                response(("read", "read_system", {"pointer": "/name"})),
                response(("return", "return_solution", SOLUTION))]) as model:
            result = session.run()
        self.assertEqual(model.call_count, 2)
        self.assertEqual(result["solution_return"]["solution"], SOLUTION)
        self.assertEqual(model.call_args.args[1], [RETURN_SOLUTION])

    def test_publication_returns_before_potentially_long_trusted_evaluation(self):
        session = self.prepare()
        raw = blank_system().to_data()
        raw["rules"] = [{"id": "bounded.open", "priority": 1,
            "when": {"op": "not", "args": [{"op": "has_contract_bid"}]}, "action": {"kind": "call", "call": "1C"}}]
        self.assertTrue(session.tools.execute("edit", "edit_system", {
            "edits": [{"op": "replace", "path": "", "value": raw}]})["ok"])
        cases = session.tools.execute("case", "add_cases", {"cases": [{"name": "bounded", "board": research.BOARD}]})
        session.tools.execute("tests", "write_tests", {"tests": [{"name": "real-opening",
            "case_id": cases["cases"][0]["case_id"], "mode": "decision", "changed_from_parent": True,
            "assertions": [{"path": "/call", "op": "eq", "value": "1C"}]}]})
        self.controller.evaluator = Mock()
        with patch.object(self.controller, "process_evaluations") as evaluate:
            with patch.object(session.client, "request", side_effect=[
                    response(("publish", "publish_candidate", {"hypothesis": "Check the actual bounded opening."})),
                    response(("return", "return_solution", SOLUTION))]):
                result = session.run()
            evaluate.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["solution_return"]["solution"], SOLUTION)
        self.assertEqual(len(result["releases"]), 1)
        self.assertTrue(self.controller.state["evaluation_queue"])


if __name__ == "__main__":
    unittest.main()
