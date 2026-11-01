"""A caller can message a running delegation, which reads it at its next turn boundary.

`tests/test_ask_caller.py` pins the loop's `ask` callback and
`tests/test_ask_caller_server.py` pins the `answer` tools. These pin the third surface:
`message_delegation`, the one way to reach a run that is simply running -- the caller's
message goes in at the top of a turn and is in the conversation for the request that
follows, never mid-turn or mid-tool.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp_tasks import call_tool_task

from claude_delegate_local import tools
from claude_delegate_local.backends.base import TextBlock, ToolResultBlock, ToolSpec
from claude_delegate_local.handles import InboxMessage
from claude_delegate_local.loop import MessageRecord
from test_agentic_loop import ScriptedTurns, says, wants
from test_ask_caller_server import _mcp, scripted
from test_server import (
    cfg,
    chat_reply,
    chat_handler,
    payload,
    tool_call_reply,
)
from test_transcript_format import errors
from wire_double import as_stream


def blocking_scripted(gate, ready, seen, *replies):
    """Like `scripted`, but the first chat request blocks on `gate` until the test lets it.

    `ready` is set the moment the first chat request arrives, so the test knows the run has
    started (and, through `Handles.start`, been registered by its task id) before it sends a
    message. `/metrics` is answered without blocking, so the decode-rate scrape (ADR-0055)
    cannot be mistaken for a chat turn.
    """
    remaining = list(replies)

    async def handler(request):
        if request.url.path.endswith("/metrics"):
            return httpx.Response(404)
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            ready.set()
            await gate.wait()
        return as_stream(remaining.pop(0))

    return handler


@pytest.fixture
def registered():
    """One cacheable tool, so a turn can call something and the loop goes around again."""
    def echo(cfg_, args):
        return f"echoed {args.get('what', '')}"

    added = {
        "echo": tools.RegisteredTool(
            spec=ToolSpec(name="echo", description="echo", input_schema={"type": "object"}),
            handler=echo,
            cacheable=True,
        ),
    }
    tools.REGISTRY.update(added)
    try:
        yield
    finally:
        tools.REGISTRY.pop("echo", None)


def run(backend, **over):
    from test_agentic_loop import run as _run
    return _run(backend, **over)


def texts(request) -> str:
    """Every text the request's messages carry, joined, for a substring check."""
    parts = []
    for m in request.messages:
        for b in m.content:
            if isinstance(b, TextBlock):
                parts.append(b.text)
            elif isinstance(b, ToolResultBlock):
                parts.append(b.content)
    return "\n".join(parts)


# --- the loop reads the message before the next request ---------------------------------


def test_a_message_is_read_before_the_runs_next_request(registered):
    backend = ScriptedTurns(
        wants(("echo", {"what": "a"})),
        wants(("echo", {"what": "b"})),
        says("done"),
    )
    queued = [InboxMessage("check the spelling", 0.0)]
    calls = 0

    def inbox():
        nonlocal calls
        calls += 1
        return [] if calls != 2 else queued

    heard: list[MessageRecord] = []
    run(backend, inbox=inbox, on_message=heard.append, max_turns=4)
    first, second, _ = backend.requests
    assert "Message from your caller" not in texts(first)
    assert "Message from your caller" in texts(second)
    assert "check the spelling" in texts(second)
    assert [(r.text, r.turn, r.delivered) for r in heard] == [("check the spelling", 2, True)]


def test_a_message_rides_in_the_user_message_after_the_tool_results(registered):
    """Not a second user message in a row: the text joins the unsent tool-result message."""
    backend = ScriptedTurns(wants(("echo", {"what": "a"})), says("done"))
    calls = 0

    def inbox():
        nonlocal calls
        calls += 1
        return [InboxMessage("steer", 0.0)] if calls == 2 else []

    run(backend, inbox=inbox, max_turns=4)
    last = backend.requests[1].messages[-1]
    assert last.role == "user"
    assert isinstance(last.content[0], ToolResultBlock)
    assert isinstance(last.content[-1], TextBlock)
    assert "steer" in last.content[-1].text


