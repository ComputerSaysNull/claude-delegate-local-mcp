"""The gate every delegation passes before it reaches a backend. ADR-0012.

The per-request context ceiling and the concurrent-sequence ceiling are ceilings, not
reservations. What actually constrains the cluster is that summed live tokens stay under
the KV pool, so six full-context requests are impossible where six fifth-size ones fit
comfortably. Oversubscription queues rather than fails, which makes this a latency
protection rather than a correctness one -- and the queueing can be severe, because large
cold prefills serialise.

**"Queues" now means a place in line, which it did not until 2026-09-04.** The sentence
above was true about waiting and false about ordering: `acquire` was a re-test loop, so a
waiter that had queued for nine minutes had no claim ahead of a request arriving that
instant. Across processes it was worse than unordered -- a release elsewhere notifies
nothing, so waiting is polling, and the winner was whoever polled at the right moment.
Every waiter now takes a ticket and the predicate refuses anyone who is not at the head,
so the wait is bounded by the work ahead of it rather than by luck. Waiting is still
polling: fairness decides *who* goes next, not how promptly anyone finds out.

**Three capacity rules, checked as one predicate.** Total in-flight sequences, summed
token estimate against the budget, and the endpoint's own declared limit. Queue position
is a fourth reason `_binding` can refuse on, which is why the class below calls it a
four-rule gate: capacity says whether a request *fits*, the queue says whether it is
*next*, and both must hold. One predicate rather
than three semaphores acquired in turn: a request that takes a sequence slot and then
blocks on another rule holds capacity it is not using for the whole wait, starving smaller
requests that would have fit every rule. Nothing here is ever partially acquired. A waiter
that does not fit holds nothing.

There were four until 2026-09-13. A cap on concurrent large cold prefills measured as pure
overhead -- 286.3s of aggregate waiting for a batch 12.1s slower end to end -- and fired
`admission_wait_timeout` four times on a cluster at 3% KV use with zero preemptions. The
engine serialises cold prefills itself, which is the cap's own justification arriving from
somewhere that does not need configuring (ADR-0077).

**Undersubscription is invisible where oversubscription announces itself**, which is why
this counts as well as gates. The high-water marks and wait totals `status()` returns are
how the four constants stop being guesses -- `backend_status` surfaces them, and a peak
that never approaches its ceiling is evidence the ceiling is too low.

Two different numbers size a request, and conflating them is a trap. Its **KV footprint**
is the prompt plus the reply it is permitted to generate, and that is what the token
budget counts. Its **prefill** is the prompt alone, and that is what decides whether it is
a large cold prefill -- decode is not prefill, and a reply allowance above the threshold
would otherwise make every request "large" and quietly bound the whole server at
`max_inflight_large_prefills`.

The estimate a lease holds is fixed when it is granted and never grows. A long agentic
delegation's true footprint can exceed it late in the loop, so the token rule is a
floor-time approximation rather than a running total. Growing it per turn would couple
this module to the turn loop's internals and add a reconciliation path on every abort;
`peak_inflight_tokens` is the cheaper way to find out whether that trade was wrong.

One instance per process, and -- since ADR-0040 -- one shared counter file across every
process on the machine. Sharing the object within a process was never sufficient: the
transport is stdio, so two editor windows are two servers, and four rules that each
bound a session bound the cluster at the configured limit times the number of windows
open. `slots.py` owns that file; this module owns what the numbers in it mean.
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

# Well under the client's 1800s stdio idle timeout, because a wait is time in which no
# turn happens and so nothing else pings (ADR-0018). Not a config field: it is tied to
# the client's timer rather than to anything an operator tunes.
_WAIT_TICK_SECONDS = 30.0

# How often a waiter re-tests the shared file. Another process's release cannot notify
# this one's condition, so cross-process waiting is polling and there is no way around
# that short of a broker. Cheap: the critical section is a small document on tmpfs.
_POLL_SECONDS = 0.25


log = logging.getLogger(__name__)

# The fifth thing that can refuse a request, and the only one that is not about capacity.
# Named rather than inlined because `AdmissionTimedOut` phrases it differently: "limit 3"
# is meaningless for a queue position, where the number is how many are ahead.
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
            # Capacity was never the problem for this one: it was still behind other
            # waiters when the clock ran out, so the answer is a longer wait or less
            # concurrent work, not a bigger limit.
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

    Distinct from a timeout on purpose. A request estimated larger than the whole token
    budget does not fit an empty gate, so queueing it spends the entire wait to reach a
    failure that was knowable immediately -- and reports it as congestion, which is the
    one thing it is not.
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
    """What was counted, so releasing subtracts exactly what acquiring added.

    Until 2026-09-13 it also carried whether the request was a large cold prefill, and
    whether that half of the lease was still held. Both existed only to enforce
    `max_inflight_large_prefills`, which measured as pure overhead and was removed
    (ADR-0077).
    """

    tokens: int
    entry_key: str
    # What the gate saw at the instant it granted this slot: sequences already in flight,
    # and everyone still queued behind. Neither is a rule -- they are carried so the
    # delegation can price its reply for the concurrency it is about to *meet* rather than
    # the one the cluster happens to be showing. The cluster's own gauge cannot answer
    # that: a lease is taken before the request is issued, so a sibling admitted moments
    # ago is invisible to `num_requests_running` while it prefills. Measured 2026-09-11:
    # six delegations fanned out in one message each read that gauge at 0 or 1.
    seqs_at_grant: int = 0
    waiting_at_grant: int = 0
    # This request's own wait, not the running total. The gate's counters answer "is the
    # cluster saturated"; this answers "was *this* delegation slow because it queued",
    # which is the question asked of one dispatch after the fact.
    waited: float = 0.0


class Admission:
    """The four-rule gate. One per process, over counters the whole machine shares."""

    @property
    def effective_token_budget(self) -> int:
        """The lower of what the operator allowed and what the machine actually has.

        `kv_token_budget` describes itself as sitting just under the measured KV pool, and
        by 2026-09-13 it did not: 2,400,000 configured against a `kv_cache_size_tokens` of
        1,467,988, about 1.64x. Nothing had failed, which is why it went unnoticed --
        over-admitting queues and preempts rather than erroring, so this protects latency
        and cannot announce that it has stopped. A new constant would drift the same way
        the next time the pool moves, as it did on the 2026-09-04 model swap.

        **Not the silent override `WindowCheck` refuses.** That validates and never derives,
        because `context_window` is the operator's claim about a *model* and adopting the
        server's figure would overrule them. These two are ceilings on the same physical
        thing, so taking the lower overrules neither: the operator's number still caps, and
        so does the hardware. Both are reported in `status()`, because a ceiling nobody can
        see would be the silent override after all.
        """
        if self._pool_tokens is None:
            return self._token_budget
        return min(self._token_budget, self._pool_tokens)

    @property
    def pool_known(self) -> bool:
        """Whether the endpoint's KV pool has ever been reported to this gate.

        Read by the dispatch path to decide whether the cluster still needs scraping when
        the rate memory already has an answer. Without it the scrape is skipped whenever the
        memory is warm -- which, since the memory started surviving reconnects, is almost
        always -- and the pool is never learned at all (ADR-0081).
        """
        return self._pool_tokens is not None

    def observe_pool(self, tokens: int | None) -> None:
        """Record what the endpoint says its KV pool is. Ignores anything unusable.

        Called from the dispatch path, where `seed_decode_rate` already scrapes the cluster
        to price the first turn and `kv_cache_size_tokens` arrives in the same payload. That
        read swallows every failure by design, so `None` is the ordinary case on an endpoint
        publishing no metrics -- and a zero or a negative would tighten this gate to nothing
        and refuse every delegation, which is a worse failure than the drift it fixes.
        """
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
            self._pool_tokens = tokens

    def would_bind_on_tokens(self, tokens: int) -> str | None:
        """Which rule an estimate of this size would hit on an idle gate, or None.

        Exists for the tests and for a reader: `_binding` needs a `Totals` and a registry
        key, and neither says anything about the token rule on its own.
        """
        return "kv_token_budget" if tokens > self.effective_token_budget else None

    def __init__(self, cfg: Config, slots: SharedSlots | None = None) -> None:
        self._slots = slots
        self._max_seqs = cfg.max_inflight_seqs
        self._token_budget = cfg.kv_token_budget
        # The pool the endpoint says it has, once anything has looked. `None` until then,
        # which is different from zero: an endpoint publishing no metrics must leave the
        # configured value standing rather than tighten this gate to nothing.
        self._pool_tokens: int | None = None
        self._grace = cfg.admission_starvation_grace
        self._idle_hold = max(0.0, float(cfg.admission_idle_hold))
        self._cond = asyncio.Condition()

        self._inflight_seqs = 0
        # The burst wait currently open, as a future carrying its answer, or None. A member
        # admitted while one is open takes that answer instead of pricing itself on the
        # position it happened to arrive in -- the half ADR-0085 left undone. A future
        # rather than a counter, so a burst waits one window between them and a joiner
        # never waits inside the wait it is joining.
        self._holding: asyncio.Future[tuple[int, int]] | None = None
        self._inflight_tokens = 0
        self._per_entry: dict[str, int] = {}

        # The queue, for the no-shared-file case only. With a shared file the tickets live
        # in it, because ordering has to hold across processes to mean anything -- these
        # two are then unused, and `_local_totals` is what reads them.
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
        on. "Waited 600s" tells an operator nothing about what to change.

        `live` is every process's usage summed, not this one's. The rules themselves are
        unchanged from when they read local attributes: that was a bug about scope, not
        a bug about policy.
        """
        if live.seqs >= self._max_seqs:
            return ("max_inflight_seqs", self._max_seqs)
        if live.tokens + tokens > self.effective_token_budget:
            return ("kv_token_budget", self.effective_token_budget)
        if live.per_entry.get(key, 0) >= limit:
            return (f"concurrency for {key}", limit)
        # Last, and the order is deliberate. A capacity rule is what an operator can act
        # on, so it must be the one a timeout reports whenever it applies; queue position
        # speaks only for a request the four rules would otherwise have admitted.
        if live.ahead:
            return (QUEUED_RULE, live.ahead)
        return None

    def _local_totals(self) -> Totals:
        """This process's own usage, in the shape the predicate reads.

        Used when there is no shared file, which reproduces the pre-ADR-0040 behaviour
        exactly rather than approximating it -- one code path, two scopes. `ahead` is left
        at zero here and filled in by the caller, so this is also what a rival waiter's
        feasibility is judged against.
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

        Atomic in both scopes, for the same reason. Within the process the caller holds
        the condition; across processes `SharedSlots.admit` holds the file lock while it
        evaluates this predicate, so no other process can decide against totals we have
        already read. Handing back totals and deciding afterwards is the time-of-check
        race this shape exists to make unrepresentable.

        Returns `(binding, ticket)`. The ticket is assigned on the first refusal and
        returned on every attempt after it, so the caller carries one place in line for
        the whole wait; admitting gives it back inside the same atomic step, which is what
        stops a slot and a queue position ever being held at once.
        """

        # `since` is the caller's own first attempt, passed in rather than stamped here:
        # this runs once per retry, and a timestamp refreshed on every attempt would
        # measure the gap between polls instead of the wait, so nothing could ever age.
        # Wall clock, not monotonic, because the shared file compares across processes.
        spec: dict[str, Any] = {
            "tokens": tokens, "key": key, "limit": limit,
            "since": since,
        }
        seen: dict[str, int] = {}

        def decide(live: Totals) -> tuple[str, int] | None:
            binding = self._binding(live, tokens, key, limit)
            if binding is None:
                # Captured from the predicate's own read, under the same lock that grants
                # the slot, so the numbers are exactly what admission decided against and
                # cost no second look at the file. Only the admitting call is recorded:
                # a refusal describes a cluster this request did not join.
                seen["seqs"] = live.seqs
                seen["waiting"] = live.waiting
            return binding

        def rival_fits(live: Totals, other: dict[str, Any]) -> bool:
            """Could the waiter described by `other` be admitted against these totals?

            The capacity rules only, with `ahead` left at zero: asking whether a rival is
            itself at the front would recurse, and the question here is narrower -- is it
            spending its turn, or holding one it cannot spend.

            One exception, and it is the whole anti-starvation mechanism. A waiter that
            has been passed over for `admission_starvation_grace` counts as ahead whether
            or not it currently fits. Without it a waiter needing more of a shared budget
            than its successors is never feasible at the instant they ask -- they hold
            the very budget it is short of -- so it is never counted, and they overtake
            it for as long as they keep arriving. Aging makes it a barrier instead: the
            arrivals queue, the in-flight work drains, and the budget falls to it. The
            grace is what keeps this from becoming the head-of-line blocking the queue
            check was written to avoid; inside it, overtaking is still the intent.
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
            binding, ticket = await self._slots.admit(
                tokens=tokens,
                entry_key=key,
                decide=decide,
                rival_fits=rival_fits,
                spec=spec,
                ticket=ticket,
            )
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

        Notifies, because the queue shrinking is exactly what lets the next waiter in this
        process proceed, and nothing else would wake it until its own tick expires. A
        release elsewhere on the machine still cannot notify us; that is what the poll is
        for and this does not change it.
        """
        if self._slots is not None:
            try:
                await self._slots.drop_ticket(ticket)
            except SlotsUnavailable:
                # Bounded by `_TICKET_STALE_AFTER_SECONDS` in `slots.py` rather than
                # permanent, and logged there when it fires. Refusing to continue here
                # would turn an unreachable file into a failed delegation.
                log.warning(
                    "could not give up admission ticket %d; it will expire", ticket
                )
        async with self._cond:
            self._waiting.pop(ticket, None)
            self._cond.notify_all()

    def _take_locally(self, tokens: int, key: str) -> None:
        """Mirror the slot into this process's own counters and peaks.

        The shared file is what the rules are tested against; these are what `status()`
        reports as this process's share, and what the peaks are measured over.
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
        # Beside `started`, not instead of it: `started` measures this wait and must not
        # jump if the clock is set, while the age a rival judges us by has to be
        # comparable across processes, which only wall clock is.
        queued_at = time.time()
        waited = False
        ticket: int | None = None

        # `finally` and not a handler per exit: the ticket must be given back on a
        # timeout, on cancellation and on anything else raised out of the wait alike. A
        # ticket left at the head of the queue starves every later waiter for as long as
        # this process lives, so the release cannot be something a code path opts into.
        try:
            async with self._cond:
                while True:
                    binding, ticket, seen = await self._try_take(
                        tokens, entry_key, entry_limit, ticket, since=queued_at
                    )
                    if binding is None:
                        break
                    # Checked after the predicate, never before: a request that fits is
                    # admitted even if its deadline has just passed. Failing one that
                    # could have run would be the gate causing the outage it exists to
                    # prevent. Since 2026-09-04 "fits" includes being at the front of the
                    # queue, so that grace no longer reaches a request the rules would
                    # admit but whose turn it is not -- deliberately, because the
                    # alternative is letting a late arrival overtake on the way out.
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        self._timeouts += 1
                        self._record_wait(now - started)
                        raise AdmissionTimedOut(now - started, *binding)
                    waited = True

                    # A release in *another* process notifies nothing here, so waiting is
                    # also polling. Short enough that a freed slot is taken promptly, long
                    # enough that an idle machine is not re-reading a file forever. A local
                    # release still wakes the condition at once and does not wait this out.
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
            # None once admitted: `_try_take` gives the ticket back inside the same step
            # that takes the slot, so this only fires on a path that never got one.
            if ticket is not None:
                await self._drop_ticket(ticket)

        elapsed = time.monotonic() - started if waited else 0.0
        if waited:
            self._record_wait(elapsed)

        seqs_at_grant, waiting_at_grant = await self._settle_burst(
            seen.get("seqs", 0),
            seen.get("waiting", 0),
            tokens=tokens,
            entry_key=entry_key,
            elapsed=elapsed,
        )

        return AdmissionLease(
            tokens=tokens, entry_key=entry_key, waited=elapsed,
            seqs_at_grant=seqs_at_grant, waiting_at_grant=waiting_at_grant,
        )

    async def _settle_burst(
        self, seqs: int, waiting: int, *, tokens: int, entry_key: str, elapsed: float
    ) -> tuple[int, int]:
        """What this request should say it met, once the burst around it has settled.

        A request that finds the gate empty waits, because its own snapshot says "solo" and
        would go on saying so however many siblings are a millisecond behind it -- and that
        label is what the rate memory is keyed by (ADR-0085).

        A request that finds a wait already open takes its answer, which is the half
        ADR-0085 left undone. Its reasoning was that a later member already sees this one,
        so concurrency is known; it is not. That member sees the siblings *ahead* of it and
        none of those still arriving behind, so `seqs + waiting + 1` is its own position in
        the burst rather than the burst's size, and a simultaneous three priced 1, 2 and 3.

        A wait open in *another* process is joined too, by counting rather than by
        awaiting: there is no future to share across a process boundary, so the flag in
        the shared record says a wait is open and the member runs its own window against
        the same shared totals. Without that, a burst spread over three processes prices
        every arm at its arrival position, which is the shape `run` fans out in.

        One wait serves the whole burst rather than one each. A second would re-count the
        same arrivals and bill every member for its own window, where what is wanted is one
        window and one answer -- and a joiner must not wait *inside* the wait it is joining,
        which is what makes this a shared result rather than a second debounce.

        The wait is deliberately *outside* the condition. Holding it would block the very
        siblings this is counting, so it would guarantee the answer it was trying to
        measure. The slot is already taken, which is what makes that safe: nothing can
        overtake, and a sibling can still be admitted beside us.

        "Already taken" is also what makes it dangerous, hence the guards. `admit` releases
        a lease from the `finally` of its own `try` and cannot reach it until `acquire`
        returns, so anything raised in here -- a cancellation, in practice -- would leave a
        slot with no owner, and `slots.py` reclaims a record only once its process stops.
        """
        if self._idle_hold <= 0:
            return seqs, waiting

        lease = AdmissionLease(tokens=tokens, entry_key=entry_key, waited=elapsed)
        open_wait = self._holding
        if open_wait is not None:
            try:
                return await asyncio.shield(open_wait)
            except asyncio.CancelledError:
                await self.release(lease)
                raise
            except Exception:
                # Whoever opened it failed. Its siblings are not implicated, and their own
                # snapshot is what they would have been given anyway.
                return seqs, waiting

        if not (seqs == 0 and waiting == 0) and not await self._burst_open_elsewhere():
            return seqs, waiting

        self._holding = asyncio.get_running_loop().create_future()
        try:
            # Published before the first window, so a sibling arriving during it sees the
            # wait and joins rather than reading its own arrival position.
            #
            # Inside the `try`, and that placement is load-bearing: this is an await, and
            # nothing may be awaited between creating `_holding` and entering the block
            # that settles it. Outside, a cancellation delivered here would leave the
            # future unsettled for ever -- every later burst in this process would join a
            # wait nobody finishes -- and would leak the slot, the release below being
            # skipped with it.
            await self._announce_burst(open_wait=True)
            counted = await self._count_the_burst()
        except BaseException as exc:
            await self._announce_burst(open_wait=False)
            self._settle_waiters(exc=exc)
            await self.release(lease)
            raise
        await self._announce_burst(open_wait=False)
        self._settle_waiters(counted=counted)
        return counted

    async def _burst_open_elsewhere(self) -> bool:
        """Whether another process on this machine is already counting a burst.

        The cross-process half of the same defect. Within one process a member joins the
        open wait as a future; across processes there is no future to await, so the wait
        is announced in the shared file and a member that finds one counts the burst
        itself. Both windows run at the same time and read the same totals, so every arm
        settles on the whole burst rather than on the siblings ahead of it -- and the
        joiner pays one window it would otherwise have skipped, which is exactly what an
        in-process joiner already pays for the same answer.

        Asked only once the local snapshot is not idle, and only while the hold is
        enabled, so an idle gate and a gate with the hold off both cost nothing extra.
        """
        if self._slots is None or self._idle_hold <= 0:
            # The hold is the debounce this flag exists to widen. With it off there is no
            # window to join, so reading another process's flag could only add work and
            # tie two gates together that were configured not to wait for each other.
            # The docstring above promised this guard before the guard existed.
            return False
        try:
            return await self._slots.burst_wait_elsewhere()
        except SlotsUnavailable:
            # The same call that took the slot read this file a moment ago, so this is
            # a file that has just become unreachable. Not joining is the old behaviour,
            # and it prices low rather than failing a delegation that was admitted.
            return False

    async def _announce_burst(self, *, open_wait: bool) -> None:
        """Say in the shared file that this process is, or is no longer, counting.

        Best effort for the reason `release` is: the slot is already taken and the lease
        is about to be handed back, so an unreachable file must cost a label rather than
        a delegation. A flag stranded by a failed close goes when the record does, which
        is why `_is_idle` must not treat a flagged record as worth keeping -- that would
        make the record permanent and the stranded flag with it.
        """
        if self._slots is None:
            return
        try:
            if open_wait:
                await self._slots.open_burst_wait()
            else:
                await self._slots.close_burst_wait()
        except SlotsUnavailable:
            log.warning(
                "could not %s the shared burst wait; this burst may price on arrival "
                "order in other processes",
                "announce" if open_wait else "withdraw",
            )

    def _settle_waiters(
        self,
        *,
        counted: tuple[int, int] | None = None,
        exc: BaseException | None = None,
    ) -> None:
        """Hand the open wait's outcome to whoever joined it, and close it."""
        pending, self._holding = self._holding, None
        if pending is None or pending.done():
            return
        if exc is not None:
            pending.set_exception(exc)
            # Retrieved here so a wait nobody joined does not log a stray exception.
            pending.exception()
        else:
            pending.set_result(counted)

    async def _count_the_burst(self) -> tuple[int, int]:
        """Wait out the burst behind an idle gate, and report what arrived.

        A debounce, not a flat wait. A client staggers a fan-out -- measured 2026-09-17, six
        calls from one message arrived 5.5s apart over 28.4s -- so a single window closes
        with two or three of six counted and files the sample under a contention it never
        met. Each window therefore ends the hold only if nothing arrived during it.

        A full gate ends it at once: the burst has already reported its size, and waiting for
        a quiet window it has earned is pure latency. That rule is also the ceiling, so no
        separate cap is needed -- the only way to run longer is arrivals that keep coming
        while earlier ones complete, where the cost is one call's dispatch latency rather
        than a stuck gate.

        Raising the flat hold instead would not do: it fires only on an idle gate, which is
        the single interactive delegation, so a longer fixed wait bills that call for a burst
        that never comes. One quiet window leaves it exactly where the flat hold already put
        it.
        """
        while True:
            before, _, _ = await self._burst_view()
            await asyncio.sleep(self._idle_hold)
            # Read after the wait rather than reporting what was read before it.
            arrived, seqs, waiting = await self._burst_view()
            if arrived >= self._max_seqs or arrived == before:
                return seqs, waiting

    async def _burst_view(self) -> tuple[int, int, int]:
        """Everything in or waiting for the gate, then the two numbers a lease carries.

        Shared totals wherever there is a file, so a burst spread across processes counts
        as one burst. That is now an ordinary shape rather than an exotic one, since `run`
        fans out as separate processes. The local counters answer for this process alone,
        which is the whole gate only when there is no file to read.

        The second number is minus one for this request, whose slot is already taken and so
        is counted here where it was not in the pre-grant numbers the caller's formula was
        written against.
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
            if self._slots is not None:
                # Best effort on purpose. A slot this process cannot give back is
                # reclaimed by the next acquirer as soon as this process exits, since
                # a record is keyed by a PID that will no longer be live -- so the
                # leak is bounded by the life of the process rather than permanent.
                # Refusing to release locally because the file was unreachable would
                # wedge this process for good, which is strictly worse.
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
            # Every waiter tests a different predicate -- a different token size, a
            # different endpoint. Waking one could wake the one this release does not
            # help while the one it does stays parked.
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
    def status(self) -> dict[str, Any]:
        """Live gauges, high-water marks and wait totals. ADR-0012's reporting half."""
        return {
            "inflight_seqs": self._inflight_seqs,
            "inflight_tokens": self._inflight_tokens,
            "peak_inflight_seqs": self._peak_seqs,
            "peak_inflight_tokens": self._peak_tokens,
            # Three numbers rather than one, because the lowered ceiling is only honest if
            # a reader can see both halves of it and which one is binding.
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
            # This process's own queue depth. Zero with a shared file, where the queue is
            # machine-wide and `cross_process` reports it -- the same split the gauges
            # above already have, for the same reason.
            "queued_waiters": len(self._waiting),
        }
