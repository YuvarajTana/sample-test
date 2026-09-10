"""Standard-library HTTP adapters. No SDK install is needed to start locally."""
import json
import os
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from .config import Config, ReviewError
from .prompts import SYSTEM_PROMPT, user_prompt
from .schema import REVIEW_SCHEMA
from .usage import normalize_usage


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    # No ambient proxy or redirects: repository data goes only to the configured endpoint.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    request = Request(url, data=json.dumps(payload, allow_nan=False).encode(), method="POST",
                      headers={"Content-Type": "application/json", **headers})
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ReviewError("Provider response exceeded 2 MB.")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ReviewError("Provider response must be a JSON object.")
            return result
    except HTTPError as exc:
        raise ReviewError(f"Provider returned HTTP {exc.code}; check model access, credentials, and quota. No automatic retry was made.") from exc
    except (URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ReviewError("Cannot reach provider or request timed out. Check endpoint, model, and timeout_seconds.") from exc
    except (ValueError, UnicodeError) as exc:
        raise ReviewError("Provider returned invalid JSON.") from exc


def required_key(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ReviewError(f"Set {name} in the launching process environment.")
    return value


def parse_output(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ReviewError("Provider did not return structured review content.")
    value = value.strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    elif value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
    try:
        return json.loads(value)
    except ValueError as exc:
        raise ReviewError("Model output was not valid JSON; try a stronger model or a smaller change.") from exc


def generate(cfg: Config, snapshot: dict) -> tuple[dict, dict]:
    if cfg.provider == "demo":
        return demo_review(snapshot), {"input_tokens": 0, "output_tokens": 0}
    return structured_generate(cfg, SYSTEM_PROMPT, user_prompt(snapshot), REVIEW_SCHEMA)


def structured_generate(cfg: Config, system: str, user: str, schema: dict) -> tuple[dict, dict]:
    endpoint = cfg.endpoint()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    usage = {"input_tokens": None, "output_tokens": None}
    try:
        if cfg.provider == "ollama":
            raw = post_json(endpoint + "/api/chat", {"model": cfg.model, "messages": messages,
                            "stream": False, "format": schema,
                            "options": {"temperature": 0, "num_predict": cfg.max_output_tokens, "num_ctx": 16384}}, {}, cfg.timeout_seconds)
            usage = normalize_usage(cfg.provider, raw)
            if raw.get("done") is not True or raw.get("done_reason") == "length":
                raise ReviewError("Ollama output was incomplete; reduce context or increase output budget.")
            value = raw["message"]["content"]
        elif cfg.provider == "openai":
            raw = post_json(endpoint + "/responses", {"model": cfg.model, "instructions": system,
                            "input": user, "store": False, "max_output_tokens": cfg.max_output_tokens,
                            "text": {"format": {"type": "json_schema", "name": "code_review", "strict": True, "schema": schema}}},
                            {"Authorization": "Bearer " + required_key("OPENAI_API_KEY")}, cfg.timeout_seconds)
            usage = normalize_usage(cfg.provider, raw)
            if raw.get("status") != "completed":
                raise ReviewError("OpenAI response was incomplete or failed; no successful review was recorded.")
            content = [c for item in raw.get("output", []) if item.get("type") == "message" for c in item.get("content", [])]
            if any(c.get("type") == "refusal" for c in content):
                raise ReviewError("OpenAI declined this review.")
            value = "".join(c.get("text", "") for c in content if c.get("type") == "output_text")
        elif cfg.provider == "anthropic":
            raw = post_json(endpoint + "/messages", {"model": cfg.model, "max_tokens": cfg.max_output_tokens,
                            "system": system, "messages": [{"role": "user", "content": user}],
                            "tools": [{"name": "submit_review", "description": "Return the requested structured result.", "input_schema": schema}],
                            "tool_choice": {"type": "tool", "name": "submit_review"}},
                            {"x-api-key": required_key("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"}, cfg.timeout_seconds)
            usage = normalize_usage(cfg.provider, raw)
            if raw.get("stop_reason") != "tool_use":
                raise ReviewError("Anthropic did not return a complete submit_review tool result.")
            blocks = [c for c in raw.get("content", []) if c.get("type") == "tool_use" and c.get("name") == "submit_review"]
            if len(blocks) != 1:
                raise ReviewError("Anthropic returned an unexpected number of review results.")
            value = blocks[0]["input"]
        else:
            headers = {}
            if os.environ.get("REVIEW_AGENT_API_KEY"):
                headers["Authorization"] = "Bearer " + os.environ["REVIEW_AGENT_API_KEY"]
            raw = post_json(endpoint + "/chat/completions", {"model": cfg.model, "messages": messages,
                            "stream": False, "max_tokens": cfg.max_output_tokens,
                            "response_format": {"type": "json_object"}}, headers, cfg.timeout_seconds)
            usage = normalize_usage(cfg.provider, raw)
            choice = raw["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ReviewError("Compatible provider output was incomplete or refused.")
            value = choice["message"]["content"]
        return parse_output(value), usage
    except ReviewError as exc:
        exc.usage = usage
        raise
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        error = ReviewError("Provider returned an unexpected response shape; check endpoint compatibility.")
        error.usage = usage
        raise error from exc


def demo_review(snapshot: dict) -> dict:
    """Deterministic smoke-test rules, explicitly NOT an LLM or production scanner."""
    findings = []
    for file in snapshot["files"]:
        for line in file["added_lines"]:
            text = file["new_lines"][str(line)]
            if re.search(r"\beval\s*\(", text):
                title = "Review dynamic expression evaluation"
                description = "This added line evaluates a string as code. If the expression is caller-controlled, a caller can execute code in this process. Confirm its source."
                suggestion = "Replace eval with a constrained parser or an explicit operation allowlist."
            elif re.search(r"execute\s*\(\s*f[\"']", text):
                title = "Review interpolated SQL execution"
                description = "This added line interpolates values into SQL. If a value is caller-controlled, it can alter the SQL statement. Confirm its source."
                suggestion = "Use bound query parameters supported by the database driver."
            else:
                continue
            findings.append({"path": file["path"], "line": line, "side": "new", "severity": "high",
                             "confidence": 0.8, "title": title, "description": description,
                             "suggestion": suggestion, "evidence": text.strip()})
    return {"summary": "DEMO ONLY: deterministic eval/interpolated-SQL pattern checks; no LLM was called.", "findings": findings}
