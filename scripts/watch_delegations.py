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
QUEUED_EVERY = 60.0      # how often a delegation that is only queued is worth repeating

# Below this, a reply's repeated lines are ordinary prose restating itself and saying so
# on every turn would be noise. Deliberately well above what healthy passes measure and
# well below what looping ones do -- observed under 1% against 20-94%, a gap wide enough
# that the exact threshold does not have to be defended. It governs a *line on a screen*,
# not a control: nothing is aborted on it, so being a little wrong costs a reader one
# glance rather than a killed delegation.
REPETITION_WORTH_SAYING = 0.15

# Rate sources whose `requests_running` is the concurrency this delegation was *priced*
# for, frozen at lease grant, rather than a reading of the cluster. Named rather than
# inferred from "not cluster_since_boot", so that a source nobody has taught this viewer
# about keeps the neutral wording instead of silently acquiring a meaning.
PRICED_SOURCES = frozenset({"observed_at_concurrency", "own_turns"})


# The state column, its two-space gutter included. Ten is what the widest state costs:
# `_ago` truncates, so seconds stop at `59s` and minutes run to `89m` before the hour unit
# takes over, and `queued ` is the longest prefix -- `queued 89m`. Python's `<` pads and
# never cuts, so a column set too narrow does not truncate, it silently shoves every column
# to its right out of line on exactly the rows a reader is scanning the list to find.
STATE_WIDTH = 12

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
    """One row, selected across its whole width, keeping the colours it came with.

    A row carries its own colour/reset pairs, and a reset ends the selection as surely as
    it ends a dim -- so wrapping the coloured string selected only as far as the first
    reset, two columns in. The old fix was to strip every escape first, which worked and
    cost the state column its colour: the one cue that says at a glance whether a row is
    live, queued or failed went monochrome exactly on the row being looked at. Re-opening
    the selection after each reset keeps both the full width and the colour.

    Dim inverse rather than plain inverse, so the selected row reads as a band rather than
    a flash. Inverse at all because it is the one selection style every terminal renders
    the same way -- a chosen background colour is legible on one theme and invisible on
    another, and this has no way to ask which it is on.
    """
    body = line.replace(R, R + SELECT)
    return f"{SELECT}{body}{R}"

R = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
INVERT = "\033[7m"
# The selected row. Dim inverse: a band rather than a flash, and inverse at all because it
# is the one selection style that renders the same on every terminal theme.
SELECT = "\033[2;7m"


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
    # Eight characters, which is the column. The pair reads as one word plus its
    # constraint, the same way `delegate_readonly` does beside `delegate`.
    "delegate_to_agent_readonly": "agent-ro",
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


# The outcomes `_run_calls` actually produces. `ok` was not among them and was the only
# thing this renderer compared against, so every successful call was painted red with the
# word `ran` beside it -- a check that could not pass, next to three that could not fire.
_GOOD_OUTCOMES = frozenset({"ran", "repeat"})


def _size_note(call: dict) -> str:
    """How much a call returned, and the exit code if a process produced one.

    Absent rather than zero when the record carries no accounting, because a call that
    returned nothing and a call whose size was never recorded are different facts and `0 B`
    would claim the first.
    """
    bits: list[str] = []
    if isinstance(size := call.get("result_bytes"), int):
        rows = call.get("result_lines")
        rows_note = f" · {rows} line{'' if rows == 1 else 's'}" if isinstance(rows, int) else ""
        bits.append(f"{size:,} B{rows_note}")
    if isinstance(code := call.get("exit_code"), int):
        bits.append(f"exit {code}")
    return " · ".join(bits)


INDENT = "      "     # where a tool call's own lines start, under the `▸` marker


