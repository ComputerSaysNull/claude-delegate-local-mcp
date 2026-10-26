"""A client without the tasks extension got only a handle, never the result (ADR-0103).

A write-capable delegation answered at once with a handle and the run carried on, so a
client that could not run tasks had to make a second `collect` call to hear the answer.
On the task protocol the finished result is the task's result; on the legacy protocol the
same finished result is delivered inline, so a caller that cannot run tasks still gets the
answer without a `collect`.
"""

import asyncio

from fastmcp import Client

from claude_delegate_local import server
from test_server import DoubleCache, cfg, chat_handler, entry, payload, registry


def _mcp(config, handler):
    return server.build(config, registry(entry()), DoubleCache(config, handler))


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
