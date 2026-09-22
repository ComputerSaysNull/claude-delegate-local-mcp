"""The reply budget is the delegation's remaining time, and nothing else.

It used to be the tighter of that and a per-call ceiling. The ceiling was not paying for
its place: doubling it from 1800 to 3600 moved the reply ceiling from 18,905 to 31,793
tokens and bought 347 more seconds and 111 more tokens, with the answer still coming from
the same attempt. Nothing bounds reasoning except the token cap the answer shares, so the
extra room enlarged the attempt that was already being thrown away (JOURNAL 2026-09-20).

What that ceiling was believed to guard is a call that has wedged, and the stall clock
guards it better: a stream that is producing emits frames 0.4s apart or better, so a
wedged call is *silent*. `test_stall_resets_on_token_arrival` holds that half -- a silent
backend is killed and a trickling one is not -- and this file is the other.

This replaces `tests/regression/test_the_budget_outlived_the_attempt.py`, whose subject
was the ceiling being added.
"""

from __future__ import annotations

from claude_delegate_local.config import Config
from claude_delegate_local.loop import budget_seconds


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def test_the_whole_of_what_is_left_is_available_to_one_reply():
    """The change. 10,000s left means 10,000s of budget, not a per-call slice of it.

    Against the retired code this returned 1800.0 -- the ceiling -- for any
    `dispatch_left` above it, which is what made a four-hour delegation price every
    reply as though it had half an hour.
    """
    assert budget_seconds(cfg(), dispatch_left=10_000.0) == 10_000.0


def test_the_delegation_deadline_still_binds_when_little_is_left():
    """The other direction, and the reason the term is still read at all."""
    assert budget_seconds(cfg(), dispatch_left=45.0) == 45.0


def test_an_expired_clock_is_never_negative():
    """A negative would multiply into a negative ceiling and floor to `reply_budget_floor`,
    which reads as a small budget rather than as no time left."""
    assert budget_seconds(cfg(), dispatch_left=-30.0) == 0.0


def test_the_stall_budget_does_not_bound_the_reply():
    """ADR-0099, and the term that must not come back now the `min` has none left.

    The stall clock is reset by `turn_done` immediately before the budget is computed, so
    a reply sized against it would be sized against the whole of `stall_timeout` -- and
    on this deployment that is well below the delegation ceiling. A reply being generated
    is not silence, and `stall_timeout` counts silence.
    """
    tight = cfg(stall_timeout=600)
    assert tight.stall_timeout < 10_000, "the case only exists below the budget under test"
    assert budget_seconds(tight, dispatch_left=10_000.0) == 10_000.0
