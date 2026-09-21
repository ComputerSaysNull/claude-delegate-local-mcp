"""The transcript viewer: which streams it lists, in what order, and how it navigates.

Two halves. The first calls the file-reading functions directly -- the cap, the
ordering and the cache, where a wrong answer is quiet rather than obvious. The second
drives the whole thing through a real pty, because the interesting failures live in the
seam between the terminal and the code and are invisible to anything that stubs stdin.
That half is skipped on Windows, where `pty` does not exist; the viewer runs under WSL
and CI runs the suite on Linux, so it is exercised in both places the viewer is used.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import select
import signal
import sys
import time
import unicodedata
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "scripts" / "watch_delegations.py"
posix_only = pytest.mark.skipif(os.name != "posix", reason="needs a pty")


def load_viewer():
    """Import scripts/watch_delegations.py by path -- scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "watch_delegations", ROOT / "scripts" / "watch_delegations.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def viewer():
    mod = load_viewer()
    mod._CACHE.clear()
    return mod


def stream(  # noqa: PLR0913 -- a builder for one event shape; each argument is a field
    directory: Path, started: datetime, task: str = "a task",
    turns: int = 0, ended: bool | None = None, *,
    tool: str = "delegate", tools: list[str] | None = None,
    effort: str | None = None, elapsed_seconds: float | None = None,
    turn_cached: int | None = None, end_cached: int | None = None,
    turn_sent: int | None = None, end_sent: int | None = None,
    turn_out: int | None = None, end_out: int | None = None,
) -> Path:
    """One `.jsonl` named the way transcript.py names it, with events to match.

    `tools` defaults to absent rather than empty, which is what a transcript written
    before the field existed looks like. The two are not interchangeable: absent means
    unknown, empty means a one-shot.
    """
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%f")[:-3]
    path = directory / f"{stamp}-0001-none.jsonl"
    start = {"t": "start", "at": started.astimezone(UTC).isoformat(),
             "tool": tool, "model_key": "m", "task": task}
    if tools is not None:
        start["tools"] = tools
    if effort is not None:
        start["effort"] = effort
    lines = [start]
    for n in range(1, turns + 1):
        event = {"t": "turn", "at": started.astimezone(UTC).isoformat(), "turn": n}
        if turn_cached is not None:
            event["cached_tokens"] = turn_cached
        if turn_sent is not None:
            event["input_tokens"] = turn_sent
        if turn_out is not None:
            event["output_tokens"] = turn_out
        lines.append(event)
    if ended is not None:
        end = {"t": "end", "at": started.astimezone(UTC).isoformat(), "ok": ended}
        if elapsed_seconds is not None:
            end["elapsed_seconds"] = elapsed_seconds
        if end_cached is not None:
            end["cached_tokens"] = end_cached
        if end_sent is not None:
            end["input_tokens"] = end_sent
        if end_out is not None:
            end["output_tokens"] = end_out
        lines.append(end)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def test_a_stream_older_than_a_week_is_still_listed(viewer, tmp_path):
    """The seven-day cutoff is gone: age no longer decides, only the cap does.

    Negative test for that removal -- it passes only because the cutoff went. Against
    the previous version the eight-day-old stream is missing and this fails.
    """
    now = datetime.now(UTC)
    fresh = stream(tmp_path, now - timedelta(days=1), task="fresh")
    stale = stream(tmp_path, now - timedelta(days=8), task="stale")
    rows, trimmed = viewer.scan(tmp_path)
    assert [row["path"] for row in rows] == [fresh, stale]
    assert trimmed == 0


def test_an_old_stream_touched_today_still_sorts_as_old(viewer, tmp_path):
    """The cutoff read the name rather than the mtime, and the ordering still does.

    Removing the cutoff must not take that with it: an eight-day-old dispatch that
    wrote a turn a moment ago belongs at the bottom of the list, not the top.
    """
    now = datetime.now(UTC)
    recent = stream(tmp_path, now - timedelta(hours=1), task="recent")
    stale = stream(tmp_path, now - timedelta(days=8), task="stale")
    os.utime(stale, (time.time(), time.time()))
    rows, _ = viewer.scan(tmp_path)
    assert [row["path"] for row in rows] == [recent, stale]


def test_streams_are_ordered_by_start_not_by_last_write(viewer, tmp_path):
    """The bug this replaces: a long-running older dispatch sorted above a newer one
    every time it wrote a turn, so the list reshuffled while you were reading it."""
    now = datetime.now(UTC)
    older = stream(tmp_path, now - timedelta(hours=2), task="older, still going")
    newer = stream(tmp_path, now - timedelta(hours=1), task="newer", ended=True)
    os.utime(older, (time.time(), time.time()))       # the older one just wrote a turn
    os.utime(newer, (time.time() - 600, time.time() - 600))
    rows, _ = viewer.scan(tmp_path)
    assert [row["path"] for row in rows] == [newer, older]


def test_ordering_falls_back_to_the_filename_when_the_start_event_is_unreadable(
        viewer, tmp_path):
    now = datetime.now(UTC)
    good = stream(tmp_path, now - timedelta(hours=3))
    broken = stream(tmp_path, now - timedelta(hours=1))
    broken.write_text("{ not json\n", encoding="utf-8")
    rows, _ = viewer.scan(tmp_path)
    assert [row["path"] for row in rows] == [broken, good]


def test_trimmed_counts_what_the_cap_dropped(viewer, tmp_path, monkeypatch):
    monkeypatch.setattr(viewer, "MAX_ROWS", 2)
    now = datetime.now(UTC)
    for n in range(5):
        stream(tmp_path, now - timedelta(minutes=n))
    rows, trimmed = viewer.scan(tmp_path)
    assert len(rows) == 2
    assert trimmed == 3, "a silently truncated list is the kind that gets trusted"


def test_the_list_is_capped_at_twenty_without_being_asked(viewer, tmp_path):
    """The cap, at its real value rather than a monkeypatched one.

    The test above proves the cap works; this one proves it is 20. Separate because a
    default nothing asserts is a default that drifts -- and 20 is the whole request.
    """
    now = datetime.now(UTC)
    for n in range(25):
        stream(tmp_path, now - timedelta(minutes=n))
    rows, trimmed = viewer.scan(tmp_path)
    assert viewer.MAX_ROWS == 20
    assert len(rows) == 20
    assert trimmed == 5


def test_a_row_carries_the_state_the_picker_renders(viewer, tmp_path):
    now = datetime.now(UTC)
    stream(tmp_path, now - timedelta(minutes=3), task="live one", turns=2)
    stream(tmp_path, now - timedelta(minutes=2), task="failed one", ended=False)
    stream(tmp_path, now - timedelta(minutes=1), task="good one", turns=1, ended=True)
    rows, _ = viewer.scan(tmp_path)
    by_task = {row["task"]: row for row in rows}
    assert by_task["live one"]["done"] is False
    assert by_task["live one"]["turns"] == 2
    assert by_task["failed one"]["done"] is True and by_task["failed one"]["ok"] is False
    assert by_task["good one"]["done"] is True and by_task["good one"]["ok"] is True


def test_the_list_says_which_kind_of_call_each_stream_was(viewer, tmp_path):
    """The column exists because every kind used to render identically. A `readonly` and
    an agent run are different amounts of trust and different amounts of money, and the
    list is where you choose which one to open."""
    now = datetime.now(UTC)
    stream(tmp_path, now - timedelta(minutes=5), "plain", tools=["read_file"])
    stream(tmp_path, now - timedelta(minutes=4), "readonly",
           tool="delegate_readonly", tools=[])
    stream(tmp_path, now - timedelta(minutes=3), "agent", tool="delegate_to_agent")
    stream(tmp_path, now - timedelta(minutes=2), "agent read-only",
           tool="delegate_to_agent_readonly")
    stream(tmp_path, now - timedelta(minutes=1), "silent one-shot", tools=[])

    rows, _ = viewer.scan(tmp_path)
    kinds = {row["task"]: viewer.kind_of(row) for row in rows}
    assert kinds == {
        "plain": "delegate",
        "readonly": "readonly",
        "agent": "agent",
        # Eight characters because the column is eight wide, and the pair reads the way
        # `readonly` does beside `delegate` -- the kind, then its constraint.
        "agent read-only": "agent-ro",
        # A `delegate` handed no tools ran the one-shot path, whatever it was called.
        "silent one-shot": "one-shot",
    }


