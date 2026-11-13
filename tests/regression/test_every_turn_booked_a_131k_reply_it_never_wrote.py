"""Every turn booked a 131k reply it never wrote.

At effort high or max `resolve_max_tokens` raises the budget to
`thinking_max_tokens_floor` (131,072) on *every* turn, and the engine books KV room for the
whole budget for as long as the request runs, while measured later turns write p99 ~16k
(high) and ~25k (max) tokens. The fix shrinks the later turns to
`min(today's resolved budget, max(later_turn_reply_min_tokens, later_turn_reply_factor x
the largest output_tokens an earlier turn of this delegation reported))`, retries once at
today's full budget when that shrunk budget truncates a non-empty reply, and leaves a
caller's explicit `max_tokens` alone.

The first and fourth tests below fail against the unfixed loop, which sends 131,072 on
every turn and never retries a `length` stop under a shrunk budget. The second, third and
fifth are the guards: a shrink that never exceeds today's budget, an explicit budget that is
never shrunk, and a `stop` that is never retried. They pass both before and after, which is
what stops the change from overshooting in either direction.
"""

from dataclasses import replace

from test_agentic_loop import ScriptedTurns, cfg, run, says, wants
from test_agentic_loop import registered  # noqa: F401 -- a fixture, requested by name below

# Today's budget at effort high, from `thinking_max_tokens_floor`.
FULL = 131072


def test_turn_two_and_three_book_only_what_earlier_turns_actually_wrote(registered):  # noqa: F811
    """Turn 2 is asked for 131,072 tokens after a turn that wrote 1,000, and turn 3 for
    131,072 after one that wrote 20,000 -- KV room booked for the whole budget while the
    request runs. Turn 1 keeps today's budget; turn 2 shrinks to the 32,768 floor, and turn
    3 to twice the largest reply an earlier turn of this delegation reported."""
    backend = ScriptedTurns(
        replace(wants(("echo", {"what": "a"})), output_tokens=1000),
        replace(wants(("echo", {"what": "b"})), output_tokens=20000),
        says("done"),
    )
    result = run(backend, effort="high", max_turns=5)

    assert backend.requests[0].max_tokens == FULL, "turn 1 keeps today's budget"
    assert backend.requests[1].max_tokens == 32768, (
        "turn 2 asked for 131,072 after a turn that wrote 1,000: the shrink to "
        "later_turn_reply_min_tokens must apply from the second turn"
    )
    assert backend.requests[2].max_tokens == 40000, (
        "turn 3 must ask for twice the 20,000 the largest earlier turn wrote, not 131,072"
    )
    assert result.response.text == "done"


def test_a_shrink_never_exceeds_the_budget_today_resolves_to(registered):  # noqa: F811
    """The shrink is a minimum. With a small configured budget (16,384, so today's resolved
    budget is 16,384) the later turns must stay at 16,384 rather than being raised to the
    32,768 floor: the floor is a lower bound on the shrink, not a new budget."""
    small = cfg(max_tokens=16384, thinking_max_tokens_floor=16384)
    backend = ScriptedTurns(
        replace(wants(("echo", {"what": "a"})), output_tokens=1000),
        says("done"),
    )
    run(backend, effort="high", cfg=small, max_turns=5)

    assert backend.requests[0].max_tokens == 16384
    assert backend.requests[1].max_tokens == 16384, (
        "the shrunk budget rose above what resolve_max_tokens gives today"
    )


def test_a_callers_explicit_budget_is_sent_unchanged_on_every_turn(registered):  # noqa: F811
    """An explicit `max_tokens` is the caller's number, not a default to improve on: the
    later-turn shrink must not touch it, or a delegation that asked for room gets less."""
    backend = ScriptedTurns(
        replace(wants(("echo", {"what": "a"})), output_tokens=1000),
        replace(wants(("echo", {"what": "b"})), output_tokens=20000),
        says("done"),
    )
    run(backend, effort="high", max_tokens=50000, max_turns=5)

    assert [r.max_tokens for r in backend.requests] == [50000, 50000, 50000], (
        "an explicit budget was shrunk on a later turn"
    )


def test_a_length_stop_under_the_shrunk_budget_is_retried_at_the_full_one(registered):  # noqa: F811
    """A shrunk budget that truncates a non-empty reply is a wrong answer, not a saving: the
    same turn must be sent once more at today's full budget, recorded as
    `LengthUnderShrunkBudget` so `attempts` alone does not have to explain itself. Today the
    shrunk budget does not exist, so the truncated reply is simply the answer."""
    backend = ScriptedTurns(
        replace(wants(("echo", {"what": "a"})), output_tokens=1000),
        replace(says("half an ans"), finish_reason="length", output_tokens=32768),
        says("the answer"),
    )
    result = run(backend, effort="high", max_turns=5, diagnostics=True)

    assert backend.requests[1].max_tokens == 32768, (
        "turn 2 sent 131,072, so there is no shrunk budget for a length stop to hit"
    )
    assert len(backend.requests) == 3, (
        "a length stop under the shrunk budget must be retried exactly once in that turn"
    )
    assert backend.requests[2].max_tokens == FULL, (
        "the retry must go out at today's full budget, or it truncates the same way"
    )
    final = result.diagnostics[-1]
    assert final.turn == 2
    assert final.attempts == 2, "the retry is an attempt of the turn it belongs to"
    assert [r.kind for r in final.retries] == ["LengthUnderShrunkBudget"]
    assert result.response.text == "the answer", (
        "the answer is the retry's, not the truncated reply"
    )


def test_a_stop_under_the_shrunk_budget_is_not_retried(registered):  # noqa: F811
    """The negative control: a reply that ended `stop` was not cut off by the budget, so it
    must cost exactly one request in its turn. If the retry fired on every reply, the shrink
    would double the bill it exists to cut."""
    backend = ScriptedTurns(
        replace(wants(("echo", {"what": "a"})), output_tokens=1000),
        replace(says("the answer"), output_tokens=500),
    )
    result = run(backend, effort="high", max_turns=5, diagnostics=True)

    assert len(backend.requests) == 2, "a reply that ended 'stop' was sent again"
    assert result.response.text == "the answer"
    assert result.diagnostics[-1].retries == ()
