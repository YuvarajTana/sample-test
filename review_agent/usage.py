"""Local accounting: observed usage and rate-card estimates, never invented bills."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
import tomllib
import uuid

from .config import ReviewError

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "cache_write_5m_tokens", "cache_write_1h_tokens", "reasoning_output_tokens")


def conversation_id(value=None):
    value = value or str(uuid.uuid4())
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ReviewError("Conversation ID must be 1–128 letters, digits, periods, underscores, colons, or hyphens.")
    return value


def normalize_usage(provider, raw):
    raw = raw or {}
    usage = raw.get("usage") or {}
    if provider == "ollama":
        result = {"input_tokens": raw.get("prompt_eval_count"), "output_tokens": raw.get("eval_count"),
                  "cached_input_tokens": 0, "cache_write_input_tokens": 0}
    elif provider == "anthropic":
        fresh, read, write = usage.get("input_tokens"), usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0)
        total = fresh + read + write if all(type(x) is int and x >= 0 for x in [fresh, read, write]) else None
        details = usage.get("cache_creation") or {}
        result = {"input_tokens": total, "output_tokens": usage.get("output_tokens"),
                  "cached_input_tokens": read, "cache_write_input_tokens": write,
                  "cache_write_5m_tokens": details.get("ephemeral_5m_input_tokens", 0),
                  "cache_write_1h_tokens": details.get("ephemeral_1h_input_tokens", 0)}
    else:
        compatible = provider == "openai-compatible"
        details = usage.get("prompt_tokens_details" if compatible else "input_tokens_details") or {}
        output_details = usage.get("completion_tokens_details" if compatible else "output_tokens_details") or {}
        result = {"input_tokens": usage.get("prompt_tokens" if compatible else "input_tokens"),
                  "output_tokens": usage.get("completion_tokens" if compatible else "output_tokens"),
                  "cached_input_tokens": details.get("cached_tokens", 0), "cache_write_input_tokens": 0,
                  "reasoning_output_tokens": output_details.get("reasoning_tokens", 0)}
    return clean_usage(result)


def clean_usage(value):
    if not isinstance(value, dict):
        raise ReviewError("Usage must be an object.")
    result = {k: value.get(k, None if k in {"input_tokens", "output_tokens"} else 0) for k in TOKEN_FIELDS}
    for k, v in result.items():
        if v is not None and (type(v) is not int or not 0 <= v <= 10**12):
            raise ReviewError(f"Invalid token count: {k}.")
    return result


def rate_card(path, provider, model):
    if not path:
        return None
    try:
        with Path(path).expanduser().open("rb") as stream:
            doc = tomllib.load(stream)
        matches = [r for r in doc.get("rates", []) if r.get("provider") == provider and r.get("model") == model]
        if len(matches) > 1:
            raise ReviewError("Duplicate provider/model entries in pricing file.")
        if not matches:
            return None
        rate = matches[0]
        if not all(isinstance(rate.get(k), str) and rate[k].strip() for k in ["source", "effective_date"]):
            raise ReviewError("Each rate requires source and effective_date strings.")
        for k, v in rate.items():
            if k.endswith("_usd_per_million"):
                amount = Decimal(str(v))
                if not amount.is_finite() or amount < 0:
                    raise ReviewError("Rate values must be finite nonnegative USD prices per million tokens.")
        return rate
    except (OSError, ValueError, InvalidOperation, TypeError, AttributeError) as exc:
        raise ReviewError("Invalid or unreadable pricing TOML.") from exc


def price(usage, rate, free=False):
    if free:
        return {"estimated_usd": "0", "basis": "No remote model charge for this event; local compute/subscription costs excluded."}
    if not rate:
        return {"estimated_usd": None, "basis": "No matching provider/model rate card."}
    u = clean_usage(usage)
    if any(u[k] is None for k in ["input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens"]):
        return {"estimated_usd": None, "basis": "Provider usage is unavailable or incomplete."}
    fresh = u["input_tokens"] - u["cached_input_tokens"] - u["cache_write_input_tokens"]
    write5, write1 = u["cache_write_5m_tokens"] or 0, u["cache_write_1h_tokens"] or 0
    unspecified_write = u["cache_write_input_tokens"] - write5 - write1
    if fresh < 0 or unspecified_write < 0 or (u["reasoning_output_tokens"] or 0) > u["output_tokens"]:
        return {"estimated_usd": None, "basis": "Inconsistent provider token breakdown."}
    buckets = {"input": fresh, "cached_input": u["cached_input_tokens"], "output": u["output_tokens"],
               "cache_write_5m": write5, "cache_write_1h": write1, "cache_write": unspecified_write}
    total = Decimal(0)
    for kind, tokens in buckets.items():
        if tokens:
            key = kind + "_usd_per_million"
            if key not in rate:
                return {"estimated_usd": None, "basis": f"Missing price for {kind} tokens."}
            total += Decimal(tokens) * Decimal(str(rate[key])) / Decimal(1_000_000)
    return {"estimated_usd": str(total), "basis": "Observed tokens × configured USD rates; not an invoice.",
            "rate": rate}


class Ledger:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, conversation TEXT NOT NULL, created_at TEXT NOT NULL, operation TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL, usage TEXT NOT NULL, cost TEXT NOT NULL, reservation TEXT, context_chars INTEGER NOT NULL, elapsed REAL NOT NULL DEFAULT 0)")
            db.execute("CREATE INDEX IF NOT EXISTS events_conversation ON events(conversation)")
            db.execute("CREATE TABLE IF NOT EXISTS review_cache (key TEXT PRIMARY KEY, created REAL NOT NULL, report TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self, cfg, conversation, operation, context_chars, reservation=None):
        conversation = conversation_id(conversation)
        identifier = str(uuid.uuid4())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM events WHERE conversation=? AND origin='agent'", (conversation,)).fetchall()
            calls = [r for r in rows if r["status"] not in {"cache_hit", "no_changes"}]
            if len(calls) >= cfg.max_calls_per_conversation:
                raise ReviewError("Conversation call limit reached; inspect costs before continuing.")
            if cfg.max_conversation_usd is not None:
                total = Decimal(0)
                for row in calls:
                    amount = row["reservation"] if row["status"] == "started" else json.loads(row["cost"])["estimated_usd"]
                    if amount is None:
                        raise ReviewError("Budget guard blocked: a prior agent call has unknown cost. Reconcile its usage first.")
                    total += Decimal(amount)
                if reservation is None:
                    raise ReviewError("Budget guard requires a matching rate and a preflight estimate.")
                if total + Decimal(reservation) > Decimal(str(cfg.max_conversation_usd)):
                    raise ReviewError("Conversation estimated spend guard would be exceeded.")
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identifier, conversation, datetime.now(timezone.utc).isoformat(), operation, cfg.provider, cfg.model,
                "started", "agent", "{}", json.dumps({"estimated_usd": None, "basis": "Request in progress."}),
                reservation, context_chars, 0))
        return identifier

    def finish(self, identifier, usage, cost, status, elapsed=0):
        with self.connection() as db:
            db.execute("UPDATE events SET usage=?,cost=?,status=?,reservation=NULL,elapsed=? WHERE id=?",
                       (json.dumps(clean_usage(usage)), json.dumps(cost), status, elapsed, identifier))

    def external(self, conversation, origin="editor_unobservable", provider="editor", model="host-selected", usage=None, rate=None, identifier=None):
        conversation = conversation_id(conversation)
        identifier = identifier or str(uuid.uuid4())
        if any(not isinstance(x, str) or not 1 <= len(x) <= 200 for x in [provider, model]):
            raise ReviewError("Imported provider/model must be non-empty names of at most 200 characters.")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 200:
            raise ReviewError("Invalid imported event ID.")
        usage = clean_usage(usage or {})
        if origin not in {"editor_unobservable", "imported"}:
            raise ReviewError("External usage must be editor_unobservable or imported.")
        cost = price(usage, rate) if origin == "imported" else {"estimated_usd": None, "basis": "Editor model usage is not exposed by this MCP boundary."}
        with self.connection() as db:
            existing = db.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone()
            if existing:
                if existing["conversation"] != conversation or existing["provider"] != provider or existing["model"] != model or json.loads(existing["usage"]) != usage:
                    raise ReviewError("Imported event ID conflicts with an existing event.")
                return identifier
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identifier, conversation, datetime.now(timezone.utc).isoformat(), "editor_handoff" if origin == "editor_unobservable" else "import",
                provider, model, "unobserved" if origin == "editor_unobservable" else "imported", origin,
                json.dumps(usage), json.dumps(cost), None, 0, 0))
        return identifier

    def reconcile(self, identifier, conversation, provider, model, usage, rate):
        """Explicitly replace an unobservable handoff/error with user-supplied measured usage."""
        conversation_id(conversation)
        usage = clean_usage(usage)
        cost = price(usage, rate)
        cost["usage_source"] = "user_supplied_reconciliation"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone()
            if not row or row["conversation"] != conversation or row["status"] not in {"unobserved", "error"}:
                raise ReviewError("Reconciliation requires an existing unobserved/editor or failed event in this conversation.")
            if row["origin"] == "agent" and (row["provider"] != provider or row["model"] != model):
                raise ReviewError("Reconciled provider/model differs from the actual agent request.")
            origin = "agent" if row["origin"] == "agent" else "imported"
            db.execute("UPDATE events SET provider=?,model=?,usage=?,cost=?,status='reconciled',origin=? WHERE id=?",
                       (provider, model, json.dumps(usage), json.dumps(cost), origin, identifier))
        return identifier

    def summary(self, conversation=None):
        if conversation:
            conversation_id(conversation)
        with self.connection() as db:
            rows = db.execute("SELECT * FROM events" + (" WHERE conversation=?" if conversation else "") + " ORDER BY created_at", (conversation,) if conversation else ()).fetchall()
        groups = {}
        for row in rows:
            group = groups.setdefault(row["conversation"], {"conversation": row["conversation"], "events": [], "known_estimated_usd": Decimal(0),
                                      "unknown_cost_events": 0, "input_tokens_observed": 0, "output_tokens_observed": 0,
                                      "cached_input_tokens_observed": 0, "usage_missing_events": 0, "cache_hits": 0})
            event = dict(row)
            event["usage"], event["cost"] = json.loads(row["usage"]), json.loads(row["cost"])
            event.pop("reservation")
            group["events"].append(event)
            amount = event["cost"]["estimated_usd"]
            if amount is None:
                group["unknown_cost_events"] += 1
            else:
                group["known_estimated_usd"] += Decimal(amount)
            u = event["usage"]
            for key in ["input_tokens", "output_tokens", "cached_input_tokens"]:
                group[key + "_observed"] += u.get(key) or 0
            group["usage_missing_events"] += int(u.get("input_tokens") is None or u.get("output_tokens") is None)
            group["cache_hits"] += int(row["status"] == "cache_hit")
        for group in groups.values():
            group["known_estimated_usd"] = str(group["known_estimated_usd"])
            group["cost_complete_for_recorded_events"] = group["unknown_cost_events"] == 0
            group["recommendations"] = []
            if group["unknown_cost_events"]:
                group["recommendations"].append("Resolve missing rates/usage before interpreting the known-cost subtotal as a total.")
            if any(e["context_chars"] > 20000 for e in group["events"]):
                group["recommendations"].append("Use smaller staged changes and fewer requirement documents; several requests carry large contexts.")
            if any(e["status"] == "error" for e in group["events"]):
                group["recommendations"].append("Inspect provider or schema errors before retrying; failed calls may still consume tokens.")
            if not group["cache_hits"]:
                group["recommendations"].append("Enable exact-review caching if you repeatedly review unchanged code and requirements.")
            group["recommendations"].append("Use the existing editor model or local Ollama for the first pass; reserve a second cloud review for higher-risk changes.")
        return {"currency": "USD", "scope": "Recorded events only; not all editor activity or subscription billing.", "conversations": list(groups.values())}