def test_a_stream_from_before_the_tool_was_recorded_is_not_guessed_at(viewer, tmp_path):
    """The negative direction, and the whole reason the column is trustworthy. Every
    call once wrote `delegate` whether or not it was one, so defaulting an old row to
    `delegate` would reproduce exactly the confusion this ends."""
    path = tmp_path / "20260101T000000.000-0001-none.jsonl"
    path.write_text(json.dumps(
        {"t": "start", "at": "2026-01-01T00:00:00+00:00", "model_key": "m",
         "task": "written before the field existed"}) + "\n", encoding="utf-8")

    row = viewer.summarise(path)
    assert viewer.kind_of(row) == "?"


def test_opening_a_transcript_lists_the_files_it_was_given(viewer):
    """What a delegation was handed was recorded only in the `.json` record, which the
    viewer never opens -- and which does not exist until the work is over."""
    lines = viewer.render({
        "t": "start", "at": "2026-01-01T00:00:00+00:00", "tool": "delegate_readonly",
        "task": "review this", "model_key": "m", "tools": [],
        "files_read": [{"path": "/mnt/c/w/a.py", "given": r"C:\w\a.py",
                        "bytes": 40, "est_tokens": 1200}],
        "files_skipped": [{"path": "/mnt/c/w/b.py", "given": r"C:\w\b.py",
                           "reason": "not text", "kind": "binary"}],
        "prefetch_tokens": 1200,
    }, 100)
    screen = "\n".join(lines)

    assert r"C:\w\a.py" in screen, "the path the caller wrote, not only the resolved one"
    assert "1.2k" in screen, screen
    assert "skipped" in screen and "not text" in screen
    assert "none · one-shot" in screen, "a read-only call resolved to no tools"


def test_a_transcript_with_no_files_recorded_shows_no_file_block(viewer):
    """Absent is not empty. A delegation given no files and one whose files were never
    written down are different facts, and rendering the second as the first would be a
    quiet lie about an old transcript."""
    screen = "\n".join(viewer.render(
        {"t": "start", "at": "2026-01-01T00:00:00+00:00", "tool": "delegate",
         "task": "no files here", "model_key": "m"}, 100))

    assert "files:" not in screen
    assert "tools:" not in screen


def test_an_unchanged_file_is_not_read_twice(viewer, tmp_path, monkeypatch):
    """The list redraws unattended every couple of seconds over /mnt/c. Re-reading every
    file each time is the difference between a viewer and a load generator."""
    stream(tmp_path, datetime.now(UTC) - timedelta(minutes=1))
    reads: list[Path] = []
    real = viewer.summarise
    monkeypatch.setattr(viewer, "summarise",
                        lambda p: (reads.append(p), real(p))[1])
    viewer.scan(tmp_path)
    viewer.scan(tmp_path)
    assert len(reads) == 1, f"read {len(reads)} times, so the cache is not holding"


def test_an_appended_file_is_read_again(viewer, tmp_path, monkeypatch):
    """The negative half: the cache must not survive the thing it is keyed on."""
    now = datetime.now(UTC)
    path = stream(tmp_path, now - timedelta(minutes=1), turns=1)
    rows, _ = viewer.scan(tmp_path)
    assert rows[0]["turns"] == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "turn", "at": now.isoformat(), "turn": 2}) + "\n")
    os.utime(path, (time.time() + 1, time.time() + 1))
    rows, _ = viewer.scan(tmp_path)
    assert rows[0]["turns"] == 2, "a live stream's row froze at the cached read"


def test_a_vanished_stream_leaves_no_cache_entry(viewer, tmp_path):
    path = stream(tmp_path, datetime.now(UTC) - timedelta(minutes=1))
    viewer.scan(tmp_path)
    assert viewer._CACHE
    path.unlink()
    viewer.scan(tmp_path)
    assert not viewer._CACHE


def test_both_clock_columns_read_in_local_time(viewer):
    """`at` is UTC and an mtime is a bare timestamp. Rendering one converted and the
    other not would put an offset between two columns describing the same dispatch."""
    moment = datetime.now().astimezone()
    if moment.utcoffset() == timedelta(0):
        pytest.skip("machine is on UTC, so this cannot tell a converted clock from a raw "
                    "one -- skipped rather than passed, because it proves nothing here")
    assert viewer._clock(moment.astimezone(UTC).isoformat()) == moment.strftime("%H:%M:%S")
    assert viewer._clock(moment.timestamp()) == moment.strftime("%H:%M:%S")


def test_created_at_falls_back_when_the_name_was_not_written_here(viewer, tmp_path):
    odd = tmp_path / "hand-copied.jsonl"
    odd.write_text("", encoding="utf-8")
    os.utime(odd, (1_700_000_000, 1_700_000_000))
    assert viewer.created_at(odd) == pytest.approx(1_700_000_000)


def test_an_unfinished_stream_stops_claiming_live_once_it_goes_quiet(viewer, tmp_path):
    """A killed server leaves a stream with no `end` event. Measured on 2026-08-31:
    closing the editor took the whole process tree down mid-dispatch, and the viewer went
    on calling it live long after the cluster had finished with it."""
    now = datetime.now(UTC)
    path = stream(tmp_path, now - timedelta(minutes=30), "abandoned", turns=1)
    os.utime(path, (time.time() - 600, time.time() - 600))
    rows, _ = viewer.scan(tmp_path)
    word, _ = viewer.state_of(rows[0])
    assert word.startswith("quiet"), f"still claiming {word!r}"
    assert "10m" in word, f"the age is the whole point of saying quiet: {word!r}"


def test_a_stream_written_moments_ago_is_still_live(viewer, tmp_path):
    """The negative half. A one-shot is silent between its start and its end, so silence
    alone must not condemn it -- only silence for longer than a dispatch usually pauses."""
    path = stream(tmp_path, datetime.now(UTC) - timedelta(minutes=1), "working", turns=1)
    os.utime(path, (time.time(), time.time()))
    rows, _ = viewer.scan(tmp_path)
    assert viewer.state_of(rows[0])[0] == "live"


def test_a_finished_stream_is_never_called_quiet(viewer, tmp_path):
    """An `end` event settles it, however long ago it was written."""
    now = datetime.now(UTC)
    good = stream(tmp_path, now - timedelta(hours=20), "done", ended=True)
    bad = stream(tmp_path, now - timedelta(hours=19), "failed", ended=False)
    for p in (good, bad):
        os.utime(p, (time.time() - 60_000, time.time() - 60_000))
    rows, _ = viewer.scan(tmp_path)
    by_task = {r["task"]: viewer.state_of(r)[0] for r in rows}
    assert by_task == {"done": "ok", "failed": "fail"}


# --- driven through a real terminal -------------------------------------------------

class Session:
    """The viewer running on the far side of a pty, with keys in and screens out."""

    def __init__(self, pid: int, fd: int):
        self.pid, self.fd = pid, fd

    def read(self, seconds: float = 1.5) -> str:
        out = b""
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if not select.select([self.fd], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        # Strip the escapes so an assertion reads as what a person would see.
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out.decode("utf-8", "replace"))

    def send(self, keys: bytes) -> None:
        os.write(self.fd, keys)

    def exit_code(self, seconds: float = 3.0) -> int | None:
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.05)
        return None


