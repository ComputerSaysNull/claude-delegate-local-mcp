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
    lines = [start]
    for n in range(1, turns + 1):
        lines.append({"t": "turn", "at": started.astimezone(UTC).isoformat(), "turn": n})
    if ended is not None:
        lines.append({"t": "end", "at": started.astimezone(UTC).isoformat(), "ok": ended})
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
    stream(tmp_path, now - timedelta(minutes=1), "silent one-shot", tools=[])

    rows, _ = viewer.scan(tmp_path)
    kinds = {row["task"]: viewer.kind_of(row) for row in rows}
    assert kinds == {
        "plain": "delegate",
        "readonly": "readonly",
        "agent": "agent",
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
