"""The admission counters, shared by every server process on this machine. ADR-0040.

`admission.py` owns the policy -- the four rules, and what happens to a request that does
not fit. This module owns the *storage* those rules count against, and nothing else.

**Why a file at all.** The transport is stdio, so the MCP client spawns one server process
per registration. Two editor windows are two processes, each with its own `Admission` and
zeroed counters, against one KV pool. Every rule would then bound a session rather than the
cluster, and the ceiling would multiply by the windows open -- the oversubscription
ADR-0012 exists to prevent.

**The decision happens inside the lock, or it is not a decision.** Reading the totals,
testing the predicate and publishing the result are one critical section under one
exclusive `flock`. Read, decide, then write is a time-of-check race in which two processes
both see room and both take it, and it widens when the cluster is busiest and the answer
matters most. So `admit()` takes the predicate as a callable and evaluates it under the
lock, rather than returning totals for a caller to judge.

**A dead process must not hold slots.** A record is keyed by `(pid, start_time)`, the start
time from field 22 of `/proc/<pid>/stat`, and is dropped once either stops matching a live
process. So a `kill -9`d editor window costs nothing: no heartbeat to miss, no timeout to
wait out, and a reused PID cannot inherit the dead process's slots. The staleness timeout
is a backstop for platforms without `/proc`, never the primary mechanism: reclaiming on a
timer either leaks for the timer's length or evicts a live process that was merely slow.

**A record also says whether its process is counting a burst.** `admission.py` keeps one
wait open per burst so every member prices on the whole burst, not on its arrival
position, and this flag lets a member in *another* process join that wait rather than
count only the siblings ahead of it. It lives inside the record, not at the document's top
level, so a process that dies mid-count takes its flag with it when the record is reaped,
and no second staleness rule is needed.

**Never block the event loop.** The lock is taken `LOCK_EX | LOCK_NB` and retried around
`await asyncio.sleep`, because a blocking `flock` would stall every delegation in this
process, including running ones that are not waiting for anything. The critical section
is a small JSON document on tmpfs, microseconds long; the retry is for contention, not
duration.

**A corrupt file must not wedge the machine.** A document that does not parse is reset and
the run continues. Refusing every delegation until someone deletes a file by hand would
turn a latency protection into an outage, inverting ADR-0012.

The file lives on tmpfs (`$XDG_RUNTIME_DIR`, else `/dev/shm`), never on `/mnt/c`: flock
across the Windows drive boundary is not dependable, and every operation there is slow
(ADR-0020). Losing the file on reboot is correct: no process survives a reboot holding a
slot.
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

# The key, inside one record, saying that process is counting a burst right now.
#
# The version above does *not* move for it, for the reason given at `next_ticket` in
# `_read`: an additive field both directions read sanely. An older reader ignores the
# unknown key and prices as it always did; a newer one meeting no flag finds no wait open
# and does the same. Nothing reads `version`, and making this a break would have an older
# process reset a file whose slots a newer one holds -- the outage the module docstring
# rules out.
_BURST_FIELD = "burst_wait"

# Retry cadence for a contended lock. Short because the critical section is: a holder
# rewrites a few hundred bytes of tmpfs.
_LOCK_RETRY_SECONDS = 0.005
_LOCK_JITTER_SECONDS = 0.004

# Backstop only, far longer than any critical section. A record this old whose liveness
# cannot be checked is assumed dead. On Linux the (pid, start_time) check settles it first.
_STALE_AFTER_SECONDS = 900.0

# The same for one waiter's ticket, bounding a bug rather than a machine state. A ticket
# is dropped on every exit from the wait, and a dead process's go with its record. What is
# left is a live process that failed to drop one, and a ticket stuck at the head starves
# every later waiter for good. So it expires, and expiry is logged: a backstop that fires
# silently hides the defect it compensates for.
_TICKET_STALE_AFTER_SECONDS = 900.0


def _waiting(records: dict[str, dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Every live waiter, as `(ticket, requirements)`, across all processes.

    The requirements travel with the ticket, because a place in line means something only
    for a request that could take the slot -- see `_ahead_of`. Expired tickets are dropped
    and logged (`_TICKET_STALE_AFTER_SECONDS`).
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

    The shape the rules read, so the one predicate tests local and shared totals alike
    without changing what it means.
    """

    seqs: int = 0
    tokens: int = 0
    per_entry: dict[str, int] = field(default_factory=dict)
    # Waiters ahead of this request. The capacity rules describe the cluster, which every
    # waiter sees identically, so they cannot pick out the request whose turn it is. Zero
    # means "go if the rules allow", as an uncontended first attempt sees.
    ahead: int = 0
    # Everyone queued, not only those ahead. No rule reads it: it lets a granted lease say
    # what concurrency it will meet. `ahead` is zero for the request admitted, while the
    # waiters behind it are the ones that will contend with it.
    waiting: int = 0


