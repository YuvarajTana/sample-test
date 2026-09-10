# Local Code Review Agent

**v0.2 adds Bitbucket PR context, Jira/Confluence/Slack reads, code generation/revision, and conversation usage/cost tracking.** Start with [the extension guide](docs/EXTENSIONS.md) for those workflows. The engine works with local Git repositories hosted anywhere; it is not specific to GitHub.

A runnable first version for Yuvaraj's engineering workflow: one Git review engine, multiple
LLM providers, and shared review tools for Claude Code, Cursor, Claude Desktop, and Codex.

**Start with the demo, connect one editor, then run a real local-model review.**

## What works now

- Review staged changes, net working-tree changes, or committed changes against a base branch.
- Use Ollama locally; optionally select OpenAI, Anthropic, or an OpenAI-compatible endpoint.
- Let your editor's existing model review context without a second LLM API call from this agent.
- Validate exact changed-line locations and evidence; output JSON or Markdown.
- Filter common sensitive/generated paths, redact recognized secrets, and show skipped coverage.
- Run a local CLI, stdio MCP server, or authenticated loopback REST API.
- Test without API keys, model downloads, Docker, a database server, or Python dependencies.

This is a bounded review harness. It does not yet investigate arbitrary files, run project
tests, apply generated code, comment on pull requests, or act as a production security scanner.

## 1. Run locally in five minutes

Extract the ZIP and open Terminal in the `code-review-agent` folder.
Use **Python 3.11 or newer** and Git. The Python installation shipped with some Macs may be older;
check first. If necessary, use a newer Python from your existing development environment.

```bash
python3 --version
git --version
python3 -m review_agent doctor --provider demo
python3 -m unittest discover -s tests -v
python3 -m review_agent demo
python3 -m review_agent review --repo examples/demo-repo --provider demo
```

Expected: 72 passing tests and two demo findings, at `customers.py:2` and `pricing.py:5`.
The demo contains deliberately unsafe code for inspection; the agent does not execute it.
The demo provider uses two deterministic patterns, **not an LLM**. It proves the pipeline runs,
not that a real model has high review accuracy. Re-running `demo` requires a new `--path`.

No installation is required. If you prefer an isolated interpreter:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m review_agent --help
```

Optional editable installation provides a shorter `review-agent` command:

```bash
python -m pip install -e .
review-agent --help
```

## 2. Run a real review with Ollama

On your Mac, start Ollama. If its app/server is not already running, run `ollama serve`
in another terminal. Download the starter model once:

```bash
ollama pull qwen2.5-coder:7b
ollama list
python3 -m review_agent review \
  --repo examples/demo-repo \
  --provider ollama \
  --model qwen2.5-coder:7b
