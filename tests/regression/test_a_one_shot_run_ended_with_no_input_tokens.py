"""A run's `end` event said nothing about the prompt tokens its turns had just reported.

`end` is built from `dispatched.total_input_tokens`, which only an `AgenticDispatch` has,
so a one-shot run streamed a `turn` carrying `input_tokens` and then an `end` carrying
`null` -- a watcher reading only the end of the stream cannot say what the run cost. The
same hole opens on a multi-turn run that fails, where `dispatched` is `None` and
`output_tokens` is summed from the streamed turns but `input_tokens` is not.

The expected rule is the one the stream already follows for `output_tokens`: sum what the
turns reported, and stay `null` when no turn reported anything, because null is "nothing
was counted" and zero would be a measurement nobody made.
"""

import asyncio
import json
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from claude_delegate_local import server
from wire_double import as_stream
from test_server import (
    DoubleCache,
    answered,
    cfg,
    chat_reply,
    entry,
    registry,
    serves_metrics,
    tool_call_reply,
)


def _events(directory: Path) -> list[dict]:
    streams = list(directory.glob("*.jsonl"))
    assert len(streams) == 1, f"expected one stream, found {[p.name for p in streams]}"
    return [
        json.loads(line)
        for line in streams[0].read_text(encoding="utf-8").splitlines()
        if line
    ]


def _run(tmp_path: Path, handler, args: dict) -> list[dict]:
    """Drive one delegation end to end and return the stream it wrote.

    A failed run raises `ToolError`; the stream is written in the run's `finally`, so the
    events are there either way and the failure is not this helper's to report.
    """
    config = cfg(transcript_dir=str(tmp_path), slots_dir=str(tmp_path / "slots"))
    mcp = server.build(config, registry(entry()), DoubleCache(config, handler))
    args = {"effort": "inherit", **args}

    async def go():
        async with Client(mcp) as client:
            try:
                return await answered(client, "delegate", args)
            except ToolError:
                return None

    asyncio.run(go())
    return _events(tmp_path)


def _only(events, kind):
    found = [e for e in events if e.get("t") == kind]
    assert found, f"no {kind} event; the stream wrote {[e.get('t') for e in events]}"
    return found


def test_a_one_shot_run_ends_with_the_input_tokens_its_turn_reported(tmp_path):
    """`delegate` with no tools runs one dispatch and streams its one turn, whose
    `input_tokens` is reported -- but `end.input_tokens` was `null`, because the one-shot
    `Dispatch` has no `total_input_tokens` for `stream.end` to read. A watcher reading the
    end of the stream alone could not say what the prompt cost."""
    handler = serves_metrics(
        lambda r: as_stream(
            chat_reply(content="a one-shot answer",
                       usage={"prompt_tokens": 710, "completion_tokens": 3})
        )
    )
    events = _run(tmp_path, handler, {"task": "summarise", "allowed_tools": []})

    [turn] = _only(events, "turn")
    assert turn["input_tokens"] == 710, turn
    end = _only(events, "end")[-1]
    assert end["input_tokens"] == 710, (
        f"end.input_tokens is {end['input_tokens']!r} while the turn it summarises "
        f"reported {turn['input_tokens']!r}"
    )
    assert end["input_tokens"] == turn["input_tokens"], (end, turn)


def _two_turns_then_unreachable():
    """Two tool-calling turns, then a dead endpoint: a run that fails after streaming.

    The turns carry distinct prompt sizes so the sum cannot be satisfied by either alone.
    """
    first = tool_call_reply("read_file", {"path": "/nope.py"})
    first["usage"] = {"prompt_tokens": 300, "completion_tokens": 3, "total_tokens": 303}
    second = tool_call_reply("read_file", {"path": "/nope2.py"}, call_id="call-1")
    second["usage"] = {"prompt_tokens": 400, "completion_tokens": 3, "total_tokens": 403}
    remaining = [first, second]

    def handler(request):
        if not remaining:
            raise httpx.ConnectError("connection failed")
        return as_stream(remaining.pop(0))

    return serves_metrics(handler)


def test_a_run_that_fails_after_two_turns_ends_with_their_input_tokens(tmp_path):
    """When the run dies mid-loop `dispatched` is `None`, so `end` sums `output_tokens`
    from the streamed turns and reads `input_tokens` off nothing at all -- `null`, on a run
    that had already streamed two turns reporting their prompt sizes. The end of the
    stream has to add up the same way the turns did, or a failed run reads as one that
    spent no prompt at all."""
    events = _run(tmp_path, _two_turns_then_unreachable(),
                  {"task": "explain the retry"})

    turns = _only(events, "turn")
    assert len(turns) == 2, [e.get("t") for e in events]
    assert [t["input_tokens"] for t in turns] == [300, 400], turns
    end = _only(events, "end")[-1]
    assert end["ok"] is False, end
    assert end["input_tokens"] == 700, (
        f"end.input_tokens is {end['input_tokens']!r} for a run whose turns reported "
        f"{[t['input_tokens'] for t in turns]}"
    )


def two_turns(request: httpx.Request) -> httpx.Response:
    """One tool-calling turn, then the answering turn: a run that finishes."""
    body = json.loads(request.content)
    if any(m.get("role") == "tool" for m in body.get("messages", [])):
        return as_stream(chat_reply(content="the retry is bounded by time"))
    return as_stream(tool_call_reply("read_file", {"path": "/nope.py"}))


def test_a_finished_run_still_ends_with_the_input_tokens_its_turns_reported(tmp_path):
    """The negative control: this path already works, and the fix must not break it.

    A finished agentic run reports `dispatched.total_input_tokens`, which is the sum of
    the turns' prompt sizes. Asserted as an equality against the streamed turns rather
    than a bare number, so a fix that replaces the source with a differently-shaped sum
    still has to add up to what the turns said."""
    events = _run(tmp_path, serves_metrics(two_turns), {"task": "explain the retry"})

    turns = _only(events, "turn")
    assert len(turns) == 2, [e.get("t") for e in events]
    end = _only(events, "end")[-1]
    assert end["ok"] is True, end
    assert end["input_tokens"] == sum(t["input_tokens"] for t in turns), (end, turns)
    assert end["input_tokens"] is not None, end
