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
import select
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

WINDOW_DAYS = 7          # how far back the list reaches
MAX_ROWS = 200           # backstop under the window, for a runaway day
REFRESH_SECONDS = 2.0    # unattended redraw of the list
POLL_SECONDS = 0.3       # how often a live stream is checked for new lines
STALL_SECONDS = 120      # silence after which an unfinished stream stops claiming "live"

# Home, then erase forward. `ESC[2J` and `ESC[3J` both cost scrollback in some terminals,
# and scrollback is how you read back over a transcript you have just watched.
CLEAR = "\033[H\033[0J"

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
    "delegate_batch": "batch",
}


def kind_of(row: dict) -> str:
    """What kind of call this was, in one word narrow enough for a column.

    Two facts, one column, because only one of them is ever a surprise: `readonly` is a
    one-shot by construction, so the shape is worth naming only when a `delegate` was
    given no tools and quietly ran as one.

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
           "tool": "", "tools": None, "created": created_at(path)}
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
                    # Absent and empty are kept apart on purpose: `kind_of` reports the
                    # first as unknown and the second as a one-shot.
                    row["tools"] = event.get("tools")
                elif event.get("t") == "turn":
                    row["turns"] = event.get("turn", row["turns"])
                elif event.get("t") == "end":
                    row["done"] = True
                    row["ok"] = event.get("ok")
    except OSError:
        pass
    row["mtime"] = path.stat().st_mtime if path.exists() else 0
    return row


_CACHE: dict[Path, tuple[tuple[float, int], dict]] = {}


def scan(directory: Path) -> tuple[list[dict], int]:
    """Every stream inside the window, newest start first, plus how many were trimmed.

    The window is applied from the filename, before anything is opened. That is the point:
    the list redraws unattended every couple of seconds, `summarise` reads a whole file,
    and this workspace lives on `/mnt/c` where that is roughly 12x the cost it would be on
    ext4 (ADR-0020). Unchanged files are then served from `_CACHE`, so a quiet refresh
    stats each candidate and reads none of them.
    """
    cutoff = time.time() - WINDOW_DAYS * 86400
    paths = [p for p in directory.glob("*.jsonl") if created_at(p) >= cutoff]
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


def pick(directory: Path) -> Path | None:
    """Newest first, live ones marked. A finished delegation is still worth reading."""
    rows, trimmed = scan(directory)
    i = 0
    while True:
        print(CLEAR, end="")
        head = f"{BOLD}delegations{R} {DIM}· ↑↓ select · enter follow · r refresh · q quit"
        head += f" · last {WINDOW_DAYS} days"
        head += f", newest {MAX_ROWS} of {len(rows) + trimmed}" if trimmed else ""
        print(f"{head}{R}")
        print(f"{DIM} started   last      state        kind      turns  task{R}")
        for n, row in enumerate(rows):
            word, colour = state_of(row)
            task = row["task"][:60] or "(no task recorded)"
            # Pad the plain word, then colour it. Padding the coloured string counts the
            # escape bytes as width and the column stops lining up.
            state = f"{colour}{word:<11}{R}"
            kind = f"{DIM}{kind_of(row):<8}{R}"
            line = (f" {_clock(started_at(row))}  {_clock(row['mtime'])}  {state} "
                    f"{kind} {DIM}{row['turns']:>5}{R}  {task}")
            print(f"{INVERT}{line}{R}" if n == i else line)
        if not rows:
            print(f"\n{DIM} nothing in the last {WINDOW_DAYS} days. "
                  f"Run a delegation and it appears here.{R}")

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
            while (chosen := pick(directory)) is not None:
                follow(chosen)
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped{R}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
