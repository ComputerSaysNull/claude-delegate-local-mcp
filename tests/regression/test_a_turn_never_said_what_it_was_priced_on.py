"""Nine delegations died without recording a single number that would explain why.

All nine were abandoned at the stall deadline having completed zero turns, and a `turn`
event is written only when a turn *completes*. So the stream held a `start`, thirty-four
heartbeats and an `end`, and not one figure about the budget the turn was given or the
load it was given it against. The diagnosis had to be reconstructed from arithmetic over
a cluster benchmark run separately, days later.

The three numbers that would have settled it in one reading are all in scope at the moment
a turn is dispatched and none of them survived the turn: what the decode rate was seeded
at, how many requests the cluster already had running when that seed was taken, and what
token ceiling the two produced. The budget is priced *before* the work, so it must be
recorded before the work, or it is only ever recorded for turns that did not need
explaining.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import json
from pathlib import Path


from test_transcript_stream import _run, two_turns



def priced(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("t") == "priced"]


def test_a_turn_records_the_budget_it_was_given(tmp_path: Path):
    """The bug. Without this the only record of a budget is on turns that completed."""
    events = _run(tmp_path, two_turns, "delegate", {"task": "explain the retry"})
    rows = priced(events)
    assert rows, f"no priced event in {[e['t'] for e in events]}"
    assert "max_tokens" in rows[0], rows[0]
    assert "budget_ceiling" in rows[0], rows[0]


def test_the_pricing_is_written_before_the_turn_it_prices(tmp_path: Path):
    """Ordering is the whole point: a turn that never finishes must still be explained.

    If the event landed with the turn it would share the turn's fate, which is exactly
    the fate that left nine deaths unexplained.
    """
    events = _run(tmp_path, two_turns, "delegate", {"task": "explain the retry"})
    kinds = [e["t"] for e in events]
    assert "priced" in kinds, kinds
    assert kinds.index("priced") < kinds.index("turn"), kinds


def test_the_pricing_says_what_load_the_rate_was_read_against(tmp_path: Path):
    """A rate without its concurrency cannot be checked afterwards.

    The seeded rate is a since-boot mean over every concurrency regime the engine has
    served, so the number alone does not say whether it applied. `requests_running` at
    the moment of the scrape is what makes it falsifiable.
    """
    events = _run(tmp_path, two_turns, "delegate", {"task": "explain the retry"})
    rows = priced(events)
    assert rows
    assert "requests_running" in rows[0], rows[0]
    assert "decode_rate" in rows[0], rows[0]


def test_every_turn_is_priced_not_only_the_first(tmp_path: Path):
    """The rate is re-derived per turn, so the record has to be per turn too.

    A single opening entry would go stale precisely when the cluster got busy, which is
    the case worth catching.
    """
    events = _run(tmp_path, two_turns, "delegate", {"task": "explain the retry"})
    turns = [e for e in events if e.get("t") == "turn"]
    assert len(priced(events)) >= len(turns), (
        f"{len(priced(events))} priced against {len(turns)} turns"
    )


def test_the_priced_event_is_json_serialisable_with_no_budget(tmp_path: Path):
    """`ceiling()` returns None when no rate is known, and None is a real answer.

    It must reach the stream as null rather than being dropped: "no cap was applied" is
    the single most incriminating thing the record can say, and an absent key reads as
    "not recorded" instead.
    """
    events = _run(tmp_path, two_turns, "delegate", {"task": "explain the retry"})
    rows = priced(events)
    # Asserted before the loop, because a loop over nothing passes whatever it asserts --
    # which is how this very test passed against the unfixed code on its first run.
    assert rows, f"no priced event in {[e['t'] for e in events]}"
    for row in rows:
        json.dumps(row)
        assert "budget_ceiling" in row
