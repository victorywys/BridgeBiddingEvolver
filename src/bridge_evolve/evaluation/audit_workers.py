"""Spawn-safe entry points for bounded, frozen-system board audits."""

from __future__ import annotations

import importlib.util
import sys
from functools import partial
from pathlib import Path
from typing import Callable

from bridge_evolve.systems import load_system

_audit: Callable[..., tuple[dict, list, list]] | None = None


def initialize_worker(viewer_path: str, project: str, directory: str, request: dict) -> None:
    global _audit
    spec = importlib.util.spec_from_file_location("board_audit_worker_viewer", viewer_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the frozen audit runner.")
    viewer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = viewer
    spec.loader.exec_module(viewer)
    definitions = {}
    for identity in (request["system_a"], request["system_b"]):
        system = load_system(Path(directory) / "systems" / f"{identity}.json")
        if system.scope != "general" or system.content_hash != identity:
            raise ValueError("Worker frozen system identity mismatch.")
        definitions[identity] = system
    engine = viewer.BoardAuditEngine(Path(project), {identity: {"system": system}
                                                   for identity, system in definitions.items()})
    _audit = partial(engine.batch_board, request=request, definitions=definitions)


def audit_board(raw: dict, fresh_dds: bool) -> tuple[dict, list, list]:
    if _audit is None:
        raise RuntimeError("The audit worker has not been initialized.")
    return _audit(raw, fresh_dds=fresh_dds)