@pytest.fixture
def session(tmp_path):
    import pty
    spawned: list[Session] = []

    def spawn(directory: Path) -> Session:
        pid, fd = pty.fork()
        if pid == 0:                                    # the child never returns
            os.chdir(ROOT)
            os.execve(sys.executable, [sys.executable, str(VIEWER)],
                      dict(os.environ, DELEGATE_TRANSCRIPT_DIR=str(directory),
                           TERM="xterm-256color"))
        made = Session(pid, fd)
        spawned.append(made)
        return made

    yield spawn
    for made in spawned:
        try:
            os.kill(made.pid, signal.SIGKILL)
            os.waitpid(made.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(made.fd)


@posix_only
def test_the_list_paints_every_stream_newest_first(session, tmp_path):
    now = datetime.now(UTC)
    ancient = stream(tmp_path, now - timedelta(days=9), "ANCIENT", ended=True)
    os.utime(ancient, (time.time(), time.time()))
    stream(tmp_path, now - timedelta(minutes=30), "OLDER", turns=1)
    stream(tmp_path, now - timedelta(minutes=5), "NEWER", ended=True)

    screen = session(tmp_path).read()
    assert "delegations" in screen
    # Painted now, and painted last: nine days old but touched a moment ago, so a list
    # ordered by write time would have put it first instead.
    assert screen.index("NEWER") < screen.index("OLDER") < screen.index("ANCIENT")
    assert "last 7 days" not in screen
    assert "r refresh" in screen and "q quit" in screen


@posix_only
def test_the_list_paints_a_duration_and_an_effort(session, tmp_path):
    """The columns as a reader actually meets them, through a real terminal.

    A finished row shows what the server measured; a running one shows how long it has
    been going, counted from its start. Asserting the values rather than the headings is
    the point -- a header can paint while the column beneath it renders `--`.
    """
    now = datetime.now(UTC)
    stream(tmp_path, now - timedelta(minutes=9), "FINISHED", ended=True,
           elapsed_seconds=125.0, effort="high", end_cached=44800, end_sent=56000)
    stream(tmp_path, now - timedelta(seconds=200), "RUNNING", turns=2, effort="low",
           turn_cached=1000, turn_sent=4000)

    screen = session(tmp_path).read()
    assert "duration" in screen and "effort" in screen
    # Four numeric columns, each answering a different question and none of them the
    # others' summary: prefill the cluster skipped, that as a share of what was sent,
    # what would have entered the calling conversation instead, and what the cluster
    # processed.
    for column in ("cached", "reuse", "return", "load"):
        assert column in screen, f"header lost the {column} column"
    assert "2m05s" in screen, "the finished row lost the server's own figure"
    assert "3m2" in screen, "the running row is not counting up from its start"
    assert "high" in screen and "low" in screen
    # A share, not a running total: 44,800 of 56,000 sent, and 2,000 of 8,000 across two
    # turns. The total grew fastest when reuse was worst, which is the one rendering that
    # could not show the eviction bug it was measuring (ADR-0058).
    assert "80%" in screen, "the finished row lost its recorded reuse"
    assert "25%" in screen, "the running row is not summing its turns"
    assert "44.8k" in screen, "the finished row lost its saved total"
    assert "2.0k" in screen, "the running row lost its saved total"


@posix_only
def test_the_list_refreshes_without_a_keypress(session, tmp_path):
    stream(tmp_path, datetime.now(UTC) - timedelta(minutes=5), "FIRST")
    live = session(tmp_path)
    live.read(1.0)
    stream(tmp_path, datetime.now(UTC), "APPEARED")
    assert "APPEARED" in live.read(3.5)


@posix_only
def test_an_arrow_key_moves_the_highlight_rather_than_quitting(session, tmp_path):
    """The regression this file was extended for. An arrow key is one escape sequence;
    reading it a character at a time through a buffered stdin left the tail in Python's
    buffer where `select` on the descriptor could not see it, so the escape read as a
    bare Escape and the viewer exited. Timing-dependent, and only a real terminal shows
    it -- so this presses the key and insists the process is still there afterwards."""
    now = datetime.now(UTC)
    stream(tmp_path, now - timedelta(minutes=9), "BOTTOM", ended=True)
    stream(tmp_path, now - timedelta(minutes=1), "TOP", ended=True)
    live = session(tmp_path)
    live.read(1.0)

    live.send(b"\x1b[B")                                # down, to BOTTOM
    live.read(1.0)
    live.send(b"\r")
    opened = live.read()
    assert "BOTTOM" in opened, "the arrow key did not move the highlight"
    assert live.exit_code(0.5) is None, "the arrow key was read as a quit"


@posix_only
def test_a_transcript_is_left_by_hand_and_returns_to_the_list(session, tmp_path):
    """`q` goes back rather than out, and reaching the end does not go back by itself."""
    stream(tmp_path, datetime.now(UTC) - timedelta(minutes=1), "ONLY", turns=1, ended=True)
    live = session(tmp_path)
    live.read(1.0)

    live.send(b"\r")
    opened = live.read()
    assert "done" in opened, "the end event was not rendered"
    assert "q to return" in opened
    assert "delegations" not in live.read(2.5), "it returned to the list on its own"

    live.send(b"q")
    assert "r refresh" in live.read(), "q did not come back to the list"
    live.send(b"q")
    assert live.exit_code() == 0, "q from the list did not exit"


@posix_only
def test_a_half_written_line_is_not_dropped(session, tmp_path):
    """The writer appends and the reader may arrive mid-line. Parsing the half loses the
    whole event: the remainder then turns up as its own unparseable fragment."""
    now = datetime.now(UTC)
    path = stream(tmp_path, now - timedelta(minutes=1), "LIVE")
    live = session(tmp_path)
    live.read(1.0)
    live.send(b"\r")
    live.read(1.0)

    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"t": "turn", "turn": 1, "text": "WHOLE"')
        fh.flush()
        live.read(1.0)
        fh.write("}\n")
    assert "WHOLE" in live.read(2.0)


def test_a_finished_row_reports_the_duration_the_server_measured(viewer, tmp_path):
    """Not mtime arithmetic. `elapsed_seconds` is what the dispatch measured for itself
    and it includes the admission wait, while an mtime only says when the last line was
    written -- and this directory is routinely synchronised, so mtime moves long after the
    work stopped. Touching the file must not change what the row claims it took."""
    stream(tmp_path, datetime.now(UTC) - timedelta(hours=3), "done one",
           ended=True, elapsed_seconds=125.0)
    (row,) = viewer.scan(tmp_path)[0]

    assert viewer.elapsed_of(row) == 125.0
    assert viewer._duration(viewer.elapsed_of(row)) == "2m05s"

    path = row["path"]
    path.touch()
    (again,) = viewer.scan(tmp_path)[0]
    assert viewer.elapsed_of(again) == 125.0, "mtime moved the duration"


def test_a_running_row_counts_up_from_its_start(viewer, tmp_path):
    """The column ticks, which is the whole request: a running delegation answers "how
    long has this been going", and the list redraws unattended to keep it honest."""
    started = datetime.now(UTC) - timedelta(seconds=90)
    stream(tmp_path, started, "running one")
    (row,) = viewer.scan(tmp_path)[0]

    at_start = viewer.elapsed_of(row, now=started.timestamp())
    later = viewer.elapsed_of(row, now=started.timestamp() + 200)
    assert at_start < 1
    assert 199 <= later <= 201, later
    assert viewer._duration(later) == "3m20s"


def test_a_finished_row_without_the_field_falls_back_rather_than_reporting_nothing(
        viewer, tmp_path):
    """A transcript written before `elapsed_seconds` existed still took some time. The
    negative direction of the test above: with no recorded figure the file's own span is
    the best available answer, and it must be used rather than rendering `--`."""
    stream(tmp_path, datetime.now(UTC) - timedelta(minutes=10), "old one", ended=True)
    (row,) = viewer.scan(tmp_path)[0]

    assert row["elapsed_seconds"] is None
    assert viewer.elapsed_of(row) is not None


def test_the_effort_is_readable_before_the_call_ends(viewer, tmp_path):
    """Effort is written in the `start` event, so it is on the row from the first redraw.
    Reading it from the end event instead would surface it exactly when it has stopped
    being worth knowing."""
    stream(tmp_path, datetime.now(UTC) - timedelta(minutes=1), "running one", effort="high")
    (row,) = viewer.scan(tmp_path)[0]

    assert row["done"] is False
    assert row["effort"] == "high"


def test_a_stream_from_before_effort_was_recorded_says_so(viewer, tmp_path):
    """Absent is not "off". An empty effort renders as `?`, the same treatment the kind
    column gives a tool it cannot name, rather than guessing at a level nobody chose."""
    stream(tmp_path, datetime.now(UTC) - timedelta(minutes=1), "old one")
    (row,) = viewer.scan(tmp_path)[0]

    assert row["effort"] == ""


def test_a_duration_keeps_two_units_where_a_staleness_keeps_one(viewer):
    """`_ago` rounds hard because it answers "how stale"; a duration sits beside other
    durations and is read against them, where 5m and 5m59s are a different answer."""
    assert viewer._duration(9) == "9s"
    assert viewer._duration(59.4) == "59s"
    assert viewer._duration(60) == "1m00s"
    assert viewer._duration(359) == "5m59s"
    assert viewer._duration(3600) == "1h00m"
    assert viewer._duration(7845) == "2h10m"
    assert viewer._duration(None) == "--"
    assert viewer._duration(-5) == "0s"


def test_a_running_row_sums_the_saving_its_turns_have_reported(viewer, tmp_path):
    """Summed, not replaced. Every turn resends the history, so the saving is what
    accumulated across all of them -- taking the last turn's figure would report one
    turn's reuse as the whole delegation's."""
    stream(tmp_path, datetime.now(UTC) - timedelta(minutes=2), "running one",
           turns=3, turn_cached=1000)
    (row,) = viewer.scan(tmp_path)[0]

    assert row["done"] is False
    assert viewer.cached_of(row) == 3000
    assert viewer._tokens(3000) == "3.0k"


def test_a_finished_row_prefers_the_total_the_end_event_recorded(viewer, tmp_path):
    """The end event carries the delegation's own total. It wins over the running sum for
    the same reason the duration does: it is the figure the dispatch measured for itself,
    and a stream can always be missing a turn event it never got to write."""
    stream(tmp_path, datetime.now(UTC) - timedelta(minutes=2), "done one",
           turns=2, turn_cached=1000, ended=True, end_cached=44800)
    (row,) = viewer.scan(tmp_path)[0]

    assert viewer.cached_of(row) == 44800
    assert viewer._tokens(44800) == "44.8k"


def test_nothing_measured_is_not_a_saving_of_zero(viewer, tmp_path):
    """The distinction the adapter goes to trouble to preserve, kept all the way to the
    column. An endpoint that reports no caching, or a transcript written before the field
    existed, must not render as a delegation that saved nothing."""
    stream(tmp_path, datetime.now(UTC) - timedelta(minutes=2), "old one",
           turns=2, ended=True)
    (row,) = viewer.scan(tmp_path)[0]

    assert viewer.cached_of(row) is None
    assert viewer._tokens(None) == "-"
    assert viewer._tokens(0) == "0", "a measured zero still renders as a number"


def test_a_token_count_is_narrow_enough_for_its_column(viewer):
    """Read for scale, not for arithmetic: the column is six wide."""
    for n in (0, 999, 1000, 26624, 999_999, 1_000_000, 12_345_678):
        assert len(viewer._tokens(n)) <= 6, (n, viewer._tokens(n))
    assert viewer._tokens(999) == "999"
    assert viewer._tokens(26624) == "26.6k"
    assert viewer._tokens(1_000_000) == "1.0M"
    # The boundary is 999,950, not a million: rounding happens after the unit is chosen,
    # so a naive `< 1_000_000` renders 999,999 as `1000.0k` and overflows the column.
    assert viewer._tokens(999_999) == "1.0M"


def _stream(tmp_path, turns, *, done=True):
    """A transcript stream with the per-turn prompt and cache figures the picker reads.

    The `end` event carries no token figures on purpose: it marks the run finished without
    displacing the per-turn sums these tests are about, since `sent_of` and `out_of` both
    prefer an `end` figure when there is one.
    """
    path = tmp_path / "20260906T000000.000-0001-no-agent.jsonl"
    lines = [{"t": "start", "task": "audit", "tool": "delegate", "at": "2026-09-06T00:00:00Z"}]
    for n, (sent, cached) in enumerate(turns, start=1):
        lines.append({"t": "turn", "turn": n, "input_tokens": sent, "cached_tokens": cached})
    if done:
        lines.append({"t": "end", "ok": True})
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return path


def test_the_picker_reports_reuse_as_a_share_not_a_running_total(viewer, tmp_path):
    """A cumulative total only ever grows, so the worse the reuse the larger the number.

    Measured on a real run: 559,872 tokens "saved" of which 479,232 was one 53,248-token
    opening prompt re-served on nine consecutive turns, against 902,996 actually sent. The
    total reads as a headline; the ratio reads as 62%, which is visibly poor for a history
    that is nominally append-only. This is the column that has to show the eviction bug
    rather than hide it.
    """
    # Nine turns re-serving the same 53,248-token prefix, exactly as the bug produced.
    row = viewer.summarise(
        _stream(tmp_path, [(80_000, 53_248)] * 9 + [(80_000, 0)])
    )

    assert viewer.cached_of(row) == 53_248 * 9
    assert viewer.sent_of(row) == 80_000 * 10
    assert viewer._reuse(viewer.reuse_of(row)) == "60%"


def test_a_healthy_append_only_run_reads_far_higher(viewer, tmp_path):
    """The other direction, or the assertion above would pass on any number at all."""
    row = viewer.summarise(
        _stream(tmp_path, [(50_000, 48_000), (60_000, 58_000), (70_000, 68_000)])
    )

    share = viewer.reuse_of(row)
    assert share is not None and share > 0.90


def test_an_endpoint_that_reports_no_caching_shows_no_share(viewer, tmp_path):
    """None is not zero: a measured miss and an unmeasured one are opposite answers, and
    rendering the second as 0% would claim a delegation reused nothing."""
    path = tmp_path / "20260906T000001.000-0001-no-agent.jsonl"
    path.write_text(
        json.dumps({"t": "start", "task": "x", "tool": "delegate"}) + "\n"
        + json.dumps({"t": "turn", "turn": 1, "input_tokens": 1000}) + "\n",
        encoding="utf-8",
    )
    row = viewer.summarise(path)

    assert viewer.cached_of(row) is None
    assert viewer.reuse_of(row) is None
    assert viewer._reuse(None) == "-"


def test_load_counts_every_resend_because_the_caller_would_have_too(viewer, tmp_path):
    """`load` is the apples-to-apples figure, not the flattering one.

    A turn loop resends its whole history, so summing prompts counts the same documents
    once per turn that carried them -- and so would Claude Code, which runs the same loop.
    Counting both sides that way is exact in method, which is why `load` is the figure to
    read as work the caller did not do.

    This replaced a `spared` column that mixed a peak prompt with total output to avoid
    that supposed double count. The premise was wrong and the metric answered no clean
    question.
    """
    row = viewer.summarise(
        _stream(tmp_path, [(10_000, 0), (20_000, 9_000), (30_000, 19_000)])
    )
    row["turn_out"] = 1_500

    assert viewer.load_of(row) == 10_000 + 20_000 + 30_000 + 1_500
    assert not hasattr(viewer, "displaced_of"), "the hybrid metric is gone, not renamed"


def test_returned_is_the_final_turns_output_and_nothing_else(viewer, tmp_path):
    """What actually reached the caller, which is exact rather than estimated.

    Every earlier turn's prompt, reasoning and tool traffic stayed on the far side of the
    call. Beside `load` this is the resource delegation protects: measured at 2,002 tokens
    of 907,400 on a real twelve-turn run.
    """
    row = viewer.summarise(_stream(tmp_path, [(10_000, 0), (20_000, 9_000)]))
    row["turn_outs"] = [4_000, 250]

    assert viewer.returned_of(row) == 250
    assert viewer.returned_of(row) < viewer.load_of(row)


def test_nothing_is_returned_until_the_delegation_ends(viewer, tmp_path):
    """A running row has returned nothing, so the column says nothing.

    It used to read `turn_outs[-1]` whatever the run's state, so a live delegation showed
    the newest turn's output and the figure grew and shrank on every 2s refresh. No caller
    ever receives an intermediate turn -- the number was real and answered no question.
    Blank is the honest reading, and it is what makes the finished figure mean something.
    """
    row = viewer.summarise(
        _stream(tmp_path, [(10_000, 0), (20_000, 9_000)], done=False)
    )
    row["turn_outs"] = [4_000, 250]

    assert row["done"] is False
    assert viewer.returned_of(row) is None
    assert viewer._tokens(viewer.returned_of(row)) == "-"


def test_a_finished_run_reports_the_end_events_own_count(viewer, tmp_path):
    """`end` carries the reply's own output figure, which beats the last thing seen.

    `out_of` already prefers it for the same reason. The turn fallback stays for a
    transcript whose `end` predates the field, which is unmeasured rather than zero.
    """
    row = viewer.summarise(_stream(tmp_path, [(10_000, 0), (20_000, 9_000)]))
    row["turn_outs"] = [4_000, 250]
    row["end_out"] = 900

    assert viewer.returned_of(row) == 900


def test_a_one_turn_delegation_returns_everything_it_generated(viewer, tmp_path):
    """The control. With one turn the whole output is the answer, so the two agree.

    It also says the honest thing about a one-shot: it returns most of what it cost, so it
    displaces far less per token than a long agentic run does.
    """
    row = viewer.summarise(_stream(tmp_path, [(14_000, 12_000)]))
    row["turn_outs"] = [6_000]

    assert viewer.returned_of(row) == 6_000


def test_no_token_figures_at_all_means_no_columns_rather_than_zeroes(viewer, tmp_path):
    """A transcript written before these fields existed is unknown, not free."""
    path = tmp_path / "20260906T000002.000-0001-no-agent.jsonl"
    path.write_text(
        json.dumps({"t": "start", "task": "x", "tool": "delegate"}) + "\n"
        + json.dumps({"t": "turn", "turn": 1}) + "\n",
        encoding="utf-8",
    )
    row = viewer.summarise(path)

    assert viewer.returned_of(row) is None
    assert viewer.load_of(row) is None
    assert viewer._tokens(None) == "-"


def test_the_highlight_survives_the_rows_own_colour_codes(viewer):
    """The selected row is selected across its whole width, not up to its first reset.

    A row carries colour/reset pairs of its own, and a reset ends the selection as surely
    as it ends a dim -- so wrapping the coloured string selected only as far as the first
    reset, which landed two columns in. The fix was once to strip every escape; since
    ADR-0072 it is to re-open the selection after each reset, which keeps the row's colour
    as well as its width. Asserted as the mechanism rather than the symptom: no reset may
    be the last thing standing before the end.
    """
    row = f"{viewer.DIM}12:00:00{viewer.R}  {viewer.DIM}high{viewer.R}  a task"
    out = viewer._highlight(row)

    assert out.startswith(viewer.SELECT)
    assert out.endswith(viewer.R)
    # Every reset but the final one is immediately followed by the selection re-opening.
    # Any that is not would end the band there, which is the bug this guards.
    assert out.count(viewer.R) - 1 == out.count(viewer.R + viewer.SELECT), out
    assert "12:00:00" in out and "a task" in out




def test_a_frame_overwrites_rather_than_blanking_first(viewer):
    """The flicker: erasing the screen and then painting leaves it blank for as long as
    the paint takes, which at a two-second cadence is visible from the corner of the eye.

    The frame goes home, overwrites each line and erases only that line tail, then
    clears the region below. Asserted as ordering, because that is the property: nothing
    may erase a region before the text that replaces it has been written.
    """
    assert viewer.HOME == "\033[H"
    assert viewer.EOL == "\033[K"
    assert viewer.BELOW == "\033[0J"
    # The old redraw is still defined, for the follow view, and is the thing the list
    # must not use: it erases forward from home before anything is printed.
    assert viewer.CLEAR.startswith(viewer.HOME)
    assert viewer.BELOW in viewer.CLEAR


def test_the_picker_opens_on_the_row_a_transcript_was_opened_from(viewer, tmp_path):
    """Coming back from the follow view landed at the top of the list, which on a busy
    directory is not where you were.

    Tested on the index rather than through a terminal: the terminal proves the wiring and
    this proves the rule, including the case the wiring cannot easily show -- a transcript
    that has aged out of the newest N, where any remembered index would point at an
    unrelated row.
    """
    rows = [{"path": tmp_path / f"{n}.jsonl"} for n in range(4)]

    assert viewer.start_index(rows, rows[2]["path"]) == 2
    assert viewer.start_index(rows, rows[0]["path"]) == 0
    assert viewer.start_index(rows, None) == 0
    assert viewer.start_index(rows, tmp_path / "aged-out.jsonl") == 0
    assert viewer.start_index([], tmp_path / "0.jsonl") == 0


def test_a_turn_says_which_effort_it_actually_ran_at(viewer):
    """The requested level and the level that answered are different facts.

    Empty-answer recovery steps the effort down and retries, so a delegation asked for at
    `high` can answer at `low`. The start event keeps the requested level and the picker
    keeps its column; without this the stepped-down one was in the stream and on no screen,
    which is how a transcript came to read `high` for a run where no turn used it.
    """
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 2,
        "input_tokens": 1000, "output_tokens": 50, "effort": "low", "attempts": 2,
    }, 100))

    assert "effort low" in screen
    assert "2 attempts" in screen


