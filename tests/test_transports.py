from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

from review_agent.config import Config, ReviewError
from review_agent.demo import create_demo
from review_agent.engine import review
from review_agent.http_server import create_server
from review_agent.mcp_server import MCPServer
from review_agent.providers import post_json
from review_agent.service import ReviewService


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = create_demo(Path(self.temp.name) / "repo")
        self.service = ReviewService(self.repo, Config(provider="demo"))

    def start_server(self, server):
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_mcp_process_handshake_discovery_and_review(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test", "version": "1"}, "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "run_review", "arguments": {}}},
        ]
        proc = subprocess.run([sys.executable, "-m", "review_agent", "mcp", "--repo", str(self.repo), "--provider", "demo", "--usage-db", str(Path(self.temp.name) / "mcp-usage.db")],
                              input="\n".join(json.dumps(x) for x in messages) + "\n", capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(x) for x in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        self.assertEqual(len(responses[1]["result"]["tools"]), 4)
        report = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(len(report["findings"]), 2)

    def test_mcp_requires_initialized_and_valid_arguments(self):
        server = MCPServer(self.service)
        before = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("error", before)
        self.assertEqual(server.dispatch([])["error"]["code"], -32600)

    def test_mcp_version_negotiation(self):
        server = MCPServer(self.service)
        result = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2099-01-01", "clientInfo": {"name": "test", "version": "1"}, "capabilities": {}}})
        self.assertEqual(result["result"]["protocolVersion"], "2025-06-18")

    def test_rest_api_auth_origin_and_review(self):
        token = "synthetic-test-token-at-least-24-chars"
        url = self.start_server(create_server(self.service, token, port=0))
        opener = build_opener(ProxyHandler({}))
        with self.assertRaises(HTTPError) as caught:
            opener.open(url + "/health")
        self.assertEqual(caught.exception.code, 401)
        with self.assertRaises(HTTPError) as caught:
            opener.open(Request(url + "/health", headers={"Authorization": "Bearer " + token, "Origin": "https://example.com"}))
        self.assertEqual(caught.exception.code, 403)
        request = Request(url + "/review", data=b"{}", headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        with opener.open(request) as response:
            result = json.load(response)
        self.assertEqual(len(result["findings"]), 2)
        # Clients cannot expand the pinned repository's scope through HTTP.
        request = Request(url + "/review", data=b'{"repo":"/etc"}', headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        with self.assertRaises(HTTPError) as caught:
            opener.open(request)
        self.assertEqual(caught.exception.code, 400)

    def test_real_http_ollama_adapter_round_trip(self):
        captured = []
        class FakeOllama(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                captured.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                data = json.dumps({"done": True, "done_reason": "stop", "message": {"content": json.dumps({"summary": "Synthetic transport response; no inference.", "findings": []})}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        url = self.start_server(ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama))
        result = review(self.repo, Config(base_url=url))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(captured[0][0], "/api/chat")
        self.assertIn("UNTRUSTED DATA", captured[0][1]["messages"][0]["content"])

    def test_redirects_are_not_followed(self):
        paths = []
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                paths.append(self.path)
                self.send_response(307)
                self.send_header("Location", "/should-not-receive-data")
                self.end_headers()
        url = self.start_server(ThreadingHTTPServer(("127.0.0.1", 0), Redirect))
        with self.assertRaisesRegex(ReviewError, "307"):
            post_json(url + "/start", {}, {}, 2)
        self.assertEqual(paths, ["/start"])


if __name__ == "__main__":
    unittest.main()
