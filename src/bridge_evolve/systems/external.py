"""Pinned, local neural baselines; only visible observations cross the process boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import select
import subprocess
from contextlib import contextmanager

from bridge_evolve.core import Call
from bridge_evolve.systems.language import SystemDefinition, SystemError, load_json, object_fields, validate_system
from bridge_evolve.systems.render import render_system
from bridge_evolve.systems.runtime import Observation

SCHEMA = "bridge-external-policy/v1"
WORKER = Path(__file__).with_name("neural_worker.py")
TIMEOUT = 60
MAX_MESSAGE = 128 * 1024
OPAQUE_MEANING = "External neural policy: no exact public call meaning is available; no hand guarantees are inferred."
LOGGER = logging.getLogger(__name__)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_identity(source: Path) -> tuple[str, dict[str, str]]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise ValueError(f"Cannot identify external source checkout: {error.stderr.strip()}") from error
    files = sorted((source / "training").rglob("*.py")) + [source / "server/emergent/brl_player.py"]
    if not files or not (source / "training/bridge/calls.py").is_file():
        raise ValueError("External source must be a complete Pidgin checkout.")
    return commit, {path.relative_to(source).as_posix(): file_hash(path) for path in files}


def _request(observation: Observation) -> dict:
    ranks = "AKQJT98765432"
    hand = ".".join(
        "".join(rank for rank in ranks if any(card.suit == suit and card.rank == rank for card in observation.hand.cards)) or "-"
        for suit in "SHDC"
    )
    return {
        "hand": hand, "seat": observation.seat.value, "dealer": observation.dealer.value,
        "vulnerability": observation.vulnerability.value,
        "history": [str(call) for call in observation.history],
        "legal_calls": [str(call) for call in observation.legal_actions],
    }


def external_identity(data: dict) -> str:
    from bridge_evolve.evaluation.data import canonical_hash
    fields = object_fields(data, {
        "schema", "name", "family", "python", "source", "source_commit", "source_files",
        "weights", "weights_sha256", "worker_sha256", "runtime", "model_source",
    })
    if fields["schema"] != SCHEMA or fields["family"] not in ("brl", "pidgin"):
        raise ValueError("Unknown external baseline schema or family.")
    return canonical_hash({
        key: value for key, value in fields.items() if key not in ("name", "python", "source", "weights")
    })


class ExternalPolicy:
    scope = "general"

    def __init__(self, data: dict):
        fields = object_fields(data, {
            "schema", "name", "family", "python", "source", "source_commit", "source_files",
            "weights", "weights_sha256", "worker_sha256", "runtime", "model_source",
        })
        if fields["schema"] != SCHEMA or fields["family"] not in ("brl", "pidgin"):
            raise ValueError("Unknown external baseline schema or family.")
        if any(not isinstance(fields[key], str) or not fields[key] for key in fields if key not in ("runtime", "source_files")):
            raise ValueError("External baseline strings must be nonempty.")
        if not isinstance(fields["runtime"], dict) or not isinstance(fields["source_files"], dict):
            raise ValueError("External baseline requires frozen runtime and source identities.")
        for key in ("python", "source", "weights"):
            if not Path(fields[key]).is_absolute():
                raise ValueError("Registered external baseline paths must be absolute.")
        self.data = copy.deepcopy(fields)
        self.name = fields["name"]
        self.process: subprocess.Popen | None = None
        self.check_identity()
        self.content_hash = external_identity(self.data)

    def check_identity(self):
        source = Path(self.data["source"])
        commit, files = source_identity(source)
        if commit != self.data["source_commit"] or files != self.data["source_files"]:
            raise ValueError("External inference source changed; register a new baseline identity.")
        if file_hash(Path(self.data["weights"])) != self.data["weights_sha256"]:
            raise ValueError("External model weights changed.")
        if file_hash(WORKER) != self.data["worker_sha256"]:
            raise ValueError("External adapter changed; register a new baseline identity.")

    def _receive(self) -> dict:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("External baseline is not running.")
        if not select.select([self.process.stdout], [], [], TIMEOUT)[0]:
            raise RuntimeError("External baseline response timed out; no fallback call was substituted.")
        line = self.process.stdout.readline(MAX_MESSAGE + 1)
        if not line or len(line) > MAX_MESSAGE or not line.endswith("\n"):
            raise RuntimeError("External baseline exited or returned an oversized/incomplete response.")
        result = json.loads(line)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError(f"External baseline failed: {result!r}")
        return result

    def __enter__(self):
        if self.process is not None:
            raise RuntimeError("External baseline process is already open.")
        self.check_identity()
        self.process = subprocess.Popen(
            [self.data["python"], "-u", str(WORKER), "--source", self.data["source"],
             "--weights", self.data["weights"], "--family", self.data["family"]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
            cwd=self.data["source"],
            env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                 "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": ""},
        )
        try:
            handshake = self._receive()
            if handshake.get("runtime") != self.data["runtime"]:
                raise ValueError("External model runtime/settings changed; register a new identity.")
        except (OSError, ValueError, RuntimeError):
            self.close()
            raise
        return self

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    LOGGER.warning("External baseline pipe was already closed during cleanup.")
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()

    def __exit__(self, *_):
        self.close()

    def decide(self, observation: Observation) -> tuple[Call, dict]:
        from bridge_evolve.systems.language import parse_call
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Open the external baseline before making decisions.")
        try:
            self.process.stdin.write(json.dumps(_request(observation), allow_nan=False) + "\n")
            self.process.stdin.flush()
            result = self._receive()
            call = parse_call(result["call"])
            if call not in observation.legal_actions:
                raise ValueError(f"External baseline emitted illegal call {call}; no fallback was substituted.")
            if result.get("model_family") != self.data["family"]:
                raise ValueError("External baseline returned a different model family.")
            return call, result
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            self.close()
            raise SystemError(f"External baseline {self.name} failed: {error}",
                              actor=observation.seat.value, system_hash=self.content_hash) from error

    def to_data(self) -> dict:
        return copy.deepcopy(self.data)


Player = SystemDefinition | ExternalPolicy


def render_player(player: Player) -> str:
    if isinstance(player, SystemDefinition):
        return render_system(player)
    return (
        f"# {player.name}\n\nExternal frozen neural baseline, not an executable rule IR.\n\n"
        f"- Identity: `{player.content_hash}`\n"
        f"- Source commit: `{player.data['source_commit']}`\n"
        f"- Weights SHA256: `{player.data['weights_sha256']}`\n"
        f"- Model source: {player.data['model_source']}\n"
        f"- {OPAQUE_MEANING}\n"
        "- CPU, one thread, deterministic greedy calls; no bidding search or DDS input.\n"
        "- Both partners use this same frozen model. This description is not a human-executable convention card.\n"
    )


@contextmanager
def open_player(path: Path):
    data = load_json(path)
    if isinstance(data, dict) and data.get("schema") == SCHEMA:
        with ExternalPolicy(data) as player:
            yield player
    else:
        yield validate_system(data)


def register_baseline(*, source: Path, python: Path, weights: Path, family: str, name: str, model_source: str) -> dict:
    source, weights = source.resolve(), weights.resolve()
    commit, files = source_identity(source)
    data = {
        "schema": SCHEMA, "name": name, "family": family, "python": str(python.absolute()),
        "source": str(source), "source_commit": commit, "source_files": files,
        "weights": str(weights), "weights_sha256": file_hash(weights),
        "worker_sha256": file_hash(WORKER), "runtime": {}, "model_source": model_source,
    }
    player = ExternalPolicy(data)
    # Registration discovers the runtime; normal runs require an exact match.
    try:
        process = subprocess.run(
            [data["python"], str(WORKER), "--source", str(source), "--weights", str(weights), "--family", family],
            input="", capture_output=True, text=True, timeout=TIMEOUT, check=True,
            env={"PATH": os.defpath, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                 "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": ""},
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"External baseline registration failed: {error.stderr.strip()}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("External baseline registration timed out.") from error
    handshake = json.loads(process.stdout)
    if handshake.get("ok") is not True or not isinstance(handshake.get("runtime"), dict):
        raise ValueError("External baseline did not return its runtime identity.")
    data["runtime"] = handshake["runtime"]
    player.check_identity()
    return data
