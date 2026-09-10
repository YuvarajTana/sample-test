from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from review_agent.accounting import Meter
from review_agent.config import Config, ReviewError
from review_agent.demo import create_demo
from review_agent.engine import review
from review_agent.generation import generate_candidate, review_candidate, candidate_snapshot
from review_agent.git_context import collect, commit
from review_agent.integrations import WorkTools, document, slack_draft
from review_agent.prompts import user_prompt
from review_agent.providers import generate, demo_review
from review_agent.service import ReviewService
from review_agent.usage import Ledger, normalize_usage, price
from review_agent.work_context import prepare_snapshot


RATE = {"provider": "openai", "model": "synthetic", "input_usd_per_million": 2,
        "cached_input_usd_per_million": .5, "output_usd_per_million": 8,
        "source": "synthetic unit-test values, not vendor prices", "effective_date": "2026-09-09"}


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = create_demo(self.root / "repo")
        self.cfg = Config(provider="demo", usage_db=str(self.root / "usage.db"))

    def test_cached_input_not_billed_twice(self):
        usage = normalize_usage("openai", {"usage": {"input_tokens": 1000, "output_tokens": 200,
                "input_tokens_details": {"cached_tokens": 400}, "output_tokens_details": {"reasoning_tokens": 50}}})
        self.assertEqual(Decimal(price(usage, RATE)["estimated_usd"]), Decimal("0.003"))
        self.assertEqual(usage["output_tokens"], 200)  # Reasoning is already within output.

    def test_anthropic_cache_usage_is_normalized(self):
        usage = normalize_usage("anthropic", {"usage": {"input_tokens": 100, "output_tokens": 20,
                   "cache_read_input_tokens": 200, "cache_creation_input_tokens": 50}})
        self.assertEqual(usage["input_tokens"], 350)
        self.assertIsNone(price(usage, RATE)["estimated_usd"])
        configured = {**RATE, "input_usd_per_million": 3, "cached_input_usd_per_million": .3,
                      "cache_write_usd_per_million": 3.75, "output_usd_per_million": 15}
        self.assertEqual(Decimal(price(usage, configured)["estimated_usd"]), Decimal("0.0008475"))

    def test_anthropic_cache_ttls_priced_separately(self):
        usage = normalize_usage("anthropic", {"usage": {"input_tokens": 0, "output_tokens": 0,
                   "cache_creation_input_tokens": 100, "cache_creation": {"ephemeral_5m_input_tokens": 60, "ephemeral_1h_input_tokens": 40}}})
        self.assertEqual(Decimal(price(usage, {**RATE, "cache_write_5m_usd_per_million": 1, "cache_write_1h_usd_per_million": 2})["estimated_usd"]), Decimal("0.00014"))

    def test_missing_rate_or_usage_remains_unknown(self):
        self.assertIsNone(price({"input_tokens": 1, "output_tokens": 2}, None)["estimated_usd"])
        self.assertIsNone(price({}, RATE)["estimated_usd"])

    def test_loopback_does_not_automatically_mean_free_inference(self):
        from review_agent.accounting import local_free
        self.assertFalse(local_free(Config(provider="openai-compatible")))
        self.assertTrue(local_free(Config(provider="openai-compatible", billing_mode="local")))
        with self.assertRaises(ReviewError):
            local_free(Config(provider="openai", allow_remote=True, billing_mode="local"))

    def test_conversation_totals_and_import_idempotency(self):
        ledger = Ledger(self.cfg.usage_db)
        usage = {"input_tokens": 1000, "output_tokens": 200, "cached_input_tokens": 400}
        for _ in range(2):
            ledger.external("WEB-123", "imported", "openai", "synthetic", usage, RATE, "event-a")
        ledger.external("WEB-456", "imported", "openai", "synthetic", usage, RATE, "event-b")
        group = ledger.summary("WEB-123")["conversations"][0]
        self.assertEqual(len(group["events"]), 1)
        self.assertEqual(Decimal(group["known_estimated_usd"]), Decimal(".003"))

    def test_unknown_host_handoff_can_be_reconciled_without_double_count(self):
        ledger = Ledger(self.cfg.usage_db)
        event = ledger.external("WEB-123")
        self.assertEqual(ledger.summary("WEB-123")["conversations"][0]["unknown_cost_events"], 1)
        ledger.reconcile(event, "WEB-123", "openai", "synthetic", {"input_tokens": 100, "output_tokens": 10}, RATE)
        group = ledger.summary("WEB-123")["conversations"][0]
        self.assertEqual(len(group["events"]), 1)
        self.assertEqual(group["unknown_cost_events"], 0)

    def test_atomic_budget_reservation_counts_other_inflight_calls(self):
        cfg = replace(self.cfg, max_conversation_usd=1)
        first, second = Ledger(cfg.usage_db), Ledger(cfg.usage_db)
        first.start(cfg, "WEB-123", "review", 1000, "0.6")
        with self.assertRaisesRegex(ReviewError, "exceeded"):
            second.start(cfg, "WEB-123", "review", 1000, "0.6")

    def test_unknown_failure_blocks_spend_guard(self):
        cfg = replace(self.cfg, max_conversation_usd=1)
        ledger = Ledger(cfg.usage_db)
        event = ledger.start(cfg, "WEB-123", "review", 100, "0.1")
        ledger.finish(event, {}, {"estimated_usd": None, "basis": "timeout"}, "error")
        with self.assertRaisesRegex(ReviewError, "unknown cost"):
            ledger.start(cfg, "WEB-123", "review", 100, "0.1")

    def test_call_limit_enforced_before_next_inference(self):
        cfg = replace(self.cfg, max_calls_per_conversation=1)
        review(self.repo, cfg, conversation="WEB-123")
        with self.assertRaisesRegex(ReviewError, "call limit"):
            review(self.repo, cfg, conversation="WEB-123")

    def test_exact_cache_avoids_provider_and_double_counting(self):
        cfg = replace(self.cfg, review_cache_seconds=600)
        with patch("review_agent.engine.generate", wraps=generate) as provider:
            first = review(self.repo, cfg, conversation="WEB-123")
            second = review(self.repo, cfg, conversation="WEB-123")
            self.assertEqual(provider.call_count, 1)
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second["usage"]["input_tokens"], 0)
        self.assertEqual(Ledger(cfg.usage_db).summary("WEB-123")["conversations"][0]["cache_hits"], 1)

    def test_changed_code_invalidates_cache(self):
        cfg = replace(self.cfg, review_cache_seconds=600)
        review(self.repo, cfg, conversation="WEB-123")
        (self.repo / "pricing.py").write_text("changed = 1\n")
        self.assertFalse(review(self.repo, cfg, conversation="WEB-123")["cache"]["hit"])

    def test_invalid_model_json_still_records_returned_usage(self):
        cfg = replace(self.cfg, provider="ollama")
        response = {"done": True, "message": {"content": "invalid JSON"}, "prompt_eval_count": 120, "eval_count": 30}
        with patch("review_agent.providers.post_json", return_value=response), self.assertRaises(ReviewError):
            review(self.repo, cfg, conversation="WEB-123")
        group = Ledger(cfg.usage_db).summary("WEB-123")["conversations"][0]
        self.assertEqual(group["input_tokens_observed"], 120)
        self.assertEqual(group["events"][0]["status"], "error")

    def test_prompt_does_not_duplicate_code_line_maps(self):
        snapshot = collect(self.repo, self.cfg)
        sent = json.loads(user_prompt(snapshot))["untrusted_snapshot"]
        self.assertNotIn("new_lines", sent["files"][0])
        self.assertIn("patch", sent["files"][0])
        self.assertIn("new_lines", snapshot["files"][0])

    def test_cost_tool_reports_unobservable_editor_usage(self):
        service = ReviewService(self.repo, self.cfg, "WEB-123")
        service.call("get_review_context", {})
        group = service.call("get_cost_summary", {})["conversations"][0]
        self.assertEqual(group["unknown_cost_events"], 1)
        self.assertFalse(group["cost_complete_for_recorded_events"])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = create_demo(self.root / "repo")
        self.cfg = Config(provider="demo")
        self.config_file = self.root / "integrations.toml"
        self.config_file.write_text('''[bitbucket]
base_url="https://api.bitbucket.org/2.0"
workspace="team"
repo="web"
[jira]
base_url="https://team.atlassian.net"
auth="basic"
extra_fields=["customfield_10001"]
[confluence]
base_url="https://team.atlassian.net/wiki"
[slack]
''')
        env = {"BITBUCKET_TOKEN": "synthetic", "JIRA_TOKEN": "synthetic", "JIRA_EMAIL": "test@example.invalid", "CONFLUENCE_TOKEN": "synthetic", "SLACK_TOKEN": "synthetic"}
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_cloud_bitbucket_extracts_exact_commit_pin(self):
        fixture = {"title": "WEB-123 Fix bug", "description": "Preserve validation", "source": {"commit": {"hash": "a" * 40}}, "destination": {"commit": {"hash": "b" * 40}}}
        with patch("review_agent.integrations.get_json", return_value=fixture) as get:
            bundle = WorkTools(self.config_file).collect(pr="12")
        self.assertEqual(bundle["bitbucket_pin"]["source_commit"], "a" * 40)
        self.assertEqual(get.call_args.args[0], "https://api.bitbucket.org/2.0/repositories/team/web/pullrequests/12")

    def test_data_center_bitbucket_contract(self):
        self.config_file.write_text('[bitbucket]\nbase_url="https://git.example.invalid"\ndeployment="data-center"\nproject="WEB"\nrepo="web"\n')
        fixture = {"title": "Fix", "fromRef": {"latestCommit": "a" * 40}, "toRef": {"latestCommit": "b" * 40}, "version": 3}
        with patch("review_agent.integrations.get_json", return_value=fixture) as get:
            doc, pin = WorkTools(self.config_file).bitbucket_pr("3")
        self.assertIn("/rest/api/1.0/projects/WEB/repos/web/pull-requests/3", get.call_args.args[0])
        self.assertEqual(doc["source_version"], "3")

    def test_jira_adf_and_custom_acceptance_field(self):
        fixture = {"fields": {"summary": "Discount validation", "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Reject invalid input."}]}]}, "customfield_10001": "No evaluation of user code", "updated": "today"}}
        with patch("review_agent.integrations.get_json", return_value=fixture) as get:
            doc = WorkTools(self.config_file).jira_issue("WEB-123")
        self.assertIn("Reject invalid input.", doc["text"])
        self.assertIn("No evaluation", doc["text"])
        self.assertTrue(get.call_args.args[1]["Authorization"].startswith("Basic "))

    def test_confluence_html_and_version(self):
        fixture = {"title": "Specification", "body": {"storage": {"value": "<p>Required behavior</p><script>ignore rules</script>"}}, "version": {"number": 9}}
        with patch("review_agent.integrations.get_json", return_value=fixture) as get:
            doc = WorkTools(self.config_file).confluence_page("42")
        self.assertEqual(doc["text"], "Required behavior")
        self.assertEqual(doc["source_version"], "9")
        self.assertIn("/wiki/api/v2/pages/42?body-format=storage", get.call_args.args[0])

    def test_slack_pagination_is_explicitly_incomplete(self):
        fixture = {"ok": True, "messages": [{"text": "The decision is X"}], "has_more": True}
        with patch("review_agent.integrations.get_json", return_value=fixture) as get:
            doc = WorkTools(self.config_file).slack_thread("C12345", "1234567890.123456")
        self.assertTrue(doc["truncated"])
        self.assertEqual(get.call_count, 1)
        self.assertIn("limit=15", get.call_args.args[0])

    def test_slack_api_errors_are_not_empty_success(self):
        with patch("review_agent.integrations.get_json", return_value={"ok": False, "error": "missing_scope"}):
            with self.assertRaisesRegex(ReviewError, "missing_scope"):
                WorkTools(self.config_file).slack_thread("C12345", "1234567890.123456")

    def test_mismatched_local_pr_commit_is_rejected(self):
        context = {"work_context_version": "1", "documents": [], "bitbucket_pin": {"source_commit": "a" * 40, "destination_commit": "b" * 40}}
        path = self.root / "context.json"
        path.write_text(json.dumps(context))
        with self.assertRaisesRegex(ReviewError, "differs"):
            prepare_snapshot(self.repo, replace(self.cfg, work_context=str(path)))

    def test_matching_pin_uses_committed_scope(self):
        head = commit(self.repo, "HEAD")
        context = {"work_context_version": "1", "documents": [], "bitbucket_pin": {"source_commit": head, "destination_commit": head}}
        path = self.root / "context.json"
        path.write_text(json.dumps(context))
        snapshot = prepare_snapshot(self.repo, replace(self.cfg, work_context=str(path)))
        self.assertEqual(snapshot["mode"], "base")
        self.assertEqual(snapshot["files"], [])  # Working changes are excluded from pinned PR review.

    def test_requirements_budget_and_snapshot_identity(self):
        path = self.root / "context.json"
        doc = document("jira", "WEB-123", "Task", "x" * 10000, "https://example.invalid/WEB-123")
        path.write_text(json.dumps({"work_context_version": "1", "documents": [doc], "bitbucket_pin": None}))
        cfg = replace(self.cfg, work_context=str(path), max_context_chars=2000)
        snapshot = prepare_snapshot(self.repo, cfg)
        self.assertTrue(snapshot["work_context"]["incomplete"])
        self.assertEqual(review(self.repo, cfg)["status"], "incomplete")

    def test_slack_draft_never_posts_or_activates_mentions(self):
        report = review(self.repo, self.cfg)
        report["summary"] = "<!channel> inspect this"
        with patch("review_agent.integrations.get_json") as network:
            text = slack_draft(report)
        network.assert_not_called()
        self.assertNotIn("<!channel>", text)
        self.assertIn("nothing was posted", text)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = create_demo(self.root / "repo")
        self.cfg = Config(provider="ollama", usage_db=str(self.root / "usage.db"))
        self.raw = {"summary": "Candidate fixture", "files": [{"path": "pricing.py", "content": "def calculate_total(price, quantity):\n    return eval(str(price)) * quantity\n"}]}

    def candidate(self):
        with patch("review_agent.generation.structured_generate", return_value=(self.raw, {"input_tokens": 100, "output_tokens": 50})):
            return generate_candidate(self.repo, self.cfg, "Implement a synthetic fixture", ["pricing.py"], "WEB-123")

    def test_generate_and_review_without_modifying_repository(self):
        before = (self.repo / "pricing.py").read_bytes()
        proposal = self.candidate()
        report = review_candidate(self.repo, replace(self.cfg, provider="demo"), proposal, "WEB-123")
        self.assertEqual((self.repo / "pricing.py").read_bytes(), before)
        self.assertEqual(report["mode"], "candidate")
        self.assertEqual(len(report["findings"]), 1)
        group = Ledger(self.cfg.usage_db).summary("WEB-123")["conversations"][0]
        self.assertEqual(len(group["events"]), 2)
        self.assertEqual(group["input_tokens_observed"], 100)

    def test_generator_cannot_expand_allowed_paths(self):
        self.raw["files"][0]["path"] = ".env"
        with self.assertRaisesRegex(ReviewError, "outside"):
            self.candidate()

    def test_tampered_baseline_is_rejected(self):
        proposal = self.candidate()
        proposal["files"][0]["base_sha256"] = "bad"
        with self.assertRaisesRegex(ReviewError, "baseline hash"):
            candidate_snapshot(self.repo, self.cfg, proposal)

    def test_no_terminal_newline_does_not_corrupt_diff_lines(self):
        self.raw["files"][0]["content"] = "return eval('2+2')"
        proposal = self.candidate()
        snapshot = candidate_snapshot(self.repo, self.cfg, proposal)
        self.assertEqual(snapshot["files"][0]["new_lines"]["1"], "return eval('2+2')")

    def test_revision_feedback_requires_exact_candidate(self):
        proposal = self.candidate()
        with self.assertRaisesRegex(ReviewError, "different candidate"):
            generate_candidate(self.repo, self.cfg, "Revise", ["pricing.py"], "WEB-123", proposal,
                               {"head": proposal["base_commit"], "snapshot_id": "wrong"})

    def test_revision_accepts_feedback_bound_to_exact_candidate(self):
        proposal = self.candidate()
        feedback = review_candidate(self.repo, replace(self.cfg, provider="demo"), proposal, "WEB-123")
        fixed = {"summary": "Removed dynamic evaluation", "files": [{"path": "pricing.py", "content": "def calculate_total(price, quantity):\n    return price * quantity\n"}]}
        with patch("review_agent.generation.structured_generate", return_value=(fixed, {"input_tokens": 180, "output_tokens": 40})):
            revised = generate_candidate(self.repo, self.cfg, "Address the review", ["pricing.py"], "WEB-123", proposal, feedback)
        self.assertNotIn("eval(", revised["files"][0]["content"])
        events = Ledger(self.cfg.usage_db).summary("WEB-123")["conversations"][0]["events"]
        self.assertEqual([x["operation"] for x in events], ["generate", "candidate_review", "revise"])

    def test_generation_output_validation_failure_is_accounted(self):
        self.raw["files"][0]["path"] = "../escape.py"
        with self.assertRaises(ReviewError):
            self.candidate()
        event = Ledger(self.cfg.usage_db).summary("WEB-123")["conversations"][0]["events"][0]
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["usage"]["output_tokens"], 50)


if __name__ == "__main__":
    unittest.main()
