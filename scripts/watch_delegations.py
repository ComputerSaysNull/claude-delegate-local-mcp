#!/usr/bin/env python3
"""Pick a delegation from the transcript directory and follow it as it happens.

Run it with no arguments: it lists what is there, newest first, live ones marked. Arrow
keys and Enter choose one; `q` leaves a transcript and comes back to the list, so one run
watches a whole session rather than one dispatch. Ctrl-C quits from anywhere. Open a
second terminal tab and run it again to watch two at once -- it holds no lock and writes
nothing.

Reads the `.jsonl` stream a dispatch appends to while it runs. The `.json` record beside
it is the finished summary and is not what this follows: a record that exists only once
the work is over cannot answer "is it stuck".

The follow view prints and never repaints, so the terminal's own scrollback holds
everything you have watched -- including after you return to the list. That is why
nothing here uses the alternate screen buffer or `ESC[3J`, both of which would throw
that away.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import shutil
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

try:
    import termios
    import tty
except ImportError:
    # Windows. The viewer is run through WSL like everything else that touches the
    # server, and guarding here rather than at the top is what lets the test suite --
    # which runs on the Windows interpreter -- import the parsing functions at all.
    termios = tty = None

def _transcript_dir() -> str:
    """Where the server writes, found the way the server finds it.

    The environment wins, then `<repo>/.env`. Reading the file matters more than it looks:
    the server loads it itself from the repo root, so a directory configured there is set
    for the server and for nothing else -- not for a shell, and not for this. Requiring the
    variable to be exported by hand would make the viewer a second place to configure the
    path, and the second place is the one that goes stale.
    """
    if found := os.environ.get("DELEGATE_TRANSCRIPT_DIR", "").strip():
        return found
    env = Path(__file__).resolve().parents[1] / ".env"
    try:
        for raw in env.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("DELEGATE_TRANSCRIPT_DIR="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


TRANSCRIPT_DIR = _transcript_dir()

MAX_ROWS = 20            # how far back the list reaches: the newest this many, always
REFRESH_SECONDS = 2.0    # unattended redraw of the list
POLL_SECONDS = 0.3       # how often a live stream is checked for new lines
STALL_SECONDS = 120      # silence after which an unfinished stream stops claiming "live"

# Home, then erase forward. `ESC[2J` and `ESC[3J` both cost scrollback in some terminals,
# and scrollback is how you read back over a transcript you have just watched.
CLEAR = "\033[H\033[0J"

# The redraw, done without a blank interval. Erasing the screen and then painting it leaves
# the terminal empty for as long as the paint takes, which at a two-second cadence reads as
# a flicker in the corner of the eye. Instead: go home, overwrite each line in place and
# erase only to the end of that line, then erase whatever is left below. Nothing is ever
# blank, because every cell is overwritten rather than cleared first.
HOME = "\033[H"
EOL = "\033[K"       # erase to end of line, after the text that replaces it
BELOW = "\033[0J"    # erase from the cursor down, for a list that just got shorter

# DEC 2026, synchronised output: the terminal is asked to present the frame in one go
# rather than as it arrives. Terminals that do not know it ignore both sequences, so this
# costs nothing where it is unsupported and removes tearing where it is.
SYNC_ON = "\033[?2026h"
SYNC_OFF = "\033[?2026l"

_ANSI = re.compile(r"\033\[[0-9;?]*[a-zA-Z]")


def _plain(text: str) -> str:
    """The text without its colour, for measuring width or re-colouring it wholesale."""
    return _ANSI.sub("", text)


def _highlight(line: str) -> str:
    """One row, inverted across its whole width.

    The colours have to come out first. A row carries its own `DIM`/reset pairs, and a
    reset ends the inverse as surely as it ends the dim -- so wrapping the coloured string
    highlighted only as far as the first reset, which was two columns in. Stripping and
    padding is what makes the highlight the width of the row rather than the width of its
    first field.
    """
    text = _plain(line)
    # Padded out to the terminal, never cut back to it. A highlight that truncates loses
    # the end of the task text, which is the part that tells two delegations apart -- and
    # a row wider than the window wraps, which is what it did before it was highlighted.
    width = max(shutil.get_terminal_size((100, 24)).columns, len(text))
    return f"{INVERT}{text.ljust(width)}{R}"

R = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
INVERT = "\033[7m"


def _clock(value: str | float | None) -> str:
    """Wall-clock time of an event, which is what a watcher is actually asking for.

    Local time in every case. The stream writes `at` as UTC and a file's mtime is a plain
    timestamp, so rendering the two side by side without converting would put an hour or
    two between columns that describe the same dispatch.
    """
    if value is None:
        return "--:--:--"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime("%H:%M:%S")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return str(value)[:8]
    return moment.astimezone().strftime("%H:%M:%S")


def _tokens(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _wrap(text: str, width: int, indent: str) -> list[str]:
    """Wrap on words, keeping the model's own line breaks, which carry its structure."""
    out: list[str] = []
    fenced = False
    for para in text.split("\n"):
        if para.strip().startswith("```"):
            fenced = not fenced
            out.append(f"{indent}{DIM}{para.strip()}{R}")
            continue
        if fenced:
            # Verbatim. Re-wrapping code on whitespace destroys the only thing that
            # makes it readable, and a model asked about code quotes code constantly
            # -- the first live delegation through this viewer flattened a function
            # into prose.
            out.append(f"{indent}{para}")
            continue
        if not para.strip():
            out.append("")
            continue
        line = indent
        for word in para.split():
            if len(line) + len(word) + 1 > width and line.strip():
                out.append(line.rstrip())
                line = indent + word + " "
            else:
                line = f"{line}{word} " if line.strip() else indent + word + " "
        if line.strip():
            out.append(line.rstrip())
    return out


