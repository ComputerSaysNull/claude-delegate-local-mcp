"""The server-level `ask_caller` wiring: `may_ask`, the `question` return, and `answer`.

`tests/test_ask_caller.py` pins the loop's `ask` callback. This pins the layer above it --
the MCP tools a client actually calls, and the contract those tools expose on the wire.
`delegate`/`delegate_to_agent` gain `may_ask`; with it true (the default) the model is
offered `ask_caller`, a question makes the call end with `status: "question"` and a
`handle`, and `answer` hands the caller's reply back to the paused run. A run that never
asks returns its result inline, with no `status` and no `handle` (ADR-0118).
"""

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from claude_delegate_local import server
from test_server import (
    DoubleCache,
    cfg,
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
    """One canned chat reply per request, each request body recorded.

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
    """Call a write-capable delegation that asks, and return its `status: question` payload."""
    args.setdefault("effort", "inherit")
    asked = payload(await client.call_tool("delegate", args))
    assert asked["status"] == "question", asked
    return asked


def test_a_delegation_that_asks_returns_the_question_and_answer_resumes_the_run():
    config = cfg()
    handler, seen = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            asked = await _delegate(client, task="q")
            handle = asked["handle"]
            assert asked["tool"] == "delegate"
            assert asked["questions"] == ["Which file?"]
            assert isinstance(asked["waiting_seconds"], (int, float))

            done = payload(await client.call_tool(
                "answer", {"handle": handle, "text": "foo.py"}))
            assert "status" not in done, done
            assert "handle" not in done, done
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
            asked = await _delegate(client, task="q")
            handle = asked["handle"]
            done = payload(await client.call_tool(
                "answer", {"handle": handle, "best_reading": True}))
            assert "status" not in done, done
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
            asked = await _delegate(client, task="q")
            handle = asked["handle"]

            with pytest.raises(ToolError):
                await client.call_tool("answer", {"handle": handle})
            with pytest.raises(ToolError):
                await client.call_tool(
                    "answer", {"handle": handle, "text": "x", "best_reading": True})

            # The run is still parked on its question; a valid answer resumes it.
            done = payload(await client.call_tool(
                "answer", {"handle": handle, "text": "foo.py"}))
            assert done["answer"] == "done"

    asyncio.run(go())


def test_answer_refuses_a_run_that_is_not_waiting():
    config = cfg()
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            asked = await _delegate(client, task="q")
            handle = asked["handle"]
            done = payload(await client.call_tool(
                "answer", {"handle": handle, "text": "foo.py"}))
            assert done["answer"] == "it is foo.py"

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
            asked = await _delegate(client, task="q")
            handle = asked["handle"]
            stopped = payload(await client.call_tool(
                "cancel_delegation", {"handle": handle}))
            assert stopped["cancelled"] is True, stopped
            assert stopped["stopped"] is True, stopped
            with pytest.raises(ToolError):
                await client.call_tool("answer", {"handle": handle, "text": "x"})

    asyncio.run(go())
    lines = [json.loads(line) for path in tmp_path.glob("*.jsonl")
             for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines and lines[-1]["t"] == "end", [e["t"] for e in lines]


def test_an_unanswered_question_ends_the_run_after_the_limit(tmp_path):
    config = cfg(question_wait_limit=0.5, transcript_dir=str(tmp_path))
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
            await asyncio.sleep(1.0)
            with pytest.raises(ToolError):
                await client.call_tool("answer", {"handle": handle, "text": "x"})

    asyncio.run(go())
    lines = [json.loads(line) for path in tmp_path.glob("*.jsonl")
             for line in path.read_text(encoding="utf-8").splitlines()]
    end = [e for e in lines if e["t"] == "end"][-1]
    assert end["ok"] is False
    assert "not answered" in end.get("error", "")
