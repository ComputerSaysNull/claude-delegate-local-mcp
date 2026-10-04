"""The read-only delegating tools' `may_ask`: offered `ask_caller`, a run that asks
returns early with the question, and a run that never asks keeps today's inline shape.

`tests/test_ask_caller_server.py` pins the write-capable pair, which answer at once with
a `handle` (ADR-0103). The read-only pair answer inline, so a run that asks must not
leave its caller blocked on it: the call returns early with `status: "question"` and a
`handle` that `answer` resumes, while a run that never asks returns its result dict in
the inline shape, with no `status` and no `handle` (ADR-0118).
"""

from __future__ import annotations

import asyncio
import json

from fastmcp import Client
from test_ask_caller_server import _mcp, scripted
from test_server import (
    called,
    cfg,
    chat_handler,
    chat_reply,
    payload,
    recording_handler,
    serves_metrics,
    tool_call_reply,
)


def test_a_read_only_call_returns_early_with_the_question():
    """The read-only tool now pauses instead of running to the end: a run that asks
    answers at once with the question, and `answer` hands the reply back to the paused run."""
    config = cfg()
    handler, seen = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            started = payload(await client.call_tool(
                "delegate_readonly", {"task": "q", "effort": "inherit"}))
            assert started["status"] == "question", started
            assert started["questions"] == ["Which file?"]
            handle = started["handle"]
            assert isinstance(handle, str) and handle
            assert started["tool"] == "delegate_readonly"
            assert isinstance(started["waiting_seconds"], (int, float))

            done = payload(await client.call_tool(
                "answer", {"handle": handle, "text": "foo.py", "wait_seconds": 5}))
            assert done["status"] == "done", done
            assert done["answer"] == "it is foo.py"

    asyncio.run(go())

    # The answer came back as the tool result of the call that asked: turn 2's request
    # carries the caller's reply where the model reads any other tool's output.
    assert len(seen) == 2
    assert "foo.py" in json.dumps(seen[1])


def test_a_read_only_call_that_never_asks_keeps_its_shape():
    """The pause machinery must not change the shape of a run that never pauses: it still
    returns the result dict, with no `status` and no `handle` (unlike a write-capable call,
    which always answers at once with `status: running`)."""
    config = cfg()
    handler, _ = scripted(chat_reply(content="the answer"))
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            result = payload(await client.call_tool(
                "delegate_readonly",
                {"task": "q", "effort": "inherit", "may_ask": True}))
            assert result["answer"] == "the answer"
            assert "status" not in result
            assert "handle" not in result

    asyncio.run(go())


def test_may_ask_false_withholds_ask_caller_from_a_read_only_call():
    """The default is on, exactly as for the write-capable tools: a plain read-only call
    offers `ask_caller`, and `may_ask=False` withholds it."""
    sent = []
    handler = serves_metrics(recording_handler(sent))

    called(handler, "delegate_readonly", task="q")
    declared = {t["function"]["name"] for t in sent[0].get("tools", [])}
    assert "ask_caller" in declared, "may_ask defaults to true, so the tool must be offered"

    sent.clear()
    called(handler, "delegate_readonly", task="q", may_ask=False)
    declared = {t["function"]["name"] for t in sent[0].get("tools", [])}
    assert "ask_caller" not in declared


def test_read_only_tools_still_say_they_cannot_write():
    """A guard: asking writes nothing, so `may_ask` must not cost the read-only pair the
    `readOnlyHint` that is the reason they exist."""
    mcp = _mcp(cfg(), chat_handler())

    async def go():
        async with Client(mcp) as client:
            return {t.name: t for t in await client.list_tools()}

    tools = asyncio.run(go())
    for name in ("delegate_readonly", "delegate_to_agent_readonly"):
        annotations = tools[name].annotations
        assert annotations is not None and annotations.readOnlyHint is True, (
            f"{name} cannot write and must say so"
        )