def test_a_turn_that_answered_first_time_does_not_mention_attempts(viewer):
    """The negative half: `attempts` is shown because it is unusual, not as decoration."""
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 1,
        "input_tokens": 10, "output_tokens": 5, "effort": "high", "attempts": 1,
    }, 100))

    assert "effort high" in screen
    assert "attempt" not in screen


def test_a_turn_repeating_itself_says_so_on_screen(viewer):
    """The number exists to be read by a person watching a run that has gone wrong.

    Putting it in the transcript and the result while leaving the live view unchanged
    would have left the watcher exactly as blind as before -- which is the failure this
    measure was added for, reproduced one layer up.
    """
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 3,
        "input_tokens": 1000, "output_tokens": 9000, "duplicate_line_share": 0.93,
    }, 100))

    assert "93% repeated" in screen


def test_an_ordinary_turn_does_not_mention_repetition(viewer):
    """The negative control, and the same rule `attempts` follows: shown because it is
    worth interrupting a reader for, not as decoration on every line."""
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 1,
        "input_tokens": 1000, "output_tokens": 900, "duplicate_line_share": 0.004,
    }, 100))

    assert "repeated" not in screen


def test_a_turn_from_before_the_share_was_streamed_claims_nothing(viewer):
    """Absent is not zero. An older transcript carries no share, and printing `0%`
    would assert that a run nobody can re-measure did not loop."""
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 1,
        "input_tokens": 10, "output_tokens": 5,
    }, 100))

    assert "repeated" not in screen


