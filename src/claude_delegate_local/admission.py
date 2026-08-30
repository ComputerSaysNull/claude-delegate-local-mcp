"""The gate every delegation passes before it reaches a backend. ADR-0012.

The per-request context ceiling and the concurrent-sequence ceiling are ceilings, not
reservations. What actually constrains the cluster is that summed live tokens stay under
the KV pool, so six full-context requests are impossible where six fifth-size ones fit
comfortably. Oversubscription queues rather than fails, which makes this a latency
protection rather than a correctness one -- and the queueing can be severe, because large
cold prefills serialise.

**Four rules, checked as one predicate.** Total in-flight sequences, summed token estimate
against the budget, concurrent large prefills, and the endpoint's own declared limit. The
third is what binds for big tasks and the reason the first three cannot be three
semaphores acquired in turn: a request that takes a sequence slot and then blocks on the
large-prefill cap holds capacity it is not using for the whole wait, starving smaller
requests that would have fit every rule. Nothing here is ever partially acquired. A waiter
that does not fit holds nothing.

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

One instance per process. It is the global budget by construction, so every dispatch path
has to share the one object or it is not global.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .config import Config

# Well under the client's 1800s stdio idle timeout, because a wait is time in which no
# turn happens and so nothing else pings (ADR-0018). Not a config field: it is tied to
# the client's timer rather than to anything an operator tunes.
_WAIT_TICK_SECONDS = 30.0


class AdmissionError(Exception):
    """A delegation that never reached a backend because the gate did not let it."""


class AdmissionTimedOut(AdmissionError):
    """Waited out `admission_wait_timeout` without ever fitting."""

    def __init__(self, waited: float, rule: str, limit: int) -> None:
        self.waited = waited
        self.rule = rule
        self.limit = limit
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

    `is_large` travels rather than being re-derived on release: recomputing it against a
    threshold reached through config a second time is how a counter drifts permanently
    upward, and a large-prefill count that only ever grows wedges the gate for good.
    """

    tokens: int
    is_large: bool
    entry_key: str


class Admission:
    """The four-rule gate. One per process."""

    def __init__(self, cfg: Config) -> None:
        self._max_seqs = cfg.max_inflight_seqs
        self._token_budget = cfg.kv_token_budget
        self._large_threshold = cfg.large_prefill_tokens
        self._max_large = cfg.max_inflight_large_prefills
        self._cond = asyncio.Condition()

        self._inflight_seqs = 0
        self._inflight_tokens = 0
        self._inflight_large = 0
        self._per_entry: dict[str, int] = {}

        self._peak_seqs = 0
        self._peak_tokens = 0
        self._peak_large = 0
        self._peak_per_entry: dict[str, int] = {}

        self._wait_seconds_total = 0.0
        self._wait_seconds_max = 0.0
        self._wait_count = 0
        self._timeouts = 0

    # ---- the predicate -----------------------------------------------------------
    def _binding(
        self, tokens: int, is_large: bool, key: str, limit: int
    ) -> tuple[str, int] | None:
        """The first rule that does not admit this request, or None if all four do.

        Returns the rule rather than a bool so a timeout can say which limit it waited
        on. "Waited 600s" tells an operator nothing about what to change.
        """
        if self._inflight_seqs >= self._max_seqs:
            return ("max_inflight_seqs", self._max_seqs)
        if self._inflight_tokens + tokens > self._token_budget:
            return ("kv_token_budget", self._token_budget)
        if is_large and self._inflight_large >= self._max_large:
            return ("max_inflight_large_prefills", self._max_large)
        if self._per_entry.get(key, 0) >= limit:
            return (f"concurrency for {key}", limit)
        return None

    # ---- acquire and release -----------------------------------------------------
    async def acquire(  # noqa: PLR0913 -- four rules need four sizes and a deadline
        self,
        tokens: int,
        *,
        prefill_tokens: int,
        entry_key: str,
        entry_limit: int,
        deadline: float | None = None,
        on_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> AdmissionLease:
        if tokens > self._token_budget:
            raise AdmissionImpossible(tokens, self._token_budget)

        # Classified on the prompt alone, never on the reply the request is allowed to
        # generate. A prefill is prompt processing; the reply is decode, and it arrives a
        # token at a time against a cache that is already warm. Folding the reply
        # reservation in here makes every delegation large the moment `max_tokens`
        # exceeds the threshold -- which is the default -- and rule 3 then bounds the
        # whole server at `max_inflight_large_prefills` while appearing to bound nothing.
        is_large = prefill_tokens > self._large_threshold
        started = time.monotonic()
        waited = False

        async with self._cond:
            while (binding := self._binding(tokens, is_large, entry_key, entry_limit)) is not None:
                # Checked after the predicate, never before: a request that fits is
                # admitted even if its deadline has just passed. Failing one that could
                # have run would be the gate causing the outage it exists to prevent.
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    self._timeouts += 1
                    self._record_wait(now - started)
                    raise AdmissionTimedOut(now - started, *binding)
                waited = True

                slice_ = _WAIT_TICK_SECONDS
                if deadline is not None:
                    slice_ = min(slice_, max(deadline - now, 0.001))
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=slice_)
                except TimeoutError:
                    # Nothing released; re-test the predicate and the deadline at the top.
                    pass
                if on_wait is not None:
                    await on_wait()

            self._inflight_seqs += 1
            self._inflight_tokens += tokens
            if is_large:
                self._inflight_large += 1
            self._per_entry[entry_key] = self._per_entry.get(entry_key, 0) + 1

            self._peak_seqs = max(self._peak_seqs, self._inflight_seqs)
            self._peak_tokens = max(self._peak_tokens, self._inflight_tokens)
            self._peak_large = max(self._peak_large, self._inflight_large)
            self._peak_per_entry[entry_key] = max(
                self._peak_per_entry.get(entry_key, 0), self._per_entry[entry_key]
            )

        if waited:
            self._record_wait(time.monotonic() - started)
        return AdmissionLease(tokens=tokens, is_large=is_large, entry_key=entry_key)

    def _record_wait(self, seconds: float) -> None:
        self._wait_seconds_total += seconds
        self._wait_seconds_max = max(self._wait_seconds_max, seconds)
        self._wait_count += 1

    async def release(self, lease: AdmissionLease) -> None:
        async with self._cond:
            self._inflight_seqs -= 1
            self._inflight_tokens -= lease.tokens
            if lease.is_large:
                self._inflight_large -= 1
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
    async def admit(  # noqa: PLR0913 -- passes `acquire`'s arguments through
        self,
        tokens: int,
        *,
        prefill_tokens: int,
        entry_key: str,
        entry_limit: int,
        deadline: float | None = None,
        on_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[AdmissionLease]:
        """Hold a slot for the body. Releases on every exit path, exceptions included."""
        lease = await self.acquire(
            tokens,
            prefill_tokens=prefill_tokens,
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
            "inflight_large_prefills": self._inflight_large,
            "peak_inflight_seqs": self._peak_seqs,
            "peak_inflight_tokens": self._peak_tokens,
            "peak_inflight_large_prefills": self._peak_large,
            "per_entry": {
                key: {"inflight": self._per_entry.get(key, 0), "peak": peak}
                for key, peak in sorted(self._peak_per_entry.items())
            },
            "admission_wait_seconds_total": round(self._wait_seconds_total, 3),
            "admission_wait_seconds_max": round(self._wait_seconds_max, 3),
            "admission_wait_count": self._wait_count,
            "admission_timeouts": self._timeouts,
        }
