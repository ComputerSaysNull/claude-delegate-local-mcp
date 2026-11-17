"""A tool call cut off at the reply limit killed the delegation instead of being handled.

When a reply hits `max_tokens` partway through a tool call, the engine hands back the
half-written call -- arguments like `{"path": "a.py", "content": "abc`, with no closing
quote. `_from_wire` parsed every tool call before it read `finish_reason`, so the JSON
error became a `BackendProtocolError` and the run died there. Protocol errors are never
retried, so the two mitigations written for exactly this reply -- `cut_by_the_shrink` and
the resend at the full budget, `RetryRecord(kind="LengthUnderShrunkBudget")` (ADR-0143) --
could never run.

The adapter now flags such a call (`ToolUseBlock.cut`, with an empty `input`), and the loop
resends the turn when the budget had been shrunk. Where there is no larger budget left --
turn 1 is already at the full one, or the resend was cut again -- the call is answered with
an error naming the limit that cut it instead of being run, so the tool is never told the
model asked for nothing, and the run goes on: the complete calls beside it still run, and
the model is told to write a large file in parts next turn.

Every test here goes through the real `OpenAICompatBackend` over a transport double, never
a scripted `CanonicalResponse`: the defect lives in the adapter's translation, and a
scripted backend hands the loop blocks that were already built, so it cannot see it. The
first reply that is cut is `finish_reason: "length"`; the other is the same cut reported as
`tool_calls`, which some engines do when the cut fell inside the call.

`registered` records every tool that ran, so it proves the cut call was not executed.
"""

import asyncio
import json

import httpx

from test_agentic_loop import cfg, entry, run
from test_agentic_loop import registered  # noqa: F401 -- a fixture, requested by name below
from wire_double import as_stream

from claude_delegate_local.backends import openai_compat as oc

# Today's budget at effort high, from `thinking_max_tokens_floor`, and the shrink turn 2 is
# asked for after a turn that wrote 1,000 tokens.
FULL = 131072
SHRUNK = 32768

# The wire's arguments for a call the reply limit landed in the middle of.
TRUNCATED = '{"path": "a.py", "content": "abc'


