"""Eviction stubbed a 34KB result; dedup handed all 34KB straight back.

The two mechanisms shared no state. `stub_oldest_tool_results` rewrote the *history*, and
the dedup cache is a separate dict keyed by call rather than by `tool_use_id`, so there was
nothing for them to match on. A repeat after an eviction therefore put the whole result
back into the window the trim had just made room in -- the eviction bought nothing, and the
turn that asked was spent for nothing too.

Measured 2026-09-13 by arithmetic on one run: turn 6 read 34,208 bytes, turn 9's repeat
returned 34,269, and `REPEAT_PREFIX` is exactly 61 bytes. Nothing re-ran, so the banner was
true; the eviction simply had no effect.

The fix marks rather than deletes. Deleting the cache entry would make the next identical
call re-run the tool, and one of the reads this happened to took 657 seconds -- paying that
again to recover bytes we deliberately discarded is worse than either mechanism alone.
"""

from __future__ import annotations

from claude_delegate_local import loop
from claude_delegate_local.backends.base import ToolUseBlock
from claude_delegate_local.config import Config
from claude_delegate_local.loop import _CachedResult, _mark_evicted_in_cache
from claude_delegate_local.tools import BashPolicy



def _cfg() -> Config:
    return Config(workspace_roots=(".",))  # type: ignore[arg-type]


KEY = ("read_file", '{"path": "big.py"}')
BULK = "y" * 34_208
OTHER_ID = "call_untouched"


def _cache() -> dict[tuple[str, str], _CachedResult]:
    return {
        KEY: _CachedResult(BULK, "call_6"),
        ("read_file", '{"path": "small.py"}'): _CachedResult("tiny", OTHER_ID),
    }


def test_an_evicted_result_is_not_handed_back_in_full():
    """The bug. After eviction the cache must stop serving what the history dropped."""
    cached = _cache()

    _mark_evicted_in_cache(cached, ("call_6",))

    assert cached[KEY].evicted is True
    # The content is still held -- marking, not deleting -- so nothing re-runs.
    assert cached[KEY].content == BULK


def test_a_result_still_in_the_history_is_untouched():
    """The control. Marking everything would satisfy the test above and break dedup.

    An entry whose result was not evicted must keep serving its content, or every repeat
    turns into a refusal and the cache stops being a cache.
    """
    cached = _cache()

    _mark_evicted_in_cache(cached, ("call_6",))

    assert cached[("read_file", '{"path": "small.py"}')].evicted is False


def test_marking_nothing_changes_nothing():
    """Eviction runs every turn and usually drops nothing. That must be free."""
    cached = _cache()

    _mark_evicted_in_cache(cached, ())

    assert all(entry.evicted is False for entry in cached.values())


def _serve(cached: dict[tuple[str, str], _CachedResult]) -> tuple[str, str]:
    """What `_run_one_call` actually hands back for the cached call, and its outcome.

    Driven through the real function rather than by rebuilding its branch here. A test
    that re-implements the code it is checking passes whatever the code does, which is the
    shape CLAUDE.md calls worse than no check -- and this project has shipped six of them.

    The call is byte-identical to the one `_cache` recorded, because dedup keys on exactly
    that. Nothing can execute: `allowed` is empty and the cache hit returns before
    `execute_tool` is reached, so a miss would surface as a refusal rather than a read.
    """
    call = ToolUseBlock(id="call_9", name="read_file", input={"path": "big.py"})
    block, outcome, _ = loop._run_one_call(
        _cfg(), call, frozenset(), cached, BashPolicy()
    )
    return block.content, outcome


def test_the_repeat_of_an_evicted_call_carries_no_content():
    """What the model actually receives, which is the point of the whole change.

    Asserted on the bytes that come out of `_run_one_call`: an entry marked evicted that
    still returned `REPEAT_PREFIX + content` would satisfy every test above and leave the
    bug exactly where it was. It must not contain the result, must stay short, and must say
    what happened and what to do -- a bare stub leaves the model repeating the call.
    """
    cached = _cache()
    _mark_evicted_in_cache(cached, ("call_6",))

    body, outcome = _serve(cached)

    assert BULK not in body, "the evicted content was handed back"
    assert len(body) < 500
    assert "dropped" in body
    assert "ask for" in body
    # Still a repeat: nothing ran. The ledger and the viewer read this vocabulary.
    assert outcome == "repeat"


def test_an_unevicted_repeat_still_carries_the_content():
    """The other direction of the same branch: dedup still does its job.

    Without this, serving the notice unconditionally would pass the test above and quietly
    turn every repeat into a refusal.
    """
    body, outcome = _serve(_cache())

    assert body == loop.REPEAT_PREFIX + BULK
    assert outcome == "repeat"
