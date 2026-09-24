"""The transcript viewer crashed on a line that was JSON but not an object, and when piped.

Both readers caught only `JSONDecodeError`, so a line that parses as a list or a number went
on to `.get` and raised, taking down the list view or the one being followed. `follow` also
asked stdout for its size, so `watch_delegations.py | tee log` crashed before showing
anything, and a reader that went away mid-print raised `BrokenPipeError`. The writer is
this server, so the practical source is a truncated or hand-edited file.
"""

from __future__ import annotations

import io
import json

import pytest

from test_watch_delegations import load_viewer

ODD_LINES = "[1, 2]\n42\n\"just a string\"\n"


def _transcript(tmp_path):
    start = {"t": "start", "at": "2026-09-24T10:00:00+00:00", "task": "t", "tool": "delegate"}
    end = {"t": "end", "at": "2026-09-24T10:00:05+00:00", "ok": True}
    path = tmp_path / "20260924T100000-0001-t.jsonl"
    path.write_text(json.dumps(start) + "\n" + ODD_LINES + json.dumps(end) + "\n",
                    encoding="utf-8")
    return path


def test_the_list_view_passes_over_a_line_that_is_not_an_object(tmp_path) -> None:
    viewer = load_viewer()
    row = viewer.summarise(_transcript(tmp_path))
    assert row is not None


def test_following_with_stdout_piped_passes_over_odd_lines(tmp_path, monkeypatch) -> None:
    viewer = load_viewer()
    monkeypatch.setattr(viewer, "wait_key", lambda _timeout: "q")
    out = io.StringIO()   # not a terminal, as when piped to another program
    monkeypatch.setattr("sys.stdout", out)
    viewer.follow(_transcript(tmp_path))
    assert "finished" in out.getvalue()


def test_following_stops_quietly_when_its_reader_goes_away(tmp_path, monkeypatch) -> None:
    viewer = load_viewer()
    monkeypatch.setattr(viewer, "wait_key", lambda _timeout: "q")

    class Gone(io.StringIO):
        def write(self, _s):
            raise BrokenPipeError

    monkeypatch.setattr("sys.stdout", Gone())
    try:
        viewer.follow(_transcript(tmp_path))
    except BrokenPipeError:
        pytest.fail("follow raised BrokenPipeError when the program it was piped to exited")
