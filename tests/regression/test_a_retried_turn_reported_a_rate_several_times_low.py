"""A turn that answered empty and was sent again reported a decode rate several times low.

`out_tok_s` on a turn event divided the answering attempt's `output_tokens` by
`backend_ms`. Those are not the same event. The tokens come from one attempt (ADR-0014);
`backend_ms` spans every attempt of the turn, every stage of `dispatch_with_recovery` and
every retry wait between them. So a turn that returned empty at a length stop, was resent
at a larger budget or a stepped-down effort and then answered divided one attempt's tokens
by two attempts' seconds.

A recorded 7.6 tok/s was exactly this shape, and the numbers below are that reading: 760
tokens against a 100s turn, of which the attempt that actually answered spent 20s
decoding -- 38 tok/s, five times what was written down. The figure is not decorative: it
seeds `RateHistory`, which keeps the minimum and then prices every later delegation's
first turn.

This is the sibling of `test_the_decode_rate_charged_prefill_to_the_decoder`, which fixed
the same arithmetic where the *rate estimator* read it and stopped at the estimator. The
transcript kept the old divisor.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_loop import FakeClock, cfg, entry

from claude_delegate_local import loop, transcript
from claude_delegate_local.backends.base import CanonicalResponse, TextBlock

# The observation this test is named for, kept as its fixture so the numbers stay tied to
# what was measured rather than becoming round ones that mean nothing.
OUTPUT_TOKENS = 760
EMPTY_PREFILL = 30.0
EMPTY_DECODE = 45.0
ANSWER_PREFILL = 5.0
ANSWER_DECODE = 20.0
TURN_SECONDS = EMPTY_PREFILL + EMPTY_DECODE + ANSWER_PREFILL + ANSWER_DECODE  # 100.0

REPORTED = OUTPUT_TOKENS / TURN_SECONDS  # 7.6 -- the bug
TRUE_RATE = OUTPUT_TOKENS / ANSWER_DECODE  # 38.0 -- the decoder


class _EmptyThenAnswers:
    """Empty at a length stop, then an answer: ADR-0014's cascade, in one turn.

    Each attempt advances the clock itself, so the turn's wall time is exact rather than
    raced -- the same reason the prefill regression does it.
    """

    def __init__(self, clock: FakeClock, *, timed: bool = True) -> None:
        self.clock = clock
        self.timed = timed
        self.calls = 0

    async def complete(self, request, *, on_token=None):
        self.calls += 1
        if self.calls == 1:
            self.clock.advance(EMPTY_PREFILL + EMPTY_DECODE)
            return CanonicalResponse(
                content=(), finish_reason="length",
                input_tokens=54_052, output_tokens=0, model="served-id-1",
                decode_seconds=EMPTY_DECODE if self.timed else None,
                prefill_seconds=EMPTY_PREFILL if self.timed else None,
            )
        self.clock.advance(ANSWER_PREFILL + ANSWER_DECODE)
        return CanonicalResponse(
            content=(TextBlock("the answer, at last"),), finish_reason="stop",
            input_tokens=54_052, output_tokens=OUTPUT_TOKENS, model="served-id-1",
            decode_seconds=ANSWER_DECODE if self.timed else None,
            prefill_seconds=ANSWER_PREFILL if self.timed else None,
        )

    async def probe_cluster(self):
        return None


def _turn_event(tmp_path: Path, *, timed: bool = True) -> dict:
    """One two-attempt turn, written to a stream the way the server writes one."""
    clock = FakeClock()
    backend = _EmptyThenAnswers(clock, timed=timed)
    started = clock()

    dispatch = asyncio.run(
        loop.dispatch_with_recovery(
            cfg(), entry(), backend,
            lambda level, budget: loop.build_one_shot_request(
                delegation=loop.Delegation("summarise this"), effort=level,
                max_tokens=budget, temperature=1.0, top_p=1.0,
            ),
            effort="high", deadline=None, clock=clock,
        )
    )
    assert backend.calls == 2, (
        f"the fixture made {backend.calls} attempt(s); the bug needs two, and one "
        "attempt divides by the same interval either way"
    )
    backend_ms = int((clock() - started) * 1000)

    watch = loop._Watch(diagnostics=True)
    watch.turn = 1
    watch.turn_cost(dispatch, evicted=0)

    path = tmp_path / "stream.jsonl"
    # `ms` and `backend_ms` as the server passes them: the turn ran no tools, so its wall
    # clock is the backend call. That equality is what makes the wrong divisor look
    # reasonable, which is why the fixture keeps it.
    transcript.Stream(path).turn(
        watch.turns[-1], "the answer, at last",
        ms=backend_ms, backend_ms=backend_ms, of_turns=1,
    )
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def test_the_rate_is_the_answering_attempts_own_decode_span(tmp_path: Path) -> None:
    """The bug. 760 tokens over a 100s turn is 7.6 tok/s and describes the retry."""
    event = _turn_event(tmp_path)

    assert event["out_tok_s"] == pytest.approx(TRUE_RATE, rel=0.01), (
        f"the turn reported {event['out_tok_s']} tok/s. {REPORTED:.1f} is the whole "
        f"turn -- one attempt's {OUTPUT_TOKENS} tokens over both attempts' "
        f"{TURN_SECONDS:.0f}s, which is arithmetic across two different events."
    )


def test_the_two_spans_are_summed_over_the_turns_attempts(tmp_path: Path) -> None:
    """Wall time actually spent, which is what a run summary adds up.

    The opposite convention from the rate above, and deliberately: a rate must divide by
    one attempt because its numerator comes from one, and a wall-clock total must include
    the attempt that produced nothing, because the clock did.
    """
    event = _turn_event(tmp_path)

    assert event["prefill_seconds"] == pytest.approx(EMPTY_PREFILL + ANSWER_PREFILL), (
        f"prefill reported as {event['prefill_seconds']}; the turn paid one prefill per "
        "attempt and the retry's is the larger of the two"
    )
    assert event["decode_seconds"] == pytest.approx(EMPTY_DECODE + ANSWER_DECODE), (
        f"decode reported as {event['decode_seconds']}; the attempt that returned empty "
        "still spent its tokens, and the clock still spent the time"
    )


def test_an_adapter_that_cannot_time_itself_still_reports_a_rate(tmp_path: Path) -> None:
    """`None` means "this adapter does not stream", not "the interval was zero".

    The old divisor stays as the fallback. A pessimistic rate is worse than a true one
    and much better than none, and it is the safe direction to err in, because
    `RateHistory` keeps the minimum.
    """
    event = _turn_event(tmp_path, timed=False)

    assert event["out_tok_s"] == pytest.approx(REPORTED, rel=0.01), (
        f"reported {event['out_tok_s']} tok/s with nothing to divide by but the turn; "
        "dropping the fallback leaves the event with no rate at all"
    )
    assert event["prefill_seconds"] is None
    assert event["decode_seconds"] is None
