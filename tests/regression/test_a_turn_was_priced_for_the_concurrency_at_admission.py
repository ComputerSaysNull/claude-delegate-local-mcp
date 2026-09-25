"""The priced label was frozen at admission, so a long delegation that started alone and
is now one of six was still priced, and its rate samples still filed, as solo.

`expected_concurrency` is computed once, when the admission lease is granted, and threaded
unchanged through the turn loop: `seed_decode_rate`, every `on_priced` row and every
`rate_history.observe` all read the same frozen number. The turn loop now re-reads it from
the shared totals at every turn after the first, so a delegation that was solo at admission
and is six-wide by its second turn is priced, labelled and filed at six-wide from then on.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local import loop, server, tools
from claude_delegate_local.backends.base import (
    CanonicalResponse,
    TextBlock,
    ToolSpec,
    ToolUseBlock,
)
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry
from claude_delegate_local.slots import SlotsUnavailable


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    # The deadlines have to nest, so a test that shrinks one must shrink the other.
    if "dispatch_timeout" in kw and "stall_timeout" not in kw:
        kw["stall_timeout"] = kw["dispatch_timeout"]
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
    """One scripted reply per call, so the test controls how many turns the loop runs."""

    def __init__(self, *replies: CanonicalResponse) -> None:
        self.replies = list(replies)

    async def complete(self, request, *, on_token=None):
        if not self.replies:
            raise AssertionError("the loop called the backend more times than scripted")
        return self.replies.pop(0)

    async def probe_cluster(self):
        return None


def tool_call() -> CanonicalResponse:
    return CanonicalResponse(
        content=(ToolUseBlock(id="call-0", name="echo", input={}),),
        finish_reason="tool_calls",
        input_tokens=10, output_tokens=5, model="served-id-1",
    )


def answer(text: str) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=10, output_tokens=5, model="served-id-1",
    )


def big_answer() -> CanonicalResponse:
    """Measurable by `RateHistory.observe`: past both floors, over a known interval."""
    return CanonicalResponse(
        content=(TextBlock("an answer"),),
        finish_reason="stop",
        input_tokens=100, output_tokens=2_000, model="served-id-1",
        decode_seconds=100.0,
    )


def run(backend, *, concurrency_now=None, expected=1, max_turns=2,  # noqa: PLR0913 -- test seams
        config=None, rate_history=None) -> tuple[object, list[dict]]:
    """One agentic loop, capturing every `on_priced` row."""
    rows: list[dict] = []

    async def capture(row: dict) -> None:
        rows.append(row)

    result = asyncio.run(loop.run_agentic_loop(
        config or cfg(),
        entry(),
        backend,
        loop.Delegation("do the thing"),
        allowed=frozenset({"echo"}),
        max_turns=max_turns,
        on_priced=capture,
        rate_history=rate_history,
        expected_concurrency=expected,
        concurrency_now=concurrency_now,
    ))
    return result, rows


def test_turns_after_the_first_are_priced_at_the_read_concurrency(echo_tool):
    """The red. The admission figure was 1; by turn two the shared totals say 5."""
    async def concurrency_now() -> int:
        return 5

    result, rows = run(
        ScriptedTurns(tool_call(), answer("done")), concurrency_now=concurrency_now,
    )

    assert result.response.text == "done"
    assert [r["expected_concurrency"] for r in rows] == [1, 5], rows
    assert rows[1]["requests_running"] == 5, rows[1]


def test_a_raising_concurrency_now_keeps_the_last_good_figure(echo_tool):
    """A read that fails must not fail the delegation, and must not lose the last figure."""
    async def concurrency_now() -> int:
        raise RuntimeError("the shared file is unreachable")

    result, rows = run(
        ScriptedTurns(tool_call(), answer("done")), concurrency_now=concurrency_now,
    )

    assert result.response.text == "done"
    assert [r["expected_concurrency"] for r in rows] == [1, 1], rows


def test_a_later_turn_files_its_rate_sample_under_the_read_concurrency(echo_tool):
    """The sample a turn files is keyed by the concurrency it actually met, not the one it
    was admitted under."""
    async def concurrency_now() -> int:
        return 5

    history = loop.RateHistory()
    result, _rows = run(
        ScriptedTurns(tool_call(), big_answer()),
        concurrency_now=concurrency_now,
        config=cfg(rate_sample_seconds=0.0),
        rate_history=history,
    )

    assert result.response.text == "an answer"
    assert history.expect(5) == pytest.approx(2_000 / 100.0), history.samples_at(5)


def test_an_unmeasured_turn_is_repriced_at_the_read_concurrency(echo_tool):
    """The row must not say 5 while the rate beside it is the solo bucket's.

    Turn one is too short to measure, so the rate is still the seed: without re-seeding,
    turn two would carry `expected_concurrency: 5` beside a rate remembered at 1.
    """
    async def concurrency_now() -> int:
        return 5

    history = loop.RateHistory()
    for _ in range(3):
        history.observe(4_000, 100.0, concurrency=1)  # 40 tok/s solo
        history.observe(1_500, 100.0, concurrency=5)  # 15 tok/s five-wide
    _result, rows = run(
        ScriptedTurns(tool_call(), answer("done")),
        concurrency_now=concurrency_now,
        rate_history=history,
    )

    assert [r["expected_concurrency"] for r in rows] == [1, 5], rows
    assert rows[0]["decode_rate"] == pytest.approx(40.0), rows[0]
    assert rows[1]["decode_rate"] == pytest.approx(15.0), rows[1]


class _Totals:
    def __init__(self, seqs: int, waiting: int) -> None:
        self.seqs = seqs
        self.waiting = waiting


class _SlotsDouble:
    """A slots double: `snapshot()` returns the totals, or raises `SlotsUnavailable`."""

    def __init__(self, totals=None, *, fail: bool = False) -> None:
        self.totals = totals
        self.fail = fail

    async def snapshot(self):
        if self.fail:
            raise SlotsUnavailable("lock held past its timeout")
        return self.totals, 1, 0


def test_the_servers_callable_reads_the_shared_totals():
    """`min(max(seqs + waiting, 1), cap)` -- this request is already in `seqs`."""
    slots = _SlotsDouble(_Totals(seqs=3, waiting=2))

    async def go() -> int:
        return await server._shared_concurrency_now(slots, cap=6, fallback=1)

    assert asyncio.run(go()) == 5


def test_the_servers_callable_falls_back_when_the_lock_cannot_be_taken():
    slots = _SlotsDouble(fail=True)

    async def go() -> int:
        return await server._shared_concurrency_now(slots, cap=6, fallback=1)

    assert asyncio.run(go()) == 1


def test_the_servers_callable_falls_back_with_no_shared_slots():
    async def go() -> int:
        return await server._shared_concurrency_now(None, cap=6, fallback=1)

    assert asyncio.run(go()) == 1


def test_the_dispatch_hands_the_callable_to_the_loop(monkeypatch: pytest.MonkeyPatch):
    """The wiring. Loop and callable are tested apart above, so a dropped hand-off between
    them passes both -- the first version dropped it, and every turn stayed priced solo."""
    seen: dict = {}

    async def fake_loop(*args, **kwargs):
        seen.update(kwargs)
        return "answered"

    async def concurrency_now() -> int:
        return 5

    async def progress(_done: int, _of: int) -> None:
        return None

    monkeypatch.setattr(server, "run_agentic_loop", fake_loop)
    asyncio.run(server.dispatch_delegation(
        cfg(), object(), object(), object(),
        allowed=frozenset({"read_file"}), effort=None, max_tokens=None,
        report_progress=progress, concurrency_now=concurrency_now,
    ))
    assert seen.get("concurrency_now") is concurrency_now
