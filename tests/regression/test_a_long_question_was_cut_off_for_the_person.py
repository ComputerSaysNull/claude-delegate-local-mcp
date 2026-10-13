"""A long question for the person was cut off where they read it (PLAN Unscheduled 112).

Claude Code shows an elicited message in at most three lines, cuts each at the panel's
width -- about 90 characters at its usual width -- and offers no way to expand it (JOURNAL
2026-10-13). The server sent a header line and then each question whole on one line, so two
delegations' long questions reached the person as their first few words. A question for the
person now has to fit three lines of 80 characters, is sent wrapped to them with no header,
and one that does not fit is refused to the model, which can shorten it and ask again.
"""

from __future__ import annotations

import asyncio
import json

from fastmcp import Client

from test_ask_caller_server import _mcp, scripted
from test_server import cfg, chat_reply, payload, tool_call_reply

LINES, WIDTH = 3, 80
LONG = "Which of the two hand-off documents should I follow " * 6  # ~310 characters
FITS = ("Should the scanner skip binary files, report them as unreadable, or stop the run "
        "at the first one it meets, given that the spec names none of these?")  # ~150


def _asked(*question_calls: dict):
    """Run a read-only delegation whose model asks each call in turn; return what reached
    the person, and every request the model was sent."""
    handler, seen = scripted(
        *(tool_call_reply("ask_caller", args) for args in question_calls),
        chat_reply(content="done"),
    )
    elicits: list[str] = []

    async def elicit(message, response_type, params, context):
        elicits.append(message)
        return "skip them"

    async def go():
        async with Client(_mcp(cfg(), handler), elicitation_handler=elicit) as client:
            result = payload(await client.call_tool(
                "delegate_readonly", {"task": "q", "effort": "inherit", "may_ask": True}))
            assert result["answer"] == "done", result

    asyncio.run(go())
    return elicits, seen


def test_a_question_too_long_for_the_prompt_goes_back_to_the_model():
    elicits, seen = _asked(
        {"questions": [LONG], "for_person": True},
        {"questions": ["Skip binary files?"], "for_person": True},
    )
    # The long one never reached the person, and did not use up the run's one question.
    assert elicits == ["Skip binary files?"], elicits
    told = json.dumps(seen[1])
    assert f"{LINES} lines of {WIDTH} characters" in told, told


def test_a_question_that_fits_is_sent_wrapped_with_no_header():
    elicits, _ = _asked({"questions": [FITS], "for_person": True})
    assert len(elicits) == 1, elicits
    lines = elicits[0].split("\n")
    assert len(lines) <= LINES and all(len(line) <= WIDTH for line in lines), lines
    assert " ".join(lines) == FITS


def test_several_questions_each_start_their_own_line():
    elicits, _ = _asked({"questions": ["Skip binary files?", "Follow the newer spec?"],
                         "for_person": True})
    assert elicits == ["- Skip binary files?\n- Follow the newer spec?"], elicits


def test_a_long_question_for_the_caller_is_not_refused():
    """The control: the limit is the person's prompt, so only their questions meet it."""
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": [LONG]}),
        chat_reply(content="done"),
    )

    async def go():
        async with Client(_mcp(cfg(), handler)) as client:
            started = payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))
            asked = payload(await client.call_tool(
                "collect", {"handle": started["handle"], "wait_seconds": 5}))
            assert asked["status"] == "question", asked
            assert asked["questions"] == [LONG]
            await client.call_tool("cancel_delegation", {"handle": started["handle"]})

    asyncio.run(go())
