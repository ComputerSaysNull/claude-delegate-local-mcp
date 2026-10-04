"""The `ask_caller` tool: a delegation that can pause and ask whoever called it.

A model that cannot tell which of two readings its task means gets to ask its caller,
once per run, several questions in that one call. The answer comes back as that call's
tool result; a later call is told to proceed on its best reading.

Two properties have no visible symptom when broken, so both are tested directly. The wait
is wall-clock time inside a run bounded by one deadline, so the clocks freeze while it is
awaited, or an answer that takes an hour would end the run. And the tool is declared only
when the loop was given someone to ask: a model told it may ask when nobody can answer
spends a turn finding that out.
"""

from __future__ import annotations

import asyncio

from claude_delegate_local import loop, tools
from claude_delegate_local.backends.base import (
    CanonicalResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist


def cfg(**over) -> Config:
    # Overflow handling off, as in test_agentic_loop: the scripted turns report a fixed
    # prompt size whatever the history holds, which an armed plateau check reads as history
    # being dropped.
    kw = {"workspace_roots": (".",), "context_overflow_enabled": False}
    kw.update(over)
    # The deadlines have to nest, and these tests set absurdly small ceilings on purpose so
    # a fake clock is cheap. Follow the ceiling down unless the test names this itself, so
    # shrinking `dispatch_timeout` does not silently become a test of the stall deadline.
    if "dispatch_timeout" in kw and "stall_timeout" not in kw:
        kw["stall_timeout"] = kw["dispatch_timeout"]
    return Config(**kw)  # type: ignore[arg-type]


def entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": HOST, "served_model_id": "served-id-1",
          "context_window_defaulted": True}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


# --- scripted replies (the test_agentic_loop.py helpers) -----------------------------


def says(text: str) -> CanonicalResponse:
    """A final answer: text, no tool calls, so the loop stops here."""
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=10,
        output_tokens=5,
        model="served-id-1",
    )


def wants(*calls: tuple[str, dict], text: str = "") -> CanonicalResponse:
    """A turn that calls tools. Ids are positional so a test can name them."""
    blocks: list = [TextBlock(text)] if text else []
    blocks += [
        ToolUseBlock(id=f"call-{i}", name=name, input=args)
        for i, (name, args) in enumerate(calls)
    ]
    return CanonicalResponse(
        content=tuple(blocks),
        finish_reason="tool_calls",
        input_tokens=10,
        output_tokens=5,
        model="served-id-1",
    )


def results_in(request) -> list[ToolResultBlock]:
    """The tool results the loop sent back, from the last message of a request."""
    return [b for b in request.messages[-1].content if isinstance(b, ToolResultBlock)]


class ScriptedTurns:
    """Returns one scripted reply per call, and records every request it was given."""

    def __init__(self, *replies: CanonicalResponse) -> None:
        self.replies = list(replies)
        self.requests: list = []

    async def complete(self, request, *, on_token=None):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("the loop called the backend more times than scripted")
        return self.replies.pop(0)

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


def run(backend, *, ask=None, allowed=frozenset({"read_file"}), **over):
    """run_agentic_loop with the boring arguments filled in, `ask` threaded through."""
    kw = {"max_turns": 5}
    kw.update(over)
    return asyncio.run(
        loop.run_agentic_loop(
            kw.pop("cfg", cfg()),
            entry(),
            backend,
            loop.Delegation(kw.pop("task", "do the thing")),
            allowed=allowed,
            ask=ask,
            **kw,
        )
    )


# --- the one question that is asked ----------------------------------------------------


def test_a_question_reaches_the_caller_and_its_answer_reaches_the_model():
    """Turn 1 the model asks two things at once; the caller answers; turn 2 the model
    answers. The questions are delivered verbatim, and the answer comes back as the tool
    result of the call that asked -- so the model reads it in the same place it reads any
    other tool's output."""
    asked: list = []

    async def answer(questions):
        asked.append(list(questions))
        return "foo.py; yes"

    backend = ScriptedTurns(
        wants(("ask_caller", {"questions": ["Which file?", "Keep the old name?"]})),
        says("kept the old name"),
    )
    result = run(backend, ask=answer)
    assert asked == [["Which file?", "Keep the old name?"]]
    seen = results_in(backend.requests[1])
    assert len(seen) == 1
    assert "foo.py; yes" in seen[0].content
    assert result.response.text == "kept the old name"
    assert result.turns == 2


# --- declaration -----------------------------------------------------------------------


def test_ask_caller_is_declared_only_when_a_caller_can_answer():
    """With no `ask` the tool must not be offered: a model told it may ask, when nobody is
    there to answer, spends a turn learning that. With one, it is offered, and last in the
    list -- appended, never inserted, so the cached prefix of the existing tools survives."""
    backend = ScriptedTurns(says("done"))
    run(backend, ask=None)
    names = [s.name for s in backend.requests[0].tools]
    assert "ask_caller" not in names

    async def answer(questions):
        return "ok"

    backend = ScriptedTurns(says("done"))
    run(backend, ask=answer)
    names = [s.name for s in backend.requests[0].tools]
    assert names[-1] == tools.ASK_CALLER_SPEC.name
    assert backend.requests[0].tools[-1] == tools.ASK_CALLER_SPEC


# --- one question per run ----------------------------------------------------------------


def test_a_second_question_is_refused():
    """The caller is one person with one answer. A model that keeps asking is told to
    proceed on its best reading, and `ask` is invoked exactly once for the run."""
    called: list = []

    async def answer(questions):
        called.append(list(questions))
        return "first answer"

    backend = ScriptedTurns(
        wants(("ask_caller", {"questions": ["first?"]})),
        wants(("ask_caller", {"questions": ["second?"]})),
        says("done"),
    )
    result = run(backend, ask=answer)
    assert len(called) == 1
    assert called == [["first?"]]
    second = results_in(backend.requests[2])
    assert len(second) == 1
    assert "already" in second[0].content
    assert result.response.text == "done"
    assert result.turns == 3


# --- the wait is outside the clocks -----------------------------------------------------


def test_waiting_for_an_answer_does_not_count_against_the_deadline():
    """One deadline covers the whole delegation, and waiting on a human is not the
    delegation's work. The caller takes an hour; the run must still finish, because the
    dispatch deadline and the stall clock both freeze while the answer is awaited."""
    now = [0.0]

    def clock():
        return now[0]

    async def answer(questions):
        now[0] += 3600.0
        return "answered after an hour"

    backend = ScriptedTurns(
        wants(("ask_caller", {"questions": ["which?"]})),
        says("done"),
    )
    result = run(
        backend, ask=answer, clock=clock, cfg=cfg(dispatch_timeout=30),
    )
    assert result.response.text == "done"
    assert result.turns == 2
