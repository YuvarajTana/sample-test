import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import sys
from dataclasses import replace
from .config import ReviewError, load_config
from .demo import create_demo
from .engine import review, markdown, finalize
from .git_context import collect
from .prompts import bundle
from .schema import SEVERITIES
from .usage import Ledger, conversation_id, rate_card
from .work_context import prepare_snapshot


def read_json_file(path):
    with Path(path).expanduser().open("rb") as stream:
        raw = stream.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ReviewError("Input JSON exceeds 2 MB.")
    return json.loads(raw)


def usage_markdown(result):
    lines = ["# Conversation usage", "", result["scope"], "",
             "| Conversation | Observed input | Observed output | Known estimated USD | Unknown-cost events | Cache hits |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for x in result["conversations"]:
        lines.append(f"| {x['conversation']} | {x['input_tokens_observed']} | {x['output_tokens_observed']} | {x['known_estimated_usd']} | {x['unknown_cost_events']} | {x['cache_hits']} |")
    lines += ["", "Known cost is a subtotal when any event has unknown cost. Local compute and subscriptions are excluded."]
    return "\n".join(lines)


def write_output(text, path=None):
    if path:
        # Exclusive create prevents accidentally overwriting source code or old reports.
        with Path(path).expanduser().open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
        print(f"Wrote {path}", file=sys.stderr)
    else:
        print(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local code reviews across LLMs and AI editors.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["review", "context", "mcp", "serve", "doctor", "generate", "review-candidate", "usage", "import-usage"]:
        command = sub.add_parser(name)
        command.add_argument("--repo", default=".")
        command.add_argument("--config")
        command.add_argument("--provider", choices=["ollama", "openai", "anthropic", "openai-compatible", "demo"])
        command.add_argument("--model")
        command.add_argument("--base-url")
        command.add_argument("--allow-remote", action="store_true", default=None)
        command.add_argument("--conversation")
        command.add_argument("--usage-db")
        command.add_argument("--pricing", dest="pricing_file")
        command.add_argument("--work-context")
        command.add_argument("--max-calls", type=int, dest="max_calls_per_conversation")
        command.add_argument("--max-usd", type=float, dest="max_conversation_usd")
        command.add_argument("--cache-seconds", type=int, dest="review_cache_seconds")
        command.add_argument("--local-inference", action="store_const", const="local", default=None, dest="billing_mode")
        if name in {"review", "context"}:
            scope = command.add_mutually_exclusive_group()
            scope.add_argument("--staged", action="store_true")
            scope.add_argument("--base")
            command.add_argument("--output")
        if name in {"review", "review-candidate", "usage"}:
            command.add_argument("--format", choices=["json", "markdown"], default="markdown")
        if name in {"review", "review-candidate"}:
            command.add_argument("--fail-on", choices=[*SEVERITIES, "none"], default="none")
        if name in {"generate", "review-candidate", "usage", "import-usage"}:
            command.add_argument("--output")
        if name == "generate":
            command.add_argument("--task-file", required=True)
            command.add_argument("--path", dest="paths", action="append", required=True)
            command.add_argument("--previous")
            command.add_argument("--feedback")
        if name in {"review-candidate", "import-usage"}:
            command.add_argument("--input", required=True)
        if name == "import-usage":
            command.add_argument("--reconcile", action="store_true")
        if name == "serve":
            command.add_argument("--port", type=int, default=8765)
    demo = sub.add_parser("demo")
    demo.add_argument("--path", default="examples/demo-repo")
    validate = sub.add_parser("validate")
    validate.add_argument("--bundle", required=True)
    validate.add_argument("--input", required=True)
    validate.add_argument("--output")
    linked = sub.add_parser("work-context")
    linked.add_argument("--integrations", required=True)
    linked.add_argument("--pr")
    linked.add_argument("--jira", action="append", default=[])
    linked.add_argument("--confluence", action="append", default=[])
    linked.add_argument("--slack-channel")
    linked.add_argument("--slack-thread")
    linked.add_argument("--output", required=True)
    draft = sub.add_parser("slack-draft")
    draft.add_argument("--input", required=True)
    draft.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "work-context":
            from .integrations import WorkTools
            # Fail before API reads if the requested output already exists.
            if Path(args.output).expanduser().exists():
                raise FileExistsError()
            context = WorkTools(args.integrations).collect(args.pr, args.jira, args.confluence, args.slack_channel, args.slack_thread)
            write_output(json.dumps(context, indent=2), args.output)
            return 0
        if args.command == "slack-draft":
            from .integrations import slack_draft
            write_output(slack_draft(read_json_file(args.input)), args.output)
            return 0
        if args.command == "demo":
            path = create_demo(args.path)
            command = shlex.join(["python3", "-m", "review_agent", "review", "--repo", str(path), "--provider", "demo"])
            print(f"Created synthetic Git repo: {path}\nReview with: {command}")
            return 0
        if args.command == "validate":
            packed = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
            raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
            report = finalize(packed["snapshot"], raw, "editor", "host-selected")
            report["limitations"].append("Offline bundle validation does not check whether your current repository has changed.")
            write_output(json.dumps(report, indent=2, allow_nan=False), args.output)
            return 3 if report["status"] == "incomplete" else 0
        cfg = load_config(args.config, **{key: getattr(args, key) for key in ["provider", "model", "base_url", "allow_remote", "usage_db",
                          "pricing_file", "work_context", "max_calls_per_conversation", "max_conversation_usd", "review_cache_seconds", "billing_mode"]})
        if not cfg.usage_db:
            cfg = replace(cfg, usage_db=os.environ.get("REVIEW_AGENT_USAGE_DB", str(Path.home() / ".local/share/review-agent/usage.sqlite3")))
        if args.conversation:
            conversation_id(args.conversation)
        if getattr(args, "output", None) and Path(args.output).expanduser().exists():
            raise FileExistsError()
        if args.command == "usage":
            summary = Ledger(cfg.usage_db).summary(args.conversation)
            write_output(json.dumps(summary, indent=2) if args.format == "json" else usage_markdown(summary), args.output)
            return 0
        if args.command == "import-usage":
            event = read_json_file(args.input)
            if not isinstance(event, dict) or set(event) != {"event_id", "conversation", "provider", "model", "usage"}:
                raise ReviewError("Usage import requires event_id, conversation, provider, model, and usage only.")
            rate = rate_card(cfg.pricing_file, event["provider"], event["model"])
            ledger = Ledger(cfg.usage_db)
            if args.reconcile:
                event_id = ledger.reconcile(event["event_id"], event["conversation"], event["provider"], event["model"], event["usage"], rate)
            else:
                event_id = ledger.external(event["conversation"], "imported", event["provider"], event["model"], event["usage"], rate, event["event_id"])
            write_output(json.dumps({"event_id": event_id, "origin": "imported", "idempotent": True}), args.output)
            return 0
        if args.command == "generate":
            from .generation import generate_candidate
            with Path(args.task_file).expanduser().open(encoding="utf-8") as task_file:
                task = task_file.read(8001)
            proposal = generate_candidate(args.repo, cfg, task, args.paths, args.conversation,
                         read_json_file(args.previous) if args.previous else None, read_json_file(args.feedback) if args.feedback else None)
            write_output(json.dumps(proposal, indent=2), args.output)
            return 0
        if args.command == "review-candidate":
            from .generation import review_candidate
            report = review_candidate(args.repo, cfg, read_json_file(args.input), args.conversation)
            write_output(json.dumps(report, indent=2) if args.format == "json" else markdown(report), args.output)
            if report["status"] == "incomplete" or (report["status"] == "demo" and args.fail_on != "none"):
                return 3
            return 1 if args.fail_on != "none" and any(SEVERITIES[f["severity"]] <= SEVERITIES[args.fail_on] for f in report["findings"]) else 0
        if args.command == "doctor":
            result = {"python": sys.version.split()[0], "git_installed": bool(shutil.which("git")),
                      "provider": cfg.provider, "model": cfg.model, "dependency_installs_required": False,
                      "remote_enabled": cfg.allow_remote,
                      "note": "Configuration check only; use review on the demo repo to verify live inference."}
            if cfg.provider != "demo":
                result["endpoint"] = cfg.endpoint()
            print(json.dumps(result, indent=2))
            return 0 if result["git_installed"] else 2
        if args.command in {"review", "context"}:
            mode = "base" if args.base else "staged" if args.staged else "worktree"
            if args.command == "context":
                packed = bundle(prepare_snapshot(args.repo, cfg, mode, args.base))
                selected_conversation = conversation_id(args.conversation)
                event_id = Ledger(cfg.usage_db).external(selected_conversation)
                packed["accounting"] = {"conversation_id": selected_conversation, "event_id": event_id,
                                        "cost": "unknown", "reason": "Host editor/chat usage is not observed by context export."}
                write_output(json.dumps(packed, indent=2, ensure_ascii=True), args.output)
                return 0
            report = review(args.repo, cfg, mode, args.base, args.conversation)
            write_output(markdown(report) if args.format == "markdown" else json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False), args.output)
            if report["status"] == "incomplete":
                return 3
            if args.fail_on != "none":
                if report["status"] == "demo":
                    return 3  # A deterministic demo must never produce a passing production gate.
                if any(SEVERITIES[f["severity"]] <= SEVERITIES[args.fail_on] for f in report["findings"]):
                    return 1
            return 0
        from .service import ReviewService
        service = ReviewService(args.repo, cfg, args.conversation)
        if args.command == "mcp":
            from .mcp_server import MCPServer
            MCPServer(service).run()
            return 0
        from .http_server import create_server
        server = create_server(service, os.environ.get("REVIEW_AGENT_TOKEN", ""), args.port)
        print(f"Local REST API: http://127.0.0.1:{server.server_port} (bearer token required)", file=sys.stderr)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    except FileExistsError:
        print("Error: output already exists. Choose a new filename to preserve existing files.", file=sys.stderr)
        return 2
    except (ReviewError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError, TypeError):
        print("Error: invalid configuration, bundle, or review JSON.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
