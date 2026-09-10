from collections import OrderedDict
from threading import RLock
from .config import Config, ReviewError
from .engine import review, finalize
from .git_context import collect, repo_root
from .prompts import bundle
from .schema import REVIEW_SCHEMA
from .work_context import prepare_snapshot
from .usage import Ledger, conversation_id

SCOPE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "mode": {"type": "string", "enum": ["worktree", "staged", "base"], "default": "worktree"},
    "base": {"type": "string", "description": "Required only for mode=base, e.g. main."},
    "conversation": {"type": "string", "description": "Stable conversation or Jira-ticket ID for usage grouping."}}}


class ReviewService:
    """One process, one repository, one operator-controlled provider config."""
    def __init__(self, repo, cfg: Config, conversation=None):
        self.repo = repo_root(repo)
        self.cfg = cfg
        self.cache = OrderedDict()
        self.lock = RLock()
        self.conversation = conversation_id(conversation)

    def tools(self):
        return [
            {"name": "get_cost_summary", "description": "Read recorded token usage and estimated cost for a conversation. Unknown editor usage is not treated as zero.",
             "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"conversation": {"type": "string"}}},
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
            {"name": "get_review_context", "description": "Read bounded, best-effort-redacted Git changes from the pinned repository. The calling editor's model can review this bundle. Source code is exposed to that editor/model; no independent LLM call is made.",
             "inputSchema": SCOPE_SCHEMA,
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
            {"name": "run_review", "description": "Review pinned repository changes using the server's configured LLM provider. May incur API charges if the operator configured a remote provider. Returns validated findings and coverage; never edits code.",
             "inputSchema": SCOPE_SCHEMA,
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}},
            {"name": "validate_review", "description": "Validate your model's review JSON against a snapshot previously obtained from get_review_context in this server session. Reject unsupported paths, lines, and evidence. Also flag if the current change has moved.",
             "inputSchema": {"type": "object", "additionalProperties": False, "required": ["snapshot_id", "review"],
                             "properties": {"snapshot_id": {"type": "string"}, "review": REVIEW_SCHEMA}},
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
        ]

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ReviewError("Tool arguments must be a JSON object.")
        with self.lock:
            if name == "get_cost_summary":
                if set(arguments) - {"conversation"}:
                    raise ReviewError("Only conversation is allowed.")
                if not self.cfg.usage_db:
                    raise ReviewError("Configure usage_db to enable accounting.")
                return Ledger(self.cfg.usage_db).summary(arguments.get("conversation") or self.conversation)
            if name in {"get_review_context", "run_review"}:
                if set(arguments) - {"mode", "base", "conversation"}:
                    raise ReviewError("Only mode, base, and conversation are allowed. Repository/provider are fixed at startup.")
                mode, base = arguments.get("mode", "worktree"), arguments.get("base")
                selected_conversation = conversation_id(arguments.get("conversation") or self.conversation)
                if not isinstance(mode, str) or (base is not None and not isinstance(base, str)):
                    raise ReviewError("mode and base must be strings.")
                if name == "run_review":
                    return review(self.repo, self.cfg, mode, base, selected_conversation)
                snapshot = prepare_snapshot(self.repo, self.cfg, mode, base)
                self.cache[snapshot["snapshot_id"]] = (snapshot, base)
                self.cache.move_to_end(snapshot["snapshot_id"])
                while len(self.cache) > 8:
                    self.cache.popitem(last=False)
                result = bundle(snapshot)
                if self.cfg.usage_db:
                    result["accounting"] = {"conversation_id": selected_conversation,
                      "event_id": Ledger(self.cfg.usage_db).external(selected_conversation),
                      "cost": "unknown", "reason": "Editor model usage is not exposed by this MCP boundary."}
                return result
            if name == "validate_review":
                if set(arguments) != {"snapshot_id", "review"} or not isinstance(arguments["snapshot_id"], str):
                    raise ReviewError("Supply snapshot_id and review only.")
                cached = self.cache.get(arguments["snapshot_id"])
                if not cached:
                    raise ReviewError("Snapshot expired or unknown. Call get_review_context again.")
                snapshot, base = cached
                report = finalize(snapshot, arguments["review"], "editor", "host-selected")
                current = prepare_snapshot(self.repo, self.cfg, snapshot["mode"], base)
                report["stale"] = current["snapshot_id"] != snapshot["snapshot_id"]
                if report["stale"]:
                    report["status"] = "incomplete"
                    report["limitations"].append("The change has moved since context collection. Review the latest snapshot before acting.")
                return report
            raise ReviewError("Unknown review tool.")
