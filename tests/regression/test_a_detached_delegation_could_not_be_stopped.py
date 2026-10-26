"""Stopping a delegation no longer needs the call that started it (ADR-0103).

A run parked on a question has no task for the client to cancel, so `cancel_delegation`
stops it. The `Handles`-level tests below pin the internal collector a stopped run reports
as cancelled, and that abandoning it leaves the run going.
"""

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from test_server import (
    DoubleCache, cfg, entry, payload, registry, serves_metrics, tool_call_reply,
)
from wire_double import as_stream

from claude_delegate_local import server
from claude_delegate_local.handles import Handles


def test_cancelling_a_handle_stops_its_run_and_collect_says_so() -> None:
    async def go():
        handles = Handles(60.0)
        stopped = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stopped.set()
                raise
            return {"answer": "never"}

        handle = handles.start(work(), tool="delegate")
        await asyncio.sleep(0)
        assert handles.cancel(handle) is True
        await asyncio.wait_for(stopped.wait(), timeout=2.0)
        return await handles.collect(handle, wait_seconds=1.0), handles.cancel(handle)

    collected, again = asyncio.run(go())
    assert collected["status"] == "cancelled"
    assert again is False, "a run already stopped reports that nothing more was stopped"


def test_abandoning_a_collect_leaves_the_run_going() -> None:
    async def go():
        handles = Handles(60.0)
        release = asyncio.Event()

        async def work():
            await release.wait()
            return {"answer": "kept"}

        handle = handles.start(work(), tool="delegate")
        waiter = asyncio.ensure_future(handles.collect(handle, wait_seconds=30))
        await asyncio.sleep(0.01)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        release.set()
        return await handles.collect(handle, wait_seconds=1.0)

    assert asyncio.run(go())["answer"] == "kept"


def _mcp(tmp_path):
    config = cfg(slots_dir=str(tmp_path), admission_idle_hold=0)
    handler = serves_metrics(
        lambda _r: as_stream(tool_call_reply("ask_caller", {"questions": ["Which file?"]})))
    return server.build(config, registry(entry()), DoubleCache(config, handler))


def test_the_tool_refuses_an_unknown_handle_and_stops_a_parked_run(tmp_path) -> None:
    """`cancel_delegation` refuses an unknown handle, and a run parked on a question is stopped."""
    mcp = _mcp(tmp_path)

    async def go():
        async with Client(mcp) as client:
            asked = payload(await client.call_tool(
                "delegate", {"task": "t", "effort": "inherit"}))
            assert asked["status"] == "question", asked
            handle = asked["handle"]
            stopped = payload(await client.call_tool(
                "cancel_delegation", {"handle": handle}))
            with pytest.raises(ToolError, match=r"unknown_handle"):
                await client.call_tool("cancel_delegation", {"handle": "d-000000000000"})
            return stopped

    stopped = asyncio.run(go())
    assert stopped["cancelled"] is True and stopped["stopped"] is True
