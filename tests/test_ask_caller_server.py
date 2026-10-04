"""The server-level `ask_caller` wiring: `may_ask`, `collect`'s `question` state, `answer`.

`tests/test_ask_caller.py` pins the loop's `ask` callback. This pins the layer above it --
the MCP tools a client actually calls, and the contract those tools expose on the wire.
`delegate`/`delegate_to_agent` gain `may_ask`; with it true (the default) the model is
offered `ask_caller`, a question pauses the run, and `answer` hands the caller's reply back
to the paused run.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from claude_delegate_local import server
from test_server import (
    DoubleCache,
    cfg,
    chat_handler,
    chat_reply,
    delegated,
    entry,
    payload,
    recording_handler,
    registry,
    serves_metrics,
    tool_call_reply,
)
from wire_double import as_stream


def scripted(*replies):
    """One canned reply per chat request, each request body recorded.

    Wrapped in `serves_metrics` so the decode-rate sampler's `/metrics` scrape (ADR-0055)
    cannot consume a queued reply, exactly as test_server's own turn helpers do.
    """
    seen = []
    remaining = list(replies)

    def inner(request):
        seen.append(json.loads(request.content))
        return as_stream(remaining.pop(0))

    return serves_metrics(inner), seen


def _mcp(config, handler):
    return server.build(config, registry(entry()), DoubleCache(config, handler))


async def _delegate(client, **args):
    """Start a write-capable delegation and return its `status: running` payload."""
    args.setdefault("effort", "inherit")
    started = payload(await client.call_tool("delegate", args))
    assert started["status"] == "running"
    return started


def test_collect_reports_the_question_and_answer_resumes_the_run():
    config = cfg()
    handler, seen = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            started = await _delegate(client, task="q")
            handle = started["handle"]

            t0 = time.monotonic()
            q = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 5}))
            elapsed = time.monotonic() - t0
            assert q["status"] == "question", q
            assert q["handle"] == handle
            assert q["tool"] == "delegate"
            assert q["questions"] == ["Which file?"]
            assert isinstance(q["waiting_seconds"], (int, float))
            assert elapsed < 5, "collect must not wait out its whole wait on a question"

            done = payload(await client.call_tool(
                "answer", {"handle": handle, "text": "foo.py", "wait_seconds": 5}))
            assert done["status"] == "done", done
            assert done["answer"] == "it is foo.py"

    asyncio.run(go())

    # The answer came back as the tool result of the call that asked: turn 2's request
    # carries the caller's reply where the model reads any other tool's output.
    assert len(seen) == 2
    assert "foo.py" in json.dumps(seen[1])


def test_best_reading_leaves_the_choice_to_the_model():
    config = cfg()
    handler, seen = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="I choose foo.py"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            started = await _delegate(client, task="q")
            handle = started["handle"]
            q = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 5}))
            assert q["status"] == "question", q
            done = payload(await client.call_tool(
                "answer", {"handle": handle, "best_reading": True, "wait_seconds": 5}))
            assert done["status"] == "done", done
            assert done["answer"] == "I choose foo.py"

    asyncio.run(go())

    # `best_reading` does not hand back a choice the caller did not make; the model is
    # told to proceed on its own best reading, which is what the tool result must say.
    assert len(seen) == 2
    assert "best reading" in json.dumps(seen[1])


def test_may_ask_false_withholds_the_tool():
    # The default is on: a plain delegate offers `ask_caller`.
    sent = []
    delegated(recording_handler(sent), task="q")
    declared = {t["function"]["name"] for t in sent[0].get("tools", [])}
    assert "ask_caller" in declared, "may_ask defaults to true, so the tool must be offered"

    # `may_ask=False` withholds it.
    sent = []
    delegated(recording_handler(sent), task="q", may_ask=False)
    declared = {t["function"]["name"] for t in sent[0].get("tools", [])}
    assert "ask_caller" not in declared


def test_answer_needs_exactly_one_of_text_and_best_reading():
    config = cfg()
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            started = await _delegate(client, task="q")
            handle = started["handle"]
            q = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 5}))
            assert q["status"] == "question", q

            with pytest.raises(ToolError):
                await client.call_tool("answer", {"handle": handle})
            with pytest.raises(ToolError):
                await client.call_tool(
                    "answer", {"handle": handle, "text": "x", "best_reading": True})

            still = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 1}))
            assert still["status"] == "question", still

    asyncio.run(go())


def test_answer_refuses_a_run_that_is_not_waiting():
    config = cfg()
    mcp = _mcp(config, serves_metrics(chat_handler()))

    async def go():
        async with Client(mcp) as client:
            started = await _delegate(client, task="q")
            handle = started["handle"]
            done = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 5}))
            assert done["status"] == "done", done

            with pytest.raises(ToolError) as exc:
                await client.call_tool("answer", {"handle": handle, "text": "x"})
            assert "waiting" in str(exc.value).lower()

    asyncio.run(go())


def test_cancelling_a_paused_run_ends_it(tmp_path):
    """A run waiting on its caller is cancelled like any other: it stops waiting, `answer`
    then has nothing to hand to, and its stream still ends rather than staying open."""
    config = cfg(transcript_dir=str(tmp_path))
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="never reached"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            handle = (await _delegate(client, task="q"))["handle"]
            asked = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 5}))
            assert asked["status"] == "question", asked
            stopped = payload(await client.call_tool(
                "cancel_delegation", {"handle": handle}))
            assert stopped["stopped"] is True, stopped
            after = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 1}))
            assert after["status"] == "cancelled", after
            with pytest.raises(ToolError):
                await client.call_tool("answer", {"handle": handle, "text": "x"})

    asyncio.run(go())
    lines = [json.loads(line) for path in tmp_path.glob("*.jsonl")
             for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines and lines[-1]["t"] == "end", [e["t"] for e in lines]


def test_an_unanswered_question_ends_the_run_after_the_limit():
    config = cfg(question_wait_limit=0.5)
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="never reached"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            started = await _delegate(client, task="q")
            handle = started["handle"]
            # Asked, and reported at once; nobody answers, so once the limit has passed
            # the run has ended rather than waiting on a caller who has gone.
            asked = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 3}))
            assert asked["status"] == "question", asked
            await asyncio.sleep(1.0)
            row = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 3}))
            assert row["status"] == "done", row
            assert row.get("ok") is False
            assert "not answered" in row.get("error", "")

    asyncio.run(go())
