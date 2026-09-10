# Spec-driven implementation tracker

> This document records the v0.1 baseline. For current v0.2 capabilities, accounting, integrations, generation, and limitations, see [EXTENSIONS.md](EXTENSIONS.md). The current suite contains 72 tests and four MCP tools.

## Phase 1 — delivered

- [x] Define scope semantics and first-version acceptance criteria.
- [x] Implement bounded Git snapshots with added/deleted line maps.
- [x] Add file exclusions, best-effort secret redaction, and provider endpoint policy.
- [x] Disable Git external diff, textconv, fsmonitor, hooks, and configured clean/process filters.
- [x] Implement Ollama, OpenAI, Anthropic, and compatible API adapters.
- [x] Validate output structure, changed-line locations, evidence, and snapshot freshness.
- [x] Expose CLI commands, JSON/Markdown output, and explicit exit codes.
- [x] Implement get_review_context, run_review, and validate_review through stdio MCP.
- [x] Add pinned-repository local REST API with bearer authentication.
- [x] Add synthetic demo creation and preserve existing files on repeated output attempts.
- [x] Generate configuration for Claude Code, Cursor, Claude Desktop, and Codex.
- [x] Pass 40 automated tests and official Python MCP SDK interoperability checks.
- [x] Document Mac setup, provider configuration, scope, limitations, and manual acceptance tests.

## Phase 1 — local acceptance on Yuvaraj's Mac

- [ ] Run the demo and full test suite after extraction.
- [ ] Connect one local editor and confirm all three tools appear.
- [ ] Complete an editor-model review and validation of the demo snapshot.
- [ ] Complete actual Ollama inference against the demo.
- [ ] Review one small real Python/FastAPI change and one TypeScript/React change.
- [ ] Record useful findings, false positives, missed known defects, latency, and usage.

## Phase 2 — useful team reviews

- [ ] Create a small labeled evaluation set from synthetic and authorized historical changes.
- [ ] Add bounded read-only retrieval of definitions and relevant tests at the same snapshot.
- [ ] Add read-only Bitbucket PR metadata/diff ingestion with pinned commit identity.
- [ ] Generate draft PR comments for human review, including stable deduplication keys.
- [ ] Add automatic posting only after repository permissions and intended behavior are explicit.
- [ ] Compare local and hosted providers on useful-finding rate, recall on known issues, and latency.

## Phase 3 — shared engineering service

- [ ] Introduce FastAPI jobs, a queue, organization/repository authorization, and durable storage.
- [ ] Add webhook signature validation, idempotent event handling, retries, and rate limits.
- [ ] Add Jira/Confluence requirement context with resource-level authorization.
- [ ] Add org policies, model routing, stronger DLP, audit logs, and token/cost budgets.
- [ ] Build authenticated remote MCP if browser-hosted clients need live access.

Acceptance of Phase 1 depends on useful local reviews, not only passing infrastructure tests.
