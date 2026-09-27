"""The gate every delegation passes before it reaches a backend. ADR-0012.

The per-request context and the sequence count are ceilings, not reservations. What
constrains the cluster is summed live tokens staying under the KV pool: six full-context
requests cannot fit where six fifth-size ones do. Oversubscription queues rather than
fails, so this protects latency, not correctness -- and the queueing can be severe,
because large cold prefills serialise. The engine serialises them itself, so there is no
cap on them here (ADR-0077).

**Queueing is a place in line.** Every waiter takes a ticket and the predicate refuses
anyone not at the head, so a wait is bounded by the work ahead of it, not by who polls at
the right moment. Waiting is still polling, because a release in another process notifies
nothing here: fairness decides *who* goes next, not how promptly anyone finds out.

**Four rules, one predicate.** Three are capacity -- in-flight sequences, the summed token
estimate against the budget, and the endpoint's own limit -- and say whether a request
*fits*; queue position says whether it is *next*. One predicate rather than semaphores
taken in turn, because a request holding one slot while it blocks on another rule holds
capacity it is not using and starves smaller requests that would fit. Nothing is ever
partially acquired: a waiter that does not fit holds nothing.

**Undersubscription is invisible where oversubscription announces itself**, so this counts
as well as gates. `status()`'s peaks and wait totals, surfaced by `backend_status`, are how
the limits stop being guesses: a peak that never nears its ceiling says the ceiling is low.

A request's size is its **KV footprint**, the prompt plus the reply it may generate. A
lease's estimate is fixed at grant and never grows, so a long agentic delegation can
outgrow it late in the loop. Growing it per turn would couple this module to the turn
loop and add a reconciliation path on every abort; `peak_inflight_tokens` shows whether
that trade is wrong.

One instance per process, over one counter file shared by every process on the machine
(ADR-0040). The transport is stdio, so two editor windows are two servers, and limits
counted per process would multiply by the windows open. `slots.py` owns the file; this
module owns what its numbers mean.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .slots import SharedSlots, SlotsUnavailable, Totals

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .config import Config

# Well under the client's 1800s stdio idle timeout: no turn runs during a wait, so
# nothing else pings (ADR-0018). Not a config field, because it follows the client's
# timer, not anything an operator tunes.
_WAIT_TICK_SECONDS = 30.0

# How often a waiter re-tests the shared file. Another process's release cannot notify
# this one, so cross-process waiting is polling short of a broker. Cheap: the critical
# section is a small document on tmpfs.
_POLL_SECONDS = 0.25


log = logging.getLogger(__name__)

# The one rule not about capacity. Named because `AdmissionTimedOut` phrases it
# differently: for a queue position the number is how many are ahead, not a limit.
QUEUED_RULE = "queued_behind_earlier_waiters"


class AdmissionError(Exception):
    """A delegation that never reached a backend because the gate did not let it."""


class AdmissionTimedOut(AdmissionError):
    """Waited out `admission_wait_timeout` without ever fitting."""

    def __init__(self, waited: float, rule: str, limit: int) -> None:
        self.waited = waited
        self.rule = rule
        self.limit = limit
        if rule == QUEUED_RULE:
            # Capacity was never the problem: it was still behind other waiters, so the
            # remedy is a longer wait or less concurrent work, not a bigger limit.
            super().__init__(
                f"admission_timed_out: waited {waited:.1f}s for a slot and gave up with "
                f"{limit} request(s) still ahead of it in the queue. Admission is "
                f"first-come-first-served, so this one never reached the front rather "
                f"than being refused by a limit. Raise DELEGATE_ADMISSION_WAIT_TIMEOUT if "
                f"the queue is expected to be this deep, or send less at once; "
                f"backend_status reports the peaks this gate has actually seen."
            )
            return
        super().__init__(
            f"admission_timed_out: waited {waited:.1f}s for a slot and gave up. The rule "
            f"still binding was {rule} (limit {limit}). Either the cluster is saturated "
            f"or that limit is set too low; DELEGATE_ADMISSION_WAIT_TIMEOUT bounds the "
            f"wait, and backend_status reports the peaks this gate has actually seen."
        )


class AdmissionImpossible(AdmissionError):
    """Refused at once: no amount of waiting could ever admit this request.

    Not a timeout: a request larger than the whole token budget does not fit an empty
    gate, so queueing it would spend the whole wait on a failure knowable at once, and
    report it as congestion, which is the one thing it is not.
    """

    def __init__(self, tokens: int, budget: int) -> None:
        super().__init__(
            f"admission_impossible: this delegation is estimated at {tokens} tokens, "
            f"which is above the whole DELEGATE_KV_TOKEN_BUDGET of {budget}. It would "
            f"not fit even against an idle cluster. Send less context, or raise the "
            f"budget if the measured KV pool actually supports it."
        )


@dataclass(frozen=True, slots=True)
class AdmissionLease:
    """What was counted, so releasing subtracts exactly what acquiring added."""

    tokens: int
    entry_key: str
    # What the gate saw at grant: sequences in flight, and waiters still queued. Not
    # rules: they let the delegation price its reply for the concurrency it is about to
    # meet. The cluster's gauge cannot say that, because a sibling admitted moments ago is
    # invisible to `num_requests_running` while it prefills.
    seqs_at_grant: int = 0
    waiting_at_grant: int = 0
    # This request's own wait, not the gate's total: "was *this* delegation slow because
    # it queued", asked of one dispatch after the fact.
    waited: float = 0.0
    # False when the shared file was unreachable at grant, so the slot exists only in this
    # process's counters. Releasing it to the file would give back one of this process's
    # *other* slots there.
    shared: bool = True


class Admission:
    """The four-rule gate. One per process, over counters the whole machine shares."""

    @property
    def effective_token_budget(self) -> int:
        """The lower of what the operator allowed and what the machine actually has.

        A configured budget above the real pool fails silently: over-admitting queues and
        preempts rather than erroring, so nothing announces it, and a constant drifts
        whenever the pool moves.

        **Not the silent override `WindowCheck` refuses.** That validates and never derives,
        because `context_window` is the operator's claim about a *model*, and adopting the
        server's figure would overrule them. These two are
        ceilings on the same physical thing, so the lower overrules neither: the operator's
        number still caps, and so does the hardware. `status()` reports both, because a
        ceiling nobody can see would be the silent override after all.
        """
        if self._pool_tokens is None:
            return self._token_budget
        return min(self._token_budget, self._pool_tokens)

    @property
    def pool_known(self) -> bool:
        """Whether the endpoint's KV pool has ever been reported to this gate.

        The dispatch path reads it to scrape the cluster even when the rate memory already
        has an answer. The memory survives reconnects and is almost always warm, so without
        this the pool would never be learned (ADR-0081).
        """
        return self._pool_tokens is not None

    def observe_pool(self, tokens: int | None) -> None:
        """Record what the endpoint says its KV pool is. Ignores anything unusable.

        Fed by the scrape `seed_decode_rate` already makes, which swallows every failure,
        so `None` is ordinary on an endpoint with no metrics. A zero or a negative would
        tighten this gate to nothing and refuse every delegation, which is worse than the
        drift it fixes.
        """
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
            self._pool_tokens = tokens

    def would_bind_on_tokens(self, tokens: int) -> str | None:
        """Which rule an estimate of this size would hit on an idle gate, or None.

        For tests and readers: `_binding` needs a `Totals` and a registry key, neither of
        which bears on the token rule.
        """
        return "kv_token_budget" if tokens > self.effective_token_budget else None

    _pool_tokens: int | None  # declared here because a method above assigns it first

    def __init__(self, cfg: Config, slots: SharedSlots | None = None) -> None:
        self._slots = slots
        self._max_seqs = cfg.max_inflight_seqs
        self._token_budget = cfg.kv_token_budget
        # The pool the endpoint reports, `None` until it does -- not zero, which would
        # tighten this gate to nothing on an endpoint that publishes no metrics.
        self._pool_tokens = None
        self._grace = cfg.admission_starvation_grace
        self._idle_hold = max(0.0, float(cfg.admission_idle_hold))
        self._cond = asyncio.Condition()

        self._inflight_seqs = 0
        # The open burst wait, as a future carrying its answer, or None. A member admitted
        # while one is open takes that answer rather than pricing itself on its arrival
        # position (ADR-0085). A future, not a counter, so a burst waits one window in all
        # and a joiner never waits inside the wait it is joining.
        self._holding: asyncio.Future[tuple[int, int]] | None = None
        self._inflight_tokens = 0
        self._per_entry: dict[str, int] = {}

        # The queue when there is no shared file; with one, the tickets live in the file,
        # since order must hold across processes. `_local_totals` reads these.
        self._next_ticket = 0
        self._waiting: dict[int, dict[str, Any]] = {}

        self._peak_seqs = 0
        self._peak_tokens = 0
        self._peak_per_entry: dict[str, int] = {}

        self._wait_seconds_total = 0.0
        self._wait_seconds_max = 0.0
        self._wait_count = 0
        self._timeouts = 0

    # ---- the predicate -----------------------------------------------------------
    def _binding(
        self, live: Totals, tokens: int, key: str, limit: int
    ) -> tuple[str, int] | None:
        """The first rule that does not admit this request, or None if all four do.

        Returns the rule rather than a bool so a timeout can say which limit it waited
        on: "waited 600s" tells an operator nothing about what to change.

        `live` is every process's usage summed, not this one's.
        """
        if live.seqs >= self._max_seqs:
            return ("max_inflight_seqs", self._max_seqs)
        if live.tokens + tokens > self.effective_token_budget:
            return ("kv_token_budget", self.effective_token_budget)
        if live.per_entry.get(key, 0) >= limit:
            return (f"concurrency for {key}", limit)
        # Last, deliberately. A capacity rule is what an operator can act on, so a timeout
        # must report it whenever it applies; queue position speaks only for a request the
        # capacity rules would have admitted.
        if live.ahead:
            return (QUEUED_RULE, live.ahead)
        return None

    def _local_totals(self) -> Totals:
        """This process's own usage, in the shape the predicate reads.

        Used when there is no shared file, where it is the single-process gate exactly,
        not an approximation of it: one code path, two scopes. `ahead` is left at
        zero for the caller to fill, so this is also what a rival waiter's feasibility is
        judged against.
        """
        return Totals(
            seqs=self._inflight_seqs,
            tokens=self._inflight_tokens,
            per_entry=self._per_entry,
            waiting=len(self._waiting),
        )

    def _local_ahead(
        self, ticket: int | None, fits: Callable[[dict[str, Any]], bool]
    ) -> int:
        """The same rule `SharedSlots._ahead_of` applies, over the in-process queue."""
        return sum(
            1
            for other, spec in self._waiting.items()
            if (ticket is None or other < ticket) and fits(spec)
        )

    async def _try_take(
        self,
        tokens: int,
        key: str,
        limit: int,
        ticket: int | None,
        *,
        since: float,
    ) -> tuple[tuple[str, int] | None, int | None, dict[str, int]]:
        """Test the rules and, if they admit, take the slot. One atomic step.

        Atomic in both scopes. In the process the caller holds the condition; across
        processes `SharedSlots.admit` holds the file lock while it evaluates the
        predicate, so no process decides against totals another has already read.
        Returning totals to decide on afterwards is the time-of-check race this shape
        makes unrepresentable.

        Returns `(binding, ticket, seen)`. The ticket is assigned on the first refusal and
        passed back on every attempt after, so the caller keeps one place in line for the
        whole wait; admitting gives it back in the same atomic step, so a slot and a queue
        position are never held at once.
        """

        # `since` is the caller's first attempt, not stamped here: this runs once per
        # retry, and a per-attempt stamp would measure the gap between polls, so nothing
        # could age. Wall clock, because the shared file compares across processes.
        spec: dict[str, Any] = {
            "tokens": tokens, "key": key, "limit": limit,
            "since": since,
        }
        seen: dict[str, int] = {}

        def decide(live: Totals) -> tuple[str, int] | None:
            binding = self._binding(live, tokens, key, limit)
            if binding is None:
                # The predicate's own read, under the lock that grants the slot: exactly
                # what admission decided against, at no second look at the file. Only an
                # admission records, since a refusal describes a cluster not joined.
                seen["seqs"] = live.seqs
                seen["waiting"] = live.waiting
            return binding

        def rival_fits(live: Totals, other: dict[str, Any]) -> bool:
            """Could the waiter described by `other` be admitted against these totals?

            Capacity rules only, `ahead` at zero: asking whether a rival is itself at the
            front would recurse, and the question is narrower -- can it spend its turn.

            One exception, the whole anti-starvation mechanism (ADR-0068). A waiter passed
            over for `admission_starvation_grace` counts as ahead whether or not it fits.
            Otherwise a waiter needing more budget than its successors is never feasible
            when they ask -- they hold the budget it lacks -- so they overtake it for as
            long as they arrive. Aged, it is a barrier: arrivals queue, in-flight work
            drains, and the budget falls to it. Inside the grace overtaking is intended,
            which keeps this from becoming head-of-line blocking.
            """
            if self._grace > 0:
                since = other.get("since")
                if isinstance(since, (int, float)) and (
                    time.time() - since >= self._grace
                ):
                    return True
            return (
                self._binding(
                    live,
                    int(other.get("tokens", 0)),
                    str(other.get("key", "")),
                    int(other.get("limit", 0)),
                )
                is None
            )

        if self._slots is not None:
            try:
                binding, ticket = await self._slots.admit(
                    tokens=tokens,
                    entry_key=key,
                    decide=decide,
                    rival_fits=rival_fits,
                    spec=spec,
                    ticket=ticket,
                )
            except SlotsUnavailable:
                # Degrade like every shared-file call here: this process's own counts,
                # queue order lost for this attempt. The ticket is kept, so a refusal still
                # waits in its place and an admission still gives it back.
                log.warning(
                    "the shared slot file was unreachable; admitting on this process's "
                    "own counts"
                )
                binding = decide(self._local_totals())
                if binding is None:
                    self._take_locally(tokens, key)
                    seen["local_only"] = 1
                    return None, ticket, seen
        else:
            base = self._local_totals()
            ahead = self._local_ahead(ticket, lambda s: rival_fits(base, s))
            binding = decide(replace(base, ahead=ahead))
            if binding is not None:
                if ticket is None:
                    ticket = self._next_ticket
                    self._next_ticket += 1
                self._waiting[ticket] = spec
            else:
                if ticket is not None:
                    self._waiting.pop(ticket, None)
                ticket = None
        if binding is not None:
            return binding, ticket, seen
        self._take_locally(tokens, key)
        return None, None, seen

    async def _drop_ticket(self, ticket: int) -> None:
        """Give up a place in line without having taken a slot.

        Notifies, because a shorter queue is what lets the next waiter in this process
        proceed, and nothing else would wake it before its tick expires. Waiters in other
        processes still find out by polling.
        """
        if self._slots is not None:
            try:
                await self._slots.drop_ticket(ticket)
            except SlotsUnavailable:
                # Bounded by `_TICKET_STALE_AFTER_SECONDS` in `slots.py`, which logs when
                # it fires. Refusing to go on would make an unreachable file a failed
                # delegation.
                log.warning(
                    "could not give up admission ticket %d; it will expire", ticket
                )
        async with self._cond:
            self._waiting.pop(ticket, None)
            self._cond.notify_all()

    def _take_locally(self, tokens: int, key: str) -> None:
        """Mirror the slot into this process's own counters and peaks.

        The rules test the shared file; these are this process's share in `status()`, and
        what the peaks are measured over.
        """
        self._inflight_seqs += 1
        self._inflight_tokens += tokens
        self._per_entry[key] = self._per_entry.get(key, 0) + 1

        self._peak_seqs = max(self._peak_seqs, self._inflight_seqs)
        self._peak_tokens = max(self._peak_tokens, self._inflight_tokens)
        self._peak_per_entry[key] = max(
            self._peak_per_entry.get(key, 0), self._per_entry[key]
        )

    # ---- acquire and release -----------------------------------------------------
    async def acquire(
        self,
        tokens: int,
        *,
        entry_key: str,
        entry_limit: int,
        deadline: float | None = None,
        on_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> AdmissionLease:
        if tokens > self._token_budget:
            raise AdmissionImpossible(tokens, self._token_budget)

        started = time.monotonic()
        # Beside `started`: that measures this wait and must not jump if the clock is set,
        # while the age a rival judges us by must compare across processes, which only
        # wall clock does.
        queued_at = time.time()
        waited = False
        ticket: int | None = None

        # `finally`, not a handler per exit: a ticket left at the head of the queue starves
        # every later waiter for the life of this process, so giving it back on a timeout,
        # a cancellation or anything else cannot be something a path opts into.
        try:
            async with self._cond:
                while True:
                    binding, ticket, seen = await self._try_take(
                        tokens, entry_key, entry_limit, ticket, since=queued_at
                    )
                    if binding is None:
                        break
                    # After the predicate, never before: a request that fits is admitted
                    # even past its deadline, since failing one that could run would be the
                    # gate causing the outage it exists to prevent. "Fits" includes being
                    # at the front, so this grace never lets a late arrival overtake.
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        self._timeouts += 1
                        self._record_wait(now - started)
                        raise AdmissionTimedOut(now - started, *binding)
                    waited = True

                    # Another process's release notifies nothing, so this also polls:
                    # short enough to take a freed slot promptly, long enough not to
                    # re-read a file forever. A local release still wakes at once.
                    slice_ = _WAIT_TICK_SECONDS if self._slots is None else _POLL_SECONDS
                    if deadline is not None:
                        slice_ = min(slice_, max(deadline - now, 0.001))
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=slice_)
                    except TimeoutError:
                        # Nothing released; re-test the predicate and deadline at the top.
                        pass
                    if on_wait is not None:
                        await on_wait()
        finally:
            # None once admitted, since `_try_take` gives the ticket back as it takes the
            # slot. So this fires only on a path that never got one, or on a local
            # admission made while the file was unreachable, which still holds it.
            if ticket is not None:
                await self._drop_ticket(ticket)

        elapsed = time.monotonic() - started if waited else 0.0
        if waited:
            self._record_wait(elapsed)

        shared = not seen.get("local_only")
        seqs_at_grant, waiting_at_grant = await self._settle_burst(
            seen.get("seqs", 0),
            seen.get("waiting", 0),
            tokens=tokens,
            entry_key=entry_key,
            elapsed=elapsed,
            shared=shared,
        )

        return AdmissionLease(
            tokens=tokens, entry_key=entry_key, waited=elapsed,
            seqs_at_grant=seqs_at_grant, waiting_at_grant=waiting_at_grant,
            shared=shared,
        )

    async def _settle_burst(  # noqa: PLR0913 -- the snapshot, and the lease it may give back
        self, seqs: int, waiting: int, *, tokens: int, entry_key: str, elapsed: float,
        shared: bool = True,
    ) -> tuple[int, int]:
        """What this request should say it met, once the burst around it has settled.

        A request that finds the gate empty waits, because its snapshot says "solo" however
        many siblings are a millisecond behind it, and that label keys the rate memory
        (ADR-0085).

        A request that finds a wait open takes its answer. Its own snapshot sees the
        siblings *ahead* of it and none still arriving, so `seqs + waiting + 1` is its
        position in the burst, not the burst's size: a simultaneous three would price 1, 2
        and 3.

        A wait open in *another* process is joined by counting, since no future crosses a
        process boundary: the shared record's flag says a wait is open, and the member runs
        its own window against the same shared totals. Otherwise a burst spread over
        processes, the shape `run` fans out in, prices each arm at its arrival position.

        One wait serves the whole burst. A second would re-count the same arrivals and bill
        each member its own window, and a joiner must not wait *inside* the wait it joins.

        The wait is *outside* the condition, because holding it would block the siblings
        being counted and so guarantee the answer it measures. The slot is already taken,
        which makes that safe: nothing can overtake, and a sibling can still be admitted.

        "Already taken" is also the danger, hence the guards. `admit` releases in the
        `finally` of a `try` it enters only once `acquire` returns, so anything raised in
        here -- a cancellation, in practice -- would leave a slot with no owner, and
        `slots.py` reclaims a record only once its process stops.
        """
        if self._idle_hold <= 0:
            return seqs, waiting

        lease = AdmissionLease(
            tokens=tokens, entry_key=entry_key, waited=elapsed, shared=shared
        )
        open_wait = self._holding
        if open_wait is None and not (seqs == 0 and waiting == 0):
            try:
                elsewhere = await self._burst_open_elsewhere()
            except BaseException:
                # An await on a file lock after the slot was taken: nobody else can give
                # it back.
                await self.release(lease)
                raise
            if not elsewhere:
                return seqs, waiting
            # That await may have let a sibling here open a wait. Join it, never replace
            # it: `_settle_waiters` settles only the current future, so whoever joined a
            # replaced one would wait for ever.
            open_wait = self._holding
        if open_wait is not None:
            try:
                return await asyncio.shield(open_wait)
            except asyncio.CancelledError:
                # This call was cancelled, or the opener was: the opener's `CancelledError`
                # arrives through the shared future looking exactly like our own. Only
                # `cancelling()` tells them apart, so cancelling one call of a fan-out
                # does not fail every sibling that joined its wait.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    await self.release(lease)
                    raise
                return seqs, waiting
            except Exception:
                # The opener failed. Its siblings are not implicated, and keep their own
                # snapshot.
                return seqs, waiting

        self._holding = asyncio.get_running_loop().create_future()
        try:
            # Published before the first window, so a sibling arriving during it joins
            # rather than reading its own arrival position.
            #
            # Inside the `try`, and that is load-bearing: nothing may be awaited between
            # creating `_holding` and entering the block that settles it. A cancellation
            # there would leave the future unsettled for ever -- every later burst here
            # would join a wait nobody finishes -- and would leak the slot.
            await self._announce_burst(open_wait=True)
            counted = await self._count_the_burst()
        except BaseException as exc:
            await self._announce_burst(open_wait=False)
            self._settle_waiters(exc)
            await self.release(lease)
            raise
        await self._announce_burst(open_wait=False)
        self._settle_waiters(counted)
        return counted

    async def _burst_open_elsewhere(self) -> bool:
        """Whether another process on this machine is already counting a burst.

        Across processes there is no future to await, so the wait is announced in the
        shared file and a member that finds one counts the burst itself. Both windows run
        at once over the same totals, so every arm settles on the whole burst, and the
        joiner pays one window, as an in-process joiner does for the same answer.

        Asked only when the local snapshot is not idle and the hold is on, so an idle gate
        and a gate with the hold off cost nothing extra.
        """
        if self._slots is None or self._idle_hold <= 0:
            # With the hold off there is no window to join: reading another process's flag
            # would add work and tie together gates configured not to wait for each other.
            return False
        try:
            return await self._slots.burst_wait_elsewhere()
        except SlotsUnavailable:
            # The file became unreachable since the slot was taken. Not joining prices low
            # rather than failing a delegation already admitted.
            return False

    async def _announce_burst(self, *, open_wait: bool) -> None:
        """Say in the shared file that this process is, or is no longer, counting.

        Best effort, like `release`: the slot is already taken, so an unreachable file
        must cost a label, not a delegation. A flag stranded by a failed close expires two
        windows after its last renewal, and `_count_the_burst` renews it every window.
        """
        if self._slots is None:
            return
        try:
            if open_wait:
                await self._slots.open_burst_wait(expires_in=2 * self._idle_hold)
            else:
                await self._slots.close_burst_wait()
        except SlotsUnavailable:
            log.warning(
                "could not %s the shared burst wait; this burst may price on arrival "
                "order in other processes",
                "announce" if open_wait else "withdraw",
            )

    def _settle_waiters(self, outcome: tuple[int, int] | BaseException) -> None:
        """Hand the open wait's outcome to whoever joined it, and close it."""
        pending, self._holding = self._holding, None
        if pending is None or pending.done():
            return
        if isinstance(outcome, BaseException):
            pending.set_exception(outcome)
            # Retrieved here so a wait nobody joined does not log a stray exception.
            pending.exception()
        else:
            pending.set_result(outcome)

    async def _count_the_burst(self) -> tuple[int, int]:
        """Wait out the burst behind an idle gate, and report what arrived.

        A debounce, not a flat wait (ADR-0088). A client staggers a fan-out over tens of
        seconds, so one window would close with a few of the burst counted and file the
        sample under a contention it never met. A window ends the hold only if nothing
        arrived during it.

        A full gate ends it at once: the burst has reported its size, and waiting for a
        quiet window is pure latency. That is also the ceiling, so no separate cap: it runs
        longer only while arrivals keep coming as earlier ones complete, which costs one
        call's dispatch latency, not a stuck gate.

        A longer flat hold would not do: it fires only on an idle gate, the single
        interactive delegation, and would bill that call for a burst that never comes. One
        quiet window costs it no more than the flat hold did.
        """
        while True:
            before, _, _ = await self._burst_view()
            await asyncio.sleep(self._idle_hold)
            # Read after the wait, not reported from before it.
            arrived, seqs, waiting = await self._burst_view()
            if arrived >= self._max_seqs or arrived == before:
                return seqs, waiting
            # Still counting, so the flag other processes join by must not expire.
            await self._announce_burst(open_wait=True)

    async def _burst_view(self) -> tuple[int, int, int]:
        """Everything in or waiting for the gate, then the two numbers a lease carries.

        Shared totals wherever there is a file, so a burst across processes -- the shape
        `run` fans out in -- counts as one. The local counters are the whole gate only when
        there is no file.

        The second number excludes this request: its slot is already taken, and the
        caller's formula was written against pre-grant numbers.
        """
        if self._slots is not None:
            totals, _, waiting = await self._slots.snapshot()
            return totals.seqs + waiting, max(totals.seqs - 1, 0), waiting
        async with self._cond:
            seqs, waiting = self._inflight_seqs, len(self._waiting)
        return seqs + waiting, max(seqs - 1, 0), waiting

    def _record_wait(self, seconds: float) -> None:
        self._wait_seconds_total += seconds
        self._wait_seconds_max = max(self._wait_seconds_max, seconds)
        self._wait_count += 1

    async def release(self, lease: AdmissionLease) -> None:
        async with self._cond:
            if self._slots is not None and lease.shared:
                # Best effort. A slot not given back is reclaimed once this process
                # exits, since its record is keyed by a PID that is then dead, so the
                # leak lasts the process, not for ever. Refusing the local release as
                # well would wedge this process for good.
                try:
                    await self._slots.release(
                        tokens=lease.tokens,
                        entry_key=lease.entry_key,
                    )
                except SlotsUnavailable:
                    log.warning(
                        "could not return a slot to the shared file; it will be "
                        "reclaimed when this process exits"
                    )
            self._inflight_seqs -= 1
            self._inflight_tokens -= lease.tokens
            remaining = self._per_entry.get(lease.entry_key, 0) - 1
            if remaining > 0:
                self._per_entry[lease.entry_key] = remaining
            else:
                self._per_entry.pop(lease.entry_key, None)
            # Each waiter tests its own predicate -- its size, its endpoint -- so waking
            # one could wake one this release does not help while the right one sleeps.
            self._cond.notify_all()

    @asynccontextmanager
    async def admit(
        self,
        tokens: int,
        *,
        entry_key: str,
        entry_limit: int,
        deadline: float | None = None,
        on_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[AdmissionLease]:
        """Hold a slot for the body. Releases on every exit path, exceptions included."""
        lease = await self.acquire(
            tokens,
            entry_key=entry_key,
            entry_limit=entry_limit,
            deadline=deadline,
            on_wait=on_wait,
        )
        try:
            yield lease
        finally:
            await self.release(lease)

    # ---- what the operator reads -------------------------------------------------
    @property
    def inflight_seqs(self) -> int:
        """Requests this process currently holds slots for.

        A property rather than a `status()` key, because the rate sampler, the one hot
        reader, asks it every tick, and `status()` builds a dozen-key dict.
        """
        return self._inflight_seqs

    def status(self) -> dict[str, Any]:
        """Live gauges, high-water marks and wait totals. ADR-0012's reporting half."""
        return {
            "inflight_seqs": self._inflight_seqs,
            "inflight_tokens": self._inflight_tokens,
            "peak_inflight_seqs": self._peak_seqs,
            "peak_inflight_tokens": self._peak_tokens,
            # Three numbers, because the lowered ceiling is honest only if a reader can see
            # both halves and which one binds.
            "kv_token_budget": self._token_budget,
            "kv_token_budget_effective": self.effective_token_budget,
            "kv_cache_size_tokens_seen": self._pool_tokens,
            "per_entry": {
                key: {"inflight": self._per_entry.get(key, 0), "peak": peak}
                for key, peak in sorted(self._peak_per_entry.items())
            },
            "admission_wait_seconds_total": round(self._wait_seconds_total, 3),
            "admission_wait_seconds_max": round(self._wait_seconds_max, 3),
            "admission_wait_count": self._wait_count,
            "admission_timeouts": self._timeouts,
            # This process's own queue depth. Zero with a shared file, whose machine-wide
            # queue `cross_process` reports -- the same split as the gauges above.
            "queued_waiters": len(self._waiting),
        }
