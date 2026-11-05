"""Bounded development iteration through the local gateway, not bidding evolution."""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from bridge_evolve.dev_sandbox import (
    GatewayBridge,
    PERSISTENT_DIRECTORIES,
    codex_vendor,
    container_command,
    create_snapshot,
    ensure_image,
    snapshot_includes,
)

GATEWAY = "http://127.0.0.1:4141/v1"
WRITABLE_DIRECTORIES = PERSISTENT_DIRECTORIES
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    timed_out: bool = False


@dataclass
class RoundRecord:
    number: int
    executor_returncode: int
    verification_returncode: int | None = None
    completion_returncode: int | None = None
    timed_out: bool = False


@dataclass
class RunState:
    project: str
    model: str
    max_rounds: int
    round_timeout: int
    completion_check: str | None
    completion_hash: str | None
    image: str
    max_requests: int
    requests_used: int = 0
    backend: str = "docker"
    controller_pid: int = field(default_factory=os.getpid)
    schema_version: int = 1
    milestone: str = "M1"
    status: str = "running"
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    active_round: int | None = None
    rounds: list[RoundRecord] = field(default_factory=list)
    error: str | None = None


def _write_state(path: Path, state: RunState) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf8") as output:
        os.chmod(temporary, 0o600)
        json.dump(asdict(state), output, indent=2)
        output.write("\n")
    temporary.replace(path)


@contextmanager
def _project_lock(runtime: Path):
    path = runtime / "development-iteration.lock"
    with path.open("a", encoding="utf8") as lock:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another development controller is already running.") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    prompt: str | None,
    stdout: Path,
    stderr: Path,
    timeout: int,
) -> ProcessOutcome:
    with stdout.open("wb") as output, stderr.open("wb") as errors:
        os.chmod(stdout, 0o600)
        os.chmod(stderr, 0o600)
        with subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            start_new_session=True,
        ) as process:
            try:
                if prompt is not None and process.stdin is not None:
                    process.stdin.write(prompt.encode())
                    process.stdin.close()
                return ProcessOutcome(process.wait(timeout=timeout))
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                if isinstance(error, KeyboardInterrupt):
                    raise
                return ProcessOutcome(process.returncode, timed_out=True)


