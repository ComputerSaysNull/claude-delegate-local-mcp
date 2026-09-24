"""A write-capable call held the client until its run finished, so the next one waited.

The client runs one write-capable call at a time and releases the next when the current one
returns or passes 120s, so six `delegate_to_agent` calls in one message started 120s apart,
the last at +688s. The call now answers at once with a handle and the run carries on,
admission wait included, so the next call is released at once (ADR-0103).
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from fastmcp import Client

from test_server import DoubleCache, cfg, chat_reply, entry, payload, registry
from wire_double import as_stream

from claude_delegate_local import server

SLOW = 3.0


def _slow_mcp(tmp_path, seconds: float = SLOW):
    async def slow(_request):
        await asyncio.sleep(seconds)
        return as_stream(chat_reply(content="finally"))

    config = cfg(slots_dir=str(tmp_path), admission_idle_hold=0)
    return server.build(config, registry(entry()), DoubleCache(config, slow))


def test_a_write_call_answers_with_a_handle_before_its_run_finishes(tmp_path) -> None:
    mcp = _slow_mcp(tmp_path)

    async def go():
        async with Client(mcp) as client:
            began = time.monotonic()
            first = payload(await client.call_tool("delegate", {"task": "t", "effort": "inherit"}))
            answered_in = time.monotonic() - began
            done = payload(await client.call_tool(
                "collect", {"handle": first["handle"], "wait_seconds": 30}))
            return first, answered_in, done

    first, answered_in, done = asyncio.run(go())
    assert first["status"] == "running"
    assert answered_in < 1.0, f"the call took {answered_in:.2f}s against a {SLOW}s run"
    assert done["status"] == "done" and done["answer"] == "finally"


def test_the_admission_wait_is_behind_the_handle_too(tmp_path, monkeypatch) -> None:
    class HeldGate(server.Admission):
        @asynccontextmanager
        async def admit(self, *args, **kwargs):
            await asyncio.sleep(SLOW)
            async with super().admit(*args, **kwargs) as lease:
                yield lease

    monkeypatch.setattr(server, "Admission", HeldGate)
    mcp = _slow_mcp(tmp_path, seconds=0.0)

    async def go():
        async with Client(mcp) as client:
            began = time.monotonic()
            first = payload(await client.call_tool("delegate", {"task": "t", "effort": "inherit"}))
            answered_in = time.monotonic() - began
            await client.call_tool("collect", {"handle": first["handle"], "wait_seconds": 30})
            return answered_in

    assert asyncio.run(go()) < 1.0, "a queued write call held its caller for the queue"


def test_a_read_only_call_still_answers_inline(tmp_path) -> None:
    mcp = _slow_mcp(tmp_path, seconds=0.0)

    async def go():
        async with Client(mcp) as client:
            return payload(await client.call_tool(
                "delegate_readonly", {"task": "t", "effort": "inherit"}))

    result = asyncio.run(go())
    assert result["answer"] == "finally" and "handle" not in result


def test_cancel_delegation_stops_a_run_that_is_still_going(tmp_path) -> None:
    mcp = _slow_mcp(tmp_path, seconds=30.0)

    async def go():
        async with Client(mcp) as client:
            first = payload(await client.call_tool("delegate", {"task": "t", "effort": "inherit"}))
            await asyncio.sleep(0.2)
            began = time.monotonic()
            stop = payload(await client.call_tool(
                "cancel_delegation", {"handle": first["handle"]}))
            took = time.monotonic() - began
            after = payload(await client.call_tool(
                "collect", {"handle": first["handle"], "wait_seconds": 0}))
            return stop, took, after

    stop, took, after = asyncio.run(go())
    assert stop == {"handle": stop["handle"], "cancelled": True, "stopped": True}
    assert took < 2.0, f"stopping a run waiting on the model took {took:.2f}s"
    assert after["status"] == "cancelled"
