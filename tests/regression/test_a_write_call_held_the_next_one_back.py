"""A write-capable call used to hold the client until its run finished, so the next one waited.

The client ran one write-capable call at a time and released the next when the current one
returned or passed 120s, so six `delegate_to_agent` calls in one message started 120s apart,
the last at +688s. The delegating tools are now MCP tasks: a call returns a task at once, the
admission wait happens inside that task, and the next call starts while the first runs
(ADR-0103). A running delegation is stopped by cancelling its task, which
tests/regression/test_a_cancelled_task_left_its_run_going.py covers.
"""

import asyncio
import time
from contextlib import asynccontextmanager

from fastmcp import Client
from fastmcp_tasks.client import call_tool_task

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


def test_two_write_calls_run_at_once(tmp_path) -> None:
    """Both `delegate` calls answer with a task before either run finishes, so the second
    call starts while the first runs instead of being held back for it."""
    mcp = _slow_mcp(tmp_path)

    async def go():
        async with Client(mcp) as client:
            began = time.monotonic()
            first = await call_tool_task(
                client, "delegate", {"task": "t", "effort": "inherit"})
            second = await call_tool_task(
                client, "delegate", {"task": "t", "effort": "inherit"})
            both_returned_in = time.monotonic() - began
            # Both calls are back before either SLOW-second run has finished.
            assert both_returned_in < SLOW, (
                f"the second call was held back for {both_returned_in:.2f}s")
            first_done = payload(await first.result())
            second_done = payload(await second.result())
            return first_done, second_done

    first, second = asyncio.run(go())
    assert first["answer"] == "finally"
    assert second["answer"] == "finally"


def test_the_admission_wait_is_inside_the_task_too(tmp_path, monkeypatch) -> None:
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
            task = await call_tool_task(
                client, "delegate", {"task": "t", "effort": "inherit"})
            returned_in = time.monotonic() - began
            done = payload(await task.result())
            return returned_in, done

    returned_in, done = asyncio.run(go())
    assert returned_in < 1.0, "a queued write call held its caller for the queue"
    assert done["answer"] == "finally"


def test_a_read_only_call_still_answers_inline(tmp_path) -> None:
    """A read-only run that never asks returns its result inline, with no handle."""
    mcp = _slow_mcp(tmp_path, seconds=0.0)

    async def go():
        async with Client(mcp) as client:
            return payload(await client.call_tool(
                "delegate_readonly", {"task": "t", "effort": "inherit"}))

    result = asyncio.run(go())
    assert result["answer"] == "finally" and "handle" not in result