def _gateway_key(project: Path, model: str) -> str:
    path = project / ".runtime/copilot-api/gateway-client-key"
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Gateway client key permissions must be 0600.")
    key = path.read_text(encoding="utf8").strip()
    if not key:
        raise RuntimeError("The gateway client key is empty.")
    request = Request(f"{GATEWAY}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            models = json.load(response)["data"]
    except HTTPError as error:
        status = error.code
        error.close()
        raise RuntimeError(f"Gateway model discovery failed with HTTP {status}.") from None
    except URLError as error:
        raise RuntimeError("Cannot connect to the authenticated local gateway.") from error
    if not isinstance(models, list) or model not in [item["id"] for item in models]:
        raise RuntimeError(f"The requested model is not available: {model}")
    return key


def _checker_hash(project: Path, checker: Path | None) -> str | None:
    if checker is None:
        return None
    checker = checker.resolve()
    if not checker.is_relative_to(project):
        raise ValueError("The completion check must be a project-local Python file.")
    if checker.suffix != ".py" or not checker.is_file():
        raise ValueError("The completion check must be an existing Python file.")
    if not snapshot_includes(project, checker):
        raise ValueError("The completion check must be included in the credential-free snapshot.")
    if any(checker.is_relative_to(project / directory) for directory in WRITABLE_DIRECTORIES):
        raise ValueError("The completion check cannot be inside executor-writable paths.")
    return hashlib.sha256(checker.read_bytes()).hexdigest()


def _prompt(round_number: int, feedback: str) -> str:
    return f"""Continue Bridge Bidding Evolution development, round {round_number}.
Read AGENTS.md and docs/01-research-brief.md through docs/06-experiments.md.
Implement M1 only: cards/hands/deals, legal auctions, final contract/declarer,
duplicate scoring and signed IMP. Do not start M2 or change research goals.
Inspect existing work before changing it. Deliver working code, not just a plan.
Write implementation under src/bridge_evolve/core and behavior tests under
tests/core (include __init__.py so unittest discovery recurses).
Documentation is read-only in this executor; report progress in your final response.
Do not change other files, automation, verification infrastructure, dependencies,
or fixed scientific requirements. Do not access .runtime, credentials, environment
secrets, or gateway tooling. Do not use the network or request elevated permissions.
You run inside a non-root, network-disabled Docker container. Only core implementation
and core tests persist; other workspace edits are discarded. Do not try to run Docker
or the development controller from inside the container.
Use .venv/bin/python -m unittest discover -s tests -v for verification.
The controller does not treat your completion statement as an acceptance check.
If M1 already appears complete, verify and repair remaining M1 requirements only.
Report actual checks and unresolved gaps without claiming DDS integration.

Previous independent verification feedback:
{feedback}
"""


def run_iterations(
    project: Path,
    *,
    model: str = "gpt-5.3-codex",
    max_rounds: int = 3,
    round_timeout: int = 1200,
    max_requests: int = 30,
    completion_check: Path | None = None,
) -> Path:
    project = project.resolve()
    if not 1 <= max_rounds <= 20 or not 1 <= round_timeout <= 3600 or not 1 <= max_requests <= 200:
        raise ValueError("Use 1..20 rounds, 1..3600 seconds per round, and 1..200 model requests.")
    if not (project / "AGENTS.md").is_file() or not (project / "pyproject.toml").is_file():
        raise ValueError("Select the Bridge Bidding Evolution project directory.")
    if (project / ".codex").exists():
        raise RuntimeError("Project .codex overrides require manual review before automatic execution.")
    checker = None
    if completion_check is not None:
        checker = (project / completion_check).resolve()
    checker_hash = _checker_hash(project, checker)
    key = _gateway_key(project, model)
    runtime = project / ".runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    with _project_lock(runtime), TemporaryDirectory(prefix="bridge-gateway-") as socket_directory:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        run = runtime / "iterations" / run_id
        run.mkdir(parents=True, mode=0o700)
        os.chmod(run.parent, 0o700)
        for directory in WRITABLE_DIRECTORIES:
            target = project / directory
            if target.is_symlink() or not target.resolve().is_relative_to(project):
                raise RuntimeError("Executor-writable directories must remain inside the project.")
            target.mkdir(parents=True, exist_ok=True)
        image = ensure_image(project)
        vendor = codex_vendor(project)
        snapshot = run / "workspace"
        create_snapshot(project, snapshot)
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8",
        }
        state = RunState(
            project=str(project),
            model=model,
            max_rounds=max_rounds,
            round_timeout=round_timeout,
            completion_check=str(checker) if checker is not None else None,
            completion_hash=checker_hash,
            image=image, max_requests=max_requests,
        )
        manifest = run / "run.json"
        _write_state(manifest, state)
        LOGGER.info("Development run: %s", manifest)
        LOGGER.info("Model=%s rounds=%d request_budget=%d", model, max_rounds, max_requests)
        feedback = "No prior round. Check current code and run the existing tests."
        bridge = GatewayBridge(
            Path(socket_directory) / "api.sock", key, model, max_requests,
            state_path=run / "gateway.json",
        )

        def execute(mode, arguments, number, prompt, stdout, stderr, timeout):
            with container_command(
                project, snapshot, image, vendor, f"bridge-evolve-{run_id}-{mode}-{number}",
                mode, arguments, socket=bridge.socket if mode == "agent" else None,
            ) as command:
                return run_process(
                    command, cwd=project, environment=environment, prompt=prompt,
                    stdout=run / stdout, stderr=run / stderr, timeout=timeout,
                )

        try:
            with bridge:
                for number in range(1, max_rounds + 1):
                    state.active_round = number
                    state.requests_used = bridge.requests_used
                    _write_state(manifest, state)
                    LOGGER.info("Round %d/%d: running the coding executor", number, max_rounds)
                    agent = execute(
                        "agent", [model], number, _prompt(number, feedback),
                        f"round-{number}.jsonl", f"round-{number}.stderr", round_timeout,
                    )
                    record = RoundRecord(number, agent.returncode, timed_out=agent.timed_out)
                    state.rounds.append(record)
                    LOGGER.info(
                        "Round %d: executor exit=%d, model requests=%d/%d",
                        number, agent.returncode, bridge.requests_used, max_requests,
                    )
                    if agent.timed_out or agent.returncode != 0:
                        state.status = (
                            "request_budget_exhausted" if bridge.budget_denied
                            else "timed_out" if agent.timed_out else "executor_failed"
                        )
                        break
                    check = execute(
                        "verify", [], number, None, f"verify-{number}.stdout",
                        f"verify-{number}.stderr", 120,
                    )
                    record.verification_returncode = check.returncode
                    LOGGER.info("Round %d: independent verification exit=%d", number, check.returncode)
                    if check.timed_out:
                        state.status = "verification_timed_out"
                        break
                    if checker is not None:
                        if _checker_hash(project, checker) != checker_hash:
                            raise RuntimeError("The frozen completion checker changed during the run.")
                        if check.returncode == 0:
                            relative = checker.relative_to(project).as_posix()
                            completion = execute(
                                "completion", [f"/workspace/project/{relative}"], number, None,
                                f"completion-{number}.stdout", f"completion-{number}.stderr", 120,
                            )
                            record.completion_returncode = completion.returncode
                            if completion.timed_out:
                                state.status = "completion_timed_out"
                                break
                            if _checker_hash(project, checker) != checker_hash:
                                raise RuntimeError("The frozen completion checker changed during verification.")
                            if completion.returncode == 0:
                                state.status = "confirmed"
                                break
                    feedback = "\n".join(
                        path.read_text(encoding="utf8", errors="replace")[-6000:]
                        for path in (run / f"verify-{number}.stdout", run / f"verify-{number}.stderr")
                    ).replace(key, "[REDACTED_GATEWAY_KEY]")
                    if not feedback.strip():
                        feedback = f"Verification exited {check.returncode}; no output was produced."
                    if checker is None:
                        feedback += "\nNo independent completion checker is configured; continue auditing M1."
                    _write_state(manifest, state)
                else:
                    state.status = "budget_exhausted"
        except KeyboardInterrupt:
            state.status = "interrupted"
            raise
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            state.status = "failed"
            state.error = str(error).replace(key, "[REDACTED_GATEWAY_KEY]")
            raise
        finally:
            if state.status == "running":
                state.status = "failed"
                state.error = "Unexpected interruption; inspect the private executor logs."
            state.active_round = None
            state.requests_used = bridge.requests_used
            state.finished_at = datetime.now(timezone.utc).isoformat()
            _write_state(manifest, state)
            LOGGER.info("Development run stopped: %s (%s)", state.status, manifest)
        return manifest


def _handle_stop(signum, frame):
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--model", default="gpt-5.3-codex")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--round-timeout", type=int, default=1200)
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--completion-check", type=Path)
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    previous_handler = signal.signal(signal.SIGTERM, _handle_stop)
    try:
        manifest = run_iterations(
            arguments.project, model=arguments.model, max_rounds=arguments.max_rounds,
            round_timeout=arguments.round_timeout, max_requests=arguments.max_requests,
            completion_check=arguments.completion_check,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Development iteration failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Development iteration interrupted; owned containers were cleaned up.", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    state = json.loads(manifest.read_text(encoding="utf8"))
    print(json.dumps({"manifest": str(manifest), "status": state["status"]}))
    return 0 if state["status"] == "confirmed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
