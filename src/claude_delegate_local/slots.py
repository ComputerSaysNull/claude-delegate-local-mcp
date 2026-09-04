"""The admission counters, shared by every server process on this machine. ADR-0040.

`admission.py` owns the policy -- the four rules and what happens to a request that does
not fit. This module owns the *storage* those rules count against, and nothing else. The
split matters because the rules were correct all along; what was wrong was the scope they
counted over.

**Why a file at all.** The transport is stdio, so the MCP client spawns one server process
per registration. Two editor windows on two projects are two processes, each with its own
`Admission` and its own zeroed counters, against one KV pool. Every rule then bounds a
session rather than the cluster, and the effective ceiling is the configured one multiplied
by however many windows happen to be open -- which is exactly the oversubscription ADR-0012
exists to prevent, arrived at by a route ADR-0012 never considered.

**The decision happens inside the lock, or it is not a decision.** Reading the totals,
testing the predicate and publishing the result are one critical section under one
exclusive `flock`. Splitting them -- read, decide, then write -- is a time-of-check race in
which two processes both see room and both take it, and the race widens exactly when the
cluster is busiest and the answer matters most. `admit()` therefore takes the predicate as
a callable and evaluates it while holding the lock, rather than returning totals for a
caller to judge afterwards.

**A dead process must not hold slots.** A record is keyed by `(pid, start_time)`, the start
time read from field 22 of `/proc/<pid>/stat`, and is dropped the moment either stops
matching a live process. That is what makes a `kill -9`d editor window cost nothing: no
heartbeat to miss, no timeout to wait out, and PID reuse cannot let an unrelated new
process inherit the slots of the dead one whose number it got. The staleness timeout below
it is a backstop for platforms without `/proc`, never the primary mechanism -- a design
that reclaims on a timer is one that either leaks for the length of the timer or evicts a
live process that was merely slow.

**Never block the event loop.** The lock is taken `LOCK_EX | LOCK_NB` and retried around
`await asyncio.sleep`, because a blocking `flock` inside the loop would stall every other
delegation in this process -- including ones already running, which are not waiting for
anything. The critical section is a small JSON document on tmpfs; it is measured in
microseconds, and the retry exists for contention, not for duration.

**A corrupt file must not wedge the machine.** If the document does not parse it is reset
and the run continues. The alternative -- refusing every delegation on every process until
someone deletes a file by hand -- turns a latency protection into an outage, which inverts
the entire point of ADR-0012.

The file lives on tmpfs (`$XDG_RUNTIME_DIR`, else `/dev/shm`), never on `/mnt/c`: flock
across the Windows drive boundary is not dependable, and every operation there pays the
12x penalty ADR-0020 measured. Losing the file on reboot is correct, not a limitation --
no process survives a reboot holding a slot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from .config import Config

log = logging.getLogger(__name__)

try:  # POSIX only. Absent on Windows, where the test suite runs but the server never does.
    import fcntl
except ImportError:  # pragma: no cover -- exercised by the Windows leg of CI
    fcntl = None  # type: ignore[assignment]

_SCHEMA_VERSION = 1
_FILENAME = "admission-slots.json"

# Retry cadence for a contended lock. Short because the critical section is short: a
# holder is reading and rewriting a few hundred bytes of tmpfs, not doing work.
_LOCK_RETRY_SECONDS = 0.005
_LOCK_JITTER_SECONDS = 0.004

# Backstop only, and deliberately far longer than any critical section. A record this old
# whose liveness cannot be checked is assumed dead. On Linux the (pid, start_time) check
# settles it first and this never fires.
_STALE_AFTER_SECONDS = 900.0

# The same idea for one waiter's ticket, and it exists to bound a bug rather than a
# machine state. A ticket is dropped explicitly on every exit from the wait -- admitted,
# timed out, cancelled, raised -- and a dead process's tickets go with its record. What is
# left is a live process that somehow failed to drop one, and a ticket stuck at the head
# of the queue starves every later waiter permanently. So it expires, and expiring one is
# logged: a backstop that fires silently hides the defect it is compensating for.
_TICKET_STALE_AFTER_SECONDS = 900.0


def _waiting(records: dict[str, dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Every live waiter, as `(ticket, requirements)`, across all processes.

    The requirements travel with the ticket because a place in line only means anything
    against a request that could actually take the slot -- see `_ahead_of`.

    Expired tickets are dropped here and logged. See `_TICKET_STALE_AFTER_SECONDS`: this
    only ever fires for a live process that failed to drop one, which is a defect rather
    than a state, so it must not pass quietly.
    """
    now = time.time()
    out: list[tuple[int, dict[str, Any]]] = []
    for key, record in records.items():
        waiting = record.get("waiting")
        if not isinstance(waiting, dict):
            continue
        for raw, spec in waiting.items():
            try:
                ticket = int(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(spec, dict):
                continue
            at = spec.get("at")
            if isinstance(at, (int, float)) and now - at > _TICKET_STALE_AFTER_SECONDS:
                log.warning(
                    "admission ticket %d held by %s expired after %.0fs and was ignored; "
                    "a waiter did not give up its place in line",
                    ticket, key, now - at,
                )
                continue
            out.append((ticket, spec))
    return out


class SlotsUnavailable(RuntimeError):
    """The shared file could not be reached, so global counting is not possible."""


@dataclass(frozen=True, slots=True)
class Totals:
    """Summed live usage across every process holding slots, this one included.

    Deliberately the same shape the four rules already read, so the predicate that used to
    test process-local attributes tests these instead without changing what it means.
    """

    seqs: int = 0
    tokens: int = 0
    large: int = 0
    per_entry: dict[str, int] = field(default_factory=dict)
    # Waiters ahead of this request in the queue, and the reason the predicate needs it:
    # the four rules describe the cluster, which every waiter sees identically, so nothing
    # in them can distinguish the request whose turn it is. Zero means "go if the rules
    # allow", which is also what an uncontended first attempt sees.
    ahead: int = 0


def default_dir() -> Path:
    """Where the shared file lives when the operator names no directory.

    tmpfs in both branches. `XDG_RUNTIME_DIR` is the right answer and is per-user already;
    `/dev/shm` is the fallback because a process launched by `wsl.exe -e` does not
    necessarily get a login session, and so does not necessarily get the former.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        return Path(runtime) / "claude-delegate-local"
    return Path("/dev/shm") / f"claude-delegate-local-{os.getuid()}"


def _proc_start_time(pid: int) -> int | None:
    """Field 22 of `/proc/<pid>/stat`, or None where that cannot be read.

    Parsed from the last `)` rather than by splitting the whole line: field 2 is the
    executable name, it is parenthesised, and it may itself contain spaces and brackets.
    Splitting naively works until someone runs a binary with a space in its name, which is
    the kind of bug that is invisible until it is a mystery.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    rest = raw[close + 2 :].split()
    start_field = 19  # field 22 overall; fields 1 and 2 were consumed above
    if len(rest) <= start_field:
        return None
    try:
        return int(rest[start_field])
    except ValueError:
        return None


def _identity(pid: int | None = None) -> str:
    """The key a process files its record under: PID plus the incarnation of that PID."""
    pid = os.getpid() if pid is None else pid
    return f"{pid}:{_proc_start_time(pid)}"


def _is_live(key: str) -> bool | None:
    """True, False, or None when this platform cannot say.

    Three-valued on purpose. Treating "cannot check" as "dead" would let a machine without
    `/proc` silently reclaim slots from processes that are still using them, which is worse
    than the leak it would be trying to prevent.
    """
    pid_text, _, start_text = key.partition(":")
    try:
        pid = int(pid_text)
    except ValueError:
        return False  # a malformed key belongs to nobody
    actual = _proc_start_time(pid)
    if actual is None:
        return None if not Path("/proc").is_dir() else False
    return start_text == str(actual)


class SharedSlots:
    """The shared counters. One instance per process, wrapping one file on disk."""

    def __init__(self, path: Path, *, lock_timeout: float = 5.0) -> None:
        self.path = path
        self._lock_timeout = lock_timeout
        self._me = _identity()

    # ---- availability ----------------------------------------------------------------
    @staticmethod
    def unavailable_reason() -> str:
        """Why global counting cannot work here, or an empty string when it can.

        Reported rather than silently swallowed. A gate that quietly degrades to counting
        one process is indistinguishable, from the outside, from one that is working.
        """
        if fcntl is None:
            return "fcntl is unavailable on this platform, so no lock can be taken"
        return ""

    def prepare(self) -> None:
        """Create the directory and the file. Raises `SlotsUnavailable` if it cannot."""
        reason = self.unavailable_reason()
        if reason:
            raise SlotsUnavailable(reason)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 0o600: the counters name registry keys, and this is per-user state.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
        except OSError as e:
            raise SlotsUnavailable(f"{self.path} is not writable: {e}") from e

    # ---- the critical section --------------------------------------------------------
    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[int]:
        """Hold the exclusive lock for the body, yielding the open descriptor."""
        if fcntl is None:  # pragma: no cover -- guarded by `unavailable_reason`
            raise SlotsUnavailable(self.unavailable_reason())
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            raise SlotsUnavailable(f"cannot open {self.path}: {e}") from e
        deadline = time.monotonic() + self._lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise SlotsUnavailable(
                            f"could not lock {self.path} within {self._lock_timeout}s"
                        ) from None
                    # Jittered so several waiters do not retry in lockstep forever.
                    await asyncio.sleep(
                        _LOCK_RETRY_SECONDS + random.random() * _LOCK_JITTER_SECONDS
                    )
            try:
                yield fd
            finally:
                with suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            with suppress(OSError):
                os.close(fd)

    def _read(self, fd: int) -> tuple[dict[str, dict[str, Any]], int]:
        """Every live record, and the next ticket to hand out.

        Dead records are dropped here, on the way past -- and their waiting tickets go
        with them, which is why a crashed waiter needs no separate cleanup.
        """
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw.strip():
            return {}, 0
        try:
            doc = json.loads(raw)
            records = doc["records"] if isinstance(doc, dict) else None
            if not isinstance(records, dict):
                raise ValueError("no records object")
        except (ValueError, KeyError, TypeError):
            # Reset rather than refuse. See the module docstring: an unparseable file must
            # not become a machine-wide outage.
            log.warning("admission slot file %s was unreadable; resetting it", self.path)
            return {}, 0
        live = {
            k: v for k, v in records.items() if isinstance(v, dict) and self._keep(k, v)
        }
        # Derived when absent or nonsense rather than trusted, and never allowed to go
        # backwards past a ticket somebody is still holding: a counter that repeats a
        # number would put two waiters at the same place in line. A file written by a
        # version that predates the queue simply has no counter, which is why this is a
        # default and not a schema break -- resetting the file instead would zero the
        # slots an older server process on this machine is still holding.
        raw_next = doc.get("next_ticket") if isinstance(doc, dict) else None
        outstanding = [ticket for ticket, _ in _waiting(live)]
        floor = max(outstanding) + 1 if outstanding else 0
        nxt = raw_next if isinstance(raw_next, int) and raw_next >= 0 else 0
        return live, max(nxt, floor)

    def _keep(self, key: str, record: dict[str, Any]) -> bool:
        live = _is_live(key)
        if live is not None:
            return live
        # Liveness is unknowable here, so fall back to the age backstop.
        updated = record.get("updated_at")
        if not isinstance(updated, (int, float)):
            return False
        return (time.time() - updated) < _STALE_AFTER_SECONDS

    def _write(
        self, fd: int, records: dict[str, dict[str, Any]], next_ticket: int = 0
    ) -> None:
        payload = json.dumps(
            {
                "version": _SCHEMA_VERSION,
                "records": records,
                "next_ticket": next_ticket,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        # No fsync. The file is tmpfs and describes processes that are running right now;
        # durability across a reboot would be describing a world that no longer exists.

    @staticmethod
    def _totals(records: dict[str, dict[str, Any]], ahead: int = 0) -> Totals:
        per_entry: dict[str, int] = {}
        seqs = tokens = large = 0
        for record in records.values():
            seqs += int(record.get("seqs", 0))
            tokens += int(record.get("tokens", 0))
            large += int(record.get("large", 0))
            for key, count in (record.get("per_entry") or {}).items():
                per_entry[key] = per_entry.get(key, 0) + int(count)
        return Totals(
            seqs=seqs, tokens=tokens, large=large, per_entry=per_entry, ahead=ahead
        )

    @staticmethod
    def _ahead_of(
        records: dict[str, dict[str, Any]],
        ticket: int | None,
        fits: Callable[[dict[str, Any]], bool],
    ) -> int:
        """How many waiters have a prior claim on the next free slot.

        Two things make this the right count rather than a plain ticket comparison.

        A request holding no ticket yet is behind **every** eligible waiter, not none of
        them. That asymmetry is the whole of the fairness fix: treating a newcomer as
        unblocked is exactly the re-test loop this replaces, where the winner was whoever
        happened to look at the right moment.

        But only a waiter that could be admitted *now* counts. Strict ticket order would
        reintroduce head-of-line blocking -- the failure the four rules are checked as one
        predicate to avoid. A large request parked on the large-prefill cap would
        otherwise block a small one that fits every rule, for as long as the request ahead
        of *it* keeps running. A waiter that cannot take the slot is not spending its
        turn, so it does not hold one.
        """
        return sum(
            1
            for other, spec in _waiting(records)
            if (ticket is None or other < ticket) and fits(spec)
        )

    def _forget_ticket(self, records: dict[str, dict[str, Any]], ticket: int) -> None:
        """Drop one of this process's tickets. Idempotent, because the caller's `finally`
        cannot know whether the admitting path already gave it back."""
        record = records.get(self._me)
        if not record:
            return
        waiting = record.get("waiting")
        if isinstance(waiting, dict):
            waiting.pop(str(ticket), None)
            if not waiting:
                record.pop("waiting", None)

    def _mine(self, records: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return records.setdefault(
            self._me,
            {"seqs": 0, "tokens": 0, "large": 0, "per_entry": {}, "updated_at": time.time()},
        )

    # ---- what admission calls --------------------------------------------------------
    async def admit(  # noqa: PLR0913 -- the request, the two callbacks, the ticket
        self,
        *,
        tokens: int,
        is_large: bool,
        entry_key: str,
        decide: Callable[[Totals], tuple[str, int] | None],
        rival_fits: Callable[[Totals, dict[str, Any]], bool],
        spec: dict[str, Any],
        ticket: int | None = None,
    ) -> tuple[tuple[str, int] | None, int | None]:
        """Test `decide` against global totals and, if it admits, publish the slot.

        Returns `(binding, ticket)`. A binding rule means refused, and the ticket coming
        back is this request's place in line -- assigned on the first refusal and handed
        back on every later attempt, so the caller holds it for the whole wait. `(None,
        None)` means admitted, and the ticket has already been given up under the same
        lock hold that took the slot.

        The test, the queue position and the publication all happen under one lock hold,
        which is the whole point: a caller that received totals and decided afterwards
        would be racing every other process. The ticket is what makes that decision
        ordered as well as atomic.
        """
        async with self._locked() as fd:
            records, next_ticket = self._read(fd)
            # Capacity first, with no queue position in it: that is what every waiter's
            # feasibility is judged against, this request's included, so it is computed
            # once and shared rather than recomputed per rival.
            base = self._totals(records)
            ahead = self._ahead_of(records, ticket, lambda s: rival_fits(base, s))
            binding = decide(replace(base, ahead=ahead))
            if binding is not None:
                if ticket is None:
                    ticket = next_ticket
                    next_ticket += 1
                # Recorded on the refusal, which already rewrote the file to persist the
                # dead-record reclamation `_read` did -- so a place in line costs no extra
                # lock hold and no extra write. The requirements go in beside the
                # timestamp because another process has to judge whether this waiter could
                # run before it can decide the waiter is ahead of anyone.
                mine = self._mine(records)
                mine.setdefault("waiting", {})[str(ticket)] = {**spec, "at": time.time()}
                mine["updated_at"] = time.time()
                self._write(fd, records, next_ticket)
                return binding, ticket
            if ticket is not None:
                self._forget_ticket(records, ticket)
            mine = self._mine(records)
            mine["seqs"] = int(mine.get("seqs", 0)) + 1
            mine["tokens"] = int(mine.get("tokens", 0)) + tokens
            if is_large:
                mine["large"] = int(mine.get("large", 0)) + 1
            entries = mine.setdefault("per_entry", {})
            entries[entry_key] = int(entries.get(entry_key, 0)) + 1
            mine["updated_at"] = time.time()
            self._write(fd, records, next_ticket)
            return None, None

    async def drop_ticket(self, ticket: int) -> None:
        """Give up a place in line without taking a slot.

        Called from the waiter's `finally`, so it runs on a timeout, a cancellation and
        any other exception alike. Not calling it is the one way this change can starve
        the machine, which is why the caller does not decide when it applies.
        """
        async with self._locked() as fd:
            records, next_ticket = self._read(fd)
            self._forget_ticket(records, ticket)
            mine = records.get(self._me)
            if mine is not None and self._is_idle(mine):
                records.pop(self._me, None)
            self._write(fd, records, next_ticket)

    async def release(self, *, tokens: int, is_large: bool, entry_key: str) -> None:
        """Give back exactly what `admit` took."""
        async with self._locked() as fd:
            records, next_ticket = self._read(fd)
            mine = self._mine(records)
            mine["seqs"] = max(0, int(mine.get("seqs", 0)) - 1)
            mine["tokens"] = max(0, int(mine.get("tokens", 0)) - tokens)
            if is_large:
                mine["large"] = max(0, int(mine.get("large", 0)) - 1)
            entries = mine.setdefault("per_entry", {})
            remaining = int(entries.get(entry_key, 0)) - 1
            if remaining > 0:
                entries[entry_key] = remaining
            else:
                entries.pop(entry_key, None)
            mine["updated_at"] = time.time()
            # `_is_idle` and not the three counters directly: a record holding no slots
            # can still hold a place in line, and dropping it would silently cancel this
            # process's other waiters.
            if self._is_idle(mine):
                records.pop(self._me, None)
            self._write(fd, records, next_ticket)

    @staticmethod
    def _is_idle(record: dict[str, Any]) -> bool:
        """Nothing held and nothing queued, so the record says nothing worth keeping."""
        return not (
            int(record.get("seqs", 0))
            or int(record.get("tokens", 0))
            or int(record.get("large", 0))
            or record.get("waiting")
        )

    async def snapshot(self) -> tuple[Totals, int, int]:
        """Global usage, the processes holding it, and the queue depth, from one hold.

        One call rather than three accessors, so the numbers cannot come from different
        moments and describe a machine state that never existed. Queue depth comes back
        separately rather than as `Totals.ahead`, which means "ahead of one particular
        request" and has no meaning without one.
        """
        async with self._locked() as fd:
            records, _ = self._read(fd)
            return self._totals(records), len(records), len(_waiting(records))


def build_slots(cfg: Config) -> tuple[SharedSlots | None, str]:
    """The shared counters for this configuration, or None and the reason why not.

    Returns the reason rather than raising. A machine that cannot lock should still serve
    delegations -- bounded per process, as it was before ADR-0040 -- but it must say so,
    because a gate that has silently narrowed its scope is indistinguishable from a
    working one right up until the cluster is oversubscribed.
    """
    if not cfg.cross_process_slots:
        return None, "disabled by DELEGATE_CROSS_PROCESS_SLOTS"
    # Before `default_dir`, not after: that reads `os.getuid`, which does not exist on the
    # platform this check is here to catch.
    reason = SharedSlots.unavailable_reason()
    if reason:
        log.warning("admission is bounded per process only: %s", reason)
        return None, reason
    configured = cfg.slots_dir.strip()
    directory = Path(os.path.expanduser(configured)) if configured else default_dir()
    slots = SharedSlots(directory / _FILENAME)
    try:
        slots.prepare()
    except SlotsUnavailable as e:
        log.warning("admission is bounded per process only: %s", e)
        return None, str(e)
    return slots, ""


async def cross_process_status(
    slots: SharedSlots | None, reason: str
) -> dict[str, Any]:
    """What `backend_status` reports about the machine-wide budget.

    `active` is answered by whether the file can actually be read now, not by what the
    configuration asked for. The two differ exactly when something is wrong, which is the
    only time anybody reads this.
    """
    if slots is None:
        return {"active": False, "reason": reason}
    try:
        totals, processes, queued = await slots.snapshot()
    except SlotsUnavailable as e:
        return {"active": False, "reason": str(e)}
    return {
        "active": True,
        "path": str(slots.path),
        # More than one means the gap ADR-0040 closes is open right now, and this
        # process's own gauges above are a fraction of what the cluster is seeing.
        "processes_holding_slots": processes,
        # The machine-wide queue, which is where a deep one is visible: a process's own
        # `queued_waiters` counts only its share, and a wait is caused by every waiter.
        "queued_waiters": queued,
        "inflight_seqs": totals.seqs,
        "inflight_tokens": totals.tokens,
        "inflight_large_prefills": totals.large,
        "per_entry": dict(sorted(totals.per_entry.items())),
    }
