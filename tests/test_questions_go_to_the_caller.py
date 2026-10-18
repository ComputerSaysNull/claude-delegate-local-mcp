"""Every question a delegation asks goes to its caller, never to the person.

The person-question route (ADR-0120) and its three-lines-of-80 cap (ADR-0123) are gone: a
model that still sends `for_person` is asked of the caller like any other question, the
client is never elicited, and a long question is not refused. The caller passes on what
only the person can decide, in its own words and without a prompt-size cap.
"""

from __future__ import annotations

import asyncio

from fastmcp import Client

from claude_delegate_local import tools
from test_ask_caller_server import _mcp, scripted
from test_server import cfg, chat_reply, payload, tool_call_reply


def _ask(arguments: dict) -> tuple[dict, list[str]]:
    """Run a read-only call whose model asks with `arguments`, under a client that can
    elicit; return what the call answered and every message the client was asked."""
    handler, _ = scripted(
        tool_call_reply("ask_caller", arguments),
        chat_reply(content="done"),
    )
    mcp = _mcp(cfg(), handler)
    elicits: list[str] = []
    out: dict = {}

    async def elicit(message, response_type, params, context):
        elicits.append(message)
        return "blue"

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            out.update(payload(await client.call_tool(
                "delegate_readonly",
                {"task": "q", "effort": "inherit", "may_ask": True})))

    asyncio.run(go())
    return out, elicits


def test_a_question_marked_for_the_person_goes_to_the_caller():
    q, elicits = _ask({"questions": ["Which file?"], "for_person": True})
    assert elicits == [], "no question may be put to the person any more"
    assert q["status"] == "question", q
    assert q["questions"] == ["Which file?"], q
    assert "person" not in q, q


def test_a_long_question_is_not_refused():
    long = "Which of the two readings is meant here? " * 10      # 410 chars, > 3 x 80
    q, elicits = _ask({"questions": [long], "for_person": True})
    assert elicits == []
    assert q["status"] == "question", q
    assert q["questions"] == [long], q


def test_ask_caller_offers_no_person_mark():
    assert "for_person" not in tools.ASK_CALLER_SPEC.input_schema["properties"]
    assert "for_person" not in tools.ASK_CALLER_SPEC.description
