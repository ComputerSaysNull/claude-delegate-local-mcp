"""A delegation's handle is dropped once its run is over, however the run ended.

Three faults of one shape. A run that raised kept its entry -- the result, the exception and
its traceback -- in `Handles` until the process exited, because `collect` re-raised before it
forgot the run. A run stopped through `cancel_delegation` was never forgotten at all, and the
handle then answered with `unknown_handle` although the result schema promises `cancelled:
false` for a run that had already stopped. And `answer` took `text=""` where
`message_delegation` refuses whitespace.

`Handles` has no public view of its entries, so the counts here read `_entries` and
`_by_task` directly. The server's own `Handles` is a closure variable that no test can
reach, so the tool-level tests observe the forgetting through what the tools say about the
handle afterwards -- an `answer` to a forgotten run is an unknown handle, not a run that is
merely not waiting.
"""

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from test_ask_caller_server import scripted
from test_server import (
    DoubleCache, cfg, chat_reply, entry, payload, registry, serves_metrics, tool_call_reply,
)
from wire_double import as_stream

from claude_delegate_local import server
from claude_delegate_local.handles import Handles


def test_a_failed_run_is_forgotten_once_collect_handed_its_exception_over() -> None:
    async def go():
        handles = Handles()

        async def work():
            raise RuntimeError("boom")

        handle = handles.start(work(), tool="delegate", task_id="t-1")
        with pytest.raises(RuntimeError, match="boom"):
            await handles.collect(handle, wait_seconds=1.0)
        return handle, handles

    handle, handles = asyncio.run(go())
    assert len(handles._entries) == 0, (
        f"a failed run stayed in _entries: {list(handles._entries)}")
    assert len(handles._by_task) == 0, (
        f"a failed run stayed reachable by task id: {handles._by_task}")
    assert handles.forgotten(handle) is True


def test_a_run_stopped_through_the_cancel_path_is_forgotten_once_it_stops() -> None:
    async def go():
        handles = Handles()
        stopped = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stopped.set()
                raise
            return {"answer": "never"}

        handle = handles.start(work(), tool="delegate", task_id="t-1")
        await asyncio.sleep(0)
        cancelled, ended = await handles.stop(handle, settle_seconds=5.0)
        assert cancelled is True
        assert ended is True
        assert stopped.is_set()
        return handle, handles

    handle, handles = asyncio.run(go())
    assert len(handles._entries) == 0, (
        f"a stopped run stayed in _entries: {list(handles._entries)}")
    assert len(handles._by_task) == 0, (
        f"a stopped run stayed reachable by task id: {handles._by_task}")
    assert handles.forgotten(handle) is True


def _cfg(tmp_path):
    """These tests are about the handle bookkeeping, not the cover-up: the delegation reads
    nothing, so Layer 3's list is an empty file written here rather than the repository's
    own, which a sandbox copy of the workdir can leave unreadable."""
    denylist = tmp_path / "secret_globs.txt"
    denylist.write_text("*.nothing-to-cover\n")
    return cfg(slots_dir=str(tmp_path), admission_idle_hold=0,
               secret_globs_file=str(denylist))


def _mcp(tmp_path):
    config = _cfg(tmp_path)
    handler = serves_metrics(
        lambda _r: as_stream(tool_call_reply("ask_caller", {"questions": ["Which file?"]})))
    return server.build(config, registry(entry()), DoubleCache(config, handler))


def test_cancelling_a_parked_run_forgets_its_handle(tmp_path) -> None:
    """The stopped run is gone: a second cancel reports `cancelled: false`, no error, and
    the handle is unknown rather than a run that is not waiting. A handle this server never
    issued is still refused."""
    mcp = _mcp(tmp_path)

    async def go():
        async with Client(mcp) as client:
            asked = payload(await client.call_tool(
                "delegate", {"task": "t", "effort": "inherit"}))
            assert asked["status"] == "question", asked
            handle = asked["handle"]
            stopped = payload(await client.call_tool(
                "cancel_delegation", {"handle": handle}))
            assert stopped["cancelled"] is True and stopped["stopped"] is True
            again = payload(await client.call_tool(
                "cancel_delegation", {"handle": handle}))
            with pytest.raises(ToolError) as exc:
                await client.call_tool("answer", {"handle": handle, "text": "late"})
            assert "unknown_handle" in str(exc.value), str(exc.value)
            with pytest.raises(ToolError, match=r"unknown_handle"):
                await client.call_tool(
                    "cancel_delegation", {"handle": "d-000000000000"})
            return again

    again = asyncio.run(go())
    assert again["cancelled"] is False and again["stopped"] is True, again


def test_answer_refuses_empty_text(tmp_path) -> None:
    config = _cfg(tmp_path)
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="done"),
    )
    mcp = server.build(config, registry(entry()), DoubleCache(config, handler))

    async def go():
        async with Client(mcp) as client:
            asked = payload(await client.call_tool(
                "delegate", {"task": "t", "effort": "inherit"}))
            handle = asked["handle"]
            for empty in ("", "   "):
                with pytest.raises(ToolError) as exc:
                    await client.call_tool("answer", {"handle": handle, "text": empty})
                assert "empty" in str(exc.value), str(exc.value)
            done = payload(await client.call_tool(
                "answer", {"handle": handle, "text": "foo.py"}))
            assert done["answer"] == "done", done

    asyncio.run(go())


def test_a_run_cancelled_with_its_wait_is_forgotten_once_it_ends() -> None:
    """A client's tasks/cancel stops the run as well as the wait, and nobody waits after it.

    So the run must be dropped when its task ends, or it holds its entry until restart. The
    plain `cancel` keeps the entry, because there a later `collect` reports "cancelled"."""
    async def go():
        handles = Handles()

        async def work():
            await asyncio.sleep(30)
            return {"answer": "never"}

        abandoned = handles.start(work(), tool="delegate", task_id="t-1")
        collected = handles.start(work(), tool="delegate", task_id="t-2")
        await asyncio.sleep(0)
        handles.cancel(abandoned, forget=True)
        handles.cancel(collected)
        for _ in range(5):
            await asyncio.sleep(0)
        return abandoned, collected, handles

    abandoned, collected, handles = asyncio.run(go())
    assert abandoned not in handles._entries, "a run cancelled with its wait was kept"
    assert "t-1" not in handles._by_task
    assert collected in handles._entries, (
        "a plain cancel dropped the entry a later collect needs to report the cancellation")
