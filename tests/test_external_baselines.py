import json
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch

from bridge_evolve.core import Deal, Seat, Vulnerability
from bridge_evolve.evaluation.data import generate_dataset, load_dataset
from bridge_evolve.evaluation.experiment import evaluate, paired_board
from bridge_evolve.oracle import DDSAdapter
from bridge_evolve.systems import decide, load_system
from bridge_evolve.systems.external import ExternalPolicy, SCHEMA, _request, file_hash, open_player
from bridge_evolve.systems.inputs import parse_board, parse_observation, run_auction
from bridge_evolve.systems.runtime import _decide_and_advance
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld, public_context

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/external_worker.py"


def observation(history=(), dealer="N", vulnerability="none"):
    seat = "NESW"[("NESW".index(dealer) + len(history)) % 4]
    return parse_observation({"hand": "AKQ.JT9.KQ32.876", "seat": seat, "dealer": dealer,
                              "vulnerability": vulnerability, "history": list(history)})


class ExternalBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        weights = self.root / "test-only-weights"
        weights.write_bytes(b"not-a-real-model")
        for target, value in (("source_identity", ("a" * 40, {"test.py": "b" * 64})), ("WORKER", FIXTURE)):
            patcher = patch("bridge_evolve.systems.external." + target, return_value=value) if target == "source_identity" else patch("bridge_evolve.systems.external." + target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.data = {"schema": SCHEMA, "name": "test-only fixture", "family": "pidgin", "python": sys.executable,
                     "source": str(self.root), "source_commit": "a" * 40, "source_files": {"test.py": "b" * 64},
                     "weights": str(weights), "weights_sha256": file_hash(weights),
                     "worker_sha256": file_hash(FIXTURE), "runtime": {"fixture": True},
                     "model_source": "test-only:no-real-fitness"}

    def test_worker_sees_only_visible_fields_and_no_inherited_secret(self):
        with patch.dict(os.environ, {"BRIDGE_TEST_SECRET": "not-a-real-credential"}):
            with ExternalPolicy(self.data) as player:
                process = player.process
                first, result = player.decide(observation())
                second, repeated = player.decide(observation())
                self.assertEqual((str(first), first, result), ("P", second, repeated))
                self.assertEqual(result["seen_fields"], sorted(
                    {"hand", "seat", "dealer", "vulnerability", "history", "legal_calls"}))
                self.assertFalse(result["secret_environment_present"])
        self.assertIsNotNone(process.poll())

    def test_weights_source_runtime_and_adapter_changes_fail_explicitly(self):
        for key, value in (("weights_sha256", "0" * 64), ("worker_sha256", "0" * 64),
                           ("source_commit", "c" * 40), ("source_files", {})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                ExternalPolicy({**self.data, key: value})
        player = ExternalPolicy({**self.data, "runtime": {"fixture": False}})
        with self.assertRaisesRegex(ValueError, "runtime"):
            player.__enter__()
        self.assertIsNone(player.process)

    def test_illegal_call_or_wrong_family_is_never_replaced_with_pass(self):
        for result in ({"ok": True, "call": "1C", "model_family": "pidgin"},
                       {"ok": True, "call": "P", "model_family": "brl"}):
            with ExternalPolicy(self.data) as player:
                with patch.object(player, "_receive", return_value=result):
                    with self.assertRaisesRegex(ValueError, "failed") as caught:
                        player.decide(observation(["7NT"]))
                self.assertEqual(caught.exception.system_hash, player.content_hash)
                self.assertEqual(caught.exception.actor, "E")
                self.assertIsNone(player.process)

    def test_path_or_label_changes_do_not_change_model_identity_but_semantics_do(self):
        a = ExternalPolicy(self.data)
        b = ExternalPolicy({**self.data, "name": "different display label"})
        self.assertEqual(a.content_hash, b.content_hash)
        data = a.to_data()
        data["runtime"]["fixture"] = False
        self.assertEqual(a.to_data()["runtime"], {"fixture": True})
        self.assertNotEqual(a.content_hash, ExternalPolicy(data).content_hash)
        descriptor = self.root / "policy.json"
        descriptor.write_text(json.dumps(self.data))
        with open_player(descriptor) as player:
            self.assertEqual(player.content_hash, a.content_hash)

    def test_mixed_auction_preserves_ir_and_marks_external_explanation_unavailable(self):
        rule = load_system(ROOT / "systems/pass-v2.json")
        board = parse_board(json.loads((ROOT / "examples/general-board.json").read_text()))
        with ExternalPolicy(self.data) as player:
            result = run_auction(rule, player, board, trace=True)
            self.assertEqual(result["contract"], {"passed_out": True})
            external = [step for step in result["auction"] if step["source"] == "external"]
            self.assertEqual(len(external), 2)
            self.assertTrue(all(step["explanation_available"] is False for step in external))
            self.assertTrue(all(step["private_trace"]["rule_id"] == "external:pidgin" for step in external))
        self.assertEqual(set(result["partnership_contexts"]), {"NS"})

    def test_explicit_black_box_has_no_guarantees_but_missing_definition_still_errors(self):
        rule = load_system(ROOT / "systems/pass-v2.json")
        obs = observation(["P", "1C"])
        obs = replace(obs, public_definitions=(("NS", rule),), opaque_sides=("EW",))
        values = public_context(rule, obs)["values"]
        self.assertEqual(values["opponent_shown_suit"], "")
        self.assertEqual(str(decide(rule, obs).call), "P")
        missing = public_context(rule, replace(obs, opaque_sides=()))["values"]
        self.assertIn("opponent_shown_suit", missing["unavailable"])
        unknown = PublicWorld({"NS": rule}, obs.dealer, obs.vulnerability)
        for call in obs.history:
            unknown.step(call)
        with self.assertRaisesRegex(ValueError, "public definition"):
            PublicContext(unknown, "NS", obs.seat).value("opponent_shown_suit")
        world = PublicWorld({"NS": rule}, obs.dealer, obs.vulnerability, opaque_sides=("EW",))
        for call in obs.history:
            world.step(call)
        with self.assertRaisesRegex(ValueError, "does not match"):
            _decide_and_advance(rule, replace(obs, opaque_sides=()), world)

    def test_other_hands_do_not_enter_worker_request(self):
        board = parse_board(json.loads((ROOT / "examples/general-board.json").read_text()))
        swapped = replace(board, deal=Deal(board.deal.north, board.deal.west, board.deal.south, board.deal.east))
        first = replace(observation(), hand=board.deal.north)
        second = replace(first, hand=swapped.deal.north)
        self.assertEqual(_request(first), _request(second))
        for field in ("hands", "dds", "seed", "board_id", "private_trace"):
            self.assertNotIn(field, _request(first))
        for sides in (["NS", "NS"], ["NS"], ["invalid"]):
            with self.subTest(sides=sides), self.assertRaises(ValueError):
                replace(first, public_definitions=(("NS", load_system(ROOT / "systems/pass-v2.json")),),
                        opaque_sides=tuple(sides))


class RealExternalBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = os.environ.get("BRIDGE_BASELINE_DIRECTORY")
        if not directory:
            if os.environ.get("BRIDGE_BASELINES_REQUIRED") == "1":
                raise RuntimeError("BRIDGE_BASELINE_DIRECTORY is required for real external baseline validation.")
            raise unittest.SkipTest("Optional neural baselines require explicit BRIDGE_BASELINE_DIRECTORY.")
        cls.stack = ExitStack()
        cls.addClassCleanup(cls.stack.close)
        cls.brl = cls.stack.enter_context(open_player(Path(directory) / "policies/brl-fsp.json"))
        cls.pidgin = cls.stack.enter_context(open_player(Path(directory) / "policies/pidgin-v3.1.json"))
        cls.directory = Path(directory)

    def test_all_dealers_vulnerabilities_doubles_redoubles_and_7nt_map_legally(self):
        for player in (self.brl, self.pidgin):
            for dealer in Seat:
                for vulnerability in Vulnerability:
                    for history in ((), ("P",), ("1C", "X", "XX"), ("7NT", "X", "XX")):
                        obs = observation(history, dealer.value, vulnerability.value)
                        call, result = player.decide(obs)
                        repeated, again = player.decide(obs)
                        self.assertIn(call, obs.legal_actions)
                        self.assertEqual((call, result), (repeated, again))
                        self.assertEqual(len(result["policy"]), 38)
                        self.assertAlmostEqual(sum(result["policy"]), 1, places=5)

    def test_real_dds_self_match_zero_swap_negation_and_hidden_hand_invariance(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = generate_dataset(Path(temporary) / "data", count=2, seed=72051, split="validation")
            _, records = load_dataset(path)
            oracle = DDSAdapter()
            self.assertEqual(oracle.provenance.to_dict()["oracle_kind"], "dds")
            for row in records:
                board, table = row.board, oracle.solve(row.board.deal)
                forward = paired_board(self.brl, self.pidgin, board, table)
                backward = paired_board(self.pidgin, self.brl, board, table)
                self.assertEqual(forward["imp_a"], -backward["imp_a"])
                for player in (self.brl, self.pidgin):
                    self.assertEqual(paired_board(player, player, board, table)["imp_a"], 0)
                    for dealer in Seat:
                        own = board.deal.hand(dealer)
                        a = replace(observation(dealer=dealer.value), hand=own)
                        b = replace(a, public_systems=("different public description not encoded by this model",))
                        self.assertEqual(player.decide(a), player.decide(b))

    def test_existing_evaluate_and_offline_viewer_zip_keep_neural_meanings_opaque(self):
        from test_board_viewer import viewer
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = generate_dataset(root / "data", count=2, seed=72052, split="validation")
            config = {"experiment_id": "test-external-real", "dataset": str(dataset),
                      "system_a": str(ROOT / "systems/pass-v2.json"),
                      "system_b": str(self.directory / "policies/pidgin-v3.1.json"),
                      "dds_cache": str(root / "dds.sqlite"), "output": str(root / "run"),
                      "bootstrap_seed": 1, "bootstrap_resamples": 100}
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            report = evaluate(config_path)
            manifest = json.loads((report.parent / "run.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["external_baselines"], [self.pidgin.content_hash])
            with patch.object(ExternalPolicy, "__enter__", side_effect=AssertionError("Viewer must not execute a policy")):
                bundle = viewer.load_run(report.parent)
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w") as output:
                    for path in report.parent.rglob("*"):
                        if path.is_file():
                            output.write(path, path.relative_to(report.parent).as_posix())
                imported = viewer.import_file(archive.getvalue(), "external-baseline.zip")
            self.assertEqual(bundle["statistics"]["boards"], 2)
            self.assertEqual(imported["statistics"]["boards"], 2)
            for record in imported["boards"]:
                for table in record["tables"]:
                    for step in table["auction"]:
                        if step.get("source") == "external":
                            meaning = step["public_explanation"]
                            self.assertEqual(meaning["basis"], "opaque-external-policy")
                            self.assertEqual(meaning["new_information"], [])


if __name__ == "__main__":
    unittest.main()
