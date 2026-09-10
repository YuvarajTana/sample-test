from decimal import Decimal
import hashlib
import json
import time
from urllib.parse import urlsplit

from .config import ReviewError
from .usage import Ledger, conversation_id, price, rate_card


def local_free(cfg):
    if cfg.provider == "demo":
        return True
    if cfg.billing_mode == "local":
        if cfg.provider not in {"ollama", "openai-compatible"} or urlsplit(cfg.endpoint()).hostname not in {"127.0.0.1", "::1"}:
            raise ReviewError("Local billing mode requires an Ollama/compatible loopback endpoint running local inference.")
        return True
    return False


class Meter:
    def __init__(self, cfg, conversation=None):
        self.cfg = cfg
        self.conversation = conversation_id(conversation)
        self.ledger = Ledger(cfg.usage_db) if cfg.usage_db else None
        self.rate = rate_card(cfg.pricing_file, cfg.provider, cfg.model)
        self.event = None
        self.free = local_free(cfg)
        if cfg.max_conversation_usd is not None and not self.ledger:
            raise ReviewError("A usage_db is required for the conversation spend guard.")

    def start(self, operation, request_text):
        # Conservative byte-based estimate plus envelope allowance; not an exact tokenizer or hard billing cap.
        guessed_input = len(request_text.encode("utf-8")) + 2048
        projected = price({"input_tokens": guessed_input, "output_tokens": self.cfg.max_output_tokens}, self.rate, self.free)
        if self.ledger:
            self.event = self.ledger.start(self.cfg, self.conversation, operation, len(request_text), projected["estimated_usd"])
        self.started = time.monotonic()

    def finish(self, usage, status="completed"):
        cost = price(usage, self.rate, self.free)
        if self.ledger and self.event:
            self.ledger.finish(self.event, usage, cost, status, time.monotonic() - self.started)
        return {"conversation_id": self.conversation, "event_id": self.event, "usage_origin": "agent_provider_response",
                "cost": cost, "tracking_enabled": self.ledger is not None}

    def cache_key(self, snapshot, system_prompt):
        return hashlib.sha256(json.dumps({"snapshot": snapshot, "prompt": system_prompt, "version": "0.2.0",
                   "provider": self.cfg.provider, "model": self.cfg.model, "endpoint": self.cfg.base_url,
                   "output_tokens": self.cfg.max_output_tokens}, sort_keys=True).encode()).hexdigest()

    def cached(self, key):
        if not self.ledger or not self.cfg.review_cache_seconds:
            return None
        with self.ledger.connection() as db:
            row = db.execute("SELECT * FROM review_cache WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row["created"] > self.cfg.review_cache_seconds:
            return None
        report = json.loads(row["report"])
        # Cache reuse does not invoke a provider and must not count old tokens a second time.
        # Use an independent ledger entry without consuming a call budget.
        import uuid
        from datetime import datetime, timezone
        identifier = str(uuid.uuid4())
        cost = {"estimated_usd": "0", "basis": "Exact review-result cache hit; no provider request."}
        usage = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
        with self.ledger.connection() as db:
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identifier, self.conversation, datetime.now(timezone.utc).isoformat(), "review", self.cfg.provider,
                self.cfg.model, "cache_hit", "agent", json.dumps(usage), json.dumps(cost), None, 0, 0))
        report["usage"] = usage
        report["accounting"] = {"conversation_id": self.conversation, "event_id": identifier, "cost": cost,
                                "usage_origin": "result_cache", "tracking_enabled": True}
        report["cache"] = {"hit": True, "original_created_at": report["created_at"]}
        report["created_at"] = datetime.now(timezone.utc).isoformat()
        report["elapsed_seconds"] = 0
        return report

    def save_cache(self, key, report):
        if self.ledger and self.cfg.review_cache_seconds and report["status"] in {"completed", "demo"} and not report.get("stale"):
            with self.ledger.connection() as db:
                db.execute("DELETE FROM review_cache WHERE created < ?", (time.time() - self.cfg.review_cache_seconds,))
                db.execute("INSERT OR REPLACE INTO review_cache VALUES (?,?,?)", (key, time.time(), json.dumps(report)))
