"""A turn's independent tool calls ran one after another on a single thread.

`_run_calls` held one thread for the whole batch. Measured 2026-09-15 across one session's
transcripts, **26 of 55** tool-using turns issued more than one call, and every expensive
turn's batch was 2-3 independent `search_files` -- all reads, no writes, no `run_bash`.

Measured before building, because the read pool in ADR-0083 was built on an estimate and
reverted: driving the real `_search_files` sequentially against a thread pool gave **1.16x**
on two calls and **1.51x** on three, against ceilings of 2 and 3. Short of the ceiling
because `resolve_permitted` mixes syscalls that release the GIL with `fnmatch` that does not
-- and worth having, unlike the 1.5% the read pool bought.

What the concurrency must not move is the dedup cache. `_run_one_call` clears it after any
non-cacheable call, because a write invalidates every read taken before it, and it reads
then writes it for a cacheable one. Both are compound sequences whose meaning depends on
order, so a lock would keep the dict intact and still let two identical calls both miss and
both run. The cache therefore stays on one thread: read before dispatch, written after.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import threading
import time

import pytest

from claude_delegate_local import loop
from claude_delegate_local.backends.base import ToolResultBlock, ToolUseBlock
from claude_delegate_local.tools import ALL_TOOL_NAMES, BashPolicy

from test_loop import cfg as cfg_loop


def call(name: str, cid: str, **args) -> ToolUseBlock:
    return ToolUseBlock(id=cid, name=name, input=args)


def _watch():
    """The turn's ledger, which `_run_calls` writes to as each call comes back."""
    return loop._Watch(diagnostics=True)


def _tracked(monkeypatch, hold: float = 0.05):
    """Replace tool execution with something slow that records how many ran at once."""
    seen = {"peak": 0, "live": 0, "order": []}
    lock = threading.Lock()

    def fake(cfg, c, allowed, policy):
        with lock:
            seen["live"] += 1
            seen["peak"] = max(seen["peak"], seen["live"])
        try:
            time.sleep(hold)
            with lock:
                seen["order"].append(c.id)
            return ToolResultBlock(tool_use_id=c.id, content=f"body-{c.id}")
        finally:
            with lock:
                seen["live"] -= 1

    monkeypatch.setattr(loop, "execute_tool", fake)
    return seen


def _run(calls, cached=None):
    return loop._run_calls(
        cfg_loop(), tuple(calls), ALL_TOOL_NAMES, cached if cached is not None else {},
        _watch(), policy=BashPolicy(),
    )


def test_independent_reads_in_one_turn_run_together(monkeypatch):
    """The fix. Three independent reads are the shape every expensive turn had."""
    seen = _tracked(monkeypatch)

    _run([call("read_file", "a", path="/x/a"),
          call("read_file", "b", path="/x/b"),
          call("read_file", "c", path="/x/c")])

    assert seen["peak"] > 1, "the calls still ran one at a time"


def test_the_results_come_back_in_the_order_the_model_asked(monkeypatch):
    """Control, and the one a completion-ordered implementation fails.

    The model matches results to calls by `tool_use_id`, so a reordered list is not merely
    untidy -- but the blocks are also consumed positionally downstream, which no assertion
    about content would catch.
    """
    seen = _tracked(monkeypatch)
    # Finish in the reverse of the requested order, so returning completions would show.
    monkeypatch.setattr(loop, "execute_tool", _reverse_timed(seen))

    results, records = _run([call("read_file", "a", path="/x/a"),
                             call("read_file", "b", path="/x/b"),
                             call("read_file", "c", path="/x/c")])

    assert [r.tool_use_id for r in results] == ["a", "b", "c"]
    assert [r.name for r in records] == ["read_file"] * 3


def _reverse_timed(seen):
    """Make the first call the slowest, so completion order is the reverse of request order."""
    delays = {"a": 0.15, "b": 0.08, "c": 0.01}

    def fake(cfg, c, allowed, policy):
        time.sleep(delays.get(c.id, 0.01))
        seen["order"].append(c.id)
        return ToolResultBlock(tool_use_id=c.id, content=f"body-{c.id}")

    return fake


