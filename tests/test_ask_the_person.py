"""A question only the person can answer goes to the person (ADR-0118).

`ask_caller` gains `for_person`: a question the model marks as one only the person behind
the caller can answer is put to that person directly through the client's elicitation
capability, rather than surfacing to the caller as a `status: "question"`. On an accept the
run is answered and the waiting call keeps going; on a decline, a cancel, an error or a
client that cannot elicit, the question still reaches the caller, with `person` saying which
of those happened. A question not marked `for_person` never elicits.
"""

from __future__ import annotations

import asyncio
import json

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

from claude_delegate_local import tools
from test_ask_caller_server import _mcp, scripted
from test_server import cfg, chat_reply, payload, tool_call_reply


def _handler(*replies):
    handler, seen = scripted(*replies)
    return handler, seen


def test_a_read_only_call_for_the_person_is_answered_directly():
    """`for_person` makes the read-only inline call elicit and keep waiting: it returns the
    finished result, never `status: question`, and the person's text reaches the model as
    the `ask_caller` tool result."""
    config = cfg()
    handler, seen = _handler(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="it is foo.py"),
    )
    mcp = _mcp(config, handler)
    elicits: list[str] = []

    async def elicit(message, response_type, params, context):
        elicits.append(message)
        return "blue"

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            result = payload(await client.call_tool(
                "delegate_readonly",
                {"task": "q", "effort": "inherit", "may_ask": True}))
            assert result["answer"] == "it is foo.py", result
            assert "status" not in result, result
            assert "handle" not in result, result

    asyncio.run(go())

    assert len(elicits) == 1
    assert "Which file?" in elicits[0]
    # The person's text came back as the result of the call that asked: turn 2's request
    # carries it where the model reads any other tool's output.
    assert "blue" in json.dumps(seen[1])


def test_a_write_capable_collect_for_the_person_returns_the_finished_result():
    """The write-capable pair answer with a handle; `collect` elicits for the person and,
    on accept, keeps waiting so the caller gets the run's finished result."""
    config = cfg()
    handler, seen = _handler(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="it is foo.py"),
    )
    mcp = _mcp(config, handler)

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

    assert "blue" in json.dumps(seen[1])


def test_a_cancelled_person_question_returns_to_the_caller_and_answer_completes():
    """A cancel is not the person's answer: the question comes back to the caller with
    `person: cancelled`, and answering through the `answer` tool still completes the run."""
    config = cfg()
    handler, _ = _handler(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)

    async def elicit(message, response_type, params, context):
        return ElicitResult(action="cancel")

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            q = payload(await client.call_tool(
                "delegate_readonly",
                {"task": "q", "effort": "inherit", "may_ask": True}))
            assert q["status"] == "question", q
            assert q["person"] == "cancelled", q
            done = payload(await client.call_tool(
                "answer", {"handle": q["handle"], "text": "foo.py", "wait_seconds": 5}))
            assert done["status"] == "done", done
            assert done["answer"] == "done", done

    asyncio.run(go())


def test_a_declined_person_question_reports_declined():
    config = cfg()
    handler, _ = _handler(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)

    async def elicit(message, response_type, params, context):
        return ElicitResult(action="decline")

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            q = payload(await client.call_tool(
                "delegate_readonly",
                {"task": "q", "effort": "inherit", "may_ask": True}))
            assert q["status"] == "question", q
            assert q["person"] == "declined", q

    asyncio.run(go())


def test_a_client_without_elicitation_reports_unavailable():
    """A client that never declared the elicitation capability cannot be asked, so the
    question comes back to the caller with `person: unavailable`."""
    config = cfg()
    handler, _ = _handler(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)

    async def go():
        async with Client(mcp) as client:
            q = payload(await client.call_tool(
                "delegate_readonly",
                {"task": "q", "effort": "inherit", "may_ask": True}))
            assert q["status"] == "question", q
            assert q["person"] == "unavailable", q

    asyncio.run(go())


def test_a_question_not_marked_for_the_person_never_elicits():
    """The negative control: without `for_person`, the elicitation handler is never called
    and the question dict carries no `person` key."""
    config = cfg()
    handler, _ = _handler(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)
    elicits: list[str] = []

    async def elicit(message, response_type, params, context):
        elicits.append(message)
        return "blue"

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            q = payload(await client.call_tool(
                "delegate_readonly",
                {"task": "q", "effort": "inherit", "may_ask": True}))
            assert q["status"] == "question", q
            assert "person" not in q, q

    asyncio.run(go())
    assert elicits == [], "a question not marked for the person must never elicit"


def test_the_person_is_asked_at_most_once():
    """`person_asked` is set after the first try, so a later wait never asks the person
    again -- it reports the run as still waiting on the question."""
    config = cfg()
    handler, _ = _handler(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)
    elicits: list[str] = []

    async def elicit(message, response_type, params, context):
        elicits.append(message)
        return ElicitResult(action="cancel")

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            started = payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))
            handle = started["handle"]
            q = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 5}))
            assert q["status"] == "question", q
            assert q["person"] == "cancelled", q
            again = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 1}))
            assert again["status"] == "question", again

    asyncio.run(go())
    assert len(elicits) == 1


def test_an_elicitation_error_reports_failed_and_does_not_fail_the_run():
    """An elicitation error must not fail the run or the call: the question comes back with
    `person: failed`, and the caller can still answer it with the `answer` tool."""
    config = cfg()
    handler, _ = _handler(
        tool_call_reply("ask_caller", {"questions": ["Which file?"], "for_person": True}),
        chat_reply(content="done"),
    )
    mcp = _mcp(config, handler)

    async def elicit(message, response_type, params, context):
        raise RuntimeError("the elicitation broke")

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            q = payload(await client.call_tool(
                "delegate_readonly",
                {"task": "q", "effort": "inherit", "may_ask": True}))
            assert q["status"] == "question", q
            assert q["person"] == "failed", q
            done = payload(await client.call_tool(
                "answer", {"handle": q["handle"], "text": "foo.py", "wait_seconds": 5}))
            assert done["status"] == "done", done
            assert done["answer"] == "done", done

    asyncio.run(go())


def test_ask_caller_schema_declares_for_person_as_an_optional_boolean():
    """The model-facing contract: `for_person` is a boolean the model may set, and it is
    not required -- a question without it is asked of the caller as always."""
    schema = tools.ASK_CALLER_SPEC.input_schema
    assert "for_person" in schema["properties"]
    assert schema["properties"]["for_person"]["type"] == "boolean"
    assert "for_person" not in schema.get("required", [])


def test_the_caller_is_told_what_person_means():
    """The other half of the contract: `collect` and `answer` declare the `person` field and
    every value the server writes, as the client sees them on the wire."""
    async def go():
        async with Client(_mcp(cfg(), scripted([]))) as client:
            return {t.name: t for t in await client.list_tools()}

    listed = asyncio.run(go())
    for name in ("collect", "answer"):
        person = listed[name].outputSchema["properties"]["person"]
        assert set(person["enum"]) == {"declined", "cancelled", "failed", "unavailable"}
    assert "person" in listed["answer"].description
