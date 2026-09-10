"""Small stdio MCP tools server, implementing the 2025-06-18 protocol subset.

No sampling, prompts, resources, HTTP MCP, or server-initiated requests.
Requests are processed sequentially. Only JSON-RPC is written to stdout.
"""
import json
import sys
from . import __version__
from .config import ReviewError

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_MESSAGE_BYTES = 1_000_000


class MCPServer:
    def __init__(self, service):
        self.service = service
        self.initialized = False
        self.ready = False

    @staticmethod
    def error(identifier, code, message):
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}

    def dispatch(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self.error(None, -32600, "Invalid Request")
        identifier = message.get("id")
        method, params = message["method"], message.get("params", {})
        if "id" in message and (type(identifier) not in {int, str}):
            return self.error(None, -32600, "Request id must be a string or integer")
        if "id" not in message:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if not isinstance(params, dict):
            return self.error(identifier, -32602, "params must be an object")
        if method == "initialize":
            if self.initialized:
                return self.error(identifier, -32600, "Already initialized")
            if not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                return self.error(identifier, -32602, "Missing initialization parameters")
            proposed = params["protocolVersion"]
            self.initialized = True
            result = {"protocolVersion": proposed if proposed in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "lakshya-review-agent", "version": __version__},
                      "instructions": "Use get_review_context then validate_review for host-model review, or run_review for an independent configured provider. The repository is pinned at startup."}
        elif method == "ping":
            result = {}
        elif not self.ready:
            return self.error(identifier, -32000, "Initialize and send notifications/initialized first")
        elif method == "tools/list":
            result = {"tools": self.service.tools()}
        elif method == "tools/call":
            if not isinstance(params.get("name"), str) or params["name"] not in {x["name"] for x in self.service.tools()}:
                return self.error(identifier, -32602, "Unknown tool name")
            try:
                value = self.service.call(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True, allow_nan=False)}], "isError": False}
            except ReviewError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except Exception:
                print("Unexpected internal review error; inspect server code locally.", file=sys.stderr)
                result = {"content": [{"type": "text", "text": "Internal review error; no successful review recorded."}], "isError": True}
        else:
            return self.error(identifier, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def run(self):
        while True:
            line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_MESSAGE_BYTES:
                response = self.error(None, -32600, "Message exceeds 1 MB")
                print(json.dumps(response), flush=True)
                return
            try:
                request = json.loads(line)
                response = self.dispatch(request)
            except (ValueError, UnicodeError, RecursionError):
                response = self.error(None, -32700, "Parse error")
            if response is not None:
                print(json.dumps(response, ensure_ascii=True, allow_nan=False), flush=True)
