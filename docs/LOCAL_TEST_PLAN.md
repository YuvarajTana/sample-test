# Local acceptance and troubleshooting

> This document records the v0.1 baseline. For current v0.2 capabilities, accounting, integrations, generation, and limitations, see [EXTENSIONS.md](EXTENSIONS.md). The current suite contains 72 tests and four MCP tools.

## Baseline validation

Run from the extracted project root:

```bash
python3 -m unittest discover -s tests -v
python3 -m review_agent demo --path examples/demo-repo
python3 -m review_agent review --repo examples/demo-repo --provider demo
```

Expected: 40 tests pass. Demo reports SQL interpolation at `customers.py:2` and dynamic
evaluation at `pricing.py:5`, with status `demo` and zero provider tokens.

## Verify review scope

In the generated demo repository, stage only one file:

```bash
git -C examples/demo-repo add pricing.py
python3 -m review_agent review --repo examples/demo-repo --staged --provider demo
```

Expected: one included file and one demo finding at `pricing.py:5`. The unstaged SQL change
must not appear. You can edit the working copy of pricing.py again and repeat; a staged review
must continue to show the staged version.

To verify branch review manually, create a branch and commit only synthetic fixture changes,
then use `--base main`. Leave a new working-tree edit uncommitted and confirm it is absent.
The automated suite already checks this behavior with disposable Git repositories.

## Verify actual inference

1. Start Ollama and confirm your selected coding model appears in `ollama list`.
2. Review the same demo with `--provider ollama --model <exact-id>`.
3. Confirm the report identifies the intended defects with valid evidence.
4. Record any missed issue or false positive. The exact wording/count is not deterministic.
5. Use a small real change with a known bug and a fixed version. The model should distinguish
   them. Check both useful-finding rate and missed defects; an empty output is not success by itself.

## Verify an editor integration

1. Generate configuration using `scripts/editor_config.py` on your machine.
2. Add the generated entry to the intended local client and enable it.
3. Confirm get_review_context, run_review, and validate_review are available.
4. Request host-model review using the first prompt in `integrations/EDITOR_PROMPTS.md`.
5. Confirm validation returns matching snapshot identity and `stale=false`.
6. Change a source line before validation and confirm the result becomes `incomplete`/stale.

Using an editor's model sends returned source context to that editor. An Ollama CLI run uses
the loopback provider and does not need an editor connection.

## Troubleshooting

| Symptom | Check / action |
| --- | --- |
| `No module named review_agent` | Run from the project root or use `/absolute/path/code-review-agent/run_agent.py` |
| Python syntax/import error | Use Python 3.11+; check the interpreter path generated into editor config |
| Cannot reach provider | Start Ollama, check `127.0.0.1:11434`, model ID, and timeout |
| HTTP 404 from Ollama | The model may not be downloaded, or the base URL includes an incorrect API suffix |
| HTTP 401/403 from cloud | Check the relevant API credential and account/model permissions |
| Invalid/incomplete model JSON | Reduce the change size, use a stronger compatible model, or increase output budget |
| MCP timeout | Use host-model context/validation, reduce change size, or increase the client tool timeout |
| MCP works in terminal but not GUI | Use the generator's absolute Python/launcher paths; GUI environment differs from shell |
| `No reviewable changed files` | Check `git status`, selected scope, and initial commit; branch review requires committed work |
| `incomplete` | Inspect skipped files, rejected findings, and stale flag; do not treat it as a clean review |
| More code changed than expected | Default scope includes untracked files; save reports outside the reviewed repository |
| Diff differs from regular Git output | Review commands disable custom clean/process filters and rename detection |
| `localhost` endpoint rejected | Use literal `127.0.0.1` for local providers; this keeps the local/remote boundary explicit |
| Corporate proxy is required | This starter intentionally disables ambient proxies; add an explicit approved proxy policy before using it there |

The default per-file byte limit bounds untracked file size and tracked diff size, not the full
size of every tracked file. The total code-context limit counts serialized included file
records, with prompt/schema overhead added separately. Files over budget are listed as skipped.

## Verification recorded during creation

Date: 9 September 2026. Runtime: Python 3.12.14, Git 2.51.1.

- 40 standard-library unit/integration tests: passed.
- Temporary Git repositories: staged, worktree, base, deleted, Unicode, special-name, excluded,
  binary, symlink, redacted, budget-exceeded, and stale-snapshot cases exercised.
- Provider adapters: request/response contract tests with synthetic fixtures passed.
- Real loopback HTTP round trip to a synthetic Ollama-compatible endpoint: passed.
- Official MCP Python SDK 1.30.0: initialization, discovery, context, validation, review passed.
- Live Ollama/cloud inference and interactive commercial editor UI checks: not run here.
