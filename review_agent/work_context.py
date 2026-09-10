import hashlib
import json
from pathlib import Path
import re
from .config import ReviewError
from .git_context import collect, commit, repo_root
from .privacy import redact


def read_context(path):
    if not path:
        return None
    try:
        with Path(path).expanduser().open("rb") as stream:
            data = stream.read(200_001)
        if len(data) > 200_000:
            raise ReviewError("Work context file exceeds 200 KB.")
        context = json.loads(data)
        docs = context["documents"]
        if context.get("work_context_version") != "1" or not isinstance(docs, list) or len(docs) > 10:
            raise ReviewError("Invalid work context version or document list.")
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") not in {"bitbucket", "jira", "confluence", "slack"}:
                raise ReviewError("Unsupported work context document.")
            for key in ["text", "title", "id", "url", "source_version", "retrieved_at"]:
                if not isinstance(doc.get(key), str):
                    raise ReviewError("Work context fields must be text.")
            doc["text"] = redact(doc["text"])[0]
        pin = context.get("bitbucket_pin")
        if pin is not None:
            if not isinstance(pin, dict) or any(not isinstance(pin.get(k), str) or not re.fullmatch(r"[0-9a-f]{40,64}", pin[k]) for k in ["source_commit", "destination_commit"]):
                raise ReviewError("Invalid Bitbucket commit pin.")
        return context
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReviewError("Cannot read work context JSON.") from exc


def attach(snapshot, cfg, context=None):
    context = context if context is not None else read_context(cfg.work_context)
    if not context:
        return snapshot
    budget = min(12000, max(0, cfg.max_context_chars - snapshot["context_chars"]))
    docs, omitted, used = [], [], 0
    for original in context["documents"]:
        doc = {key: original[key] for key in ["kind", "id", "title", "text", "url", "source_version", "retrieved_at"]}
        doc["truncated"] = bool(original.get("truncated"))
        size = len(json.dumps(doc, ensure_ascii=True))
        if used + size > budget:
            omitted.append({"kind": doc["kind"], "id": doc["id"], "reason": "requirements context budget"})
            continue
        used += size
        docs.append(doc)
    linked = {"documents": docs, "omitted": omitted, "context_chars": used,
              "incomplete": bool(omitted or any(x["truncated"] for x in docs)),
              "bitbucket_pin": context.get("bitbucket_pin")}
    snapshot = dict(snapshot)
    snapshot["work_context"] = linked
    snapshot["snapshot_id"] = hashlib.sha256(json.dumps({"code_snapshot": snapshot["snapshot_id"], "work_context": linked}, sort_keys=True).encode()).hexdigest()
    return snapshot


def prepare_snapshot(repo, cfg, mode="worktree", base=None):
    context = read_context(cfg.work_context)
    if context and context.get("bitbucket_pin"):
        pin = context["bitbucket_pin"]
        head = commit(repo_root(repo), "HEAD")
        if head != pin["source_commit"]:
            raise ReviewError("Local HEAD differs from the selected Bitbucket PR source commit. Fetch and check out that commit locally first.")
        if mode == "staged" or (base and commit(repo_root(repo), base) != pin["destination_commit"]):
            raise ReviewError("Pinned PR context requires its committed source/destination scope.")
        mode, base = "base", pin["destination_commit"]
    return attach(collect(repo, cfg, mode, base), cfg, context)