def test_a_rate_from_the_delegations_own_turns_says_what_the_number_means(viewer):
    """`own_turns` is the commonest source by far -- five of six rows on a real
    six-turn delegation -- and it fell through to the generic branch.

    The distinction the label carries is real: on anything but a `cluster_since_boot`
    row, `requests_running` echoes the concurrency frozen at lease grant rather than a
    reading of the cluster. "concurrency 1" invites it to be read as the latter.
    """
    screen = "\n".join(viewer.render({
        "t": "priced", "at": "2026-01-01T00:00:00+00:00", "turn": 2,
        "budget_ceiling": 139438, "decode_rate": 43.0, "requests_running": 1.0,
        "rate_source": "own_turns",
    }, 100))

    assert "priced for 1" in screen
    assert "concurrency" not in screen


def test_a_rate_read_from_the_cluster_still_says_running(viewer):
    """The negative control. Only this source carries a real reading, and collapsing
    the two labels would throw away the distinction rather than fix it."""
    screen = "\n".join(viewer.render({
        "t": "priced", "at": "2026-01-01T00:00:00+00:00", "turn": 1,
        "budget_ceiling": 80265, "decode_rate": 24.8, "requests_running": 4.0,
        "rate_source": "cluster_since_boot",
    }, 100))

    assert "4 running" in screen
    assert "priced for" not in screen


