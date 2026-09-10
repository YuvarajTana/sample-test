from datetime import datetime, timezone
import time

from .config import Config
from .git_context import collect
from .providers import generate
from .schema import validate_review
from .accounting import Meter
from .config import ReviewError
from .prompts import SYSTEM_PROMPT, user_prompt
from .work_context import prepare_snapshot


def finalize(snapshot, raw, provider, model, usage=None, elapsed=0):
    summary, findings, rejected = validate_review(raw, snapshot)
    incomplete = bool(snapshot["skipped"] or rejected or snapshot.get("work_context", {}).get("incomplete"))
    return {"report_version": "1", "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_id": snapshot["snapshot_id"], "head": snapshot["head"], "mode": snapshot["mode"],
            "provider": provider, "model": "deterministic-rules" if provider == "demo" else model,
            "status": "incomplete" if incomplete else "demo" if provider == "demo" else "completed",
            "summary": summary, "findings": findings, "rejected_findings": rejected,
            "coverage": {"changed_files": snapshot["changed_files"], "reviewed_files": [x["path"] for x in snapshot["files"]],
                         "skipped": snapshot["skipped"], "context_chars": snapshot["context_chars"],
                         "redactions": snapshot["redactions"], "scope": snapshot["scope"]},
            "usage": usage or {"input_tokens": None, "output_tokens": None}, "elapsed_seconds": round(elapsed, 3),
            "limitations": ["Location and evidence validation does not prove a finding is correct.",
                            "No code or project tests were executed. A clean report is not approval to merge."]}


def review_snapshot(snapshot, cfg, conversation=None, operation="review", freshness=None):
    meter = Meter(cfg, conversation)
    key = meter.cache_key(snapshot, SYSTEM_PROMPT)
    cached = meter.cached(key)
    if cached:
        if freshness and not freshness():
            cached["stale"] = True
            cached["status"] = "incomplete"
        return cached
    start = time.monotonic()
    meter.start(operation, SYSTEM_PROMPT + user_prompt(snapshot))
    usage = {}
    try:
        raw, usage = generate(cfg, snapshot)
        report = finalize(snapshot, raw, cfg.provider, cfg.model, usage, time.monotonic() - start)
        report["stale"] = bool(freshness and not freshness())
        if report["stale"]:
            report["status"] = "incomplete"
            report["limitations"].append("The change moved during inference. Review the latest snapshot before acting.")
        report["accounting"] = meter.finish(usage, report["status"])
        report["cache"] = {"hit": False}
        linked = snapshot.get("work_context", {})
        report["requirements"] = {"sources": [{k: x[k] for k in ["kind", "id", "title", "url", "source_version", "retrieved_at", "truncated"]} for x in linked.get("documents", [])],
                                  "omitted": linked.get("omitted", []), "incomplete": linked.get("incomplete", False)}
        meter.save_cache(key, report)
        return report
    except Exception as exc:
        meter.finish(getattr(exc, "usage", usage), "error")
        raise


def review(repo, cfg: Config, mode="worktree", base=None, conversation=None):
    snapshot = prepare_snapshot(repo, cfg, mode, base)
    if not snapshot["files"]:
        report = finalize(snapshot, {"summary": "No reviewable changed files.", "findings": []}, cfg.provider, cfg.model)
        report["status"] = "incomplete" if snapshot["skipped"] else "no_changes"
        return report
    return review_snapshot(snapshot, cfg, conversation, freshness=lambda: prepare_snapshot(repo, cfg, mode, base)["snapshot_id"] == snapshot["snapshot_id"])


def markdown(report: dict) -> str:
    lines = ["# Code review", "", f"Status: **{report['status']}** · Provider: `{report['provider']}` · Model: `{report['model']}`", "",
             report["summary"], "", f"Snapshot: `{report['snapshot_id']}`", "",
             f"Reviewed {len(report['coverage']['reviewed_files'])} of {report['coverage']['changed_files']} changed files.", ""]
    for finding in report["findings"]:
        lines += [f"## {finding['severity'].upper()}: {finding['title']}", "",
                  f"`{finding['path']}:{finding['line']}` ({finding['side']} side) · Confidence: {finding['confidence']}", "",
                  finding["description"], "", "Suggested fix: " + finding["suggestion"], "",
                  "Evidence: " + repr(finding["evidence"]), ""]
    if not report["findings"]:
        lines += ["No validated findings. This does not establish that the change is safe.", ""]
    if report["coverage"]["skipped"]:
        lines += ["## Skipped coverage", ""]
        lines += [f"- `{x['path']}`: {x['reason']}" for x in report["coverage"]["skipped"]]
    if report["rejected_findings"]:
        lines += ["", f"Rejected {len(report['rejected_findings'])} model findings due to invalid fields, location, or evidence."]
    if report.get("accounting"):
        accounting = report["accounting"]
        cost = accounting["cost"]["estimated_usd"]
        lines += ["", "## Usage and cost", "", f"Conversation: `{accounting['conversation_id']}`",
                  f"Input tokens: {report['usage'].get('input_tokens')} · Output tokens: {report['usage'].get('output_tokens')}",
                  "Estimated USD for this call: " + (cost if cost is not None else "unknown"), accounting["cost"]["basis"]]
    if report.get("requirements", {}).get("sources"):
        lines += ["", "## Requirements sources", ""]
        lines += [f"- {x['kind']} {x['id']}: {x['title']} (version {x['source_version']})" for x in report["requirements"]["sources"]]
    lines += ["", *report["limitations"], ""]
    return "\n".join(lines)