def wire_call(call_id: str, name: str, arguments: str) -> dict:
    """One tool call in the wire's shape, arguments unparsed as the wire sends them."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def calls_body(
    *calls: dict, finish: str, completion_tokens: int, text: str = "",
) -> dict:
    """One chat-completions reply carrying the given tool calls, in order."""
    return {
        "model": "served-id-1",
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": list(calls),
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": completion_tokens},
    }


def tool_body(
    *, finish: str, completion_tokens: int, arguments: str = TRUNCATED,
    name: str = "write_file", text: str = "",
) -> dict:
    """One chat-completions reply carrying a single tool call."""
    return calls_body(
        wire_call("call-w", name, arguments),
        finish=finish, completion_tokens=completion_tokens, text=text,
    )


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


def backend_over(wire: ScriptedWire, *, root=None):
    """The real adapter, wired to the double, with the config the loop is driven under.

    `root` is the workspace the file tools may write in; without it the config's own
    `(".")` root applies, which is what the tests that only call `echo` need.
    """
    config = cfg() if root is None else cfg(workspace_roots=(str(root),))
    model = entry()
    client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    return oc.OpenAICompatBackend(config, model, client=client), config, model, client


def tool_messages(request: dict) -> list[dict]:
    """The tool results the loop sent back, as the wire carries them."""
    return [m for m in request["messages"] if m.get("role") == "tool"]


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


def test_a_tool_calls_reply_cut_at_the_shrunk_budget_is_resent(registered):  # noqa: F811
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


def test_a_cut_call_at_the_full_budget_is_refused_and_the_run_goes_on(tmp_path):
    """Turn 1 is already at the full budget, so a call cut there has nothing to be resent
    at.

    It must not be run on an empty `input` -- the tool would be told the model asked for
    nothing -- and it must not end the run either. It is answered with an error naming the
    limit that cut it, so the model knows to write in parts, and turn 2 answers normally.
    """
    wire = ScriptedWire(
        tool_body(finish="length", completion_tokens=FULL, text="writing the file"),
        answer_body("done"),
    )
    backend, config, model, client = backend_over(wire, root=tmp_path)
    try:
        result = run(backend, allowed=frozenset({"write_file"}), effort="high",
                     max_turns=5, cfg=config, entry=model, diagnostics=True)
    finally:
        asyncio.run(client.aclose())

    assert len(wire.requests) == 2, "there is no larger budget, so there is no resend"
    assert result.turns == 2
    assert result.response.text == "done", "the run must go on past a cut call"

    turn = result.diagnostics[0]
    assert turn.turn == 1
    record = turn.tool_calls[0]
    assert (record.name, record.outcome) == ("write_file", "error"), (
        "the half-written call was executed, or left unanswered"
    )
    assert record.ms is None, "nothing ran, so there is nothing to time"
    assert "cut off at the reply limit" in record.message
    assert str(FULL) in record.message, "the ledger must name the limit that cut it"

    follow_up = tool_messages(wire.requests[1])
    assert [m["tool_call_id"] for m in follow_up] == ["call-w"], (
        "the cut call needs a result of its own, or the history is not valid JSON"
    )
    assert "cut off at the reply limit" in follow_up[0]["content"]
    assert str(FULL) in follow_up[0]["content"]
    assert not (tmp_path / "a.py").exists()


def test_a_complete_call_beside_a_cut_one_still_runs(tmp_path):
    """Only the call the limit cut through is refused; its sibling runs as usual."""
    (tmp_path / "notes.txt").write_text("one\ntwo\n")
    read_args = json.dumps({"path": str(tmp_path / "notes.txt")})
    cut_args = '{"path": "%s", "content": "abc' % (tmp_path / "big.py")
    wire = ScriptedWire(
        calls_body(
            wire_call("call-r", "read_file", read_args),
            wire_call("call-w", "write_file", cut_args),
            finish="length", completion_tokens=FULL, text="reading, then writing",
        ),
        answer_body("done"),
    )
    backend, config, model, client = backend_over(wire, root=tmp_path)
    try:
        result = run(backend, allowed=frozenset({"read_file", "write_file"}),
                     effort="high", max_turns=5, cfg=config, entry=model,
                     diagnostics=True)
    finally:
        asyncio.run(client.aclose())

    assert result.response.text == "done"
    records = result.diagnostics[0].tool_calls
    assert [(r.name, r.outcome) for r in records] == [
        ("read_file", "ran"), ("write_file", "error")
    ], "the complete call must still run, and only the cut one be refused"

    follow_up = tool_messages(wire.requests[1])
    assert [m["tool_call_id"] for m in follow_up] == ["call-r", "call-w"]
    assert "one" in follow_up[0]["content"], "the read did not run"
    assert "cut off at the reply limit" in follow_up[1]["content"]
    assert str(FULL) in follow_up[1]["content"]
    assert not (tmp_path / "big.py").exists()


# Effort low asks for `max_tokens`, which the ladder's stage 2 doubles -- raised to
# `thinking_max_tokens_floor` -- since that is the size a retried empty answer is sent at.
FIRST = 65536
RAISED = 131072


def test_the_refusal_names_the_budget_that_actually_cut_the_call(tmp_path):
    """Turn 1's first reply is the reasoning-exhaustion signature: empty text, a length stop
    and no tool calls, so `dispatch_with_recovery` resends it at the raised budget, and that
    resend is the reply the limit cut through.

    The loop tracked the budget it asked for in `sent_budget`, which the ladder's resend
    never updated, so the number named to the model was the first request's -- a budget the
    cut reply was not sent at. The limit that actually cut it travels on the block
    (`ToolUseBlock.cut_limit`), from the request the adapter was given.
    """
    wire = ScriptedWire(
        calls_body(finish="length", completion_tokens=5),
        tool_body(finish="length", completion_tokens=RAISED, text="writing the file"),
        answer_body("done"),
    )
    backend, config, model, client = backend_over(wire, root=tmp_path)
    try:
        result = run(backend, allowed=frozenset({"write_file"}), effort="low",
                     max_turns=5, cfg=config, entry=model, diagnostics=True)
    finally:
        asyncio.run(client.aclose())

    assert [r["max_tokens"] for r in wire.requests][:2] == [FIRST, RAISED], (
        "the empty reply must be retried at the raised budget, and that retry is the one "
        "the limit cut through"
    )
    first_limit = wire.requests[0]["max_tokens"]
    retry_limit = wire.requests[1]["max_tokens"]
    assert retry_limit > first_limit

    assert result.turns == 2
    assert result.response.text == "done", "the run must go on past a cut call"
    turn = result.diagnostics[0]
    record = turn.tool_calls[0]
    assert (record.name, record.outcome) == ("write_file", "error")
    assert str(retry_limit) in record.message, (
        "the ledger must name the budget the cut reply was sent at"
    )
    assert str(first_limit) not in record.message, (
        "it named the budget the ladder replaced, which cut nothing"
    )

    follow_up = tool_messages(wire.requests[2])
    assert [m["tool_call_id"] for m in follow_up] == ["call-w"]
    assert str(retry_limit) in follow_up[0]["content"]
    assert str(first_limit) not in follow_up[0]["content"]
    assert not (tmp_path / "a.py").exists()
