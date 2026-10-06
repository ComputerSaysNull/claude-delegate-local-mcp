"""The turn event says how long the model reasoned, in seconds.

`turn.reasoning_seconds` is the gap from a response's first streamed chunk to its first
chunk of kind "answer" -- a tool call counts as an answer, so a turn that reasons and then
only calls a tool ends its reasoning at the tool-call delta. Measured in the adapter, beside
`prefill_seconds` and `decode_seconds`, and never in the loop: the loop's chunk counters
accumulate across retries, so a value measured there would count the failed attempt's
reasoning against a turn that answered on the retry.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastmcp import Client

from claude_delegate_local import loop, server, transcript
from claude_delegate_local.backends import base
from claude_delegate_local.backends import openai_compat as oc
from claude_delegate_local.registry import ModelEntry
from test_loop import FakeClock, cfg as loop_cfg, entry as loop_entry
from test_server import (
    answered,
    cfg as server_cfg,
    entry as server_entry,
    registry,
    serves_metrics,
)
from wire_double import Clock, delta, paced

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist


# --- adapter-level: the gap is measured beside prefill/decode ----------------------------

def _entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": HOST, "served_model_id": "served-id-1"}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


def _cfg_a(**over):
    from claude_delegate_local.config import Config
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def _request(**over) -> base.CanonicalRequest:
    kw = {
        "system": "static system prompt",
        "messages": (base.Message("user", (base.TextBlock("hello"),)),),
        "max_tokens": 1000,
        "effort": "low",
        "temperature": 0.0,
        "top_p": 1.0,
    }
    kw.update(over)
    return base.CanonicalRequest(**kw)  # type: ignore[arg-type]


def _backend(handler, *, clock=None) -> oc.OpenAICompatBackend:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kw = {"client": client}
    if clock is not None:
        kw["clock"] = clock
    return oc.OpenAICompatBackend(_cfg_a(), _entry(), **kw)


def _line(at: float, frame: dict) -> tuple[float, str]:
    return (at, "data: " + json.dumps(frame) + "\n\n")


def _usage(**over) -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2}, **over}


async def test_the_gap_is_from_the_first_chunk_to_the_first_answer_chunk():
    """Reasoning at t=1.0, the first answer chunk at t=4.5."""
    clock = Clock()
    schedule = [
        _line(1.0, delta(reasoning="think")),
        _line(2.0, delta(reasoning="hard")),
        _line(4.5, delta(content="answer")),
        _line(4.5, _usage()),
        (4.5, "data: [DONE]\n\n"),
    ]
    r = await _backend(paced(clock, schedule), clock=clock).complete(_request())
    assert r.reasoning_seconds == pytest.approx(3.5)


async def test_a_first_chunk_that_is_already_an_answer_reports_none():
    """The model did not reason, so there is no interval to report."""
    clock = Clock()
    schedule = [
        _line(5.0, delta(content="a")),
        _line(5.0, _usage()),
        (5.0, "data: [DONE]\n\n"),
    ]
    r = await _backend(paced(clock, schedule), clock=clock).complete(_request())
    assert r.reasoning_seconds is None


async def test_reasoning_then_only_a_tool_call_counts_the_tool_call_as_the_answer():
    """A turn that reasons and then calls a tool ends its reasoning at the call."""
    clock = Clock()
    call = {"index": 0, "id": "call-0", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"}}
    schedule = [
        _line(1.0, delta(reasoning="think")),
        _line(3.0, delta(tool_calls=[call])),
        _line(3.0, _usage()),
        (3.0, "data: [DONE]\n\n"),
    ]
    r = await _backend(paced(clock, schedule), clock=clock).complete(_request())
    assert r.tool_uses
    assert r.reasoning_seconds == pytest.approx(2.0)


# --- end to end through the server ------------------------------------------------------

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


def _run(tmp_path: Path, handler, clock, *, tool: str, args: dict) -> list[dict]:
    config = server_cfg(transcript_dir=str(tmp_path), max_turns_default=4)
    mcp = server.build(config, registry(server_entry()), _ClockedCache(config, handler, clock))
    args = {"effort": "inherit", **args}

    async def go():
        async with Client(mcp) as client:
            return await answered(client, tool, args)

    asyncio.run(go())
    return _events(tmp_path)


def _reasoning_then_answer(clock):
    """A stream that reasons for a while and then answers, on the driven clock."""
    schedule = [
        (0.5, "data: " + json.dumps(
            {"model": "served-id-1", "choices": [{"index": 0, "delta": {"role": "assistant"}}]}
        ) + "\n\n"),
        _line(1.0, delta(reasoning="think")),
        _line(2.0, delta(reasoning="hard")),
        _line(4.5, delta(content="done")),
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
        _line(2.0, delta(content=" world")),
        _line(2.0, _usage(prompt_tokens=7, completion_tokens=3)),
        (2.0, "data: [DONE]\n\n"),
    ]
    return serves_metrics(paced(clock, schedule))


def test_a_delegation_that_reasoned_writes_a_numeric_reasoning_seconds(tmp_path):
    clock = Clock()
    events = _run(tmp_path, _reasoning_then_answer(clock), clock,
                  tool="delegate", args={"task": "explain the retry"})
    turns = [e for e in events if e["t"] == "turn"]
    assert turns, [e["t"] for e in events]
    assert turns[0]["reasoning_seconds"] == pytest.approx(3.5)


def test_a_delegation_that_did_not_reason_writes_null_reasoning_seconds(tmp_path):
    clock = Clock()
    events = _run(tmp_path, _answer_only(clock), clock,
                  tool="delegate_readonly", args={"task": "summarise"})
    turns = [e for e in events if e["t"] == "turn"]
    assert turns, [e["t"] for e in events]
    assert turns[0]["reasoning_seconds"] is None


# --- the retry pin ----------------------------------------------------------------------

class _ReasoningThenAnswers:
    """First attempt reasons and comes back empty at a length stop, the retry answers.

    The first attempt is the shape ADR-0014's cascade retries. Its `reasoning_seconds` is
    deliberately different from the retry's, so a turn-level figure that summed across
    attempts (as `decode_seconds` does) would fail the pin.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls = 0

    async def complete(self, request, *, on_token=None):
        self.calls += 1
        if self.calls == 1:
            self.clock.advance(10.0)
            return base.CanonicalResponse(
                content=(base.ThinkingBlock("thinking, then nothing"),),
                finish_reason="length",
                input_tokens=100, output_tokens=0, model="served-id-1",
                reasoning_seconds=3.0,
                decode_seconds=7.0, prefill_seconds=3.0,
            )
        self.clock.advance(10.0)
        return base.CanonicalResponse(
            content=(base.TextBlock("the answer"),),
            finish_reason="stop",
            input_tokens=100, output_tokens=5, model="served-id-1",
            reasoning_seconds=1.5,
            decode_seconds=7.0, prefill_seconds=3.0,
        )

    async def probe_cluster(self):
        return None


