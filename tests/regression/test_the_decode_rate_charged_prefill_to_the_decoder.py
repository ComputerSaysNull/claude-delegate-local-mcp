"""The decode rate divided an answer's tokens by an interval that included its prefill.

The sequel to `test_the_decode_rate_measured_the_wrong_interval`, whose own docstring
names this as what it did not fix: the answering attempt still contains its own prompt
processing, so a turn with a large prompt and a short answer reads slow. 442 tokens
against 54,052 of input took 50.2s, of which roughly 30 was prefill -- a true decode rate
near 22 tok/s reported as 8.8.

`RateHistory.expect` keeps the **minimum**, so the most contaminated sample wins and then
prices every later delegation's first turn. Measured live on 2026-09-12: a remembered
13.4 tok/s, learned from an 855- and a 1,170-token answer, set a ceiling of 14,475 tokens
on a cluster that had just delivered 17,779 in one turn at 45.2 tok/s. In the audit
fan-out that followed, four of six passes were given ceilings below the 23,701 tokens the
one surviving pass needed, and each burned its whole budget on reasoning and returned an
empty answer.

`MIN_TOKENS` cannot catch this: it guards the size of the *answer* when the problem is the
size of the *prompt*, and 855 tokens sails past a threshold of 64. No threshold can, while
the interval is the whole attempt -- which is why the fix replaces the instrument rather
than filtering its output. Streaming makes `last token - first token` observable, and that
quantity simply does not contain prefill (ADR-0070).

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio

import pytest
from test_loop import FakeClock, cfg

from claude_delegate_local import loop
from claude_delegate_local.backends.base import CanonicalResponse, TextBlock
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist

# The measurement this test is named for, used as its fixture so the numbers stay tied to
# the observation rather than becoming round ones that mean nothing.
INPUT_TOKENS = 54_052
OUTPUT_TOKENS = 442
ATTEMPT_SECONDS = 50.0
DECODE_SECONDS = 20.0  # the rest was prefill


class PrefillHeavy:
    """One answer: a large prompt, a short reply, and the clock spent mostly prefilling.

    The clock is advanced by the backend itself so the attempt interval is exact rather
    than raced -- the same reason the sibling regression does it.
    """

    def __init__(self, clock: FakeClock, *, decode_seconds: float | None) -> None:
        self.clock = clock
        self.decode_seconds = decode_seconds
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        self.clock.advance(ATTEMPT_SECONDS)
        return CanonicalResponse(
            content=(TextBlock("a short answer"),),
            finish_reason="stop",
            input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            model="served-id-1",
            decode_seconds=self.decode_seconds,
        )

    async def probe_cluster(self):
        """Nothing published, so nothing can seed the rate but the history itself."""
        return None


def run(backend, clock, history):
    return asyncio.run(
        loop.run_agentic_loop(
            cfg(),
            ModelEntry(key="flash", base_url=HOST, served_model_id="served-id-1"),
            backend,
            loop.Delegation("summarise this"),
            allowed=frozenset(),
            max_turns=2,
            clock=clock,
            rate_history=history,
            expected_concurrency=1,
        )
    )


def test_the_rate_remembered_is_the_tokens_own_interval():
    """The bug. 442 tokens over a 50s attempt is 8.8 tok/s and describes the prefill."""
    clock = FakeClock()
    history = loop.RateHistory()
    run(PrefillHeavy(clock, decode_seconds=DECODE_SECONDS), clock, history)

    remembered = history.expect(1)
    assert remembered == pytest.approx(OUTPUT_TOKENS / DECODE_SECONDS), (
        f"remembered {remembered} tok/s; "
        f"{OUTPUT_TOKENS / ATTEMPT_SECONDS:.1f} is the whole attempt, which is the bug"
    )


def test_an_adapter_that_cannot_time_the_tokens_still_reports_something():
    """`None` means "this adapter does not stream", not "the interval was zero".

    The fallback has to stay: a backend added later that cannot separate the two is worse
    served by no rate at all than by a pessimistic one, and `expect` keeps the minimum, so
    a pessimistic sample is the safe direction for it to err in.
    """
    clock = FakeClock()
    history = loop.RateHistory()
    run(PrefillHeavy(clock, decode_seconds=None), clock, history)

    assert history.expect(1) == pytest.approx(OUTPUT_TOKENS / ATTEMPT_SECONDS)


def test_a_zero_interval_is_not_divided_by():
    """Degenerate rather than instantaneous, and the adapter reports `None` for it.

    Asserted here as well as in the adapter because this is the site that would divide:
    a 0.0 arriving from any future adapter must fall through to the attempt interval, not
    raise and not record an infinite rate.
    """
    clock = FakeClock()
    history = loop.RateHistory()
    run(PrefillHeavy(clock, decode_seconds=0.0), clock, history)

    assert history.expect(1) == pytest.approx(OUTPUT_TOKENS / ATTEMPT_SECONDS)
