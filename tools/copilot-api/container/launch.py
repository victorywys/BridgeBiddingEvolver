import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import venv
from pathlib import Path

WORKSPACE = Path("/workspace/project")
HOME = Path("/tmp/codex-home")
SOCKET = "/run/bridge-gateway.sock"

CONFIG = """
model_provider = "bridge"
model_reasoning_effort = "medium"
approval_policy = "never"
allow_login_shell = false
web_search = "disabled"
sandbox_mode = "danger-full-access"

[model_providers.bridge]
name = "Budgeted local Copilot bridge"
base_url = "http://127.0.0.1:4141/v1"
wire_api = "responses"
requires_openai_auth = false
request_max_retries = 0
stream_max_retries = 0

[shell_environment_policy]
inherit = "core"
ignore_default_excludes = false

[otel]
exporter = "none"
log_user_prompt = false
"""


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as upstream:
            upstream.connect(SOCKET)
            peers = {self.request: upstream, upstream: self.request}
            import select

            while True:
                readable, _, _ = select.select(list(peers), [], [], 180)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)


class RelayServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def prepare():
    if os.getuid() == 0:
        raise RuntimeError("The development container must run as a non-root user.")
    shutil.copytree("/seed", WORKSPACE, dirs_exist_ok=True, symlinks=False)
    for relative, persistent in (
        ("src/bridge_evolve/core", "/persist/core"),
        ("tests/core", "/persist/core-tests"),
    ):
        target = WORKSPACE / relative
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(persistent, target_is_directory=True)
    HOME.mkdir(mode=0o700)
    (HOME / "config.toml").write_text(CONFIG)
    environment = Path("/tmp/venv")
    venv.EnvBuilder(with_pip=True, system_site_packages=True, symlinks=True).create(environment)
    (WORKSPACE / ".venv").symlink_to(environment, target_is_directory=True)
    os.environ.update(
        HOME=str(HOME),
        CODEX_HOME=str(HOME),
        PATH=f"{environment}/bin:/opt/codex/bin:/usr/local/bin:/usr/bin:/bin",
        TMPDIR="/tmp",
    )
    subprocess.run(
        [str(environment / "bin/python"), "-m", "pip", "install", "--no-index",
         "--no-deps", "--no-build-isolation", "--no-input", "-e", str(WORKSPACE)],
        stdout=sys.stderr, stderr=sys.stderr, check=True,
    )
    os.chdir(WORKSPACE)


def main():
    prepare()
    mode = sys.argv[1]
    if mode == "verify":
        command = ["/tmp/venv/bin/python", "-m", "unittest", "discover", "-s", "tests", "-v"]
    elif mode == "completion":
        command = ["/tmp/venv/bin/python", sys.argv[2]]
    elif mode == "probe":
        assert not (WORKSPACE / ".runtime").exists()
        assert not Path("/seed/.runtime").exists()
        assert not Path("/var/run/docker.sock").exists()
        assert not any("KEY" in name or "TOKEN" in name for name in os.environ)
        try:
            Path("/opt/bridge/forbidden").write_text("must fail")
        except OSError:
            pass
        else:
            raise RuntimeError("Container root filesystem is writable.")
        for path in (Path("/seed/pyproject.toml"), Path("/persist/core/.read-only-probe")):
            try:
                with path.open("a"):
                    pass
            except OSError:
                pass
            else:
                raise RuntimeError("A verification input mount is writable.")
        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=2):
                pass
        except OSError:
            pass
        else:
            raise RuntimeError("Container has unexpected external network access.")
        print("BRIDGE_CONTAINER_ISOLATION_OK", flush=True)
        return 0
    elif mode == "agent":
        with RelayServer(("127.0.0.1", 4141), Relay) as relay:
            thread = threading.Thread(target=relay.serve_forever, daemon=True)
            thread.start()
            try:
                return subprocess.run(
                    ["/opt/codex/bin/codex", "exec", "--strict-config",
                     "--skip-git-repo-check", "--json", "--ephemeral",
                     "--model", sys.argv[2], "--cd", str(WORKSPACE), "-"],
                    check=False,
                ).returncode
            finally:
                relay.shutdown()
    else:
        raise ValueError("Unknown development container mode.")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