_KINDS = {
    "delegate": "delegate",
    "delegate_readonly": "readonly",
    "delegate_to_agent": "agent",
}


def kind_of(row: dict) -> str:
    """What kind of call this was, in one word narrow enough for a column.

    Two facts, one column, because only one of them is ever a surprise: which tool was
    called is in the name, so the shape is worth naming only when a `delegate` was given
    no tools and quietly ran as one. `readonly` used to imply that and no longer does.

    A transcript written before the tool was recorded says `?` rather than `delegate`.
    Every call used to write `delegate` whether or not it was one, so guessing here would
    reproduce the exact confusion this column exists to end.
    """
    tool = row.get("tool") or ""
    if not tool:
        return "?"
    if tool == "delegate" and row.get("tools") == []:
        return "one-shot"
    return _KINDS.get(tool, tool)


def _given(event: dict) -> list[str]:
    """What the delegation was handed: the tools it resolved to, and every file.

    Rendered only from fields that are present. An older transcript shows nothing here
    rather than an invented "none" -- a delegation given no files and one whose files
    were never written down are different facts, and the second must not read as the
    first.
    """
    out: list[str] = []
    if isinstance(tools := event.get("tools"), list):
        named = ", ".join(tools) if tools else "none · one-shot"
        out.append(f"{DIM}          tools: {named}{R}")
    if isinstance(read := event.get("files_read"), list):
        total = event.get("prefetch_tokens")
        head = f"{len(read)} file{'' if len(read) == 1 else 's'}"
        head += f" · {_tokens(total)} tokens" if isinstance(total, int) else ""
        out.append(f"{DIM}          files: {head}{R}")
        for item in read:
            est = item.get("est_tokens")
            out.append(f"{DIM}            {item.get('given') or item.get('path', '?')}"
                       f"{'  ' + _tokens(est) if isinstance(est, int) else ''}{R}")
    for item in event.get("files_skipped") or []:
        # Loud, in a block that is otherwise dim. A file the caller believes it passed and
        # the model never saw is the one thing here worth interrupting a reader for.
        out.append(f"          {YELLOW}skipped{R} "
                   f"{item.get('given') or item.get('path', '?')} "
                   f"{DIM}{item.get('reason', 'no reason recorded')}{R}")
    return out


