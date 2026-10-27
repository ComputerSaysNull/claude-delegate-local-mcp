"""A stream that named its tool but not its tools was listed as `delegate`.

The stream gained `tool` a day before it gained `tools`, and in between every call wrote
`delegate`: a read-only one and a one-shot as much as a loop. `kind_of` tested only for a
missing `tool`, so a stream from that day showed `delegate` where ARCHITECTURE.md says a
missing `tools` is unknown. It now shows `?`, which is what the column promises for any
row it cannot tell apart.
"""

from datetime import UTC, datetime

from test_watch_delegations import stream

from claude_delegate_local import watch


def test_a_delegate_stream_with_no_tools_is_unknown(tmp_path):
    path = stream(tmp_path, datetime(2026, 8, 31, 12, tzinfo=UTC), "written between the two")
    watch._CACHE.clear()

    row = watch.summarise(path)
    assert row["tool"] == "delegate" and row["tools"] is None
    assert watch.kind_of(row) == "?"


def test_a_delegate_stream_that_names_its_tools_is_still_a_delegate(tmp_path):
    """The positive direction: the fix must not turn every `delegate` into `?`."""
    path = stream(tmp_path, datetime(2026, 9, 2, 12, tzinfo=UTC), "written after",
                  tools=["read_file"])
    watch._CACHE.clear()

    assert watch.kind_of(watch.summarise(path)) == "delegate"