# --- the wire task id reaches the run ---------------------------------------------------


def test_a_message_reaches_a_run_by_its_wire_task_id():
    config = cfg()
    gate, ready, seen = asyncio.Event(), asyncio.Event(), []
    handler = blocking_scripted(
        gate, ready, seen,
        tool_call_reply("read_file", {"path": "/x/a.py"}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(client, "delegate", {"effort": "inherit", "task": "q"})
            await ready.wait()
            result = payload(await client.call_tool(
                "message_delegation", {"task_id": task.task_id, "text": "steer"}))
            assert result["status"] == "queued"
            assert result["pending"] == 1
            gate.set()
            done = payload(await task.result())
            assert done["answer"] == "done"

    asyncio.run(go())
    # The wire id the client was handed is the `ctx.task_id` the run registered under, so
    # the message lands in the turn after the one the run was blocked on.
    assert "Message from your caller" not in json.dumps(seen[0])
    assert "Message from your caller" in json.dumps(seen[1])


def test_a_delivered_message_is_streamed_when_it_is_read(tmp_path):
    """Written at the turn boundary that read it, so a watcher sees it before the run ends."""
    config = cfg(transcript_dir=str(tmp_path))
    gate, ready, seen = asyncio.Event(), asyncio.Event(), []
    handler = blocking_scripted(
        gate, ready, seen,
        tool_call_reply("read_file", {"path": "/x/a.py"}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(client, "delegate", {"effort": "inherit", "task": "q"})
            await ready.wait()
            await client.call_tool(
                "message_delegation", {"task_id": task.task_id, "text": "steer"})
            gate.set()
            done = payload(await task.result())
            assert done["messages"] == {"delivered": 1, "undelivered": []}

    asyncio.run(go())
    stream = next(tmp_path.glob("*.jsonl"))
    kinds = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()]
    message = next(e for e in kinds if e["t"] == "message")
    assert (message["text"], message["turn"], message["delivered"]) == ("steer", 2, True)
    order = [e["t"] for e in kinds]
    assert order.index("message") < order.index("end")
    assert order.index("message") < max(i for i, k in enumerate(order) if k == "turn")


def test_a_message_to_a_one_shot_is_reported_undelivered():
    """A run with no tools has no turn boundary to read it at, and must say so."""
    gate, ready, seen = asyncio.Event(), asyncio.Event(), []
    handler = blocking_scripted(gate, ready, seen, chat_reply(content="done"))
    mcp = _mcp(cfg(), handler)

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(client, "delegate", {
                "effort": "inherit", "task": "q", "allowed_tools": []})
            await ready.wait()
            await client.call_tool(
                "message_delegation", {"task_id": task.task_id, "text": "unread"})
            gate.set()
            done = payload(await task.result())
            assert done["messages"] == {"delivered": 0, "undelivered": ["unread"]}

    asyncio.run(go())


# --- refusals ---------------------------------------------------------------------------


def test_message_refuses_an_unknown_task_id():
    mcp = _mcp(cfg(), chat_handler())

    async def go():
        async with Client(mcp) as client:
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "message_delegation", {"task_id": "does-not-exist", "text": "x"})
            assert "reconnect" in str(exc.value)

    asyncio.run(go())


def test_message_refuses_a_finished_run():
    handler, _ = scripted(chat_reply(content="done"))
    mcp = _mcp(cfg(), handler)

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(client, "delegate", {"effort": "inherit", "task": "q"})
            await task.result()
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "message_delegation", {"task_id": task.task_id, "text": "x"})
            assert "reconnect" in str(exc.value)

    asyncio.run(go())


def test_message_refuses_a_run_waiting_on_a_question():
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="done"),
    )
    mcp = _mcp(cfg(), handler)

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(client, "delegate", {"effort": "inherit", "task": "q"})
            asked = payload(await task.result())
            assert asked["status"] == "question"
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "message_delegation", {"task_id": task.task_id, "text": "x"})
            message = str(exc.value)
            assert "answer" in message
            assert asked["handle"] in message

    asyncio.run(go())


