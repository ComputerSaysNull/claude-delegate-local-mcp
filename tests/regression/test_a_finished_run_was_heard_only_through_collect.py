"""A finished delegation was heard only by collecting its handle (ADR-0103).

A write-capable call answered at once with a `handle` and the run carried on, so a caller
had to make a second `collect` call to learn the answer. Once the delegating tools run as
MCP tasks, the finished run's dict *is* the task's result, so a caller that waits on the
task hears the answer with no `collect` -- and the run is what the task is waiting on, so
cancelling the task cancels the run rather than leaving it going.
"""

import asyncio
import time

from fastmcp import Client
from fastmcp_tasks.client import call_tool_task

from claude_delegate_local import server
from test_server import DoubleCache, cfg, chat_handler, entry, payload, registry


def _mcp(config, handler):
    return server.build(config, registry(entry()), DoubleCache(config, handler))


async def _terminal(task, timeout=15.0):
    """Wait until the task is no longer working, then return its status."""
    deadline = time.monotonic() + timeout
    while True:
        state = await task.status()
        if state.status in ("completed", "failed", "cancelled", "input_required"):
            return state
        if time.monotonic() >= deadline:
            raise TimeoutError(f"task still {state.status} after {timeout}s")
        await asyncio.sleep(0.05)


def test_a_finished_run_is_delivered_as_the_task_result():
    config = cfg()
    mcp = _mcp(config, chat_handler(content="the answer"))

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(
                client, "delegate", {"task": "q", "effort": "inherit"})
            await _terminal(task)
            return payload(await task.result())

    result = asyncio.run(go())
    assert result["answer"] == "the answer"
    assert result.get("status") != "running"