def default_dir() -> Path:
    """Where the shared file lives when the operator names no directory.

    tmpfs either way. `XDG_RUNTIME_DIR` is right and already per-user; `/dev/shm` is the
    fallback because a process launched by `wsl.exe -e` may get no login session, and so
    no `XDG_RUNTIME_DIR`.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        return Path(runtime) / "claude-delegate-local"
    return Path("/dev/shm") / f"claude-delegate-local-{os.getuid()}"


def default_dir_if_available() -> Path | None:
    """`default_dir()` where the platform has one, else None.

    The check comes *before* the call: `default_dir` reads `os.getuid`, which does not
    exist on the platform being checked for -- the ordering trap `build_slots` documents.

    Probed with `fcntl` not for a lock, but because it tells a POSIX host with a tmpfs
    runtime directory from one without. A caller given None keeps its in-memory behaviour.
    """
    if fcntl is None:
        return None
    return default_dir()


def rate_history_path(cfg: Config) -> Path | None:
    """Where the decode-rate memory lives, which is deliberately not where the slots do.

    Durable by default, the reverse of the slots: losing this file is a cold start, not a
    clean slate, and the since-boot mean it falls back to is the worst tail measured
    (ADR-0094). The file is published by `os.replace` and needs no lock, so the durable
    branch has no `fcntl` probe and works on any platform.

    Blank is valid, not an error: it means the runtime directory, through
    `default_dir_if_available`, since `default_dir` unguarded crashes off POSIX. None
    flows on to `RateHistory`, which then keeps its memory per process.

    The filename is joined here, once, so call sites cannot drift.
    """
    configured = cfg.rate_history_dir.strip()
    if configured:
        return Path(os.path.expanduser(configured)) / "rate-history.json"
    runtime = default_dir_if_available()
    return None if runtime is None else runtime / "rate-history.json"


def _proc_start_time(pid: int) -> int | None:
    """Field 22 of `/proc/<pid>/stat`, or None where that cannot be read.

    Parsed from the last `)` rather than by splitting the line: field 2 is the executable
    name, parenthesised, and may itself contain spaces and brackets.
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


def _burst_open(flag: object, now: float) -> bool:
    """A number is the flag's expiry; a bare `True`, from an older server, is open."""
    if flag is True:
        return True
    if isinstance(flag, (int, float)) and not isinstance(flag, bool):
        return flag > now
    return False


def _identity(pid: int | None = None) -> str:
    """The key a process files its record under: PID plus the incarnation of that PID."""
    pid = os.getpid() if pid is None else pid
    return f"{pid}:{_proc_start_time(pid)}"