def test_a_retried_turn_reports_only_the_answering_attempts_reasoning(tmp_path):
    """The pin: measured per response, not across attempts, so a retried turn does not
    count the failed attempt's reasoning against itself."""
    clock = FakeClock()
    backend = _ReasoningThenAnswers(clock)
    started = clock()

    dispatch = asyncio.run(
        loop.dispatch_with_recovery(
            loop_cfg(), loop_entry(), backend,
            lambda level, budget: loop.build_one_shot_request(
                delegation=loop.Delegation("summarise this"), effort=level,
                max_tokens=budget, temperature=1.0, top_p=1.0,
            ),
            effort="high", deadline=None, clock=clock,
        )
    )
    assert backend.calls == 2, (
        f"the fixture made {backend.calls} attempt(s); the pin needs two, one reasoning "
        "attempt plus the retry that answers"
    )
    backend_ms = int((clock() - started) * 1000)

    watch = loop._Watch(diagnostics=True)
    watch.turn = 1
    watch.turn_cost(dispatch, evicted=0)

    path = tmp_path / "stream.jsonl"
    transcript.Stream(path).turn(
        watch.turns[-1], dispatch.response.text,
        ms=backend_ms, backend_ms=backend_ms, of_turns=1,
    )
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["reasoning_seconds"] == pytest.approx(1.5), (
        f"the turn reported {event['reasoning_seconds']}; reasoning is measured per "
        "response, so a retried turn must not count the failed attempt's reasoning"
    )