def _alone(text: str, colour: str, width: int) -> list[str]:
    """One value on lines of its own, sharing them with nothing else.

    **The honest limit**: a path or a pattern longer than the terminal still occupies
    more than one row. `_wrap` breaks on word boundaries and a path is one word, so it
    is emitted over-long and the terminal folds it where it likes. The promise these
    layouts make is not that a path never wraps -- it is that a path never *shares*, so
    whatever a reader sees on the rest of that row belongs to the same value.
    """
    return [f"{colour}{line}{R}" for line in _wrap(text, width, INDENT)]


def _rest(arguments: dict, *named: str) -> list[str]:
    """Every argument a layout did not name, still as `k=v`.

    A layout that knew about three keys and silently dropped a fourth would be lying
    about what the call was asked. `search_files` alone carries two more -- `glob` and
    `max_results` -- and a search narrowed to `*.py` that found nothing reads very
    differently from one that was not narrowed at all.
    """
    return [f"{k}={v}" for k, v in arguments.items() if k not in named]


def _read_file_lines(call: dict, width: int) -> list[str]:
    """`read_file`: the path, then the range asked for and the size returned."""
    args = call.get("arguments") or {}
    out = _alone(str(args.get("path", "?")), CYAN, width)
    # Optional arguments, so they are collected rather than positioned: a missing
    # `end_line` must not leave behind the separator that would have followed it.
    bits = [f"{label} {args[key]}" for key, label in
            (("start_line", "start"), ("end_line", "end")) if args.get(key) is not None]
    bits += _rest(args, "path", "start_line", "end_line")
    # `_size_note` owns the absent-versus-zero rule for these counts, so this joins its
    # answer rather than reading `result_bytes` and `result_lines` a second time.
    bits += [note] if (note := _size_note(call)) else []
    return out + ([f"{INDENT}{DIM}{' · '.join(bits)}{R}"] if bits else [])


def _search_files_lines(call: dict, width: int) -> list[str]:
    """`search_files`: the path, the pattern, then what came back.

    Two lines rather than one because they are the two halves of the question, and the
    generic layout put them on the same line whenever they happened to fit -- which is
    the report this exists to answer: `pattern=` running straight on from `path=`, with
    nothing to say where one ended.
    """
    args = call.get("arguments") or {}
    out = (_alone(str(args.get("path", "?")), CYAN, width)
           + _alone(str(args.get("pattern", "?")), BOLD, width))
    bits = _rest(args, "path", "pattern")
    bits += [note] if (note := _size_note(call)) else []
    return out + ([f"{INDENT}{DIM}{' · '.join(bits)}{R}"] if bits else [])


# The tools worth laying out by hand, and the only ones. Everything else keeps the
# generic `k=v` tail: a per-tool layout is a claim about which of a call's arguments a
# reader looks at first, and that claim is only worth making where someone has looked.
_LAYOUTS = {"read_file": _read_file_lines, "search_files": _search_files_lines}


def _call_lines(call: dict, width: int) -> list[str]:
    """One tool call: what it was asked, how it ended, and why if it refused.

    The refusal message gets its own wrapped lines and is never trimmed to fit. That is the
    whole point of the record -- a delegation reporting one error across twelve `read_git`
    calls could not be diagnosed at all before it existed -- so if anything has to give on
    a narrow terminal it is the argument list, which drops to its own line rather than
    being cut.

    `▸ name` stays yellow whichever layout runs, because that marker is what a reader
    scans for down the left of a turn, and a layout that recoloured it would cost them
    the scan to gain a line.
    """
    outcome = str(call.get("outcome") or "error")
    colour = GREEN if outcome in _GOOD_OUTCOMES else RED
    head = f"  {YELLOW}▸ {call.get('name', '?')}{R} {colour}{outcome}{R}"
    out = [head]
    if layout := _LAYOUTS.get(str(call.get("name") or "")):
        out.extend(layout(call, width))
    else:
        tail = "  ".join(
            part
            for part in (
                "  ".join(f"{k}={v}" for k, v in (call.get("arguments") or {}).items()),
                _size_note(call),
            )
            if part
        )
        if tail and len(_plain(head)) + len(tail) + 2 <= width:
            out = [f"{head}  {DIM}{tail}{R}"]
        elif tail:
            out.extend(f"{DIM}{line}{R}" for line in _wrap(tail, width, INDENT))
    if message := str(call.get("message") or "").strip():
        # Red, like the outcome beside it. The word `error` was coloured and the reason for
        # it was not, which is the wrong way round: the outcome is one token a reader can
        # find anywhere on the line, and the message is the thing they came for. Blank
        # lines inside a wrapped refusal are left bare rather than wrapped in codes that
        # colour nothing.
        out.extend(
            f"{RED}{line}{R}" if line.strip() else line
            for line in _wrap(message, width, INDENT)
        )
    return out


