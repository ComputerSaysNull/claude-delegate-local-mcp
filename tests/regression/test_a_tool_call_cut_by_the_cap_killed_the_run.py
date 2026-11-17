"""A tool call cut off at the reply limit killed the delegation instead of being resent.

When a reply hits `max_tokens` partway through a tool call, the engine hands back the
half-written call -- arguments like `{"path": "a.py", "content": "abc`, with no closing
quote. `_from_wire` parsed every tool call before it read `finish_reason`, so the JSON
error became a `BackendProtocolError` and the run died there. Protocol errors are never
retried, so the two mitigations written for exactly this reply -- `cut_by_the_shrink` and
the resend at the full budget, `RetryRecord(kind="LengthUnderShrunkBudget")` (ADR-0143) --
could never run.

Both tests below go through the real `OpenAICompatBackend` over a transport double, never
a scripted `CanonicalResponse`: the defect lives in the adapter's translation, and a
scripted backend hands the loop blocks that were already built, so it cannot see it. The
first reply that is cut is `finish_reason: "length"`; the second is the same cut reported
as `tool_calls`, which some engines do when the cut fell inside the call.

The cut call must never be executed: `registered` records every tool that ran, and it holds
only the first turn's call. The third test is the other side -- a cut on turn 1, which is
already at the full budget, so there is nothing to resend it at, and it is refused rather
than run on an empty `input`.
"""

import asyncio
import json

import httpx
import pytest

from test_agentic_loop import cfg, entry, run
from test_agentic_loop import registered  # noqa: F401 -- a fixture, requested by name below
from wire_double import as_stream

from claude_delegate_local.backends import openai_compat as oc
from claude_delegate_local.backends.base import BackendProtocolError

# Today's budget at effort high, from `thinking_max_tokens_floor`, and the shrink turn 2 is
# asked for after a turn that wrote 1,000 tokens.
FULL = 131072
SHRUNK = 32768

# The wire's arguments for a call the reply limit landed in the middle of.
TRUNCATED = '{"path": "a.py", "content": "abc'


def tool_body(
    *, finish: str, completion_tokens: int, arguments: str = TRUNCATED,
    name: str = "write_file", text: str = "",
) -> dict:
    """One chat-completions reply carrying a single tool call."""
    return {
        "model": "served-id-1",
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": "call-w",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": completion_tokens},
    }


def answer_body(text: str, completion_tokens: int = 5) -> dict:
    return {
        "model": "served-id-1",
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": completion_tokens},
    }


class ScriptedWire:
    """One streamed reply per chat call, recording every request body it was given."""

    def __init__(self, *bodies: dict) -> None:
        self.bodies = list(bodies)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            # No metrics surface, which `probe_cluster` reports as a confirmed absence.
            return httpx.Response(404, text="Not Found")
        assert request.url.path == "/v1/chat/completions", request.url.path
        self.requests.append(json.loads(request.content))
        if not self.bodies:
            raise AssertionError("the loop called the backend more times than scripted")
        return as_stream(self.bodies.pop(0))


def backend_over(wire: ScriptedWire):
    """The real adapter, wired to the double, with the config the loop is driven under."""
    config, model = cfg(), entry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    return oc.OpenAICompatBackend(config, model, client=client), config, model, client


def test_a_length_stop_inside_a_tool_call_is_resent_at_the_full_budget(registered):  # noqa: F811
    """Turn 2 runs under the shrunk later-turn budget and its first reply is cut mid-call.

    Today the adapter raises "arguments that are not JSON" before it has read the
    `finish_reason` that says the reply was cut, so the delegation dies on turn 2 and the
    resend at the full budget never happens.
    """
    wire = ScriptedWire(
        tool_body(finish="tool_calls", completion_tokens=1000, name="echo",
                  arguments='{"what": "a"}'),
        tool_body(finish="length", completion_tokens=SHRUNK, text="writing the file"),
        answer_body("done"),
    )
    backend, config, model, client = backend_over(wire)
    try:
        result = run(backend, effort="high", max_turns=5, cfg=config, entry=model,
                     diagnostics=True)
    finally:
        asyncio.run(client.aclose())

    assert [r["max_tokens"] for r in wire.requests] == [FULL, SHRUNK, FULL], (
        "turn 2 must be asked for the shrunk budget and the retry sent at the full one"
    )
    assert result.turns == 2
    assert result.response.text == "done", "the answer is the resend's"
    final = result.diagnostics[-1]
    assert final.turn == 2
    assert final.attempts == 2, "the resend is an attempt of the turn it belongs to"
    assert [r.kind for r in final.retries] == ["LengthUnderShrunkBudget"]
    assert registered == [("echo", {"what": "a"})], (
        "the half-written call was executed instead of being resent"
    )


