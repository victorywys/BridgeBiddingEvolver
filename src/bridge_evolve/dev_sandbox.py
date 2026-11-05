"""Credential-free Docker execution with a budgeted, Unix-socket model bridge."""

import hashlib
import http.client
import json
import os
import shutil
import socketserver
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
MAX_BODY = 16 * 1024 * 1024
MAX_CONNECTIONS = 4
PERSISTENT_DIRECTORIES = ("src/bridge_evolve/core", "tests/core")
SNAPSHOT_EXCLUDES = {
    ".runtime", ".venv", ".git", ".dev-tmp", "node_modules", "__pycache__", "dist", "build",
}


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(30)
        return connection, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            body = b"Too many gateway connections."
            headers = (
                "HTTP/1.0 503 Service Unavailable\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
            try:
                request.sendall(headers + body)
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class GatewayBridge:
    def __init__(
        self, socket: Path, key: str, model: str, max_requests: int,
        *, upstream_port: int = 4141, interval: float = 1.0, state_path: Path | None = None,
    ):
        if not 1 <= max_requests <= 200:
            raise ValueError("The model request budget must be between 1 and 200.")
        self.socket = socket
        self.key = key
        self.model = model
        self.max_requests = max_requests
        self.upstream_port = upstream_port
        self.interval = interval
        self.state_path = state_path or socket.parent / "gateway.json"
        self.requests_used = 0
        self.budget_denied = False
        self.last_error = None
        self._last_request = 0.0
        self._lock = threading.Lock()
        self._active = threading.Lock()

    def _state(self):
        path = self.state_path
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf8") as output:
            os.chmod(temporary, 0o600)
            json.dump(
                {
                    "requests_used": self.requests_used,
                    "max_requests": self.max_requests,
                    "budget_denied": self.budget_denied,
                    "last_error": self.last_error,
                },
                output,
            )
        temporary.replace(path)

    def __enter__(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, format, *args):
                pass

            def reject(self, status, message):
                body = json.dumps({"error": {"message": message, "type": "bridge_denied"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                parsed = urlsplit(self.path)
                if (parsed.query and parsed.path != "/v1/models") or parsed.netloc or (
                    self.command, parsed.path
                ) not in {("GET", "/v1/models"), ("POST", "/v1/responses")}:
                    self.reject(403, "This route is not available to the development executor.")
                    return
                body = None
                acquired = False
                if self.command == "POST":
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 1 <= size <= MAX_BODY:
                            self.reject(413, "Invalid request body size.")
                            return
                        self.connection.settimeout(30)
                        payload = json.loads(self.rfile.read(size))
                    except (ValueError, TimeoutError):
                        self.reject(400, "Invalid JSON request.")
                        return
                    if not isinstance(payload, dict) or payload.get("model") != bridge.model:
                        self.reject(403, "Only the configured coding model is allowed.")
                        return
                    limit = payload.get("max_output_tokens", 8192)
                    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                        self.reject(400, "Invalid output token limit.")
                        return
                    payload["max_output_tokens"] = min(limit, 8192)
                    payload["store"] = False
                    body = json.dumps(payload).encode()
                    acquired = bridge._active.acquire(blocking=False)
                    if not acquired:
                        self.reject(429, "Concurrent model requests are not allowed.")
                        return
                    with bridge._lock:
                        if bridge.requests_used >= bridge.max_requests:
                            bridge.budget_denied = True
                            bridge._state()
                            bridge._active.release()
                            self.reject(429, "The development model request budget is exhausted.")
                            return
                        delay = bridge.interval - (time.monotonic() - bridge._last_request)
                        if delay > 0:
                            time.sleep(delay)
                        bridge._last_request = time.monotonic()
                        bridge.requests_used += 1
                        bridge._state()
                connection = http.client.HTTPConnection("127.0.0.1", bridge.upstream_port, timeout=180)
                headers = {
                    "Authorization": f"Bearer {bridge.key}",
                    "Content-Type": "application/json",
                }
                for name in ("Accept", "User-Agent"):
                    value = self.headers.get(name)
                    if value is not None:
                        headers[name] = value
                response_started = False
                try:
                    connection.request(self.command, parsed.path, body=body, headers=headers)
                    response = connection.getresponse()
                    self.send_response(response.status)
                    for name in ("Content-Type", "Content-Length", "Cache-Control"):
                        value = response.getheader(name)
                        if value is not None:
                            self.send_header(name, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    response_started = True
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (OSError, http.client.HTTPException) as error:
                    with bridge._lock:
                        bridge.last_error = type(error).__name__
                        bridge._state()
                    if not response_started:
                        self.reject(502, "The local gateway transport failed.")
                finally:
                    connection.close()
                    if acquired:
                        bridge._active.release()
                    self.close_connection = True

        self.server = UnixHTTPServer(str(self.socket), Handler)
        os.chmod(self.socket, 0o600)
        self._state()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.socket.unlink(missing_ok=True)


def image_name(project: Path) -> str:
    context = project / "tools/copilot-api/container"
    digest = hashlib.sha256()
    for name in ("Dockerfile", "launch.py"):
        digest.update(name.encode())
        digest.update((context / name).read_bytes())
    return "bridge-evolve-dev:" + digest.hexdigest()[:16]


@contextmanager
def docker_client():
    with TemporaryDirectory(prefix="bridge-docker-client-") as directory:
        yield [*DOCKER, "--config", directory]


def ensure_image(project: Path) -> str:
    image = image_name(project)
    with docker_client() as docker:
        result = subprocess.run(
            [*docker, "image", "inspect", image], capture_output=True, text=True, check=False,
        )
        if result.returncode:
            if "no such image" not in result.stderr.lower():
                raise RuntimeError("Cannot inspect images on the local Docker daemon.")
            subprocess.run(
                [*docker, "build", "--tag", image, str(project / "tools/copilot-api/container")],
                stdout=None, stderr=None, check=True,
            )
    return image


def snapshot_includes(project: Path, path: Path) -> bool:
    relative = path.relative_to(project)
    return not (
        relative.is_relative_to(Path("tools/copilot-api"))
        or any(part in SNAPSHOT_EXCLUDES or part.endswith(".egg-info") for part in relative.parts)
    )


def create_snapshot(project: Path, destination: Path) -> None:
    def ignore(directory, names):
        skipped = set()
        for name in names:
            path = Path(directory) / name
            if not snapshot_includes(project, path):
                skipped.add(name)
            elif path.is_symlink():
                raise RuntimeError(f"The development snapshot refuses symlinks: {path.relative_to(project)}")
        return skipped

    shutil.copytree(project, destination, ignore=ignore)


def codex_vendor(project: Path) -> Path:
    candidates = list(
        (project / "tools/copilot-api/node_modules/@openai").glob("codex-*/vendor/*/bin/codex")
    )
    if len(candidates) != 1:
        raise RuntimeError("Expected exactly one pinned native Codex executable.")
    return candidates[0].parent.parent.resolve()


def remove_container(name: str, docker: list[str]) -> None:
    result = subprocess.run(
        [*docker, "rm", "--force", name], capture_output=True, text=True, check=False,
    )
    if result.returncode and "no such container" not in result.stderr.lower():
        raise RuntimeError(f"Cannot clean up the owned development container: {name}")


@contextmanager
def container_command(
    project: Path, snapshot: Path, image: str, vendor: Path, name: str,
    mode: str, arguments: list[str], *, socket: Path | None = None,
):
    with docker_client() as docker:
        command = [
            *docker, "run", "--rm", "--init", "--interactive", "--name", name,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--pids-limit", "256", "--memory", "2g", "--cpus", "2",
            "--tmpfs", "/tmp:rw,nosuid,nodev,exec,size=256m,mode=1777",
            "--tmpfs", "/workspace:rw,nosuid,nodev,size=512m,mode=1777",
            "--mount", f"type=bind,src={snapshot},dst=/seed,readonly",
            "--mount", f"type=bind,src={vendor},dst=/opt/codex,readonly",
        ]
        for source, target in zip(PERSISTENT_DIRECTORIES, ("core", "core-tests"), strict=True):
            options = ",readonly" if mode != "agent" else ""
            command.extend([
                "--mount", f"type=bind,src={project / source},dst=/persist/{target}{options}",
            ])
        if socket is not None:
            command.extend(["--mount", f"type=bind,src={socket},dst=/run/bridge-gateway.sock,readonly"])
        command.extend([image, mode, *arguments])
        try:
            yield command
        finally:
            remove_container(name, docker)
