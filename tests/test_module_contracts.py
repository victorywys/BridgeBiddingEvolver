import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_evolve.core import Seat
from bridge_evolve.evolution.config import ROOT
from bridge_evolve.evolution.contracts import ACTIVE, ModuleContract
from bridge_evolve.evolution.config import blank_system
from bridge_evolve.evolution.controller import Controller, initialize
from bridge_evolve.evolution.proposals import evidence_packet
from bridge_evolve.evolution.research_tools import ResearchTools
from bridge_evolve.systems import load_system, validate_system
from bridge_evolve.systems.inputs import parse_board, run_auction

CONTRACT = ROOT / "systems/nt-module-v1/contract.json"


class ModuleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(CONTRACT.read_text())
        cls.anchor = load_system(CONTRACT.parent / cls.data["anchor"])
        cls.contract = ModuleContract(cls.data, cls.anchor)
        cls.pass_system = load_system(ROOT / "systems/pass-v2.json")

    def test_all_finite_strata_and_interference_interfaces_run_with_declared_scope(self):
        result = self.contract.check(self.anchor)
        self.assertEqual(result["status"], "passed", result["failures"])
        self.assertEqual((result["passed"], result["cases"]), (42, 42))
        self.assertEqual(result["counterfactual_interference_probes"], 5)
        self.assertEqual(set(result["covered_shape_groups"].values()), {7})
        self.assertIn("not exhaustive", result["scope"])

    def test_isolation_does_not_change_the_original_naturally_reached_calls(self):
        original = load_system(ROOT / "systems/research-z" / (self.data["source_system_hash"] + ".json"))
        for case in self.data["cases"]:
            board = parse_board(case["board"])
            side = board.dealer.partnership.value
            games = []
            for candidate in (original, self.anchor):
                systems = (candidate, self.pass_system) if side == "NS" else (self.pass_system, candidate)
                games.append(run_auction(*systems, board, compact=True))
            self.assertEqual([item["call"] for item in games[0]["auction"]],
                             [item["call"] for item in games[1]["auction"]], case["id"])

    def test_game_response_cannot_be_replaced_with_all_pass_and_called_coverage(self):
        raw = self.anchor.to_data()
        for rule in raw["rules"]:
            if rule["id"].startswith("nt15."):
                rule["action"] = {"kind": "call", "call": "P"}
        result = self.contract.check(validate_system(raw))
        self.assertEqual(result["status"], "failed")
        self.assertGreater(result["failure_count"], 0)

    def test_missing_coupled_answer_and_highest_priority_overlap_are_detected(self):
        raw = self.anchor.to_data()
        raw["rules"] = [rule for rule in raw["rules"] if not rule["id"].startswith("nt15.transfer.accept")]
        missing = self.contract.check(validate_system(raw))
        self.assertEqual(missing["status"], "failed")
        raw = self.anchor.to_data()
        original = next(rule for rule in raw["rules"] if rule["id"] == "nt15.nt.transfer_hearts")
        duplicate = copy.deepcopy(original)
        duplicate.update(id="nt15.overlapping", action={"kind": "call", "call": "2H"})
        duplicate["when"] = {"op": "and", "args": [duplicate["when"], True]}
        raw["rules"].append(duplicate)
        overlap = self.contract.check(validate_system(raw))
        self.assertEqual(overlap["status"], "failed")
        self.assertTrue(any("Conflicting highest-priority" in str(error) for error in overlap["failures"]))

    def test_new_coupled_states_and_calls_can_be_added_without_an_old_rule_id_whitelist(self):
        raw = self.anchor.to_data()
        finish = next(item for item in raw["protocol"]["states"]["nt15.nt"]["transitions"]
                      if item["id"] == "nt15.nt_finish")
        game_exit = copy.deepcopy(finish)
        game_exit.update(id="nt15.restructured_game_exit", to="nt15.new_exit", forcing="one_round")
        game_exit["when"] = {"op": "and", "args": [game_exit["when"],
            {"op": "ne", "args": [{"op": "context", "name": "event.call"}, "P"]}]}
        game_exit["set"][ACTIVE] = True
        finish["when"] = {"op": "and", "args": [finish["when"],
            {"op": "eq", "args": [{"op": "context", "name": "event.call"}, "P"]}]}
        raw["protocol"]["states"]["nt15.nt"]["transitions"].append(game_exit)
        raw["protocol"]["states"]["nt15.new_exit"] = {"otherwise": "nt15.new_exit", "transitions": [{
            "id": "new_exit_reply", "when": {"op": "and", "args": [
                {"op": "eq", "args": [{"op": "context", "name": "event.by"}, "ours"]},
                {"op": "eq", "args": [{"op": "context", "name": "event.call"}, "P"]}]},
            "to": "finished", "set": {ACTIVE: False}, "allow_pass": True, "forcing": "none"}]}
        raw["rules"].append({"id": "original-new-protocol-exit", "priority": 100,
            "when": {"op": "eq", "args": [{"op": "state"}, "nt15.new_exit"]},
            "action": {"kind": "call", "call": "P"}})
        result = self.contract.check(validate_system(raw))
        self.assertEqual(result["status"], "passed", result["failures"])
        self.assertIn("nt15.new_exit", result["visited_module_states"])

    def test_false_public_guarantees_and_unannounced_exits_are_detected(self):
        raw = self.anchor.to_data()
        transition = next(item for item in raw["protocol"]["states"]["nt15.nt"]["transitions"]
                          if item["to"] == "nt15.transfer_accept")
        transition["set"]["captain_H_min"] = 7
        result = self.contract.check(validate_system(raw))
        self.assertEqual(result["status"], "failed")
        raw = self.anchor.to_data()
        transition = raw["protocol"]["states"]["nt15.nt"]["transitions"][0]
        transition["set"][ACTIVE] = True
        result = self.contract.check(validate_system(raw))
        self.assertTrue(any("clear the module" in str(error) for error in result["failures"]))

    def test_exterior_opening_changes_and_shared_helper_changes_are_rejected(self):
        raw = self.anchor.to_data()
        raw["rules"][0]["action"] = {"kind": "call", "call": "P"}
        self.assertEqual(self.contract.check(validate_system(raw))["status"], "failed")
        raw = self.anchor.to_data()
        raw["features"]["total_low"] = 40
        self.assertEqual(self.contract.check(validate_system(raw))["status"], "failed")

    def test_frozen_anchor_cases_and_module_only_origin_are_enforced_at_initialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = initialize({"profile": "smoke", "origins": ["module"],
                "module_contract": str(CONTRACT), "source": "research"}, Path(temporary) / "run")
            controller = Controller(directory)
            self.assertEqual(controller.state["champions"], {"Z": self.anchor.content_hash})
            original = (directory / "module-contract.json").read_text()
            (directory / "module-contract.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "input identity"):
                Controller(directory)
            (directory / "module-contract.json").write_text(original)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "origins"):
                initialize({"profile": "smoke", "origins": ["existing"], "module_contract": str(CONTRACT),
                            "source": "research"}, Path(temporary) / "invalid")

    def test_missing_or_weakened_probe_categories_cannot_be_reported_as_complete_coverage(self):
        data = copy.deepcopy(self.data)
        data["cases"].pop()
        with self.assertRaisesRegex(ValueError, "missing a required"):
            ModuleContract(data, self.anchor)
        data = copy.deepcopy(self.data)
        positive = next(case for case in data["cases"] if case["require_game"])
        positive["require_game"] = False
        with self.assertRaisesRegex(ValueError, "weakened"):
            ModuleContract(data, self.anchor)

    def test_research_tools_expose_read_only_contract_and_reject_missing_module(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = initialize({"profile": "smoke", "origins": ["module"],
                "module_contract": str(CONTRACT), "source": "research"}, Path(temporary) / "run")
            controller = Controller(directory)
            packet = evidence_packet(self.anchor, [], ["*"])
            tools = ResearchTools(controller, "g001-Z", self.anchor.content_hash, packet, self.anchor.to_data(), None)
            self.assertEqual(tools.execute("check", "run_contract", {})["status"], "passed")
            self.assertNotIn("cases", tools.execute("interface", "read_contract", {"view": "interface"}))
            self.assertEqual(tools.execute("cases", "read_contract", {"view": "cases", "limit": 2})["total"], 42)
            raw = self.anchor.to_data()
            raw["rules"][0]["action"] = {"kind": "call", "call": "P"}
            tools.data["system"] = raw
            self.assertFalse(tools.execute("bad-publish", "publish_candidate", {"hypothesis": "invalid outside change"})["ok"])

    def test_controlled_research_seeds_only_consumed_diagnostic_cases_and_disables_confirmation(self):
        from bridge_evolve.evolution.research import ResearchSession
        with tempfile.TemporaryDirectory() as temporary:
            directory = initialize({"profile": "smoke", "origins": ["module"], "module_contract": str(CONTRACT),
                "source": "research", "allow_promotion": False}, Path(temporary) / "run")
            controller = Controller(directory)
            packet = evidence_packet(self.anchor, [], ["*"])
            with patch("bridge_evolve.evolution.research.ModelClient") as model:
                session = ResearchSession(controller, 1, "Z", self.anchor.content_hash, packet)
            self.assertEqual(len(session.tools.data["cases"]), 42)
            self.assertEqual(session.tools.data["lab_boards"], 0)
            model.return_value.request.assert_not_called()
            entry = {"hash": "candidate", "parent": self.anchor.content_hash, "lineage": "Z"}
            controller.confirm_candidate(1, {"promotions": {}}, entry)
            self.assertEqual(entry["status"], "development_only")
            self.assertEqual(controller.state["promotion_attempts"], 0)

    def test_new_minor_protocol_can_publish_with_a_natural_witness_and_real_contract_gate(self):
        from bridge_evolve.evolution.research import ResearchSession
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            opponents = []
            for name in ("A", "B"):
                data = blank_system().to_data()
                data["name"] = name
                path = folder / (name + ".json")
                path.write_text(json.dumps(data))
                opponents.append(str(path))
            directory = initialize({"profile": "smoke", "origins": ["module"], "module_contract": str(CONTRACT),
                "baselines": opponents, "source": "research", "max_pairs": 1000, "max_seconds": 600}, folder / "run")
            controller = Controller(directory)
            session = ResearchSession(controller, 1, "Z", self.anchor.content_hash,
                                      evidence_packet(self.anchor, [], ["*"]))
            tools = session.tools
            raw = self.anchor.to_data()
            raw["rules"].append({"id": "nt15.new_weak_minor", "priority": 645, "when": {"op": "and", "args": [
                {"op": "eq", "args": [{"op": "state"}, "nt15.nt"]},
                {"op": "lt", "args": [{"op": "hcp"}, 8]},
                {"op": "ge", "args": [{"op": "length", "suit": "C"}, 6]}]},
                "action": {"kind": "call", "call": "3C"}})
            state = raw["protocol"]["states"]["nt15.nt"]
            rejected = next(item for item in state["transitions"] if item["id"] == "nt15.nt_unsupported_exit")
            rejected["when"] = {"op": "and", "args": [rejected["when"],
                {"op": "ne", "args": [{"op": "context", "name": "event.call"}, "3C"]}]}
            state["transitions"].append({"id": "new_weak_minor_end", "when": {"op": "and", "args": [
                {"op": "eq", "args": [{"op": "context", "name": "event.by"}, "ours"]},
                {"op": "eq", "args": [{"op": "context", "name": "event.call"}, "3C"]}]},
                "to": "finished", "set": {ACTIVE: False, "responder_C_min": 6}, "forcing": "none"})
            tools.data["system"] = raw
            case = next(case for case in self.contract.cases() if case["id"] == "hcp0-6-long_C")
            tools.data["tests"] = [{"name": "natural-minor", "mode": "auction", "case_id": case["case_id"],
                "side": case["side"], "changed_from_parent": True,
                "assertions": [{"path": "/auction/2/call", "op": "eq", "value": "3C"}]}]
            result = tools.execute("publish", "publish_candidate", {"hypothesis": "Test-only coherent minor protocol change"})
            self.assertTrue(result["ok"], result)
            job = controller.evaluations.jobs[result["evaluation"]["id"]]
            self.assertEqual(job["research_resources_at_publication"]["requests"], 0)
            self.assertEqual(job["status"], "queued")
            self.assertEqual(tools.data["last_contract"]["passed"], 42)

    def test_module_diagnosis_follows_states_instead_of_assuming_traditional_rule_names(self):
        from unittest.mock import patch
        record = {"test_only": True, "dealer": "N", "vulnerability": "none", "imp": -1,
            "hand_summary": {}, "hands": {}, "tables": [{"systems": {"NS": {"hash": self.anchor.content_hash}},
            "auction": [{"seat": "N", "call": "2C", "state": "nt15.nt",
                         "private_trace": {"rule_id": "entirely.new.name"}}]}]}
        with patch("bridge_evolve.evolution.proposals.agent_board", return_value={}), \
             patch("bridge_evolve.evolution.proposals.agent_decision", return_value={}):
            packet = evidence_packet(self.anchor, [record], ["*"], state_prefix="nt15.")
        self.assertEqual(packet["total_groups"], 1)
        self.assertEqual(packet["selected_groups"][0]["rule_id"], "entirely.new.name")


if __name__ == "__main__":
    unittest.main()
