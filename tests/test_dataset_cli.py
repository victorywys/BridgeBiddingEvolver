import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DatasetCLITests(unittest.TestCase):
    def test_generate_show_and_invalid_index_have_explicit_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def run(*args):
                return subprocess.run([sys.executable, "-m", "bridge_evolve", *map(str, args)],
                                      cwd=root, text=True, capture_output=True, check=False)
            created = run("dataset", "generate", "--output", root / "data",
                          "--count", 16, "--seed", 42, "--split", "train")
            self.assertEqual(created.returncode, 0, created.stderr)
            manifest = json.loads(created.stdout)["manifest"]
            shown = run("dataset", "show", "--dataset", manifest, "--index", 0)
            self.assertEqual(shown.returncode, 0, shown.stderr)
            board = json.loads(shown.stdout)
            self.assertEqual(set(board), {"dealer", "vulnerability", "hands"})
            self.assertEqual(set(board["hands"]), set("NESW"))
            invalid = run("dataset", "show", "--dataset", manifest, "--index", 16)
            self.assertEqual(invalid.returncode, 1)
            self.assertEqual(invalid.stdout, "")
            self.assertIn("outside", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
