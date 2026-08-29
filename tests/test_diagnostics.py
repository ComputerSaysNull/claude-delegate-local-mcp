"""The per-call diagnostics ledger: what each turn cost, and what was re-read after eviction.

ADR-0007 extended from exit codes to context economics. Everything here is something the
server watched -- token counts the backend reported, evictions this loop performed, tool
calls it ran -- and nothing is the model's account of itself.

The correlation is the part worth testing hardest. "This delegation was expensive" is
already answerable from the aggregate ledger; "it was expensive because it kept re-reading
what we dropped" is the claim that needs evidence, and a correlation that reported every
re-read, or none, would look equally plausible in a report. So both directions.
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
BULK = "y" * 20_000  # large enough that retention actually bites


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",), "keep_tool_results": 1}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def reads(path: str) -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock("looking"), ToolUseBlock(id=f"c-{path}", name="reader",
                                                    input={"path": path})),
        finish_reason="tool_calls",
        input_tokens=100,
        output_tokens=5,
        model="served-id-1",
    )


def answers(text: str = "done") -> CanonicalResponse:
    return CanonicalResponse(
        content=(TextBlock(text),),
        finish_reason="stop",
        input_tokens=100,
        output_tokens=5,
        model="served-id-1",
    )


class Scripted:
    def __init__(self, *replies: CanonicalResponse) -> None:
        self.replies = list(replies)

    async def complete(self, request):
        if not self.replies:
            raise AssertionError("the loop called the backend more times than scripted")
        return self.replies.pop(0)

    async def probe(self):
        return ("served-id-1",)

    async def aclose(self):
        pass


@pytest.fixture
def reader():
    """A tool taking a `path`, so the correlation has something to key on.

    Not cacheable: dedup would serve the second read of the same path from memory, and a
    re-read that never happened is not the thing being measured.
    """
    added = {
        "reader": tools.RegisteredTool(
            spec=ToolSpec(name="reader", description="read", input_schema={"type": "object"}),
            handler=lambda cfg_, args: BULK,
            cacheable=False,
        )
    }
    tools.REGISTRY.update(added)
    try:
        yield
    finally:
        for name in added:
            tools.REGISTRY.pop(name, None)


def run(*replies, diagnostics: bool = False, **over):
    return asyncio.run(
        loop.run_agentic_loop(
            cfg(**over),
            ModelEntry(key="flash", base_url=HOST, served_model_id="served-id-1"),
            Scripted(*replies),
            loop.Delegation("do the thing"),
            allowed=frozenset({"reader"}),
            max_turns=8,
            diagnostics=diagnostics,
        )
    )


# --- opt-in ---------------------------------------------------------------------------------


def test_nothing_is_collected_unless_the_caller_asks(reader):
    result = run(reads("/a.py"), answers())
    assert result.diagnostics == ()
    assert result.rereads == ()


def test_asking_produces_one_record_per_turn(reader):
    """Including the turn that answers and runs no tools -- the one an accumulator placed at
    the end of the loop body silently skips, because that turn breaks out before reaching it.
    """
    result = run(reads("/a.py"), reads("/b.py"), answers(), diagnostics=True)
    assert [t.turn for t in result.diagnostics] == [1, 2, 3]
    assert result.turns == 3


def test_a_turn_record_carries_what_the_server_watched(reader):
    result = run(reads("/a.py"), answers(), diagnostics=True)
    first = result.diagnostics[0]
    assert first.input_tokens == 100
    assert first.output_tokens == 5
    assert first.attempts == 1
    assert first.effort in ("off", "low", "high", "max")
    assert first.tool_calls == (("reader", "ran"),)


def test_the_answering_turn_ran_no_tools_and_says_so(reader):
    """The other direction on the field above: an empty tuple, not a missing record."""
    result = run(reads("/a.py"), answers(), diagnostics=True)
    assert result.diagnostics[-1].tool_calls == ()


def test_the_aggregate_ledger_is_unaffected_by_the_flag(reader):
    """Diagnostics observe; they must not change what the delegation did or reports."""
    plain = run(reads("/a.py"), reads("/b.py"), answers())
    detailed = run(reads("/a.py"), reads("/b.py"), answers(), diagnostics=True)
    for field in ("turns", "tool_calls", "tool_errors", "deduped", "evicted", "attempts"):
        assert getattr(plain, field) == getattr(detailed, field), field


# --- the correlation ---------------------------------------------------------------------------


def test_a_file_read_again_after_we_dropped_it_is_reported(reader):
    """The measurement the item exists for.

    `/a.py` is read on turn one, its result is evicted once two newer ones exist, and the
    model then reads it again. That is the delegation paying twice for the same bytes
    because of this server's retention setting -- which is a different problem from a
    delegation that is simply large, and has a different fix.
    """
    result = run(
        reads("/a.py"), reads("/b.py"), reads("/c.py"), reads("/a.py"), answers(),
        diagnostics=True,
    )
    assert result.evicted > 0, "no eviction happened, so this proves nothing"
    paths = [r.path for r in result.rereads]
    assert "/a.py" in paths
    hit = next(r for r in result.rereads if r.path == "/a.py")
    assert hit.evicted_at_turn < hit.reread_at_turn


def test_a_file_read_twice_without_an_eviction_between_is_not_reported(reader):
    """The other direction, and the one that decides whether the number means anything.

    The same file, read twice, with retention generous enough that nothing was dropped. The
    model simply asked again -- that costs a tool call, not a re-read of lost context, and
    reporting it would make the correlation a re-read counter with a misleading name.
    """
    result = run(
        reads("/a.py"), reads("/a.py"), answers(),
        diagnostics=True, keep_tool_results=50,
    )
    assert result.evicted == 0
    assert result.rereads == ()


def test_a_file_evicted_but_never_read_again_is_not_reported(reader):
    """The other other direction. Eviction alone is not the finding -- eviction is working
    as configured. Only eviction followed by paying for the same file again is.
    """
    result = run(
        reads("/a.py"), reads("/b.py"), reads("/c.py"), answers(),
        diagnostics=True,
    )
    assert result.evicted > 0
    assert result.rereads == ()


def test_the_correlation_is_not_collected_without_the_flag(reader):
    result = run(reads("/a.py"), reads("/b.py"), reads("/c.py"), reads("/a.py"), answers())
    assert result.rereads == ()


# --- the eviction diff the correlation rests on --------------------------------------------------


def test_the_diff_names_only_what_this_pass_stubbed():
    """`newly_evicted_ids` must not re-report a result stubbed on an earlier turn, or every
    later turn would look like a fresh eviction and every re-read would correlate.
    """
    from claude_delegate_local.backends.base import Message, ToolResultBlock

    def history(*contents):
        return tuple(
            Message("user", (ToolResultBlock(tool_use_id=f"t{i}", content=c),))
            for i, c in enumerate(contents)
        )

    before = history(loop.EVICTED_STUB, "kept", "kept")
    after = history(loop.EVICTED_STUB, loop.EVICTED_STUB, "kept")
    assert loop.newly_evicted_ids(before, after) == ("t1",)


def test_the_diff_is_empty_when_nothing_changed():
    """The other direction. Returning everything stubbed would pass the test above."""
    from claude_delegate_local.backends.base import Message, ToolResultBlock

    same = (
        Message("user", (ToolResultBlock(tool_use_id="t0", content=loop.EVICTED_STUB),)),
    )
    assert loop.newly_evicted_ids(same, same) == ()