def test_run_bash_is_never_run_beside_anything(monkeypatch):
    """`run_bash` can commit, move a file, or rewrite what a sibling is reading.

    It is not cacheable, and not cacheable is exactly the property that makes a call a
    barrier: `_run_one_call` clears the whole dedup cache after one, because a write
    invalidates every read taken before it.
    """
    seen = _tracked(monkeypatch)

    _run([call("read_file", "a", path="/x/a"),
          call("run_bash", "b", command="true"),
          call("read_file", "c", path="/x/c")])

    assert seen["peak"] == 1, "a barrier call overlapped its neighbours"


def test_a_write_separates_the_reads_on_either_side(monkeypatch):
    """The reads before a write may overlap each other; they may not overlap the write.

    Written against the tempting implementation that pools every read in the turn and runs
    the writes afterwards -- which would let a read that must see the write run before it.
    """
    seen = _tracked(monkeypatch)

    _run([call("read_file", "a", path="/x/a"),
          call("read_file", "b", path="/x/b"),
          call("write_file", "w", path="/x/w", content="x"),
          call("read_file", "c", path="/x/c"),
          call("read_file", "d", path="/x/d")])

    assert seen["peak"] == 2, "the two groups of reads should pool separately, not together"


def test_an_identical_call_in_one_batch_still_runs_once(monkeypatch):
    """The dedup guarantee is a compound read-then-write, so concurrency is where it breaks.

    Two identical calls dispatched together would both miss the cache and both execute. The
    second must still come back as a repeat, exactly as it does when the batch is serial.
    """
    ran = []

    def fake(cfg, c, allowed, policy):
        ran.append(c.id)
        return ToolResultBlock(tool_use_id=c.id, content="same-body")

    monkeypatch.setattr(loop, "execute_tool", fake)

    results, records = _run([call("read_file", "a", path="/x/same"),
                             call("read_file", "b", path="/x/same")])

    assert len(ran) == 1, f"the identical call ran {len(ran)} times"
    assert [r.outcome for r in records] == ["ran", "repeat"]
    assert results[1].tool_use_id == "b", "the repeat must carry its own call's id"


def test_a_cached_result_is_still_served_without_running(monkeypatch):
    """Control. A hit from an earlier turn must not be dispatched just because it is pooled."""
    ran = []

    def fake(cfg, c, allowed, policy):
        ran.append(c.id)
        return ToolResultBlock(tool_use_id=c.id, content="fresh")

    monkeypatch.setattr(loop, "execute_tool", fake)
    cached = {("read_file", loop._dedup_key(call("read_file", "z", path="/x/a"))):
              loop._CachedResult("earlier", "z")}

    results, records = _run([call("read_file", "a", path="/x/a"),
                             call("read_file", "b", path="/x/b")], cached=cached)

    assert ran == ["b"], "the cached call was dispatched anyway"
    assert records[0].outcome == "repeat"
    assert "earlier" in results[0].content


def test_a_single_call_turn_is_unchanged(monkeypatch):
    """Control. One call must not acquire a pool, and must still record normally."""
    seen = _tracked(monkeypatch)

    results, records = _run([call("read_file", "a", path="/x/a")])

    assert seen["peak"] == 1
    assert len(results) == 1
    assert records[0].outcome == "ran"


@pytest.mark.parametrize("name", ["read_git", "write_file", "edit_file", "run_bash"])
def test_every_non_cacheable_tool_is_a_barrier(monkeypatch, name):
    """`read_git` is read-only but not cacheable either -- it reads a tree `run_bash` commits to.

    Conservative on purpose, and it costs nothing measurable: the batches that cost are
    `search_files`, which is cacheable.
    """
    seen = _tracked(monkeypatch)

    _run([call("read_file", "a", path="/x/a"),
          call(name, "n", path="/x/n", content="c", command="true", subcommand="log"),
          call("read_file", "c", path="/x/c")])

    assert seen["peak"] == 1
