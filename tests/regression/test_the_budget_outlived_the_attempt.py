"""The reply budget was sized against a deadline no single attempt could ever use.

`DecodeRate.ceiling` converts seconds into tokens, and it was handed
`min(stall_left(), deadline - clock())` -- the two *delegation* deadlines. But a reply is
delivered by one backend call, and that call is bounded by `turn_timeout`, which is tighter
than both by default (1800 against 2100 and 14400). So the budget authorised a reply the
attempt carrying it could not deliver, and the overrun was then retried with the same
budget against whatever clock remained.

Measured 2026-09-11: nine delegations died at 2100s having completed zero turns, which is
1800s of attempt plus 300s of a retry that never had enough time to succeed.

This is wrong independently of the decode rate. Even with a perfect rate the margin is
being spent against the wrong clock: `reply_budget_margin` is a fraction of the time
available, and the time available to one attempt is `turn_timeout`.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.config import Config
from claude_delegate_local.loop import budget_seconds


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "turn_timeout": 1800,
        "stall_timeout": 2100,
        "dispatch_timeout": 14400,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def test_one_attempt_bounds_the_budget_when_it_is_the_tightest():
    """The bug, at the default settings. 1800 binds, not 2100 and not 14400."""
    assert budget_seconds(cfg(), stall_left=2100.0, dispatch_left=14400.0) == 1800.0


def test_a_nearly_spent_stall_deadline_still_wins():
    """The other direction, or the fix would be a constant wearing a `min`'s clothes.

    Late in a delegation the stall clock is the tighter one, and a budget sized to
    `turn_timeout` there would authorise a reply that outlives the delegation.
    """
    assert budget_seconds(cfg(), stall_left=120.0, dispatch_left=14400.0) == 120.0


def test_the_whole_delegation_deadline_still_wins_when_it_is_tightest():
    """All three are real bounds; none may be dropped from the comparison."""
    assert budget_seconds(cfg(), stall_left=2100.0, dispatch_left=45.0) == 45.0


def test_an_expired_clock_is_never_negative():
    """A negative would multiply into a negative ceiling and floor to `reply_budget_floor`,
    which reads as a small budget rather than as no time left."""
    assert budget_seconds(cfg(), stall_left=-30.0, dispatch_left=14400.0) == 0.0


@pytest.mark.parametrize("turn_timeout", [600, 1800, 2100])
def test_the_bound_follows_the_setting_rather_than_a_literal(turn_timeout: int):
    """It reads the setting, not the default it happens to have.

    2100 is the upper end config permits -- `turn_timeout <= stall_timeout` is enforced --
    and there the fix changes nothing, which is the case that keeps it a `min` rather than
    a substitution.
    """
    got = budget_seconds(cfg(turn_timeout=turn_timeout), stall_left=2100.0,
                         dispatch_left=14400.0)
    assert got == float(turn_timeout)