def _span(seconds: float) -> str:
    """A duration inside a sentence, where `_duration`'s floor reads as a missing number.

    `_duration` rounds for the picker's column of totals, so a tool that returned in 140ms
    renders as `0s` -- which reads as a placeholder for a figure nobody recorded rather
    than as the measurement it is. Its rounding is right there and wrong here, so this
    says "under a second" instead of changing it for every other caller.
    """
    return _duration(seconds) if seconds >= 1 else "<1s"


def _turn_tool_ms(event: dict) -> int | None:
    """How long a turn spent in tools, in ms, or None when it cannot be said.

    The sum of the calls' own `ms`, which never includes the dispatch's bookkeeping. A
    transcript older than that field falls back to the turn less its backend call, gated
    on the calls and clamped at zero so clock skew reads as "none", not a negative.
    """
    calls = event.get("tool_calls") or []
    measured = [c.get("ms") for c in calls if isinstance(c.get("ms"), int)]
    if measured:
        return sum(measured)
    ms, gen = event.get("ms"), event.get("backend_ms")
    if calls and isinstance(ms, (int, float)) and isinstance(gen, (int, float)):
        return int(max(ms - gen, 0))
    return None


def _turn_timings(event: dict) -> str:
    """Where a turn's wall clock went: all of it, the tools' share, the cluster's.

    Three named durations rather than one number and a conditional contrast. The older
    form printed the wall clock and then "of <backend>", which on a turn that ran no
    tools was the same duration twice joined by a word implying they differed, and on
    one that did ran the two together without saying which was which. Naming each is
    longer and says what it means.

    `tools` is shown only where a turn actually called one -- gated on the calls rather
    than on the arithmetic, so a turn whose tools were instant still says it ran them,
    and one that ran none says nothing rather than "0s tools".
    """
    ms, gen = event.get("ms"), event.get("backend_ms")
    parts = []
    if isinstance(ms, (int, float)):
        parts.append(f"{_duration(ms / 1000)} total")
    tool_ms = _turn_tool_ms(event)
    if tool_ms is not None:
        parts.append(f"{_span(tool_ms / 1000)} tools")
    if isinstance(gen, (int, float)):
        parts.append(f"{_duration(gen / 1000)} generating")
    return f"  {DIM}{' · '.join(parts)}{R}" if parts else ""


