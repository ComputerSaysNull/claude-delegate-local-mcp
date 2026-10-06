"""The delegation itself: build a request, send it, hand back what came back.

Three layers, bottom up: a dispatch that survives a dropped route, a temporary refusal and
a `Retry-After`; recovery from an intact reply left empty because reasoning used the whole
budget (retry larger, step effort down, then say so; ADR-0014); and the turn loop, with the
context economics that watch what it spends.

The only place that *interprets* a backend's reply, which is why the adapter passes
`finish_reason`, token counts and `Retry-After` through untouched. It also owns resolution
(model, effort, budget: once, and sent explicitly) and prompt assembly, whose order the
prefix cache depends on.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Any

from .backends.base import (
    Backend,
    BackendRefused,
    BashOutcome,
    BackendUnavailable,
    CanonicalRequest,
    CanonicalResponse,
    ContentBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from .config import (
    EFFORT_LEVELS,
    OVERFLOW_ABORT_AT,
    OVERFLOW_EVICT_AT,
    OVERFLOW_NUDGE_AT,
    OVERFLOW_TIGHTEN_AT,
    Config,
)
from .context import FileEntry, line_number_width, numbered_line, numbered_lines, numbered_span
from .paths import repo_status
from .registry import ModelEntry
from .tools import (
    ASK_CALLER_SPEC,
    ASKED_ALREADY,
    REGISTRY,
    BashPolicy,
    SeenFiles,
    declared_tools,
    execute_tool,
)

# Derived from EFFORT_LEVELS, not written out: a second copy stops agreeing when a level is
# added, and fails silently. Gives max -> high -> low -> off and nothing below off: there is
# no level beneath "do not reason", so an empty answer there is not reasoning exhaustion.
_STEP_DOWN = dict(zip(EFFORT_LEVELS[1:], EFFORT_LEVELS[:-1], strict=True))

# Statuses worth resending. A constant, not config: which codes mean "temporary" is a fact
# about the protocol. Exact, not "any refusal": 429 means later, and 500/502/503/504 a
# failure that may not repeat; every other status describes the request, so resending it
# cannot change the answer.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Must stay a constant: one dynamic byte silently disables the cluster's prefix cache, so
# dynamic content goes in the message. One prompt covers both file shapes. Why: ADR-0011.
SYSTEM_PROMPT_ONE_SHOT = (
    "You are answering a single delegated task for another engineer, who will read your "
    "reply directly and act on it.\n\n"
    "You have no tools. Everything you can use is in this message: the task, and any "
    "files the server has already read from disk and included for you. You cannot open "
    "anything else, and there is no second turn in which to ask. If the task cannot be "
    "answered from what is here, say precisely what is missing rather than guessing at "
    "it or describing what you would do given access.\n\n"
    "A file listed as not included is unavailable. Do not infer its contents from its "
    "name, or from the files that were included.\n\n"
    "Answer the task as asked. Do not restate it, do not narrate your approach, and do "
    "not close by summarising what you just said."
)


# Not a variant of the one-shot prompt, which says there are no tools and no second turn.
# One caller never alternates the two, so separate prefixes cost nothing. Static byte for
# byte; the countdown lives in the tail. Why: ADR-0011.
SYSTEM_PROMPT_AGENTIC = (
    "You are carrying out a delegated task for another engineer, who will read your final "
    "reply directly and act on it.\n\n"
    "You have tools, and a limited number of turns. Each turn you may either call tools or "
    "give your final answer; the answer ends the delegation. Turns remaining are stated "
    "alongside your tool results -- when the last one is reached, tools are withdrawn and "
    "whatever you write is what the engineer receives, so do not spend the final turn "
    "planning work you can no longer do.\n\n"
    "Files the server already read from disk are included below. They are current. Reading "
    "one again with a tool spends a turn to learn what you were given for free.\n\n"
    "Repeating a tool call with the same arguments returns the answer you already had, "
    "marked as a repeat. It is not a way to make something change; if a result is not what "
    "you expected, the next step is a different call, not the same one again.\n\n"
    "A tool result marked as an error is a refusal you can act on, not a failure of the "
    "delegation. Read what it says and correct the call.\n\n"
    "Answer the task as asked. Do not restate it, do not narrate your approach, and do not "
    "close by summarising what you just said."
)


class InvalidDelegation(ValueError):
    """A caller's argument is wrong. Raised before anything is sent to a backend."""


class ContextOverflowAborted(InvalidDelegation):
    """The delegation stopped because its context was full or had silently truncated.

    Carries a state report: an abort lands mid-work, the caller's next question is what it
    did to the tree, and the model's summary is the one answer not to trust, so the report
    puts the server's tool-call ledger beside `git status` (ADR-0007). Subclasses
    `InvalidDelegation` so `server.py` catches it; `delegate()` returns `report`.
    """

    def __init__(self, message: str, *, report: dict[str, object]) -> None:
        super().__init__(message)
        self.report = report


class DispatchTimedOut(Exception):
    """The whole delegation outlived `dispatch_timeout`.

    Distinct from every backend failure: the endpoint may be answering fine, so sending the
    caller to check the cluster over a deadline they set would be the wrong diagnosis
    (ADR-0007).
    """

    def __init__(  # noqa: PLR0913 -- one message's fields; the three counters are
                   # keyword-only and every raise site omits them
                 self, elapsed: float, limit: int, stage: str,
                 setting: str = "DELEGATE_DISPATCH_TIMEOUT",
                 remedy: str = "Raise that setting if the work legitimately takes this "
                               "long, or shorten the task.",
                 *, turns: int | None = None, tool_calls: int | None = None,
                 last_tool: str | None = None) -> None:
        self.elapsed = elapsed
        self.limit = limit
        # Carries its own preposition, so the stall does not read "while with no turn
        # completed".
        self.stage = stage
        self.setting = setting
        # Which deadline fired, so a caller can tell a stall from a deadline without
        # parsing the message.
        self.stalled = setting == "DELEGATE_STALL_TIMEOUT"
        # A parameter because "raise that setting" suits a ceiling reached while producing
        # and is wrong for a stall, where a longer deadline only waits longer for a run that
        # has stopped.
        self.remedy = remedy
        # Progress when the deadline fired. `None` at every raise site, which cannot see the
        # counters on the loop's `_Watch`; the loop fills them in via `with_progress`.
        self.turns = turns
        self.tool_calls = tool_calls
        self.last_tool = last_tool
        # What the abandoned turn had decoded (ADR-0078). Attached afterwards by the one
        # caller that can see the backend's accumulator, and kept out of the message so a
        # late assignment cannot make `str(e)` and the fields disagree.
        self.partial: Any = None
        super().__init__(
            f"Delegation abandoned after {elapsed:.1f}s, past the "
            f"{setting} of {limit}s, {stage}.{self._progress()} {remedy}"
        )

    def _progress(self) -> str:
        """What it had done, or nothing when the counters were never supplied.

        Empty rather than "0 turns": a one-shot completes no turns by construction, so a
        zero would make the one path that cannot stall read as stalled.
        """
        if self.turns is None or self.tool_calls is None:
            return ""
        last = f", last tool {self.last_tool}" if self.last_tool else ""
        return (
            f" Progress at that point: {self.turns} "
            f"turn{'' if self.turns == 1 else 's'} completed, {self.tool_calls} "
            f"tool call{'' if self.tool_calls == 1 else 's'}{last}."
        )

    def with_progress(self, *, turns: int, tool_calls: int,
                      last_tool: str | None) -> DispatchTimedOut:
        """A copy that also reports progress, for the turn loop to raise instead.

        A copy because the message is built in `__init__`: assigning afterwards would leave
        `str(e)` disagreeing with the fields (ADR-0007).
        """
        copy = DispatchTimedOut(
            self.elapsed, self.limit, self.stage, self.setting, self.remedy,
            turns=turns, tool_calls=tool_calls, last_tool=last_tool,
        )
        # Every timed-out agentic delegation passes through here, so dropping it loses it.
        copy.partial = self.partial
        return copy


@dataclass(frozen=True, slots=True)
class Delegation:
    """What to delegate: the task, the material assembled for it, and the agent's prompt.

    One value because they are one prompt whose order matters (ADR-0011); `render` keeps
    that rule in one place instead of two call sites.
    """

    task: str
    files_block: str = ""
    agent_body: str = ""
    workdir: str | None = None

    def render(self) -> str:
        """The user message: agent body, then files, then the workdir, then the task.

        Never the system prompt, a cached byte-for-byte constant that nothing per-delegation
        may enter, the agent body included. Why: ADR-0011. The workdir is named because
        nothing else names it: the shell starts there and the file tools refuse a relative
        path, so a task's relative path means nothing without it.
        """
        if not self.task or not self.task.strip():
            raise InvalidDelegation("task is empty. There is nothing to delegate.")
        where = WORKDIR_LINE.format(path=self.workdir) if self.workdir else ""
        parts = (self.agent_body, self.files_block, where, self.task)
        return "\n\n".join(part for part in parts if part)


# The tool calls running now, each `{"name", "arguments"}` capped as a turn record is.
RunningCalls = tuple[dict[str, Any], ...]

# One alias for the four places that declare the heartbeat callback, since `_keepalive`
# swallows the `TypeError` a drifted shape would raise.
AliveCallback = Callable[[float, int, float, int, int, float | None, RunningCalls], Awaitable[None]]

# Rendered just before the task when a workdir is bound.
WORKDIR_LINE = (
    "Your workdir is {path}. `run_bash` starts there, and a relative path in the task "
    "below is relative to it; the file tools take the absolute path."
)


def resolve_effort(cfg: Config, entry: ModelEntry, explicit: str | None = None) -> str:
    """Explicit argument, then the registry row, then the global default.

    Never the cluster's boot default, which is set elsewhere and not ours to assume.
    ADR-0013.
    """
    if explicit:
        if explicit not in EFFORT_LEVELS:
            raise InvalidDelegation(
                f"effort={explicit!r} is not one of {EFFORT_LEVELS}. Refused before "
                "dispatch: these are this project's levels, which the adapter translates "
                "into the server's own vocabulary, and an unlisted one has no translation."
            )
        return explicit
    return entry.effective_effort(cfg)


# Below these an observation is noise, not a measurement. Both estimators divide by
# `decode_seconds` (first token to last, prefill and queueing outside), so one test serves
# both; sized from the deployment's decode benchmark. Why: ADR-0071.
MIN_MEASURABLE_TOKENS = 512
MIN_MEASURABLE_SECONDS = 1.0


class DecodeRate:
    """Tokens per second this deployment actually decodes, kept across turns.

    Measured, never configured, because the reply budget is priced in seconds (ADR-0055).
    Seeded from the cluster's since-boot mean, then replaced by this delegation's own turns.
    An exponential average, so one odd turn cannot halve the next budget; an implausible
    sample is refused, not smoothed.
    """

    # One test for both estimators, because they divide by the same thing.
    MIN_TOKENS = MIN_MEASURABLE_TOKENS
    MIN_SECONDS = MIN_MEASURABLE_SECONDS
    # Five turns move it about 90% of the way to a new rate; one turn is not the estimate.
    WEIGHT = 0.4

    # `seen_running` is unused in calculation: it records the load the seed was read
    # against, since a since-boot mean over every regime cannot otherwise be checked.
    __slots__ = ("_rate", "seen_running", "source")

    def __init__(self, seed: float | None = None,
                 seen_running: float | None = None,
                 source: str = "unknown") -> None:
        self._rate = seed if seed and seed > 0 else None
        self.seen_running = seen_running
        # For the record: a rate that cannot be traced to its source cannot be argued with.
        self.source = source

    @property
    def rate(self) -> float | None:
        """What a turn would currently be priced at, or None if nothing is known."""
        return self._rate

    @property
    def known(self) -> bool:
        return self._rate is not None

    # `source` once this delegation's own turn moves the number; the seed's label stops
    # being true at the first sample.
    OWN_TURNS = "own_turns"

    def observe(self, output_tokens: int, seconds: float) -> None:
        if output_tokens < self.MIN_TOKENS or seconds < self.MIN_SECONDS:
            return
        sample = output_tokens / seconds
        self._rate = (
            sample if self._rate is None
            else (1 - self.WEIGHT) * self._rate + self.WEIGHT * sample
        )
        # Relabelled here, only on a sample actually taken, so the label cannot claim a
        # measurement the floors refused; `rate_source` is what readers trust the rate by.
        self.source = self.OWN_TURNS

    def ceiling(self, cfg: Config, seconds_available: float) -> int | None:
        """The largest reply the clock can pay for, or None when nothing is known.

        None rather than a guess: an unmeasured cap is the constant this design avoids,
        and a caller that cannot bound the budget should say so.
        """
        if self._rate is None or seconds_available <= 0:
            return None
        return max(
            cfg.reply_budget_floor,
            int(seconds_available * self._rate * cfg.reply_budget_margin),
        )


def budget_seconds(cfg: Config, *, dispatch_left: float) -> float:
    """Seconds the next reply can be delivered in: what the delegation deadline leaves.

    Not `turn_timeout` (ADR-0100) and not `stall_left` (ADR-0099): a reply being generated
    is not silence, so that term would cut every reply to the stall budget. Never negative,
    which would read through `ceiling` as a small budget rather than no time left.
    """
    return max(dispatch_left, 0.0)


