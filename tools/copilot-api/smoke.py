"""Validate the external sandbox; --with-model also exercises a real coding tool."""

import argparse
import os
import shutil
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from bridge_evolve.automation import _gateway_key, run_process
from bridge_evolve.dev_sandbox import (
    GatewayBridge,
    PERSISTENT_DIRECTORIES,
    codex_vendor,
    container_command,
    create_snapshot,
    ensure_image,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-model", action="store_true")
    arguments = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    run = project / ".runtime" / ("docker-smoke-" + uuid.uuid4().hex[:8])
    run.mkdir(mode=0o700)
    for directory in PERSISTENT_DIRECTORIES:
        (project / directory).mkdir(parents=True, exist_ok=True)
    snapshot = run / "workspace"
    create_snapshot(project, snapshot)
    image, vendor = ensure_image(project), codex_vendor(project)
    environment = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}

    def execute(mode, parameters, prompt=None, socket=None, persistent_project=project):
        with container_command(
            persistent_project, snapshot, image, vendor, "bridge-smoke-" + uuid.uuid4().hex[:12],
            mode, parameters, socket=socket,
        ) as command:
            result = run_process(
                command, cwd=project, environment=environment, prompt=prompt,
                stdout=run / f"{mode}.stdout", stderr=run / f"{mode}.stderr", timeout=240,
            )
        if result.returncode:
            raise RuntimeError(f"{mode} failed with exit {result.returncode}; private logs: {run}")

    execute("probe", [])
    if "BRIDGE_CONTAINER_ISOLATION_OK" not in (run / "probe.stdout").read_text():
        raise RuntimeError("The container did not confirm its isolation checks.")
    print("Container isolation checks passed.", flush=True)
    execute("verify", [])
    print("Fresh offline verification container passed the project tests.", flush=True)
    if arguments.with_model:
        model_project = run / "model-probe"
        for relative in PERSISTENT_DIRECTORIES:
            shutil.copytree(
                project / relative, model_project / relative,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        marker = model_project / PERSISTENT_DIRECTORIES[0] / ("_automation_probe_" + uuid.uuid4().hex[:8] + ".txt")
        try:
            with TemporaryDirectory(prefix="bridge-smoke-socket-") as directory:
                with GatewayBridge(
                    Path(directory) / "api.sock", _gateway_key(project, "gpt-5.3-codex"),
                    "gpt-5.3-codex", 5, state_path=run / "gateway.json",
                ) as bridge:
                    prompt = (
                        "This is only a coding-executor connectivity test, not M1 implementation. "
                        "Use a shell tool to write exactly BRIDGE_CONTAINER_AGENT_OK into "
                        f"src/bridge_evolve/core/{marker.name}. Do not create or modify any other "
                        "persistent files. Do not read credentials or use the network. Then finish."
                    )
                    execute("agent", ["gpt-5.3-codex"], prompt, bridge.socket, model_project)
                    if marker.read_text().strip() != "BRIDGE_CONTAINER_AGENT_OK":
                        raise RuntimeError("The real coding executor did not write the expected marker.")
                    print(f"Real coding-tool write passed; model requests: {bridge.requests_used}.")
        finally:
            marker.unlink(missing_ok=True)
    print(f"Smoke-test evidence: {run}")


if __name__ == "__main__":
    main()
