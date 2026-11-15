"""The turn loop never told admission its size, so a run that grew to a 200k-token prompt
was still admitted as its opening estimate.

`Admission.grow` exists: a held lease's booking can be raised to what the run has actually
booked in the engine's KV pool, and it never waits or refuses, because the memory is already
held (ADR-0144). Nothing called it. A delegation whose first turn prefilled 50k and whose
second prefilled 200k kept a booking of its opening estimate -- the task text plus the first
turn's reply budget -- so every other process admitted against a figure the run had long
outgrown.

The loop now reports each completed turn's footprint -- the prompt the backend said it
booked plus the reply budget that turn asked for, which is the KV the engine reserves for
the whole request while it runs (ADR-0143). The server hands that to `Admission.grow` for
the lease it holds. A turn that does not say its prompt size reports nothing, and a callback
that raises must not fail the delegation: the booking is a best-effort gauge, not a
correctness condition.

Named after the bug, per the project's convention.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from test_server import (
    chat_reply,
    cfg as server_cfg,
    delegated,
    tool_call_reply,
    turn_handler,
)

from claude_delegate_local import loop, server, tools
from claude_delegate_local.backends.base import (
    CanonicalResponse,
    TextBlock,
    ToolSpec,
    ToolUseBlock,
)
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry


def cfg(**over) -> Config:
    # Overflow handling off: the scripted turns report a fixed prompt size whatever the
    # history holds, which an armed plateau check reads as history being dropped.
    kw = {"workspace_roots": (".",), "context_overflow_enabled": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def entry(**over) -> ModelEntry:
    kw = {"key": "flash", "base_url": "http://example.com:8000",
          "served_model_id": "served-id-1", "context_window_defaulted": True}
    kw.update(over)
    return ModelEntry(**kw)  # type: ignore[arg-type]


@pytest.fixture
def echo_tool():
    """One cacheable tool in the real registry, removed afterwards."""

    def handler(cfg_, args):
        return "echoed"

    added = {
        "echo": tools.RegisteredTool(
            spec=ToolSpec(name="echo", description="echo", input_schema={"type": "object"}),
            handler=handler,
            cacheable=True,
        ),
    }
    tools.REGISTRY.update(added)
    try:
        yield
    finally:
        tools.REGISTRY.pop("echo", None)


class ScriptedTurns:
    """One scripted reply per call, recording every request so a test can read the budget
    that turn actually asked for rather than guessing it."""

    def __init__(self, *replies: CanonicalResponse) -> None:
        self.replies = list(replies)
        self.requests: list = []

    async def complete(self, request, *, on_token=None):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("the loop called the backend more times than scripted")
        return self.replies.pop(0)

    async def probe_cluster(self):
        return None


def tool_call(input_tokens) -> CanonicalResponse:
    return CanonicalResponse(
        content=(ToolUseBlock(id="call-0", name="echo", input={}),),
        finish_reason="tool_calls",
        input_tokens=input_tokens, output_tokens=5, model="served-id-1",
    )


def answer(text: str, input_tokens) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=input_tokens, output_tokens=5, model="served-id-1",
    )


def run(backend, *, on_footprint=None, max_turns=2):
    return asyncio.run(loop.run_agentic_loop(
        cfg(),
        entry(),
        backend,
        loop.Delegation("do the thing"),
        allowed=frozenset({"echo"}),
        max_turns=max_turns,
        on_footprint=on_footprint,
    ))


# --- the loop reports what each turn booked ------------------------------------------------


def test_each_turn_reports_its_prompt_plus_the_budget_it_asked_for(echo_tool):
    """The red. Turn two's prompt is four times turn one's, and the booking stayed put."""
    seen: list[int] = []

    async def on_footprint(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(tool_call(50_000), answer("done", 200_000))
    result = run(backend, on_footprint=on_footprint)

    assert result.response.text == "done"
    assert result.turns == 2
    assert len(backend.requests) == 2, "one request per turn, so one footprint per turn"
    assert seen == [
        50_000 + backend.requests[0].max_tokens,
        200_000 + backend.requests[1].max_tokens,
    ], seen
    assert backend.requests[1].max_tokens < backend.requests[0].max_tokens, (
        "the later turn asked for a shrunk budget (ADR-0143), and that is the figure the "
        "engine books, so it must be the one reported"
    )


@pytest.mark.parametrize("reported", [None, 0])
def test_a_turn_that_does_not_say_its_prompt_size_reports_nothing(echo_tool, reported):
    """Absent is not zero: neither an unreported nor a reported-zero prompt is a booking."""
    seen: list[int] = []

    async def on_footprint(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(answer("done", reported))
    result = run(backend, on_footprint=on_footprint, max_turns=1)

    assert result.response.text == "done"
    assert seen == [], f"input_tokens={reported!r} produced a footprint: {seen}"


def test_a_raising_footprint_callback_does_not_fail_the_delegation(echo_tool):
    """The booking is a gauge, so a gate that cannot be told must not cost the answer."""
    async def on_footprint(tokens: int) -> None:
        raise RuntimeError("the shared file is unreachable")

    backend = ScriptedTurns(tool_call(50_000), answer("done", 200_000))
    result = run(backend, on_footprint=on_footprint)

    assert result.response.text == "done"
    assert result.turns == 2


# --- the server hands the lease to the loop -----------------------------------------------


GATES_BUILT: list = []


class RecordingGate(server.Admission):
    """The real gate, keeping the lease it granted and every instance built."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.opening: int | None = None
        GATES_BUILT.append(self)

    @asynccontextmanager
    async def admit(self, *args, **kwargs):
        async with super().admit(*args, **kwargs) as lease:
            self.opening = lease.tokens
            yield lease


def with_prompt_tokens(reply: dict, prompt_tokens: int) -> dict:
    """The same canned reply, reporting a prompt of this size."""
    return {**reply, "usage": {**reply["usage"], "prompt_tokens": prompt_tokens}}


def test_the_server_grows_the_lease_the_run_holds(tmp_path, monkeypatch) -> None:
    """The wiring. Loop and `grow` are tested apart, so a dropped hand-off between them
    passes both -- and a booking that stays at its opening estimate is the whole bug."""
    target = tmp_path / "note.py"
    target.write_text("# hello\n", encoding="utf-8")

    monkeypatch.setattr(server, "Admission", RecordingGate)
    config = server_cfg(workspace_roots=(str(tmp_path),), slots_dir=str(tmp_path))
    result = delegated(
        turn_handler(
            with_prompt_tokens(
                tool_call_reply("read_file", {"path": str(target)}), 50_000
            ),
            with_prompt_tokens(chat_reply(content="it says hello"), 200_000),
        ),
        config=config,
        task="what does the file say",
    )

    assert result["answer"] == "it says hello"
    assert result["turns"] == 2
    assert len(GATES_BUILT) == 1, GATES_BUILT
    gate = GATES_BUILT[0]
    assert gate.opening is not None, "the delegation never reached `admit`"
    peak = gate.status()["peak_inflight_tokens"]
    assert peak > gate.opening, (
        f"the booking never grew past its opening estimate of {gate.opening}: {peak}"
    )
    assert peak >= 200_000, (
        "the last turn's prompt alone was 200k, so the peak booking cannot be below it: "
        f"{peak}"
    )