class RateHistory:
    """What this deployment has decoded, kept across delegations.

    `DecodeRate` dies with its delegation, so without this every first turn is priced from
    the since-boot mean. A memory, not a fitted curve, which would be a constant baked to
    one deployment's hardware. Keyed by contention, because the rate is not one number.

    `expect` takes each bucket's median, then the worst median at that concurrency or above:
    pessimism between buckets is sound (contention only slows a stream), inside one it is
    sampling noise. A median ignores slow outliers and, unlike a minimum, does not move with
    the sample count. Under-pricing is not free: it truncates the largest replies.
    """

    # Outlasts one fan-out and forgets a reconfigured cluster; unbounded would leak and let
    # an ancient sample vote forever. Counted in moments (one reading per scrape, not per
    # stream), so it spans the same wall-clock time at every fan-out width.
    DEFAULT_KEEP = 64

    # Shared with `DecodeRate` so the two tests cannot drift.
    MIN_TOKENS = MIN_MEASURABLE_TOKENS
    MIN_SECONDS = MIN_MEASURABLE_SECONDS

    # Bumped when the payload changes; an older file is discarded rather than guessed at.
    SCHEMA_VERSION = 1

    __slots__ = ("_keep", "_path", "_seen", "_stamp")

    def __init__(
        self,
        keep: int = DEFAULT_KEEP,
        *,
        path: Path | None = None,
        stamp: str = "",
    ) -> None:
        """`path` makes the memory outlive this process, at `slots.rate_history_path`.

        Durable because a stale rate self-corrects as samples follow, where a cold start
        costs every dispatch until the memory refills; `stamp` covers a model swap. Never a
        hard dependency: every failure below leaves an empty memory. Why: ADR-0094.
        """
        self._keep = max(1, keep)
        # One bucket per concurrency, each capped on its own: a shared deque evicted by
        # recency would spend itself on whichever regime was busiest lately.
        self._seen: dict[int, deque[float]] = {}
        self._path = path
        self._stamp = stamp
        if path is not None:
            for seen_at, rate in self._read():
                self._remember(seen_at, rate)

    def _remember(self, concurrency: int, rate: float) -> None:
        """Put one sample in its own bucket, creating it on first sight."""
        bucket = self._seen.get(concurrency)
        if bucket is None:
            bucket = self._seen[concurrency] = deque(maxlen=self._keep)
        bucket.append(rate)

    def samples_at(self, concurrency: int) -> tuple[float, ...]:
        """One bucket's held samples, oldest first; empty when nothing was seen there.

        For a reader that needs the count, which `expect`'s statistic cannot give, without
        reaching into `_seen`.
        """
        return tuple(self._seen.get(max(int(concurrency), 1)) or ())

    def _pairs(self) -> list[tuple[int, float]]:
        """Every sample held, flattened back to the shape the file stores."""
        return [(seen_at, rate) for seen_at, b in self._seen.items() for rate in b]

    def _read(self) -> list[tuple[int, float]]:
        """Whatever the file holds that is still trustworthy.

        Every field is shape-checked: any process of this user can write the file, and a
        sample that reaches `expect` keeps its vote for `DEFAULT_KEEP` observations. A
        malformed pair is skipped, the rest kept.
        """
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, dict):
            return []
        if raw.get("version") != self.SCHEMA_VERSION or raw.get("stamp") != self._stamp:
            return []

        kept: list[tuple[int, float]] = []
        for item in raw.get("seen") or ():
            if not isinstance(item, list) or len(item) != 2:
                continue
            seen_at, rate = item
            # `True` is an `int` and would become concurrency 1; nothing here writes one.
            if isinstance(seen_at, bool) or not isinstance(seen_at, int) or seen_at < 1:
                continue
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
                continue
            kept.append((seen_at, float(rate)))
        # Not truncated here: the cap lives per bucket, and a global trim would bring back
        # the cross-concurrency eviction the buckets exist to stop.
        return kept

    def _write(self) -> None:
        """Persist, merging with whatever another server process has written since.

        Each stdio client gets its own process (ADR-0040), so two can share this file, and
        writing only this process's samples would drop the other's. Merged as a set: an
        identical rate at one concurrency is far more often this process's own read-back
        than a second measurement. Written to a sibling and renamed, so no reader sees half.
        """
        assert self._path is not None
        merged = dict.fromkeys(self._read())
        merged.update(dict.fromkeys(self._pairs()))
        # Bounded per bucket, as in memory, so no one regime writes the others out.
        held: dict[int, list[float]] = {}
        for seen_at, rate in merged:
            held.setdefault(seen_at, []).append(rate)
        payload = json.dumps(
            {
                "version": self.SCHEMA_VERSION,
                "stamp": self._stamp,
                "seen": [
                    [seen_at, rate]
                    for seen_at, rates in held.items()
                    for rate in rates[-self._keep:]
                ],
            },
            separators=(",", ":"),
        )
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            # Costs the next process its head start and nothing else; raising would turn an
            # optimisation into an outage.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def observe(self, output_tokens: int, seconds: float, *, concurrency: int) -> None:
        """Record what one completed turn achieved, and how contended it was.

        Takes tokens and interval, not a rate, because a rate cannot be judged: it cannot
        tell throughput from a short turn that was mostly prefill. The guard lives here so a
        second caller cannot bypass it by dividing first.
        """
        # Degenerate inputs would price every later delegation; refused on the operands,
        # since with both floors positive a check on the quotient could never fire.
        if output_tokens < self.MIN_TOKENS or seconds < self.MIN_SECONDS:
            return
        self._remember(max(int(concurrency), 1), output_tokens / seconds)
        # After the floor, so persistence is not a second way in for a refused sample.
        if self._path is not None:
            self._write()

    def observe_window(self, generated: int, seconds: float, *, concurrency: int) -> bool:
        """One scrape window of the cluster's own counter, filed as a single sample.

        Not `observe`: its token floor would discard every solo window, which decodes far
        fewer tokens, and a window carries no prefill (`RateSampler` refuses one inside a
        prefill). Only `MIN_SECONDS` applies, so the counter's granularity cannot dominate.

        The aggregate is divided by the concurrency from the *same* scrape, and exactly one
        sample comes out whatever the width, so a full bucket spans the same wall clock at
        six streams as at one. `generated < 1` is refused here too, so no second caller gets
        a zero-throughput sample in. Returns whether the sample was kept.
        """
        if generated < 1 or seconds < self.MIN_SECONDS:
            return False
        width = max(int(concurrency), 1)
        self._remember(width, generated / seconds / width)
        if self._path is not None:
            self._write()
        return True

    def expect(self, concurrency: int, *, trusted: bool = False) -> float | None:
        """This concurrency's median rate, or the worst busier bucket's. None if neither.

        Busier observations answer quieter questions, not the reverse: contention only slows
        a stream, so six-way bounds four-way from below and solo says nothing about six.

        `trusted` says whether the label can be believed, default no. Untrusted, the answer
        comes from that concurrency *or busier*, protecting a burst's first member, which
        finds the gate empty, labels itself solo, then decodes at six-way. Trusted, the
        bucket comes first and widening is the fallback. Only `admission_idle_hold` makes it
        true, by waiting for a burst before recording the label, so that setting disables
        both halves at once (ADR-0085).

        Each bucket answers with its median and the widening takes the worst median, never
        one pooled mean, which would let a quiet bucket's fast samples lift a six-way
        answer. A slow outlier is a minute the cluster had, not its rate. Over-estimating
        kills a turn with nothing where under-estimating truncates, but that asymmetry is
        paid between buckets and by `reply_budget_margin`; a within-bucket minimum would pay
        it a third time, out of the large answers.
        """
        want = max(int(concurrency), 1)
        if trusted:
            at = self._seen.get(want)
            if at:
                return median(at)
        # An empty bucket (created, never filed) is skipped, not counted as zero, which
        # would price the next turn at no throughput.
        medians = [
            median(bucket) for seen_at, bucket in self._seen.items()
            if seen_at >= want and bucket
        ]
        return min(medians) if medians else None


class RateSampler:
    """Fills `RateHistory` from the cluster's counter on a ticker, not on turn completion.

    Per turn, six streams put six samples on one moment, so a six-wide bucket would span a
    sixth of a solo bucket's wall clock; one sample per scrape makes every bucket span the
    same moments, which is what makes `expect`'s cross-bucket comparison mean anything.

    Rate and concurrency come from the *same* scrape (`_DecodeWindow` differences
    `generation_tokens_total`), so the per-stream rate divides two numbers from one moment.
    Never a dependency: every failure leaves the memory as it was, since a sampler that
    could fail a delegation would trade a pricing gain for an outage.
    """

    # How far past the interval a window may run before it is dropped. The ticker scrapes
    # only while busy, so a busy period's first window can span hours of idle and report a
    # falsely low rate. Two intervals still admits a tick that ran late.
    STALE_WINDOW_FACTOR = 2.0

    __slots__ = ("_busy", "_every", "_filed", "_history", "_prefill_windows",
                 "_probe", "_stale_windows", "_windows")

    def __init__(
        self,
        history: RateHistory,
        *,
        probe: Callable[[], Awaitable[Mapping[str, Any] | None]],
        busy: Callable[[], bool],
        every: float,
    ) -> None:
        self._history = history
        self._probe = probe
        self._busy = busy
        # Clamped, not validated: a non-positive interval turns sampling off, and refusing
        # to start the server over it would be the worse failure.
        self._every = max(0.0, float(every))
        self._windows = 0
        self._filed = 0
        self._prefill_windows = 0
        self._stale_windows = 0

    @property
    def enabled(self) -> bool:
        """Whether an interval was configured at all."""
        return self._every > 0

    def record(self, scrape: Mapping[str, Any] | None) -> bool:
        """File the one sample this scrape supports, if any.

        Synchronous and pure apart from the history, so it can be tested against a dict.
        """
        generated = scrape.get("decode_tokens_window") if scrape else None
        seconds = scrape.get("decode_window_seconds") if scrape else None
        running = scrape.get("requests_running") if scrape else None
        # No window: the first scrape after a gap. Not a drop, since nothing was measured.
        if not isinstance(generated, int) or isinstance(generated, bool):
            return False
        if not isinstance(seconds, (int, float)) or not isinstance(running, (int, float)):
            return False
        # An idle window has no divisor and nothing to say. Also not a drop.
        concurrency = int(running)
        if concurrency < 1:
            return False

        self._windows += 1
        # Only with an interval set: with sampling off, `0 * anything` makes every window
        # stale.
        if self.enabled and seconds > self._every * self.STALE_WINDOW_FACTOR:
            self._stale_windows += 1
            return False
        # A window inside a prefill differences to zero tokens while requests run, which
        # would file a rate of zero in a busy bucket, and large prompts make it common.
        # Counted, because a guard whose firing rate nobody sees cannot be trusted.
        if generated == 0:
            self._prefill_windows += 1
            return False

        kept = self._history.observe_window(
            generated, float(seconds), concurrency=concurrency
        )
        if kept:
            self._filed += 1
        return kept

    async def run(self) -> None:
        """Scrape on the interval while anything is decoding. Cancelled to stop.

        Idle ticks reach no endpoint: there is no rate to sample, and scraping would keep a
        connection warm against a cluster this process is not using.
        """
        if not self.enabled:
            return
        while True:
            await asyncio.sleep(self._every)
            if not self._busy():
                continue
            try:
                self.record(await self._probe())
            except Exception:  # a monitoring read must never fail anything
                continue

    def status(self) -> dict[str, Any]:
        """What the sampler has seen and thrown away, for `backend_status`.

        The drop counts are the point: a dropped window leaves no trace, so without them a
        working sampler looks the same as one refusing every window.
        """
        return {
            "sample_interval_seconds": round(self._every, 1),
            "windows_seen": self._windows,
            "samples_filed": self._filed,
            "windows_dropped_prefill": self._prefill_windows,
            "windows_dropped_stale": self._stale_windows,
        }


async def seed_decode_rate(  # noqa: PLR0913 -- one argument per thing the seed consults
    backend: Backend,
    history: RateHistory | None = None,
    expected_concurrency: int = 1,
    on_pool: Callable[[int | None], None] | None = None,
    label_trusted: bool = False,
    *,
    fallback: float = 0.0,
) -> DecodeRate:
    """The estimator, seeded from the cluster if it will say and empty if it will not.

    Never raises: a delegation must not refuse to start because `/metrics` was briefly
    down. An endpoint that publishes nothing leaves the first turn uncapped. Why: ADR-0055.
    """
    # What was measured at this contention beats the since-boot mean, a blend of every
    # regime, when the first turn is about to meet one. Falls through only on a cold start.
    remembered = (
        history.expect(expected_concurrency, trusted=label_trusted)
        if history is not None else None
    )

    # Not an early return while `on_pool` wants the KV pool size: the scrape below is the
    # only place on the dispatch path that sees it. The caller stops passing it once known,
    # so this costs one extra read per process. Why: ADR-0081.
    if remembered is not None and on_pool is None:
        return DecodeRate(remembered, float(expected_concurrency),
                          source="observed_at_concurrency")
    try:
        cluster = await backend.probe_cluster()
    except Exception:  # a monitoring read must never fail a delegation
        # A remembered rate outlives a failed scrape, rather than a brief outage discarding
        # a measurement in hand.
        if remembered is not None:
            return DecodeRate(remembered, float(expected_concurrency),
                              source="observed_at_concurrency")
        # Not the floor below, which means "never measured here"; a failed scrape's
        # answer is no ceiling rather than a guessed one.
        return DecodeRate(source="unknown")
    if on_pool is not None:
        pool = (cluster or {}).get("kv_cache_size_tokens")
        on_pool(pool if isinstance(pool, int) else None)
    # The remembered rate still prices the turn; the scrape was for the pool. Otherwise a
    # warm memory would do worse than a cold one.
    if remembered is not None:
        return DecodeRate(remembered, float(expected_concurrency),
                          source="observed_at_concurrency")
    running = (cluster or {}).get("requests_running")
    # Nothing remembered this busy: a configured floor prices the first turn, at `running`,
    # instead of the since-boot mean, which errs optimistic (ADR-0094) and so kills turns
    # rather than truncating them. Why: ADR-0101.
    if fallback > 0:
        return DecodeRate(
            float(fallback),
            running if isinstance(running, (int, float)) else float(expected_concurrency),
            source="configured_fallback",
        )
    rate = (cluster or {}).get("decode_tokens_per_second_since_boot")
    return DecodeRate(
        rate if isinstance(rate, (int, float)) else None,
        running if isinstance(running, (int, float)) else None,
        source="cluster_since_boot" if isinstance(rate, (int, float)) else "unknown",
    )


def resolve_max_tokens(
    cfg: Config,
    entry: ModelEntry,
    effort: str,
    explicit: int | None = None,
    *,
    ceiling: int | None = None,
) -> int:
    """The reply budget: the caller's number, else the configured one raised at high effort.

    Reasoning spends this same budget, so high effort under a low cap answers empty; the
    floor prevents that. Precedence: the call argument, the agent's frontmatter, then the
    configured default, **last**. The floor is a `max()` over the configured value only;
    an explicit number is not raised. The per-model cap applies last on every path, since
    it is what the wire accepts.

    `ceiling` is the deadline in tokens and binds the explicit argument too: a budget above
    it is the same answer killed at `stall_timeout`, all of it discarded. Why: ADR-0014,
    ADR-0024, ADR-0055.
    """
    if explicit is not None:
        if explicit < 1:
            raise InvalidDelegation(
                f"max_tokens={explicit} must be at least 1. A budget of nothing cannot "
                "produce an answer."
            )
        budget = explicit
    else:
        budget = cfg.max_tokens
        if effort in ("high", "max"):
            budget = max(budget, cfg.thinking_max_tokens_floor)
    if ceiling is not None:
        budget = min(budget, ceiling)
    return entry.cap_tokens(budget)


