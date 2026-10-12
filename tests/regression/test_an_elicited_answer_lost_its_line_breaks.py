"""A person's multi-line answer reached the model as one line (ADR-0120).

Claude Code returns an elicited answer with each line break as a bare carriage return, so
a pasted document arrived whole but read as one line wherever `\\r` returns to the start of
it: the delegation's model, a terminal, the transcript viewer. The person's answer is now
read back with every line break as `\\n`.
"""

from __future__ import annotations

import asyncio

from fastmcp import Client

from test_answer_says_who import _answers, _config, _mcp_dir
from test_ask_caller_transcript import _events
from test_ask_the_person import scripted
from test_server import chat_reply, payload, tool_call_reply


def _answer_given(tmp_path, typed: str) -> str:
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Paste the spec?"], "for_person": True}),
        chat_reply(content="done"),
    )
    mcp = _mcp_dir(_config(tmp_path), handler)

    async def elicit(message, response_type, params, context):
        return typed

    async def go():
        async with Client(mcp, elicitation_handler=elicit) as client:
            started = payload(await client.call_tool(
                "delegate", {"task": "q", "effort": "inherit"}))
            done = payload(await client.call_tool(
                "collect", {"handle": started["handle"], "wait_seconds": 5}))
            assert done["status"] == "done", done

    asyncio.run(go())
    answers = _answers(_events(tmp_path))
    assert len(answers) == 1 and answers[0]["by"] == "person", answers
    return answers[0]["text"]


def test_carriage_returns_in_a_person_answer_become_newlines(tmp_path):
    assert _answer_given(tmp_path, "# Spec\r\rline one\rline two") == (
        "# Spec\n\nline one\nline two")


def test_windows_line_ends_become_one_newline_each(tmp_path):
    assert _answer_given(tmp_path, "one\r\ntwo\r\n") == "one\ntwo\n"
