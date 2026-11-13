"""The final turn's `tool_choice: "none"` makes the engine throw the prefix away.

Measured on the live engine: a request carrying `tool_choice: "none"` drops the tool block
*and* every earlier assistant turn's reasoning from the prompt. The prompt collapsed from
3,054 tokens to 99, so the prefix cache missed on the one turn that must also fit a whole
answer inside a single deadline (ADR-0055, ADR-0057).

Worse than the prefill bill: `_OverflowGuard.observe` reads the reported `input_tokens`,
sees a prompt that did not grow while this server appended a tool result and evicted
nothing, and stops the delegation with "Something upstream is dropping history silently"
-- on a run where nothing was dropped at all. So the final turn must offer the tools with
`tool_choice: "auto"`, and a final turn that still calls one is answered by exactly one
more request with `"none"`, exempt from that plateau check because the shrink it causes is
the engine's documented behaviour rather than lost history.

Each test below fails today against the unfixed loop, except the last, which is the
negative control proving the plateau check still fires on a mid-run turn.
"""

import asyncio

import pytest

from claude_delegate_local import loop, tools
from claude_delegate_local.backends.base import (
    CanonicalResponse,
    TextBlock,
    ToolSpec,
    ToolUseBlock,
)
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist


def cfg(**over) -> Config:
    """Overflow handling on: these tests are about what it does with the reported prompt."""
    kw = {"workspace_roots": (".",), "context_overflow_enabled": True}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def entry(**over) -> ModelEntry:
    """A window big enough that the abort threshold is never what fires here."""
    kw = {"key": "flash", "base_url": HOST, "served_model_id": "served-id-1",
          "context_window": 1_000_000}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


# --- scripted replies ---------------------------------------------------------------------


def says(text: str) -> tuple:
    """An answer: text and no tool calls."""
    return (TextBlock(text),)


def wants(name: str, args: dict) -> tuple:
    """One tool call, with no text beside it, so the reply says nothing itself."""
    return (ToolUseBlock(id="call-0", name=name, input=args),)


class Engine:
    """A scripted backend that also reports what the prompt cost.

    The live engine's `input_tokens` is the only thing the overflow guard can see, so the
    two failures in this file have to be reproduced as a number: the engine shrinking the
    prompt on a `tool_choice: "none"` request, and a backend that drops history on an
    ordinary one. `reported` is the policy, kept apart from the replies so a test can say
    which of the two it is testing.
    """

    def __init__(self, replies, reported) -> None:
        self._replies = list(replies)
        self._reported = reported
        self.requests: list = []
        self.reported_tokens: list[int] = []

    async def complete(self, request, *, on_token=None):
        self.requests.append(request)
        if not self._replies:
            raise AssertionError("the loop called the backend more times than scripted")
        content = self._replies.pop(0)
        tokens = self._reported(request, len(self.requests), self.reported_tokens)
        self.reported_tokens.append(tokens)
        return CanonicalResponse(
            content=content,
            finish_reason="tool_calls" if any(
                isinstance(b, ToolUseBlock) for b in content
            ) else "stop",
            input_tokens=tokens,
            output_tokens=5,
            model="served-id-1",
        )

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


def growing(request, n: int, seen: list[int]) -> int:
    """What a healthy turn reports: the prompt grows by what was appended."""
    return 10_000 + 2_000 * n


def shrinks_on_forbidden(request, n: int, seen: list[int]) -> int:
    """The measured engine: `tool_choice: "none"` drops the tool block and the reasoning.

    3,054 -> 99 on the live cluster; the magnitude does not matter, only that it is smaller
    than the turn before it, which is what the plateau check reads.
    """
    before = seen[-1] if seen else 10_000
    return before - 5_000 if request.tool_choice == "none" else before + 2_000


def flat(request, n: int, seen: list[int]) -> int:
    """A backend that silently drops what was appended: the prompt never moves."""
    return 10_000


# --- a tool whose executions are observable ------------------------------------------------


@pytest.fixture
def recorded():
    """One cacheable tool, in the real registry, whose handler records each call.

    The result is deliberately long: the plateau check needs more than
    `overflow_min_growth_tokens` (256) of estimated history to be evidence of anything.
    """
    ran: list[dict] = []

    def note(cfg_, args):
        ran.append(dict(args))
        return "the result " + "x" * 4000

    added = {
        "note": tools.RegisteredTool(
            spec=ToolSpec(name="note", description="note", input_schema={"type": "object"}),
            handler=note,
            cacheable=True,
        )
    }
    tools.REGISTRY.update(added)
    try:
        yield ran
    finally:
        for name in added:
            tools.REGISTRY.pop(name, None)


