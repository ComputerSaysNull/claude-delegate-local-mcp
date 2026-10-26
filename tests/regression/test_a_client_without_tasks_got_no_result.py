"""A client without the tasks extension got only a handle, never the result (ADR-0103).

A write-capable delegation answered at once with a handle and the run carried on, so a
client that could not run tasks had to make a second `collect` call to hear the answer.
On the task protocol the finished result is the task's result; on the legacy protocol the
same finished result is delivered inline, so a caller that cannot run tasks still gets the
answer without a `collect`.
"""

import asyncio
import json
from pathlib import Path

from fastmcp import Client

from claude_delegate_local import server
from test_server import DoubleCache, cfg, chat_handler, entry, payload, registry


def _mcp(config, handler):
    return server.build(config, registry(entry()), DoubleCache(config, handler))


def _events(directory: Path) -> list[dict]:
    streams = list(directory.glob("*.jsonl"))
    assert len(streams) == 1, f"expected one stream, found {[p.name for p in streams]}"
    return [json.loads(line) for line in streams[0].read_text(encoding="utf-8").splitlines()]


def test_a_legacy_client_gets_the_finished_result_inline():
    config = cfg()
    mcp = _mcp(config, chat_handler(content="the answer"))

    async def go():
        async with Client(mcp, mode="legacy") as client:
            return payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))

    result = asyncio.run(go())
    assert result.get("status") != "running"
    assert result["answer"] == "the answer"


def test_the_inline_runs_transcript_says_it_did_not_run_as_an_mcp_task(tmp_path):
    """A legacy client's run was delivered inline, so it did not run as an MCP task."""
    config = cfg(transcript_dir=str(tmp_path))
    mcp = _mcp(config, chat_handler(content="the answer"))

    async def go():
        async with Client(mcp, mode="legacy") as client:
            return payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))

    asyncio.run(go())
    events = _events(tmp_path)
    assert events[0]["t"] == "start", [e["t"] for e in events]
    assert events[0]["mcp_task"] is False
