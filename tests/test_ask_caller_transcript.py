"""The stream records when a delegation asks its caller, and what came back.

`tests/test_ask_caller.py` pins the loop's `ask` callback and
`tests/test_ask_caller_server.py` pins the MCP tools above it. These pin the third
surface: the transcript a watcher reads while the run waits. A run that pauses on a
question must say so in the stream, or a watcher sees only silence where the delegation
is waiting on its caller -- indistinguishable from one whose server died.

The two events are new kinds, so the stream's format is 1.3 under ADR-0111: an added
kind is a minor bump.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastmcp import Client

from claude_delegate_local import server, transcript
from test_ask_caller_server import scripted
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


def _events(directory: Path) -> list[dict]:
    streams = list(directory.glob("*.jsonl"))
    assert len(streams) == 1, f"expected one stream, found {[p.name for p in streams]}"
    return [json.loads(line) for line in streams[0].read_text(encoding="utf-8").splitlines()]


def _drive(tmp_path: Path, handler, *, text: str | None = None,
           best_reading: bool = False) -> list[dict]:
    """Run a write-capable delegation that asks one question, answer it, read the stream."""
    config = cfg(transcript_dir=str(tmp_path), max_turns_default=4,
                 max_turns_default_writing=4)
    mcp = server.build(config, registry(entry()), DoubleCache(config, handler))

    async def go():
        async with Client(mcp) as client:
            started = payload(await client.call_tool(
                "delegate", {"effort": "inherit", "task": "q"}))
            assert started["status"] == "running", started
            handle = started["handle"]
            asked = payload(await client.call_tool(
                "collect", {"handle": handle, "wait_seconds": 5}))
            assert asked["status"] == "question", asked
            answer = {"handle": handle, "wait_seconds": 5}
            if best_reading:
                answer["best_reading"] = True
            else:
                answer["text"] = text
            done = payload(await client.call_tool("answer", answer))
            assert done["status"] == "done", done

    asyncio.run(go())
    return _events(tmp_path)


def test_a_question_and_its_answer_are_written_to_the_stream(tmp_path):
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    events = _drive(tmp_path, handler, text="foo.py")

    questions = [e for e in events if e["t"] == "question"]
    assert len(questions) == 1, [e["t"] for e in events]
    assert questions[0]["questions"] == ["Which file?"]

    answers = [e for e in events if e["t"] == "answer"]
    assert len(answers) == 1, [e["t"] for e in events]
    assert answers[0]["text"] == "foo.py"
    waited = answers[0]["waited_seconds"]
    assert isinstance(waited, (int, float)) and waited >= 0
    assert "best_reading" not in answers[0]

    kinds = [e["t"] for e in events]
    assert kinds.index("question") < kinds.index("answer") < kinds.index("end"), kinds


def test_a_best_reading_answer_says_so_in_the_stream(tmp_path):
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="I choose foo.py"),
    )
    events = _drive(tmp_path, handler, best_reading=True)

    answers = [e for e in events if e["t"] == "answer"]
    assert len(answers) == 1, [e["t"] for e in events]
    assert answers[0]["best_reading"] is True
    assert "best reading" in answers[0]["text"]


def test_the_new_events_validate_against_the_schema(tmp_path):
    """Every event the question flow writes matches the shipped schema.

    The validator is the one test_transcript_format.py uses, so a kind added to the
    schema and a kind added to the writer cannot drift apart.
    """
    handler, _ = scripted(
        tool_call_reply("ask_caller", {"questions": ["Which file?"]}),
        chat_reply(content="it is foo.py"),
    )
    events = _drive(tmp_path, handler, text="foo.py")
    bad = {e["t"]: errors(e) for e in events if errors(e)}
    assert bad == {}


def test_the_format_is_1_3():
    assert transcript.FORMAT == "1.3"
    assert schema()["x-transcript-format"] == "1.3"
