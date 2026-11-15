"""A prompt the engine rebuilt left no trace in `backend_status`.

`backend_status`'s `preemptions` counts requests pushed out while running. A run between
turns has no running request, so when the engine drops its kept prompt state under load
the next turn silently re-prefills everything, and nothing reports it: one evening of 31
turns at 200-365k re-prefilled 8.65M tokens while `preemptions` read 0. The runs that hit
it came from another session's server process, so a per-process count alone would still
have shown 0.

So a turn is reported as a rebuild when its prompt grew since the previous turn (the
history was append-only, so nothing of ours removed a prefix), its cached prompt is under
half of what the previous turn sent, and this server evicted no tool results in between --
an eviction rewrites the prefix, so a miss there is ours rather than the engine's. The
count is kept machine-wide, at the shared file's top level, since the process that hit it
may already have exited.

Named after the bug, per the project's convention.
"""

import asyncio
import json
from pathlib import Path

import pytest

from test_server import (
    chat_reply,
    cfg as server_cfg,
    delegated,
    tool_call_reply,
    turn_handler,
)

from claude_delegate_local import loop, server, tools
from claude_delegate_local.admission import Admission
from claude_delegate_local.backends.base import (
    CanonicalResponse,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry
from claude_delegate_local.slots import SharedSlots, cross_process_status


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
    """One scripted reply per call, recording every request the loop built."""

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


def tool_call(input_tokens, cached_tokens=None) -> CanonicalResponse:
    return CanonicalResponse(
        content=(ToolUseBlock(id="call-0", name="echo", input={}),),
        finish_reason="tool_calls",
        input_tokens=input_tokens, output_tokens=5, model="served-id-1",
        cached_tokens=cached_tokens,
    )


def answer(text: str, input_tokens, cached_tokens=None) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=input_tokens, output_tokens=5, model="served-id-1",
        cached_tokens=cached_tokens,
    )


def run(backend, *, on_rebuild=None, max_turns=2, **cfg_over):
    return asyncio.run(loop.run_agentic_loop(
        cfg(**cfg_over),
        entry(),
        backend,
        loop.Delegation("do the thing"),
        allowed=frozenset({"echo"}),
        max_turns=max_turns,
        on_rebuild=on_rebuild,
    ))


def stubbed(request) -> bool:
    """Whether this request carried a tool result the server had already dropped."""
    return any(
        block.content == loop.EVICTED_STUB
        for message in request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )


# --- the loop reports a prompt the engine had to prefill again ---------------------------


