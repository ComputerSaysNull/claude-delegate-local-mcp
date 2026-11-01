"""A delegation's handle: the name of a run parked on a question.

A delegating call returns a handle only when the run stops to ask its caller a question
(`status: "question"`); `answer` continues that run and `cancel_delegation` stops it. A
run parked on a question has no task alive to cancel, so that is what the handle is for.

In-process on purpose. Every caller is one client talking to one process, and the run
lives in that process, so a handle that outlived a restart would name work that no longer
exists. A restart leaves the transcript, and an unknown handle is refused with a pointer
to it.
"""

import asyncio
import math
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class UnknownHandle(LookupError):
    """No delegation by that handle: never issued here, or from before a restart."""

    def __init__(self, handle: str) -> None:
        super().__init__(
            f"unknown_handle: no delegation {handle!r} is known to this server. A handle "
            "lives in the server process that issued it, so a reconnect or restart forgets "
            "it -- and the run with it -- and a finished run is dropped as soon as its "
            "result was handed to its caller. The transcript directory still holds the run's "
            "record."
        )
        self.handle = handle


class UnknownTask(LookupError):
    """No delegation by that task id: never issued here, or from before a restart."""

    def __init__(self, task_id: str) -> None:
        super().__init__(
            f"unknown_task: no delegation {task_id!r} is known to this server. A task id "
            "names a run in the server process that issued it, so a reconnect or restart "
            "forgets it -- and the run with it -- and a finished run is dropped as soon as "
            "its result was handed to its caller. The transcript directory still holds the "
            "run's record."
        )
        self.task_id = task_id


class WaitingForAnswer(LookupError):
    """The run is parked on a question it asked, so a message would be out of order."""

    def __init__(self, handle: str) -> None:
        super().__init__(
            f"waiting: delegation {handle!r} is parked on a question. Reply with `answer` "
            "to resume it -- a message is read at a turn boundary, not while the run waits "
            "on its caller."
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


@dataclass(frozen=True, slots=True)
class InboxMessage:
    """One message a caller queued for a run, and the moment it was queued.

    `queued_at` is monotonic, so the run can say how long the message sat before it read
    it. Created by `Handles.message`; a run's loop drains these at each turn boundary.
    """

    text: str
    queued_at: float


class Inbox:
    """The queue of caller messages for one run, drained by the run's loop.

    Created per delegation, handed to the loop as the drain source and to `Handles.start`
    so a `message_delegation` call can find it by task id. In-process like the rest of the
    handle state, so it dies with the server process that owns the run.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._messages: list[InboxMessage] = []
        self.closed = False

    def push(self, text: str) -> int:
        """Queue a message and return how many are now waiting."""
        if self.closed:
            raise RuntimeError("push to a closed inbox")  # Handles.message checks first
        self._messages.append(InboxMessage(text, self._clock()))
        return len(self._messages)

    def drain(self) -> list[InboxMessage]:
        """Hand over everything queued and clear the queue, so it is read at most once."""
        drained = self._messages
        self._messages = []
        return drained

    def close(self) -> list[InboxMessage]:
        """Take the last of the queue and refuse anything after it: the run has ended."""
        self.closed = True
        return self.drain()

    def pending(self) -> int:
        return len(self._messages)

    def age(self, message: InboxMessage) -> float:
        """Seconds since `message` was queued, on this inbox's clock."""
        return self._clock() - message.queued_at


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

    def ask(self, questions: list[str]) -> asyncio.Future[str]:
        """Record the questions the run is waiting on, and return the future it awaits."""
        self.questions = list(questions)
        self.asked_at = time.monotonic()
        self.asked_count += 1
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
    pause: Pause | None = None
    inbox: Inbox | None = None
    task_ids: set[str] = field(default_factory=set)


class Handles:
    """Every delegation this process started and has not yet forgotten, by handle.

    An entry is dropped the moment `collect` returns its finished or cancelled result --
    the task that ran it already carries that result, so nothing reads it back -- and kept
    while the run is running or parked on a question.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._by_task: dict[str, str] = {}

    def start(self, work: Awaitable[dict[str, Any]], *, tool: str,
              pause: Pause | None = None, inbox: Inbox | None = None,
              task_id: str | None = None) -> str:
        """Run `work` as a task of its own and return the handle it can be collected by.

        `task_id` registers the run so `message_delegation` can find it by the MCP task id
        the caller was handed; `inbox` is the queue that call then delivers into.
        """
        handle = f"d-{secrets.token_hex(6)}"
        while handle in self._entries:  # pragma: no cover - 48 bits
            handle = f"d-{secrets.token_hex(6)}"
        entry = _Entry(asyncio.ensure_future(work), tool, self._clock(),
                       pause=pause, inbox=inbox)
        if task_id is not None:
            entry.task_ids.add(task_id)
            self._by_task[task_id] = handle

        def finished(task: asyncio.Future[dict[str, Any]]) -> None:
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

        Its only caller in the server waits without limit -- until the run ends or asks a
        question -- so the finite `wait_seconds` exists for tests that need to watch a run
        still going. Waiting never cancels the run: `asyncio.wait` leaves what it waits on
        alone, so a caller that stops waiting -- a timeout, a cancelled `collect` -- stops
        only itself.

        `keepalive` is called with the run's age every `every` seconds of waiting, because
        a wait that says nothing is dropped by the client at its idle timeout (ADR-0018).
        A finished or cancelled run is forgotten once this returns it.
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
            self._forget(handle)
            return {"handle": handle, "status": "cancelled", "tool": entry.tool}
        result = entry.task.result()
        self._forget(handle)
        return {**result, "handle": handle, "status": "done"}

    def _question(self, handle: str, entry: _Entry, pause: Pause) -> dict[str, Any]:
        assert pause.asked_at is not None  # `waiting()` is only true once it asked
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

    def cancel(self, handle: str) -> bool:
        """Cancel a running delegation. False when it had already finished."""
        task = self._entry(handle).task
        if task.done():
            return False
        task.cancel()
        return True

    def message(self, task_id: str, text: str) -> int:
        """Queue `text` for the run the task id names, and say how many now wait.

        Raises `UnknownTask` when no run answers to that id -- never issued here, or
        already ended -- and `WaitingForAnswer` when the run is parked on a question it
        asked, in which case the caller should `answer` rather than message it.
        """
        handle = self._by_task.get(task_id)
        if handle is None:
            raise UnknownTask(task_id)
        entry = self._entry(handle)
        if entry.task.done() or (entry.inbox is not None and entry.inbox.closed):
            # Ended, its result still uncollected: nothing is left to read it.
            raise UnknownTask(task_id)
        if entry.pause is not None and entry.pause.waiting():
            raise WaitingForAnswer(handle)
        if entry.inbox is None:
            # A queue nobody drains would swallow the message; a run started without one
            # has no reader, so it is not reachable this way.
            raise UnknownTask(task_id)
        return entry.inbox.push(text)

    def bind(self, task_id: str, handle: str) -> None:
        """Map another task id onto an existing run -- `answer`'s own task follows the run."""
        entry = self._entry(handle)
        entry.task_ids.add(task_id)
        self._by_task[task_id] = handle

    def _forget(self, handle: str) -> None:
        """Drop a run, with every task id that named it."""
        entry = self._entries.pop(handle, None)
        if entry is None:
            return
        for task_id in entry.task_ids:
            self._by_task.pop(task_id, None)

    def _entry(self, handle: str) -> _Entry:
        entry = self._entries.get(handle)
        if entry is None:
            raise UnknownHandle(handle)
        return entry
