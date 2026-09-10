from dataclasses import dataclass
from pathlib import Path
import os
import math
import tomllib
from urllib.parse import urlsplit


class ReviewError(Exception):
    """An actionable error whose message is safe to display."""


@dataclass(frozen=True)
class Config:
    provider: str = "ollama"
    model: str = "qwen2.5-coder:7b"
    base_url: str = ""
    allow_remote: bool = False
    max_files: int = 20
    max_file_bytes: int = 200_000
    max_context_chars: int = 32_000
    max_output_tokens: int = 4096
    timeout_seconds: int = 180
    exclude: tuple[str, ...] = ()
    usage_db: str = ""
    pricing_file: str = ""
    work_context: str = ""
    max_calls_per_conversation: int = 25
    max_conversation_usd: float | None = None
    review_cache_seconds: int = 0
    billing_mode: str = "metered"

    def __post_init__(self):
        if self.provider not in {"ollama", "openai", "anthropic", "openai-compatible", "demo"}:
            raise ReviewError("Unknown provider; use ollama, openai, anthropic, openai-compatible, or demo.")
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200:
            raise ReviewError("Set a non-empty model ID (maximum 200 characters).")
        for name, low, high in [("max_files", 1, 100), ("max_file_bytes", 100, 1_000_000),
                                ("max_context_chars", 1000, 200_000), ("max_output_tokens", 256, 32_768),
                                ("timeout_seconds", 1, 600), ("max_calls_per_conversation", 1, 1000),
                                ("review_cache_seconds", 0, 86400)]:
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ReviewError(f"{name} must be an integer between {low} and {high}.")
        if type(self.allow_remote) is not bool:
            raise ReviewError("allow_remote must be true or false.")
        if not isinstance(self.base_url, str):
            raise ReviewError("base_url must be a string.")
        if not isinstance(self.exclude, (list, tuple)) or not all(isinstance(x, str) for x in self.exclude):
            raise ReviewError("exclude must be an array of glob patterns.")
        for key in ["usage_db", "pricing_file", "work_context"]:
            if not isinstance(getattr(self, key), str):
                raise ReviewError(f"{key} must be a file path string.")
        if self.max_conversation_usd is not None and (type(self.max_conversation_usd) not in {int, float}
                or not math.isfinite(self.max_conversation_usd) or self.max_conversation_usd <= 0):
            raise ReviewError("max_conversation_usd must be a positive finite USD amount.")
        if self.billing_mode not in {"metered", "local"}:
            raise ReviewError("billing_mode must be metered or local.")

    def endpoint(self):
        defaults = {"ollama": "http://127.0.0.1:11434", "openai": "https://api.openai.com/v1",
                    "anthropic": "https://api.anthropic.com/v1", "openai-compatible": "http://127.0.0.1:1234/v1"}
        url = (self.base_url or defaults.get(self.provider, "")).rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ReviewError("base_url must be an HTTP(S) endpoint without credentials, query, or fragment.")
        # Literal loopback only: avoid classifying a remotely resolved hostname as local.
        local = parsed.hostname in {"127.0.0.1", "::1"}
        if not local and not self.allow_remote:
            raise ReviewError("Remote provider disabled. Set allow_remote=true in your explicit config or use --allow-remote.")
        if not local and parsed.scheme != "https":
            raise ReviewError("Remote provider endpoints require HTTPS.")
        if self.provider in {"openai", "anthropic"}:
            expected = defaults[self.provider]
            if url != expected:
                raise ReviewError(f"Use {expected} for {self.provider}; custom servers use openai-compatible.")
        return url


def load_config(path: str | None = None, **overrides) -> Config:
    # Never auto-load config from the repository being reviewed.
    selected = path or os.environ.get("REVIEW_AGENT_CONFIG")
    values = {}
    if selected:
        try:
            with Path(selected).expanduser().open("rb") as stream:
                doc = tomllib.load(stream)
            if set(doc) - {"review"} or not isinstance(doc.get("review", {}), dict):
                raise ReviewError("Config must contain a [review] table only.")
            values = doc.get("review", {})
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ReviewError("Cannot read config; check its path and TOML syntax.") from exc
    values.update({k: v for k, v in overrides.items() if v is not None})
    if "exclude" in values and isinstance(values["exclude"], list):
        values["exclude"] = tuple(values["exclude"])
    try:
        return Config(**values)
    except TypeError as exc:
        raise ReviewError("Unknown config field; see review-agent.example.toml.") from exc