def _is_live(key: str) -> bool | None:
    """True, False, or None when this platform cannot say.

    Three-valued: treating "cannot check" as "dead" would let a machine without `/proc`
    silently reclaim slots still in use, which is worse than the leak it would prevent.
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

        Reported, not swallowed: a gate quietly counting one process looks, from outside,
        exactly like one that works.
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

        Dead records are dropped on the way past, their tickets with them, so a crashed
        waiter needs no separate cleanup.
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
            # Reset rather than refuse: an unparseable file must not become a machine-wide
            # outage.
            log.warning("admission slot file %s was unreadable; resetting it", self.path)
            return {}, 0
        live = {
            k: v for k, v in records.items() if isinstance(v, dict) and self._keep(k, v)
        }
        # Derived when absent or nonsense, and never behind a ticket someone still holds:
        # a repeated number would put two waiters at one place in line. A file from a
        # version without the queue has no counter, so this is a default, not a schema
        # break -- resetting the file would zero slots an older process still holds.
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
        # No fsync: the file is tmpfs and describes processes running now, which a reboot
        # ends anyway.

    @staticmethod
    def _totals(records: dict[str, dict[str, Any]], ahead: int = 0) -> Totals:
        per_entry: dict[str, int] = {}
        seqs = tokens = 0
        for record in records.values():
            seqs += int(record.get("seqs", 0))
            tokens += int(record.get("tokens", 0))
            for key, count in (record.get("per_entry") or {}).items():
                per_entry[key] = per_entry.get(key, 0) + int(count)
        return Totals(
            seqs=seqs, tokens=tokens, per_entry=per_entry, ahead=ahead,
            waiting=len(_waiting(records)),
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
        them. That asymmetry is the fairness: a newcomer treated as unblocked would win
        whenever it happened to look at the right moment.

        But only a waiter that could be admitted *now* counts. Strict ticket order would
        bring back head-of-line blocking, which the single predicate exists to avoid: a
        large request waiting for token budget would block a small one that fits every
        rule, for as long as the request ahead of *it* runs. A waiter that cannot take the
        slot is not spending its turn, so it does not hold one.
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
            {"seqs": 0, "tokens": 0, "per_entry": {}, "updated_at": time.time()},
        )

    # ---- what admission calls --------------------------------------------------------
    async def admit(  # noqa: PLR0913 -- the request, the two callbacks, the ticket
        self,
        *,
        tokens: int,
        entry_key: str,
        decide: Callable[[Totals], tuple[str, int] | None],
        rival_fits: Callable[[Totals, dict[str, Any]], bool],
        spec: dict[str, Any],
        ticket: int | None = None,
    ) -> tuple[tuple[str, int] | None, int | None]:
        """Test `decide` against global totals and, if it admits, publish the slot.

        Returns `(binding, ticket)`. A binding rule means refused, and the ticket is this
        request's place in line -- assigned on the first refusal and passed back on every
        later attempt, so the caller holds it for the whole wait. `(None, None)` means
        admitted, the ticket already given up in the lock hold that took the slot.

        Test, queue position and publication share one lock hold: a caller deciding on
        totals afterwards would race every other process. The ticket makes the decision
        ordered as well as atomic.
        """
        async with self._locked() as fd:
            records, next_ticket = self._read(fd)
            # Capacity with no queue position in it: every waiter's feasibility is judged
            # against it, this request's included, so it is computed once and shared rather
            # than recomputed per rival.
            base = self._totals(records)
            ahead = self._ahead_of(records, ticket, lambda s: rival_fits(base, s))
            binding = decide(replace(base, ahead=ahead))
            if binding is not None:
                if ticket is None:
                    ticket = next_ticket
                    next_ticket += 1
                # Recorded in the write that persists `_read`'s reclamation anyway, so a
                # place in line costs no extra lock hold or write. The requirements go in
                # beside the timestamp because another process must judge whether this
                # waiter could run before counting it ahead of anyone.
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
            entries = mine.setdefault("per_entry", {})
            entries[entry_key] = int(entries.get(entry_key, 0)) + 1
            mine["updated_at"] = time.time()
            self._write(fd, records, next_ticket)
            return None, None

    async def drop_ticket(self, ticket: int) -> None:
        """Give up a place in line without taking a slot.

        Called from the waiter's `finally`, so it runs on a timeout, a cancellation and any
        other exception alike: not calling it is the one way the queue can starve the
        machine, so no path opts into it.
        """
        async with self._locked() as fd:
            records, next_ticket = self._read(fd)
            self._forget_ticket(records, ticket)
            mine = records.get(self._me)
            if mine is not None and self._is_idle(mine):
                records.pop(self._me, None)
            self._write(fd, records, next_ticket)

    async def release(self, *, tokens: int, entry_key: str) -> None:
        """Give back exactly what `admit` took, minus anything already given back early."""
        async with self._locked() as fd:
            records, next_ticket = self._read(fd)
            mine = self._mine(records)
            mine["seqs"] = max(0, int(mine.get("seqs", 0)) - 1)
            mine["tokens"] = max(0, int(mine.get("tokens", 0)) - tokens)
            entries = mine.setdefault("per_entry", {})
            remaining = int(entries.get(entry_key, 0)) - 1
            if remaining > 0:
                entries[entry_key] = remaining
            else:
                entries.pop(entry_key, None)
            mine["updated_at"] = time.time()
            # `_is_idle`, not the counters: a record holding no slots can still hold a
            # place in line, and dropping it would silently cancel this process's waiters.
            if self._is_idle(mine):
                records.pop(self._me, None)
            self._write(fd, records, next_ticket)

    @staticmethod
    def _is_idle(record: dict[str, Any]) -> bool:
        """Nothing held and nothing queued, so the record says nothing worth keeping.

        **An open burst wait is deliberately not in this list**, though other processes
        read it. A process counting a burst holds the slot it was just granted, so `seqs`
        already keeps the record. The flag here would be redundant normally and harmful
        once: a close that failed against an unreachable file leaves the flag set, and a
        record protected by its own stranded flag could never be reaped, undoing the
        expiry that bounds how long others read it.
        """
        return not (
            int(record.get("seqs", 0))
            or int(record.get("tokens", 0))
            or record.get("waiting")
        )

    # ---- the burst flag --------------------------------------------------------------
    async def open_burst_wait(self, expires_in: float) -> None:
        """Say that this process is counting a burst, until `expires_in` from now."""
        await self._set_burst_wait(True, expires_in)

    async def close_burst_wait(self) -> None:
        """Say that it is no longer counting one. Idempotent: the caller's exit paths
        cannot all know whether the flag was ever written."""
        await self._set_burst_wait(False)

    async def _set_burst_wait(self, open_wait: bool, expires_in: float = 0.0) -> None:
        async with self._locked() as fd:
            records, next_ticket = self._read(fd)
            if open_wait:
                mine = self._mine(records)
                mine[_BURST_FIELD] = time.time() + expires_in
                mine["updated_at"] = time.time()
            else:
                held = records.get(self._me)
                if held is not None:
                    held.pop(_BURST_FIELD, None)
                    held["updated_at"] = time.time()
                    # A record that held nothing but the flag is now saying nothing.
                    if self._is_idle(held):
                        records.pop(self._me, None)
            # Written even when nothing changed, to persist `_read`'s reclamation.
            self._write(fd, records, next_ticket)

    async def burst_wait_elsewhere(self) -> bool:
        """Whether some *other* process is counting a burst right now.

        This process's record is excluded: `admission.py` joins its own open wait as a
        future, and reading its own flag back would make a count that failed to clear it
        join itself for ever.
        """
        async with self._locked() as fd:
            records, _ = self._read(fd)
            now = time.time()
            return any(
                key != self._me and _burst_open(record.get(_BURST_FIELD), now)
                for key, record in records.items()
            )

    async def snapshot(self) -> tuple[Totals, int, int]:
        """Global usage, the processes holding it, and the queue depth, from one hold.

        One call, not three accessors, so the numbers cannot come from different moments
        and describe a state that never existed. Queue depth is separate from
        `Totals.ahead`, which means "ahead of one particular request" and has no meaning
        without one.
        """
        async with self._locked() as fd:
            records, _ = self._read(fd)
            return self._totals(records), len(records), len(_waiting(records))


def build_slots(cfg: Config) -> tuple[SharedSlots | None, str]:
    """The shared counters for this configuration, or None and the reason why not.

    Returns the reason rather than raising. A machine that cannot lock still serves
    delegations, bounded per process, but must say so: a gate that silently narrowed its
    scope looks like a working one until the cluster is oversubscribed.
    """
    if not cfg.cross_process_slots:
        return None, "disabled by DELEGATE_CROSS_PROCESS_SLOTS"
    # Before `default_dir`, which reads `os.getuid`, absent on the platform this catches.
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

    `active` says whether the file can be read now, not what the configuration asked for:
    the two differ exactly when something is wrong, the only time anybody reads this.
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
        # More than one means this process's own gauges are a fraction of what the
        # cluster sees (ADR-0040).
        "processes_holding_slots": processes,
        # The machine-wide queue: a process's own `queued_waiters` counts only its share,
        # and a wait is caused by every waiter.
        "queued_waiters": queued,
        "inflight_seqs": totals.seqs,
        "inflight_tokens": totals.tokens,
        "per_entry": dict(sorted(totals.per_entry.items())),
    }