def test_a_grown_prompt_that_prefilled_again_reports_the_rebuild(echo_tool):
    """The red. Turn two's prompt grew, and it came back with nothing cached."""
    seen: list[int] = []

    async def on_rebuild(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(
        tool_call(200_000, cached_tokens=190_000),
        answer("done", 210_000, cached_tokens=0),
    )
    result = run(backend, on_rebuild=on_rebuild)

    assert result.response.text == "done"
    assert seen == [210_000], (
        f"a 210,000-token prompt that prefilled with nothing cached reported {seen}"
    )


def test_an_eviction_between_turns_is_not_the_engine_rebuilding(echo_tool):
    """The negative control: our own eviction rewrites the prefix, so the miss is ours."""
    seen: list[int] = []

    async def on_rebuild(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(
        tool_call(200_000, cached_tokens=190_000),
        answer("done", 210_000, cached_tokens=0),
    )
    result = run(
        backend, on_rebuild=on_rebuild,
        keep_tool_results=0, overflow_tightened_keep_tool_results=0,
        retained_tool_result_tokens=0,
    )

    assert result.response.text == "done"
    assert stubbed(backend.requests[1]), (
        "no tool result was stubbed, so this test is not about eviction"
    )
    assert seen == [], f"an eviction was reported as an engine rebuild: {seen}"


def test_a_turn_whose_prompt_shrank_is_not_a_rebuild(echo_tool):
    """A smaller prompt means the prefix changed on our side, or the turn is a fallback."""
    seen: list[int] = []

    async def on_rebuild(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(
        tool_call(200_000, cached_tokens=190_000),
        answer("done", 190_000, cached_tokens=0),
    )
    run(backend, on_rebuild=on_rebuild)

    assert seen == [], f"a shrunken prompt was reported as a rebuild: {seen}"


def test_a_turn_that_hit_the_cache_is_not_a_rebuild(echo_tool):
    """Most of the previous prompt still cached: nothing was prefilled again."""
    seen: list[int] = []

    async def on_rebuild(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(
        tool_call(200_000, cached_tokens=190_000),
        answer("done", 210_000, cached_tokens=205_000),
    )
    run(backend, on_rebuild=on_rebuild)

    assert seen == [], f"a cache hit was reported as a rebuild: {seen}"


def test_the_first_turn_is_never_a_rebuild(echo_tool):
    """There is no previous turn to have cached anything."""
    seen: list[int] = []

    async def on_rebuild(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(answer("done", 200_000, cached_tokens=0))
    run(backend, on_rebuild=on_rebuild, max_turns=1)

    assert seen == [], f"the first turn was reported as a rebuild: {seen}"


@pytest.mark.parametrize("reported", [None])
def test_a_turn_that_does_not_say_its_cached_tokens_reports_nothing(echo_tool, reported):
    """Absent is not zero: a backend that says nothing about the cache says nothing."""
    seen: list[int] = []

    async def on_rebuild(tokens: int) -> None:
        seen.append(tokens)

    backend = ScriptedTurns(
        tool_call(200_000, cached_tokens=190_000),
        answer("done", 210_000, cached_tokens=reported),
    )
    run(backend, on_rebuild=on_rebuild)

    assert seen == [], f"cached_tokens={reported!r} produced a rebuild: {seen}"


def test_a_raising_rebuild_callback_does_not_fail_the_delegation(echo_tool):
    """A gauge, so a gate that cannot be told must not cost the answer."""
    async def on_rebuild(tokens: int) -> None:
        raise RuntimeError("the shared file is unreachable")

    backend = ScriptedTurns(
        tool_call(200_000, cached_tokens=190_000),
        answer("done", 210_000, cached_tokens=0),
    )
    result = run(backend, on_rebuild=on_rebuild)

    assert result.response.text == "done"
    assert result.turns == 2


# --- the server hands the count to the gate ---------------------------------------------


GATES_BUILT: list = []


class RecordingGate(server.Admission):
    """The real gate, keeping every instance built."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        GATES_BUILT.append(self)


def with_usage(reply: dict, prompt_tokens: int, cached_tokens: int | None) -> dict:
    """The same canned reply, reporting a prompt of this size and this much cached."""
    details = {} if cached_tokens is None else {"cached_tokens": cached_tokens}
    return {
        **reply,
        "usage": {
            **reply["usage"],
            "prompt_tokens": prompt_tokens,
            "prompt_tokens_details": details,
        },
    }


def test_the_server_counts_a_rebuild_this_process_prefilled(tmp_path, monkeypatch) -> None:
    """The wiring. Loop and `note_rebuild` are tested apart, so a dropped hand-off between
    them passes both -- and a rebuilt prompt nobody counted is the whole bug."""
    target = tmp_path / "note.py"
    target.write_text("# hello\n", encoding="utf-8")

    denylist = tmp_path / "secret_globs.txt"
    denylist.write_text("*.secret\n", encoding="utf-8")

    monkeypatch.setattr(server, "Admission", RecordingGate)
    config = server_cfg(
        workspace_roots=(str(tmp_path),), slots_dir=str(tmp_path),
        # Beside the workspace, so the run does not need the checkout's own list.
        secret_globs_file=str(denylist),
    )
    result = delegated(
        turn_handler(
            with_usage(
                tool_call_reply("read_file", {"path": str(target)}), 200_000, 190_000
            ),
            with_usage(chat_reply(content="it says hello"), 210_000, 0),
        ),
        config=config,
        task="what does the file say",
    )

    assert result["answer"] == "it says hello"
    assert len(GATES_BUILT) == 1, GATES_BUILT
    gate = GATES_BUILT[0]
    status = gate.status()
    assert status["cache_rebuilds"] == 1, (
        f"the rebuilt prompt was not counted: {status['cache_rebuilds']} rebuilds"
    )
    assert status["cache_rebuild_tokens"] == 210_000, status["cache_rebuild_tokens"]


# --- the gate keeps the count, here and machine-wide --------------------------------------


def slots_at(path: Path) -> SharedSlots:
    slots = SharedSlots(path)
    slots.prepare()
    return slots


def gate(**over) -> Admission:
    return Admission(cfg(**over))


async def test_note_rebuild_counts_the_rebuild_and_what_it_re_prefilled():
    g = gate()
    await g.note_rebuild(210_000)
    await g.note_rebuild(90_000)

    status = g.status()
    assert status["cache_rebuilds"] == 2, status["cache_rebuilds"]
    assert status["cache_rebuild_tokens"] == 300_000, status["cache_rebuild_tokens"]


async def test_another_process_gate_reads_the_machine_wide_pair(tmp_path):
    """The bug's own shape: the run that rebuilt came from another session's server."""
    path = tmp_path / "shared.json"
    first = Admission(cfg(), slots_at(path))
    await first.note_rebuild(210_000)

    second_slots = slots_at(path)
    view = await cross_process_status(second_slots, "")

    assert view["active"] is True, view
    assert view["cache_rebuilds"] == 1, view.get("cache_rebuilds")
    assert view["cache_rebuild_tokens"] == 210_000, view.get("cache_rebuild_tokens")


async def test_a_file_written_without_the_pair_reads_as_zero(tmp_path):
    """An older server's file has no counters, and zero is the honest answer for it."""
    path = tmp_path / "shared.json"
    path.write_text(
        json.dumps({"version": 1, "records": {}, "next_ticket": 0}), encoding="utf-8"
    )
    slots = slots_at(path)

    view = await cross_process_status(slots, "")
    assert view["cache_rebuilds"] == 0, view.get("cache_rebuilds")
    assert view["cache_rebuild_tokens"] == 0, view.get("cache_rebuild_tokens")

    await Admission(cfg(), slots).note_rebuild(1_000)
    view = await cross_process_status(slots, "")
    assert view["cache_rebuilds"] == 1, "the pair was not written into an older file"


async def test_the_pair_survives_another_gates_acquire_and_release(tmp_path):
    """Every other write path rewrites the document; the pair must ride along."""
    path = tmp_path / "shared.json"
    counter = Admission(cfg(), slots_at(path))
    await counter.note_rebuild(50_000)

    worker = Admission(cfg(), slots_at(path))
    held = await worker.acquire(1_000, entry_key="flash", entry_limit=5)
    await worker.release(held)

    view = await cross_process_status(slots_at(path), "")
    assert view["cache_rebuilds"] == 1, (
        f"a write path dropped the machine-wide count: {view.get('cache_rebuilds')}"
    )
    assert view["cache_rebuild_tokens"] == 50_000, view.get("cache_rebuild_tokens")
