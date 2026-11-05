import http.client
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge_evolve.dev_sandbox import MAX_BODY, MAX_CONNECTIONS, GatewayBridge, create_snapshot


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class GatewayBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="bridge-proxy-test-")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.upstream_requests = []
        requests = self.upstream_requests

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                body = json.dumps({"data": [{"id": "test-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((dict(self.headers), payload))
                body = b'data: {"type":"response.completed"}\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(body)

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)

    def request(self, bridge, method="POST", path="/v1/responses", payload=None):
        connection = UnixConnection(bridge.socket)
        self.addCleanup(connection.close)
        body = json.dumps(payload if payload is not None else {"model": "test-model"}).encode()
        connection.request(method, path, body=body, headers={"Authorization": "untrusted-client-value"})
        response = connection.getresponse()
        return response.status, response.read()

    def bridge(self, budget=2):
        return GatewayBridge(
            self.home / "api.sock", "host-only-test-key", "test-model", budget,
            upstream_port=self.upstream.server_port, interval=0,
        )

    def test_streaming_response_and_host_only_auth_injection(self) -> None:
        with self.bridge() as bridge:
            status, body = self.request(
                bridge, payload={"model": "test-model", "store": True, "max_output_tokens": 9000}
            )
            self.assertEqual(status, 200)
            self.assertIn(b"response.completed", body)
            headers, payload = self.upstream_requests[0]
            self.assertEqual(headers["Authorization"], "Bearer host-only-test-key")
            self.assertFalse(payload["store"])
            self.assertEqual(payload["max_output_tokens"], 8192)
            self.assertNotIn(b"host-only-test-key", body)
            self.assertEqual(bridge.socket.stat().st_mode & 0o777, 0o600)
        self.assertFalse(bridge.socket.exists())

    def test_admin_routes_and_other_models_never_reach_the_gateway(self) -> None:
        with self.bridge() as bridge:
            for method, path, payload in (
                ("GET", "/admin/config", {}),
                ("POST", "/v1/responses", {"model": "other-model"}),
                ("POST", "http://external.example/v1/responses", {"model": "test-model"}),
            ):
                with self.subTest(path=path):
                    self.assertEqual(self.request(bridge, method, path, payload)[0], 403)
            self.assertEqual(bridge.requests_used, 0)
            self.assertEqual(self.upstream_requests, [])

    def test_model_request_budget_is_enforced_not_just_recorded(self) -> None:
        with self.bridge(budget=1) as bridge:
            self.assertEqual(self.request(bridge)[0], 200)
            self.assertEqual(self.request(bridge)[0], 429)
            self.assertTrue(bridge.budget_denied)
            self.assertEqual(len(self.upstream_requests), 1)
            state = json.loads((self.home / "gateway.json").read_text())
            self.assertEqual(state["requests_used"], 1)
            self.assertTrue(state["budget_denied"])
            self.assertNotIn("host-only-test-key", json.dumps(state))

    def test_discovery_does_not_consume_inference_budget(self) -> None:
        with self.bridge() as bridge:
            status, body = self.request(bridge, "GET", "/v1/models?client_version=test")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["data"][0]["id"], "test-model")
            self.assertEqual(bridge.requests_used, 0)

    def test_oversized_requests_are_rejected_before_reading_or_forwarding(self) -> None:
        with self.bridge() as bridge:
            connection = UnixConnection(bridge.socket)
            self.addCleanup(connection.close)
            connection.request(
                "POST", "/v1/responses", body=b"", headers={"Content-Length": str(MAX_BODY + 1)}
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
            self.assertEqual(self.upstream_requests, [])
            self.assertEqual(bridge.requests_used, 0)

    def test_open_connections_are_bounded_even_before_http_headers_arrive(self) -> None:
        with self.bridge() as bridge:
            held = []
            try:
                for _ in range(MAX_CONNECTIONS):
                    connection = UnixConnection(bridge.socket)
                    connection.connect()
                    held.append(connection)
                status, _ = self.request(bridge, "GET", "/v1/models")
                self.assertEqual(status, 503)
                self.assertEqual(bridge.requests_used, 0)
            finally:
                for connection in held:
                    connection.close()


class SnapshotTests(unittest.TestCase):
    def test_snapshot_omits_credentials_dependencies_and_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-snapshot-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            for path in (".runtime/token", ".venv/bin/python", "tools/copilot-api/private", "src/app.py"):
                target = project / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("test fixture")
            snapshot = Path(directory) / "snapshot"
            create_snapshot(project, snapshot)
            self.assertTrue((snapshot / "src/app.py").exists())
            self.assertFalse((snapshot / ".runtime").exists())
            self.assertFalse((snapshot / ".venv").exists())
            self.assertFalse((snapshot / "tools/copilot-api").exists())

    def test_snapshot_does_not_follow_source_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-snapshot-test-") as directory:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "outside-link").symlink_to("/etc")
            with self.assertRaisesRegex(RuntimeError, "refuses symlinks"):
                create_snapshot(project, Path(directory) / "snapshot")


if __name__ == "__main__":
    unittest.main()
