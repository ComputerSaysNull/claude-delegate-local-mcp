"""`list_delegations` reports the runs this process still holds, and only open questions.

A caller that lost a run -- its context compacted, its handle gone with it -- needs to find
the run parked on a question before it can answer it or stop it. `Handles.listing` is the
accessor the tool serves; the last test drives it over a real session, because the point of
the tool is a run somebody else's context can no longer name.
"""

import asyncio

from fastmcp import Client

from claude_delegate_local.handles import Handles, Pause
from test_ask_caller_server import _mcp, scripted
from test_server import cfg, chat_reply, payload, tool_call_reply


def _row(listed: list[dict], handle: str) -> dict:
    [row] = [entry for entry in listed if entry["handle"] == handle]
    return row


def test_a_running_run_is_listed_with_its_task_ids_and_no_questions():
    async def go():
        handles = Handles()
        release = asyncio.Event()

        async def work():
            await release.wait()
            return {"answer": "done"}

        handle = handles.start(work(), tool="delegate", task_id="t-1")
        listed = handles.listing()
        release.set()
        await handles.collect(handle, 5)
        return handle, listed

    handle, listed = asyncio.run(go())
    assert [row["handle"] for row in listed] == [handle]
    row = listed[0]
    assert row["tool"] == "delegate"
    assert row["task_ids"] == ["t-1"]
    assert row["status"] == "running"
    assert row["questions"] == [], "a running run has no open question"
    assert row["age_seconds"] >= 0


def test_a_run_parked_on_a_question_is_listed_with_the_questions_it_waits_on():
    async def go():
        handles = Handles()
        pause = Pause()

        async def work():
            return {"reply": await pause.ask(["which reading?", "or the other?"])}

        handle = handles.start(work(), tool="delegate_readonly", pause=pause)
        while not pause.waiting():
            await asyncio.sleep(0.01)
        listed = handles.listing()
        handles.answer(handle, "the first")
        await handles.collect(handle, 5)
        return handle, listed

    handle, listed = asyncio.run(go())
    row = _row(listed, handle)
    assert row["status"] == "question"
    assert row["questions"] == ["which reading?", "or the other?"], row
    assert row["tool"] == "delegate_readonly"


def test_an_answered_run_is_listed_without_the_question_it_already_answered():
    """The negative control for the one above: `Pause.questions` is kept after an answer,
    so a listing that read it unconditionally would report a question nobody can answer."""
    async def go():
        handles = Handles()
        pause = Pause()
        release = asyncio.Event()

        async def work():
            reply = await pause.ask(["which reading?"])
            await release.wait()
            return {"reply": reply}

        handle = handles.start(work(), tool="delegate", pause=pause)
        while not pause.waiting():
            await asyncio.sleep(0.01)
        before = handles.listing()
        handles.answer(handle, "the first")
        after = handles.listing()
        release.set()
        await handles.collect(handle, 5)
        return handle, before, after

    handle, before, after = asyncio.run(go())
    assert _row(before, handle)["questions"] == ["which reading?"]
    row = _row(after, handle)
    assert row["questions"] == [], "an answered question is not an open one"
    assert row["status"] == "running"


def test_a_run_that_ended_is_not_listed():
    async def go():
        handles = Handles()

        async def work():
            return {}

        collected = handles.start(work(), tool="delegate")
        await handles.collect(collected, 5)
        uncollected = handles.start(work(), tool="delegate")
        await handles.task(uncollected)
        return handles.listing()

    assert asyncio.run(go()) == []


def test_the_age_is_measured_from_the_moment_the_run_started():
    async def go():
        handles = Handles(clock=lambda: 1000.0)
        release = asyncio.Event()

        async def work():
            await release.wait()
            return {}

        handle = handles.start(work(), tool="delegate")
        listed = handles.listing(now=1030.0)
        release.set()
        await handles.collect(handle, 5)
        return handle, listed

    handle, listed = asyncio.run(go())
    assert _row(listed, handle)["age_seconds"] == 30.0


def test_the_tool_finds_a_run_parked_on_a_question_over_a_real_session():
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="done"),
    )
    mcp = _mcp(cfg(), handler)

    async def go():
        async with Client(mcp) as client:
            asked = payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))
            assert asked["status"] == "question", asked
            listed = payload(await client.call_tool("list_delegations", {}))
            payload(await client.call_tool(
                "answer", {"handle": asked["handle"], "text": "foo.py"}))
            after = payload(await client.call_tool("list_delegations", {}))
            return asked, listed, after

    asked, listed, after = asyncio.run(go())
    row = _row(listed["delegations"], asked["handle"])
    assert listed["count"] == 1, listed
    assert row["tool"] == "delegate"
    assert row["questions"] == ["Which file?"], row
    assert row["task_ids"], "the call ran as a task, so its id must be listed"
    assert after["count"] == 0, after