def _turn_lines(event: dict, stamp: str, width: int) -> list[str]:
    """One completed turn: what it cost, what it ran at, and what it said.

    Its own function because the branch outgrew `render`, which is a dispatch and
    should read like one.
    """
    n = event.get("turn", "?")
    # What it cost, then how fast, then where the time went -- in that order, because a
    # reader scanning a transcript asks those three questions in that order and the last
    # is the one they only ask when a number above it looked wrong.
    cost = (f"{DIM}{_tokens(event.get('input_tokens'))} in · "
            f"{_tokens(event.get('output_tokens'))} out{R}")
    # Generation rate over the backend call, which is the figure that says whether the
    # cluster is slow. The turn's own wall clock includes tool execution, so a rate
    # taken from it would blame the cluster for time it did not spend generating.
    if isinstance(rate := event.get("out_tok_s"), (int, float)):
        cost += f"  {GREEN}{rate:g} tok/s{R}"
    cost += _turn_timings(event)
    # The effort this turn actually ran at, which is not always the one that was asked
    # for: empty-answer recovery steps the level down and retries, so a delegation
    # requested at `high` can answer at `low` and the header would still say `high`.
    # The requested level stays where it was -- the start event above, and the picker's
    # own column -- so the two are readable side by side rather than one hiding the
    # other.
    # "turn 3" says where this is; "of 10" says whether that is near the end. The budget
    # rides on the turn event as well as the head of the stream, because a reader
    # scrolling a long transcript is not looking at the header any more.
    of = event.get("of_turns")
    whose = f"turn {n} of {of}" if isinstance(of, int) else f"turn {n}"
    head = f"{stamp}  {BOLD}{CYAN}{whose}{R}"
    if effort := event.get("effort"):
        head += f"  {DIM}effort {effort}{R}"
    # Shown only above one, because that is the whole signal. `attempts` is the reason
    # an effort differs from the requested one, and a turn that answered first time
    # saying "1 attempt" would be noise on every line of every transcript.
    if isinstance(tries := event.get("attempts"), int) and tries > 1:
        head += f"  {YELLOW}{tries} attempts{R}"
    # Shown only when it is a signal, for the same reason `attempts` is. A turn looping
    # inside itself is invisible in every other number on this line -- it fills its
    # ceiling at a length stop exactly as a long answer does -- and the person watching
    # is the one who can act on it. Absent is not zero: an older transcript carries no
    # share, and printing 0% would assert that a run nobody can re-measure did not loop.
    share = event.get("duplicate_line_share")
    if isinstance(share, (int, float)) and share >= REPETITION_WORTH_SAYING:
        head += f"  {YELLOW}{share:.0%} repeated{R}"
    lines = ["", f"{head}  {cost}"]
    for call in event.get("tool_calls", []) or []:
        lines.extend(_call_lines(call, width))
    if text := (event.get("text") or "").strip():
        lines.append("")
        lines.extend(_wrap(text, width, "  "))
    return lines


def _alive_line(event: dict) -> str:
    """One dim line, no rule. Since ADR-0072 it can say that something *is* happening.

    Worth knowing during a one-shot, where nothing else lands between start and end, and
    still worth not shouting about.
    """
    secs = event.get("elapsed_seconds")
    of = event.get("of_seconds")
    spent = f"{secs / 60:.0f}m" if isinstance(secs, (int, float)) and secs >= 90 else (
        f"{secs:.0f}s" if isinstance(secs, (int, float)) else "?")
    budget = f" of {of // 60}m" if isinstance(of, int) else ""
    # The countdown is what a reader is actually asking for. `of` is the delegation
    # ceiling and is rarely what ends a run, so a heartbeat carrying only that reads as
    # enormous headroom right up to the moment a tighter deadline fires.
    left = event.get("ends_in_seconds")
    ends = ""
    if isinstance(left, (int, float)):
        ends = (f", ends in {left / 60:.0f}m" if left >= 90
                else f", ends in {left:.0f}s")
    # What streaming made knowable. "chunks" rather than "tokens" because that is what was
    # counted -- a frame usually carries one token on this stack and is not promised to.
    # The gap since the last is the half that separates a delegation generating from one
    # that has gone quiet, so it appears whenever it is long enough to mean anything.
    seen = event.get("chunks_seen")
    reasoning = event.get("reasoning_chunks")
    since = event.get("since_chunk_seconds")
    flow = ""
    if isinstance(seen, int) and seen > 0:
        flow = f", {seen:,} chunks"
        # The split, when the stream carries it. A watcher's question is whether the model
        # is thinking or answering, and the total cannot answer it; the two halves can.
        # Absent is not zero: a transcript written before the split existed must not read
        # as a run that reasoned not at all.
        if isinstance(reasoning, int):
            flow += f" ({reasoning:,} thinking, {seen - reasoning:,} answering)"
        if isinstance(since, (int, float)) and since >= 2:
            flow += f" (last {since:.0f}s ago)"
    return f"{DIM}still running · {spent}{budget}{ends}{flow}{R}"


