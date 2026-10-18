"""A delegation's handle: the name a second call collects its answer by.

The client stops *waiting* on a call after 120s and will not issue the next write-capable
call until the current one returns or is backgrounded, so a fan-out of `delegate` calls
starts 120s apart. A call that returns a handle at once lets the next one start; the work
carries on in a task this process owns, and `collect` hands back what it produced.

In-process on purpose. Every caller is one client talking to one process, and the run
lives in that process, so a handle that outlived a restart would name work that no longer
exists. A restart leaves the transcript, and an unknown handle is refused with a pointer
to it.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class UnknownHandle(LookupError):
    """No delegation by that handle: never issued here, reaped, or from before a restart."""

    def __init__(self, handle: str) -> None:
        super().__init__(
            f"unknown_handle: no delegation {handle!r} is known to this server. A handle "
            "lives in the server process that issued it, so a reconnect or restart forgets "
            "it -- and the run with it -- and a collected result is kept only for "
            "DELEGATE_HANDLE_TTL_SECONDS after it finished. The transcript directory still "
            "holds the run's record."
        )
        self.handle = handle


class NotWaitingError(LookupError):
    """The run is not paused on a question: finished, never asked, or already answered."""

    def __init__(self, handle: str) -> None:
        super().__init__(
            f"not waiting: delegation {handle!r} is not waiting for an answer -- it "
            "finished, never asked, or was already answered."
        )
        self.handle = handle


class Pause:
    """A delegation paused on a question it asked its caller.

    The run records its questions here and waits on the answer future; `collect` reports
    the pause as `status: "question"` and `answer` resolves the future. A run may ask
    again, so an answer clears the event and the next question sets it afresh (ADR-0125).
    """

    def __init__(self) -> None:
        self.questions: list[str] = []
        self.asked_at: float | None = None
        self.answer: asyncio.Future[str] | None = None
        self.asked = asyncio.Event()
        self.asked_count = 0
        self.on_change: Callable[[], None] | None = None

    def ask(self, questions: list[str]) -> asyncio.Future[str]:
        """Record the questions the run is waiting on, and return the future it awaits."""
        self.questions = list(questions)
        self.asked_at = time.monotonic()
        self.asked_count += 1
        self.answer = asyncio.get_running_loop().create_future()
        self.asked.set()
        if self.on_change is not None:
            self.on_change()
        return self.answer

    def waiting(self) -> bool:
        """Whether the run is still paused on an unanswered question."""
        return self.asked.is_set() and self.answer is not None and not self.answer.done()


@dataclass
class _Entry:
    task: asyncio.Future[dict[str, Any]]
    tool: str
    started: float
    finished: float | None = None
    pause: Pause | None = None
    questions_shown: int = 0
    collected: bool = False


class Handles:
    """Every delegation this process started and has not yet forgotten, by handle.

    With a `state_dir` each handle also carries a small JSON file there, so a wait-hook in
    another process can poll it and wake the caller when a run finishes or asks and nobody
    is listening (ADR-0126). Without one, nothing is written.
    """

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic,
                 state_dir: Path | None = None) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._state_dir = state_dir
        self._entries: dict[str, _Entry] = {}

    def start(self, work: Awaitable[dict[str, Any]], *, tool: str,
              pause: Pause | None = None) -> str:
        """Run `work` as a task of its own and return the handle it can be collected by."""
        self._reap()
        handle = f"d-{secrets.token_hex(6)}"
        while handle in self._entries:  # pragma: no cover - 48 bits
            handle = f"d-{secrets.token_hex(6)}"
        entry = _Entry(asyncio.ensure_future(work), tool, self._clock(), pause=pause)
        if pause is not None:
            pause.on_change = lambda: self._write_state(handle)

        def finished(task: asyncio.Future[dict[str, Any]]) -> None:
            entry.finished = self._clock()
            if not task.cancelled():
                # Marked as retrieved, so a run nobody collects does not log "exception
                # was never retrieved" at exit. `collect` still raises it.
                task.exception()
            self._write_state(handle)

        entry.task.add_done_callback(finished)
        self._entries[handle] = entry
        self._write_state(handle)
        return handle

    def task(self, handle: str) -> asyncio.Future[dict[str, Any]]:
        return self._entry(handle).task

    def pause(self, handle: str) -> Pause | None:
        """The `Pause` a delegation asked with, or None if it was never given one."""
        return self._entry(handle).pause

    async def collect(
        self,
        handle: str,
        wait_seconds: float,
        *,
        keepalive: Callable[[float], Awaitable[None]] | None = None,
        every: float = 0.0,
    ) -> dict[str, Any]:
        """The run's result if it finishes within `wait_seconds`, else where it has got to.

        Waiting never cancels the run: `asyncio.wait` leaves what it waits on alone, so a
        caller that stops waiting -- a timeout, a cancelled `collect` -- stops only itself.

        `keepalive` is called with the run's age every `every` seconds of waiting, because
        a wait that says nothing is dropped by the client at its idle timeout (ADR-0018).
        """
        entry = self._entry(handle)
        pause = entry.pause
        remaining = wait_seconds
        while not entry.task.done() and remaining > 0:
            if pause is not None and pause.waiting():
                return self._question(handle, entry, pause)
            step = min(remaining, every) if keepalive and every > 0 else remaining
            watching: set[asyncio.Future[Any]] = {entry.task}
            # A run that may still ask answers a `collect` the moment it does, so the wait
            # watches for the question beside the task. An answer clears the event, so a
            # wait arms again for the next question instead of waking at once.
            asked: asyncio.Task[Any] | None = None
            if pause is not None and not pause.asked.is_set():
                asked = asyncio.ensure_future(pause.asked.wait())
                watching.add(asked)
            try:
                await asyncio.wait(watching, timeout=step,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                if asked is not None:
                    asked.cancel()
            if pause is not None and pause.waiting() and not entry.task.done():
                return self._question(handle, entry, pause)
            # An infinite step is an infinite wait, not something to subtract from a budget.
            if step != math.inf:
                remaining -= step
            if keepalive and not entry.task.done() and remaining > 0:
                await keepalive(self._clock() - entry.started)
        if not entry.task.done():
            return {"handle": handle, "status": "running", "tool": entry.tool,
                    "running_seconds": round(self._clock() - entry.started, 1)}
        if entry.task.cancelled():
            entry.collected = True
            self._write_state(handle)
            return {"handle": handle, "status": "cancelled", "tool": entry.tool}
        entry.collected = True
        self._write_state(handle)
        return {**entry.task.result(), "handle": handle, "status": "done"}

    def _question(self, handle: str, entry: _Entry, pause: Pause) -> dict[str, Any]:
        assert pause.asked_at is not None  # `waiting()` is only true once it asked
        entry.questions_shown = pause.asked_count
        self._write_state(handle)
        return {"handle": handle, "status": "question", "tool": entry.tool,
                "questions": list(pause.questions),
                "waiting_seconds": round(time.monotonic() - pause.asked_at, 1)}

    def answer(self, handle: str, text: str) -> None:
        """Hand `text` to a run paused on a question, so it resumes with it.

        Raises `NotWaitingError` when the run is not waiting for an answer -- finished,
        never asked, or already answered.
        """
        entry = self._entry(handle)
        pause = entry.pause
        if pause is None or not pause.waiting() or pause.answer is None:
            raise NotWaitingError(handle)
        pause.asked.clear()
        pause.answer.set_result(text)
        self._write_state(handle)

    def cancel(self, handle: str) -> bool:
        """Cancel a running delegation. False when it had already finished."""
        task = self._entry(handle).task
        if task.done():
            return False
        task.cancel()
        return True

    def _entry(self, handle: str) -> _Entry:
        self._reap()
        entry = self._entries.get(handle)
        if entry is None:
            raise UnknownHandle(handle)
        return entry

    def _reap(self) -> None:
        """Forget runs that finished more than the TTL ago. Running ones are never reaped."""
        now = self._clock()
        for handle in [h for h, e in self._entries.items()
                       if e.finished is not None and now - e.finished > self._ttl]:
            del self._entries[handle]
            self._remove_state(handle)

    def _write_state(self, handle: str) -> None:
        """Write the handle's state file, best-effort. `None` state_dir means no files."""
        if self._state_dir is None:
            return
        entry = self._entries.get(handle)
        if entry is None:
            return
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            path = self._state_dir / f"{handle}.json"
            tmp = self._state_dir / f"{handle}.json.tmp"
            tmp.write_text(json.dumps(self._state_dict(handle, entry)))
            os.replace(tmp, path)
        except OSError:
            # A state file must never fail a run or a tool call: the wait-hook just keeps
            # polling whatever the last good write said.
            pass

    def _remove_state(self, handle: str) -> None:
        if self._state_dir is None:
            return
        try:
            (self._state_dir / f"{handle}.json").unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _state_dict(handle: str, entry: _Entry) -> dict[str, Any]:
        if entry.task.cancelled():
            status = "cancelled"
        elif entry.task.done():
            status = "done"
        elif entry.pause is not None and entry.pause.waiting():
            status = "question"
        else:
            status = "running"
        return {
            "handle": handle,
            "pid": os.getpid(),
            "tool": entry.tool,
            "status": status,
            "questions_asked": entry.pause.asked_count if entry.pause is not None else 0,
            "questions_shown": entry.questions_shown,
            "collected": entry.collected,
        }
