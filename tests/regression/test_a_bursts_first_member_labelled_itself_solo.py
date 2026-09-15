"""A burst's first member labelled itself concurrency 1 and was priced for a quiet cluster.

`expected_concurrency` is a snapshot taken when the lease is granted:

    expected = min(lease.seqs_at_grant + lease.waiting_at_grant + 1, cfg.max_inflight_seqs)

The first member of a fan-out finds nothing in flight and nothing waiting, so it labels
itself 1 — microseconds before five siblings arrive and it decodes at six-way contention.
That label is the key the rate memory is written under and read by, so an untrue one poisons
both ends.

`expect` has been carrying the cost of that. It returns the worst rate seen at the requested
concurrency **or busier**, precisely because the label cannot be trusted, and that pooling is
what already protects this call (see `test_a_short_turn_poisoned_the_rate_memory`). Measured
2026-09-15 over 94 `priced` events, the protection costs 4.0x: `expect` returned 10.95 at
concurrency 1, 2 and 3 alike against a 44.1 solo benchmark.

So the two halves ship together and neither is useful alone. The hold makes the label true;
the bucketing spends it. With the hold off, the label is untrusted and `expect` pools exactly
as before — which is why `admission_idle_hold` is documented as disabling both.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from claude_delegate_local import admission as adm
from claude_delegate_local.admission import Admission
from claude_delegate_local.config import Config

from claude_delegate_local.loop import RateHistory


def history(*samples: tuple[int, float]) -> RateHistory:
    h = RateHistory()
    for concurrency, rate in samples:
        h._seen.append((concurrency, rate))
    return h


# --- the bucketing, which is only reachable once the label can be trusted ----------------

def test_a_trusted_solo_label_is_answered_from_solo_samples():
    """The fix. Today the six-way sample wins because it is the minimum overall."""
    h = history((1, 44.1), (1, 41.8), (6, 10.95))

    assert h.expect(1, trusted=True) == 41.8


def test_an_untrusted_label_still_pools_exactly_as_before():
    """The control that keeps the hold and the bucketing honest about each other.

    With `admission_idle_hold` at 0 nothing makes the label true, so the old pooling has to
    survive unchanged. If this ever diverges, turning the hold off stops being safe.
    """
    h = history((1, 44.1), (1, 41.8), (6, 10.95))

    assert h.expect(1, trusted=False) == 10.95
    assert h.expect(1) == 10.95, "pooling must remain the default"


def test_a_busy_question_is_answered_from_busy_samples_either_way():
    """Control. The fix must not make a contended call optimistic under either setting."""
    h = history((1, 44.1), (6, 10.95))

    assert h.expect(6, trusted=True) == 10.95
    assert h.expect(6, trusted=False) == 10.95


def test_a_trusted_label_with_nothing_at_that_concurrency_still_widens():
    """The widening survives as the fallback, which is where it was always sound.

    Nothing seen at four. A six-way sample bounds it from below, and using one beats
    falling through to the cluster's lifetime mean.
    """
    h = history((1, 44.1), (6, 10.95))

    assert h.expect(4, trusted=True) == 10.95


def test_a_quieter_sample_never_answers_a_busier_question():
    """Control, and the asymmetry the whole design rests on.

    A solo measurement says nothing about six. Answering a six-way question with 44.1 would
    authorise a reply that cannot be decoded in the time allowed, and the turn dies with
    nothing rather than returning something short.
    """
    h = history((1, 44.1))

    assert h.expect(6, trusted=True) is None


def test_the_worst_sample_in_the_bucket_is_the_one_used():
    """Control. Within the bucket the minimum is kept, deliberately.

    Over-estimating authorises a reply the clock cannot pay for; under-estimating truncates.
    A mean would discard that asymmetry, which is the one thing the old design got right.
    """
    h = history((2, 30.0), (2, 22.5), (2, 27.1))

    assert h.expect(2, trusted=True) == 22.5


# --- the hold, which is what makes `trusted=True` legitimate -----------------------------

def held_gate(monkeypatch, **over):
    """A gate whose hold is observable: the sleep is recorded rather than waited out."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(adm.asyncio, "sleep", fake_sleep)
    kw = {"workspace_roots": (".",), "max_inflight_seqs": 5, "kv_token_budget": 100_000}
    kw.update(over)
    return Admission(Config(**kw)), slept  # type: ignore[arg-type]


async def take(g, tokens=1000, *, key="flash", limit=5):
    return await g.acquire(tokens, prefill_tokens=tokens, entry_key=key, entry_limit=limit)


async def test_an_idle_gate_holds_before_recording_its_concurrency(monkeypatch):
    """The fix. The first member waits, so a burst behind it is counted."""
    g, slept = held_gate(monkeypatch, admission_idle_hold=10.0)

    await take(g)

    assert slept == [10.0]


async def test_a_busy_gate_does_not_hold(monkeypatch):
    """Control, and the half that keeps the hold free.

    Concurrency is already known once anything is in flight, so there is nothing to learn
    by waiting and the latency would be pure cost on every call after the first.
    """
    g, slept = held_gate(monkeypatch, admission_idle_hold=10.0)
    await take(g)
    slept.clear()

    await take(g)

    assert slept == [], "a busy gate must not pay the hold"


async def test_the_hold_is_skipped_entirely_when_disabled(monkeypatch):
    """Control. 0 has to mean no wait at all, not a zero-length one."""
    g, slept = held_gate(monkeypatch, admission_idle_hold=0.0)

    await take(g)

    assert slept == []


async def test_the_snapshot_is_taken_after_the_hold_not_before(monkeypatch):
    """The point of the whole exercise, and the bug an obvious implementation keeps.

    Holding and then reporting the numbers read *before* the wait would cost the latency
    and record the same untrue label, which no timing assertion would catch.
    """
    g, _ = held_gate(monkeypatch, admission_idle_hold=10.0)
    seen: list[int] = []

    async def fake_sleep(_seconds: float) -> None:
        # A sibling lands while the first member is holding.
        seen.append(1)
        await g.acquire(1000, prefill_tokens=1000, entry_key="flash", entry_limit=5)

    monkeypatch.setattr(adm.asyncio, "sleep", fake_sleep)

    lease = await take(g)

    assert seen == [1], "the sibling never arrived, so this proves nothing"
    assert lease.seqs_at_grant >= 1, "the snapshot predates the hold"