def test_a_turn_that_ran_tools_names_all_three_durations(viewer):
    """Total, tools, generating -- each said rather than inferred from a contrast.

    The older line printed the wall clock and then "of <backend>", which named neither
    and, on a turn that ran no tools, printed the same duration twice joined by a word
    implying they differed.
    """
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 1,
        "input_tokens": 1000, "output_tokens": 500, "out_tok_s": 20.0,
        "ms": 300_000, "backend_ms": 120_000,
        "tool_calls": [{"name": "read_file", "outcome": "ran"}],
    }, 100))

    assert "5m00s total" in screen
    assert "3m00s tools" in screen
    assert "2m00s generating" in screen


def test_a_turn_that_ran_no_tools_does_not_claim_zero_tool_time(viewer):
    """The negative control, and the reason the split is gated on the calls rather than
    on the arithmetic: "0s tools" on every answering turn is noise asserting a
    measurement nobody made."""
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 2,
        "input_tokens": 1000, "output_tokens": 500, "out_tok_s": 20.0,
        "ms": 120_100, "backend_ms": 120_000,
    }, 100))

    assert "2m00s total" in screen
    assert "2m00s generating" in screen
    assert "tools" not in screen


def test_a_turn_whose_tools_were_instant_still_says_it_ran_them(viewer):
    """Gated on the calls, so a fast tool reads as fast rather than as absent."""
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 3,
        "input_tokens": 10, "output_tokens": 5, "out_tok_s": 20.0,
        "ms": 10_000, "backend_ms": 9_990,
        "tool_calls": [{"name": "read_file", "outcome": "ran"}],
    }, 100))

    assert "<1s tools" in screen, (
        "a tool that returned in milliseconds must read as measured, not as a missing "
        "number: `_duration` floors under a minute and rendered this as `0s`"
    )


def test_the_budget_line_says_which_turn_of_how_many(viewer):
    """"turn 3" says where a delegation is; "of 25" says whether that is anywhere near
    the end. Only the second tells a reader whether to raise the cap, and it was the one
    the stream never carried."""
    screen = "\n".join(viewer.render({
        "t": "priced", "at": "2026-01-01T00:00:00+00:00", "turn": 3, "of_turns": 25,
        "budget_ceiling": 1000, "decode_rate": 20.0,
    }, 100))

    assert "turn 3 of 25" in screen


def test_a_finished_delegation_says_how_much_of_its_budget_it_used(viewer):
    """"6 turns" and "6 of 6 turns" are the same run and different news."""
    screen = "\n".join(viewer.render({
        "t": "end", "at": "2026-01-01T00:00:00+00:00", "ok": True,
        "turns": 6, "max_turns": 25, "elapsed_seconds": 60.0,
    }, 100))

    assert "6 of 25 turns" in screen


def test_a_stream_without_a_recorded_budget_claims_none(viewer):
    """The negative control for both, and the rule this viewer already follows twice:
    absent is not a value. An older transcript carries no budget, and inventing one
    would assert something about a run nobody can go back and check."""
    priced = "\n".join(viewer.render({
        "t": "priced", "at": "2026-01-01T00:00:00+00:00", "turn": 3,
        "budget_ceiling": 1000, "decode_rate": 20.0,
    }, 100))
    ended = "\n".join(viewer.render({
        "t": "end", "at": "2026-01-01T00:00:00+00:00", "ok": True,
        "turns": 6, "elapsed_seconds": 60.0,
    }, 100))

    assert "turn 3" in priced and " of " not in priced
    assert "6 turns" in ended and " of " not in ended


def test_an_unrecognised_rate_source_keeps_the_neutral_wording(viewer):
    """The third case, and the one a two-way split gets wrong.

    A source this viewer has not been taught about must not silently acquire a meaning.
    `unknown` is a real value the server writes, and a stream from before `rate_source`
    existed carries none at all -- neither can support either claim.
    """
    screen = "\n".join(viewer.render({
        "t": "priced", "at": "2026-01-01T00:00:00+00:00", "turn": 1,
        "budget_ceiling": 1000, "decode_rate": 20.0, "requests_running": 2.0,
        "rate_source": "unknown",
    }, 100))

    assert "concurrency 2" in screen
    assert "priced for" not in screen
    assert "running" not in screen


def test_a_turn_from_before_effort_was_streamed_claims_nothing(viewer):
    """Absent is not `default`. An older transcript carries no per-turn effort, and
    printing one would invent a fact about a run nobody can go back and check."""
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 1,
        "input_tokens": 10, "output_tokens": 5,
    }, 100))

    assert "effort" not in screen
    assert "turn 1" in screen


def test_the_requested_effort_still_heads_the_transcript(viewer):
    """Adding the per-turn value must not have moved the requested one off the header."""
    screen = "\n".join(viewer.render({
        "t": "start", "at": "2026-01-01T00:00:00+00:00", "tool": "delegate_to_agent",
        "task": "audit", "model_key": "m", "effort": "high",
    }, 100))

    assert "effort high" in screen


def test_a_turn_reports_its_time_as_a_length_not_as_seconds(viewer):
    """`171.4s` is a number a reader has to convert before it means anything."""
    screen = "\n".join(viewer.render({
        "t": "turn", "at": "2026-01-01T00:00:00+00:00", "turn": 1,
        "input_tokens": 10, "output_tokens": 5, "ms": 171_400,
    }, 100))

    assert "2m51s" in screen
    assert "171.4s" not in screen
    # Not `2:51`: that reads as a time of day, and it would sit on the same line as the
    # `_clock` stamp this renderer already prints.
    assert "2:51" not in screen


def test_a_finished_dispatch_reports_its_total_as_a_length(viewer):
    """The end event was the worst of the three: a twenty-minute run read as 1200.0s."""
    screen = "\n".join(viewer.render({
        "t": "end", "at": "2026-01-01T00:00:00+00:00", "ok": True,
        "turns": 3, "elapsed_seconds": 1200.0,
    }, 100))

    assert "20m00s" in screen
    assert "1200.0s" not in screen


def test_the_still_running_line_keeps_its_own_coarser_shape(viewer):
    """Deliberate: a live counter against a budget reads better as minutes."""
    screen = "\n".join(viewer.render({
        "t": "alive", "at": "2026-01-01T00:00:00+00:00",
        "elapsed_seconds": 200, "of_seconds": 3600,
    }, 100))

    assert "3m" in screen
    assert "3m20s" not in screen


# --- defect 1 and 2: the budget line ------------------------------------------------


def _priced(**over) -> dict:
    """One `priced` event with every field `transcript.priced` writes.

    Built from the writer's own keyword list rather than from what the renderer happens to
    read, so a field the server starts writing does not have to be remembered here twice.
    """
    event = {
        "t": "priced", "at": "2026-01-01T00:00:00+00:00", "turn": 16,
        "effort": "high", "max_tokens": 4000, "budget_ceiling": 3200,
        "decode_rate": 31.2, "requests_running": 4.0,
        "rate_source": "cluster_since_boot", "expected_concurrency": 4,
    }
    event.update(over)
    return event


