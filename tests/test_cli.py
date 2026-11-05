import io
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from bridge_evolve import __version__
from bridge_evolve.cli import main
from bridge_evolve.diagnostics import collect_diagnostics


class InstalledCLITests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="bridge-evolve-test-")
        self.addCleanup(directory.cleanup)
        self.working_directory = directory.name

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "bridge_evolve", *arguments],
            cwd=self.working_directory,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_help_from_outside_the_source_tree(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("doctor", result.stdout)
        self.assertIn("evolve", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_no_arguments_prints_help(self) -> None:
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: bridge-evolve", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_module_entry_point_version(self) -> None:
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"bridge-evolve {__version__}")
        self.assertEqual(result.stderr, "")

    def test_console_entry_point_is_installed(self) -> None:
        executable = shutil.which("bridge-evolve", path=sysconfig.get_path("scripts"))
        self.assertIsNotNone(executable, "Install the package before running the tests.")
        if executable is None:
            return
        result = subprocess.run(
            [executable, "--version"],
            cwd=self.working_directory,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"bridge-evolve {__version__}")

    def test_doctor_distinguishes_diagnostics_from_solver_availability(self) -> None:
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = collect_diagnostics()
        self.assertIn("DDS: " + ("available" if report.dds.available else "unavailable"), result.stdout)
        self.assertIn("DDS adapter: " + report.dds.adapter_status, result.stdout)
        self.assertEqual(result.stderr, "")

    def test_doctor_json_is_one_machine_readable_report(self) -> None:
        result = self.run_cli("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["package_version"], __version__)
        self.assertEqual(report["python_executable"], sys.executable)
        self.assertTrue(report["python_supported"])
        self.assertEqual(report["dds"]["available"], collect_diagnostics().dds.available)
        self.assertEqual(result.stderr, "")

    def test_require_dds_uses_real_availability_and_preserves_json_stdout(self) -> None:
        result = self.run_cli("doctor", "--json", "--require-dds")
        available = collect_diagnostics().dds.available
        self.assertEqual(result.returncode, 0 if available else 1)
        self.assertEqual(json.loads(result.stdout)["dds"]["available"], available)
        if available:
            self.assertEqual(result.stderr, "")
        else:
            self.assertIn("DDS is required but unavailable", result.stderr)

    def test_invalid_commands_cannot_report_success(self) -> None:
        for command in ("not-a-command",):
            with self.subTest(command=command):
                result = self.run_cli(command)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("invalid choice", result.stderr)

    def test_unknown_doctor_option_is_rejected(self) -> None:
        result = self.run_cli("doctor", "--pretend-dds")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("unrecognized arguments", result.stderr)

    def test_doctor_does_not_dump_environment_secrets(self) -> None:
        secret = "bridge-evolve-test-value-must-not-be-printed"
        with patch.dict(os.environ, {"BRIDGE_EVOLVE_TEST_SECRET": secret}):
            result = self.run_cli("doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn("BRIDGE_EVOLVE_TEST_SECRET", result.stdout + result.stderr)


class DoctorFailureTests(unittest.TestCase):
    def test_unsupported_python_fails_explicitly(self) -> None:
        report = replace(
            collect_diagnostics(), python_version="3.10.0", python_supported=False
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("bridge_evolve.cli.collect_diagnostics", return_value=report),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = main(["doctor", "--json"])
        self.assertEqual(exit_code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["python_supported"])
        self.assertIn("Python 3.11 or newer is required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
