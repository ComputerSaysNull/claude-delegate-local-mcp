"""The turn event says how repetitive its thinking was, as a share of repeated lines.

`turn.reasoning_duplicate_line_share` is `duplicate_line_share` measured on the
answering attempt's thinking: the same function `turn.duplicate_line_share` applies to
the reply text, so a reader tells a turn looping on its reasoning from one looping on
its reply. Measured where the diagnostic is built, beside `reasoning_seconds`, and null
when the turn had no thinking.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from claude_delegate_local import server
from claude_delegate_local.backends import base
from claude_delegate_local.backends import openai_compat as oc
from claude_delegate_local.registry import ModelEntry
from test_server import (
    answered,
    cfg as server_cfg,
    entry as server_entry,
    registry,
    serves_metrics,
)
from wire_double import Clock, delta, paced

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist

# A thinking that repeats itself, and a reply that does not, so the two shares diverge.
LOOPING_THINKING = "same line\nsame line\nsame line"
CLEAN_REPLY = "a clean, non-repeating answer"
# The mirror: clean thinking, a reply that loops.
CLEAN_THINKING = "one clean thought"
LOOPING_REPLY = "repeat me\nrepeat me\nrepeat me"


class _ClockedCache(server.BackendCache):
    """A cache whose backends speak to a transport double on a clock a test drives."""

    def __init__(self, config, handler, clock) -> None:
        super().__init__(config)
        self._handler = handler
        self._clock = clock

    def get(self, model: ModelEntry):
        backend = self._backends.get(model.key)
        if backend is None:
            client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))
            backend = oc.OpenAICompatBackend(
                self._cfg, model, client=client, clock=self._clock)
            self._backends[model.key] = backend
        return backend


def _events(directory: Path) -> list[dict]:
    streams = list(directory.glob("*.jsonl"))
    assert len(streams) == 1, f"expected one stream, found {[p.name for p in streams]}"
    return [json.loads(line) for line in streams[0].read_text(encoding="utf-8").splitlines()]


def _line(at: float, frame: dict) -> tuple[float, str]:
    return (at, "data: " + json.dumps(frame) + "\n\n")


def _usage(**over) -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2}, **over}


def _reasoning_then_answer(clock, thinking: str, reply: str):
    """A stream that reasons, then answers, on the driven clock."""
    schedule = [
        (0.5, "data: " + json.dumps(
            {"model": "served-id-1", "choices": [{"index": 0, "delta": {"role": "assistant"}}]}
        ) + "\n\n"),
        _line(1.0, delta(reasoning=thinking)),
        _line(4.5, delta(content=reply)),
        _line(4.5, _usage(prompt_tokens=7, completion_tokens=3)),
        (4.5, "data: [DONE]\n\n"),
    ]
    return serves_metrics(paced(clock, schedule))


def _answer_only(clock):
    """A stream that answers at once, never reasoning, on the driven clock."""
    schedule = [
        (0.5, "data: " + json.dumps(
            {"model": "served-id-1", "choices": [{"index": 0, "delta": {"role": "assistant"}}]}
        ) + "\n\n"),
        _line(1.0, delta(content="hello")),
        _line(2.0, _usage(prompt_tokens=7, completion_tokens=3)),
        (2.0, "data: [DONE]\n\n"),
    ]
    return serves_metrics(paced(clock, schedule))


def _run(tmp_path: Path, handler, clock, *, tool: str, args: dict) -> list[dict]:
    config = server_cfg(transcript_dir=str(tmp_path), max_turns_default=4)
    mcp = server.build(config, registry(server_entry()), _ClockedCache(config, handler, clock))
    args = {"effort": "inherit", **args}

    async def go():
        async with Client(mcp) as client:
            return await answered(client, tool, args)

    asyncio.run(go())
    return _events(tmp_path)


def _turn(events: list[dict]) -> dict:
    turns = [e for e in events if e["t"] == "turn"]
    assert turns, [e["t"] for e in events]
    return turns[0]


def test_looping_thinking_over_a_clean_reply_is_reported_high(tmp_path):
    clock = Clock()
    events = _run(tmp_path, _reasoning_then_answer(clock, LOOPING_THINKING, CLEAN_REPLY), clock,
                  tool="delegate", args={"task": "explain the retry"})
    turn = _turn(events)
    assert turn["reasoning_duplicate_line_share"] > 0.5
    assert turn["duplicate_line_share"] == 0


def test_looping_reply_over_clean_thinking_is_not_swapped(tmp_path):
    """The pin: the two shares are not transposed, or a looping reply would read as a
    looping thought."""
    clock = Clock()
    events = _run(tmp_path, _reasoning_then_answer(clock, CLEAN_THINKING, LOOPING_REPLY), clock,
                  tool="delegate", args={"task": "explain the retry"})
    turn = _turn(events)
    assert turn["reasoning_duplicate_line_share"] == 0
    assert turn["duplicate_line_share"] > 0.5


def test_a_turn_with_no_thinking_reports_none(tmp_path):
    clock = Clock()
    events = _run(tmp_path, _answer_only(clock), clock,
                  tool="delegate_readonly", args={"task": "summarise"})
    turn = _turn(events)
    assert turn["reasoning_duplicate_line_share"] is None


def test_the_value_is_duplicate_line_share_of_the_thinking(tmp_path):
    clock = Clock()
    events = _run(tmp_path, _reasoning_then_answer(clock, LOOPING_THINKING, CLEAN_REPLY), clock,
                  tool="delegate", args={"task": "explain the retry"})
    turn = _turn(events)
    assert turn["reasoning_duplicate_line_share"] == pytest.approx(
        base.duplicate_line_share(LOOPING_THINKING)
    )


def test_the_one_shot_path_also_writes_it(tmp_path):
    clock = Clock()
    events = _run(
        tmp_path, _reasoning_then_answer(clock, LOOPING_THINKING, CLEAN_REPLY), clock,
        tool="delegate", args={"task": "explain the retry", "allowed_tools": []},
    )
    turn = _turn(events)
    assert turn["reasoning_duplicate_line_share"] == pytest.approx(
        base.duplicate_line_share(LOOPING_THINKING)
    )
