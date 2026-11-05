import json
import unittest
from pathlib import Path

from bridge_evolve.systems import load_system, render_system
from bridge_evolve.systems.inputs import parse_board

ARCHIVE = Path(__file__).resolve().parents[1] / "systems/research-z"


class PublishedArchiveTests(unittest.TestCase):
    def test_frozen_protocols_and_specifications_have_the_original_identities(self):
        manifest = json.loads((ARCHIVE / "index.json").read_text())
        self.assertEqual(len(manifest["snapshots"]), 3)
        for item in manifest["snapshots"]:
            with self.subTest(identity=item["hash"]):
                system = load_system(ARCHIVE / item["ir"])
                self.assertEqual(system.content_hash, item["hash"])
                self.assertEqual(len(system.rules), item["rules"])
                self.assertEqual(render_system(system), (ARCHIVE / item["specification"]).read_text())

    def test_published_tests_have_all_required_cases_without_local_runs(self):
        manifest = json.loads((ARCHIVE / "index.json").read_text())
        releases = [item for item in manifest["snapshots"] if "release" in item]
        self.assertEqual([item["tests"] for item in releases], [48, 72])
        for item in releases:
            release = json.loads((ARCHIVE / item["release"]).read_text())
            self.assertEqual(release["hash"], item["hash"])
            self.assertEqual(release["parent"], item["parent"])
            self.assertEqual(len(release["test_definitions"]), item["tests"])
            self.assertEqual(release["tests"]["failed"], 0)
            for test in release["test_definitions"]:
                self.assertIn(test["case_id"], release["cases"])
            for case in release["cases"].values():
                parse_board(case["board"])


if __name__ == "__main__":
    unittest.main()
