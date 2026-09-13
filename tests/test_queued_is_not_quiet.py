"""A delegation waiting for a slot is not the same as one that has gone silent.

`state_of` had two facts to work from: whether an `end` event had been written, and how
long since the file was last touched. Both a queued delegation and a killed one are
unfinished and silent, so both read `quiet Nm` -- true of each and useful about neither.
Its docstring was right that the *file* cannot tell them apart, and right that a pid in the
stream would only mean something on the machine that wrote it.

The server can tell them apart, and now writes it down. `waiting` is recorded from the same
tick that already resets the client's idle timer during an admission wait (ADR-0072).

The highlight is here too, because it is the same screen and the same complaint: the
selected row was losing the colour of the very column these states live in.
"""

from __future__ import annotations

import time

import scripts.watch_delegations as wd


def _row(**over) -> dict:
    row = {"done": False, "ok": None, "mtime": time.time(), "last_signal": "",
           "waited_seconds": None}
    row.update(over)
    return row


def test_a_queued_delegation_says_so_rather_than_reading_as_silent():
    word, colour = wd.state_of(
        _row(last_signal="waiting", waited_seconds=400, mtime=time.time() - 400)
    )
    assert word.startswith("queued")
    assert "6m" in word, "the wait is reported from what the server measured"
    assert colour != wd.DIM, "queued is a state, not an absence"


def test_a_silent_delegation_that_was_never_queued_still_reads_quiet():
    """The control. Without it, a change that labelled every stale row `queued` passes.

    That change is the tempting one -- every row in a slow patch looks queued -- and it is
    exactly the conflation this fix exists to undo, in the opposite direction.
    """
    word, colour = wd.state_of(_row(mtime=time.time() - 400))
    assert word.startswith("quiet")
    assert colour == wd.DIM


def test_a_delegation_that_queued_and_then_ran_is_no_longer_queued():
    """`waiting` is not sticky. A flag set once would outlast the wait it described."""
    word, _ = wd.state_of(_row(last_signal="turn", waited_seconds=400))
    assert not word.startswith("queued")


def test_the_last_signal_written_is_what_summarise_keeps(tmp_path):
    """End to end from a file, because the ordering is the whole mechanism."""
    import json

    stream = tmp_path / "d.jsonl"
    stream.write_text("".join(json.dumps(e) + "\n" for e in [
        {"t": "start", "task": "x", "model_key": "m"},
        {"t": "waiting", "waited_seconds": 30.0, "of_seconds": 1800},
        {"t": "waiting", "waited_seconds": 60.0, "of_seconds": 1800},
    ]), encoding="utf-8")
    assert wd.summarise(stream)["last_signal"] == "waiting"

    with stream.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "priced", "budget_tokens": 1}) + "\n")
    assert wd.summarise(stream)["last_signal"] == "priced", (
        "a delegation that got its slot must stop reporting as queued"
    )


def test_the_selected_row_keeps_the_colour_of_its_state_column():
    row = f" 10:00  {wd.GREEN}ok{wd.R}  {wd.DIM}3{wd.R}  a task"
    out = wd._highlight(row)
    assert wd.GREEN in out, "stripping the row's colour costs the state column its cue"


def test_the_selected_row_still_spans_the_whole_width():
    """The other half, and the reason the old version stripped colour in the first place.

    A reset ends the selection as surely as it ends a dim, so simply *not* stripping
    highlights as far as the first reset and no further. This is the negative control for
    that naive fix: it passes the colour test above and fails this one.
    """
    row = f" 10:00  {wd.GREEN}ok{wd.R}  {wd.DIM}3{wd.R}  a task"
    out = wd._highlight(row)

    trailing = out.split(wd.SELECT)[-1]
    assert trailing.rstrip(wd.R).endswith("  "), "the selection must be padded to the width"
    # Every reset the row carries is followed by the selection being re-opened, or the
    # band dies at the first one.
    assert wd.R + wd.SELECT in out