def render(event: dict, width: int) -> list[str]:
    """One event, as a block a person reads rather than a line a machine parses."""
    kind = event.get("t")
    stamp = f"{DIM}{_clock(event.get('at'))}{R}"

    if kind == "start":
        model = event.get("model_key", "?")
        effort = event.get("effort") or "default"
        agent = event.get("agent")
        who = f"{event.get('tool', 'delegate')}" + (f" · {agent}" if agent else "")
        head = f"{stamp}  {BOLD}{BLUE}{who}{R} {DIM}· {model} · effort {effort}{R}"
        return ["", f"{DIM}{'─' * width}{R}", head,
                *_wrap(event.get("task", ""), width, "          "),
                *_given(event),
                f"{DIM}{'─' * width}{R}"]

    if kind == "turn":
        n = event.get("turn", "?")
        cost = (f"{DIM}{_tokens(event.get('input_tokens'))} in · "
                f"{_tokens(event.get('output_tokens'))} out{R}")
        secs = event.get("ms")
        cost += f" {DIM}· {secs / 1000:.1f}s{R}" if isinstance(secs, (int, float)) else ""
        # Generation rate over the backend call, which is the figure that says whether the
        # cluster is slow. The turn's own wall clock includes tool execution, so a rate
        # taken from it would blame the cluster for time it did not spend generating.
        if isinstance(rate := event.get("out_tok_s"), (int, float)):
            gen = event.get("backend_ms")
            served = f" of {gen / 1000:.1f}s" if isinstance(gen, (int, float)) else ""
            cost += f"  {GREEN}{rate:g} tok/s{R}{DIM}{served}{R}"
        lines = ["", f"{stamp}  {BOLD}{CYAN}turn {n}{R}  {cost}"]
        for call in event.get("tool_calls", []) or []:
            ok = call.get("outcome") == "ok"
            mark = f"{GREEN}ok{R}" if ok else f"{RED}{call.get('outcome', 'error')}{R}"
            lines.append(f"  {YELLOW}▸ {call.get('name', '?')}{R} {DIM}"
                         f"{call.get('detail', '')}{R} {mark}")
        if text := (event.get("text") or "").strip():
            lines.append("")
            lines.extend(_wrap(text, width, "  "))
        return lines

    if kind == "alive":
        # One line, dim, no rule. It reports that nothing has happened, which is worth
        # knowing during a one-shot and worth not shouting about.
        secs = event.get("elapsed_seconds")
        of = event.get("of_seconds")
        spent = f"{secs / 60:.0f}m" if isinstance(secs, (int, float)) and secs >= 90 else (
            f"{secs:.0f}s" if isinstance(secs, (int, float)) else "?")
        budget = f" of {of // 60}m" if isinstance(of, int) else ""
        return [f"{stamp}  {DIM}still running · {spent}{budget}{R}"]

    if kind == "end":
        ok = event.get("ok")
        verdict = f"{GREEN}done{R}" if ok else f"{RED}failed{R}"
        secs = event.get("elapsed_seconds")
        n_turns = event.get("turns")
        tail = f"{DIM}{n_turns if n_turns is not None else '?'} turn"
        tail += "" if n_turns == 1 else "s"
        tail += f" · {secs:.1f}s" if isinstance(secs, (int, float)) else ""
        tail += R
        if isinstance(rate := event.get("out_tok_s"), (int, float)):
            out = event.get("output_tokens")
            tail += f"  {GREEN}{rate:g} tok/s{R}"
            tail += f"{DIM} · {_tokens(out)} out{R}" if isinstance(out, int) else ""
        lines = ["", f"{stamp}  {BOLD}{verdict}{R}  {tail}"]
        if err := event.get("error"):
            lines.extend(_wrap(err, width, f"  {RED}"))
            lines.append(R)
        lines.append(f"{DIM}{'─' * width}{R}")
        return lines

    return [f"{stamp}  {DIM}{json.dumps(event)[:width]}{R}"]