def build_one_shot_request(
    *, delegation: Delegation, effort: str, max_tokens: int, temperature: float,
    top_p: float,
) -> CanonicalRequest:
    """One user message, no tools, and a system prompt that does not vary.

    The order lives on `Delegation`, so this and the turn loop cannot disagree. ADR-0011.
    """
    body = delegation.render()
    return CanonicalRequest(
        system=SYSTEM_PROMPT_ONE_SHOT,
        messages=(Message("user", (TextBlock(body),)),),
        max_tokens=max_tokens,
        effort=effort,
        temperature=temperature,
        top_p=top_p,
    )


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait, from either legal form of the header, or None if it says nothing.

    RFC 7231 allows seconds or an HTTP-date, and a server may send either. None rather than
    raising: a malformed or absent header falls back to backoff, never aborts. A past date
    clamps to zero, meaning "now".
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        # Seconds are an integer by spec; "1.5" fails both parses and lands on backoff.
        return max(0.0, float(int(text)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # An HTTP-date is GMT by definition; a naive one is not a different instant.
        when = when.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0.0, (when - reference).total_seconds())


def _is_retryable(error: Exception) -> bool:
    """Whether the identical request could plausibly get a different answer.

    Only the reachability kinds: `BackendProtocolError` (the wrong stack answered) and
    `CanonicalShapeError` (our bug) would just repeat the mistake more slowly.
    """
    if isinstance(error, BackendUnavailable):
        return True
    return isinstance(error, BackendRefused) and error.status in _RETRYABLE_STATUSES


def _retry_is_plausible(
    error: Exception, *, attempt_seconds: float, seconds_left: float | None
) -> bool:
    """Whether sending it again could plausibly end differently *in the time that is left*.

    `_is_retryable` asks whether the failure can change; this asks whether there is room,
    which the error class cannot say. Only a failure *while generating* is time-tested: a
    connect failure spent nothing and deserves its retry, while a read timeout used its
    whole allowance, so less time cannot do the same work. `None` means unbounded
    (ADR-0055), not zero. Inclusive: as much time as the failed attempt used is enough,
    that attempt being the only evidence of the cost.
    """
    if not _is_retryable(error):
        return False
    if not getattr(error, "while_generating", False):
        return True
    if seconds_left is None:
        return True
    return seconds_left >= attempt_seconds


def _delay_before_retry(
    cfg: Config, error: Exception, attempt: int, jitter: Callable[[float, float], float]
) -> float:
    """How long to wait before attempt `attempt + 1`, capped either way.

    An explicit `Retry-After` is used as sent, not jittered: jitter decorrelates guessing
    clients, and the server has removed the guess. The cap applies to it too, or a hostile
    header stalls the call in a wait between requests that no HTTP timeout reaches.
    """
    asked = parse_retry_after(getattr(error, "retry_after", None))
    if asked is not None:
        return min(asked, cfg.retry_max_delay)
    backoff = min(cfg.retry_base_delay * (2 ** (attempt - 1)), cfg.retry_max_delay)
    return jitter(0.0, backoff)


# How often a live deadline is re-read during a call. Not a setting: a wakeup per quarter
# second against how sharply a deadline lands is no knob for an operator.
_DEADLINE_TICK = 0.25


async def _until_deadline(
    coro,
    left: Callable[[], float | None],
    *,
    tick: float,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
):
    """Run `coro`, cancelling it once `left()` has actually run out.

    Not `asyncio.wait_for`, whose budget is fixed at call time: token arrival resets the
    stall deadline, so a fixed timeout would kill a turn that was producing. Why: ADR-0072.

    `tick_sleep` is a test seam: this is the one wait on the wall rather than the injected
    clock. `None`, outside tests, waits on the call itself and returns the instant it
    answers, where polling would add a tick to every backend call. Raises `TimeoutError`,
    and the caller asks which deadline expired.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            budget = left()
            if budget is not None and budget <= 0:
                task.cancel()
                partial = None
                try:
                    await task
                except BaseException as e:  # the cancellation itself; TimeoutError reports it
                    # The one place to catch the partial: the adapter hangs it on whatever
                    # exception leaves, cancellation included, and it survives `await task`
                    # (measured, CPython 3.12/3.14); a cancelled `task.exception()` refuses.
                    partial = getattr(e, "partial", None)
                timed_out = TimeoutError()
                timed_out.partial = partial  # type: ignore[attr-defined]
                raise timed_out
            wait_for = tick if budget is None else min(tick, budget)
            if tick_sleep is None:
                done, _ = await asyncio.wait({task}, timeout=wait_for)
                if done:
                    return task.result()
                continue
            done, _ = await asyncio.wait({task}, timeout=0)
            if done:
                return task.result()
            await tick_sleep(wait_for)
    finally:
        # Whatever brought us here, the call must not outlive it: cancelling closes its HTTP
        # stream, the only thing that tells the engine to stop generating.
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass  # its own cancellation; the exception that brought us here propagates


# The kinds a `RetryRecord.kind` may hold, mirrored as the `retry.kind` enum in
# `transcript.schema.json` (kept in lockstep by tests/test_every_retry_says_why.py).
RETRY_KINDS = ("BackendUnavailable", "BackendRefused", "EmptyAtLength")


@dataclass(frozen=True, slots=True)
class RetryRecord:
    """One failed attempt the turn tried again after, and why.

    `attempts` cannot say which failure it was, and a dropped route and a 503 have
    different remedies. `kind` is the exception class for a transport retry, or
    `EmptyAtLength` for a recovery stage; `status` a refusal's HTTP status, `seconds` the
    failed attempt's run, `wait` the sleep chosen.
    """

    kind: str
    status: int | None
    seconds: float
    wait: float

    def as_json(self) -> dict[str, Any]:
        """The record as one JSON object, rounded for the transcript."""
        return {
            "kind": self.kind,
            "status": self.status,
            "seconds": round(self.seconds, 3),
            "wait": round(self.wait, 3),
        }


async def complete_with_retry(  # noqa: PLR0913 -- five of the eight are test seams
    cfg: Config,
    backend: Backend,
    request: CanonicalRequest,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
    deadline: float | None = None,
    stall_left: Callable[[], float] | None = None,
    on_token: Callable[[str, str], None] | None = None,
    on_retry: Callable[[RetryRecord], None] | None = None,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[CanonicalResponse, int, float]:
    """Send until it answers, a failure is not worth repeating, or the attempts run out.

    Returns the response, the real call count and the answering attempt's seconds. The
    count is server-captured truth about the cost (ADR-0007): the only way a caller sees a
    quiet success was three tries. On exhaustion the last exception propagates unchanged,
    so the layer above can tell the four kinds apart.

    `sleep`, `jitter` and `clock` are test seams: otherwise a retry test sleeps for real, a
    cap test cannot pin the random factor, and a deadline test spends the deadline. One
    clock governs every ceiling.

    `deadline` (absolute, on `clock`, owned by the delegation) bounds attempts plus waits:
    checked before each attempt, applied to it as a ceiling, and checked against the wait,
    so a backoff that would outlast it ends the delegation rather than waking to find it
    gone. `stall_left` asks "has it stopped getting anywhere": seconds before the
    no-progress deadline, at or below zero a stall. A callable, because the caller resets
    it on progress. The tighter of the two bounds each attempt, or one wedged call could
    sit for all of `dispatch_timeout`. `None` disables either; the empty-answer stages pass
    it once they have spent the budget.
    """
    attempts = 0
    started = clock()

    def remaining() -> float | None:
        return None if deadline is None else deadline - clock()

    def spent() -> float:
        """How much of the *delegation's* budget is gone, not how long this call ran.

        This runs once per turn against one delegation deadline, so measuring from
        `started` would report a turn's time beside the whole limit. Derived from the
        deadline, so there is no second origin; `started` serves only when there is none.
        """
        if deadline is None:
            return max(clock() - started, 0.0)
        return max(cfg.dispatch_timeout - (deadline - clock()), 0.0)

    def stalled() -> None:
        """Raise if the no-progress deadline has passed. Named for what it reports."""
        if stall_left is None:
            return
        s = stall_left()
        if s > 0:
            return
        raise DispatchTimedOut(
            cfg.stall_timeout - s, cfg.stall_timeout, "with no turn completed",
            setting="DELEGATE_STALL_TIMEOUT",
            remedy="The delegation was still alive and had stopped getting anywhere, so "
                   "raising this would only lengthen the wait. Check the endpoint with "
                   "backend_status, or send a task that can finish a turn.",
        )

    def ceiling() -> float | None:
        """The tighter of the two deadlines, read *now*, not fixed for the call.

        Token arrival moves `stall_left` (ADR-0072); a fixed budget would kill a turn that
        streamed the whole way at whatever was left when it started.
        """
        s = None if stall_left is None else stall_left()
        both = [x for x in (remaining(), s) if x is not None]
        return min(both) if both else None

    while True:
        left = remaining()
        if left is not None and left <= 0:
            raise DispatchTimedOut(spent(), cfg.dispatch_timeout, "while waiting on the backend")
        stalled()
        attempts += 1
        attempt_started = clock()
        try:
            if ceiling() is None:
                answer = await backend.complete(request, on_token=on_token)
                # From this attempt's start, or a retried turn divides one attempt's
                # tokens by every attempt's seconds, backoff included.
                return answer, attempts, clock() - attempt_started
            # The only per-attempt ceiling: the adapter bounds silence between frames, not
            # the length of a call that keeps producing.
            answer = await _until_deadline(
                backend.complete(request, on_token=on_token), ceiling,
                tick=_DEADLINE_TICK, tick_sleep=tick_sleep,
            )
            return answer, attempts, clock() - attempt_started
        except TimeoutError as e:
            # Ask which expired before blaming the ceiling, or a stall reads as a
            # delegation that ran too long.
            stalled()
            if deadline is None:
                # No delegation deadline was in play, so only the stall clock can have
                # expired; naming the other would send an operator to the wrong setting.
                timed_out = DispatchTimedOut(
                    spent(), cfg.stall_timeout, "while waiting on one turn",
                    setting="DELEGATE_STALL_TIMEOUT",
                    remedy="The attempt stopped producing rather than merely ran long, "
                           "so raising this would only lengthen the wait. Check the "
                           "endpoint with backend_status.",
                )
            else:
                timed_out = DispatchTimedOut(
                    spent(), cfg.dispatch_timeout, "while waiting on the backend"
                )
            # Both diagnoses carry the decoded partial: which deadline expired decides the
            # message, never whether the tokens are returned.
            timed_out.partial = getattr(e, "partial", None)
            raise timed_out from e
        except (BackendUnavailable, BackendRefused) as e:
            # Time-tested too: an attempt that used its whole allowance cannot do the work
            # in less (ADR-0055).
            if attempts >= cfg.retry_max_attempts or not _retry_is_plausible(
                e, attempt_seconds=clock() - attempt_started, seconds_left=ceiling()
            ):
                raise
            wait = _delay_before_retry(cfg, e, attempts, jitter)
            left = ceiling()
            if left is not None and wait >= left:
                # `ceiling()` cannot say which deadline this was; the stall check names it.
                stalled()
                # Not after sleeping, which would burn the budget and blame a wait this
                # function chose.
                raise DispatchTimedOut(
                    spent() + wait, cfg.dispatch_timeout, "while waiting to retry"
                ) from e
            if on_retry is not None:
                on_retry(RetryRecord(
                    kind=type(e).__name__,
                    status=getattr(e, "status", None),
                    seconds=clock() - attempt_started,
                    wait=wait,
                ))
            await sleep(wait)


@dataclass(frozen=True, slots=True)
class Dispatch:
    """What one delegation actually did, as opposed to what it was asked to do.

    Named fields, since `effort` may not be the level asked for and `attempts` may not be
    one. `reasoning_exhausted` is narrow: still empty at a length stop *after* a larger
    budget and a lower effort were both tried (ADR-0014). An empty answer nothing was tried
    on is a mechanical fact, not this verdict.
    """

    response: CanonicalResponse
    effort: str
    attempts: int
    # Every failed attempt retried, in order, across every stage of the recovery ladder.
    retries: tuple[RetryRecord, ...] = ()
    reasoning_exhausted: bool = False
    # The answering attempt's time, not the turn's: the token counts come from that attempt
    # alone (ADR-0014), so a rate must divide by the same event.
    answered_seconds: float = 0.0
    # Summed over every stage, on purpose: a resent turn paid a second prefill, and a run
    # summary wants both. A rate divides by `response.decode_seconds` instead. `None` means
    # the adapter cannot stream, not that no time was spent.
    prefill_seconds: float | None = None
    decode_seconds: float | None = None


def is_empty_at_length(response: CanonicalResponse) -> bool:
    """The reasoning-exhaustion signature, and nothing broader.

    Empty text *and* a length stop. Empty at `stop` is a model with nothing to say, and a
    retry buys the same non-answer; a length stop with text is ordinary truncation. Widening
    ADR-0014's line is expensive: each mitigation is a dispatch at the largest budget.
    """
    return response.text == "" and response.finish_reason == "length"


async def dispatch_with_recovery(  # noqa: PLR0913 -- three of the seven are test seams
    cfg: Config,
    entry: ModelEntry,
    backend: Backend,
    build: Callable[[str, int], CanonicalRequest],
    *,
    effort: str,
    deadline: float | None,
    stall_left: Callable[[], float] | None = None,
    on_token: Callable[[str, str], None] | None = None,
    max_tokens: int | None = None,
    budget_ceiling: int | None = None,
    # Computed by the caller so the priced row and the send are one number; `None` (the
    # tests) falls back to the same `resolve_max_tokens`.
    asked_budget: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Dispatch:
    """One dispatch, plus two mitigations for an answer that ran out of room (ADR-0014).

    Send; on the exhaustion signature, retry once at a larger budget; if still empty, step
    effort down one level and send once more. No deeper cascade: every stage is a real
    dispatch at the largest budget, and more would cost more than the answer is worth.
    Stepping down comes last because the level is in the prompt: it misses the prefix
    cache and pays a fresh prefill, where the budget retry keeps both.

    `build(level, budget)` makes the request, so the caller decides everything one-shot
    and the turn loop differ in, and the stages are decided here; two copies would
    diagnose exhaustion two ways.

    `budget_ceiling` bounds every stage, or the retry would reach a budget the clock
    cannot pay. At the ceiling, the identical-request test skips the retry and the ladder
    steps down: think less rather than ask for time that does not exist.

    `attempts` sums every stage and transport retry, the real cost; token counts come
    from the answering attempt, since a retry must not charge the turn budget.
    """
    if asked_budget is None:
        asked_budget = resolve_max_tokens(cfg, entry, effort, max_tokens, ceiling=budget_ceiling)
    attempts = 0
    # `done` closes over this, so no exit from the ladder can drop a record.
    retries: list[RetryRecord] = []
    # Wall clock per engine phase, summed over stages since each is a fresh prefill; the
    # one accumulation here. `None` until a stage reports: the adapter cannot time itself.
    spans: dict[str, float | None] = {"prefill": None, "decode": None}

    def stage(reply: CanonicalResponse) -> None:
        """Add one stage's two intervals to the running totals."""
        for key, value in (
            ("prefill", reply.prefill_seconds), ("decode", reply.decode_seconds),
        ):
            if value is not None:
                spans[key] = (spans[key] or 0.0) + value

    def done(reply: CanonicalResponse, level: str, exhausted: bool = False) -> Dispatch:
        """The one place a `Dispatch` is built, so none of the four exits drops a field."""
        return Dispatch(
            response=reply, effort=level, attempts=attempts,
            reasoning_exhausted=exhausted, retries=tuple(retries),
            answered_seconds=answered,
            prefill_seconds=spans["prefill"], decode_seconds=spans["decode"],
        )

    response, spent, answered = await complete_with_retry(
        cfg, backend, build(effort, asked_budget),
        sleep=sleep, deadline=deadline, stall_left=stall_left, on_token=on_token,
        on_retry=retries.append, tick_sleep=tick_sleep, clock=clock,
    )
    attempts += spent
    stage(response)
    if not is_empty_at_length(response):
        return done(response, effort)
    # Moving on from an empty attempt is a retry too, and recorded like one: `attempts`
    # alone cannot say why it rose.
    retries.append(RetryRecord(kind="EmptyAtLength", status=None, seconds=answered, wait=0.0))

    # Stage 2: same level, more room, sized by `thinking_max_tokens_floor` (no second
    # setting).
    enlarged = max(2 * asked_budget, cfg.thinking_max_tokens_floor)
    if budget_ceiling is not None:
        enlarged = min(enlarged, budget_ceiling)
    floor = entry.cap_tokens(enlarged)
    if floor > asked_budget:
        response, spent, answered = await complete_with_retry(
            cfg, backend, build(effort, floor),
            sleep=sleep, deadline=deadline, stall_left=stall_left, on_token=on_token,
            on_retry=retries.append, tick_sleep=tick_sleep, clock=clock,
        )
        attempts += spent
        stage(response)
        if not is_empty_at_length(response):
            return done(response, effort)
        retries.append(RetryRecord(kind="EmptyAtLength", status=None, seconds=answered, wait=0.0))
    # Otherwise the model's cap pinned the first budget and the retry would be
    # byte-identical: skipped, since paying a full generation for a chance is no mitigation.

    stepped = _STEP_DOWN.get(effort)
    if stepped is None:
        # Effort is already off, so this is a budget too small, NOT reasoning exhaustion,
        # which would send the caller to lower an effort already lowest.
        return done(response, effort)

    response, spent, answered = await complete_with_retry(
        cfg, backend, build(
            stepped,
            resolve_max_tokens(cfg, entry, stepped, max_tokens, ceiling=budget_ceiling),
        ),
        sleep=sleep, deadline=deadline, stall_left=stall_left, on_token=on_token,
        on_retry=retries.append, tick_sleep=tick_sleep, clock=clock,
    )
    attempts += spent
    stage(response)
    return done(response, stepped, is_empty_at_length(response))


async def run_one_shot(  # noqa: PLR0913 -- see the note below the docstring
    cfg: Config,
    entry: ModelEntry,
    backend: Backend,
    delegation: Delegation,
    *,
    effort: str | None = None,
    max_tokens: int | None = None,
    on_alive: AliveCallback | None = None,
    on_priced: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_pool: Callable[[int | None], None] | None = None,
    rate_history: RateHistory | None = None,
    expected_concurrency: int = 1,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_token: Callable[[], None] | None = None,
    on_partial: Callable[[int, str, str], None] | None = None,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Dispatch:
    """One turn, no tools, and the recovery cascade around it.

    The right shape when no tools are offered: a model that cannot open anything is told so,
    not handed an empty tool list and a hint of a second turn. It shares
    `dispatch_with_recovery` with `run_agentic_loop`, so exhaustion is diagnosed the same.

    `dispatch_timeout` is enforced here, the only layer that knows what a delegation is: one
    deadline at entry, passed down, or the real bound would be the timeout times the stages.
    That bounds a wait, not the client's 1800s idle timeout, which could abandon a healthy
    call; with no turns to report on, `on_alive` reports on a timer (ADR-0018).
    """
    resolved = resolve_effort(cfg, entry, effort)
    origin = clock()
    deadline = origin + cfg.dispatch_timeout

    # Separate from `origin`, which anchors `deadline`. A one-shot completes no turns, so
    # token arrival is its only progress signal (ADR-0072).
    last_progress = origin

    def stall_left() -> float:
        """The no-progress deadline, from the last token this call produced.

        Without token arrival it would count from entry, making the bound the tighter of
        `stall_timeout` and `dispatch_timeout` (ADR-0047, ADR-0072).
        """
        return cfg.stall_timeout - (clock() - last_progress)

    chunks = 0
    reasoning_chunks = 0
    partial = PartialText(
        cfg.partial_every_seconds,
        None if on_partial is None else lambda r, a: on_partial(1, r, a), clock)

    def token_arrived(kind: str = "answer", piece: str = "") -> None:
        """One frame carried generated output; `kind` names which half of the reply.

        The defaults keep a backend that calls `on_token()` bare working: any frame is at
        least an answer's worth of progress, and no text is no partial.
        """
        nonlocal last_progress, chunks, reasoning_chunks
        last_progress = clock()
        chunks += 1
        if kind == "reasoning":
            reasoning_chunks += 1
        partial.add(kind, piece)
        if on_token is not None:
            on_token()

    def streamed() -> tuple[int, int, float | None]:
        """What has arrived, and how long since. Chunks, not tokens: a frame is not promised
        to be one token, and only the final usage frame counts them. The reasoning count
        lets a watcher tell thinking from answering.
        """
        return chunks, reasoning_chunks, None if not chunks else clock() - last_progress

    def request_at(level: str, budget: int) -> CanonicalRequest:
        return build_one_shot_request(
            delegation=delegation,
            effort=level,
            max_tokens=budget,
            temperature=cfg.temperature,
            top_p=cfg.top_p,
        )

    async def dispatch() -> Dispatch:
        nonlocal last_progress
        # Seeded, since a one-shot has no earlier turn and the least slack: its whole answer
        # must fit the one deadline running from entry (ADR-0055).
        rate = await seed_decode_rate(
            backend, rate_history, expected_concurrency, on_pool=on_pool,
            label_trusted=cfg.admission_idle_hold > 0,
        fallback=cfg.rate_fallback_tok_s,
        )
        # Restarted after the seed, as the loop does, so a slow `/metrics` is not charged
        # to the silence budget the answer is measured against.
        last_progress = clock()
        ceiling = rate.ceiling(cfg, budget_seconds(
            cfg, dispatch_left=deadline - clock()
        ))
        # Resolved once, before the priced row, which must say what the turn was *allowed*;
        # passed down so the send is the same number, not recomputed another way.
        asked_budget = resolve_max_tokens(
            cfg, entry, resolved, max_tokens, ceiling=ceiling
        )
        if on_priced is not None:
            await on_priced({
                "turn": 1, "effort": resolved, "max_tokens": max_tokens,
                "max_tokens_sent": asked_budget,
                "budget_ceiling": ceiling, "decode_rate": rate.rate,
                "rate_source": rate.source,
                "expected_concurrency": expected_concurrency,
                "requests_running": rate.seen_running,
                "temperature": cfg.temperature, "top_p": cfg.top_p,
            })
        return await dispatch_with_recovery(
            cfg, entry, backend, request_at,
            effort=resolved, max_tokens=max_tokens,
            budget_ceiling=ceiling, asked_budget=asked_budget,
            sleep=sleep, deadline=deadline, stall_left=stall_left, on_token=token_arrived,
            tick_sleep=tick_sleep, clock=clock,
        )

    if on_alive is None:
        return await dispatch()

    # The dispatch is one chain of awaits, so the heartbeat runs beside it; the `finally`
    # cancels and awaits it, so it never outlives the dispatch.
    beat = asyncio.create_task(_keepalive(
        cfg, on_alive, clock,
        # Not `budget_seconds`, which sizes a reply and so ignores the stall clock; a
        # countdown names whichever deadline will end the run, usually the stall (ADR-0099).
        lambda: max(min(stall_left(), deadline - clock()), 0.0),
        streamed,
    ))
    try:
        return await dispatch()
    finally:
        beat.cancel()
        try:
            await beat
        except asyncio.CancelledError:
            # Ours; re-raising would replace whatever actually ended the delegation.
            pass


class PartialText:
    """A turn's reply as it streams, handed on in pieces for a person watching (M19.5).

    The first piece of a turn goes at once, so a watcher sees text the moment there is any,
    then at most one hand-off per `every` seconds, holding what arrived between: a write per
    frame would be the four-events-a-second stream the `waiting` rate limit exists to
    prevent. Only while tokens arrive, so a quiet turn writes nothing. `every` of zero turns
    it off. The `turn` event still carries the whole reply; this is never the record.
    """

    def __init__(self, every: float, emit: Callable[[str, str], None] | None,
                 clock: Callable[[], float]) -> None:
        self._every = every
        self._emit = emit if every > 0 else None
        self._clock = clock
        self._reasoning: list[str] = []
        self._answer: list[str] = []
        self._last: float | None = None

    def add(self, kind: str, piece: str) -> None:
        if self._emit is None or not piece:
            return
        (self._reasoning if kind == "reasoning" else self._answer).append(piece)
        now = self._clock()
        if self._last is None or now - self._last >= self._every:
            self._last = now
            reasoning, answer = "".join(self._reasoning), "".join(self._answer)
            self._reasoning.clear()
            self._answer.clear()
            try:
                self._emit(reasoning, answer)
            except Exception:  # a watcher's line must never fail a turn
                self._emit = None

    def reset(self) -> None:
        """A new turn: drop what the last one held, and send the next first piece at once."""
        self._reasoning.clear()
        self._answer.clear()
        self._last = None


async def _keepalive(  # noqa: PLR0913, PLR0917 -- one reading per argument, all read per beat
    cfg: Config,
    on_alive: AliveCallback,
    clock: Callable[[], float],
    ends_in: Callable[[], float],
    streamed: Callable[[], tuple[int, int, float | None]] = lambda: (0, 0, None),
    running: Callable[[], RunningCalls] = lambda: (),
) -> None:
    """Say the delegation is still running, on a timer, until cancelled.

    It protects a long delegation from being abandoned by its client. Sleeps on
    `asyncio.sleep`, since the injected `sleep` is instant in tests and would spin. A
    failing callback stops only the heartbeat: a failed notification is not even evidence
    the client is gone. So an arity `TypeError` looks like a delivery failure, and
    `on_alive`'s shape is asserted in the tests rather than found as a heartbeat that
    quietly stopped.
    """
    started = clock()
    while True:
        await asyncio.sleep(cfg.keepalive_interval)
        try:
            # What is allowed, when the tightest deadline fires, and from streaming, how
            # much has arrived and how long since (ADR-0072), and which tools are running.
            chunks, reasoning_chunks, since = streamed()
            await on_alive(
                clock() - started, cfg.dispatch_timeout, ends_in(),
                chunks, reasoning_chunks, since, running(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return


# --- the turn loop ------------------------------------------------------------------------


def _tool_result_sizes(cfg: Config, messages: tuple[Message, ...]) -> list[int]:
    """Estimated tokens of each tool result, in history order, which eviction walks.

    Sizes, not a count, because bytes fill a window. Costed at the densest ratio, so it
    over-counts: under-counting would retain more than fits.
    """
    return [
        cfg.estimate_tokens(len(b.content))
        for m in messages
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]


# What an evicted tool result becomes. The block keeps its `tool_use_id`, since some
# backends require a result for every tool_use and would fail the delegation at the wire.
EVICTED_STUB = "[dropped from the history to keep it bounded. Call the tool again if needed.]"

# Prefixed to a result served from the dedup cache: a repeating model is usually stuck, and
# hearing that nothing new happened is what breaks that.
REPEAT_PREFIX = "[repeat of an identical earlier call; nothing was run again]\n"

# The pointer served on the FIRST read answered from a prefetched entry. The model already
# holds the lines -- they are in the files block at the start of the conversation -- so
# resending them is the waste this cache exists to stop. The pointer tells it they are there
# and how to get them repeated, rather than silently returning nothing.
PREFETCHED_POINTER = (
    "[these lines are already in the files block at the start of this conversation, under "
    "{path}; nothing was run. Ask again with the same arguments to have them repeated here.]"
)

# Prefixed to a read answered from a prefetched entry that already got its pointer: the
# model asked for the lines again, so it gets them, marked as coming from the files block.
# Not `REPEAT_PREFIX`, whose "identical earlier call" would be false -- nothing was run,
# and this was not an earlier call's result.
PREFETCHED_REPEAT_PREFIX = (
    "[from the files block at the start of this conversation:]\n"
)

# Served once eviction dropped the cached result, so an identical call answers at once
# without re-running (ADR-0080); it points the model at asking for a narrower part.
EVICTED_REPEAT = (
    "[identical to an earlier call whose result was dropped from the history to stay "
    "inside the context window. Nothing was run again and the content is not being "
    "restored -- ask for the part of it you need.]"
)


@dataclass(frozen=True, slots=True)
class _CachedResult:
    """One dedup-cache entry, and whether the history still holds what it copies.

    `tool_use_id` lets eviction find the entry for a result it stubbed, the cache being
    keyed by call. The last three fields are `read_file`'s range coverage: `path` and
    `span` describe the read that produced `content`, `reached_eof` whether it ran to the
    end of the file untruncated. Everything else carries None/False, so an exact-argument
    hit is unaffected.
    """

    content: str
    tool_use_id: str
    evicted: bool = False
    path: str | None = None
    span: tuple[int, int] | None = None
    reached_eof: bool = False
    # Seeded from a prefetched `FileEntry`, not from a tool result: nothing was run to
    # produce it, and the lines are in the prompt's files block already. A prefetched entry
    # is never evicted (its `tool_use_id` never enters the history), so its first covering
    # read answers with a pointer and `pointed` records that the pointer was sent.
    prefetched: bool = False
    pointed: bool = False


# The truncation footer `_read_file` appends when a character budget cut a range short. Its
# presence means the result is not the lines it was asked for, so it cannot be used to cover
# a range ending past what it actually holds.
_TRUNCATION_MARKER = "[truncated:"


def _read_coverage(
    call: ToolUseBlock, content: str
) -> tuple[str | None, tuple[int, int] | None, bool]:
    """The range facts a `read_file` result records, for storing in the cache.

    `span` is what the result holds; `reached_eof` is whether it ran to the file's end
    untruncated -- no truncation footer, and no `end_line` (or one of 0, which `_read_file`
    treats as none) or an `end_line` past the last line it holds. Only `read_file` has
    range facts; anything else records None.
    """
    if call.name != "read_file":
        return None, None, False
    path = call.input.get("path")
    path = path if isinstance(path, str) else None
    span = numbered_span(content)
    reached_eof = False
    if span is not None and _TRUNCATION_MARKER not in content:
        end_line = call.input.get("end_line")
        if end_line is None or end_line == 0:
            reached_eof = True
        elif isinstance(end_line, int) and not isinstance(end_line, bool) and end_line > 0:
            reached_eof = end_line > span[1]
    return path, span, reached_eof


def _requested_range(call: ToolUseBlock) -> tuple[int, int | None] | None:
    """The (start, end) `call` asks for, normalized, or None when not coverable.

    `start_line` omitted means 1. `end_line` omitted or 0 is `_read_file`'s "to the end"
    (None). A range the tool would refuse -- a start below 1, a negative or non-integer
    `end_line`, or an `end_line` before the start -- is not coverable, since serving a
    repeat would turn a refusal into an empty answer.
    """
    start = call.input.get("start_line", 1)
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        return None
    raw_end = call.input.get("end_line")
    if raw_end is None:
        end = None
    elif not isinstance(raw_end, int) or isinstance(raw_end, bool) or raw_end < 0:
        return None
    else:
        end = raw_end if raw_end > 0 else None
    if end is not None and end < start:
        return None
    return start, end


def _slice_lines(call: ToolUseBlock, body: str) -> str:
    """The numbered lines of `body` whose number lies in the range `call` asks for.

    The requested lines only, kept in the form the earlier result rendered them, so a
    covered read hands back exactly what was asked rather than the whole earlier read.
    """
    range_ = _requested_range(call)
    if range_ is None:
        return ""
    start, end = range_
    kept: list[str] = []
    for number, line in numbered_lines(body):
        if number < start:
            continue
        if end is not None and number > end:
            continue
        kept.append(line)
    return "\n".join(kept)


def _covering_entry(
    cached: dict[tuple[str, str], _CachedResult], call: ToolUseBlock
) -> tuple[tuple[str, str], _CachedResult] | None:
    """A cached `read_file` whose span covers `call`'s range, with its key, or None.

    Coverage is by the span the earlier result actually holds. A not-evicted covering entry
    is preferred so content can be sliced from it; if only an evicted one covers, it still
    answers as a repeat, but `EVICTED_REPEAT` is served rather than content being restored.
    Only `read_file` gets this; every other tool stays exact-argument.

    The key is returned with it, since a prefetched entry's pointer is recorded by
    replacing the entry and the caller needs the key to do so without a second scan.
    """
    if call.name != "read_file":
        return None
    path = call.input.get("path")
    if not isinstance(path, str):
        return None
    range_ = _requested_range(call)
    if range_ is None:
        return None
    start, end = range_
    evicted_fallback: tuple[tuple[str, str], _CachedResult] | None = None
    for key, entry in cached.items():
        if entry.path != path or entry.span is None:
            continue
        first, last = entry.span
        if start < first or start > last:
            continue
        if end is None:
            if not entry.reached_eof:
                continue
        elif end > last:
            continue
        if not entry.evicted:
            return key, entry
        if evicted_fallback is None:
            evicted_fallback = (key, entry)
    return evicted_fallback


def _repeat_body(
    call: ToolUseBlock,
    entry: _CachedResult,
    *,
    exact: bool,
    cached: dict[tuple[str, str], _CachedResult] | None = None,
    key: tuple[str, str] | None = None,
) -> str:
    """What a repeat hands back: the prefetched pointer or content, the evicted notice, or
    `REPEAT_PREFIX` plus the content.

    `exact` says the call is byte-identical to the one that produced `entry`, so the whole
    content is served; otherwise the requested lines are sliced out of it.

    A prefetched entry answers its first covering read with the pointer, not the content --
    the model already holds the lines, so resending them is the waste this cache exists to
    stop -- and is rewritten `pointed`, so a later read of the same span is answered with
    the content, prefixed to say it came from the files block.
    """
    if entry.prefetched:
        assert cached is not None and key is not None
        if not entry.pointed:
            cached[key] = replace(entry, pointed=True)
            return PREFETCHED_POINTER.format(path=entry.path)
        return PREFETCHED_REPEAT_PREFIX + _slice_lines(call, entry.content)
    if entry.evicted:
        return EVICTED_REPEAT
    if exact:
        return REPEAT_PREFIX + entry.content
    return REPEAT_PREFIX + _slice_lines(call, entry.content)


def _prefetch_span(pre: FileEntry) -> tuple[int, int]:
    """The line span a prefetched entry holds, `(first, last)` in the file's own numbers."""
    if pre.start_line is None:
        return 1, len(pre.text.splitlines())
    return pre.start_line, pre.start_line + len(pre.text.split("\n")) - 1


def _prefetch_key(pre: FileEntry) -> tuple[str, str]:
    """The cache key for a prefetched entry: unique per span, never a real call's key.

    A real call's key is `(name, dedup_key)`; this one can never collide with it, so a
    prefetched entry is reached only through `_covering_entry`, never as an exact repeat.
    """
    first, last = _prefetch_span(pre)
    return ("prefetch", f"{pre.path}:{first}-{last}")


def _prefetched_cache_entry(pre: FileEntry) -> _CachedResult:
    """A prefetched file as a cache entry shaped like a `read_file` result for its span.

    The content is numbered exactly as `read_file` renders it -- the same width and the
    file's own line numbers, via `context`'s numbering helpers -- so `_slice_lines` hands
    back a sub-range identical to what the tool would have returned, and `_covering_entry`
    covers it by the span the entry actually holds.
    """
    first, last = _prefetch_span(pre)
    total = pre.total_lines if pre.start_line is not None and pre.total_lines else last
    width = line_number_width(total)
    lines = pre.text.split("\n") if pre.start_line is not None else pre.text.splitlines()
    body = "\n".join(numbered_line(n, line, width) for n, line in enumerate(lines, first))
    return _CachedResult(
        content=body,
        # Never in the history, so `_mark_evicted_in_cache` can never match it.
        tool_use_id=f"prefetch:{pre.path}:{first}-{last}",
        path=pre.path,
        span=(first, last),
        reached_eof=(last == total),
        prefetched=True,
    )


async def _no_progress(turn: int, of: int) -> None:
    """The default when nobody is listening. Tests inject a recorder, `server.py` the real
    one."""


def resolve_max_turns(
    cfg: Config, explicit: int | None = None, allowed: frozenset[str] = frozenset()
) -> int:
    """The turn budget: the caller's number, else the configured default, capped either way.

    The hard cap applies silently, so no caller or agent file holds the cluster for hours;
    refusing would be worse, since only the number is wrong. Writing work iterates, so it
    gets the writing default; a reading pass keeps the smaller one.
    """
    if explicit is None:
        if any(REGISTRY[name].writes for name in allowed if name in REGISTRY):
            return min(cfg.max_turns_default_writing, cfg.max_turns_hard_cap)
        # Not clamped: config.py refuses a default above the cap, and a guard that cannot
        # fire is trusted anyway.
        return cfg.max_turns_default
    if explicit < 1:
        raise InvalidDelegation(
            f"max_turns={explicit} must be at least 1. A delegation with no turns cannot "
            "produce an answer."
        )
    return min(explicit, cfg.max_turns_hard_cap)


def stub_oldest_tool_results(
    messages: tuple[Message, ...], upto: int
) -> tuple[tuple[Message, ...], int]:
    """Collapse the oldest `upto` tool results. Returns the history and a count.

    Every turn resends the history, so cost grows with its square. `upto` is a boundary the
    caller carries, not recomputed from `keep`, so one rewrite buys `keep` turns of prefix
    stability (ADR-0056). Only content goes; an already-evicted result is not recounted.
    """
    positions = [
        (mi, bi)
        for mi, message in enumerate(messages)
        for bi, block in enumerate(message.content)
        if isinstance(block, ToolResultBlock)
    ]
    stale = positions[:upto] if upto > 0 else []
    doomed = {
        (mi, bi)
        for mi, bi in stale
        if getattr(messages[mi].content[bi], "content", None) != EVICTED_STUB
    }
    if not doomed:
        return messages, 0

    touched = {mi for mi, _ in doomed}
    rebuilt: list[Message] = []
    for mi, message in enumerate(messages):
        if mi not in touched:
            rebuilt.append(message)
            continue
        blocks: list[ContentBlock] = []
        for bi, block in enumerate(message.content):
            if (mi, bi) in doomed and isinstance(block, ToolResultBlock):
                blocks.append(
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id,
                        content=EVICTED_STUB,
                        is_error=block.is_error,
                    )
                )
            else:
                blocks.append(block)
        rebuilt.append(Message(message.role, tuple(blocks)))
    return tuple(rebuilt), len(doomed)


def countdown_line(turns_left: int) -> str:
    """What the model is told about its remaining budget, on the message carrying results.

    Not in the system prompt, where a counter would cost a full prefill per turn (ADR-0011);
    the tail beside fresh results was never cached.
    """
    if turns_left <= 1:
        return (
            "[final turn: tools are withdrawn for it. Give your answer now -- whatever you "
            "write next is what the engineer receives.]"
        )
    return f"[{turns_left} turns remain, this one included.]"


# --- context economics --------------------------------------------------------------------


# Told when projected usage crosses the nudge threshold. Beside `countdown_line`, never
# instead: one counts room, the other turns, and either can run short alone.
WRAP_UP_LINE = (
    "[context is running short. Stop opening new threads of work: finish what you have, "
    "and write your answer while there is still room for it.]"
)


def estimate_message_tokens(cfg: Config, message: Message) -> int:
    """Rough size of one history message, in the same estimated tokens as everything else.

    Ours, not the backend's: the plateau check holds our number against theirs, and two
    from one source could agree while both wrong. Densest ratio, so it over-counts;
    under-counting would suppress a real detection.
    """
    nbytes = 0
    for block in message.content:
        if isinstance(block, (TextBlock, ThinkingBlock)):
            nbytes += len(block.text)
        elif isinstance(block, ToolResultBlock):
            nbytes += len(block.content)
        elif isinstance(block, ToolUseBlock):
            nbytes += len(block.name) + len(_dedup_key(block))
    return cfg.estimate_tokens(nbytes)


def overflow_reserve(cfg: Config, entry: ModelEntry) -> int:
    """Headroom held back for the reply, as a fraction of *this model's* window.

    A fraction, never a count: a reserve prudent on a 1M window exceeds 95% of an 8K one,
    so the smaller model would read as full with an empty history.
    """
    return round(entry.context_window * cfg.overflow_reserve_fraction)


def projected_fraction(
    cfg: Config, entry: ModelEntry, last_input_tokens: int, pending_tokens: int
) -> float:
    """Share of the window this turn is projected to occupy, in [0, ...).

    **The only place the denominator is chosen: `entry.context_window`**, never a reply
    budget or a count of our own evictions. A wrong denominator is the usual shape of this
    bug, so every threshold uses this. An entry omitting `context_window` silently inherits
    a default, which is why `context_overflow_enabled` is off by default.
    """
    reserve = overflow_reserve(cfg, entry)
    return (last_input_tokens + pending_tokens + reserve) / entry.context_window


def plateaued_without_eviction(
    cfg: Config,
    *,
    prev_input_tokens: int,
    this_input_tokens: int,
    grew_by: int,
    evicted_this_turn: int,
) -> bool:
    """Did the prompt stop growing for a reason this server cannot account for?

    If real content was appended, the reported prompt did not grow, and we did not evict,
    the backend dropped it silently. `evicted_this_turn` is **a count this server set when
    it evicted** (ADR-0007), checked first because our own eviction is the likeliest
    plateau. `grew_by` has a floor since a plateau under an empty append is not evidence,
    and the slop keeps a trimmed token from reading as truncation.
    """
    if evicted_this_turn > 0:
        return False
    if grew_by <= cfg.overflow_min_growth_tokens:
        return False
    return this_input_tokens <= prev_input_tokens + cfg.overflow_plateau_slop_tokens


# One tool call as the server saw it: name, path argument, failed. Enough to reconcile with
# `git status`; content would make an abort report as big as the delegation.
_Ledger = list[tuple[str, str, bool]]

# Caps on a recorded argument and refusal; constants, since they shape a record, not a
# deployment. The refusal gets more: it is why the record exists.
TOOL_ARG_VALUE_CAP = 240
TOOL_MESSAGE_CAP = 600

# Arguments carrying a file body: summarised to length and digest, since a truncated body is
# useless to read and still a partial copy at rest (ADR-0039).
_BODY_ARG_NAMES = frozenset({"content", "old_string", "new_string"})


def _elide(text: str, cap: int) -> str:
    """Shorten to `cap`, saying how much was dropped, or a truncated path reads as sent."""
    if len(text) <= cap:
        return text
    return f"{text[:cap]}... [+{len(text) - cap} chars]"


def _line_count(text: str) -> int:
    """Lines in a result, none when empty; a trailing newline adds no line."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


# Where each tool puts its truncation note: after a blank line at the end, or first in
# the output that follows the result's header line and its blank line.
_NOTE_LAST = frozenset({"read_file", "read_git"})
_NOTE_FIRST = frozenset({"run_bash"})


def _output_line_count(name: str, text: str) -> int:
    """Lines of the tool's own output, leaving out the server's note that it was cut.

    Counted, the note made a 657-line read show as 659. Recognised only where that tool
    writes it, so a file quoting the marker keeps every line.
    """
    if name in _NOTE_LAST:
        body, sep, note = text.rpartition("\n\n" + _TRUNCATION_MARKER)
        if sep and "\n" not in note:
            return _line_count(body)
    elif name in _NOTE_FIRST:
        head, sep, output = text.partition("\n\n")
        if sep and output.startswith(_TRUNCATION_MARKER):
            return _line_count(head + sep + output.partition("\n")[2])
    return _line_count(text)


def _body_summary(text: str) -> str:
    """A file body as a length and a digest, never as bytes.

    The digest tells two writes apart, the one question asked of a body; twelve hex
    characters identify a write within one delegation.
    """
    digest = sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"<{len(text)} chars, sha256:{digest}>"


def record_arguments(call: ToolUseBlock) -> tuple[tuple[str, str], ...]:
    """What the model asked for, capped per field and with bodies summarised.

    Sorted, so records of one call compare equal whatever order a backend serialised.
    """
    return tuple(
        (
            key,
            _body_summary(str(value)) if key in _BODY_ARG_NAMES
            else _elide(str(value), TOOL_ARG_VALUE_CAP),
        )
        for key, value in sorted(call.input.items())
    )


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool call as the record keeps it: what was asked, and what came back.

    A dataclass, since widening a tuple other code unpacks breaks a caller quietly.
    `message` is the refusal, on an error only; success carries accounting, never content
    (ADR-0039).
    """

    name: str
    outcome: str
    arguments: tuple[tuple[str, str], ...]
    # Capped refusal text; empty on success, and the outcome says which.
    message: str = ""
    # Size in bytes and lines, for every tool: lines is `search_files`' match count,
    # `run_bash`'s output and `read_file`'s file length, where per-tool counters would
    # drift. None when no result reached us.
    result_bytes: int | None = None
    result_lines: int | None = None
    # Only when a process exited. None is both "no shell command" and "killed first"
    # (`bash_failures` tells them apart); 0 is a real exit code (see `BashOutcome`).
    exit_code: int | None = None
    # Timed where it executed; `None` when the dedup cache answered.
    ms: int | None = None
    # Pipelines (2+ stages) in which a stage exited non-zero, for the operator transcript
    # only. Empty when none failed this way; the model never sees these.
    stages: tuple[tuple[tuple[str, int], ...], ...] = ()

    def as_json(self) -> dict[str, Any]:
        """The record as one JSON object, absent fields absent rather than null, which
        would read as a measured empty."""
        row: dict[str, Any] = {
            "name": self.name,
            "outcome": self.outcome,
            "arguments": dict(self.arguments),
        }
        if self.message:
            row["message"] = self.message
        if self.result_bytes is not None:
            row["result_bytes"] = self.result_bytes
            row["result_lines"] = self.result_lines
        if self.exit_code is not None:
            row["exit_code"] = self.exit_code
        if self.ms is not None:
            row["ms"] = self.ms
        if self.stages:
            # Capped like an argument, because a stage can be a whole heredoc.
            row["stages"] = [
                [
                    {"command": _elide(command, TOOL_ARG_VALUE_CAP), "status": status}
                    for command, status in stage
                ]
                for stage in self.stages
            ]
        return row


def tool_call_record(
    call: ToolUseBlock, outcome: str, result: ToolResultBlock | None,
    *, ms: int | None = None,
) -> ToolCallRecord:
    """Build one call's record from what the server saw, never from the model's account.

    The refusal is the string the model was handed, so the two agree (ADR-0007). `ms` comes
    from `_run_calls`, `None` when nothing ran.
    """
    message = ""
    if outcome == "error" and result is not None:
        message = _elide(result.content, TOOL_MESSAGE_CAP)
    exit_code = None
    if result is not None and result.bash is not None and result.bash.ran:
        exit_code = result.bash.exit_code
    return ToolCallRecord(
        name=call.name,
        outcome=outcome,
        arguments=record_arguments(call),
        message=message,
        result_bytes=None if result is None else len(result.content),
        # Zero for empty, or an empty `search_files` reads as one match.
        result_lines=None if result is None else _output_line_count(call.name, result.content),
        exit_code=exit_code,
        ms=ms,
        stages=() if result is None or result.bash is None else result.bash.stages,
    )


@dataclass(frozen=True, slots=True)
class TurnDiagnostic:
    """What one turn cost and what it did, kept only when the caller asked for it.

    Server-watched, never the model's account (ADR-0007): where the ledger says nine turns
    and twelve evictions, this says which turn and what the prompt cost around it. No
    result content.
    """

    turn: int
    input_tokens: int
    output_tokens: int
    # Served from the prefix cache. Per turn, because a cache going cold mid-run hides in a
    # delegation-wide figure. `None`: the endpoint reports no caching, which is not zero.
    cached_tokens: int | None
    attempts: int
    effort: str
    evicted: int
    tool_calls: tuple[ToolCallRecord, ...]
    # Beside `attempts`: the count says how many, these say which and why.
    retries: tuple[RetryRecord, ...] = ()
    # Backend time summed over attempts, since each paid a prefill. Carried here because
    # the turn event reads everything off this object, and a second channel would drift.
    prefill_seconds: float | None = None
    decode_seconds: float | None = None
    # The answering attempt's decode span: the only interval `output_tokens` may divide,
    # since the counts come from that attempt (ADR-0014).
    answered_decode_seconds: float | None = None
    # The answering attempt's reasoning span: first streamed chunk to first answer chunk,
    # a tool call counting as an answer. Measured per response, so a retried turn must not
    # count the failed attempt's reasoning against itself.
    reasoning_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class RereadAfterEviction:
    """A file read again after this server dropped the first read from the history.

    What tells an expensive delegation from one paying twice for the same bytes, the one
    that raising `keep_tool_results` fixes.
    """

    path: str
    evicted_at_turn: int
    reread_at_turn: int


def _mark_evicted_in_cache(
    cached: dict[tuple[str, str], _CachedResult], dropped_ids: tuple[str, ...]
) -> None:
    """Tell the dedup cache that the history no longer holds what it copied.

    Marked, not deleted, which would make the next identical call re-run the tool; a marked
    entry still runs nothing but stops undoing the eviction. Monotonic, like the boundary,
    since the history is never restored.
    """
    if not dropped_ids:
        return
    gone = set(dropped_ids)
    for key, entry in cached.items():
        if not entry.evicted and entry.tool_use_id in gone:
            cached[key] = replace(entry, evicted=True)


def newly_evicted_ids(
    before: tuple[Message, ...], after: tuple[Message, ...]
) -> tuple[str, ...]:
    """Which tool results this eviction pass replaced with the stub, by `tool_use_id`.

    A diff, not a third return from `stub_oldest_tool_results`, whose tested tuple would
    break a caller quietly if widened; it reads that function's output, so cannot disagree.
    """
    was_stub = {
        block.tool_use_id
        for message in before
        for block in message.content
        if isinstance(block, ToolResultBlock) and block.content == EVICTED_STUB
    }
    return tuple(
        block.tool_use_id
        for message in after
        for block in message.content
        if isinstance(block, ToolResultBlock)
        and block.content == EVICTED_STUB
        and block.tool_use_id not in was_stub
    )


class _Watch:
    """Everything the server observed about one delegation, in one place.

    Calls, the file each named, evictions and cost: server-captured, never the model's
    account (ADR-0007). `calls` always (an overflow abort needs it), per-turn detail only
    with `diagnostics`, and the path maps always, since their correlation cannot be rebuilt.
    """

    def __init__(self, *, diagnostics: bool) -> None:
        self.diagnostics = diagnostics
        self.calls: _Ledger = []
        self.turns: list[TurnDiagnostic] = []
        # Always summed: cluster cost is not a debugging extra, and the per-turn detail is
        # off by default (ADR-0058). `cached` stays None until an endpoint reports caching.
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_tokens: int | None = None
        self.rereads: list[RereadAfterEviction] = []
        self.turn = 0  # the turn now running, so callees need not be handed it
        # `AgenticDispatch`'s ledger. Counters, since `attempts` and `evicted` have no entry
        # in `calls` to derive from.
        self.attempts = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.deduped = 0
        self.evictions = 0
        # Split by tool, counted as calls happen rather than rebuilt from `calls` later.
        self.by_tool: Counter[str] = Counter()
        # From real process exits, never the model's account (ADR-0007).
        self.bash_calls = 0
        self.bash_failures = 0
        # Kept apart from `bash_failures`: a failure hidden inside a compound line calls for
        # different action than a non-zero exit.
        self.bash_masked_failures = 0
        # Calls that failed at all, each once. `tool_errors` and `bash_failures` overlap,
        # and neither contains the other, so neither nor their sum gives this.
        self.failed_calls = 0
        self.last_bash_exit: int | None = None
        self._paths: dict[str, str] = {}  # tool_use_id -> the path argument it carried
        self._evicted_at: dict[str, int] = {}  # path -> the turn its result was dropped

    def called(
        self, call: ToolUseBlock, outcome: str, result: ToolResultBlock | None = None
    ) -> None:
        """Record one tool call, and correlate it against anything we evicted earlier."""
        is_error = outcome == "error"
        self.tool_calls += 1
        self.by_tool[call.name] += 1
        self.tool_errors += is_error
        self.deduped += outcome == "repeat"
        shell_failed = False
        if result is not None and result.bash is not None:
            shell_failed = self._bash(result.bash, is_error)
        self.failed_calls += is_error or shell_failed
        path = str(call.input.get("path") or "")
        self.calls.append((call.name, path, is_error))
        if path:
            # By id, since an eviction knows only ids. The raw argument: `tools.py` never
            # returns the resolved path, and a reader reconciling the tree wants this one.
            self._paths[call.id] = path
        self._reread(path)

    def _bash(self, bash: BashOutcome, is_error: bool) -> bool:
        """One shell command, as the server saw it rather than as the model reports it.

        Counts attempts, refusals included. `last_bash_exit` moves only when something ran:
        a timeout kill ran and reports no code, so a previous 0 cannot read as success; a
        refusal started nothing, so the previous code stands. `masked_failure` counts in
        `bash_failures` but never touches `last_bash_exit`, where a non-zero stays
        trustworthy and a zero ambiguous (ADR-0007).
        """
        self.bash_calls += 1
        masked = bash.ran and bash.masked_failure
        failed = bool(is_error or bash.timed_out or bash.exit_code != 0 or masked)
        self.bash_failures += failed
        self.bash_masked_failures += masked
        if bash.ran:
            self.last_bash_exit = bash.exit_code
        return failed

    def evicted(self, before: tuple[Message, ...], after: tuple[Message, ...], count: int) -> None:
        self.evictions += count
        for dropped_id in newly_evicted_ids(before, after):
            path = self._paths.get(dropped_id)
            if path:
                self._evicted_at.setdefault(path, self.turn)

    def _reread(self, path: str) -> None:
        """Note a file read again after this server dropped the first read of it."""
        was = self._evicted_at.pop(path, 0) if path else 0
        if was:
            self.rereads.append(
                RereadAfterEviction(path, evicted_at_turn=was, reread_at_turn=self.turn)
            )

    def turn_cost(self, dispatch: Dispatch, *, evicted: int) -> None:
        """What this turn cost, recorded when the backend answers, since the answering turn
        breaks out before the loop body ends."""
        self.attempts += dispatch.attempts
        self.total_input_tokens += dispatch.response.input_tokens or 0
        self.total_output_tokens += dispatch.response.output_tokens or 0
        if dispatch.response.cached_tokens is not None:
            self.total_cached_tokens = (
                (self.total_cached_tokens or 0) + dispatch.response.cached_tokens
            )
        if not self.diagnostics:
            return
        self.turns.append(
            TurnDiagnostic(
                turn=self.turn,
                input_tokens=dispatch.response.input_tokens,
                output_tokens=dispatch.response.output_tokens,
                cached_tokens=dispatch.response.cached_tokens,
                attempts=dispatch.attempts,
                effort=dispatch.effort,
                evicted=evicted,
                tool_calls=(),
                retries=dispatch.retries,
                prefill_seconds=dispatch.prefill_seconds,
                decode_seconds=dispatch.decode_seconds,
                answered_decode_seconds=dispatch.response.decode_seconds,
                reasoning_seconds=dispatch.response.reasoning_seconds,
            )
        )

    def turn_tools(self, records: tuple[ToolCallRecord, ...]) -> None:
        """Attach this turn's tool records to the record `turn_cost` already opened."""
        if self.diagnostics and self.turns:
            self.turns[-1] = replace(self.turns[-1], tool_calls=records)


class _OverflowGuard:
    """The context economics of one delegation, kept together rather than in the loop.

    Apart from the loop so the thresholds are testable without a delegation, since a check
    here that could not fire would be trusted. Inert unless `context_overflow_enabled`:
    every method then answers "do nothing", so the loop needs no branch around each call.
    """

    def __init__(self, cfg: Config, entry: ModelEntry) -> None:
        self.cfg = cfg
        self.entry = entry
        # Only tightens: headroom won by evicting does not stop the growth.
        self.keep = cfg.keep_tool_results
        # Oldest results already stubbed. Monotonic, which keeps the prefix: a recomputed
        # boundary moves every turn and each move costs the cache what follows (ADR-0056).
        self.evicted_upto = 0
        self.prev_input_tokens = 0  # what the backend reported for the previous request
        self.pending_tokens = 0  # what we have appended since that request
        self.tightened_at = 0
        self.nudged_at = 0

    @property
    def armed(self) -> bool:
        return self.cfg.context_overflow_enabled

    @property
    def pressure_known(self) -> bool:
        """Whether `share()` is measured against a window somebody actually chose.

        The overflow flag ships off because an entry omitting `context_window` inherits a
        silent default. Only `evict_upto` reads this; tighten, nudge, abort and the plateau
        check act on a delegation and stay on the flag. Otherwise an unarmed `evict_upto`
        would stub on count alone and its threshold would never apply as shipped.
        """
        return not self.entry.context_window_defaulted

    def share(self) -> float:
        return projected_fraction(
            self.cfg, self.entry, self.prev_input_tokens, self.pending_tokens
        )

    def evict_upto(self, sizes: Sequence[int]) -> int:
        """How many of the oldest tool results should be stubbed, now. Never retreats.

        Takes sizes, oldest first, since a count prices a refusal like a 50KB file
        (ADR-0079). `keep` is the floor and the step, so one rewrite buys `keep` turns of
        prefix stability; below the pressure threshold the boundary holds, un-stubbing
        costing the same cache (ADR-0056). Only the holding is gated, or the default history
        would be unbounded.
        """
        step = max(self.keep, 1)
        # The floor first: `keep` results survive whatever the sizes say.
        evictable = max(len(sizes) - self.keep, 0)
        # Oldest-first, never cheapest-first: cutting from the middle of a prefix-cached
        # prompt invalidates everything after it (ADR-0056). Size decides only where.
        budget = self.cfg.retained_tool_result_tokens
        need = 0
        retained = sum(sizes)
        while need < evictable and retained > budget:
            retained -= sizes[need]
            need += 1
        # Floored to a whole step. Rounding up, capped by `evictable`, would advance by one
        # every turn: the per-turn boundary ADR-0056 exists to stop.
        want = (min(need, evictable) // step) * step
        if (self.armed or self.pressure_known) and self.share() < OVERFLOW_EVICT_AT:
            return self.evicted_upto
        self.evicted_upto = max(self.evicted_upto, want)
        return self.evicted_upto

    def added(self, message: Message) -> None:
        self.pending_tokens += estimate_message_tokens(self.cfg, message)

    def begin_turn(self, *, turn: int, turns: int, ledger: _Ledger) -> None:
        """Abort if there is no room left, tighten retention if there is nearly none.

        Before eviction, since the projection decides how hard to evict; after would
        measure the cure.
        """
        if not self.armed:
            return
        share = self.share()
        if share >= OVERFLOW_ABORT_AT:
            raise ContextOverflowAborted(
                f"Stopped on turn {turn} of {turns}: projected context use reached "
                f"{share:.0%} of this model's {self.entry.context_window}-token window, at "
                f"or past the {OVERFLOW_ABORT_AT:.0%} abort threshold. Continuing would "
                "have produced an answer written against a history the backend was already "
                "dropping. The report below is what the server watched happen.",
                report=_overflow_report(ledger, turn=turn, turns=turns, share=share),
            )
        if share >= OVERFLOW_TIGHTEN_AT and not self.tightened_at:
            self.tightened_at = turn
            self.keep = self.cfg.overflow_tightened_keep_tool_results

    def observe(
        self,
        response: CanonicalResponse,
        *,
        evicted_this_turn: int,
        turn: int,
        turns: int,
        ledger: _Ledger,
    ) -> None:
        """Record what the prompt actually cost, and abort if it stopped growing.

        Only an eviction made *this* turn can explain this turn's plateau.
        """
        if self.armed and turn > 1 and plateaued_without_eviction(
            self.cfg,
            prev_input_tokens=self.prev_input_tokens,
            this_input_tokens=response.input_tokens,
            grew_by=self.pending_tokens,
            evicted_this_turn=evicted_this_turn,
        ):
            raise ContextOverflowAborted(
                f"Stopped on turn {turn} of {turns}: about {self.pending_tokens} estimated "
                f"tokens were added to the history, but the backend reported the prompt "
                f"moving only from {self.prev_input_tokens} to {response.input_tokens} "
                "tokens, and this server evicted nothing this turn that would account for "
                "it. Something upstream is dropping history silently, so anything the "
                "model says from here is written against a conversation it can no longer "
                "see the whole of.",
                report=_overflow_report(ledger, turn=turn, turns=turns, share=None),
            )
        self.prev_input_tokens = response.input_tokens
        self.pending_tokens = 0

    def nudge_due(self, *, turn: int) -> bool:
        """Whether this turn's results should carry the wrap-up line.

        Asked after the response, so it uses the backend's cost, not the opening estimate.
        """
        if not self.armed or self.share() < OVERFLOW_NUDGE_AT:
            return False
        self.nudged_at = self.nudged_at or turn
        return True


def _preserve_across_nudge(
    response: CanonicalResponse, *, said_before: str
) -> CanonicalResponse:
    """Keep what the model said before it was told to wrap up.

    The final reply's text is the whole answer, and a nudged model often replies "finishing
    now", replacing the substance it wrote. So a nudge reply **concatenates, never
    overwrites**. Only here: elsewhere earlier turns are narration and would worsen every
    answer. Unchanged when there is nothing to keep or the reply already contains it.
    """
    if not said_before.strip():
        return response
    ending = response.text
    if said_before.strip() in ending:
        return response
    kept = TextBlock(said_before.rstrip() + "\n\n")
    return replace(response, content=(kept, *response.content))


def _overflow_report(
    ledger: _Ledger, *, turn: int, turns: int, share: float | None
) -> dict[str, object]:
    """What the server watched, beside what the working tree says.

    Never merged: where they disagree, that is the finding (ADR-0007). `git status` covers
    only the directories written into.
    """
    writes = [path for name, path, is_error in ledger if name == "write_file" and path]
    failed = [path for name, path, is_error in ledger if name == "write_file" and is_error]
    directories = sorted({str(Path(path).parent) for path in writes if path})
    return {
        "stopped_on_turn": turn,
        "of_turns": turns,
        "projected_context_use": None if share is None else round(share, 4),
        "tool_calls_run": len(ledger),
        "files_written": sorted(set(writes) - set(failed)),
        "writes_that_failed": sorted(set(failed)),
        "git_status": {top: list(lines) for top, lines in repo_status(directories).items()},
    }


def _dedup_key(call: ToolUseBlock) -> str:
    """A stable rendering of one call's arguments, for comparison only.

    `sort_keys`, since key order does not make a different call; `default=str`, since this
    must never raise on what a model sent, at worst missing a dedup.
    """
    return json.dumps(call.input, sort_keys=True, default=str)


@dataclass(frozen=True, slots=True)
class AgenticDispatch:
    """What one agentic delegation did, counted by the server rather than told by the model.

    The first four fields match `Dispatch`; the rest is ADR-0007's ledger, extended to the
    loop's economics.
    """

    response: CanonicalResponse
    effort: str
    attempts: int
    reasoning_exhausted: bool = False
    turns: int = 1
    tool_calls: int = 0
    # By tool name, sorted, so equal calls render equally in any order. A tuple, since a
    # dict in a frozen dataclass would let a caller rewrite the ledger it was handed.
    tool_calls_by_name: tuple[tuple[str, int], ...] = ()
    tool_errors: int = 0
    deduped: int = 0
    evicted: int = 0
    # The whole run, where `response` is the answering turn: "was it truncated" wants that,
    # "what did it cost" wants these (ADR-0058).
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int | None = None
    hit_turn_limit: bool = False
    # The resolved turn budget: "six turns" and "six of six" are different news.
    max_turns: int = 0
    # ADR-0007. `last_bash_exit` None means nothing exited (none ran, or killed on timeout);
    # 0 is a real exit code.
    bash_calls: int = 0
    bash_failures: int = 0
    # Also inside `bash_failures`; apart, it says how many failures the exit code hid.
    bash_masked_failures: int = 0
    failed_calls: int = 0
    last_bash_exit: int | None = None
    # Zero is never (turns count from one). A tightened or nudged answer was written under
    # different conditions, which its text does not show.
    overflow_tightened_at: int = 0
    overflow_nudged_at: int = 0
    # Empty unless asked, so it cannot read as a delegation that did nothing (as
    # `server.py::_loop_ledger`).
    diagnostics: tuple[TurnDiagnostic, ...] = ()
    rereads: tuple[RereadAfterEviction, ...] = ()


def _assistant_blocks(cfg: Config, response: CanonicalResponse) -> tuple[ContentBlock, ...]:
    """The model's own turn, as it goes back into the history.

    Reasoning only when `resend_reasoning` says so (its description has why). Tool calls
    always, since some backends reject a history missing a `tool_use`.
    """
    if cfg.resend_reasoning:
        return response.content
    return tuple(b for b in response.content if not isinstance(b, ThinkingBlock))


# Times each tool call; looked up per call so a test can swap in the server's fake clock.
_tool_clock: Callable[[], float] = time.monotonic


def _run_one_call(  # noqa: PLR0913 -- the sixth is the per-delegation read record
    cfg: Config,
    call: ToolUseBlock,
    allowed: frozenset[str],
    cached: dict[tuple[str, str], _CachedResult],
    policy: BashPolicy,
    *,
    seen: SeenFiles | None = None,
) -> tuple[ToolResultBlock, str, int | None]:
    """Execute one tool call, or serve it from what an identical earlier one returned.

    Dedup is byte-identical, for `cacheable` tools only (`RegisteredTool`), with `read_file`
    additionally covered by range: a call whose lines an earlier read of the same path
    already holds is served, sliced, as a repeat. Any other call *clears* the cache: a
    write invalidates earlier reads, and serving a file from before its overwrite is worse
    than reading again. The third value is the call's milliseconds, `None` from cache.
    """
    key = (call.name, _dedup_key(call))
    entry = cached.get(key)
    if entry is not None:
        # "repeat" either way, since nothing ran; only the body differs.
        return ToolResultBlock(
            tool_use_id=call.id, content=_repeat_body(call, entry, exact=True)
        ), "repeat", None

    covering = _covering_entry(cached, call)
    if covering is not None:
        # A narrower read of lines an earlier read already returned. Nothing ran. A
        # prefetched entry answers with the pointer first, then the content.
        cover_key, cover_entry = covering
        return ToolResultBlock(
            tool_use_id=call.id,
            content=_repeat_body(call, cover_entry, exact=False, cached=cached, key=cover_key),
        ), "repeat", None

    started = _tool_clock()
    result = execute_tool(cfg, call, allowed, policy, seen)
    ms = int((_tool_clock() - started) * 1000)
    tool = REGISTRY.get(call.name)
    if tool is None or not tool.cacheable:
        # Unknown or side-effecting: any entry may be stale, and none can be singled out.
        cached.clear()
    elif not result.is_error:
        # Errors are not cached: many are transient (a file not yet written), and caching
        # would make them permanent.
        path, span, reached_eof = _read_coverage(call, result.content)
        cached[key] = _CachedResult(
            result.content, call.id, path=path, span=span, reached_eof=reached_eof
        )
    return result, "error" if result.is_error else "ran", ms


# A turn's calls in flight together. A constant: the model sets batch size (two or three,
# measured), and the cap only stops a pathological batch opening a thread per call.
MAX_CONCURRENT_TOOL_CALLS = 8


def _pooled_groups(calls: tuple[ToolUseBlock, ...]) -> Iterator[tuple[ToolUseBlock, ...]]:
    """One turn's calls, split into groups that may run together, in the order given.

    A group is a run of consecutive *cacheable* calls, which is what makes overlap safe: a
    non-cacheable call clears the cache, a barrier, so it runs alone where the model put
    it. `read_git` is not cacheable, since `run_bash` may just have committed; keeping it
    out costs nothing, the costly batches being `search_files`. Unknown tools run alone.
    """
    group: list[ToolUseBlock] = []
    for call in calls:
        tool = REGISTRY.get(call.name)
        if tool is not None and tool.cacheable:
            group.append(call)
            continue
        if group:
            yield tuple(group)
            group = []
        yield (call,)
    if group:
        yield tuple(group)


def _run_group(  # noqa: PLR0913 -- the sixth is the per-delegation read record
    cfg: Config,
    group: tuple[ToolUseBlock, ...],
    allowed: frozenset[str],
    cached: dict[tuple[str, str], _CachedResult],
    policy: BashPolicy,
    *,
    seen: SeenFiles | None = None,
) -> list[tuple[ContentBlock, str, int | None]]:
    """One group's calls, overlapped, with `cached` read and written only on this thread.

    Not a lock, which would let two identical calls both miss and run: the read-then-write
    stays off the workers, so a duplicate runs once and its twin is assembled from the
    store. All cacheable, so none clears the cache and `_run_one_call`'s ordering cannot be
    observed to change. Each call is timed on its own thread, so none is charged the
    group's span; a cached one carries `None`.
    """
    keys = [(c.name, _dedup_key(c)) for c in group]
    first_for: dict[tuple[str, str], ToolUseBlock] = {}
    for call, key in zip(group, keys, strict=True):
        if key in cached:
            continue
        if _covering_entry(cached, call) is not None:
            # An earlier read of the same path already holds these lines; the cache answers
            # and the call must not run.
            continue
        first_for.setdefault(key, call)

    fetched: dict[tuple[str, str], tuple[ContentBlock, int]] = {}
    pending = list(first_for.items())
    if len(pending) == 1:
        # One miss needs no pool, so the common batch of a call and a repeat does not pay
        # for a thread it would not use.
        key, call = pending[0]
        started = _tool_clock()
        fetched[key] = (
            execute_tool(cfg, call, allowed, policy, seen),
            int((_tool_clock() - started) * 1000),
        )
    elif pending:
        def run(item: tuple[tuple[str, str], ToolUseBlock]) -> tuple[ContentBlock, int]:
            started = _tool_clock()
            block = execute_tool(cfg, item[1], allowed, policy, seen)
            return block, int((_tool_clock() - started) * 1000)

        workers = min(len(pending), MAX_CONCURRENT_TOOL_CALLS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # `map`: blocks are consumed by position, so completion order means nothing.
            fetched = dict(zip((k for k, _ in pending), pool.map(run, pending), strict=True))

    out: list[tuple[ContentBlock, str, int | None]] = []
    for call, key in zip(group, keys, strict=True):
        entry = cached.get(key)
        if entry is not None:
            out.append((
                ToolResultBlock(
                    tool_use_id=call.id, content=_repeat_body(call, entry, exact=True)
                ),
                "repeat", None,
            ))
            continue
        covering = _covering_entry(cached, call)
        if covering is not None:
            # Not byte-identical, but a read of the same path whose span already holds the
            # requested lines -- from an earlier turn, a sibling this loop just wrote, or a
            # prefetched entry. A prefetched one answers with the pointer first, then the
            # content.
            cover_key, cover_entry = covering
            out.append((
                ToolResultBlock(
                    tool_use_id=call.id,
                    content=_repeat_body(
                        call, cover_entry, exact=False, cached=cached, key=cover_key
                    ),
                ),
                "repeat", None,
            ))
            continue
        result, ms = fetched[key]
        if isinstance(result, ToolResultBlock) and not result.is_error:
            path, span, reached_eof = _read_coverage(call, result.content)
            cached[key] = _CachedResult(
                result.content, call.id, path=path, span=span, reached_eof=reached_eof
            )
        is_error = isinstance(result, ToolResultBlock) and result.is_error
        out.append((result, "error" if is_error else "ran", ms))
    return out


def _run_calls(  # noqa: PLR0913 -- one turn's inputs; the sixth is the sandbox policy
    cfg: Config,
    calls: tuple[ToolUseBlock, ...],
    allowed: frozenset[str],
    cached: dict[tuple[str, str], _CachedResult],
    watch: _Watch,
    *,
    policy: BashPolicy,
    seen: SeenFiles | None = None,
    status: list[str] | None = None,
) -> tuple[list[ContentBlock], tuple[ToolCallRecord, ...]]:
    """One turn's tool calls, run in order. Returns the result blocks and their records.

    The ledger belongs to the turn and records the path the model asked for, not the one
    resolved: what the model *believed* it wrote, against the disk, is the abort report's
    point. Built here, the only place holding both arguments and refusal text.

    `status` holds one word per call for the heartbeat to read: `queued`, `running` while
    its group runs, then `done` or its outcome. By group, since a group's calls run at once.
    """
    results: list[ContentBlock] = []
    records: list[ToolCallRecord] = []
    first = 0
    for group in _pooled_groups(calls):
        if status is not None:
            status[first:first + len(group)] = ["running"] * len(group)
        # A group of one keeps the original path, cache clear and all.
        outcomes = (
            [_run_one_call(cfg, group[0], allowed, cached, policy, seen=seen)]
            if len(group) == 1
            else _run_group(cfg, group, allowed, cached, policy, seen=seen)
        )
        # On one thread, in the model's order; a worker appending would reorder the report.
        for i, (call, (block, outcome, ms)) in enumerate(zip(group, outcomes, strict=True)):
            result = block if isinstance(block, ToolResultBlock) else None
            watch.called(call, outcome, result)
            records.append(tool_call_record(call, outcome, result, ms=ms))
            results.append(block)
            if status is not None:
                status[first + i] = "done" if outcome in ("ran", "repeat") else outcome
        first += len(group)
    return results, tuple(records)


async def _reread_concurrency(
    concurrency_now: Callable[[], Awaitable[int]],
    current: int,
    decode_rate: DecodeRate,
    rate_history: RateHistory | None,
) -> tuple[int, DecodeRate]:
    """The concurrency this turn meets, and the rate to price it from.

    A failed read keeps the last figure. Until a turn of its own, the rate is re-seeded at
    the new figure, or the priced row pairs one concurrency with another's rate.
    """
    before = current
    try:
        current = await concurrency_now()
    except Exception:  # a monitoring read must never fail a delegation
        return before, decode_rate
    if current == before or rate_history is None or decode_rate.source == DecodeRate.OWN_TURNS:
        return current, decode_rate
    remembered = rate_history.expect(current)
    if remembered is None:
        return current, decode_rate
    return current, DecodeRate(remembered, float(current), source="observed_at_concurrency")


async def run_agentic_loop(  # noqa: PLR0912, PLR0913, PLR0915 -- three of the nine are test
    # seams; the statements are the turn lifecycle, and splitting would reorder it.
    cfg: Config,
    entry: ModelEntry,
    backend: Backend,
    delegation: Delegation,
    *,
    allowed: frozenset[str],
    effort: str | None = None,
    max_tokens: int | None = None,
    max_turns: int | None = None,
    policy: BashPolicy | None = None,
    prefetched: tuple[FileEntry, ...] = (),
    diagnostics: bool = False,
    report_progress: Callable[[int, int], Awaitable[None]] = _no_progress,
    on_alive: AliveCallback | None = None,
    on_priced: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_tools: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_pool: Callable[[int | None], None] | None = None,
    rate_history: RateHistory | None = None,
    expected_concurrency: int = 1,
    concurrency_now: Callable[[], Awaitable[int]] | None = None,
    on_turn_done: Callable[[TurnDiagnostic, str, float], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_token: Callable[[], None] | None = None,
    on_partial: Callable[[int, str, str], None] | None = None,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    ask: Callable[[list[str]], Awaitable[str]] | None = None,
) -> AgenticDispatch:
    """Turns, until the model answers or the budget runs out.

    A turn is one reply plus the tools it called. The loop ends on a reply with no tool
    calls (the answer) or at the last turn, where tools are forbidden so the model cannot
    end on a call nobody runs. `hit_turn_limit` is `turn == turns` and nothing else:
    requiring a tool call on that reply would leave it false in exactly the case it
    reports, and true only when a backend ignores the withdrawal. A run that would have
    stopped there anyway costs a reader one look at `max_turns`; the other way costs a
    truncated answer read as complete. Empty-answer recovery is `dispatch_with_recovery`'s.

    **One deadline covers the whole delegation**, or the bound would be the timeout times
    `max_turns`. `report_progress` and `on_alive` reset the client's idle timer
    (ADR-0018), injected since `loop.py` imports no MCP and tests need no client. Effort
    reported is the last turn's; a step-down does not persist, so one bad turn cannot
    downgrade the rest.
    """
    resolved_effort = resolve_effort(cfg, entry, effort)
    turns = resolve_max_turns(cfg, max_turns, allowed)
    specs = declared_tools(cfg, allowed)
    if ask is not None:
        # Appended, never inserted, so the cached prefix of the other tools survives. Only
        # when someone can answer; a model told it may ask when nobody is there spends a
        # turn finding that out (test_ask_caller).
        specs = (*specs, ASK_CALLER_SPEC)
    deadline = clock() + cfg.dispatch_timeout
    chunks = 0
    reasoning_chunks = 0
    last_progress: float  # bound below the seed, where the stall clock starts

    def stall_left() -> float:
        return cfg.stall_timeout - (clock() - last_progress)

    # Read when a piece is handed on, so each one is filed under the turn that wrote it.
    partial = PartialText(
        cfg.partial_every_seconds,
        None if on_partial is None else lambda r, a: on_partial(turn, r, a), clock)

    def token_arrived(kind: str = "answer", piece: str = "") -> None:
        """The finer progress signal (ADR-0072): turn completion cannot see inside a long
        turn, and unlike the notification and keepalive this is real. `kind` lets thinking
        be tallied apart, `piece` is shown; the defaults keep a bare `on_token()` working.
        """
        nonlocal last_progress, chunks, reasoning_chunks
        last_progress = clock()
        chunks += 1
        if kind == "reasoning":
            reasoning_chunks += 1
        partial.add(kind, piece)
        if on_token is not None:
            on_token()

    def streamed() -> tuple[int, int, float | None]:
        """What has arrived this delegation, and how long since. Frames, not tokens (only
        the usage frame counts those); never reset per turn, or it would read as stopped.
        """
        return chunks, reasoning_chunks, None if not chunks else clock() - last_progress

    bash_policy = policy or BashPolicy()

    history: list[Message] = [Message("user", (TextBlock(delegation.render()),))]

    cached: dict[tuple[str, str], _CachedResult] = {}
    # What the model last saw, for `edit_file`'s changed-file refusal. Seeded from the
    # prefetch: those files were read before the first turn, so an edit of one is judged
    # against the bytes the model was actually shown, not against nothing.
    seen = SeenFiles()
    for pre in prefetched:
        seen.record(pre.path, pre.sha256)
    # The read cache is seeded from the prefetch too, so a read of lines the files block
    # already holds is answered from it rather than run -- the measured re-read waste. A
    # write still clears the cache, seeded entries included, so a read after one is fresh.
    for pre in prefetched:
        cached[_prefetch_key(pre)] = _prefetched_cache_entry(pre)
    watch = _Watch(diagnostics=diagnostics)
    guard = _OverflowGuard(cfg, entry)
    dispatch: Dispatch | None = None
    text_before_nudge = ""
    nudge_pending = False
    # Asked at most once per run, every question in that one call: a model that keeps asking
    # is told to proceed on its best reading.
    asked = False
    turn = 0

    async def turn_done(text: str, backend_seconds: float) -> None:
        """One finished turn, for anything watching this delegation as it runs.

        Called at both ends a turn can have (after tools, and at the break, which carries
        the answer). The text is a parameter since `dispatch` is rebound each iteration.
        """
        nonlocal last_progress
        # Outside the callback branch, or a delegation nobody observes would stall out.
        last_progress = clock()
        if on_turn_done is not None and watch.turns:
            await on_turn_done(watch.turns[-1], text, backend_seconds)

    # One scrape before the first turn, never again (ADR-0055); before the stall clock
    # starts, so no turn is charged for it.
    decode_rate = await seed_decode_rate(
        backend, rate_history, expected_concurrency, on_pool=on_pool,
        label_trusted=cfg.admission_idle_hold > 0,
        fallback=cfg.rate_fallback_tok_s,
    )

    # When a turn last *finished* (ADR-0047): the notification fires on entry to a turn
    # that may then wedge, and the keepalive's timer is not progress. Set after the seed, so
    # a slow `/metrics` does not spend turn 1's silence budget.
    last_progress = clock()

    # Progress is reported only at a turn's top, so one long turn is silent. Past the
    # client's idle timeout the client abandons the call, nothing reaches the server, and
    # the slot is held to the end. `None` rather than an early return, which would need two
    # copies of the turn lifecycle (ADR-0018).
    tools_running = False
    running_calls: RunningCalls = ()
    call_status: list[str] = []
    beat = asyncio.create_task(_keepalive(
        cfg, on_alive, clock,
        # Names the deadline that will end the run: usually the stall (ADR-0099), but while
        # tools run nothing reads the stall clock, so only the delegation deadline.
        lambda: (max(deadline - clock(), 0.0)
                 if tools_running
                 else max(min(stall_left(), deadline - clock()), 0.0)),
        streamed,
        lambda: tuple(
            {**c, "status": s} for c, s in zip(running_calls, call_status, strict=False)
        ),
    )) if on_alive else None
    current = expected_concurrency
    try:
        while turn < turns:
            turn += 1
            watch.turn = turn
            partial.reset()
            await report_progress(turn, turns)
            final = turn == turns
            if turn > 1 and concurrency_now is not None:
                current, decode_rate = await _reread_concurrency(
                    concurrency_now, current, decode_rate, rate_history
                )

            guard.begin_turn(turn=turn, turns=turns, ledger=watch.calls)
            before = tuple(history)
            trimmed, dropped = stub_oldest_tool_results(
                before, guard.evict_upto(_tool_result_sizes(cfg, before))
            )
            history = list(trimmed)
            watch.evicted(before, trimmed, dropped)
            # Or a repeat would hand an evicted result straight back into the room just made
            # (ADR-0080). The ledger reads the same diff, so they cannot disagree.
            _mark_evicted_in_cache(cached, newly_evicted_ids(before, trimmed))

            def build(
                level: str, budget: int, *, _msgs: tuple[Message, ...] = tuple(history),
                _final: bool = final,
            ) -> CanonicalRequest:
                return CanonicalRequest(
                    system=SYSTEM_PROMPT_AGENTIC,
                    messages=_msgs,
                    max_tokens=budget,
                    effort=level,
                    temperature=cfg.temperature,
                    top_p=cfg.top_p,
                    # Forbidden, not withdrawn, on the final turn (ADR-0057): dropping the
                    # tool block changes the prompt's front and re-prefills all of it.
                    tools=specs,
                    tool_choice="none" if _final else "auto",
                )

            backend_started = clock()
            # Per turn: the clock left and the rate other tenants leave both move, so a
            # ceiling fixed at entry would be a guess.
            ceiling = decode_rate.ceiling(cfg, budget_seconds(
                cfg, dispatch_left=deadline - clock()
            ))
            # Priced before the call, since a turn killed at a deadline writes no `turn`
            # event; resolved once and passed down so the row and the send agree.
            asked_budget = resolve_max_tokens(
                cfg, entry, resolved_effort, max_tokens, ceiling=ceiling
            )
            if on_priced is not None:
                await on_priced({
                    "turn": turn, "effort": resolved_effort,
                    "max_tokens": max_tokens, "max_tokens_sent": asked_budget,
                    "budget_ceiling": ceiling,
                    "decode_rate": decode_rate.rate,
                    "rate_source": decode_rate.source,
                    "expected_concurrency": current,
                    "requests_running": current if turn > 1 else decode_rate.seen_running,
                    "temperature": cfg.temperature, "top_p": cfg.top_p,
                    "of_turns": turns,
                })
            dispatch = await dispatch_with_recovery(
                cfg, entry, backend, build,
                effort=resolved_effort, max_tokens=max_tokens,
                budget_ceiling=ceiling, asked_budget=asked_budget,
                sleep=sleep, deadline=deadline, stall_left=stall_left,
                on_token=token_arrived, tick_sleep=tick_sleep, clock=clock,
            )
            # The backend call alone: over the whole turn, tools and retry waits would make
            # the cluster look slower, a wrong number that gets believed.
            backend_seconds = max(clock() - backend_started, 0.0)
            # The token count comes from the answering attempt (ADR-0014), so it divides by
            # that attempt, not `backend_seconds`, whose halved rate would seed the next
            # delegation's first turn. Prefer the tokens' own interval: `answered_seconds`
            # includes prefill, so a short answer on a large prompt would price the queue
            # (ADR-0070). `None` means the adapter does not stream.
            measured = dispatch.response.decode_seconds
            interval = measured or dispatch.answered_seconds or backend_seconds
            decode_rate.observe(dispatch.response.output_tokens, interval)
            # Remembered only while `RateSampler` is off: both on would file several
            # readings per moment and make buckets incomparable, and a memory with no feeder
            # is the cold start that costs every first turn.
            if rate_history is not None and cfg.rate_sample_seconds <= 0:
                # Tokens and interval, not the quotient, so its stricter floor can judge.
                rate_history.observe(
                    dispatch.response.output_tokens,
                    interval,
                    concurrency=current,
                )
            watch.turn_cost(dispatch, evicted=dropped)
            guard.observe(
                dispatch.response, evicted_this_turn=dropped, turn=turn, turns=turns,
                ledger=watch.calls,
            )

            calls = dispatch.response.tool_uses
            if final or not calls:
                await turn_done(dispatch.response.text, backend_seconds)
                break

            assistant = Message("assistant", _assistant_blocks(cfg, dispatch.response))
            history.append(assistant)
            guard.added(assistant)

            # `ask_caller` is answered by whoever gave the delegation, so it is awaited on the
            # event loop rather than run in the worker thread: take those calls out of the
            # batch and put them back in the model's order after.
            ask_here: list[tuple[int, ToolUseBlock]] = []
            batch: list[tuple[int, ToolUseBlock]] = []
            for i, c in enumerate(calls):
                if ask is not None and c.name == "ask_caller":
                    ask_here.append((i, c))
                else:
                    batch.append((i, c))

            # Off the event loop: `run_bash` blocks in `subprocess.run`, which on the loop
            # would stop the heartbeat, `report_progress` and every other delegation's
            # stdio, and a heartbeat quiet during the longest block cannot fail. One thread
            # for the batch, keeping the model's order; the timer beside it touches neither
            # `cached` nor `watch`.
            tools_running = True
            # For the stream and the heartbeat, since the turn record lands only once these
            # finish; the worker thread moves each call's status along.
            running_calls = tuple(
                {"name": c.name, "arguments": dict(record_arguments(c))} for c in calls
            )
            call_status = ["queued"] * len(calls)
            # The heartbeat reads `call_status` while the thread writes it, so a turn with
            # no question hands the thread that very list. One that also asks writes the
            # batch's own and copies it over after: its other calls read as queued until
            # they finish, which only a turn mixing the two can show.
            batch_status = call_status if not ask_here else ["queued"] * len(batch)
            if on_tools is not None:
                await on_tools({"turn": turn, "of_turns": turns,
                                "tool_calls": [dict(c) for c in running_calls]})
            batch_results, batch_records = await asyncio.to_thread(
                _run_calls, cfg, tuple(c for _, c in batch), allowed, cached, watch,
                policy=bash_policy, seen=seen, status=batch_status,
            )
            if ask_here:
                for (idx, _), word in zip(batch, batch_status, strict=True):
                    call_status[idx] = word

            # Both halves are indexed by the model's call order, so the results and the
            # records they became line up with the `tool_use` blocks they answer.
            by_index: dict[int, tuple[ContentBlock, ToolCallRecord]] = {
                idx: (block, record)
                for (idx, _), block, record in zip(
                    batch, batch_results, batch_records, strict=True
                )
            }

            for idx, call in ask_here:
                assert ask is not None  # `ask_here` is only filled when there is someone to ask
                call_status[idx] = "running"
                questions = call.input.get("questions")
                if not (isinstance(questions, list) and questions
                        and all(isinstance(q, str) for q in questions)):
                    result = ToolResultBlock(
                        tool_use_id=call.id,
                        content="ask_caller: `questions` must be a non-empty list of "
                                "strings.",
                        is_error=True,
                    )
                    outcome = "error"
                elif asked:
                    result = ToolResultBlock(tool_use_id=call.id, content=ASKED_ALREADY)
                    outcome = "ran"
                else:
                    # Waiting on the caller is not the delegation's work, so neither the
                    # dispatch deadline nor the stall clock counts it: push the deadline
                    # forward by the wait and treat the answer as fresh progress.
                    # `tools_running` keeps the heartbeat on the deadline rather than the
                    # stall clock while it is awaited.
                    started = clock()
                    tools_running = True
                    try:
                        answer = await ask(questions)
                    finally:
                        waited = clock() - started
                        deadline += waited
                        last_progress = clock()
                        tools_running = False
                    asked = True
                    result = ToolResultBlock(tool_use_id=call.id, content=answer)
                    outcome = "ran"
                call_status[idx] = "done"
                # Filed like any other call, with the duration zeroed so a long wait cannot
                # inflate tool time; the wait is not this tool's work either.
                watch.called(call, outcome, result)
                by_index[idx] = (result, tool_call_record(call, outcome, result, ms=0))

            results = [by_index[i][0] for i in range(len(calls))]
            outcomes = tuple(by_index[i][1] for i in range(len(calls)))
            tools_running = False
            running_calls = ()
            watch.turn_tools(outcomes)
            await turn_done(dispatch.response.text, backend_seconds)
            results.append(TextBlock(countdown_line(turns - turn)))

            nudge_pending = guard.nudge_due(turn=turn)
            if nudge_pending:
                # Beside the countdown, never instead (see `WRAP_UP_LINE`).
                results.append(TextBlock(WRAP_UP_LINE))
                text_before_nudge = dispatch.response.text

            user_message = Message("user", tuple(results))
            history.append(user_message)
            guard.added(user_message)

        if dispatch is None:  # unreachable: turns >= 1, so the body ran at least once
            raise InvalidDelegation("max_turns resolved to zero turns.")
        return AgenticDispatch(
            response=_preserve_across_nudge(
                dispatch.response, said_before=text_before_nudge if nudge_pending else ""
            ),
            effort=dispatch.effort,
            attempts=watch.attempts,
            reasoning_exhausted=dispatch.reasoning_exhausted,
            turns=turn,
            tool_calls=watch.tool_calls,
            tool_calls_by_name=tuple(sorted(watch.by_tool.items())),
            tool_errors=watch.tool_errors,
            deduped=watch.deduped,
            evicted=watch.evictions,
            total_input_tokens=watch.total_input_tokens,
            total_output_tokens=watch.total_output_tokens,
            total_cached_tokens=watch.total_cached_tokens,
            hit_turn_limit=turn == turns,
            max_turns=turns,
            bash_calls=watch.bash_calls,
            bash_failures=watch.bash_failures,
            bash_masked_failures=watch.bash_masked_failures,
            failed_calls=watch.failed_calls,
            last_bash_exit=watch.last_bash_exit,
            overflow_tightened_at=guard.tightened_at,
            overflow_nudged_at=guard.nudged_at,
            diagnostics=tuple(watch.turns),
            rereads=tuple(watch.rereads) if diagnostics else (),
        )
    except DispatchTimedOut as e:
        # The only place these numbers survive a failure. Without them a timeout looks like
        # a dead endpoint, and treating a stall as one leads to the wrong response (stop
        # using the server). One handler here, not five raise sites that cannot read the
        # counters. `watch.turn` is the running turn, so one fewer completed; the total
        # separates a task too large for a turn from a backend that produced nothing.
        raise e.with_progress(
            turns=max(watch.turn - 1, 0),
            tool_calls=watch.tool_calls,
            last_tool=watch.calls[-1][0] if watch.calls else None,
        ) from e
    finally:
        if beat is not None:
            beat.cancel()
            try:
                await beat
            except asyncio.CancelledError:
                # Ours, as in `run_one_shot`; re-raising would hide what ended it.
                pass