def test_a_cut_call_with_no_larger_budget_to_resend_at_is_refused(registered):  # noqa: F811
    """The other side of the fix, and what keeps today's outcome for it.

    Turn 1 already asks for the full budget, so a call cut there has nothing to be resent
    at. It must not be run on an empty `input` -- the tool would be told the model asked
    for nothing -- and it must say why it stopped, naming the limit that cut it.
    """
    wire = ScriptedWire(tool_body(finish="length", completion_tokens=FULL,
                                  text="writing the file"))
    backend, config, model, client = backend_over(wire)
    try:
        with pytest.raises(BackendProtocolError, match=f"reply limit of {FULL} tokens"):
            run(backend, effort="high", max_turns=5, cfg=config, entry=model)
    finally:
        asyncio.run(client.aclose())

    assert len(wire.requests) == 1, "there is no larger budget, so there is no resend"
    assert registered == [], "the half-written call was executed"

    """Some engines answer `tool_calls` for a call the reply limit cut through.

    `completion_tokens` equal to the budget the turn was sent at is the other signature of
    a cut reply, and the shrink must recognise it or this turn dies where the one above
    was rescued.
    """
    wire = ScriptedWire(
        tool_body(finish="tool_calls", completion_tokens=1000, name="echo",
                  arguments='{"what": "a"}'),
        tool_body(finish="tool_calls", completion_tokens=SHRUNK, text="writing the file"),
        answer_body("done"),
    )
    backend, config, model, client = backend_over(wire)
    try:
        result = run(backend, effort="high", max_turns=5, cfg=config, entry=model,
                     diagnostics=True)
    finally:
        asyncio.run(client.aclose())

    assert [r["max_tokens"] for r in wire.requests] == [FULL, SHRUNK, FULL]
    assert result.response.text == "done"
    final = result.diagnostics[-1]
    assert final.turn == 2
    assert [r.kind for r in final.retries] == ["LengthUnderShrunkBudget"]
    assert registered == [("echo", {"what": "a"})]


def test_a_reply_that_is_only_a_cut_call_is_resent_rather_than_retried_empty(registered):  # noqa: F811
    """Turn 2 under the shrunk budget answers with no text and one cut call.

    `is_empty_at_length` reads empty text plus a length stop as reasoning exhaustion, so
    the reply entered that ladder, which retried at `max(2*budget, floor)` -- possibly
    short of the full budget -- and recorded `EmptyAtLength`, which is exactly what makes
    `cut_by_the_shrink` decline the resend. A cut call is content, so the turn must skip
    the ladder and go out again at the full budget.
    """
    wire = ScriptedWire(
        tool_body(finish="tool_calls", completion_tokens=1000, name="echo",
                  arguments='{"what": "a"}'),
        tool_body(finish="length", completion_tokens=SHRUNK, name="echo", text=""),
        answer_body("done"),
    )
    backend, config, model, client = backend_over(wire)
    try:
        result = run(backend, effort="high", max_turns=5, cfg=config, entry=model,
                     diagnostics=True)
    finally:
        asyncio.run(client.aclose())

    assert [r["max_tokens"] for r in wire.requests] == [FULL, SHRUNK, FULL], (
        "the cut turn must be resent at the full budget, not retried at a raised one"
    )
    final = result.diagnostics[-1]
    assert final.turn == 2
    assert [r.kind for r in final.retries] == ["LengthUnderShrunkBudget"], (
        "EmptyAtLength is what stops the resend at the full budget"
    )
    assert result.response.text == "done"
    assert registered == [("echo", {"what": "a"})], (
        "the half-written call was executed instead of being resent"
    )