def _waiting_line(event: dict) -> str:
    """Queued at the admission gate, having reached no backend at all.

    Its own line rather than a variant of `alive`, because the two say different things:
    one is the model working, the other is this server not having started.
    """
    waited = event.get("waited_seconds")
    of = event.get("of_seconds")
    spent = _ago(waited) if isinstance(waited, (int, float)) else "?"
    cap = f" of {of // 60}m" if isinstance(of, int) else ""
    return f"{DIM}queued at the gate · {spent}{cap}{R}"


# The events that render as one dim line with no rule. Kept as a table so `render` does
# not grow a branch and a return for each.
_ONE_LINERS = {"waiting": _waiting_line, "alive": _alive_line}


def _end_head(event: dict) -> str:
    """The closing line: how much of the budget went, over how long, at what cost.

    Extracted for the reason `_turn_lines` is -- `render` is a dispatcher, and a branch
    that grew to a dozen statements stops reading as one arm of a choice.
    """
    verdict = f"{GREEN}done{R}" if event.get("ok") else f"{RED}failed{R}"
    secs = event.get("elapsed_seconds")
    n_turns = event.get("turns")
    # Against the budget where the stream carries one: "6 turns" and "6 of 6 turns"
    # describe the same run and are different news, and only the second says whether
    # the cap is what ended it.
    budget = event.get("max_turns")
    used = f"{n_turns if n_turns is not None else '?'}"
    if isinstance(n_turns, int) and isinstance(budget, int):
        used = f"{n_turns} of {budget}"
    tail = f"{DIM}{used} turn"
    tail += "" if n_turns == 1 else "s"
    tail += f" · {_duration(secs)}" if isinstance(secs, (int, float)) else ""
    tail += R
    if isinstance(rate := event.get("out_tok_s"), (int, float)):
        tail += f"  {GREEN}{rate:g} tok/s{R}"
    # Both directions, so the closing line can answer "what did this cost" on its own.
    # It reported only `out`, which is the half a reader is least likely to be asking
    # about on a delegation that was handed ninety thousand tokens.
    for value, label in ((event.get("input_tokens"), "in"),
                         (event.get("output_tokens"), "out")):
        if isinstance(value, int):
            tail += f"{DIM} · {_tokens(value)} {label}{R}"
    return f"{BOLD}{verdict}{R}  {tail}"


def _end_counts(event: dict) -> list[str]:
    """What the run did, and how much of it went wrong.

    `tool_calls` is an integer here and a list of call records on a `turn` event -- the
    same key, two shapes, because the loop reports a total where the turn reports its
    calls. Every read below is type-guarded rather than truth-tested for that reason: a
    truthy `tool_calls` on the wrong event shape would format a list into this line.
    """
    out: list[str] = []
    if isinstance(calls := event.get("tool_calls"), int):
        out.append(f"{calls} tool call{'' if calls == 1 else 's'}")
    # Only where a shell actually ran. A measured zero is a real fact, but `0 shell` on
    # every read-only delegation is noise, and nothing here rests on telling that zero
    # from an absent field -- `tool_calls` beside it already says whether anything ran.
    if isinstance(shells := event.get("bash_calls"), int) and shells:
        out.append(f"{shells} shell")
    errors, bad_shell = event.get("tool_errors"), event.get("bash_failures")
    distinct = event.get("failed_calls")
    if isinstance(distinct, int) or (isinstance(errors, int) and isinstance(bad_shell, int)):
        # `failed_calls` counts each failing call once. The two counters beside it overlap
        # on every shell call that exits non-zero -- it is an error and a shell failure at
        # once -- so their sum showed one failed command as "2 failures". The sum is kept
        # only for a transcript written before `failed_calls` existed, where it errs high:
        # a count that flags one twice beats one that hides one. Both halves are required
        # there rather than defaulted, since adding an absent half as zero would be the
        # absent-is-not-zero mistake the rest of this renderer goes to trouble to avoid.
        failed = distinct if isinstance(distinct, int) else errors + bad_shell
        word = f"{failed} failure{'' if failed == 1 else 's'}"
        # The one thing that breaks the dimness, and only when there is something to
        # break it for. Dim and red combine into a dim red rather than replacing each
        # other, so the dim is closed first and reopened after -- the same trick
        # `_highlight` uses on the row it selects.
        out.append(f"{R}{RED}{word}{R}{DIM}" if failed else word)
    return out


