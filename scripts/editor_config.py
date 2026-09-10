"""Print configuration only. Never modify existing editor settings."""
import argparse
import json
from pathlib import Path
import shlex
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--client", required=True, choices=["cursor", "claude-code", "claude-desktop", "codex"])
parser.add_argument("--repo", required=True)
parser.add_argument("--config")
args = parser.parse_args()
repo = Path(args.repo).expanduser().resolve()
if not repo.is_dir():
    parser.error("--repo must be an existing local directory")
launcher = Path(__file__).resolve().parents[1] / "run_agent.py"
command = sys.executable
arguments = [str(launcher), "mcp", "--repo", str(repo)]
if args.config:
    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        parser.error("--config must be an existing TOML file")
    arguments.extend(["--config", str(config)])
if args.client == "claude-code":
    print(shlex.join(["claude", "mcp", "add", "--transport", "stdio", "--scope", "local", "review-agent", "--", command, *arguments]))
elif args.client == "codex":
    print("[mcp_servers.review-agent]")
    print("command = " + json.dumps(command))
    print("args = " + json.dumps(arguments))
    print("startup_timeout_sec = 20\ntool_timeout_sec = 600")
else:
    print(json.dumps({"mcpServers": {"review-agent": {"command": command, "args": arguments}}}, indent=2))
