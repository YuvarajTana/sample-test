import math
from .config import ReviewError
from .privacy import redact

SEVERITIES = {"critical": 0, "high": 1, "medium": 2, "low": 3}
FINDING_PROPERTIES = {
    "path": {"type": "string"}, "line": {"type": "integer"},
    "side": {"type": "string", "enum": ["new", "old"]},
    "severity": {"type": "string", "enum": list(SEVERITIES)},
    "confidence": {"type": "number"}, "title": {"type": "string"},
    "description": {"type": "string"}, "suggestion": {"type": "string"},
    "evidence": {"type": "string"},
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "findings"],
    "properties": {"summary": {"type": "string"}, "findings": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": list(FINDING_PROPERTIES), "properties": FINDING_PROPERTIES}}},
}


def validate_review(raw: dict, snapshot: dict):
    if not isinstance(raw, dict) or set(raw) != {"summary", "findings"}:
        raise ReviewError("Provider output must contain only summary and findings.")
    if not isinstance(raw["summary"], str) or not 1 <= len(raw["summary"]) <= 4000:
        raise ReviewError("Provider returned an invalid summary.")
    if not isinstance(raw["findings"], list) or len(raw["findings"]) > 100:
        raise ReviewError("Provider findings must be a list with at most 100 entries.")
    files = {f["path"]: f for f in snapshot["files"]}
    accepted, rejected, seen = [], [], set()
    for index, item in enumerate(raw["findings"]):
        reason = None
        if not isinstance(item, dict) or set(item) != set(FINDING_PROPERTIES):
            reason = "invalid finding fields"
        elif any(not isinstance(item[k], str) or not item[k].strip() or len(item[k]) > 6000
                 for k in ["path", "side", "severity", "title", "description", "suggestion", "evidence"]):
            reason = "invalid text field"
        elif type(item["line"]) is not int or item["line"] < 1 or item["side"] not in {"new", "old"}:
            reason = "invalid line or side"
        elif item["severity"] not in SEVERITIES:
            reason = "invalid severity"
        elif type(item["confidence"]) not in {int, float} or not math.isfinite(item["confidence"]) or not 0 <= item["confidence"] <= 1:
            reason = "invalid confidence"
        elif item["path"] not in files:
            reason = "path outside reviewed snapshot"
        else:
            file = files[item["path"]]
            changed = file["added_lines"] if item["side"] == "new" else file["removed_lines"]
            line_text = file["new_lines" if item["side"] == "new" else "old_lines"].get(str(item["line"]), "")
            if item["line"] not in changed:
                reason = "anchor is not a changed line on the stated side"
            elif item["evidence"].strip() not in line_text or item["evidence"].strip() == "[REDACTED]":
                reason = "evidence does not match the cited line"
        if reason:
            # Do not echo untrusted invalid content into the report.
            rejected.append({"index": index, "reason": reason})
            continue
        key = (item["path"], item["line"], item["side"], item["title"].casefold())
        if key not in seen:
            seen.add(key)
            accepted.append({k: redact(v)[0] if isinstance(v, str) else v for k, v in item.items()})
    accepted.sort(key=lambda x: (SEVERITIES[x["severity"]], x["path"], x["line"]))
    return redact(raw["summary"])[0], accepted, rejected
