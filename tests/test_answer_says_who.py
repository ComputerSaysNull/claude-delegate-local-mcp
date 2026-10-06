"""The transcript's `answer` event says who answered the question (ADR-0118/0120).

A delegation's question is answered either by the caller, through the `answer` MCP tool,
or -- since ADR-0120 -- by the person, through MCP elicitation in `_collect_wait`. The
stream's `answer` event must record which, so a watcher can tell a caller's reply from a
person's. `by` follows who actually supplied the text, not the `for_person` mark: a
`for_person` question the person declines and the caller then answers is `by: "caller"`.
"""

from __future__ import annotations

import asyncio

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

from claude_delegate_local import server, transcript
from claude_delegate_local.config import Config
from test_ask_caller_transcript import _drive, _events
from test_ask_the_person import scripted
from test_server import (
    DoubleCache,
    cfg,
    chat_reply,
    entry,
    payload,
    registry,
    tool_call_reply,
)
from test_transcript_format import errors, schema


def _config(tmp_path) -> Config:
    return cfg(transcript_dir=str(tmp_path), max_turns_default=4,
               max_turns_default_writing=4)


def _mcp_dir(config, handler):
    return server.build(config, registry(entry()), DoubleCache(config, handler))


def _answers(events: list[dict]) -> list[dict]:
    return [e for e in events if e["t"] == "answer"]


def test_a_person_answer_is_marked_by_person(tmp_path):
    """The person accepts through elicitation: the stream says `by: "person"`."""
    config = _config(tmp_path)
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="it is foo.py"),
    )
    mcp = _mcp_dir(config, handler)

    async def elicit(message, response_type, params, context):
        return "blue"

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            started = payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))
            assert started["status"] == "running", started
            done = payload(await client.call_tool(
                "collect", {"handle": started["handle"], "wait_seconds": 5}))
            assert done["status"] == "done", done
            assert done["answer"] == "it is foo.py", done

    asyncio.run(go())

    answers = _answers(_events(tmp_path))
    assert len(answers) == 1, answers
    assert answers[0]["by"] == "person", answers[0]
    assert errors(answers[0]) == [], errors(answers[0])


def test_a_caller_answer_is_marked_by_caller(tmp_path):
    """An ordinary question the caller answers through `answer`: `by: "caller"`."""
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    events = _drive(tmp_path, handler, text="foo.py")

    answers = _answers(events)
    assert len(answers) == 1, answers
    assert answers[0]["by"] == "caller", answers[0]
    assert errors(answers[0]) == [], errors(answers[0])


def test_a_person_question_the_caller_answers_is_marked_by_caller(tmp_path):
    """A `for_person` question the person cancels, then the caller answers through the
    `answer` tool, is `by: "caller"` -- the mark does not decide, the answerer does."""
    config = _config(tmp_path)
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="done"),
    )
    mcp = _mcp_dir(config, handler)

    async def elicit(message, response_type, params, context):
        return ElicitResult(action="cancel")

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            started = payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))
            assert started["status"] == "running", started
            handle = started["handle"]
            q = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 5}))
            assert q["status"] == "question", q
            assert q["person"] == "cancelled", q
            done = payload(await client.call_tool(
                "answer", {"handle": handle, "text": "foo.py", "wait_seconds": 5}))
            assert done["status"] == "done", done
            assert done["answer"] == "done", done

    asyncio.run(go())

    answers = _answers(_events(tmp_path))
    assert len(answers) == 1, answers
    assert answers[0]["by"] == "caller", answers[0]
    assert errors(answers[0]) == [], errors(answers[0])


def test_a_best_reading_reply_is_marked_by_caller(tmp_path):
    """A `best_reading` reply is still the caller answering: `by: "caller"`."""
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="I choose foo.py"),
    )
    events = _drive(tmp_path, handler, best_reading=True)

    answers = _answers(events)
    assert len(answers) == 1, answers
    assert answers[0]["by"] == "caller", answers[0]
    assert answers[0]["best_reading"] is True
    assert errors(answers[0]) == [], errors(answers[0])


def test_every_answer_event_validates_against_the_schema(tmp_path):
    """Every `answer` event the question flow writes matches the shipped schema, the way
    test_ask_caller_transcript does -- here for the person-answered path too."""
    config = _config(tmp_path)
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="it is foo.py"),
    )
    mcp = _mcp_dir(config, handler)

    async def elicit(message, response_type, params, context):
        return "blue"

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            started = payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))
            assert started["status"] == "running", started
            done = payload(await client.call_tool(
                "collect", {"handle": started["handle"], "wait_seconds": 5}))
            assert done["status"] == "done", done

    asyncio.run(go())

    bad = {e["t"]: errors(e) for e in _events(tmp_path) if errors(e)}
    assert bad == {}


def test_the_schema_defines_by_since_1_10():
    """Pinned by kind, not by the current version: a later minor bump must not break it."""
    defs = schema()["$defs"]
    by = defs["answer"]["properties"]["by"]
    assert by["enum"] == ["caller", "person"]
    assert "1.10" in by["description"]
    assert "by" not in defs["answer"].get("required", [])
    major, minor = (int(part) for part in transcript.FORMAT.split("."))
    assert (major, minor) >= (1, 10)
