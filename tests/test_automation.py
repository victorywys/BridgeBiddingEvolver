import io
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch

from bridge_evolve.automation import (
    ProcessOutcome,
    _project_lock,
    main,
    run_iterations,
    run_process,
)
from bridge_evolve.dev_sandbox import container_command

TEST_KEY = "test-key-that-must-not-appear-in-prompts-or-command-arguments"


class DevelopmentIterationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="bridge-iteration-test-")
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        (self.project / "AGENTS.md").write_text("Test project constraints.")
        (self.project / "pyproject.toml").write_text("[project]\nname = 'test'")
        executor = self.project / "tools/copilot-api/node_modules/.bin/codex"
        executor.parent.mkdir(parents=True)
        executor.touch()
        self.gateway = patch("bridge_evolve.automation._gateway_key", return_value=TEST_KEY).start()
        self.addCleanup(patch.stopall)
        patch("bridge_evolve.automation.ensure_image", return_value="test-image").start()
        patch("bridge_evolve.automation.codex_vendor", return_value=self.project / "vendor").start()
        patch("bridge_evolve.dev_sandbox.remove_container").start()

    def run_with(self, execute, **options):
        with patch("bridge_evolve.automation.run_process", side_effect=execute):
            manifest = run_iterations(self.project, **options)
        return json.loads(manifest.read_text()), manifest

    def successful_process(self, command, **options):
        self.assertNotIn(TEST_KEY, " ".join(command))
        if options["prompt"] is None:
            self.assertNotIn("BRIDGE_GATEWAY_API_KEY", options["environment"])
            self.assertIn(command[-1], ("verify", "/workspace/project/acceptance.py"))
        self.assertEqual(command[0], "docker")
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("--read-only", command)
        options["stdout"].write_text("Verification output.")
        options["stderr"].write_text("")
        return ProcessOutcome(0)

    def test_model_completion_and_passing_tests_do_not_short_circuit_the_budget(self) -> None:
        prompts = []

        def execute(command, **options):
            if options["prompt"] is not None:
                prompts.append(options["prompt"])
            return self.successful_process(command, **options)

        state, manifest = self.run_with(execute, max_rounds=2)
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(len(prompts), 2)
        self.assertIn("No independent completion checker", prompts[1])
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)

    def test_verification_failure_is_carried_to_the_next_round_and_keys_are_redacted(self) -> None:
        prompts = []
        verification_count = 0

        def execute(command, **options):
            nonlocal verification_count
            if options["prompt"] is not None:
                prompts.append(options["prompt"])
                return self.successful_process(command, **options)
            verification_count += 1
            options["stdout"].write_text("")
            options["stderr"].write_text(f"Failed boundary check. {TEST_KEY}")
            return ProcessOutcome(1 if verification_count == 1 else 0)

        state, _ = self.run_with(execute, max_rounds=2)
        self.assertEqual(state["rounds"][0]["verification_returncode"], 1)
        self.assertIn("Failed boundary check", prompts[1])
        self.assertNotIn(TEST_KEY, prompts[1])
        self.assertIn("[REDACTED_GATEWAY_KEY]", prompts[1])

    def test_only_a_frozen_external_completion_check_can_confirm_completion(self) -> None:
        checker = self.project / "acceptance.py"
        checker.write_text("raise SystemExit(0)")
        state, _ = self.run_with(
            self.successful_process, max_rounds=3, completion_check=checker
        )
        self.assertEqual(state["status"], "confirmed")
        self.assertEqual(len(state["rounds"]), 1)
        self.assertEqual(state["rounds"][0]["completion_returncode"], 0)
        self.assertIsNotNone(state["completion_hash"])

    def test_mutating_the_completion_checker_invalidates_the_run(self) -> None:
        checker = self.project / "acceptance.py"
        checker.write_text("raise SystemExit(1)")

        def execute(command, **options):
            if options["prompt"] is not None:
                checker.write_text("raise SystemExit(0)")
            return self.successful_process(command, **options)

        with self.assertRaisesRegex(RuntimeError, "checker changed"):
            self.run_with(execute, completion_check=checker)
        manifest = next((self.project / ".runtime/iterations").glob("*/run.json"))
        self.assertEqual(json.loads(manifest.read_text())["status"], "failed")

    def test_executor_errors_stop_without_retrying_or_running_verification(self) -> None:
        calls = []

        def execute(command, **options):
            calls.append(command)
            return ProcessOutcome(1)

        state, _ = self.run_with(execute)
        self.assertEqual(state["status"], "executor_failed")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(state["rounds"][0]["verification_returncode"])

    def test_executor_timeouts_stop_the_run(self) -> None:
        state, _ = self.run_with(lambda *args, **kwargs: ProcessOutcome(-15, True))
        self.assertEqual(state["status"], "timed_out")
        self.assertEqual(len(state["rounds"]), 1)

    def test_simultaneous_controllers_are_rejected(self) -> None:
        runtime = self.project / ".runtime"
        runtime.mkdir()
        with _project_lock(runtime):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                run_iterations(self.project)

    def test_completion_check_must_be_outside_writable_paths(self) -> None:
        checker = self.project / "tests/core/acceptance.py"
        checker.parent.mkdir(parents=True)
        checker.write_text("raise SystemExit(0)")
        with self.assertRaisesRegex(ValueError, "executor-writable"):
            run_iterations(self.project, completion_check=checker)
        self.gateway.assert_not_called()

    def test_relative_completion_checks_resolve_against_the_selected_project(self) -> None:
        (self.project / "acceptance.py").write_text("raise SystemExit(0)")
        state, _ = self.run_with(
            self.successful_process, completion_check=Path("acceptance.py")
        )
        self.assertEqual(state["status"], "confirmed")
        self.assertEqual(state["completion_check"], str(self.project / "acceptance.py"))

    def test_completion_checks_in_excluded_directories_are_rejected(self) -> None:
        checker = self.project / ".runtime/acceptance.py"
        checker.parent.mkdir()
        checker.write_text("raise SystemExit(0)")
        with self.assertRaisesRegex(ValueError, "credential-free snapshot"):
            run_iterations(self.project, completion_check=checker)
        self.gateway.assert_not_called()

    def test_invalid_budgets_are_rejected_before_model_access(self) -> None:
        for options in (
            {"max_rounds": 0}, {"max_rounds": 21}, {"round_timeout": 3601}, {"max_requests": 0},
        ):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    run_iterations(self.project, **options)
        self.gateway.assert_not_called()

    def test_container_has_no_network_or_credentials_and_verification_mounts_are_read_only(self) -> None:
        with container_command(
            self.project, self.project / "snapshot", "test-image", self.project / "vendor",
            "owned-test-container", "verify", [],
        ) as command:
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertIn("--read-only", command)
            self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
            self.assertEqual(command[command.index("--security-opt") + 1], "no-new-privileges")
            self.assertEqual(command[1:3], ["--host", "unix:///var/run/docker.sock"])
            client_config = Path(command[command.index("--config") + 1])
            self.assertEqual(list(client_config.iterdir()), [])
            mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
            self.assertTrue(all("readonly" in mount for mount in mounts))
            self.assertFalse(any(".runtime" in mount for mount in mounts))
            self.assertNotIn(TEST_KEY, " ".join(command))
        self.assertFalse(client_config.exists())

    def test_real_subprocess_timeout_terminates_the_owned_executor(self) -> None:
        outcome = run_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=self.project, environment={"PATH": os.defpath}, prompt=None,
            stdout=self.project / "timeout.stdout", stderr=self.project / "timeout.stderr",
            timeout=1,
        )
        self.assertTrue(outcome.timed_out)
        self.assertNotEqual(outcome.returncode, 0)

    def test_cli_interrupt_restores_the_signal_handler_and_reports_failure(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        error_output = io.StringIO()
        with (
            patch("bridge_evolve.automation.run_iterations", side_effect=KeyboardInterrupt),
            patch("bridge_evolve.automation.logging.basicConfig"),
            redirect_stderr(error_output),
        ):
            self.assertEqual(main(["--project", str(self.project)]), 130)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertIn("interrupted", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