def run(backend, *, max_turns: int, diagnostics: bool = False, **over):
    return asyncio.run(
        loop.run_agentic_loop(
            cfg(**over),
            entry(),
            backend,
            loop.Delegation("do the thing"),
            allowed=frozenset({"note"}),
            max_turns=max_turns,
            diagnostics=diagnostics,
        )
    )


# --- the final turn -----------------------------------------------------------------------


def test_the_final_turn_offers_the_tools_and_allows_a_call(recorded):
    """It sends `tool_choice: "none"` today, which is what makes the engine drop the tool
    block and every earlier turn's reasoning -- the shrink the next test measures."""
    backend = Engine([wants("note", {"what": "first"}), says("the answer")], growing)
    result = run(backend, max_turns=2)

    assert len(backend.requests) == 2, "the final turn answered, so nothing more was sent"
    final = backend.requests[1]
    assert final.tool_choice == "auto", (
        "the final turn must allow a call: `none` makes the engine drop the tool block and "
        "the earlier reasoning from the prompt"
    )
    assert final.tools == backend.requests[0].tools, (
        "and the same tools, or the prompt's front diverges and the cache misses anyway"
    )
    assert result.response.text == "the answer"


def test_a_final_turn_that_shrinks_the_prompt_is_not_read_as_dropped_history(recorded):
    """Today the final request is `none`, the engine reports a smaller prompt, and
    `_OverflowGuard.observe` stops the run with "dropping history silently" although this
    server evicted nothing and dropped nothing."""
    backend = Engine([wants("note", {"what": "first"}), says("the answer")],
                     shrinks_on_forbidden)
    result = run(backend, max_turns=2)

    assert result.response.text == "the answer"
    assert result.turns == 2
    assert backend.requests[-1].tool_choice == "auto"


def test_a_final_turn_that_still_calls_a_tool_gets_one_exempt_fallback_request(recorded):
    """A final turn under `auto` can still call a tool, and its result would never be read
    because the loop ends there. So exactly one more request goes out with `none` to get a
    text answer -- and the shrink that request causes must not stop the run."""
    backend = Engine(
        [wants("note", {"what": "first"}),
         wants("note", {"what": "second"}),
         says("the answer")],
        shrinks_on_forbidden,
    )
    result = run(backend, max_turns=2)

    assert recorded == [{"what": "first"}], (
        "the final turn's tool call must not be executed: nothing would read its result"
    )
    assert len(backend.requests) == 3, "exactly one fallback request, and no more"
    assert backend.requests[1].tool_choice == "auto"
    assert backend.requests[2].tool_choice == "none", (
        "the fallback is the request that forbids calls, so it can only answer"
    )
    assert backend.requests[2].tools == backend.requests[1].tools
    assert result.response.text == "the answer", (
        "the answer is the fallback's text, not the empty reply that called a tool"
    )


def test_the_fallback_is_recorded_as_the_turn_s_extra_attempt(recorded):
    """The turn now takes two attempts, and `attempts` alone cannot say why the second one
    happened: the record has to name the call that was never run."""
    backend = Engine(
        [wants("note", {"what": "first"}),
         wants("note", {"what": "second"}),
         says("the answer")],
        growing,
    )
    result = run(backend, max_turns=2, diagnostics=True)

    final = result.diagnostics[-1]
    assert final.turn == 2
    assert final.attempts == 2, "the fallback is the final turn's second attempt"
    assert [r.kind for r in final.retries] == ["FinalTurnToolCall"]
    assert final.retries[0].status is None
    assert final.retries[0].wait == 0.0
    assert final.retries[0].seconds >= 0.0
    assert result.response.text == "the answer"


def test_a_mid_run_plateau_still_stops_the_run(recorded):
    """The negative control, which must pass today and after the fix.

    A turn that is neither the final one nor the fallback, reporting a prompt that did not
    grow while more than `overflow_min_growth_tokens` was appended and nothing evicted, is
    still evidence that something upstream dropped history -- and still stops the run. If
    the fix exempted more than the fallback, this would go quiet.
    """
    backend = Engine([wants("note", {"what": "first"}), says("the answer")], flat)

    with pytest.raises(loop.ContextOverflowAborted) as caught:
        run(backend, max_turns=4)

    assert "dropping history silently" in str(caught.value)
    assert caught.value.report["stopped_on_turn"] == 2
    assert backend.requests[1].tool_choice == "auto", (
        "the control is about an ordinary mid-run turn, not a forbidden one"
    )
