# Local Code Review Agent — Phase 1 specification

> This document records the v0.1 baseline. For current v0.2 capabilities, accounting, integrations, generation, and limitations, see [EXTENSIONS.md](EXTENSIONS.md). The current suite contains 72 tests and four MCP tools.

Version 0.1.0 · 9 September 2026

## Problem

Developers switch among Claude Code, Cursor, Codex, and chat products. Review expectations,
context selection, output quality, and privacy behavior vary. Yuvaraj needs a shared review
pipeline that works on a Mac before extending it to Bitbucket and team engineering workflows.

## First outcome

From any local Git repository with an initial commit, produce an inspectable review of a
specified change. Findings include path, changed line, old/new side, severity, confidence,
evidence, impact, and suggested fix. Report omitted coverage and unsuccessful validation.

## Two execution modes

1. **Editor model:** MCP collects context. The host editor reviews it with its current model,
   then calls validation. This requires no separate provider credential in this project.
2. **Independent model:** the CLI, MCP tool, or local API calls a configured provider.
   Ollama is the local default. OpenAI, Anthropic, and compatible endpoints are opt-in choices.

The server does not take over an editor, intercept every completion, or reuse a subscription
as an API key. It supplies tools the editor can invoke. No automatic edits or commits.

## Functional requirements

| ID | Requirement | Implementation |
| --- | --- | --- |
| FR-01 | Review net HEAD-to-worktree changes and non-ignored untracked files | `git_context.collect` |
| FR-02 | Review the staged snapshot without leaking unstaged edits | `--staged` |
| FR-03 | Review committed branch changes from merge-base to HEAD | `--base main` |
| FR-04 | Bound file count, file/patch bytes, and serialized code context | Config and coverage report |
| FR-05 | Exclude sensitive/generated paths, binary files, symlinks, submodules | Filtering before provider call |
| FR-06 | Redact recognized secrets while preserving patch line numbering | `privacy.redact` |
| FR-07 | Keep finding locations and evidence grounded in one snapshot | `schema.validate_review` |
| FR-08 | Flag changes that move during review | Snapshot comparison |
| FR-09 | Output machine-readable JSON and human-readable Markdown | CLI |
| FR-10 | Integrate local editors through stdio MCP | Three review tools |
| FR-11 | Expose a loopback REST interface for scripts | Authenticated development API |
| FR-12 | Test without model downloads or API charges | Deterministic demo and standard-library tests |

## Scope semantics

- Worktree mode means net differences from HEAD, combining staged and unstaged edits. Changes
  that cancel each other do not appear. Untracked non-ignored files are included.
- Staged mode uses the index. It excludes untracked and unstaged content.
- Base mode uses `merge-base(base, HEAD)` through HEAD. Commit your feature work first.
- Renames are represented as delete/add, deliberately favoring simple, exact old/new anchors.
- Deleted code can be reported on the old side. New-side findings must anchor to added lines.
- Eight surrounding lines per hunk provide limited context. There is no full-file dependency graph.
- Repositories with merge conflicts or no initial commit return an error.

## Trust and data boundaries

Source is untrusted input to the LLM. Repository text cannot select providers, override the
system review prompt, change endpoints, grant filesystem access, or launch project commands.
MCP and API processes pin a repository at startup; tool arguments cannot select another root.

Best-effort redaction is not a DLP or fintech compliance system. Editor-model context is sent
to that editor and whatever model it uses. Choose local Ollama through the CLI if you need
the review itself to stay on the machine. User-controlled Git configuration is still assumed
to belong to a trusted local development environment, not an adversarial hosted execution box.

## Acceptance criteria

- Seeded demo reports both intended pattern findings without external calls.
- Staged mode reviews staged content even when the working file differs.
- Branch mode excludes unstaged edits and uses the merge-base.
- Invented paths, lines, evidence, malformed findings, and truncated responses cannot become a
  successful empty review. Invalid individual findings produce `incomplete` coverage status.
- Excluded files are listed; context is never silently truncated into a claimed complete review.
- Configured Git clean/process filters, textconv, external diff, hooks, and fsmonitor cannot
  execute through the review diff path.
- An MCP client can initialize, discover tools, collect context, validate, and run a demo review.
- The local API requires a bearer token and rejects browser Origin and unexpected Host headers.

## Status meanings

| Status | Meaning |
| --- | --- |
| `completed` | Provider or editor result passed structure/location checks for included context |
| `demo` | Deterministic fixture rules ran; no LLM inference |
| `no_changes` | No changed files in the selected scope |
| `incomplete` | Files skipped, findings rejected, or snapshot became stale |

No status asserts that code is secure or ready to merge. Confidence values come from the model
and are not calibrated probabilities.

## Deferred

Automatic remediation, whole-repository scanning, dependency resolution, test execution,
remote web MCP, OAuth/SSO, Bitbucket comments, Jira/Confluence retrieval, multi-tenant storage,
organizational RBAC, dashboards, model routing, caching, production rate limits, and metrics.

The first expansion should be a small labeled review benchmark and read-only Bitbucket PR
ingestion. Measure usefulness before adding autonomous actions or more tools.
