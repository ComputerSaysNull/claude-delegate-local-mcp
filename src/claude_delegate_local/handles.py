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
import math
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
    the pause as `status: "question"` and `answer` resolves the future. One question per
    run, so the event is set once and the future resolved once.
    """

    def __init__(self) -> None:
        self.questions: list[str] = []
        self.asked_at: float | None = None
        self.answer: asyncio.Future[str] | None = None
        self.asked = asyncio.Event()
        self.for_person = False
        self.person_asked = False  # tried once, never again (ADR-0120)
        self.answered_by: str | None = None  # "caller" or "person", for the transcript

    def ask(self, questions: list[str], for_person: bool = False) -> asyncio.Future[str]:
        """Record the questions the run is waiting on, and return the future it awaits."""
        self.questions = list(questions)
        self.for_person = bool(for_person)
        self.asked_at = time.monotonic()
        self.answer = asyncio.get_running_loop().create_future()
        self.asked.set()
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


class Handles:
    """Every delegation this process started and has not yet forgotten, by handle."""

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    def start(self, work: Awaitable[dict[str, Any]], *, tool: str,
              pause: Pause | None = None) -> str:
        """Run `work` as a task of its own and return the handle it can be collected by."""
        self._reap()
        handle = f"d-{secrets.token_hex(6)}"
        while handle in self._entries:  # pragma: no cover - 48 bits
            handle = f"d-{secrets.token_hex(6)}"
        entry = _Entry(asyncio.ensure_future(work), tool, self._clock(), pause=pause)

        def finished(task: asyncio.Future[dict[str, Any]]) -> None:
            entry.finished = self._clock()
            if not task.cancelled():
                # Marked as retrieved, so a run nobody collects does not log "exception
                # was never retrieved" at exit. `collect` still raises it.
                task.exception()

        entry.task.add_done_callback(finished)
        self._entries[handle] = entry
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
            # watches for the question beside the task -- only until one is asked, or an
            # answered question would wake every wait at once.
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
            return {"handle": handle, "status": "cancelled", "tool": entry.tool}
        return {**entry.task.result(), "handle": handle, "status": "done"}

    @staticmethod
    def _question(handle: str, entry: _Entry, pause: Pause) -> dict[str, Any]:
        assert pause.asked_at is not None  # `waiting()` is only true once it asked
        return {"handle": handle, "status": "question", "tool": entry.tool,
                "questions": list(pause.questions),
                "waiting_seconds": round(time.monotonic() - pause.asked_at, 1)}

    def answer(self, handle: str, text: str, *, by: str = "caller") -> None:
        """Hand `text` to a run paused on a question, so it resumes with it.

        `by` is "caller" for the `answer` tool, "person" for an elicitation. Raises
        `NotWaitingError` when the run is not waiting for an answer -- finished, never
        asked, or already answered.
        """
        entry = self._entry(handle)
        pause = entry.pause
        if pause is None or not pause.waiting() or pause.answer is None:
            raise NotWaitingError(handle)
        pause.answered_by = by
        pause.answer.set_result(text)

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