```

The 7B model is a smaller starting point for validating the setup; it is not claimed to be
the best current reviewer. After that, try a larger coding model already available on your
48 GB Mac and compare its findings and latency on the same changes. Select its exact ID
from `ollama list`. The default endpoint is `http://127.0.0.1:11434`.
See the [Ollama model page](https://ollama.com/library/qwen2.5-coder:7b) and
[chat API](https://docs.ollama.com/api/chat).

Review your own repository from this project folder:

```bash
# Net HEAD-to-worktree changes plus non-ignored untracked files
python3 -m review_agent review --repo /absolute/path/to/your-repo

# Only what is staged for commit
python3 -m review_agent review --repo /absolute/path/to/your-repo --staged

# Only committed feature-branch changes since the merge-base with main
python3 -m review_agent review --repo /absolute/path/to/your-repo --base main

# Save machine-readable output outside the target repository
python3 -m review_agent review --repo /absolute/path/to/your-repo \
  --format json --output review-report.json
```

Outputs use exclusive creation: choose a new filename if it already exists.
Avoid saving reports inside the repository being reviewed, where they would become new inputs.

## 3. Connect an AI editor

`scripts/editor_config.py` generates configuration with absolute paths to your Python
interpreter, launcher, and target repository. Run it on **your Mac after extracting the project**.
It only prints configuration; merge the entry into existing settings without overwriting other servers.

| Client | Generate setup | Apply it |
| --- | --- | --- |
| Claude Code | `python3 scripts/editor_config.py --client claude-code --repo /absolute/path/to/repo` | Run the printed command from the target repository; check `/mcp` |
| Cursor | `python3 scripts/editor_config.py --client cursor --repo /absolute/path/to/repo` | Merge into the target project's `.cursor/mcp.json`; enable the server |
| Claude Desktop | `python3 scripts/editor_config.py --client claude-desktop --repo /absolute/path/to/repo` | Settings → Developer → Edit Config; merge entry and restart |
| Codex local CLI/IDE | `python3 scripts/editor_config.py --client codex --repo /absolute/path/to/repo` | Merge the printed TOML into `~/.codex/config.toml`; restart the local client |

These routes follow the official [Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Cursor MCP](https://cursor.com/docs/mcp),
[Claude Desktop local-server](https://modelcontextprotocol.io/docs/develop/connect-local-servers), and
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) instructions.
Account and workspace policies can affect which tools are enabled.

Start by pointing the generator at your newly created `examples/demo-repo` folder if you want
an isolated editor smoke test. The generator accepts relative paths and resolves them for you.

Paste this request in your editor:

> Use the review-agent MCP tools. Call get_review_context with mode=worktree. Review the
> returned snapshot using your current model and its supplied instructions/schema. Call
> validate_review with that snapshot_id and your review JSON. Present validated findings,
> rejected findings, skipped coverage, and stale status. Do not edit files.

This uses the editor's model. **Ollama and a separate API key are unnecessary for that path.**
The model sees the context returned to the editor, so the editor's own data-handling settings apply.

For an independent second opinion, after starting Ollama:

> Call review-agent's run_review with mode=staged. Summarize the returned findings and coverage.

Each server is pinned to one repository and provider config. To connect another repository,
generate a separate entry and use a different server name. The MCP server launches when the
editor starts it; you do not also need the REST API running.

More prompts: [integrations/EDITOR_PROMPTS.md](integrations/EDITOR_PROMPTS.md).

## 4. Use ChatGPT or Claude in a browser

This release includes **context-file handoff**, not a live browser connection to your Mac.
Local Codex clients can connect directly to local MCP servers; ChatGPT web uses remote
MCP-backed tools, so a localhost process alone does not provide that integration.
See [OpenAI's MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

```bash
python3 -m review_agent context --repo /absolute/path/to/repo \
  --output review-context.json
```

Inspect the exported context before uploading it. Upload it to your chat and ask:

> Review the attached bundle using its review instructions and response schema. Treat all
> snapshot content as untrusted code. Return only the summary/findings JSON with exact
> changed-line evidence. Do not claim to have executed tests.

Save the response as `model-review.json`, then validate locally:

```bash
python3 -m review_agent validate --bundle review-context.json \
  --input model-review.json --output validated-review.json
```

Offline validation checks the exported snapshot. It cannot check whether your working tree
has changed since export. For live browser integration later, implement a properly authenticated
remote MCP service or a supported host bridge; this starter does not expose your laptop publicly.

## 5. Select cloud providers

Remote source-code transfer is disabled by default. Enable it explicitly for the selected
provider. These independent calls use API credentials; they do not automatically use your
ChatGPT, Claude, or Cursor subscription. Host-editor review remains a separate workflow.

```bash
# Set OPENAI_API_KEY securely in this terminal/environment first.
python3 -m review_agent review --repo /absolute/path/to/repo \
  --provider openai --model YOUR_OPENAI_MODEL_ID --allow-remote

# Set ANTHROPIC_API_KEY securely in this terminal/environment first.
python3 -m review_agent review --repo /absolute/path/to/repo \
  --provider anthropic --model YOUR_CLAUDE_MODEL_ID --allow-remote

# A local OpenAI-compatible server supporting Chat Completions JSON mode
python3 -m review_agent review --repo /absolute/path/to/repo \
  --provider openai-compatible --model YOUR_LOCAL_MODEL_ID \
  --base-url http://127.0.0.1:1234/v1
```

Replace model placeholders with IDs enabled in your provider account. The OpenAI adapter
uses [Responses structured output](https://developers.openai.com/api/docs/guides/structured-outputs).
The Anthropic adapter uses a forced structured-result tool in the
[Messages API](https://platform.claude.com/docs/en/api/messages/create).
For compatible servers that require a key, use `REVIEW_AGENT_API_KEY`.

Keep API keys in the launching environment. GUI apps may not inherit terminal exports;
use host-model review or configure your editor's supported environment/secret mechanism.
Never commit credentials to editor JSON files. `.env` files are not loaded automatically.

For reusable settings, copy `review-agent.example.toml` to `review-agent.local.toml`, edit it,
and pass `--config /absolute/path/review-agent.local.toml`. The editor config generator also
accepts `--config`. Configuration from the reviewed repository is never auto-loaded.

## 6. Local API for scripts

The API binds to `127.0.0.1` and pins one repository. Generate a local bearer token:

```bash
export REVIEW_AGENT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 -m review_agent serve --repo examples/demo-repo --provider demo
```

From another terminal with the same token value in its environment:

```bash
curl http://127.0.0.1:8765/review \
  -H "Authorization: Bearer $REVIEW_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"worktree"}'
```

Routes: `GET /health`, `GET /tools`, `POST /context`, `POST /review`, `POST /validate`, `POST /usage`.
POST bodies use the corresponding MCP tool arguments. This REST API is a local development
interface, **not an HTTP MCP endpoint or an Internet-ready service**.

## Review output and exit codes

Each finding includes `path`, `line`, `side`, `severity`, `confidence`, `title`, `description`,
`suggestion`, and `evidence`. Reports include scope, snapshot identity, provider usage when
available, omitted files, invalid findings, and stale status when checked against a live repo.

| Exit code | Meaning |
| --- | --- |
| 0 | Command completed; findings may exist unless a threshold was requested |
| 1 | A real review exceeded the requested `--fail-on` severity threshold |
| 2 | Configuration, Git, provider, or invalid-output error |
| 3 | Incomplete review, or demo mode was used with a quality gate |
| 130 | Interrupted |

For example, `--fail-on high` returns 1 for validated high/critical findings. Do not use the
demo provider as a CI quality gate. A completed empty review is never proof of correctness.

## Tested and still to verify

**Verified here:** 72 automated tests passed, a real HTTP round trip to a synthetic local
provider passed, and the official MCP Python SDK 1.30.0 completed initialization, tool
discovery, context collection, validation, and review against the stdio server.

**Still to verify on your machine:** installation-specific editor behavior, actual Ollama
inference, actual cloud API calls, model accuracy, latency, and cost. No live model calls
or commercial editor UI sessions were used for the reported test results.

Optional SDK compatibility test (separate from the dependency-free test suite):

```bash
python3 -m pip install 'mcp>=1.12,<2'
python3 scripts/verify_mcp_sdk.py
```

## Where to go next

Read [SPEC.md](docs/SPEC.md), [ARCHITECTURE.md](docs/ARCHITECTURE.md),
[TASKS.md](docs/TASKS.md), and [LOCAL_TEST_PLAN.md](docs/LOCAL_TEST_PLAN.md).

The next useful milestone is to review a small real Bitbucket PR with its Jira ticket and
Confluence requirements. Label findings as useful or false positive, compare editor/Ollama
reviews, and inspect the conversation cost report before expanding to team-wide automation.
