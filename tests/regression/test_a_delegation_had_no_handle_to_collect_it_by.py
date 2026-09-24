"""A delegation could only be answered by the call that started it (PLAN M20.1).

The client will not issue the next write-capable call until the current one returns, so a
fan-out of six `delegate` calls started 120s apart. A call that can return a handle lets the
next one start, and needs a second call to collect the answer by. This slice adds the handle
and `collect`; every call still answers inline, so nothing about timing changes yet.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from test_server import DoubleCache, cfg, chat_reply, entry, payload, registry
from wire_double import as_stream

from claude_delegate_local import server
from claude_delegate_local.handles import Handles, UnknownHandle


def _mcp(tmp_path):
    config = cfg(slots_dir=str(tmp_path), admission_idle_hold=0)
    return server.build(config, registry(entry()),
                        DoubleCache(config, lambda _r: as_stream(chat_reply(content="done"))))


def test_a_writing_call_names_its_handle_and_collect_returns_the_same_answer(tmp_path) -> None:
    mcp = _mcp(tmp_path)

    async def go():
        async with Client(mcp) as client:
            first = payload(await client.call_tool("delegate", {"task": "t", "effort": "inherit"}))
            again = payload(await client.call_tool(
                "collect", {"handle": first["handle"], "wait_seconds": 30}))
            return first, again

    first, again = asyncio.run(go())
    assert first["handle"].startswith("d-")
    assert again["status"] == "done"
    assert again["answer"] == "done"


def test_an_unknown_handle_is_refused_with_where_to_look(tmp_path) -> None:
    mcp = _mcp(tmp_path)

    async def go():
        async with Client(mcp) as client:
            await client.call_tool("collect", {"handle": "d-000000000000"})

    with pytest.raises(ToolError, match=r"unknown_handle.*transcript"):
        asyncio.run(go())


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_running_delegation_is_reported_and_never_cancelled_by_waiting() -> None:
    async def go():
        handles = Handles(60.0)
        release = asyncio.Event()

        async def work():
            await release.wait()
            return {"answer": "late"}

        handle = handles.start(work(), tool="delegate")
        running = await handles.collect(handle, wait_seconds=0.01)
        release.set()
        done = await handles.collect(handle, wait_seconds=1.0)
        return running, done

    running, done = asyncio.run(go())
    assert running["status"] == "running"
    assert done == {"answer": "late", "handle": done["handle"], "status": "done"}


def test_a_finished_result_is_forgotten_after_the_ttl_and_a_running_one_never() -> None:
    async def go():
        clock = FakeClock()
        handles = Handles(60.0, clock=clock)
        hold = asyncio.Event()

        async def quick():
            return {"answer": "x"}

        async def slow():
            await hold.wait()
            return {"answer": "y"}

        done_h = handles.start(quick(), tool="delegate")
        slow_h = handles.start(slow(), tool="delegate")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        clock.now = 61.0
        with pytest.raises(UnknownHandle):
            await handles.collect(done_h, wait_seconds=0)
        still = await handles.collect(slow_h, wait_seconds=0)
        hold.set()
        return still

    assert asyncio.run(go())["status"] == "running"