def created_at(path: Path) -> float:
    """When the dispatch started, from the name its writer chose.

    `transcript.py` stamps the filename at creation, so this needs no stat and cannot be
    confused by a later append. Neither `st_ctime` nor `st_mtime` would do: on Linux the
    first is the inode-change time rather than a birth time, and the second moves every
    time a turn lands. Falling back to mtime is only for a name this did not write.
    """
    stamp = path.name.split("-", 1)[0]
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S.%f").replace(tzinfo=UTC).timestamp()
    except ValueError:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def started_at(row: dict) -> float:
    """Sort key: the start event's own clock, or the filename when it cannot be read."""
    value = row.get("at")
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            moment = None
        if moment is not None:
            return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()
    return row.get("created", 0.0)


def summarise(path: Path) -> dict:
    """One row for the picker, read cheaply: the head of the file plus its mtime."""
    row = {"path": path, "task": "", "model": "", "turns": 0, "done": False,
           "tool": "", "tools": None, "effort": "", "elapsed_seconds": None,
           "turn_cached": None, "end_cached": None, "turn_sent": 0, "end_sent": None,
           "turn_peak": 0, "turn_out": 0, "end_out": None, "turn_outs": [],
           "created": created_at(path)}
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("t") == "start":
                    row["task"] = (event.get("task") or "").replace("\n", " ")
                    row["model"] = event.get("model_key", "")
                    row["at"] = event.get("at")
                    row["tool"] = event.get("tool", "")
                    row["effort"] = event.get("effort") or ""
                    # Absent and empty are kept apart on purpose: `kind_of` reports the
                    # first as unknown and the second as a one-shot.
                    row["tools"] = event.get("tools")
                elif event.get("t") == "turn":
                    row["turns"] = event.get("turn", row["turns"])
                    # Summed, not replaced: every turn resends the history, so the saving
                    # is what accumulated across all of them.
                    cached = event.get("cached_tokens")
                    if cached is not None:
                        row["turn_cached"] = (row["turn_cached"] or 0) + cached
                    sent = event.get("input_tokens") or 0
                    row["turn_sent"] += sent
                    # The fullest the history ever got, which is the unique content -- the
                    # sum counts the same documents once per turn that resent them.
                    row["turn_peak"] = max(row["turn_peak"], sent)
                    # Recorded only when the turn reported one. A turn carrying no
                    # `output_tokens` is a transcript written before the field existed,
                    # which is unmeasured rather than a turn that generated nothing --
                    # and `returned_of` would otherwise answer 0 where it knows nothing.
                    out = event.get("output_tokens")
                    if out is not None:
                        row["turn_out"] += out
                        row["turn_outs"].append(out)
                elif event.get("t") == "end":
                    row["done"] = True
                    row["ok"] = event.get("ok")
                    row["elapsed_seconds"] = event.get("elapsed_seconds")
                    row["end_cached"] = event.get("cached_tokens")
                    row["end_sent"] = event.get("input_tokens")
                    row["end_out"] = event.get("output_tokens")
    except OSError:
        pass
    row["mtime"] = path.stat().st_mtime if path.exists() else 0
    return row


_CACHE: dict[Path, tuple[tuple[float, int], dict]] = {}


def scan(directory: Path) -> tuple[list[dict], int]:
    """The newest `MAX_ROWS` streams, newest start first, plus how many were trimmed.

    A count rather than an age. A window in days answers "what happened lately", which
    is not the question the list is for -- it left a busy day unreadable and a quiet week
    nearly empty. The newest N is the same length whatever the week looked like.

    Dropping the window also drops the cheap pre-filter: it was applied from the filename
    before anything was opened, and ordering by start time needs the cached row. `_CACHE`
    is what bounds the cost instead. The list redraws unattended every couple of seconds,
    `summarise` reads a whole file, and this workspace lives on `/mnt/c` where that is
    roughly 12x the cost it would be on ext4 (ADR-0020) -- so an unchanged file is served
    from the cache, and a quiet refresh stats each candidate and reads none of them.
    """
    paths = list(directory.glob("*.jsonl"))
    for gone in set(_CACHE) - set(paths):
        del _CACHE[gone]
    rows = [_cached(p) for p in paths]
    rows.sort(key=started_at, reverse=True)
    return rows[:MAX_ROWS], max(len(rows) - MAX_ROWS, 0)