def test_a_budget_priced_from_the_grant_does_not_claim_a_live_cluster(viewer):
    """`requests_running` is only sometimes a reading, and the line said it always was.

    Where the rate came from `observed_at_concurrency`, the field is handed straight back
    from the admission lease: it is the concurrency the grant was made at, frozen when the
    slot was taken, and nothing went and looked at the cluster. Rendering it as `4 running`
    offered a reader a measurement to reason about that no one ever took -- the worst kind
    of wrong number, because it is exactly the number a stuck delegation is diagnosed with.
    """
    line = "\n".join(viewer.render(
        _priced(rate_source="observed_at_concurrency",
                requests_running=4.0, expected_concurrency=4), 100))

    assert "running" not in line, f"a grant-time echo presented as a cluster reading: {line}"
    assert "priced for 4" in line, line


def test_a_budget_read_from_the_cluster_still_reports_what_was_running(viewer):
    """The control, and the reason the fix is three branches rather than a deletion.

    Under `cluster_since_boot` the figure is the engine's own `num_requests_running` gauge,
    read in the same scrape as the rate. It is the load the rate was read against, and
    without it the ceiling can be read but not argued with -- which is what the field was
    added for. A change that made every priced line stop saying `running` would pass the
    test above and fail this one.
    """
    line = "\n".join(viewer.render(
        _priced(rate_source="cluster_since_boot", requests_running=3.0), 100))

    assert "3 running" in line, line


def test_a_budget_with_no_recorded_source_claims_neither(viewer):
    """A stream written before `rate_source` existed cannot say which kind of number it
    holds, so the line says neither. Absent is not `cluster`, in the same way absent is not
    `default` for a turn's effort -- and the direction of the guess matters here, because
    one of the two available guesses invents a cluster measurement."""
    old = _priced()
    del old["rate_source"]
    line = "\n".join(viewer.render(old, 100))

    assert "running" not in line, line
    assert "concurrency 4" in line, line


def test_a_budget_line_names_the_turn_it_priced(viewer):
    """A budget is written *before* the turn it pays for, and lands beside the end of the
    one before it. Measured on a real transcript: turn 15's `turn` event and turn 16's
    `priced` event were 5 milliseconds apart, so both stamps rendered `10:42:07` and the
    budget read as turn 15's epitaph rather than turn 16's allowance.

    Naming the turn is what settles it. A finer stamp would not: the reader groups by the
    blank line and by what the line says, not by a clock they are not comparing digit by
    digit -- and `_clock` is shared with the picker's 8-wide `started` column, so
    sub-second precision there would break a column to fix a sentence.
    """
    line = "\n".join(viewer.render(_priced(turn=16), 100))

    assert "turn 16" in line, line
    assert "3,200 tok" in line and "31.2 tok/s" in line, "the figures themselves went"


def test_a_budget_line_opens_a_block_rather_than_closing_the_one_above(viewer):
    """The other half of the same misattribution: whitespace is what groups a transcript.

    Every other multi-line event here opens with a blank -- `turn`, `start` and `end` all
    do -- and `priced` was the one that did not, so it butted straight onto the last
    wrapped line of the turn above and inherited its paragraph.
    """
    lines = viewer.render(_priced(), 100)

    assert lines[0] == "", f"the budget line still joins the turn above it: {lines}"
    assert len(lines) == 2, lines


def test_a_one_shot_budget_still_renders_without_a_turn_number(viewer):
    """The negative direction. `turn` is written on every path today -- the one-shot writes
    `1` -- but a renderer that only works where a field is present is a renderer that
    crashes on the oldest transcript in the directory, which is the one being read when
    something has gone wrong."""
    without = _priced()
    del without["turn"]
    line = "\n".join(viewer.render(without, 100))

    assert "budget" in line and "3,200 tok" in line, line


# --- defect 3: the state column ------------------------------------------------------


def test_the_state_column_fits_the_widest_state_that_can_occur(viewer):
    """`queued 89m` is ten characters and the column was formatted nine wide.

    Nothing truncates -- Python's `<` pads and never cuts -- so the overflow was silent:
    the state ran into the `kind` beside it and every column right of it lost its
    alignment for that row, on exactly the rows a reader is scanning the list to find.

    Ten is the ceiling and not a guess: `_ago` truncates, so seconds stop at `59s`, minutes
    run to `89m` before the hour unit takes over, and `queued ` is the longest prefix.
    """
    queued = {"done": False, "ok": None, "mtime": time.time(),
              "last_signal": "waiting", "waited_seconds": 89 * 60}
    word, _ = viewer.state_of(queued)

    assert word == "queued 89m"
    assert len(word) + 2 <= viewer.STATE_WIDTH, (
        f"{word!r} is {len(word)} wide in a column of {viewer.STATE_WIDTH}, gutter included")


@posix_only
def test_the_state_column_keeps_its_gutter_when_the_state_is_widest(session, tmp_path):
    """The unit test above proves the number; this proves the paint uses it.

    A width asserted on a constant and then not threaded into both format strings is the
    check that cannot fail: the header and the row are composed separately, and they have
    drifted apart before -- which is why they are built from the same widths at all.
    """
    now = datetime.now(UTC)
    path = stream(tmp_path, now - timedelta(minutes=95), "QUEUED ONE")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "waiting", "at": now.isoformat(),
                             "waited_seconds": 89 * 60, "of_seconds": 1800}) + "\n")

    screen = session(tmp_path).read()
    assert "queued 89m  delegate" in screen, (
        "the widest state ran into the kind column beside it")


# --- defect 4: a queued delegation says so once a minute -----------------------------


def test_a_queued_delegation_is_repeated_once_a_minute(viewer):
    """The admission gate polls four times a second when the slot file is shared, and
    writes a `waiting` event on every tick. The follow view prints and never repaints --
    deliberately, so the terminal's scrollback holds what you have watched -- so a
    ten-minute wait did not flicker, it *was* the scrollback, and it pushed the turns
    either side of the wait out of it.
    """
    pacing = viewer.QueuedPacing()
    ticks = [{"t": "waiting", "waited_seconds": n * 0.25, "of_seconds": 1800}
             for n in range(301)]                      # 0.00s to 75.00s, every 0.25s

    shown = [out for tick in ticks for out in pacing.admit(tick)]

    assert [out["waited_seconds"] for out in shown] == [0.0, 60.0], shown


def test_the_wait_is_painted_once_more_when_it_ends(viewer):
    """Which line is held matters as much as the rate.

    A throttle that simply drops what it skips under-reports every wait by up to a minute,
    and reports it as a measurement -- a delegation queued for 119 seconds would close on
    `60s`. The newest skipped line is kept instead and painted when the queue breaks, so
    the last thing on screen is the wait's real total, immediately above whatever ended it.
    """
    pacing = viewer.QueuedPacing()
    for n in range(191):                               # 0.00s to 47.50s
        pacing.admit({"t": "waiting", "waited_seconds": n * 0.25, "of_seconds": 1800})

    out = pacing.admit({"t": "priced", "turn": 1, "budget_ceiling": 3200})

    assert [event["t"] for event in out] == ["waiting", "priced"], out
    assert out[0]["waited_seconds"] == 47.5, "the final line is not the newest wait"


def test_nothing_that_is_not_a_wait_is_ever_held_back(viewer):
    """The control. A pacer that swallowed anything else would be invisible in the tests
    above and catastrophic on screen -- the events it would swallow are the turns."""
    pacing = viewer.QueuedPacing()
    events = [{"t": "start", "task": "x"}, {"t": "turn", "turn": 1},
              {"t": "alive", "elapsed_seconds": 3}, {"t": "turn", "turn": 2},
              {"t": "end", "ok": True}]

    assert [out for event in events for out in pacing.admit(event)] == events


def test_a_second_wait_starts_its_minute_again(viewer):
    """A delegation can be queued, run, and be queued again on a later turn. The clock
    restarts with the new wait rather than carrying the last one's mark, or the first line
    of the second wait would be withheld for up to a minute -- the one line that matters
    most, because it is the one that says the delegation has stopped moving."""
    pacing = viewer.QueuedPacing()
    pacing.admit({"t": "waiting", "waited_seconds": 0.0})
    pacing.admit({"t": "waiting", "waited_seconds": 30.0})
    pacing.admit({"t": "turn", "turn": 1})

    assert pacing.admit({"t": "waiting", "waited_seconds": 0.25}) != []