def _end_timings(event: dict) -> list[str]:
    """Where the whole run's wall clock went, in the words a turn already uses.

    Nothing at all unless the backend's own halves were recorded. `elapsed_seconds` is on
    every transcript ever written and already heads the closing line above, so a summary
    carrying it alone would be the same number a second time with nothing to divide it
    between -- and the division is the only reason this line exists.
    """
    total = event.get("elapsed_seconds")
    prefill, decode = event.get("prefill_seconds"), event.get("decode_seconds")
    halves = ((prefill, "prefill"), (decode, "generating"))
    if not any(isinstance(value, (int, float)) for value, _ in halves):
        return []
    out = [f"{_span(total)} total"] if isinstance(total, (int, float)) else []
    # Read, never derived. `elapsed - (prefill + decode)` looks like the same number and is
    # not: the turn clock starts before the admission gate, so that subtraction carries the
    # queue and the server's bookkeeping as well, and calling the result "tools" overstates
    # it by however long the run waited for a slot. `tool_seconds` is timed from the grant.
    # Gated on the calls like `_turn_timings` above, so a dispatch that ran none says
    # nothing rather than "0s tools" -- and so a transcript written before the server
    # stopped charging its own bookkeeping to the bucket cannot show it here either.
    if isinstance(tools := event.get("tool_seconds"), (int, float)) and event.get(
        "tool_calls"
    ):
        out.append(f"{_span(tools)} tools")
    out += [f"{_span(value)} {label}" for value, label in halves
            if isinstance(value, (int, float))]
    return out


def _end_summary(event: dict) -> list[str]:
    """The whole run on one line beneath the verdict: what it ran, and where its time went.

    A second line rather than more of the first. The closing line answers "did it work and
    what did it cost"; this answers "what did it spend that on", which is the question a
    reader only asks once the first answer looked wrong -- the same ordering `_turn_timings`
    follows one level down.

    Every field is absent on a transcript written before the loop reported it, and absent
    is not zero: an unmeasured run must not read as a run that called no tools in no time.
    So each part appears only where its own field does, and a stream carrying none of them
    gets no line at all rather than a row of zeroes.

    A list rather than a string, so `render` extends with it and stays a dispatcher: the
    empty case is "nothing to add" rather than a branch on the way past.
    """
    parts = _end_counts(event) + _end_timings(event)
    return [f"  {DIM}{' · '.join(parts)}{R}"] if parts else []


