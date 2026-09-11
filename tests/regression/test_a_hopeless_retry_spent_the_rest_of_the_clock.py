"""A read timeout was retried with the same budget against a fraction of the clock.

`_is_retryable` is true for every `BackendUnavailable`, and the adapter wraps every
`httpx.HTTPError` into one. So a connect failure at five seconds and a read timeout at
`turn_timeout` -- 1800s, the request delivered and no reply ever arriving -- were treated
as the same kind of accident.

They are not. A connect failure says nothing was attempted and a retry is cheap. A read
timeout says the endpoint took the whole allowance and did not finish, and the retry that
follows gets *less* time to do the same work. In the measured case the second attempt had
300s to do what the first could not do in 1800, which it cannot, so the only thing the
retry bought was the rest of the delegation's clock.

The rule here is time, not error class, because the error class alone cannot say whether
a retry is hopeless: the same read timeout is worth retrying when the deadline has room
for another attempt. What is never worth retrying is an attempt that cannot fit.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.backends.base import BackendRefused, BackendUnavailable
from claude_delegate_local.loop import _retry_is_plausible


def overrun(seconds: float = 1800.0) -> BackendUnavailable:
    """What a read timeout becomes: reached, delivered, and never answered."""
    return BackendUnavailable("ReadTimeout posting to /v1/chat/completions",
                              while_generating=True)


def unreachable() -> BackendUnavailable:
    """What a connect failure becomes. Nothing was generated, so nothing was lost."""
    return BackendUnavailable("ConnectError posting to /v1/chat/completions")


def test_an_overrun_is_not_retried_into_less_time_than_it_already_used():
    """The bug: 1800s of attempt, 300s remaining, retried anyway."""
    assert not _retry_is_plausible(overrun(), attempt_seconds=1800.0, seconds_left=300.0)


def test_an_overrun_is_still_retried_when_the_clock_can_afford_it():
    """The other direction. A blanket refusal would pass the test above and be wrong.

    The same read timeout is worth another attempt where the deadline has room for one,
    which is why this is a rule about time rather than about the error's class.
    """
    assert _retry_is_plausible(overrun(), attempt_seconds=1800.0, seconds_left=3000.0)


def test_an_unreachable_endpoint_is_retried_even_with_little_time_left():
    """A connect failure costs nothing and can succeed in a moment.

    Applying the time rule to every error would refuse the retry that most deserves one.
    """
    assert _retry_is_plausible(unreachable(), attempt_seconds=5.0, seconds_left=30.0)


def test_an_unretryable_error_stays_unretryable():
    """The existing predicate still governs. This narrows retries, never widens them."""
    assert not _retry_is_plausible(
        BackendRefused(400, "bad request"),
        attempt_seconds=1.0, seconds_left=10_000.0,
    )


def test_no_deadline_means_the_time_rule_cannot_refuse():
    """`ceiling()` returns None when nothing bounds the attempt, and None is not zero.

    Reading an absent deadline as no-time-left would refuse every retry on a deployment
    that publishes no rate -- turning a missing bound into a stricter one.
    """
    assert _retry_is_plausible(overrun(), attempt_seconds=1800.0, seconds_left=None)


@pytest.mark.parametrize("left", [1800.0, 1801.0])
def test_the_boundary_is_inclusive(left: float):
    """Exactly as much time as the failed attempt used is enough to try again."""
    assert _retry_is_plausible(overrun(), attempt_seconds=1800.0, seconds_left=left)
