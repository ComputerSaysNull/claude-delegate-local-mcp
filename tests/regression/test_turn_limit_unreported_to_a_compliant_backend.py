"""`hit_turn_limit` was false in exactly the case it exists to report.

Found by the 2026-09-01 audit. The final turn is declared with no tools, so the model
cannot end on a call nobody will run. The flag was `turn == turns and
bool(dispatch.response.tool_uses)` -- but a backend offered no tools returns no tool calls,
so the conjunct was false whenever the backend behaved, and the flag could only become true
when one *ignored* the withdrawal. A delegation truncated by its turn budget therefore
reported `hit_turn_limit: false`, and a caller reading a partial answer had nothing to tell
it apart from a complete one. Three documents stated the intended meaning and all three
were wrong about the code.

The whole suite missed it because every scripted backend replied from a list without
looking at the request it was given, so none of them could honour the withdrawal that the
bug depended on. `CompliantBackend` below is the missing piece: it answers when it is
offered no tools, which is what a real backend does and what the loop's own short-circuit
assumes. `IgnoresTheWithdrawal` keeps the other direction covered, so the fix cannot
silently stop reporting the case the old flag did catch.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

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
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": HOST, "served_model_id": "served-id-1"}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


def _answer(text: str) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),), finish_reason="stop",
        input_tokens=10, output_tokens=5, model="served-id-1",
    )


def _tool_call() -> CanonicalResponse:
    return CanonicalResponse(
        content=(ToolUseBlock(id="call-0", name="echo", input={}),),
        finish_reason="tool_use", input_tokens=10, output_tokens=5, model="served-id-1",
    )


class CompliantBackend:
    """Calls a tool while it is offered one, and answers when it is not.

    This is the behaviour the final-turn short-circuit is designed around, and the
    behaviour no scripted backend in the suite had.
    """

    def __init__(self, calls: int = 99) -> None:
        self.requests: list = []
        self.calls = calls

    async def complete(self, request):
        self.requests.append(request)
        if not request.tools or len(self.requests) > self.calls:
            return _answer("done")
        return _tool_call()


class IgnoresTheWithdrawal:
    """Returns a tool call on every turn, including one offered no tools."""

    def __init__(self) -> None:
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return _tool_call()


@pytest.fixture
def registered():
    def echo(cfg_, args):
        return "echoed"

    tools.REGISTRY["echo"] = tools.RegisteredTool(
        spec=ToolSpec(name="echo", description="echo", input_schema={"type": "object"}),
        handler=echo,
        cacheable=False,
    )
    try:
        yield
    finally:
        tools.REGISTRY.pop("echo", None)


def run(backend, **over):
    kw = {"max_turns": 3}
    kw.update(over)
    return asyncio.run(
        loop.run_agentic_loop(
            cfg(), entry(), backend, loop.Delegation("do the thing"),
            allowed=frozenset({"echo"}), **kw,
        )
    )


def test_a_compliant_backend_that_exhausts_the_budget_reports_the_limit(registered):
    """The bug. Before the fix this was false, and the caller could not tell it was cut off."""
    backend = CompliantBackend()
    result = run(backend, max_turns=3)
    assert backend.requests[-1].tools == (), "the last turn must withdraw the tools"
    assert result.turns == 3
    assert result.response.tool_uses == (), "a compliant backend answers when offered none"
    assert result.hit_turn_limit is True


def test_an_answer_before_the_last_turn_does_not_report_the_limit(registered):
    """The other direction. A flag hardwired true would pass the test above."""
    backend = CompliantBackend()
    result = run(backend, max_turns=1)
    # One turn, and it is the final one, so tools are withdrawn from the start.
    assert result.hit_turn_limit is True
    # With room to spare the loop stops on the answer, well short of the budget.
    result = run(CompliantBackend(calls=1), max_turns=8)
    assert result.turns == 2, "one tool call, then a freely given answer"
    assert result.hit_turn_limit is False


def test_a_backend_ignoring_the_withdrawal_still_reports_the_limit(registered):
    """The case the old flag did catch must not be lost to the fix."""
    result = run(IgnoresTheWithdrawal(), max_turns=2)
    assert result.turns == 2
    assert result.hit_turn_limit is True
