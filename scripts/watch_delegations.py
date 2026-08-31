#!/usr/bin/env python3
"""Pick a delegation from the transcript directory and follow it as it happens.

Run it with no arguments: it lists what is there, newest first, live ones marked. Arrow
keys and Enter choose one; Ctrl-C stops following and exits. Open a second terminal tab
and run it again to watch two at once -- it holds no lock and writes nothing.

Reads the `.jsonl` stream a dispatch appends to while it runs. The `.json` record beside
it is the finished summary and is not what this follows: a record that exists only once
the work is over cannot answer "is it stuck".
"""

from __future__ import annotations

import json
import os
import sys
import termios
import time
import tty
from datetime import datetime
from pathlib import Path

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
    """Wall-clock time of an event, which is what a watcher is actually asking for."""
    if value is None:
        return "--:--:--"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime("%H:%M:%S")
    try:
        return datetime.fromisoformat(value).strftime("%H:%M:%S")
    except ValueError:
        return str(value)[:8]


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


def summarise(path: Path) -> dict:
    """One row for the picker, read cheaply: the head of the file plus its mtime."""
    row = {"path": path, "task": "", "model": "", "turns": 0, "done": False}
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
                elif event.get("t") == "turn":
                    row["turns"] = event.get("turn", row["turns"])
                elif event.get("t") == "end":
                    row["done"] = True
                    row["ok"] = event.get("ok")
    except OSError:
        pass
    row["mtime"] = path.stat().st_mtime if path.exists() else 0
    return row


def _key() -> str:
    """One keypress, raw, so the picker needs no dependency and no Enter after arrows."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":                      # arrow keys arrive as an escape sequence
            return {"A": "up", "B": "down"}.get(sys.stdin.read(2)[-1:], "esc")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def pick(rows: list[dict]) -> Path | None:
    """Newest first, live ones marked. A finished delegation is still worth reading."""
    i = 0
    while True:
        print("\033[2J\033[H", end="")
        print(f"{BOLD}delegations{R} {DIM}· ↑↓ select · enter follow · q quit{R}\n")
        for n, row in enumerate(rows):
            live = not row["done"]
            state = (f"{YELLOW}live{R}" if live
                     else (f"{GREEN}ok{R}" if row.get("ok") else f"{RED}fail{R}"))
            task = row["task"][:60] or "(no task recorded)"
            line = (f" {_clock(row.get('at'))}  {state:<14} "
                    f"{DIM}turn {row['turns']}{R}  {task}")
            print(f"{INVERT}{line}{R}" if n == i else line)
        key = _key()
        if key in ("q", "esc"):
            return None
        if key == "up":
            i = (i - 1) % len(rows)
        elif key == "down":
            i = (i + 1) % len(rows)
        elif key == "enter":
            return rows[i]["path"]


def follow(path: Path) -> None:
    """Print what is already there, then whatever arrives, until Ctrl-C."""
    width = min(os.get_terminal_size().columns, 100)
    print("\033[2J\033[H", end="")
    with path.open(encoding="utf-8") as fh:
        while True:
            where = fh.tell()
            line = fh.readline()
            if not line:
                if line_is_final(path):
                    print(f"\n{DIM}(finished — Ctrl-C to exit){R}")
                    while True:
                        time.sleep(3600)
                fh.seek(where)
                time.sleep(0.3)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            for out in render(event, width):
                print(out)


def line_is_final(path: Path) -> bool:
    try:
        last = path.read_text(encoding="utf-8").rstrip().rsplit("\n", 1)[-1]
        return json.loads(last).get("t") == "end"
    except (OSError, ValueError, IndexError):
        return False


def main() -> int:
    if not TRANSCRIPT_DIR:
        print("DELEGATE_TRANSCRIPT_DIR is not set, so there is nothing to watch.",
              file=sys.stderr)
        return 2
    directory = Path(TRANSCRIPT_DIR)
    if not directory.is_dir():
        print(f"{TRANSCRIPT_DIR} is not a directory.", file=sys.stderr)
        return 2
    streams = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                     reverse=True)[:25]
    if not streams:
        print("No delegation streams yet. Run one and try again.", file=sys.stderr)
        return 1
    rows = [summarise(p) for p in streams]
    try:
        chosen = pick(rows)
        if chosen is not None:
            follow(chosen)
    except KeyboardInterrupt:
        print(f"\n{DIM}stopped{R}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