def test_message_refuses_empty_text():
    config = cfg()
    gate, ready, seen = asyncio.Event(), asyncio.Event(), []
    handler = blocking_scripted(gate, ready, seen, chat_reply(content="done"))
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(client, "delegate", {"effort": "inherit", "task": "q"})
            await ready.wait()
            with pytest.raises(ToolError) as exc:
                await client.call_tool(
                    "message_delegation", {"task_id": task.task_id, "text": "   "})
            assert "empty" in str(exc.value)
            gate.set()
            await task.result()

    asyncio.run(go())


# --- reachability -----------------------------------------------------------------------


def test_a_may_ask_false_run_is_reachable():
    config = cfg()
    gate, ready, seen = asyncio.Event(), asyncio.Event(), []
    handler = blocking_scripted(
        gate, ready, seen,
        tool_call_reply("read_file", {"path": "/x/a.py"}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(
                client, "delegate", {"effort": "inherit", "task": "q", "may_ask": False})
            await ready.wait()
            result = payload(await client.call_tool(
                "message_delegation", {"task_id": task.task_id, "text": "steer"}))
            assert result["status"] == "queued"
            gate.set()
            done = payload(await task.result())
            assert done["answer"] == "done"

    asyncio.run(go())
    assert "Message from your caller" in json.dumps(seen[1])


def test_an_answer_tasks_id_reaches_the_continued_run():
    config = cfg()
    gate, ready, seen = asyncio.Event(), asyncio.Event(), []
    remaining = [
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="done"),
    ]

    async def handler(request):
        if request.url.path.endswith("/metrics"):
            return httpx.Response(404)
        seen.append(json.loads(request.content))
        if len(seen) == 2:
            ready.set()
            await gate.wait()
        return as_stream(remaining.pop(0))

    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            delegate_task = await call_tool_task(
                client, "delegate", {"effort": "inherit", "task": "q"})
            asked = payload(await delegate_task.result())
            handle = asked["handle"]
            answer_task = await call_tool_task(
                client, "answer", {"handle": handle, "text": "foo.py"})
            await ready.wait()
            result = payload(await client.call_tool(
                "message_delegation", {"task_id": answer_task.task_id, "text": "steer"}))
            assert result["status"] == "queued"
            gate.set()
            done = payload(await answer_task.result())
            assert done["answer"] == "done"

    asyncio.run(go())


# --- the result and the stream ---------------------------------------------------------


def test_an_undelivered_message_is_reported_in_the_result_and_stream(tmp_path):
    config = cfg(transcript_dir=str(tmp_path))
    gate, ready, seen = asyncio.Event(), asyncio.Event(), []
    handler = blocking_scripted(gate, ready, seen, chat_reply(content="done"))
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            task = await call_tool_task(client, "delegate", {"effort": "inherit", "task": "q"})
            await ready.wait()
            result = payload(await client.call_tool(
                "message_delegation", {"task_id": task.task_id, "text": "too late"}))
            assert result["status"] == "queued"
            gate.set()
            done = payload(await task.result())
            assert done["messages"] == {"delivered": 0, "undelivered": ["too late"]}

    asyncio.run(go())
    stream = next(tmp_path.glob("*.jsonl"))
    events = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()]
    messages = [e for e in events if e["t"] == "message"]
    assert len(messages) == 1, [e["t"] for e in events]
    assert messages[0]["text"] == "too late"
    assert messages[0]["delivered"] is False
    assert messages[0]["turn"] is None
    assert isinstance(messages[0]["queued_seconds"], (int, float))
    assert messages[0]["by"] == "caller"


# --- the sample validates against the schema -------------------------------------------


def test_the_message_sample_validates_against_the_schema():
    sample = Path(__file__).parent / "transcript_samples" / "message.jsonl"
    events = [json.loads(line) for line in sample.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    bad = {e["t"]: errors(e) for e in events if errors(e)}
    assert bad == {}
    assert {e["t"] for e in events} >= {"message"}