def _cached(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return summarise(path)
    key = (stat.st_mtime, stat.st_size)
    if (hit := _CACHE.get(path)) and hit[0] == key:
        return hit[1]
    row = summarise(path)
    _CACHE[path] = (key, row)
    return row


def _duration(seconds: float | None) -> str:
    """How long something took, kept comparable between rows.

    Two units, not one: `_ago` answers "how stale is this" and rounds hard, but a duration
    sits in a column beside other durations and is read against them, where 5m and 5m59s
    are a different answer. Seconds are zero-padded so the column stays rectangular.
    """
    if seconds is None:
        return "--"
    seconds = max(seconds, 0)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"


def elapsed_of(row: dict, now: float | None = None) -> float | None:
    """How long this delegation ran, or has been running.

    A finished row uses the server's own `elapsed_seconds` rather than mtime arithmetic.
    It is the figure the dispatch measured for itself and it includes the admission wait,
    where the file's mtime only says when the last line was written -- and this directory
    is routinely synchronised, so an mtime can move long after the work stopped.

    An unfinished row counts from the start event, which is why the column ticks: the list
    redraws unattended every couple of seconds. That is a real number even for a dispatch
    whose server was killed, and `state_of` is what says whether to trust it as live.
    """
    if row.get("done"):
        recorded = row.get("elapsed_seconds")
        if recorded is not None:
            return recorded
        # Written before the field existed, or an end event that lost it: fall back to the
        # span the file itself describes rather than reporting nothing.
        started = started_at(row)
        return max(row.get("mtime", 0) - started, 0) if started else None
    started = started_at(row)
    if not started:
        return None
    return max((time.time() if now is None else now) - started, 0)


def _tokens(n: int | None) -> str:
    """A token count narrow enough for a column, or `-` when there is none to show.

    Thousands are rounded to one decimal because the column is read for scale, not for
    arithmetic: 26.6k answers "was the prefix reused" and 26624 does not answer it any
    better while costing three characters.
    """
    if n is None:
        return "-"
    if n < 1000:
        return str(n)
    # The unit switches at 999,950 rather than 1,000,000, because rounding happens after
    # the choice: 999,999 divided by a thousand renders as `1000.0k`, which is seven
    # characters in a six-wide column. Found by the test that asserts the width.
    if n < 999_950:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def load_of(row: dict) -> int | None:
    """Everything the cluster processed: prompt plus output, summed over every turn.

    **This is the apples-to-apples figure**, and the one to read as work the caller did not
    do. A turn loop resends its whole history, so it counts the same documents once per turn
    that carried them -- and so would Claude Code, which runs the same loop and resends its
    context the same way. Counting both sides by summing across turns is therefore exact in
    method; the looseness is only that the two runs are not the same run.

    Much of it is served from cache rather than recomputed, which `cached` and `reuse` say.
    """
    sent, out = sent_of(row), out_of(row)
    if sent is None and out is None:
        return None
    return (sent or 0) + (out or 0)


def out_of(row: dict) -> int | None:
    if row.get("end_out") is not None:
        return row["end_out"]
    return row.get("turn_out") or None


def returned_of(row: dict) -> int | None:
    """Tokens of answer that actually reached the caller, or `None` until one has.

    Exact, and the point of the column. Beside `load` it says what a delegation cost against
    what it put in the calling conversation -- 2,002 tokens of 907,400 on a twelve-turn run,
    or 0.22%. Every earlier turn's prompt, reasoning and tool traffic stayed on the far side
    of the call, which is the resource delegation actually protects.

    **Nothing is returned until the delegation ends**, so a running row reports nothing
    rather than the newest turn's output. It used to read `turn_outs[-1]` unconditionally,
    which advanced on every 2s refresh and looked like an answer growing and shrinking --
    an intermediate turn's output is not a figure any caller ever receives. `done` is set by
    the `end` event, and `end` is written in a `try/finally`, so a delegation that was killed
    still ends and still reports.

    `end_out` is preferred over the last turn once done, for the same reason `out_of` prefers
    it: it is the reply's own count rather than the last thing seen on the way there. The
    fallback covers a transcript whose `end` predates the field.

    This replaced a `spared` column that summed a peak prompt with total output. That was
    invented to avoid "counting the same documents once per turn", on the reasoning that a
    caller doing the work itself would not have paid for them repeatedly. The reasoning was
    wrong: Claude Code runs the same agentic loop and resends its context every turn too, so
    it would have paid exactly the same way. `load` is therefore already the apples-to-apples
    figure, counted identically on both sides, and the hybrid answered no clean question.
    """
    if not row.get("done"):
        return None
    if row.get("end_out") is not None:
        return row["end_out"]
    turns = row.get("turn_outs") or ()
    return turns[-1] if turns else None


def _reuse(share: float | None) -> str:
    """A cache-reuse share for a narrow column, or `-` when nothing was measured.

    Whole percent: the column is read to spot a run that stopped reusing its prefix, and a
    decimal place answers that no better while costing two characters.
    """
    if share is None:
        return "-"
    return f"{share * 100:.0f}%"


def cached_of(row: dict) -> int | None:
    """Prompt tokens the cluster served from its prefix cache instead of computing.

    Named for what it measures, not for what a reader hopes it measures. It was `saved`
    until 2026-09-06, and that name was misread twice -- once here, into deleting the column
    outright, and once by a reader taking it for tokens the *caller* did not have to spend.
    It is neither: it is the serving stack's own prefix reuse, and it would read the same if
    no caller existed. `spared` is the one that answers what delegating was worth.

    A finished row uses the total the `end` event recorded; a running one sums what the
    turns have reported so far, so the figure grows as the delegation does. `None` is an
    endpoint that reports no caching, or a transcript written before the field existed --
    neither is a zero, and rendering them as one would claim a delegation saved nothing
    when the truth is that nothing was measured.
    """
    if row.get("end_cached") is not None:
        return row["end_cached"]
    return row.get("turn_cached")


def sent_of(row: dict) -> int | None:
    """Prompt tokens sent across every turn -- the denominator `cached_of` never had."""
    if row.get("end_sent"):
        return row["end_sent"]
    return row.get("turn_sent") or None


def reuse_of(row: dict) -> float | None:
    """Share of everything sent that the cluster served from cache.

    The column used to be the bare cumulative total, and that number is real but reads as
    a headline when it is a symptom. A run whose eviction boundary moved every turn
    re-served the *same* opening prompt on each of them: 559,872 "saved" of which 479,232
    was one 53,248-token prefix counted nine times, against 902,996 actually sent. As a
    ratio that is 62%, which is visibly poor for a history that is nominally append-only,
    and it moves the moment the prefix stops being reused. As a total it only ever grows,
    so the worse the reuse the larger the number -- the one presentation that cannot show
    the bug it is measuring.
    """
    cached, sent = cached_of(row), sent_of(row)
    if cached is None or not sent:
        return None
    return cached / sent


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{int(seconds / 3600)}h"


def state_of(row: dict) -> tuple[str, str]:
    """What the stream is doing, and the colour to say it in.

    A stream ends by writing an `end` event, so anything without one used to be shown as
    `live`. That is a claim the file cannot support: a dispatch whose server was killed --
    a closed editor takes the whole process tree with it -- stops mid-stream and leaves a
    file that is indistinguishable from one still being written. Measured on 2026-08-31,
    and it showed as live in the viewer while the cluster had long since finished with it.

    Nor can it be resolved by looking harder. A pid in the stream would only be meaningful
    on the machine that wrote it, and this directory is routinely synchronised. So the
    third state says what is actually known -- nothing has been written for a while -- and
    names the age, because a one-shot delegation is legitimately silent between its start
    and its end and must not be called dead for it.
    """
    if row["done"]:
        return ("ok", GREEN) if row.get("ok") else ("fail", RED)
    idle = max(time.time() - row.get("mtime", 0), 0)
    if idle < STALL_SECONDS:
        return "live", YELLOW
    return f"quiet {_ago(idle)}", DIM


@contextlib.contextmanager
def terminal():
    """Single-keypress input for as long as the viewer runs, restored on the way out.

    `cbreak` rather than `raw`: raw clears `OPOST` too, and every line the follow view
    prints would then stair-step down the screen. It also leaves `ISIG` alone, so Ctrl-C
    stays a signal and reaches the handler in `main` from inside a blocking read.
    """
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_key() -> str:
    """One keypress. Assumes `terminal()` is held.

    Reads the descriptor rather than `sys.stdin`, and that is not a style choice.
    `sys.stdin.read(1)` pulls a whole chunk into Python's own buffer and hands back one
    character of it, while `select` below can only see the descriptor -- so an arrow key,
    whose three bytes arrive together, left `[A` in the buffer with the descriptor looking
    idle. The escape was then read as a bare Escape and the viewer quit instead of moving
    the highlight. Intermittent, timing-dependent, and invisible to any test that does not
    drive a real terminal.
    """
    data = os.read(sys.stdin.fileno(), 8)
    if not data:
        return "eof"
    if data[:1] == b"\x1b":               # arrow keys arrive as an escape sequence
        return {b"A": "up", b"B": "down"}.get(data[2:3], "esc") if len(data) > 1 else "esc"
    if data[:1] in (b"\r", b"\n"):
        return "enter"
    if data[:1] == b"\x03":
        raise KeyboardInterrupt
    return data[:1].decode("utf-8", "replace")


def wait_key(timeout: float | None) -> str | None:
    """A keypress, or None once `timeout` passes with nothing typed."""
    if timeout is not None and not select.select([sys.stdin.fileno()], [], [], timeout)[0]:
        return None
    return read_key()


def start_index(rows: list[dict], start_at: Path | None) -> int:
    """Where the highlight opens: on `start_at` if it is still listed, else the top.

    Its own function so it can be tested without a terminal. Falls back to the top rather
    than to a remembered position, because a transcript that has aged out of the newest N
    is gone from the list and any index kept for it would point at an unrelated row.
    """
    return next((n for n, row in enumerate(rows) if row["path"] == start_at), 0)


def pick(directory: Path, start_at: Path | None = None) -> Path | None:
    """Newest first, live ones marked. A finished delegation is still worth reading.

    `start_at` puts the highlight back on a named transcript, so returning from the follow
    view lands on the row you just left rather than at the top of a list that may have
    grown underneath you.
    """
    rows, trimmed = scan(directory)
    i = start_index(rows, start_at)
    while True:
        out: list[str] = []
        head = f"{BOLD}delegations{R} {DIM}· ↑↓ select · enter follow · r refresh · q quit"
        head += f" · newest {MAX_ROWS} of {len(rows) + trimmed}" if trimmed else ""
        out.append(f"{head}{R}")
        # Composed from the same widths the row below uses, rather than typed out and
        # eyeballed. A header typed by hand drifts the moment a column is added -- which
        # is exactly what happened when the token figures became four columns.
        head_cols = (
            f" {'started':<8}  {'duration':<8}  {'effort':<6}  {'state':<9} "
            f"{'kind':<8} {'turns':>5}  {'cached':>6}  {'reuse':>5}  "
            f"{'return':>6}  {'load':>6}  task"
        )
        out.append(f"{DIM}{head_cols}{R}")
        for n, row in enumerate(rows):
            word, colour = state_of(row)
            task = row["task"][:60] or "(no task recorded)"
            # Pad the plain word, then colour it. Padding the coloured string counts the
            # escape bytes as width and the column stops lining up.
            state = f"{colour}{word:<9}{R}"
            kind = f"{DIM}{kind_of(row):<8}{R}"
            spent = f"{DIM}{_duration(elapsed_of(row)):<8}{R}"
            effort = f"{DIM}{(row.get('effort') or '?'):<6}{R}"
            # Both, because they answer different questions and one is not the other's
            # summary. `cached` is prefill the cluster skipped from its own prefix cache,
            # and `reuse` is that against the prompt tokens sent -- input only, since output
            # is never cached -- which is the figure that falls when a run stops reusing its
            # prefix. Showing only the total hides the bug; only the share hides the win.
            cached = f"{DIM}{_tokens(cached_of(row)):>6}{R}"
            reuse = f"{DIM}{_reuse(reuse_of(row)):>5}{R}"
            # `return` is what reached the caller; `load` is what the cluster processed
            # doing it, and is also what the caller would have spent running the same loop
            # itself. The gap between them is the resource delegation protects: on a
            # twelve-turn run, 2,002 tokens of 907,400. `cached` beside them is a third
            # thing again, the cluster's own prefix reuse rather than anything about a caller.
            spared = f"{DIM}{_tokens(returned_of(row)):>6}{R}"
            load = f"{DIM}{_tokens(load_of(row)):>6}{R}"
            line = (f" {_clock(started_at(row))}  {spent}  {effort}  {state} "
                    f"{kind} {DIM}{row['turns']:>5}{R}  {cached}  {reuse}  "
                    f"{spared}  {load}  {task}")
            out.append(_highlight(line) if n == i else line)
        if not rows:
            out.append("")
            out.append(f"{DIM} no delegations recorded yet. "
                       f"Run one and it appears here.{R}")

        # One write, so the frame arrives as a unit. Each line erases only its own tail,
        # and only the region below the last row is cleared -- so no cell is ever blank
        # between frames, which is what the flicker was.
        frame = HOME + "".join(f"{row}{EOL}\n" for row in out) + BELOW
        sys.stdout.write(f"{SYNC_ON}{frame}{SYNC_OFF}")
        sys.stdout.flush()

        key = wait_key(REFRESH_SECONDS)
        if key in ("q", "esc", "eof"):
            return None
        if key is None or key == "r":
            # Unattended redraw, or `r`. Hold the highlight on the same stream where it
            # survived the rescan, so a list that grows underneath you does not move the
            # row you were about to open.
            here = rows[i]["path"] if rows else None
            rows, trimmed = scan(directory)
            i = next((n for n, row in enumerate(rows) if row["path"] == here),
                     min(i, len(rows) - 1) if rows else 0)
        elif not rows:
            continue
        elif key == "up":
            i = (i - 1) % len(rows)
        elif key == "down":
            i = (i + 1) % len(rows)
        elif key == "enter":
            return rows[i]["path"]


def follow(path: Path) -> None:
    """Print what is already there, then whatever arrives, until you leave.

    Reaching the `end` event deliberately does not return on its own: the last thing a
    dispatch writes is usually the thing you were waiting to read, and yanking the screen
    away at that exact moment is the one behaviour a watcher must not have.
    """
    width = min(os.get_terminal_size().columns, 100)
    print(CLEAR, end="")
    finished = False
    with path.open(encoding="utf-8") as fh:
        while True:
            where = fh.tell()
            line = fh.readline()
            if line.endswith("\n"):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for out in render(event, width):
                    print(out)
                if event.get("t") == "end":
                    finished = True
                    print(f"\n{DIM}(finished — q to return, Ctrl-C to quit){R}")
                continue
            # No newline yet: either nothing new, or the writer is mid-append and this is
            # half a line. Rewind either way -- parsing the half would drop a whole event,
            # because the remainder arrives as its own unparseable fragment.
            fh.seek(where)
            if (key := wait_key(None if finished else POLL_SECONDS)) is None:
                continue
            if key in ("q", "esc", "enter", "eof"):
                return


def main() -> int:
    if not TRANSCRIPT_DIR:
        print("DELEGATE_TRANSCRIPT_DIR is not set, so there is nothing to watch.",
              file=sys.stderr)
        return 2
    directory = Path(TRANSCRIPT_DIR)
    if not directory.is_dir():
        print(f"{TRANSCRIPT_DIR} is not a directory.", file=sys.stderr)
        return 2
    if termios is None:
        print("This needs a POSIX terminal. Run it under WSL, where the server runs.",
              file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("This needs a terminal: it reads single keypresses.", file=sys.stderr)
        return 2
    try:
        with terminal():
            last: Path | None = None
            while (chosen := pick(directory, last)) is not None:
                follow(chosen)
                last = chosen
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped{R}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
