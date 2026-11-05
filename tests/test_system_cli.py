import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = ROOT / "systems/1nt-v1.json"


class SystemCLITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="bridge-system-cli-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "bridge_evolve", *map(str, arguments)],
            cwd=self.directory, capture_output=True, text=True, timeout=20, check=False,
        )

    def test_validate_reports_actual_coverage_limitations(self):
        result = self.run_cli("system", "validate", SYSTEM)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(len(report["system_hash"]), 64)
        self.assertIn("not an exhaustive proof", report["limitations"])

    def test_render_to_stdout_and_file_is_identical(self):
        target = self.directory / "system.md"
        stdout = self.run_cli("system", "render", SYSTEM)
        saved = self.run_cli("system", "render", SYSTEM, "--output", target)
        self.assertEqual(stdout.returncode, 0, stdout.stderr)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertEqual(target.read_text(), stdout.stdout)
        self.assertIn("首轮控制", stdout.stdout)
        self.assertIn("transfer_spades", stdout.stdout)
        self.assertIn("兜底", stdout.stdout)
        self.assertIn("h_transfer_accept", stdout.stdout)

    def test_decision_and_trace_are_separate_from_public_explanation(self):
        arguments = ("system", "decide", SYSTEM, "--observation", ROOT / "examples/observation.json")
        public = self.run_cli(*arguments)
        private = self.run_cli(*arguments, "--trace")
        self.assertEqual(public.returncode, 0, public.stderr)
        self.assertEqual(private.returncode, 0, private.stderr)
        a, b = json.loads(public.stdout), json.loads(private.stdout)
        self.assertEqual(a["call"], "2H")
        self.assertEqual(a["public_meaning"], b["public_meaning"])
        self.assertNotIn("private_trace", a)
        self.assertEqual(b["private_trace"]["rule_id"], "transfer_spades")

    def test_full_board_auction_returns_contract_without_fake_dds(self):
        result = self.run_cli(
            "auction", "run", "--system", SYSTEM, "--board", ROOT / "examples/board.json", "--trace",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["contract"]["strain"], "S")
        self.assertEqual(report["contract"]["declarer"], "N")
        self.assertEqual(report["evaluation"], "not_evaluated")

    def test_browse_exposes_small_catalog_and_contextual_hand_queries(self):
        system = ROOT / "systems/general-v2.json"
        result = self.run_cli("system", "browse", system)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["view"], "catalog")
        request = self.directory / "query.json"
        request.write_text(json.dumps({"view": "hand", "observation":
                                      json.loads((ROOT / "examples/general-observation.json").read_text())}))
        result = self.run_cli("system", "browse", system, "--request", request)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["decision"]["call"], "5H")
        self.assertIn("source_keys", report["public_meaning"])
        request.write_text(json.dumps({"view": "diff", "kind": "rule"}))
        result = self.run_cli("system", "browse", system, "--request", request, "--compare-to", system)
        self.assertEqual(json.loads(result.stdout)["changes"]["total"], 0)

    def test_general_mode_runs_two_systems_and_can_continue_a_prefix(self):
        prefix = self.directory / "prefix.json"
        prefix.write_text(json.dumps(["1S", "P", "3S", "P", "4NT", "P"]))
        result = self.run_cli(
            "auction", "run", "--mode", "general",
            "--ns", ROOT / "systems/general-v2.json", "--ew", ROOT / "systems/pass-v2.json",
            "--board", ROOT / "examples/general-board.json", "--prefix", prefix, "--trace",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["mode"], "general")
        self.assertEqual(report["auction"][6]["call"], "5H")
        self.assertEqual(report["contract"]["strain"], "S")
        self.assertEqual(report["contract"]["level"], 6)
        self.assertEqual(report["auction"][0]["source"], "prefix")
        self.assertEqual(report["evaluation"], "not_evaluated")

    def test_v2_context_decide_validate_and_specification_are_consistent(self):
        system = ROOT / "systems/general-v2.json"
        observation = ROOT / "examples/general-observation.json"
        context = self.run_cli("system", "context", system, "--observation", observation)
        self.assertEqual(context.returncode, 0, context.stderr)
        values = json.loads(context.stdout)
        self.assertEqual(values["state"], "keycards")
        self.assertEqual(values["values"]["agreed_trump"], "S")
        self.assertTrue(values["values"]["opponents_vulnerable"])
        self.assertEqual(values["values"]["forcing"], "game")
        self.assertTrue(values["values"]["must_bid"])
        decision = self.run_cli("system", "decide", system, "--observation", observation)
        self.assertEqual(decision.returncode, 0, decision.stderr)
        self.assertEqual(json.loads(decision.stdout)["call"], "5H")
        self.assertNotIn("private_trace", json.loads(decision.stdout))
        validated = self.run_cli("system", "validate", system)
        self.assertEqual(json.loads(validated.stdout)["schema_version"], 2)
        rendered = self.run_cli("system", "render", system)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("schema_version=2", rendered.stdout)
        for phrase in ("公开声明", "keycards_25_with_queen", "one_round", "trump"):
            self.assertIn(phrase, rendered.stdout)

    def test_general_and_local_cli_modes_cannot_silently_mix(self):
        for arguments in (
            ("--mode", "general", "--system", SYSTEM),
            ("--system", SYSTEM, "--ns", SYSTEM),
            ("--mode", "general", "--ns", SYSTEM, "--ew", ROOT / "systems/pass-v2.json"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli("auction", "run", *arguments, "--board", ROOT / "examples/board.json")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)

    def test_single_hand_cli_accepts_static_opponent_definitions_not_private_logs(self):
        raw = json.loads((ROOT / "systems/pass-v2.json").read_text())
        raw["announcements"] = [{
            "id": "strong_club", "when": {
                "op": "eq", "args": [{"op": "context", "name": "event.call"}, "1C"],
            }, "tags": ["artificial", "strong_club"],
        }]
        opponent = self.directory / "opponent.json"
        opponent.write_text(json.dumps(raw))
        defender_raw = json.loads((ROOT / "systems/pass-v2.json").read_text())
        defender_raw["rules"] = [{
            "id": "defense", "priority": 1,
            "when": {"op": "announced", "which": "opening", "tag": "strong_club"},
            "action": {"kind": "call", "call": "X"},
        }]
        defender = self.directory / "defender.json"
        defender.write_text(json.dumps(defender_raw))
        observation = self.directory / "observation.json"
        observation.write_text(json.dumps({
            "hand": "AKQ.JT9.KQ32.876", "seat": "E", "dealer": "N",
            "vulnerability": "none", "history": ["1C"],
        }))
        missing = self.run_cli("system", "decide", defender, "--observation", observation)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("public definition", missing.stderr)
        result = self.run_cli(
            "system", "decide", defender, "--observation", observation, "--public-ns", opponent,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["call"], "X")

    def test_invalid_system_observation_and_output_paths_fail_without_success_output(self):
        malformed = self.directory / "bad.json"
        for content in ("{}", "{", '{"schema_version":1,"schema_version":2}'):
            malformed.write_text(content)
            result = self.run_cli("system", "validate", malformed)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("error:", result.stderr)
        malformed.write_text(json.dumps({
            "hand": "JT987.AKQ2.AJ.T9", "seat": "S", "dealer": "N",
            "vulnerability": "none", "history": ["1NT", "P"], "dds": [13] * 20,
        }))
        result = self.run_cli("system", "decide", SYSTEM, "--observation", malformed)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown", result.stderr)
        result = self.run_cli("system", "render", SYSTEM, "--output", self.directory / "missing/file.md")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