@posix_only
def test_a_queued_transcript_does_not_fill_the_screen_with_itself(session, tmp_path):
    """End to end, because the pacer is only worth anything if `follow` routes through it.

    Two lines for a fifty-second wait: the one that opened it, and the one that closed it.
    """
    now = datetime.now(UTC)
    path = stream(tmp_path, now - timedelta(minutes=2), "QUEUED")
    with path.open("a", encoding="utf-8") as fh:
        for n in range(200):                           # 0.00s to 49.75s
            fh.write(json.dumps({"t": "waiting", "at": now.isoformat(),
                                 "waited_seconds": n * 0.25, "of_seconds": 1800}) + "\n")
        fh.write(json.dumps({"t": "turn", "at": now.isoformat(), "turn": 1,
                             "input_tokens": 10, "output_tokens": 5}) + "\n")

    live = session(tmp_path)
    live.read(1.0)
    live.send(b"\r")
    screen = live.read(2.5)

    assert screen.count("queued at the gate") == 2, (
        f"{screen.count('queued at the gate')} queued lines for one wait")
    assert "queued at the gate · 49s" in screen, "the closing line lost the wait's total"
    assert "turn 1" in screen, "the turn the wait was hiding"


# --- defect 5: the selected row and the width of an emoji ----------------------------


def _columns(text: str) -> int:
    """How many cells a terminal spends on a string, which is not how many characters
    Python counts in it. `W` and `F` are the two east-asian widths that render double."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def test_a_selected_row_with_an_emoji_stays_inside_the_terminal(viewer, monkeypatch):
    """The control this defect was fixed for: a row must not wrap because it was selected.

    `_highlight` padded to the terminal using `len()`, and a character is not a column.
    Every emoji a task text carries is east-asian-width `W` and renders two cells wide, so
    the count came up short by one per emoji, the pad overshot by exactly that much, and
    the row spilled onto a second line -- which the list then left as a stray blank
    directly under the selected row, since the frame only erases to the end of each line
    it wrote.
    """
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    row = f" 12:00:00  {viewer.GREEN}ok{viewer.R}  ✅ ship the thing"

    assert unicodedata.east_asian_width("✅") == "W", (
        "the premise: this emoji really does cost two cells")
    assert _columns(viewer._plain(row)) < 80, "the fixture row must fit before it is selected"
    assert _columns(viewer._plain(viewer._highlight(row))) <= 80


def test_the_selection_band_is_no_longer_padded_to_the_terminal(viewer, monkeypatch):
    """The mechanism, stated as what it now is: the band ends where the text does.

    Measuring columns properly would mean carrying a width table in a viewer, for a cue a
    shorter band already gives -- so the padding went rather than the measurement changing.
    """
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("LINES", "24")
    row = f" 12:00:00  {viewer.GREEN}ok{viewer.R}  a task"

    assert viewer._plain(viewer._highlight(row)) == viewer._plain(row)


def test_the_selection_still_never_cuts_the_row_back(viewer, monkeypatch):
    """The concern the padding was written around, kept. A truncated row loses the end of
    the task text, which is the part that tells two delegations apart -- and removing a pad
    cannot truncate, which is the whole argument for removing it rather than fixing it."""
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.setenv("LINES", "24")
    long_task = "x" * 400

    assert long_task in viewer._highlight(f" 12:00:00  {long_task}")


def test_the_highlight_still_survives_the_rows_own_colour_codes(viewer):
    """Unchanged behaviour, re-asserted because the line that produces it was rewritten.

    A row carries colour/reset pairs of its own and a reset ends the selection as surely as
    it ends a dim, so the selection is re-opened after each one. No reset may be the last
    thing standing before the end of the row.
    """
    row = f"{viewer.DIM}12:00:00{viewer.R}  {viewer.GREEN}ok{viewer.R}  a task"
    out = viewer._highlight(row)

    assert out.startswith(viewer.SELECT) and out.endswith(viewer.R)
    assert out.count(viewer.R) - 1 == out.count(viewer.R + viewer.SELECT), out
    assert viewer.GREEN in out, "the state column lost its colour on the selected row"


# --- defect 6: the unit switch in _ago ------------------------------------------------


def test_a_staleness_switches_to_minutes_where_the_minute_starts(viewer):
    """It switched at 90 seconds while truncating, so the display read `88s`, `89s`, `1m`.

    A reader watching a number count up saw it fall, and `1m` then covered 90 through 119
    seconds -- a minute-and-a-half reported as a minute, on the column that says how long
    a delegation has been silent. At 60 the unit changes where the word does.
    """
    assert viewer._ago(59.9) == "59s"
    assert viewer._ago(60) == "1m"
    assert viewer._ago(89) == "1m"
    assert viewer._ago(119.9) == "1m"
    assert viewer._ago(120) == "2m"


def test_a_staleness_never_reads_lower_than_the_second_before_it(viewer):
    """The property rather than the boundary, because the boundary alone would pass on a
    version that only moved the reversal somewhere else -- rounding to the nearest minute
    puts it back at 90 seconds, printing `2m` where `1m30s` would have been."""
    seen = [viewer._ago(float(s)) for s in range(0, 5400)]
    minutes = [int(word[:-1]) for word in seen if word.endswith("m")]

    assert all(word.endswith("s") for word in seen[:60])
    assert all(word.endswith("m") for word in seen[60:])
    assert minutes == sorted(minutes), "the minute count goes backwards somewhere"
    assert minutes[0] == 1 and minutes[-1] == 89


def test_the_hour_switch_is_left_where_it_was(viewer):
    """Only the first boundary moved. The second one is not a reversal -- `89m` to `1h` is
    a coarsening, not a number falling -- and moving it would make every quiet row in a
    quiet directory read `1h`."""
    assert viewer._ago(5399) == "89m"
    assert viewer._ago(5400) == "1h"
    assert viewer._ago(7200) == "2h"


# --- defect 7: a truncated reply reads as a finished one ------------------------------


def _end(**over) -> dict:
    """One `end` event, with the fields the writer actually puts in it."""
    event = {
        "t": "end", "at": "2026-01-01T00:00:00+00:00", "ok": True,
        "turns": 3, "elapsed_seconds": 12.5, "output_tokens": 4000,
        "cached_tokens": 0, "backend_ms": 9000, "out_tok_s": 444.4,
    }
    event.update(over)
    return event


def test_a_reply_cut_off_at_its_token_limit_says_so(viewer):
    """A truncated dispatch is a *successful* one: `ok` is true and nothing raised.

    So "done" is the one word that reads most wrongly about it, and the reader who believes
    it treats a reply that stopped mid-sentence as the whole answer.
    """
    out = "\n".join(viewer.render(_end(finish_reason="length"), 100))

    assert "cut off" in out
    assert "token limit" in out, "and why, or the reader cannot tell which fix applies"


def test_a_reply_the_endpoint_stopped_names_a_different_cause(viewer):
    """The reason has to discriminate, or it sends the reader to the wrong fix.

    A token limit is a budget to raise. A content filter is not, and saying only "cut off"
    would have someone raising max_tokens at a wall that does not move.
    """
    out = "\n".join(viewer.render(_end(finish_reason="content_filter"), 100))

    assert "cut off" in out
    assert "token limit" not in out


def test_an_ordinary_completion_is_not_labelled_cut_off(viewer):
    """Control. A warning on every finished dispatch is noise, and noise is ignored."""
    for reason in ("stop", "tool_calls", "", None):
        out = "\n".join(viewer.render(_end(finish_reason=reason), 100))
        assert "cut off" not in out, reason


def test_an_end_event_from_before_this_change_still_renders(viewer):
    """Control. Older transcripts carry no `finish_reason` at all and must not break."""
    out = "\n".join(viewer.render(_end(), 100))

    assert "done" in out
    assert "cut off" not in out
