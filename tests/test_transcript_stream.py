"""The transcript written while a delegation runs, as opposed to once it is over.

`transcript.write` answers what happened. These cover the stream that answers what *is*
happening, which is a different question and cannot be served by the same file: a record
produced at the end cannot say whether a delegation is still making progress.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastmcp import Client

from claude_delegate_local import server, transcript
from test_server import DoubleCache, cfg, chat_reply, entry, registry, tool_call_reply


def _events(directory: Path) -> list[dict]:
    streams = list(directory.glob("*.jsonl"))
    assert len(streams) == 1, f"expected one stream, found {[p.name for p in streams]}"
    return [json.loads(line) for line in streams[0].read_text(encoding="utf-8").splitlines()]


def _run(tmp_path: Path, handler, tool: str, args: dict) -> list[dict]:
    config = cfg(transcript_dir=str(tmp_path), max_turns_default=4)
    mcp = server.build(config, registry(entry()), DoubleCache(config, handler))

    # `effort` is required on every delegation tool, and "inherit" is the value that
    # preserves the pre-ADR-0045 behaviour exactly: it falls through to the agent, the
    # registry row and then `thinking_default`. A test that cares about the level sets
    # it itself; that it is *refused* when absent is asserted directly, not here.
    args = {"effort": "inherit", **args}

    async def go():
        async with Client(mcp) as client:
            return (await client.call_tool(tool, args)).data

    asyncio.run(go())
    return _events(tmp_path)


def two_turns(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if any(m.get("role") == "tool" for m in body.get("messages", [])):
        return httpx.Response(200, json=chat_reply(content="the retry is bounded by time"))
    return httpx.Response(200, json=tool_call_reply("read_file", {"path": "/nope.py"}))


def test_a_delegation_streams_start_turns_and_end(tmp_path):
    """The three shapes a watcher needs, in order, while the work is still going."""
    events = _run(tmp_path, two_turns, "delegate", {"task": "explain the retry"})

    kinds = [e["t"] for e in events]
    assert kinds[0] == "start", kinds
    assert kinds[-1] == "end", kinds
    assert "turn" in kinds, kinds
    assert events[0]["task"] == "explain the retry"
    assert events[-1]["ok"] is True


def test_a_streamed_turn_carries_what_the_model_said(tmp_path):
    """The reason the stream exists rather than the counters alone.

    ADR-0039 excluded file *bodies* as bulky and recoverable by path. A reply is neither,
    and is the thing a person is reading the stream to see.
    """
    events = _run(tmp_path, two_turns, "delegate", {"task": "explain the retry"})
    said = [e.get("text", "") for e in events if e["t"] == "turn"]

    assert any("retry is bounded by time" in t for t in said), said


def test_a_one_shot_delegation_still_streams_its_answer(tmp_path):
    """delegate_readonly runs no turns, and a start-then-end stream would look like a
    delegation that produced nothing -- indistinguishable from one that failed silently."""
    events = _run(
        tmp_path,
        lambda r: httpx.Response(200, json=chat_reply(content="a one-shot answer")),
        "delegate_readonly", {"task": "summarise"},
    )
    turns = [e for e in events if e["t"] == "turn"]

    assert len(turns) == 1, [e["t"] for e in events]
    assert "a one-shot answer" in turns[0]["text"]


def test_throughput_is_measured_over_the_backend_call_not_the_turn(tmp_path):
    """A rate divided by the turn's wall clock would fold tool execution into the divisor
    and report the cluster as slower than it is. Both intervals are kept, and the rate
    must be derived from the shorter one."""
    events = _run(tmp_path, two_turns, "delegate", {"task": "explain the retry"})
    turns = [e for e in events if e["t"] == "turn"]

    assert turns, "no turns streamed"
    for t in turns:
        assert "backend_ms" in t and "ms" in t
        assert t["backend_ms"] is None or t["ms"] is None or t["backend_ms"] <= t["ms"], (
            f"backend time {t['backend_ms']}ms exceeds the turn's own {t['ms']}ms, so one "
            "of them is measuring the wrong interval"
        )


def test_nothing_is_streamed_when_no_transcript_directory_is_set(tmp_path):
    """The switch is one setting and it governs both files.

    Asserted on `open_stream` rather than by running a delegation and looking in a
    directory: with the setting empty there is no configured directory to look in, so a
    test that checks `tmp_path` is checking somewhere the stream was never going to be
    written and would pass with the switch removed entirely. It was written that way
    first, and could not fail.
    """
    assert transcript.open_stream(cfg(transcript_dir=""), None) is None
    assert transcript.open_stream(cfg(transcript_dir=str(tmp_path)), None) is not None
