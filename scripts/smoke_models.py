"""Smoke-test list_models through an isolated MCP server.

    uv run python scripts/smoke_models.py --cwd /absolute/project

Uses a temporary Bridge home under ignored lab/, preserving worker login homes.
ACP may leave empty discovery sessions in the worker's own history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent_bridge.paths import bridge_home


def payload(result: Any) -> dict:
    structured = result.structured_content
    if isinstance(structured, dict):
        return structured
    for content in result.content:
        if getattr(content, "type", None) == "text":
            return json.loads(content.text)
    raise RuntimeError("MCP result contained no JSON payload")


async def run(cwd: Path, agents: list[str], config_home: Path, output: Path | None) -> int:
    lab = Path(__file__).resolve().parents[1] / "lab"
    lab.mkdir(exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix="model-discovery-", dir=lab) as scratch:
        overlay = config_home / "agents.toml"
        if overlay.is_file():
            shutil.copyfile(overlay, Path(scratch) / "agents.toml")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from agent_bridge.server import mcp; mcp.run(transport='stdio')"],
            cwd=str(cwd),
            env={**os.environ, "AGENT_BRIDGE_HOME": scratch, "AGENT_BRIDGE_PARENT_CONTEXT": ""},
        )
        async with stdio_client(params) as streams, ClientSession(*streams, read_timeout_seconds=120) as session:
            await session.initialize()
            listing = await session.list_tools()
            assert "list_models" in {tool.name for tool in listing.tools}
            for agent in agents:
                args = {"agent": agent, "cwd": str(cwd)}
                discovered = payload(await session.call_tool("list_models", args))
                if discovered.get("ok"):
                    cached = payload(await session.call_tool("list_models", args))
                    assert cached.get("cached") is True
                    assert cached["models"] == discovered["models"]
                results[agent] = discovered
                print(json.dumps({"agent": agent, "ok": discovered.get("ok"),
                                  "models": len(discovered.get("models", [])),
                                  "error": discovered.get("error")}, ensure_ascii=False), flush=True)
            sessions = payload(await session.call_tool("list_sessions", {}))
            assert sessions["sessions"] == [], "Discovery registered a Bridge work session"
    if output:
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(result.get("ok") for result in results.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--agents", nargs="+", default=["kimi", "dsh", "claude", "antigravity", "codex"])
    parser.add_argument("--config-home", type=Path, default=bridge_home())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.cwd.resolve(), args.agents, args.config_home, args.output)))
