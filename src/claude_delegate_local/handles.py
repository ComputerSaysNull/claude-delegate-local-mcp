"""A delegation's handle: the name a second call collects its answer by.

The client stops *waiting* on a call after 120s, and it will not issue the next
write-capable call until the current one returns or is backgrounded, so a fan-out of six
`delegate` calls started 120s apart (PLAN M20.1). A call that returns a handle at once
lets the next one start; the work carries on in a task this process owns, and `collect`
hands back what it produced.

In-process on purpose. Every caller of this server is one client talking to one process,
and the run itself lives in that process -- a handle that outlived a restart would name
work that no longer exists. What a restart does leave is the transcript, and an unknown
handle is refused with a pointer to it.
"""

from __future__ import annotations

import asyncio
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


@dataclass
class _Entry:
    task: asyncio.Future[dict[str, Any]]
    tool: str
    started: float
    finished: float | None = None


class Handles:
    """Every delegation this process started and has not yet forgotten, by handle."""

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    def start(self, work: Awaitable[dict[str, Any]], *, tool: str) -> str:
        """Run `work` as a task of its own and return the handle it can be collected by."""
        self._reap()
        handle = f"d-{secrets.token_hex(6)}"
        while handle in self._entries:  # pragma: no cover - 48 bits
            handle = f"d-{secrets.token_hex(6)}"
        entry = _Entry(asyncio.ensure_future(work), tool, self._clock())

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

    async def collect(self, handle: str, wait_seconds: float) -> dict[str, Any]:
        """The run's result if it finishes within `wait_seconds`, else where it has got to.

        Waiting never cancels the run: `asyncio.wait` leaves what it waits on alone, so a
        caller that stops waiting -- a timeout, a cancelled `collect` -- stops only itself.
        """
        entry = self._entry(handle)
        if not entry.task.done() and wait_seconds > 0:
            await asyncio.wait({entry.task}, timeout=wait_seconds)
        if not entry.task.done():
            return {"handle": handle, "status": "running", "tool": entry.tool,
                    "running_seconds": round(self._clock() - entry.started, 1)}
        if entry.task.cancelled():
            return {"handle": handle, "status": "cancelled", "tool": entry.tool}
        return {**entry.task.result(), "handle": handle, "status": "done"}

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
