"""Optional interoperability check: requires mcp>=1.12,<2 in your test environment."""
import asyncio
from datetime import timedelta
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_agent.demo import create_demo
from review_agent.providers import demo_review


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import json
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        repo = create_demo(Path(tmp) / "repo")
        params = StdioServerParameters(command=sys.executable, args=[str(root / "run_agent.py"), "mcp", "--repo", str(repo), "--provider", "demo",
                                       "--usage-db", str(Path(tmp) / "usage.db"), "--conversation", "sdk-test"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert names == {"get_review_context", "run_review", "validate_review", "get_cost_summary"}
                context = await session.call_tool("get_review_context", {})
                assert not context.isError
                snapshot = json.loads(context.content[0].text)["snapshot"]
                validated = await session.call_tool("validate_review", {"snapshot_id": snapshot["snapshot_id"], "review": demo_review(snapshot)})
                assert not validated.isError
                report = json.loads(validated.content[0].text)
                assert len(report["findings"]) == 2 and not report["stale"]
                result = await session.call_tool("run_review", {})
                assert not result.isError
                assert len(json.loads(result.content[0].text)["findings"]) == 2
                costs = await session.call_tool("get_cost_summary", {})
                assert not costs.isError
                assert json.loads(costs.content[0].text)["conversations"][0]["unknown_cost_events"] == 1
                print(json.dumps({"sdk_interoperability": "passed", "protocol": init.protocolVersion,
                                  "tools": sorted(names), "validated_findings": 2}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
