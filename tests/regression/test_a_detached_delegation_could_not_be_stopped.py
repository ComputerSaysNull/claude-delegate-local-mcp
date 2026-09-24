"""A delegation answered by handle would have had no way to be stopped (ADR-0103).

Stopping a call used to stop its work, because the work ran inside the call. Once a call
returns a handle at once, the call a client would cancel is already over, so the run needs
a stop of its own -- and "stop waiting" has to stay a different thing from "stop the work",
or abandoning a `collect` would kill what it was waiting for.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from test_server import DoubleCache, cfg, chat_reply, entry, payload, registry
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
    return server.build(config, registry(entry()),
                        DoubleCache(config, lambda _r: as_stream(chat_reply(content="done"))))


def test_the_tool_refuses_an_unknown_handle_and_reports_a_finished_one(tmp_path) -> None:
    mcp = _mcp(tmp_path)

    async def go():
        async with Client(mcp) as client:
            done = payload(await client.call_tool("delegate", {"task": "t", "effort": "inherit"}))
            # Finished first, so "already over" is what is being asked about.
            await client.call_tool("collect", {"handle": done["handle"], "wait_seconds": 30})
            late = payload(await client.call_tool("cancel_delegation", {"handle": done["handle"]}))
            with pytest.raises(ToolError, match=r"unknown_handle"):
                await client.call_tool("cancel_delegation", {"handle": "d-000000000000"})
            return late

    late = asyncio.run(go())
    assert late["cancelled"] is False and late["stopped"] is True
