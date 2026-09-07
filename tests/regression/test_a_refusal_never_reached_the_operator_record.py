"""End to end: a refused tool call's reason reaches the transcript on disk, and does so
without the calling session having asked for anything.

The item this closes was filed because a delegation reporting `tool_errors: 1` across
twelve `read_git` calls could not be diagnosed *even with transcripts enabled*. So the
unit-level checks on `ToolCallRecord` are not sufficient evidence on their own -- the
claim is about what an operator finds in a file, through the real MCP tool, and every
layer between the loop and that file is part of it.

`diagnostics` is deliberately never passed. ADR-0039's last decision is that a configured
transcript asks for the per-turn records itself, and ADR-0060 leaves the caller's flag
shaping the reply alone. If that ever regresses, the fields below go quietly empty rather
than failing, which is exactly the shape of bug this file exists to catch.
"""

from __future__ import annotations

import json

import httpx

from test_server import cfg, chat_reply
from test_transcript import records, run


def wants(tool: str, **arguments: object):
    """Ask for one tool call, then answer once its result comes back.

    Keyed on the request rather than on a call counter. A counter looked obviously right
    and was not: something ahead of the delegation makes its own request through this
    double, so the first scripted reply was consumed before the loop ever saw it and the
    delegation ran with the answer, one turn and no tool calls. Reading the history makes
    the double independent of how many times it is called.
    """
    asked = {
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "function": {"name": tool, "arguments": json.dumps(arguments)}}
            ],
        },
    }

    async def handler(request):
        sent = json.loads(request.content or b"{}").get("messages") or []
        answered = any(message.get("role") == "tool" for message in sent)
        body = chat_reply(content="done") if answered else chat_reply(choices=[asked])
        return httpx.Response(200, json=body)

    return handler


def calls_in(record: dict) -> list[dict]:
    return [call for turn in record["per_turn"] for call in turn["tool_calls"]]


def test_a_refused_call_records_why_it_refused(tmp_path):
    """The gap, closed. `run_bash` is not in `allowed_tools`, so `execute_tool` refuses
    it and the reason was previously handed to the model and dropped."""
    run(
        wants("run_bash", command="pytest -q"),
        config=cfg(transcript_dir=str(tmp_path)),
        task="x",
        allowed_tools=["read_file"],
    )
    refused = [c for c in calls_in(records(tmp_path)[0]) if c["outcome"] == "error"]

    assert refused, "no refused call reached the record at all"
    assert "not available in this delegation" in refused[0]["message"]
    assert refused[0]["arguments"] == {"command": "pytest -q"}


def test_a_call_that_ran_records_no_message(tmp_path):
    """The negative direction. A record that put text on every call would pass the test
    above while saying nothing about refusals in particular."""
    run(
        wants("read_file", path="/nope.py"),
        config=cfg(transcript_dir=str(tmp_path)),
        task="x",
        allowed_tools=["read_file"],
    )
    record = records(tmp_path)[0]
    ran = [c for c in calls_in(record) if c["outcome"] != "error"]

    for call in ran:
        assert "message" not in call, call


def test_the_reply_is_unchanged_by_any_of_this(tmp_path):
    """ADR-0060 keeps the caller's `diagnostics` flag reply-shaping only. The record grew;
    an unasked-for reply must not have."""
    reply = run(
        wants("run_bash", command="pytest -q"),
        config=cfg(transcript_dir=str(tmp_path)),
        task="x",
        allowed_tools=["read_file"],
    )

    assert "diagnostics" not in reply, "the caller did not ask, so the reply carries none"
    assert records(tmp_path)[0]["per_turn"], "and the transcript got it anyway"