def render(event: dict, width: int) -> list[str]:
    """One event, as a block a person reads rather than a line a machine parses."""
    kind = event.get("t")
    stamp = f"{DIM}{_clock(event.get('at'))}{R}"

    if kind == "start":
        model = event.get("model_key", "?")
        effort = event.get("effort") or "default"
        agent = event.get("agent")
        who = f"{event.get('tool', 'delegate')}" + (f" · {agent}" if agent else "")
        turns = event.get("max_turns")
        budget = f" · {turns} turns" if isinstance(turns, int) else ""
        head = (f"{stamp}  {BOLD}{BLUE}{who}{R} "
                f"{DIM}· {model} · effort {effort}{budget}{R}")
        return ["", f"{DIM}{'─' * width}{R}", head,
                *_wrap(event.get("task", ""), width, "          "),
                *_given(event),
                f"{DIM}{'─' * width}{R}"]

    if kind == "turn":
        return _turn_lines(event, stamp, width)

    if kind == "priced":
        # What the turn below was allowed, and what that was worked out from. Shown
        # because a turn that dies at a deadline prints nothing else: the ceiling and the
        # load it was read against are the two numbers that say whether the budget was
        # ever payable, and they are meaningless apart.
        cap = event.get("budget_ceiling")
        rate = event.get("decode_rate")
        running = event.get("requests_running")
        source = event.get("rate_source")
        n = event.get("turn")
        # "of 25" is the half that says whether this turn is anywhere near the last, and
        # so whether the answer to a delegation running long is to raise the cap. Omitted
        # when absent rather than guessed: an older stream carries no budget.
        of = event.get("of_turns")
        whose = f"turn {n}" if isinstance(n, int) else "the turn below"
        if isinstance(n, int) and isinstance(of, int):
            whose = f"turn {n} of {of}"
        cap_s = f"{cap:,} tok" if isinstance(cap, int) else "uncapped"
        rate_s = f"{rate:.1f} tok/s" if isinstance(rate, (int, float)) else "rate unknown"
        load_s = ""
        if isinstance(running, (int, float)):
            # Three cases, not two, and the third is the one a two-way split gets wrong.
            # Only `cluster_since_boot` carries a reading of the machine. Every other
            # *known* source echoes the concurrency frozen at lease grant, which is a
            # claim about this delegation's pricing -- and `own_turns`, the commonest of
            # all at five of six rows on a real six-turn run, used to fall through to the
            # neutral word and invite the cluster reading. A source that is absent or
            # unrecognised still gets that neutral word, because a stream written before
            # the field existed cannot support either claim.
            if source == "cluster_since_boot":
                load_s = f", {running:.0f} running"
            elif source in PRICED_SOURCES:
                load_s = f", priced for {running:.0f}"
            else:
                load_s = f", concurrency {running:.0f}"
        return ["", f"{stamp}  {DIM}budget for {whose}: {cap_s} · {rate_s}{load_s}{R}"]

    if kind in _ONE_LINERS:
        return [f"{stamp}  {_ONE_LINERS[kind](event)}"]

    if kind == "end":
        lines = ["", f"{stamp}  {_end_head(event)}"]
        lines.extend(_end_summary(event))
        # A truncated reply is a *successful* dispatch -- `ok` is true and nothing raised --
        # so "done" is the one word that reads most wrongly about it. Said on its own line,
        # with the reason, because the failure worth catching here is a reader treating a
        # reply that stopped mid-sentence as the whole answer.
        if why := _TRUNCATED.get(str(event.get("finish_reason") or "")):
            lines.append(f"  {YELLOW}cut off: {why}{R}")
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


# The events that say where a delegation has got to. Ordered by nothing -- only the last
# one written matters, and only for telling "still queued" from "ran, then went silent".
# Wire `finish_reason` values that mean the reply stopped before the model was done, and
# what each one tells a reader to do about it. Values outside this table -- `stop`, and
# `tool_calls` -- are ordinary completions and say nothing. A table rather than a truth
# test, because "it was cut off" without "by what" sends the reader to the wrong fix: a
# token limit is a budget to raise, a content filter is not.
_TRUNCATED = {
    "length": "the reply hit its token limit. Raise max_tokens, or split the task.",
    "content_filter": "the endpoint stopped the reply itself. Not a budget problem.",
}

_SIGNALS = frozenset({"waiting", "priced", "turn", "alive", "end"})


