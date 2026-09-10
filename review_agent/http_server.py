"""Loopback development REST API; not a production or remote MCP server."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from .config import ReviewError


def create_server(service, token: str, port=8765):
    if not isinstance(token, str) or len(token) < 24 or not token.isascii():
        raise ReviewError("REVIEW_AGENT_TOKEN must contain at least 24 ASCII characters.")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def reply(self, status, value):
            data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def authorize(self):
            if self.headers.get("Origin"):
                self.reply(403, {"error": "Browser-origin requests are disabled."})
                return False
            host = self.headers.get("Host", "")
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if host not in expected:
                self.reply(403, {"error": "Invalid Host."})
                return False
            provided = self.headers.get("Authorization", "").encode("utf-8")
            if not hmac.compare_digest(provided, ("Bearer " + token).encode()):
                self.reply(401, {"error": "Bearer token required."})
                return False
            return True

        def do_GET(self):
            if not self.authorize():
                return
            if self.path == "/health":
                self.reply(200, {"status": "ok", "provider": service.cfg.provider})
            elif self.path == "/tools":
                self.reply(200, {"tools": service.tools()})
            else:
                self.reply(404, {"error": "Not found."})

        def do_POST(self):
            if not self.authorize():
                return
            mapping = {"/context": "get_review_context", "/review": "run_review", "/validate": "validate_review", "/usage": "get_cost_summary"}
            if self.path not in mapping:
                self.reply(404, {"error": "Not found."})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json" or self.headers.get("Transfer-Encoding"):
                self.reply(415, {"error": "Use application/json with Content-Length."})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= 1_000_000:
                    self.reply(413, {"error": "Body must be at most 1 MB."})
                    return
                value = json.loads(self.rfile.read(length))
                result = service.call(mapping[self.path], value)
                self.reply(200, result)
            except (ValueError, UnicodeError, RecursionError):
                self.reply(400, {"error": "Invalid JSON request."})
            except ReviewError as exc:
                self.reply(400, {"error": str(exc)})
            except Exception:
                self.reply(500, {"error": "Internal review error; no successful review recorded."})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
