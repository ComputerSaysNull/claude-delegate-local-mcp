"""The `wait-hook` subcommand: poll a delegation's state file and wake the caller.

`delegate` answers with a handle at once, and a caller that ends its turn without holding a
`collect` or `answer` open hears nothing more until the person writes (ADR-0126). Claude
Code's `asyncRewake` hook runs this module in the background; when it exits with code 2 its
stderr wakes the model, so a run that finished or asked gets its caller back.

It must start fast and never disturb the protocol: this module imports no server or loop
state, only `slots` for the runtime directory, and any failure returns 0 rather than raising.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import slots
from .handles import pid_alive

# A `collect` already waiting returns within milliseconds, so this long is genuinely unheard.
GRACE_SECONDS = 3.0

# One cheap file read a second is plenty for a background hook.
POLL_SECONDS = 1.0


def handle_from_hook(payload: dict) -> str | None:
    """The `handle` of a delegation the hook should watch, or None.

    `payload["tool_response"]` is the delegating tool's result, which reaches the hook as
    JSON text, sometimes already a dict, and sometimes as a list of content blocks whose
    `"text"` member holds that JSON. A result that is finished or cancelled is not worth
    waking anyone for, and neither is one that carries no handle.
    """
    result = _result_dict(payload.get("tool_response"))
    if result is None:
        return None
    handle = result.get("handle")
    if not isinstance(handle, str) or not handle.startswith("d-"):
        return None
    if result.get("status") in ("done", "cancelled"):
        return None
    return handle


def decide(state: dict | None, *, pid_alive: bool, held_for: float) -> tuple[str, str] | None:
    """What the wait-hook should do about a state it just read.

    `("exit", "")` ends the hook quietly -- no state, the server is gone, or the run
    finished and was already collected. `("wake", message)` exits 2 with `message` once the
    condition has held for `GRACE_SECONDS`. None means keep waiting.
    """
    if state is None or not pid_alive:
        return ("exit", "")
    status = state.get("status")
    collected = state.get("collected", False)
    handle = state.get("handle", "")
    if status in ("done", "cancelled") and collected:
        return ("exit", "")
    if status in ("done", "cancelled"):
        if held_for >= GRACE_SECONDS:
            return ("wake", (f"A delegation finished and nobody has collected it: call "
                             f"`collect` with handle {handle}."))
        return None
    if (state.get("questions_asked", 0) > state.get("questions_shown", 0)
            and held_for >= GRACE_SECONDS):
        return ("wake", (f"A delegation is waiting on a question nobody has read: call "
                         f"`collect` with handle {handle} to read it, then `answer`."))
    return None


def wait(handle: str, state_dir: Path, *, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic,
         alive: Callable[[int], bool] = pid_alive) -> tuple[int, str]:
    """Poll the handle's state file, and report how the hook should exit.

    Returns `(0, "")` to exit quietly or `(2, message)` to wake the caller with `message`
    on stderr. Only one waiter per handle: whoever holds the `.waiter` file (and is alive)
    is the one that wakes, and anyone else returns at once so a fan-out of hooks does not
    each start its own wait.
    """
    waiter = state_dir / f"{handle}.waiter"
    if not _claim_waiter(waiter, alive):
        return (0, "")
    try:
        path = state_dir / f"{handle}.json"
        held_since: float | None = None
        while True:
            state = _read_state(path)
            pid = state.get("pid") if state is not None else None
            pid_alive = isinstance(pid, int) and alive(pid)
            now = clock()
            if _wake_key(state, pid_alive) is None:
                held_since = None
            elif held_since is None:
                held_since = now
            decision = decide(state, pid_alive=pid_alive,
                              held_for=0.0 if held_since is None else now - held_since)
            if decision is not None:
                return (0, "") if decision[0] == "exit" else (2, decision[1])
            sleep(POLL_SECONDS)
    finally:
        _release_waiter(waiter)


def main(stdin_text: str) -> int:
    """The `wait-hook` entrypoint: read the hook payload, poll, wake or exit quietly.

    Never raises: a hook that crashes must not wake anyone, and it has no protocol to
    corrupt, so a failure is simply a quiet exit.
    """
    try:
        payload = json.loads(stdin_text)
        if not isinstance(payload, dict):
            return 0
        handle = handle_from_hook(payload)
        if handle is None:
            return 0
        state_dir = slots.default_dir_if_available()
        if state_dir is None:
            return 0
        code, message = wait(handle, state_dir / "handles")
        if code == 2:
            print(message, file=sys.stderr)
        return code
    except Exception:
        return 0


def _result_dict(raw: Any) -> dict | None:
    """The delegation-result dict, from JSON text, a dict, or a list of content blocks."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        parsed = _loads(raw)
        return parsed if isinstance(parsed, dict) else None
    if isinstance(raw, list):
        for block in raw:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parsed = _loads(block["text"])
                if isinstance(parsed, dict):
                    return parsed
    return None


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _read_state(path: Path) -> dict | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    data = _loads(text)
    return data if isinstance(data, dict) else None


def _wake_key(state: dict | None, pid_alive: bool) -> str | None:
    """A key for the condition the wait is tracking, or None when there is none.

    Two different things need a grace before waking -- a finished run nobody collected, and
    an unread question. The key is the state of the run's progress, so that a change (a new
    question, the run collected, the server gone) restarts the grace rather than inheriting
    the old one.
    """
    if state is None or not pid_alive:
        return None
    status = state.get("status")
    collected = state.get("collected", False)
    if status in ("done", "cancelled"):
        return None if collected else "finished"
    if state.get("questions_asked", 0) > state.get("questions_shown", 0):
        return f"question:{state.get('questions_asked')}"
    return None


def _claim_waiter(waiter: Path, alive: Callable[[int], bool]) -> bool:
    """Become the one waiter for a handle. False if a live one already waits."""
    try:
        fd = os.open(waiter, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pid = _waiter_pid(waiter)
        if pid is not None and alive(pid):
            return False
        _write_waiter(waiter)
        return True
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def _waiter_pid(waiter: Path) -> int | None:
    try:
        text = waiter.read_text().strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def _write_waiter(waiter: Path) -> None:
    waiter.write_text(str(os.getpid()))


def _release_waiter(waiter: Path) -> None:
    try:
        waiter.unlink(missing_ok=True)
    except OSError:
        pass