def summarise(path: Path) -> dict:
    """One row for the picker, read cheaply: the head of the file plus its mtime."""
    row = {"path": path, "task": "", "model": "", "turns": 0, "done": False,
           "last_signal": "", "waited_seconds": None,
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
                if not isinstance(event, dict):
                    continue  # JSON, but not an event: a hand-edited or truncated file
                # Which of the recurring events came last, which is what separates a
                # delegation still queued from one that got a slot and then went quiet.
                # A flag set by `waiting` alone would stay set for the rest of the run.
                if event.get("t") in _SIGNALS:
                    row["last_signal"] = event["t"]
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
                elif event.get("t") == "waiting":
                    row["waited_seconds"] = event.get("waited_seconds")
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
    """How stale something is, rounded hard."""
    if seconds < 60:
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
    # Queued is a fact the server wrote down, not silence this reader interpreted. It is
    # reported however long the wait has run: a delegation parked at the gate is not
    # getting anywhere, but it is not a candidate for having died either, and `quiet`
    # said both of those at once by saying neither. (ADR-0072)
    if row.get("last_signal") == "waiting":
        waited = row.get("waited_seconds")
        age = _ago(waited if isinstance(waited, (int, float)) else idle)
        return f"queued {age}", CYAN
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
            f" {'started':<8}  {'duration':<8}  {'effort':<6}  {'state':<{STATE_WIDTH}}"
            f"{'kind':<8} {'turns':>5}  {'cached':>6}  {'reuse':>5}  "
            f"{'return':>6}  {'load':>6}  task"
        )
        out.append(f"{DIM}{head_cols}{R}")
        for n, row in enumerate(rows):
            word, colour = state_of(row)
            task = row["task"][:60] or "(no task recorded)"
            # Pad the plain word, then colour it. Padding the coloured string counts the
            # escape bytes as width and the column stops lining up.
            state = f"{colour}{word:<{STATE_WIDTH}}{R}"
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
            line = (f" {_clock(started_at(row))}  {spent}  {effort}  {state}"
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


class QueuedPacing:
    """How often a delegation that is only queued is worth saying so again."""

    def __init__(self, every: float = QUEUED_EVERY) -> None:
        self.every = every
        self._painted: float | None = None
        self._held: dict | None = None

    def admit(self, event: dict) -> list[dict]:
        """What to print for this event: nothing, itself, or a held line then itself."""
        if event.get("t") != "waiting":
            held, self._held, self._painted = self._held, None, None
            return [held, event] if held is not None else [event]
        waited = event.get("waited_seconds")
        waited = float(waited) if isinstance(waited, (int, float)) else 0.0
        if self._painted is None or waited - self._painted >= self.every:
            self._painted, self._held = waited, None
            return [event]
        self._held = event
        return []


def follow(path: Path) -> None:
    """Print what is already there, then whatever arrives, until you leave.

    Reaching the `end` event deliberately does not return on its own: the last thing a
    dispatch writes is usually the thing you were waiting to read, and yanking the screen
    away at that exact moment is the one behaviour a watcher must not have.
    """
    try:
        _follow(path)
    except BrokenPipeError:
        # Piped to a program that exited, `| head` being the usual one. Nothing is left to
        # show anything to, so stop without a traceback; stdout goes to the null device so
        # the interpreter's own flush at exit does not raise the same error again.
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # for the rest of the process


def _follow(path: Path) -> None:
    # `shutil` rather than `os.get_terminal_size()`, which raises when stdout is a pipe:
    # `watch_delegations.py | tee log` checked only that stdin was a terminal and crashed.
    width = min(shutil.get_terminal_size((100, 24)).columns, 100)
    print(CLEAR, end="")
    finished = False
    pacing = QueuedPacing()
    with path.open(encoding="utf-8") as fh:
        while True:
            where = fh.tell()
            line = fh.readline()
            if line.endswith("\n"):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                for shown in pacing.admit(event):
                    for out in render(shown, width):
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
