"""The delegation itself: build a request, send it, hand back what came back.

Three layers that compose, built in that order. At the bottom, a dispatch that survives a
dropped route, a refusal the endpoint calls temporary, and a `Retry-After` it asks us to
honour. Above it, recovery from a reply that arrived intact and empty because reasoning
consumed the whole budget (ADR-0014): retry once at a larger budget, then step effort down,
then say plainly that everything was tried. Above that, the turn loop -- turns, tool calls,
history eviction -- and the context economics that watch what the loop is spending.

This is the only place in the project that *interprets* what a backend handed back, which
is why the adapter below goes on passing `finish_reason`, the token counts and the raw
`Retry-After` string through untouched. One layer decides; the other translates.

This module owns resolution -- which model, which effort, which budget -- because that
has to happen exactly once and be sent explicitly, and assembly: the order of the parts
of the prompt, which is load-bearing for the cluster's prefix cache and so is decided
here rather than wherever a part happens to be produced.
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
from .paths import repo_status
from .registry import ModelEntry
from .tools import REGISTRY, BashPolicy, declared_tools, execute_tool

# One level down, DERIVED from the vocabulary rather than written out again. A hand-written
# table is a second copy of EFFORT_LEVELS that stops agreeing with it the moment a level is
# added -- and it would fail silently, by stepping down to a level that no longer exists or
# by skipping one that does. Same reasoning as BYTES_PER_TOKEN_DEFAULT in config.py.
#
# Gives max -> high -> low -> off, and nothing below off, which is the point: there is no
# level beneath "do not reason", so an empty answer there is not reasoning exhaustion.
_STEP_DOWN = dict(zip(EFFORT_LEVELS[1:], EFFORT_LEVELS[:-1], strict=True))

# Statuses worth sending again. A module constant and deliberately not a config field:
# which HTTP codes mean "temporary" is a fact about the protocol, not a preference, and
# the same reasoning keeps SERVER_EFFORT_VALUES in the adapter rather than in config.py.
#
# The set is exact, not "any refusal". base.py says a refusal is *usually* not worth
# retrying, and the carve-out is the whole content of that word: 429 is the endpoint
# saying later, and 500/502/503/504 are it or something in front of it failing in a way
# that may not repeat. Everything else -- 400, 401, 403, 404 -- describes the request,
# and sending the same request again cannot change the answer.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# The system prompt is a constant, and must stay one: the cluster caches prefixes, and a
# single dynamic byte -- a timestamp, a session id, a turn counter -- silently disables
# that with no error and no symptom beyond slower prefill. Dynamic content goes in the
# tail, inside the message. One constant covers both the files and no-files shapes, so a
# caller that alternates between them still hits the cache. Why: ADR-0011.
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


# The second static prompt, and deliberately not a variant of the first: the one-shot
# prompt tells the model it has no tools and no second turn, which would be a lie here.
# The two shapes are never alternated by one caller, so they cost nothing as separate
# prefixes. Static, byte for byte, as ADR-0011 requires -- no turn number, no counter, no
# budget -- and the countdown lives in the tail, on the message carrying tool results.
# Why: ADR-0011.
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
    """The delegation was stopped because its context was full, or had silently truncated.

    Carries a state report rather than only a message. An abort here lands in the middle of
    work: files may already have been written, and the caller's next question is always
    "what did it actually do to my tree?" The model's own summary is the one answer that
    cannot be trusted for it, so the report puts the server's ledger of tool calls beside
    `git status` and lets the reader see where the two disagree (ADR-0007).

    A subclass of `InvalidDelegation` so `server.py`'s existing branch catches it without a
    second except clause -- but it carries `report`, and `delegate()` returns that rather
    than only the message.
    """

    def __init__(self, message: str, *, report: dict[str, object]) -> None:
        super().__init__(message)
        self.report = report


class DispatchTimedOut(Exception):
    """The whole delegation outlived `dispatch_timeout`.

    Distinct from every backend failure on purpose. `BackendUnavailable` says the endpoint
    did not answer; this says the endpoint may well be answering and the delegation has
    still taken longer than the operator allows. Sending a caller to check the cluster on
    a deadline they set themselves would be the wrong diagnosis, and ADR-0007's argument
    for server-captured truth applies to *which* failure this was as much as to exit codes.
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
        # `stage` carries its own preposition, so it reads correctly for the three
        # waiting stages and does not produce "while with no turn completed" for the
        # stall -- the one message a reader is most likely to be staring at.
        self.stage = stage
        self.setting = setting
        # The remedy is a parameter because it stopped being one sentence. "Raise that
        # setting" is right for a ceiling reached by work that was still producing, and
        # wrong for a stall: raising a no-progress deadline buys a longer wait for a run
        # that has already stopped progressing, which is the opposite of the fix.
        self.remedy = remedy
        # What the delegation had managed when the deadline fired. `None` at every raise
        # site, because not one of them can see it: the counters live on the turn loop's
        # `_Watch`, a local of `run_agentic_loop`, and `AgenticDispatch` -- which does
        # carry them -- is built only on the success path. So the loop fills them in on the
        # way out, via `with_progress`.
        self.turns = turns
        self.tool_calls = tool_calls
        self.last_tool = last_tool
        # What the abandoned turn had already decoded, when it had decoded anything
        # (ADR-0078). Deliberately not a constructor argument and deliberately absent from
        # the message: every raise site here is a deadline, none of them can see the
        # backend's accumulator, and the caller that can attaches it afterwards. Keeping
        # it out of the message is what lets it be assigned late without `str(e)` and the
        # fields beside it drifting apart -- the thing `with_progress` copies rather than
        # mutates precisely to avoid.
        self.partial: Any = None
        super().__init__(
            f"Delegation abandoned after {elapsed:.1f}s, past the "
            f"{setting} of {limit}s, {stage}.{self._progress()} {remedy}"
        )

    def _progress(self) -> str:
        """What it had done, or nothing at all when the counters were never supplied.

        Empty rather than "0 turns" when unknown, because absent and zero are different
        facts and this message must not merge them: a one-shot completes no turns by
        construction, so a zero there would describe the one path that cannot stall as
        though it had stalled.
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
        """A copy that also reports progress, for the turn loop to raise in place of this.

        A copy rather than assigning the attributes, because the message is built in
        `__init__`: setting them afterwards would leave `str(e)` disagreeing with the
        fields sitting beside it, which is the drift between a report and the thing it
        reports that ADR-0007 exists to refuse.
        """
        copy = DispatchTimedOut(
            self.elapsed, self.limit, self.stage, self.setting, self.remedy,
            turns=turns, tool_calls=tool_calls, last_tool=last_tool,
        )
        # Carried across explicitly. A copy that silently dropped it would lose the
        # partial at the one seam every timed-out agentic delegation passes through.
        copy.partial = self.partial
        return copy


@dataclass(frozen=True, slots=True)
class Delegation:
    """What to delegate: the task, the material assembled for it, and the agent's own prompt.

    A value object rather than three parameters, because these are the parts of one prompt
    and the order they go in is load-bearing (ADR-0011). `render` is what keeps that rule in
    one place. It has to be a method rather than a convention, because two call sites that
    must agree and are not made to are a drift waiting to happen: the one-shot builder and
    the turn loop each concatenate the parts themselves, and the agent body would be a third
    segment to add to both.
    """

    task: str
    files_block: str = ""
    agent_body: str = ""

    def render(self) -> str:
        """The user message: agent body, then files, then task. Never the system prompt.

        The system prompt is a byte-for-byte constant the cluster caches, so nothing that
        varies per delegation may enter it -- the agent body included. Task last, because
        it varies most between calls. Why: ADR-0011.
        """
        if not self.task or not self.task.strip():
            raise InvalidDelegation("task is empty. There is nothing to delegate.")
        parts = (self.agent_body, self.files_block, self.task)
        return "\n\n".join(part for part in parts if part)


def resolve_effort(cfg: Config, entry: ModelEntry, explicit: str | None = None) -> str:
    """Explicit argument, then the registry row, then the global default.

    Never falls through to whatever the cluster was booted with: that default is set
    elsewhere by someone else and is not ours to assume. ADR-0013.
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


# Below these an observation is arithmetic on noise rather than a measurement. The test is
# the same for both estimators because since ADR-0070 both divide by `decode_seconds`,
# last token minus first, with prefill and queueing outside it. Sized from the
# deployment's own decode benchmark (a 400-token cap), so a few hundred tokens is enough
# to describe the decoder. Why: ADR-0071.
MIN_MEASURABLE_TOKENS = 512
MIN_MEASURABLE_SECONDS = 1.0


class DecodeRate:
    """Tokens per second this deployment actually decodes, kept across turns.

    The reply budget is denominated in seconds before it is compared with a deadline, so
    the rate is measured and never configured (ADR-0055). Two sources, in this order: the
    cluster's own since-boot mean read once at dispatch, then this delegation's own turns,
    which replace the seed as soon as there is one. An exponential average rather than the
    last value, so one anomalous turn cannot halve the next turn's budget, and an
    implausible observation is refused outright rather than smoothed. Why: ADR-0055.
    """

    # One test for both estimators, because since ADR-0070 they divide by the same thing.
    MIN_TOKENS = MIN_MEASURABLE_TOKENS
    MIN_SECONDS = MIN_MEASURABLE_SECONDS
    # New evidence is worth more than old, but not so much more that one turn is the
    # estimate. Five turns move it roughly 90% of the way to a changed rate.
    WEIGHT = 0.4

    # `seen_running` is not used in any calculation. It is carried so the record can say
    # what load the seed was read against: the seed is a since-boot mean over every
    # concurrency regime the engine has served, so the rate alone cannot be checked
    # afterwards against what the cluster was actually doing.
    __slots__ = ("_rate", "seen_running", "source")

    def __init__(self, seed: float | None = None,
                 seen_running: float | None = None,
                 source: str = "unknown") -> None:
        self._rate = seed if seed and seed > 0 else None
        self.seen_running = seen_running
        # Which of the two answers this came from, carried only so the record can say.
        # A rate that cannot be traced to its source cannot be argued with afterwards.
        self.source = source

    @property
    def rate(self) -> float | None:
        """What a turn would currently be priced at, or None if nothing is known."""
        return self._rate

    @property
    def known(self) -> bool:
        return self._rate is not None

    # What `source` reads once a turn of this delegation's own has moved the number. The
    # seed's label describes where the *first* value came from and stops being true the
    # moment `observe` takes a sample, which is the second turn of almost every delegation.
    OWN_TURNS = "own_turns"

    def observe(self, output_tokens: int, seconds: float) -> None:
        if output_tokens < self.MIN_TOKENS or seconds < self.MIN_SECONDS:
            return
        sample = output_tokens / seconds
        self._rate = (
            sample if self._rate is None
            else (1 - self.WEIGHT) * self._rate + self.WEIGHT * sample
        )
        # Relabelled here rather than at the call site, and only on a sample that was
        # actually taken: the number and the label have to move together or the record
        # claims a measurement the floors above just refused. `rate_source` is what a reader
        # uses to decide whether to trust the rate, and ARCHITECTURE.md reads a second fact
        # off it -- that `requests_running` is a real cluster figure only on a
        # `cluster_since_boot` row -- so a label outliving its number makes both wrong.
        self.source = self.OWN_TURNS

    def ceiling(self, cfg: Config, seconds_available: float) -> int | None:
        """The largest reply the clock can pay for, or None when nothing is known yet.

        `None` rather than a guess. A cap invented from no measurement is the constant
        this design exists to avoid, and a caller that cannot bound the budget should say
        so in the ledger rather than pretend to.
        """
        if self._rate is None or seconds_available <= 0:
            return None
        return max(
            cfg.reply_budget_floor,
            int(seconds_available * self._rate * cfg.reply_budget_margin),
        )


def budget_seconds(cfg: Config, *, dispatch_left: float) -> float:
    """Seconds the reply about to be asked for can actually be delivered in.

    One bound, the delegation deadline. `turn_timeout` is not one (ADR-0100), and
    `stall_left` is deliberately not a term either (ADR-0099): a reply being generated is
    not silence, so putting it here cuts every reply to the stall budget -- the opposite
    of what it measures. Never negative: a negative would multiply through `ceiling` into
    `reply_budget_floor` and read as a small budget rather than as no time remaining.
    """
    return max(dispatch_left, 0.0)


class RateHistory:
    """What this deployment has actually decoded, kept across delegations.

    `DecodeRate` learns within one delegation and dies with it, so every delegation's
    *first* turn is priced from the cluster's since-boot mean -- and the first turn is the
    one with no observation of its own and the only one that can die before making any.
    This is that memory, and it is a memory rather than a model on purpose: a curve
    fitted to rate-against-concurrency would be a constant baked to one deployment's
    hardware, which is the mistake PLAN.md already records against `kv_token_budget`.

    Keyed by how contended the turn was, because the rate is not one number. Asking for
    "the rate" without saying at what concurrency is asking a question with six answers.

    `expect` returns a bucket's **median**, and then the worst of those medians across the
    buckets at that concurrency or above. The pessimism therefore lives between buckets,
    where it is the sound part -- contention only slows a stream -- and not inside one,
    where it was a sampling artefact. A median ignores the slow outliers that drag a mean
    down and does not move with the sample count the way a minimum does. An under-priced
    rate is not free: the budget genuinely binds on the largest answers, so pricing it low
    truncates the biggest replies the server produces.
    """

    # Enough to outlast one fan-out and forget a cluster that has since been reconfigured.
    # Unbounded would be a slow leak in a process that runs for days, and would let one
    # ancient sample keep a vote in the estimate for the life of the server.
    #
    # Counted in *moments* since the sampler files one reading per scrape rather than one
    # per stream, so this is the same span of wall-clock memory at every fan-out width.
    # Per completed turn it was not: six streams file six samples on one moment, so a
    # six-wide bucket holds a sixth of the history a solo bucket does and the two are not
    # comparable numbers.
    DEFAULT_KEEP = 64

    # The same test `DecodeRate` applies, shared rather than restated so the two cannot
    # drift apart. Why one constant serves both is above the definition.
    MIN_TOKENS = MIN_MEASURABLE_TOKENS
    MIN_SECONDS = MIN_MEASURABLE_SECONDS

    # Bumped when the payload shape changes. A file written by an older server is
    # discarded rather than guessed at -- the same reasoning as the stamp.
    SCHEMA_VERSION = 1

    __slots__ = ("_keep", "_path", "_seen", "_stamp")

    def __init__(
        self,
        keep: int = DEFAULT_KEEP,
        *,
        path: Path | None = None,
        stamp: str = "",
    ) -> None:
        """`path` makes the memory outlive this process; without one it is unchanged.

        The file is durable, and `slots.rate_history_path` decides where. A stale rate
        self-corrects as samples follow, where a cold start costs every dispatch until the
        memory refills, so durability is the better of two wrong answers (ADR-0094).
        `stamp` still covers a model swap. Never a hard dependency: every failure below
        leaves an empty memory rather than raising. Why: ADR-0094.
        """
        self._keep = max(1, keep)
        # One bucket per concurrency, each capped on its own. A single shared deque evicted
        # by recency would spend its whole capacity on whichever regime was busiest *lately*,
        # walking out the reading that prices a quieter regime honestly. The cap moves here
        # rather than going: unbounded would be a slow leak in a process that runs for days,
        # and would let one ancient sample keep a vote in a bucket for the life of the server.
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
        """One bucket's held samples, oldest first. Empty when nothing was seen there.

        `expect` answers with a statistic, and a statistic cannot say how many moments it
        was taken over -- which is the quantity this whole mechanism is about, since a
        bucket filled one reading per stream spans a sixth of the wall clock a solo bucket
        spans. A reader that needs the count should not have to reach into `_seen` for it.
        """
        return tuple(self._seen.get(max(int(concurrency), 1)) or ())

    def _pairs(self) -> list[tuple[int, float]]:
        """Every sample held, flattened back to the shape the file stores."""
        return [(seen_at, rate) for seen_at, b in self._seen.items() for rate in b]

    def _read(self) -> list[tuple[int, float]]:
        """Whatever the file holds that is still trustworthy, and nothing else.

        Every field is shape-checked rather than trusted. The file sits on a tmpfs any
        process of this user can write, and a sample reaching `expect` keeps its vote for
        `DEFAULT_KEEP` observations -- so a malformed pair is skipped and the rest are
        kept, which is what a partial write during a reboot actually looks like.
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
            # `bool` is an `int` and `True` would become concurrency 1. Excluded rather
            # than tolerated: it can only arrive from a file nothing here wrote.
            if isinstance(seen_at, bool) or not isinstance(seen_at, int) or seen_at < 1:
                continue
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
                continue
            kept.append((seen_at, float(rate)))
        # Not truncated here. The caller files each pair into its own bucket, which is
        # where the cap lives -- trimming the tail globally would reinstate exactly
        # the cross-concurrency eviction the buckets exist to stop, at load time.
        return kept

    def _write(self) -> None:
        """Persist, merging with whatever another server process has written since.

        stdio gives every connected client a process of its own (ADR-0040), so two can
        share this file. Writing only what this process has seen would drop the other's
        samples, and a bucket priced at its median is only as good as how many moments it
        holds. Merged as a set, and that stays right: two byte-identical float rates at one
        concurrency are a sample this process read back from the file far more often than
        they are two separate measurements that happened to agree, so counting the copy
        would weight one moment twice on every write.

        Written to a sibling and renamed, so a reader never sees a half-written file.
        """
        assert self._path is not None
        merged = dict.fromkeys(self._read())
        merged.update(dict.fromkeys(self._pairs()))
        # Bounded per bucket, for the same reason the in-memory cap is: a global tail
        # would let the busiest regime's samples be written out by whichever one has been
        # noisiest, and the file is what makes the memory warm after a reconnect.
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
            # An unwritable directory costs the next process its head start and nothing
            # else. Raising here would turn an optimisation into an outage.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def observe(self, output_tokens: int, seconds: float, *, concurrency: int) -> None:
        """Record what one completed turn achieved, and how contended it was.

        Takes the turn's tokens and its decode interval rather than a finished rate,
        because a rate cannot be judged: the caller that divided first handed this a
        number with no way to tell a throughput measurement from a 237-token turn whose
        interval was mostly prefill and queueing. The guard lives here rather than at the
        call site so a second caller cannot bypass it by dividing on its own.
        """
        # Zero, negative and degenerate inputs are arithmetic accidents rather than
        # measurements, and one admitted here would price every later delegation against
        # it. They are refused by these two comparisons rather than by a check on the
        # quotient: with both floors positive the quotient cannot be, so a guard on it
        # would be a check that can never fire and would be trusted anyway.
        if output_tokens < self.MIN_TOKENS or seconds < self.MIN_SECONDS:
            return
        self._remember(max(int(concurrency), 1), output_tokens / seconds)
        # After the floor, never before it: persistence must not be a second way in for a
        # sample the admission test just refused.
        if self._path is not None:
            self._write()

    def observe_window(self, generated: int, seconds: float, *, concurrency: int) -> bool:
        """One scrape window of the cluster's own counter, filed as a single sample.

        Separate from `observe` because a window is not a turn and the two are judged
        differently. `observe`'s token floor refuses a turn too short to be a throughput
        measurement -- a handful of tokens over an interval that was mostly prefill -- and
        applying it here would silently throw away every solo window, which decodes well
        under that many tokens in a sampling interval. A window has no prefill charged to
        it: `RateSampler` refuses the window that lies inside one and counts how often it
        does. What is left to check is that the clock is long enough for the counter's
        granularity not to dominate, which is `MIN_SECONDS`.

        The aggregate is divided by the concurrency read in the *same* scrape, so the rate
        and its divisor describe one moment rather than two. Exactly one sample comes out,
        whatever the width -- that is what makes a full bucket the same span of wall-clock
        memory at six streams as at one.

        `generated < 1` is refused here as well as by the sampler, for the reason
        `observe`'s floors live here rather than at their call site: a second caller must
        not be able to get a zero-throughput sample in by dividing on its own. Returns
        whether the sample was kept, which is what the sampler counts.
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

        Busier observations answer quieter questions and not the reverse: contention only
        slows a stream, so a six-way measurement bounds a four-way one from below, while a
        solo measurement says nothing about six. Discarding the busier ones would throw
        away exactly the observations worth keeping.

        `trusted` says whether the label can be believed, and defaults to no. Untrusted, a
        question is answered from every sample at that concurrency *or busier* -- which is
        pessimistic on purpose and protects a burst's first member, the call that finds
        the gate empty, labels itself solo, and then decodes at six-way.

        Trusted, the bucket is preferred and the widening becomes the fallback it is sound
        as. Only `admission_idle_hold` can make it true, by waiting long enough for a
        burst to arrive before the label is recorded -- which is why that setting disables
        both halves at once and why this one cannot be turned on by itself (ADR-0085).

        A bucket answers with its **median**, and the widening then takes the worst of those
        medians rather than the worst sample anywhere in them. Pooling every sample from
        every busier bucket into one mean would be the obvious alternative and is wrong:
        it mixes regimes, so a quiet bucket's fast samples would pull a six-way answer up
        past anything six-way was ever measured at. Taking the minimum *of the medians*
        keeps the one direction that is sound -- a busier bucket bounds a quieter question
        from below -- while pricing each bucket at what that bucket actually did. A median
        is what the bucket actually did: a slow outlier that drags the mean down is a
        minute the cluster had, not the rate it decodes at, and the median ignores it.

        The asymmetry a within-bucket minimum would defend is real: over-estimating
        authorises a reply that cannot be decoded inside the delegation deadline and the turn
        dies with nothing, where under-estimating truncates and something comes back. It is
        paid for between buckets, above, and by `reply_budget_margin`. Paying it a third
        time inside the bucket buys no safety and costs the large answers -- see the class
        docstring for the measurement.
        """
        want = max(int(concurrency), 1)
        if trusted:
            at = self._seen.get(want)
            if at:
                return median(at)
        # One median per bucket, then the worst of them. An empty bucket cannot have a
        # median and is skipped rather than counted as zero -- a bucket is only ever empty
        # here if something created it without filing, and a zero would price the next
        # turn at no throughput at all.
        medians = [
            median(bucket) for seen_at, bucket in self._seen.items()
            if seen_at >= want and bucket
        ]
        return min(medians) if medians else None


class RateSampler:
    """Fills `RateHistory` from the cluster's counter on a ticker, not on turn completion.

    A completed turn is the wrong unit, and the reason is arithmetic rather than taste.
    Six streams each filing when they finish put six samples on one moment, so a bucket
    of `DEFAULT_KEEP` slots holds only a sixth as many moments at six-wide as at
    one-wide: the wider the fan-out, the shorter that bucket's memory in wall-clock
    terms, and the less two buckets' means have to do with each other. One sample per
    scrape makes every bucket span the same number of moments at every width, which is
    what makes `expect`'s comparison across buckets a comparison at all.

    Rate and concurrency come from the *same* reading. `_DecodeWindow` differences
    `generation_tokens_total` across two scrapes, so one scrape yields the aggregate
    tokens generated, the seconds they took and `requests_running` together -- and the
    per-stream rate is then a quotient of two numbers describing one moment, rather than
    a rate from one moment divided by a width observed at another.

    Never a dependency. Every failure below leaves the memory exactly as it was: this is
    a monitoring read, and a sampler that could fail a delegation would be trading a
    pricing improvement for an outage.
    """

    # How much longer than the interval a window may be before it is thrown away. The
    # ticker only scrapes while something is in flight, so the first scrape of a busy
    # period differences against the last scrape of the previous one -- which may be
    # hours old and spans an idle stretch the counter did not move through. That window
    # reports the burst's tokens over the idle time as well, which is a spuriously low
    # rate at a genuinely busy concurrency: exactly the sample this whole item exists to
    # stop being filed. Two intervals is loose enough that an ordinary tick that ran late
    # still counts.
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
        # Clamped rather than validated, the way `admission_idle_hold` is: a
        # non-positive interval means the operator has turned sampling off, and a
        # ticker that refused to start the server over it would be the worse failure.
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
        """File the one sample this scrape supports, if it supports one.

        Synchronous and pure apart from the history it writes, so the decision can be
        tested against a dict rather than against a cluster.
        """
        generated = scrape.get("decode_tokens_window") if scrape else None
        seconds = scrape.get("decode_window_seconds") if scrape else None
        running = scrape.get("requests_running") if scrape else None
        # A scrape with no window in it is the first one after a gap, which `_DecodeWindow`
        # correctly refuses to turn into a rate. Not counted as a drop: nothing was
        # measured, so there is no measurement to have thrown away.
        if not isinstance(generated, int) or isinstance(generated, bool):
            return False
        if not isinstance(seconds, (int, float)) or not isinstance(running, (int, float)):
            return False
        # Concurrency is the divisor, so a window the cluster spent idle has nothing to
        # divide by and nothing to say. Also not a drop, for the same reason.
        concurrency = int(running)
        if concurrency < 1:
            return False

        self._windows += 1
        # Only when an interval was configured: with sampling off there is no tick to
        # compare against, and `0 * anything` would make every window stale.
        if self.enabled and seconds > self._every * self.STALE_WINDOW_FACTOR:
            self._stale_windows += 1
            return False
        # The prefill guard. A window lying entirely inside a prefill differences to zero
        # generated tokens while `requests_running` is nonzero, and filing that would put
        # a rate of zero in a busy bucket. Not a rare shape: prefill generates nothing for
        # a long stretch on a large prompt. Counted rather than silently skipped -- a guard
        # whose firing rate nobody can see is a guard nobody can tell has started firing on
        # everything.
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
        """Scrape on the interval for as long as anything is decoding. Cancelled to stop.

        Idle ticks cost nothing and reach no endpoint: with nothing in flight there is no
        rate to sample, and scraping anyway would keep a connection warm against a cluster
        this process is not using.
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
        """What the sampler has seen and what it threw away, for `backend_status`.

        The drop counts are the point of reporting this at all. Both guards are silent by
        construction -- a dropped window leaves no trace in the memory it declined to
        write -- so without these an operator cannot tell a sampler that is working from
        one that is refusing every window.
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

    Never raises: this is one scrape of a monitoring surface, and a delegation must not
    refuse to start because `/metrics` was briefly unavailable. An endpoint that publishes
    nothing leaves the first turn uncapped. Why: ADR-0055.
    """
    # What this deployment has actually managed at this much contention beats what the
    # cluster averaged since boot, because the since-boot figure is a blend over every
    # concurrency regime the engine has served and the first turn is about to meet a
    # specific one. Consulted first, and only falls through when nothing has been seen
    # this busy -- the cold start, where no measurement exists.
    remembered = (
        history.expect(expected_concurrency, trusted=label_trusted)
        if history is not None else None
    )

    # `on_pool` is why this is not simply `if remembered is not None: return`. The scrape
    # below is the only place on the dispatch path that sees `kv_cache_size_tokens`, so
    # returning early would leave the pool unread and the token budget back on the
    # configured number. The caller passes `on_pool` only while it still wants the figure;
    # once known it passes None and this returns early, so the cost is one extra metrics
    # read per process rather than per delegation. Not a separate `pool_known` flag, which
    # an intermediate could forget to forward. Why: ADR-0081.
    if remembered is not None and on_pool is None:
        return DecodeRate(remembered, float(expected_concurrency),
                          source="observed_at_concurrency")
    try:
        cluster = await backend.probe_cluster()
    except Exception:  # a monitoring read must never fail a delegation
        # A remembered rate outlives a failed scrape. Falling back to `unknown` here would
        # let a momentary `/metrics` outage throw away a measurement already in hand, which
        # is a worse trade than the one this whole function refuses to make.
        if remembered is not None:
            return DecodeRate(remembered, float(expected_concurrency),
                              source="observed_at_concurrency")
        # Deliberately NOT the floor below. That floor answers "nothing was ever
        # measured at this concurrency"; this is "the scrape that would have told us
        # failed", and the established answer is no ceiling rather than a guessed one.
        return DecodeRate(source="unknown")
    # The same payload carries the size of the KV pool. Reporting it costs nothing -- this
    # scrape already happened to price the turn -- and it is the only place on the dispatch
    # path that sees the figure at all.
    if on_pool is not None:
        pool = (cluster or {}).get("kv_cache_size_tokens")
        on_pool(pool if isinstance(pool, int) else None)
    # The remembered rate still wins the pricing question; the scrape above was for the
    # pool. Deciding otherwise would make a warm memory worse than a cold one.
    if remembered is not None:
        return DecodeRate(remembered, float(expected_concurrency),
                          source="observed_at_concurrency")
    running = (cluster or {}).get("requests_running")
    # Nothing remembered at this concurrency or busier. A configured floor prices the first
    # turn instead of the since-boot figure, which is a blend over every regime (ADR-0094)
    # and errs optimistic -- the direction that kills a turn rather than truncating one.
    # The floor is used only in this empty case, and `running` is the concurrency it is
    # priced at. Narrows ADR-0055 to what to do when no measurement exists. Why: ADR-0101.
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

    Reasoning is spent from this same budget, so a high effort with a low cap returns an
    empty answer; that is what the floor is for.

    Precedence, most specific first: the call argument, then the agent's frontmatter (M6,
    which resolves into `explicit` when it exists), then the configured default -- which
    comes **last** rather than first. The floor is applied as a `max()` over the configured
    value, never as an alternative to it.

    An explicit number is *not* raised to that floor, and the caller who asks for a small
    budget at max effort still gets the recovery, which retries at the floor.

    The per-model cap applies to every path, last, because it is what the wire will accept.

    `ceiling` is the deadline expressed in tokens, and unlike the floor it applies to the
    explicit argument too. A budget above it is not a larger answer, it is the same answer
    killed at `stall_timeout` with everything generated discarded -- which is how a
    productive turn gets reported as a stall. Why: ADR-0014, ADR-0024, ADR-0055.
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

    The ordering -- system, agent body, files block, task last -- lives on `Delegation`, so
    this function and the turn loop cannot disagree about it. Why: ADR-0011.
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

    RFC 7231 allows two spellings -- a count of seconds, and an HTTP-date -- and a server
    may send either, so honouring only one is honouring the header by luck. Returning None
    rather than raising is the point: a malformed or absent header must fall back to
    ordinary backoff, never abort a delegation. A header is a hint from someone else's
    machine, and it is not worth failing a call over.

    Negative results clamp to zero. A date already in the past means "now", not "go back".
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        # The seconds form is specified as an integer. Accepting "1.5" here would be
        # inventing a third form the spec does not have; it falls through to the date
        # parse, fails there too, and lands on plain backoff -- which is correct.
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
    """Whether sending the identical request again could plausibly get a different answer.

    Only the two reachability kinds are ever eligible. `BackendProtocolError` means the
    endpoint answered and is not the stack we meant, and `CanonicalShapeError` is our own
    bug -- retrying either just performs the same mistake more slowly.
    """
    if isinstance(error, BackendUnavailable):
        return True
    return isinstance(error, BackendRefused) and error.status in _RETRYABLE_STATUSES


def _retry_is_plausible(
    error: Exception, *, attempt_seconds: float, seconds_left: float | None
) -> bool:
    """Whether sending it again could plausibly end differently *in the time that is left*.

    `_is_retryable` answers whether the failure is the kind that can change; this answers
    whether there is room for it to. The two are separate because the error class alone
    cannot say: the same read timeout is worth another attempt against a deadline with
    room for one, and worth nothing against a deadline without.

    Only a failure raised *while generating* is time-tested. A connect failure spent
    nothing and can succeed in a moment, so applying the rule to it would refuse the retry
    that most deserves one. A read timeout has already consumed the whole allowance
    without answering, so a retry that gets less time than that cannot do the same work --
    it can only spend the rest of the delegation proving it.

    `None` is not zero. An absent deadline means nothing bounds the attempt (ADR-0055);
    reading it as no-time-left would turn a missing bound into the strictest one there is.

    Inclusive at the boundary: exactly as much time as the failed attempt used is enough
    to try again, because that attempt is the only evidence of what the work costs.
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

    An explicit `Retry-After` wins and is used as sent, not jittered: the endpoint named
    a time, and shortening it by a random factor is not honouring it. Jitter exists to
    decorrelate clients that are all guessing, and a server that told us when to come back
    has removed the guess.

    The cap applies to both paths, including the honoured one. Without it a large or
    hostile header stalls the call for as long as it likes, in a wait that sits *between*
    requests where no HTTP timeout reaches it.
    """
    asked = parse_retry_after(getattr(error, "retry_after", None))
    if asked is not None:
        return min(asked, cfg.retry_max_delay)
    backoff = min(cfg.retry_base_delay * (2 ** (attempt - 1)), cfg.retry_max_delay)
    return jitter(0.0, backoff)


# How often a live deadline is re-read while a call is in flight. Not a config setting:
# it trades a wakeup per quarter second against how sharply a deadline lands, and neither
# side of that is a knob an operator should have to reason about -- the deadlines
# themselves are the settings.
_DEADLINE_TICK = 0.25


async def _until_deadline(
    coro,
    left: Callable[[], float | None],
    *,
    tick: float,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
):
    """Run `coro`, cancelling it once `left()` has actually run out.

    Deliberately not `asyncio.wait_for`, which takes one budget at call time and cannot see
    it move: token arrival resets the stall deadline (ADR-0072), so the budget grows while
    the call runs and a fixed timeout would kill a turn that had been producing all along.
    Why: ADR-0072.

    `tick_sleep` is a test seam, for the reason `clock` and `sleep` already are: this is the
    one place that waits on the wall rather than on the injected clock, so a test of a
    deadline would otherwise have to spend it. `None` -- always, outside the tests -- waits
    on the call itself, which costs one suspension and returns the instant it answers.
    Polling unconditionally instead would add a tick of latency to every backend call.

    Raises `TimeoutError` on expiry, so the diagnosis below is unchanged: the caller still
    asks which of the two deadlines expired rather than assuming.
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
                    # The one place the partial can be caught. The adapter hangs what it had
                    # decoded on whatever exception leaves it, cancellation included, and the
                    # attribute does survive `await task` -- measured on 3.12.3 and 3.14.6.
                    # The task object cannot be asked instead: once it is cancelled,
                    # `task.exception()` refuses rather than answering.
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
        # Whatever brought us here -- the caller cancelled, a client disconnected -- the call
        # must not outlive it. Cancelling the task is what closes its HTTP stream, and a
        # closed stream is the only thing that tells the engine to stop generating.
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass  # its own cancellation; the exception that brought us here propagates


@dataclass(frozen=True, slots=True)
class RetryRecord:
    """One failed attempt `complete_with_retry` chose to retry, and why.

    `attempts` counts a retry but cannot say which failure it was, and a dropped route and a
    503 have different remedies. `kind` is the exception's class, `status` the HTTP status a
    refusal carried, `seconds` how long the failed attempt ran, `wait` the sleep chosen.
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
    on_token: Callable[[str], None] | None = None,
    on_retry: Callable[[RetryRecord], None] | None = None,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[CanonicalResponse, int, float]:
    """Send until it answers, a failure is not worth repeating, or the attempts run out.

    Returns the response, how many real calls it took, and the answering attempt's own
    seconds. That count is server-captured
    ground truth about what this dispatch actually cost, in the spirit of ADR-0007, and it
    is the only honest way for a caller to see that a quiet success was really three tries.

    On exhaustion the last real exception propagates unchanged. Wrapping it in something
    that says "gave up after N" would hide which failure it actually was, and the four
    kinds are distinguishable precisely so the layer above can act on them.

    `sleep` and `jitter` are injected for the reason `client` is injected into the adapter
    and `cache` into the server: without that, a test of the retry logic sleeps for real,
    and a test of the cap cannot pin the random factor.

    `deadline` is an absolute reading of `clock`, set by the caller that owns the whole
    delegation, and it bounds the sum that would otherwise be unbounded: attempts, and
    the waits between them. It is checked before each attempt, applied *to* each attempt as
    a ceiling, and checked against the wait before sleeping -- a backoff that would sleep
    past the deadline ends the delegation instead of waking up to find it already gone.
    `None` disables it, which is what the empty-answer stages above use when they have
    already exhausted the budget themselves.

    `stall_left` is the second deadline, and it answers a different question: not "has
    this delegation run too long" but "has it stopped getting anywhere". It returns the
    seconds remaining before the no-progress deadline, so a value at or below zero is a
    stall. A callable and not a number, because the caller owns when progress last
    happened and resets it -- a float handed down once would stop moving forward while the
    run kept completing turns, which is a deadline that expires on healthy work.

    Both bound each attempt, and the tighter one wins. Without that the raised ceiling
    would let a single wedged call sit for the whole of `dispatch_timeout`, which is the
    failure this pair exists to split apart. `None` disables it, as `deadline` does.

    `clock` is injected for the same reason `sleep` and `jitter` are: a test of the
    deadline must not spend the deadline. The ceiling on each attempt is measured with the
    same clock, so a fake clock governs the whole function and the event loop is never
    asked to wait for real.
    """
    attempts = 0
    started = clock()

    def remaining() -> float | None:
        return None if deadline is None else deadline - clock()

    def spent() -> float:
        """How much of the *delegation's* budget is gone -- not how long this call ran.

        On the loop path this function is entered fresh per turn (`run_agentic_loop`
        calls it once per turn) against a deadline taken once for the whole delegation.
        Measuring from `started` would therefore report one turn's elapsed time beside the
        whole delegation's limit -- a number that is not past it, under a remedy the number
        does not support.

        Derived from the deadline itself, so there is no second origin to disagree with
        it. `started` remains the only thing available when there is no deadline at all,
        which is what the empty-answer stages pass once they have spent the budget.
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
        """The tighter of the two deadlines, read *now* rather than fixed for the call.

        Since ADR-0072 token arrival moves `stall_left`, so this is re-read while a call is
        in flight rather than handed to `asyncio.wait_for` once at the top. A fixed budget
        cannot see the thing that moves it: a turn streaming tokens the whole way would
        still be killed at whatever was left when it started, which is exactly the death
        this is meant to stop.
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
                # Timed from this attempt's start, not the call's. A turn that failed once
                # and answered on the second try divides one attempt's tokens by every
                # attempt's seconds otherwise -- and the backoff between them is not
                # generation either, so it is outside this interval by construction.
                return answer, attempts, clock() - attempt_started
            # The per-attempt ceiling, and the only one: the adapter's client bounds silence
            # between frames, not the length of a call that keeps producing. Without this an
            # attempt could overshoot whichever of the two delegation-level deadlines is
            # tighter.
            answer = await _until_deadline(
                backend.complete(request, on_token=on_token), ceiling,
                tick=_DEADLINE_TICK, tick_sleep=tick_sleep,
            )
            return answer, attempts, clock() - attempt_started
        except TimeoutError as e:
            # Which of the two expired decides the diagnosis, so ask before blaming the
            # ceiling: a stall inside a raised `dispatch_timeout` would otherwise be
            # reported as a delegation that ran too long, sending an operator to raise a
            # setting that had nothing to do with it.
            stalled()
            if deadline is None:
                # The delegation deadline was not in play, so it is not what expired;
                # naming it would send an operator to raise a setting that had no part in
                # this. What is left is the stall clock -- the only bound this function
                # applies to one attempt -- so that is what the message names, and the
                # remedy is the stall one for the same reason `stalled()` uses it: a run
                # that stopped producing is not helped by being given longer to stop
                # producing in.
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
            # `_until_deadline` cancelled the call, and what it had decoded came back on
            # the TimeoutError. Both diagnoses carry it: which deadline expired decides
            # the message, never whether the tokens are worth returning.
            timed_out.partial = getattr(e, "partial", None)
            raise timed_out from e
        except (BackendUnavailable, BackendRefused) as e:
            # Time-tested, not just kind-tested. An attempt that consumed its whole
            # allowance without answering cannot do the same work in less, so retrying it
            # only spends the rest of the delegation discovering that (ADR-0055).
            if attempts >= cfg.retry_max_attempts or not _retry_is_plausible(
                e, attempt_seconds=clock() - attempt_started, seconds_left=ceiling()
            ):
                raise
            wait = _delay_before_retry(cfg, e, attempts, jitter)
            left = ceiling()
            if left is not None and wait >= left:
                # Which deadline made the wait impossible decides the diagnosis, exactly
                # as it does for a timed-out attempt above. `ceiling()` is the tighter of
                # the two, so reaching here says nothing about which one it was, and the
                # stall check names it.
                stalled()
                # Sleeping first would burn the rest of the budget and then report the
                # deadline, naming a wait this function chose rather than the work.
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

    A value object rather than a widening tuple. `effort` is not always the level the
    caller asked for, `attempts` is not always one, and `reasoning_exhausted` is a
    verdict only this module is in a position to reach. Returning a shape lets each of
    those be named at the call site instead of positioned.

    `reasoning_exhausted` is deliberately narrow. It is true only when the answer is still
    empty at a length stop *after* a larger budget and a lower effort have both been tried
    -- ADR-0014's `reasoning_exhausted_budget`, and the only case where the phrase is
    earned. An empty answer that nothing was tried on is a mechanical fact, not this
    verdict.
    """

    response: CanonicalResponse
    effort: str
    attempts: int
    # Every failed attempt retried, in order, across every stage of the recovery ladder.
    retries: tuple[RetryRecord, ...] = ()
    reasoning_exhausted: bool = False
    # How long the attempt that *answered* took, which is not how long the turn took.
    # The token counts come from that attempt alone (ADR-0014), so a rate derived from
    # them must divide by the same event -- a turn that failed once and answered on the
    # second try otherwise reports one attempt's tokens over every attempt's seconds.
    answered_seconds: float = 0.0
    # Summed over every stage, unlike the two above and unlike the token counts. A turn
    # that answered empty and was sent again paid a *second* prefill, and a run summary
    # asking where the wall clock went wants both of them -- so these are the one place
    # here that adds attempts together on purpose. The answering attempt's own decode
    # span is still `response.decode_seconds`, which is what a rate must divide by.
    # `None` when no stage reported an interval, which is an adapter that cannot stream
    # rather than a turn that spent no time.
    prefill_seconds: float | None = None
    decode_seconds: float | None = None


def is_empty_at_length(response: CanonicalResponse) -> bool:
    """The reasoning-exhaustion signature, and nothing broader.

    Empty text *and* a length stop, together. Either alone means something else entirely:
    an empty answer at `finish_reason == "stop"` is a model that genuinely had nothing to
    say, and retrying it buys the same non-answer at full price; a length stop with text in
    it is ordinary truncation, where an answer exists and is merely cut short.

    ADR-0014 draws exactly this line, and widening it is the expensive mistake -- the
    mitigations below cost two extra dispatches at the largest budget the model allows.
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
    on_token: Callable[[str], None] | None = None,
    max_tokens: int | None = None,
    budget_ceiling: int | None = None,
    # The first attempt's resolved budget, computed by the caller so the priced row and
    # the send are the same number rather than two computations that can drift. When not
    # supplied -- the direct callers in the tests -- this falls back to the same
    # `resolve_max_tokens` below, so the behaviour is unchanged.
    asked_budget: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Dispatch:
    """One dispatch, plus the two mitigations for an answer that ran out of room.

    Three stages, following ADR-0014: send; on the exhaustion signature retry once at a
    larger budget; if that is still empty, step effort down one level and send once more.
    Deliberately not a cascade through every level -- each stage is a real dispatch at the
    largest budget the model permits, and a four-stage climb down would cost more than the
    answer is worth while the caller waits.

    Stepping effort down is not the cheap fallback it looks like. The level is part of the
    rendered prompt, so `prompt_tokens` moves with it: a stepped-down dispatch misses the
    cluster's prefix cache entirely and pays a fresh prefill on top of the generation.
    That is why it is the last resort rather than the first, and why the budget retry --
    which keeps the level, and the prefix -- comes first.

    `build` takes the effort level and the budget and returns the request to send at them,
    which is what lets one-shot and one turn of the agentic loop share this. Everything
    that differs between them -- the system prompt, the history, whether tools are declared
    -- is decided by the caller's builder, and everything that differs between the *stages*
    is decided here. Turning this into two copies is how the turn loop would end up
    diagnosing exhaustion differently from the one-shot path.

    `budget_ceiling` bounds every stage, including the enlarged retry. Bounding only the
    first would make stage 2 the way back to a budget the clock cannot pay, and stage 2 is
    reached precisely when the model has already shown it will use everything it is given.
    Where the first budget is already at the ceiling the retry is skipped by the existing
    identical-request test, and the cascade falls through to stepping effort down -- which
    is the correct remedy once more room is not available: think less, rather than ask for
    time that does not exist.

    Attempts accumulate across all three stages and every transport retry inside them, so
    the number reported is what this dispatch really cost. What is *not* summed is the
    token counts: those come from the attempt that answered. ADR-0014 says the retry must
    not charge the turn budget, so a turn is charged for the answer it got.
    """
    if asked_budget is None:
        asked_budget = resolve_max_tokens(cfg, entry, effort, max_tokens, ceiling=budget_ceiling)
    attempts = 0
    # `done` closes over this, so no exit from the ladder can drop a record.
    retries: list[RetryRecord] = []
    # The wall clock this dispatch spent, split the way the engine spends it, and summed
    # over the stages because each stage is a fresh prompt and so a fresh prefill. The
    # token counts deliberately are not summed -- they come from the attempt that answered
    # -- so these are the one accumulation here, and the comment on `Dispatch` says why.
    # `None` until some stage reports an interval: absent means the adapter cannot time
    # itself, which is not the same claim as zero.
    spans: dict[str, float | None] = {"prefill": None, "decode": None}

    def stage(reply: CanonicalResponse) -> None:
        """Add one stage's two intervals to the running totals."""
        for key, value in (
            ("prefill", reply.prefill_seconds), ("decode", reply.decode_seconds),
        ):
            if value is not None:
                spans[key] = (spans[key] or 0.0) + value

    def done(reply: CanonicalResponse, level: str, exhausted: bool = False) -> Dispatch:
        """The one place a `Dispatch` is built, so no exit can forget a field.

        Four returns below each spell the constructor out, so a field added here would have
        to be carried by three of them.
        """
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

    # Stage 2: the same level, more room. `thinking_max_tokens_floor` documents itself as
    # the size retried after an empty answer, so there is no second setting for it.
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
    # Otherwise the model's own cap already pinned the first budget, and "retry at a larger
    # budget" would send a byte-identical request. Skipped rather than spent: an identical
    # dispatch cannot produce a different outcome at temperature zero, and even where it
    # might, paying a full generation for the chance is not a mitigation.

    stepped = _STEP_DOWN.get(effort)
    if stepped is None:
        # Effort is already off. There is nothing left to disable, so this is a budget too
        # small for the answer -- NOT reasoning exhaustion. Reporting it as exhaustion
        # would be a diagnosis the caller could act on wrongly, sending them to lower the
        # effort that is already lowest instead of raising the budget or shortening the task.
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
    on_alive: Callable[[float, int, float, int, int, float | None], Awaitable[None]] | None = None,
    on_priced: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_pool: Callable[[int | None], None] | None = None,
    rate_history: RateHistory | None = None,
    expected_concurrency: int = 1,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_token: Callable[[], None] | None = None,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Dispatch:
    """One turn, no tools, and the recovery cascade around it.

    Still reachable, and still the right shape when the caller offers no tools: a model
    that cannot open anything should be told so plainly rather than handed an empty tool
    list and a prompt implying there is a second turn. `run_agentic_loop` covers the case
    where tools are offered, and the two share `dispatch_with_recovery` so exhaustion is
    diagnosed identically on both paths.

    `dispatch_timeout` is enforced here rather than lower down, because this is the only
    layer that knows a delegation is what it is. One deadline is taken at entry and passed
    down; the recovery stages do not each get a fresh budget, which would make the real
    bound the timeout times the number of stages.

    What this bounds is a wait, not the client's idle timeout. The default is 3600s against
    Claude Code's 1800s stdio idle timeout, so a delegation could be abandoned by the client
    while this is perfectly happy. ADR-0018's per-turn progress notification is what answers
    that on the loop path, and there are no turns here to report -- so `on_alive` reports on
    a timer instead. Without one supplied this path is silent for its whole duration, which
    is the one call shape able to reach that idle timeout.
    """
    resolved = resolve_effort(cfg, entry, effort)
    origin = clock()
    deadline = origin + cfg.dispatch_timeout

    # Separate from `origin`, which anchors `deadline` and must not move. Since ADR-0072
    # token arrival is progress, and this is the path with the most to gain from that: a
    # one-shot completes no turns, so without streaming it has no progress signal and its
    # stall clock would run from entry no matter how well the call is going.
    last_progress = origin

    def stall_left() -> float:
        """The no-progress deadline, counting from the last token this call produced.

        A one-shot has exactly one unit of *turn* progress to make and makes it at the end,
        so without token arrival there is nothing to reset this and it counts from entry:
        the effective bound would be the tighter of `stall_timeout` and `dispatch_timeout`
        rather than the ceiling alone (ADR-0047). Token arrival supplies that signal
        (ADR-0072).
        """
        return cfg.stall_timeout - (clock() - last_progress)

    chunks = 0
    reasoning_chunks = 0

    def token_arrived(kind: str = "answer") -> None:
        """One frame carried generated output. `kind` names which half of the reply.

        The default keeps a backend that still calls `on_token()` with no argument working:
        the only difference is that the reasoner is not told what arrived, and a frame that
        arrived at all was at least an answer's worth of progress.
        """
        nonlocal last_progress, chunks, reasoning_chunks
        last_progress = clock()
        chunks += 1
        if kind == "reasoning":
            reasoning_chunks += 1
        if on_token is not None:
            on_token()

    def streamed() -> tuple[int, int, float | None]:
        """What has arrived, and how long since. Chunks, deliberately not tokens.

        A frame usually carries one token on this stack and is not promised to, and the
        only real token count arrives in the final usage frame -- after the heartbeat has
        stopped mattering. Reporting frames as tokens would be a guess dressed as a
        measurement, which is the thing this event's own history warns against.

        The reasoning count rides beside the total so a watcher can tell thinking from
        answering: the two halves are the point of the split, and a reader that has to
        subtract them from one number is doing the work this event exists to do.
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
        # Seeded from the cluster, because a one-shot has no earlier turn to learn from and
        # is the shape with the least slack: it completes no turns, so its deadline runs
        # from entry and it must fit a whole answer inside one of them (ADR-0055).
        rate = await seed_decode_rate(
            backend, rate_history, expected_concurrency, on_pool=on_pool,
            label_trusted=cfg.admission_idle_hold > 0,
        fallback=cfg.rate_fallback_tok_s,
        )
        ceiling = rate.ceiling(cfg, budget_seconds(
            cfg, dispatch_left=deadline - clock()
        ))
        # The one-shot completes no turns, so without this it can only ever be explained
        # by what it was *asked*, never by what it was allowed.
        # Resolved once, here, so the priced row and the send are the same number. The
        # budget is computed before the row because the row has to say what the turn was
        # *allowed*; passing it down keeps `dispatch_with_recovery` from recomputing it a
        # second way.
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

    # Everything from here down to the backend call is one sequential chain of awaits, so a
    # heartbeat cannot be another `await` inside it -- it has to run beside the chain and be
    # torn down with it. The `finally` is what makes that safe: cancel, then await the
    # cancellation, so no task outlives the dispatch it was reporting on. `run_agentic_loop`
    # does the same around its turn loop.
    beat = asyncio.create_task(_keepalive(
        cfg, on_alive, clock,
        # Deliberately not `budget_seconds`, though the shapes are close. That one sizes
        # one reply and counts the delegation deadline only, because a stream that is
        # producing is not silent and must not be charged the stall clock. A countdown
        # shown to a reader has the opposite job: name whichever deadline will actually
        # end the run, and since ADR-0099 that is usually the stall one.
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
            # Ours, not the caller's. A cancelled dispatch reaches this `finally` too, and
            # re-raising here would replace whatever actually ended the delegation.
            pass


async def _keepalive(
    cfg: Config,
    on_alive: Callable[[float, int, float, int, int, float | None], Awaitable[None]],
    clock: Callable[[], float],
    ends_in: Callable[[], float],
    streamed: Callable[[], tuple[int, int, float | None]] = lambda: (0, 0, None),
) -> None:
    """Say the delegation is still running, on a timer, until cancelled.

    Sleeps on `asyncio.sleep` rather than the injected `sleep` seam the retry path uses.
    That seam is stubbed instantly in tests, which would turn this into a busy loop
    hammering the callback while the test waited on something else.

    A failing callback stops the heartbeat and nothing else. It exists to protect a long
    delegation from being abandoned, and a heartbeat that instead killed one -- because a
    notification could not be delivered, which is not even evidence the client is gone --
    would be strictly worse than not having it. The cost of that trade is that a `TypeError`
    from an arity mismatch looks exactly like a delivery failure, so `on_alive`'s shape is
    asserted in the tests rather than discovered here as a heartbeat that quietly stopped.
    """
    started = clock()
    while True:
        await asyncio.sleep(cfg.keepalive_interval)
        try:
            # Four figures, because they answer different questions. `dispatch_timeout` is
            # what the delegation is allowed; `ends_in` is how long until the tightest
            # deadline actually fires, which is the one a reader needs. The last two are
            # what streaming made knowable: how much has arrived, and how long since any
            # of it did (ADR-0072).
            chunks, reasoning_chunks, since = streamed()
            await on_alive(
                clock() - started, cfg.dispatch_timeout, ends_in(),
                chunks, reasoning_chunks, since,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return


# --- the turn loop ------------------------------------------------------------------------


# What an evicted tool result is replaced by. The block stays and keeps its `tool_use_id`:
# some backends validate that every tool_use has a matching result, so dropping the block
# outright would make a long delegation fail at the wire rather than merely forget.
def _tool_result_sizes(cfg: Config, messages: tuple[Message, ...]) -> list[int]:
    """Estimated tokens of each tool result, oldest first.

    A list rather than a count, because what fills a context window is bytes. Order is
    history order and the eviction boundary walks it from the front, so this must not be
    sorted.

    Extension-less, so `estimate_tokens` costs every result at the densest ratio it knows
    and over-counts rather than under-counts -- the same bias `estimate_message_tokens`
    takes, and for the same reason: under-counting here retains more than fits.
    """
    return [
        cfg.estimate_tokens(len(b.content))
        for m in messages
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]


EVICTED_STUB = "[dropped from the history to keep it bounded. Call the tool again if needed.]"

# Prefixed to a result served from the dedup cache. Silently returning the identical bytes
# would teach the model nothing, and a model that repeats a call usually does so because it
# is stuck -- saying plainly that nothing new happened is what breaks that.
REPEAT_PREFIX = "[repeat of an identical earlier call; nothing was run again]\n"

# Served instead of the cached bytes once eviction has dropped that result from the
# history, so the next identical call answers instantly instead of re-running the tool
# (ADR-0080). Saying what happened lets the model ask for a narrower part, the only
# outcome that actually fits the window.
EVICTED_REPEAT = (
    "[identical to an earlier call whose result was dropped from the history to stay "
    "inside the context window. Nothing was run again and the content is not being "
    "restored -- ask for the part of it you need.]"
)


@dataclass(frozen=True, slots=True)
class _CachedResult:
    """One dedup-cache entry, and whether the history still holds what it copies.

    `tool_use_id` is carried so eviction can find the entry belonging to a result it just
    stubbed. The cache is keyed by call rather than by id, so without it the two structures
    have nothing in common to match on -- which is precisely why they could not see each
    other.
    """

    content: str
    tool_use_id: str
    evicted: bool = False


async def _no_progress(turn: int, of: int) -> None:
    """The default when nobody is listening. Tests inject a recorder, `server.py` the real one."""


def resolve_max_turns(
    cfg: Config, explicit: int | None = None, allowed: frozenset[str] = frozenset()
) -> int:
    """The turn budget: the caller's number, else the configured default, capped either way.

    The hard cap is applied to both, silently, because it exists to stop a caller -- or an
    agent file in M6 -- occupying the cluster for hours, and a limit that can be argued out
    of is not one. Refusing the call instead would be worse: the work is legitimate, only
    the number is not.

    The default depends on `allowed`: a toolset that can write gets the writing default,
    because writing work iterates, and a reading pass keeps the smaller one that bounds it.
    """
    if explicit is None:
        if any(REGISTRY[name].writes for name in allowed if name in REGISTRY):
            return min(cfg.max_turns_default_writing, cfg.max_turns_hard_cap)
        # Not clamped: config.py already refuses to load a default above the cap, so a
        # min() here could never bind. A guard that cannot fire is worse than none,
        # because the next reader trusts it.
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

    Every turn resends the whole history, so without this the cost of a delegation grows
    with the square of its length.

    **`upto` is a boundary the caller carries, not a window recomputed from `keep`**, so one
    rewrite buys `keep` turns of prefix stability (ADR-0056). Only the *content* goes: the
    block and its `tool_use_id` stay, and an already-evicted result is not counted twice --
    the count is what this call did, not how much of the history is stubbed.
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

    Here rather than in the system prompt: a turn counter in the prefix would change one
    byte of it per turn and cost a full prefill (ADR-0011). The tail is where dynamic
    content is free, because the tool results next to it were never cached anyway.
    """
    if turns_left <= 1:
        return (
            "[final turn: tools are withdrawn for it. Give your answer now -- whatever you "
            "write next is what the engineer receives.]"
        )
    return f"[{turns_left} turns remain, this one included.]"


# --- context economics --------------------------------------------------------------------


# What the model is told when projected usage crosses the nudge threshold. A separate line
# from `countdown_line` and never a replacement for it: the two say different things -- one
# counts turns, the other counts room -- and a delegation can be short of either without
# being short of both.
WRAP_UP_LINE = (
    "[context is running short. Stop opening new threads of work: finish what you have, "
    "and write your answer while there is still room for it.]"
)


def estimate_message_tokens(cfg: Config, message: Message) -> int:
    """Rough size of one history message, in the same estimated tokens as everything else.

    Deliberately our own estimate rather than anything the backend reports. It is one side
    of the plateau comparison, and the whole point of that comparison is to hold a number
    we computed against a number the backend did -- two estimates from the same source
    could agree while both being wrong.

    Extension-less on purpose: `estimate_tokens` then costs it at the densest ratio it
    knows, so a message is over-counted rather than under-counted. Under-counting here
    would suppress a real detection, which is the failure that matters.
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

    A fraction and never a flat count. A flat reserve big enough to be worth holding on a
    1M-token window is larger than 95% of an 8K one, so the same constant that is prudent
    for one model reports the other as full while its history is still empty.
    """
    return round(entry.context_window * cfg.overflow_reserve_fraction)


def projected_fraction(
    cfg: Config, entry: ModelEntry, last_input_tokens: int, pending_tokens: int
) -> float:
    """Share of the window this turn is projected to occupy, in [0, ...).

    **This is the only place the denominator is chosen, and it is `entry.context_window`.**
    Not `thinking_max_tokens_floor`, which is a reply budget and a different number for a
    different purpose; not a count of what this server evicted, which measures our own
    housekeeping rather than the model's room. A threshold over the wrong denominator is
    the usual shape of that bug, so the denominator is read once, here, and every
    threshold is expressed against what this returns.

    Note that a registry entry omitting `context_window` inherits a default silently
    (`registry.py`), which is why `context_overflow_enabled` is off by default rather than
    on.
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

    The retroactive half of overflow handling. If we appended real content to the history
    and the backend's reported prompt nevertheless did not grow, something dropped it --
    and if this server did not do the dropping, the backend did, silently, which means the
    model has been answering from a history neither of us can see the whole of.

    `evicted_this_turn` is the explanatory variable and it is **a count this server set at
    the point it evicted**, never a reading of `finish_reason` and never the model's
    account of what it still remembers (ADR-0007). That ordering matters: our own eviction
    is by far the likeliest reason a prompt plateaus, so a check that did not subtract it
    first would fire constantly on healthy delegations and be switched off within a day.

    `grew_by` is compared against a floor because most turns add almost nothing, and a
    plateau under an empty append is not evidence of anything. The slop on the other side
    exists because a backend that trims a token between turns must not read as truncation.
    """
    if evicted_this_turn > 0:
        return False
    if grew_by <= cfg.overflow_min_growth_tokens:
        return False
    return this_input_tokens <= prev_input_tokens + cfg.overflow_plateau_slop_tokens


# One tool call as the server saw it: name, the path argument if it had one, and
# whether it failed. Enough to reconcile against `git status`, and nothing more --
# result content would make an abort report the size of the delegation it aborted.
_Ledger = list[tuple[str, str, bool]]

# How much of one argument value the record keeps, and how much of a refusal. Constants
# rather than settings: these size a diagnostic record's shape, not a deployment, and an
# operator who wants more of a refusal wants the refusal, which is what the elision marker
# tells them they are missing. The message cap is the larger of the two because a refusal
# is the whole reason this record exists, while an argument only has to be recognisable.
TOOL_ARG_VALUE_CAP = 240
TOOL_MESSAGE_CAP = 600

# Arguments that carry a file body rather than an identifier. These are summarised to a
# length and a digest instead of being elided, because a truncated body is the worst of
# both answers -- useless for reading and still a partial copy at rest on disk. ADR-0039
# excluded bodies from the record and this keeps them out of it through the arguments door.
_BODY_ARG_NAMES = frozenset({"content", "old_string", "new_string"})


def _elide(text: str, cap: int) -> str:
    """Shorten to `cap`, saying how much was dropped.

    The marker is not decoration. A silently truncated argument reads as a complete one, so
    a reader diagnosing a refusal would draw conclusions from a path that was never the
    path the model sent.
    """
    if len(text) <= cap:
        return text
    return f"{text[:cap]}... [+{len(text) - cap} chars]"


def _line_count(text: str) -> int:
    """Lines in a result, counting an empty result as none.

    A trailing newline does not add a line, so a one-match `search_files` reports 1 whether
    or not its output ends in a break -- the two differ by a byte and mean the same thing.
    """
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _body_summary(text: str) -> str:
    """A file body as a length and a digest, never as bytes.

    The digest is what makes two writes of the same file distinguishable, which is the only
    question a record is asked about a body: which of these two writes actually ran. Twelve
    hex characters, because this identifies a write within one delegation rather than
    resisting an adversary looking for a collision.
    """
    digest = sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"<{len(text)} chars, sha256:{digest}>"


def record_arguments(call: ToolUseBlock) -> tuple[tuple[str, str], ...]:
    """What the model asked for, capped per field and with bodies summarised.

    Sorted, so two records of the same call compare equal regardless of the order the
    backend serialised the arguments in -- which is what makes a test on this record
    stable across backends.
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

    A dataclass rather than a wider tuple, deliberately: the pair it replaces is unpacked at
    three call sites, and widening a tuple other code unpacks breaks one caller quietly.

    `message` is the refusal text and is present only on an error outcome. On success the
    record carries accounting instead -- size, line count, exit code -- and never content,
    which ADR-0039 keeps out of the record.
    """

    name: str
    outcome: str
    arguments: tuple[tuple[str, str], ...]
    # Capped refusal text. Empty on success, and empty is not "no reason given": the
    # outcome says which of the two it is.
    message: str = ""
    # Size of what came back, in bytes and in lines. Universal rather than per-tool: lines
    # is the match count for `search_files`, the output length for `run_bash` and the file
    # length for `read_file`, and a per-tool table of counters would drift every time a
    # tool changed its output. None when no result reached us at all.
    result_bytes: int | None = None
    result_lines: int | None = None
    # Only when a process actually exited. None covers both "no shell command here" and
    # "killed before it could exit", which `bash_failures` distinguishes -- and neither is
    # 0, a real exit code that must not collide with either (see `BashOutcome`).
    exit_code: int | None = None
    # How long the call took, timed where it executed. `None` where nothing ran: a call
    # served from the dedup cache.
    ms: int | None = None

    def as_json(self) -> dict[str, Any]:
        """The record as one JSON object, with absent fields absent rather than null.

        Following `_diagnostics_block`'s rule: a key present and empty reads as a measured
        empty, so a call that ran no shell command must not report `exit_code: null` beside
        one that was killed before exiting.
        """
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
        return row


def tool_call_record(
    call: ToolUseBlock, outcome: str, result: ToolResultBlock | None,
    *, ms: int | None = None,
) -> ToolCallRecord:
    """Build one call's record from what the server saw, never from the model's account.

    The refusal text is taken from the result block `tools.py` builds, the same string the
    model is handed, so the record and the model agree about what was said (ADR-0007).

    `ms` is the call's own measured duration, supplied by `_run_calls` and `None` when
    nothing ran.
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
        # Zero rather than one for empty content. `count("\n") + 1` alone reports a line
        # that is not there, and "one line" is the answer an operator would read as a
        # `search_files` that found a match.
        result_lines=None if result is None else _line_count(result.content),
        exit_code=exit_code,
        ms=ms,
    )


@dataclass(frozen=True, slots=True)
class TurnDiagnostic:
    """What one turn cost and what it did, kept only when the caller asked for it.

    Every field here is something the server watched, never something the model reported
    about itself (ADR-0007). The aggregate ledger on `AgenticDispatch` says a delegation ran
    nine turns and evicted twelve results; this says which turn and what the prompt cost
    either side of it.

    Metadata only: result *content* is not carried, and each call carries `ToolCallRecord` --
    arguments capped per field, a refusal message when there was one, accounting otherwise.
    """

    turn: int
    input_tokens: int
    output_tokens: int
    # Of `input_tokens`, how many the cluster served from its prefix cache instead of
    # computing. Per turn rather than per delegation because that is where the story is:
    # turn one pays for the whole prefix and every turn after it should not, so a flat
    # figure across a delegation would hide the case worth seeing, a cache going cold
    # mid-run. `None` means the endpoint reports no caching at all, which is not zero.
    cached_tokens: int | None
    attempts: int
    effort: str
    evicted: int
    tool_calls: tuple[ToolCallRecord, ...]
    # Beside `attempts`: the count says how many, these say which and why.
    retries: tuple[RetryRecord, ...] = ()
    # Where this turn's backend time went, summed over its attempts -- a turn sent three
    # times paid three prefills, and wall clock is what a run summary adds up. Carried on
    # the diagnostic rather than handed to the transcript separately, because everything
    # else the turn event reports is already read off this object and a second channel is
    # a second thing to keep in step.
    prefill_seconds: float | None = None
    decode_seconds: float | None = None
    # And the answering attempt's decode span alone, which is the only interval
    # `output_tokens` may be divided by: the counts come from one attempt (ADR-0014), so
    # a rate over the sum above is arithmetic across two different events.
    answered_decode_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class RereadAfterEviction:
    """A file read again after this server dropped the first read from the history.

    The measurement that separates a genuinely expensive delegation from one paying twice
    for the same bytes. Both are slow; only the second is fixable by raising
    `keep_tool_results`, and without this there is no way to tell them apart -- which is why
    PLAN.md calls it a prerequisite for sizing eviction rather than a report about it.
    """

    path: str
    evicted_at_turn: int
    reread_at_turn: int


def _mark_evicted_in_cache(
    cached: dict[tuple[str, str], _CachedResult], dropped_ids: tuple[str, ...]
) -> None:
    """Tell the dedup cache that the history no longer holds what it copied.

    Marked rather than deleted, because deleting means the next identical call re-runs the
    tool. A marked entry still answers instantly and still runs nothing; it just stops
    undoing the eviction it was ignoring.

    Monotonic, like the boundary that drives it: an entry never becomes un-evicted, because
    the history it copied is not restored either.
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

    A separate diff rather than a third return value from `stub_oldest_tool_results`. That
    function's `(kept, dropped)` shape is asserted on directly by existing tests, and
    widening a tuple that other code unpacks is the kind of change that compiles everywhere
    and breaks one caller quietly. This reads the same two values that function already
    hands back, so it cannot disagree with it about what happened.
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

    Four structures, one concern: what was called, which file each call named, what the
    loop evicted, and what it cost -- server-captured facts, never the model's account of
    itself (ADR-0007).

    `calls` is maintained always, because an overflow abort needs it to produce a report. The
    per-turn detail is kept only when `diagnostics` is requested; the two path dictionaries
    are maintained regardless, since the correlation they feed cannot be reconstructed.
    """

    def __init__(self, *, diagnostics: bool) -> None:
        self.diagnostics = diagnostics
        self.calls: _Ledger = []
        self.turns: list[TurnDiagnostic] = []
        # Summed over every turn, and outside the `diagnostics` branch deliberately. What a
        # delegation cost the cluster is not a debugging extra: it is the only figure that
        # answers "what did delegating save", and the per-turn detail that would let a
        # caller add it up themselves is off by default (ADR-0058). `cached` stays None
        # until an endpoint reports caching at all, because a measured zero and an endpoint
        # that does not report are opposite answers.
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cached_tokens: int | None = None
        self.rereads: list[RereadAfterEviction] = []
        self.turn = 0  # the turn now running, so callees need not be handed it
        # The aggregate ledger `AgenticDispatch` reports. Counters rather than derived from
        # `calls`, because `attempts` and `evicted` have no entry there to be derived from.
        self.attempts = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.deduped = 0
        self.evictions = 0
        # The same count, split by tool. `calls` already holds every name and this could be
        # derived from it -- but `calls` is a list per delegation and the caller wants a
        # total, so deriving it would mean walking that list at the end to recover something
        # each call already knew. Kept beside `tool_calls` rather than folded into it,
        # because the total is what a caller reads first and a sum is not a substitute for it.
        self.by_tool: Counter[str] = Counter()
        # ADR-0007's original subject. Counted from real process exits, never from the
        # model's account of them, and reported beside its prose rather than inside it.
        self.bash_calls = 0
        self.bash_failures = 0
        # Separate from `bash_failures` rather than folded into it, because collapsing the
        # two loses the new fact: "three calls exited non-zero" and "one call hid a failure
        # inside a compound line" are different things for a caller to do something about.
        self.bash_masked_failures = 0
        # Calls that failed in any way, each counted once. `tool_errors` and `bash_failures`
        # overlap on every shell call that exits non-zero, and neither contains the other --
        # a masked failure is a shell failure and not an error -- so no sum of the two, and
        # neither alone, says how many calls went wrong.
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
            # Keyed by id, because an eviction knows only ids and has to be able to name a
            # file. The raw argument rather than the resolved path: `tools.py` never hands
            # the resolved one back, and the model's own argument is what a reader
            # reconciling this against the tree is looking for anyway.
            self._paths[call.id] = path
        self._reread(path)

    def _bash(self, bash: BashOutcome, is_error: bool) -> bool:
        """One shell command, as the server saw it rather than as the model reports it.

        Attempts, not completions: a call refused before a process started still happened,
        and `tool_calls` beside it counts the same way.

        `last_bash_exit` moves only when something actually ran. A command killed on timeout
        *did* run, so it becomes the last one and reports no exit code, where leaving the
        previous 0 standing would let a kill read as a success. A refusal never started a
        process, so the previous exit code still stands (ADR-0007).

        `masked_failure` reaches `bash_failures` because that field accumulates across the
        delegation, so call 1's hidden failure survives to call 10. `last_bash_exit` is not
        touched by it: it only ever means *the last*, and per ADR-0007 a non-zero there stays
        trustworthy while a zero stays ambiguous.
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
        """What this turn cost, recorded the moment the backend answers.

        Here rather than at the end of the turn body, and that is not a stylistic choice:
        the turn that produces the answer calls no tools and breaks out of the loop before
        reaching the end of it, so a count taken there would miss that turn's attempts.
        """
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
            )
        )

    def turn_tools(self, records: tuple[ToolCallRecord, ...]) -> None:
        """Attach this turn's tool records to the record `turn_cost` already opened."""
        if self.diagnostics and self.turns:
            self.turns[-1] = replace(self.turns[-1], tool_calls=records)


class _OverflowGuard:
    """The context economics of one delegation, kept together rather than in the loop.

    Extracted for the reason `Config._validate_retry` was: these are several statements
    serving a single concern with a name, and the turn loop reads better delegating to it
    than interleaving it. The side benefit is that the thresholds become testable without
    running a delegation at all, which matters here -- a check on this path that could not
    fire would be trusted.

    Entirely inert unless `context_overflow_enabled`. Every method returns the do-nothing
    answer when it is off, so the loop needs no second branch around each call.
    """

    def __init__(self, cfg: Config, entry: ModelEntry) -> None:
        self.cfg = cfg
        self.entry = entry
        # Only ever tightens. A delegation that wins back headroom by evicting has not
        # stopped growing, so relaxing again would only re-run the same climb.
        self.keep = cfg.keep_tool_results
        # How many of the oldest tool results are already stubbed. Monotonic, and the
        # reason the prefix survives: a boundary recomputed each turn moves each turn,
        # and every move costs the cache everything after it (ADR-0056).
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

        `context_overflow_enabled` ships `False` for one stated reason: every threshold here
        is measured against `ModelEntry.context_window`, and a registry entry omitting that
        field inherits a silent default, so arming against it would compute each threshold
        from a number the operator never picked. That reason is as good as it ever was, and
        it says nothing about an entry whose window *was* declared.

        Only `evict_upto` reads this, deliberately. The flag still governs the preventive
        half -- tighten, nudge, abort, and the plateau check -- because those act on a
        delegation, and turning them on for every deployment that names a window is a much
        larger claim than the one measured. Unarmed, `evict_upto` skips the pressure check
        and stubs on count alone, so the threshold the design is built around never applies
        to the shipped configuration.
        """
        return not self.entry.context_window_defaulted

    def share(self) -> float:
        return projected_fraction(
            self.cfg, self.entry, self.prev_input_tokens, self.pending_tokens
        )

    def evict_upto(self, sizes: Sequence[int]) -> int:
        """How many of the oldest tool results should be stubbed, now. Never retreats.

        Takes the results' estimated sizes, oldest first, and not a count of them: a count
        prices a one-line refusal and a 50KB file identically (ADR-0079).

        `keep` is both the floor (the newest results stay intact) and the step, so one
        rewrite buys `keep` turns of prefix stability; below the pressure threshold the
        boundary is returned unchanged rather than zeroed, since un-stubbing would cost the
        same cache (ADR-0056). The stepping is unconditional and only the holding is gated,
        because `context_overflow_enabled` is off by default and gating everything on it
        would leave the default configuration's history unbounded. An unarmed guard still
        steps; arming it additionally holds the boundary while there is room.
        """
        step = max(self.keep, 1)
        # The floor, applied first: whatever the sizes say, `keep` results survive intact.
        evictable = max(len(sizes) - self.keep, 0)
        # Oldest-first, never cheapest-first. Selection stays in history order because the
        # prompt is cached by prefix, so dropping a large result out of the middle would
        # invalidate everything after it -- trading the whole cache for a few thousand
        # tokens (ADR-0056). Only *where the cut falls* is driven by size.
        budget = self.cfg.retained_tool_result_tokens
        need = 0
        retained = sum(sizes)
        while need < evictable and retained > budget:
            retained -= sizes[need]
            need += 1
        # Floored to a whole step, so the boundary jumps by `keep` and one rewrite buys
        # `keep` turns of stability. Rounding *up* instead looks more eager and is the bug:
        # capped by `evictable` it advances by one every turn, which is precisely the
        # per-turn boundary ADR-0056 exists to stop.
        want = (min(need, evictable) // step) * step
        if (self.armed or self.pressure_known) and self.share() < OVERFLOW_EVICT_AT:
            return self.evicted_upto
        self.evicted_upto = max(self.evicted_upto, want)
        return self.evicted_upto

    def added(self, message: Message) -> None:
        self.pending_tokens += estimate_message_tokens(self.cfg, message)

    def begin_turn(self, *, turn: int, turns: int, ledger: _Ledger) -> None:
        """Abort if there is no room left, tighten retention if there is nearly none.

        Called before eviction, not after: the projection is what decides how hard to
        evict, so reading it from a history this turn has already trimmed would be
        measuring the cure rather than the illness.
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

        `evicted_this_turn` rather than a running total, because what has to be ruled out
        is an eviction *this* turn -- that is the only one that could explain *this* turn's
        prompt failing to grow.
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

        Asked after the response rather than before it, so the decision uses what the
        backend said the prompt cost rather than the estimate the turn began on.
        """
        if not self.armed or self.share() < OVERFLOW_NUDGE_AT:
            return False
        self.nudged_at = self.nudged_at or turn
        return True


def _preserve_across_nudge(
    response: CanonicalResponse, *, said_before: str
) -> CanonicalResponse:
    """Keep what the model said before it was told to wrap up.

    The loop ends on any reply carrying no tool calls, and that reply's text becomes the
    whole answer. A model answering a wrap-up nudge often replies with an acknowledgement
    -- "understood, finishing now" -- which under that rule silently replaces the substance
    it had already written.

    So a nudge reply **concatenates and never overwrites**. Only on the nudge path: an
    ordinary multi-turn delegation still answers with its final turn, because its earlier
    turns are narration ("let me check that file") and joining those onto every answer
    would make every answer worse to fix a case that only arises here.

    Returns the response unchanged when there is nothing to preserve, or when the final
    reply already contains it -- a model that restated its own findings does not need them
    twice.
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
    """What the server watched, beside what the working tree says. ADR-0007.

    The ledger and `git status` are deliberately not merged: where they disagree the
    disagreement is the finding, and a report that reconciled them would hide it. `git
    status` is scoped to the directories the delegation wrote into, never the whole
    workspace. Why: ADR-0007.
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

    `sort_keys` because two dicts that differ only in insertion order are the same call,
    and `default=str` because this must never raise on something a model sent -- an
    unserialisable argument compares by its repr, which at worst misses a dedup.
    """
    return json.dumps(call.input, sort_keys=True, default=str)


@dataclass(frozen=True, slots=True)
class AgenticDispatch:
    """What one agentic delegation did, counted by the server rather than told by the model.

    The first four fields match `Dispatch` so a caller reads both the same way; the rest is
    the ledger ADR-0007 asks for, extended from exit codes to the economics of the loop:
    turns actually taken, tools actually run, results actually evicted. Why: ADR-0007.
    """

    response: CanonicalResponse
    effort: str
    attempts: int
    reasoning_exhausted: bool = False
    turns: int = 1
    tool_calls: int = 0
    # `tool_calls` split by tool name, name-sorted. A tuple rather than a dict because this
    # dataclass is frozen and a dict field would be frozen in name only -- a caller holding
    # the result could rewrite the ledger it was handed. Sorted so two delegations that made
    # the same calls render identically, whatever order the model made them in.
    tool_calls_by_name: tuple[tuple[str, int], ...] = ()
    tool_errors: int = 0
    deduped: int = 0
    evicted: int = 0
    # The whole run, where `response` describes only the turn that answered. Both are kept
    # and neither is derivable from the other: a caller asking "was this answer truncated"
    # wants the answering turn, and one asking "what did this delegation cost" wants these.
    # ADR-0058; with only `response`, a twelve-turn delegation would report one turn's usage.
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int | None = None
    hit_turn_limit: bool = False
    # The resolved budget `turns` was allowed to reach, after the agent file and the
    # operator clamp. Carried beside the count rather than derived from it: "six turns"
    # and "six of six turns" are the same run and different news.
    max_turns: int = 0
    # ADR-0007. `last_bash_exit` is None for "nothing exited" -- no command ran, or the last
    # one was killed on timeout -- which 0 cannot mean, being a real exit code.
    bash_calls: int = 0
    bash_failures: int = 0
    # Counted inside `bash_failures` as well, and reported separately because the two
    # answer different questions: how many calls failed, and how many of those the exit
    # code alone would not have shown.
    bash_masked_failures: int = 0
    failed_calls: int = 0
    last_bash_exit: int | None = None
    # Zero means never, rather than "on turn zero" -- turns are numbered from one, so the
    # sentinel cannot collide with a real answer. Reported because a delegation that was
    # tightened or nudged produced its answer under different conditions from one that was
    # not, and the caller cannot tell from the text which they are reading.
    overflow_tightened_at: int = 0
    overflow_nudged_at: int = 0
    # Empty unless the caller asked. Absent-not-zero-filled, the same convention
    # `server.py::_loop_ledger` documents: an empty ledger the caller did not request must
    # not read as a delegation that did nothing.
    diagnostics: tuple[TurnDiagnostic, ...] = ()
    rereads: tuple[RereadAfterEviction, ...] = ()


def _assistant_blocks(cfg: Config, response: CanonicalResponse) -> tuple[ContentBlock, ...]:
    """The model's own turn, as it goes back into the history.

    Reasoning goes back only when `resend_reasoning` says so; its description has why.
    Tool calls are never dropped -- a result whose `tool_use` is missing is a history some
    backends reject outright.
    """
    if cfg.resend_reasoning:
        return response.content
    return tuple(b for b in response.content if not isinstance(b, ThinkingBlock))


# What each tool call is timed by. Looked up per call, so a test can hand it the same fake
# clock the server reads.
_tool_clock: Callable[[], float] = time.monotonic


def _run_one_call(
    cfg: Config,
    call: ToolUseBlock,
    allowed: frozenset[str],
    cached: dict[tuple[str, str], _CachedResult],
    policy: BashPolicy,
) -> tuple[ToolResultBlock, str, int | None]:
    """Execute one tool call, or serve it from what an identical earlier one returned.

    Dedup is byte-identical on name and arguments, and applies only to tools declared
    `cacheable` -- see `RegisteredTool`. Anything else not only misses the cache but
    *clears* it: a write invalidates every read taken before it, and serving a file from
    before its own overwrite is a worse failure than paying for the read again.

    Known gap, recorded rather than papered over: a re-read of the same file at a different
    offset is a different argument set and is not caught. Upstream's version has the same
    hole. Closing it needs range tracking, which is its own piece of work.

    The third value is the call's own milliseconds, `None` when the cache answered.
    """
    key = (call.name, _dedup_key(call))
    entry = cached.get(key)
    if entry is not None:
        # Still "repeat" in both cases: nothing ran either way, and the outcome vocabulary
        # is what the ledger and the viewer read. What differs is only what comes back.
        body = EVICTED_REPEAT if entry.evicted else REPEAT_PREFIX + entry.content
        return ToolResultBlock(tool_use_id=call.id, content=body), "repeat", None

    started = _tool_clock()
    result = execute_tool(cfg, call, allowed, policy)
    ms = int((_tool_clock() - started) * 1000)
    tool = REGISTRY.get(call.name)
    if tool is None or not tool.cacheable:
        # Unknown or side-effecting. Everything cached so far may describe a world that no
        # longer exists, and there is no way from here to tell which entries those are.
        cached.clear()
    elif not result.is_error:
        # Errors are not cached. Several are transient by nature -- a file that does not
        # exist yet is the obvious one -- and caching a refusal would make it permanent for
        # the rest of the delegation.
        cached[key] = _CachedResult(result.content, call.id)
    return result, "error" if result.is_error else "ran", ms


# How many of a turn's calls may be in flight together. A constant rather than a setting,
# because the number that decides it is the size of the batch the *model* produced, which an
# operator does not choose -- measured batches here are two and three. The cap exists so a
# pathological batch cannot open a thread per call, not as something to tune.
MAX_CONCURRENT_TOOL_CALLS = 8


def _pooled_groups(calls: tuple[ToolUseBlock, ...]) -> Iterator[tuple[ToolUseBlock, ...]]:
    """One turn's calls, split into groups that may run together, in the order given.

    A group is a run of consecutive *cacheable* calls. Cacheable is exactly the property
    that makes overlapping safe, and not by coincidence: `_run_one_call` clears the whole
    dedup cache after any non-cacheable call, because a write invalidates every read taken
    before it. That clear is a barrier on the turn's own history -- the call performing it
    has to see every earlier call's effect and be seen by every later one -- so a
    non-cacheable call is a group of one and stays exactly where the model put it.

    `run_bash` and the write tools are never cacheable. `read_git` is read-only and not
    cacheable either, since it reads a tree `run_bash` may just have committed to, so it
    runs alone as well. Conservative, and it costs nothing: the batches
    that cost are `search_files`, which is cacheable. A tool absent from the registry also
    lands alone, which keeps the unknown-tool path exactly where it was.
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


def _run_group(
    cfg: Config,
    group: tuple[ToolUseBlock, ...],
    allowed: frozenset[str],
    cached: dict[tuple[str, str], _CachedResult],
    policy: BashPolicy,
) -> list[tuple[ContentBlock, str, int | None]]:
    """One group's calls, overlapped, with `cached` read and written only on this thread.

    Not a lock. A lock would keep the dict structurally intact and still let two identical
    calls both miss and both run: the dedup guarantee is a compound read-then-write, and the
    only way to keep it is to leave both halves off the workers entirely. So the cache is
    consulted before dispatch, the misses are executed, and the results are stored on the
    way out -- which also means a duplicate inside one batch is dispatched once and its
    second occurrence assembled from what the first stored, exactly as a serial run does.

    Every call here is cacheable by construction, so none of them clears the cache and the
    ordering `_run_one_call` protects cannot be observed to change.

    Each executed call carries its own milliseconds, timed on the thread that ran it, so
    overlapped calls are not charged the group's span; a cached one carries `None`.
    """
    keys = [(c.name, _dedup_key(c)) for c in group]
    first_for: dict[tuple[str, str], ToolUseBlock] = {}
    for call, key in zip(group, keys, strict=True):
        if key not in cached:
            first_for.setdefault(key, call)

    fetched: dict[tuple[str, str], tuple[ContentBlock, int]] = {}
    pending = list(first_for.items())
    if len(pending) == 1:
        # One miss needs no pool, and saying so keeps the common two-call batch where one
        # call is a repeat from paying for a thread it would not use.
        key, call = pending[0]
        started = _tool_clock()
        fetched[key] = (
            execute_tool(cfg, call, allowed, policy),
            int((_tool_clock() - started) * 1000),
        )
    elif pending:
        def run(item: tuple[tuple[str, str], ToolUseBlock]) -> tuple[ContentBlock, int]:
            started = _tool_clock()
            block = execute_tool(cfg, item[1], allowed, policy)
            return block, int((_tool_clock() - started) * 1000)

        workers = min(len(pending), MAX_CONCURRENT_TOOL_CALLS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # `map`, not `as_completed`: the blocks are consumed positionally downstream and
            # the model matches them by id, so completion order is not an ordering at all.
            fetched = dict(zip((k for k, _ in pending), pool.map(run, pending), strict=True))

    out: list[tuple[ContentBlock, str, int | None]] = []
    for call, key in zip(group, keys, strict=True):
        entry = cached.get(key)
        if entry is not None:
            body = EVICTED_REPEAT if entry.evicted else REPEAT_PREFIX + entry.content
            out.append((ToolResultBlock(tool_use_id=call.id, content=body), "repeat", None))
            continue
        result, ms = fetched[key]
        if isinstance(result, ToolResultBlock) and not result.is_error:
            cached[key] = _CachedResult(result.content, call.id)
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
) -> tuple[list[ContentBlock], tuple[ToolCallRecord, ...]]:
    """One turn's tool calls, run in order. Returns the result blocks and their records.

    Separate from `_run_one_call` because the ledger entry belongs to the turn rather than
    to the call: it records the path argument the model asked for, not the path the policy
    resolved, and that distinction is worth keeping in one visible place. The two can
    differ, and it is the model's own argument that the abort report needs -- reconciling
    what it *believed* it wrote against what is on disk is the point of that report.

    This is also the one place where the arguments and the refusal text are both in scope,
    which is why the record is built here rather than reconstructed later. Nothing
    downstream of this line still holds the result block, so a record assembled anywhere
    else would have to be assembled from less.
    """
    results: list[ContentBlock] = []
    records: list[ToolCallRecord] = []
    for group in _pooled_groups(calls):
        # A group of one keeps the original path, cache clear and all. Only a genuine batch
        # of independent reads takes the other branch, which is the case the pool is for.
        outcomes = (
            [_run_one_call(cfg, group[0], allowed, cached, policy)]
            if len(group) == 1
            else _run_group(cfg, group, allowed, cached, policy)
        )
        # The ledger is written here, on one thread, in the order the model asked -- a
        # worker appending to `watch` would race it and reorder what the abort report reads.
        for call, (block, outcome, ms) in zip(group, outcomes, strict=True):
            result = block if isinstance(block, ToolResultBlock) else None
            watch.called(call, outcome, result)
            records.append(tool_call_record(call, outcome, result, ms=ms))
            results.append(block)
    return results, tuple(records)


async def _reread_concurrency(
    concurrency_now: Callable[[], Awaitable[int]],
    current: int,
    decode_rate: DecodeRate,
    rate_history: RateHistory | None,
) -> tuple[int, DecodeRate]:
    """The concurrency this turn meets, and the rate to price it from.

    A read that fails keeps the last good figure. Until a turn of this delegation's own has
    been measured the rate is still the seed, so it is re-seeded at the new figure;
    otherwise the priced row would name one concurrency beside another one's rate.
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


async def run_agentic_loop(  # noqa: PLR0913, PLR0915 -- three of the nine are test
    # seams, and the statement count is the turn lifecycle: dispatch, observe, decide,
    # run tools, report. Splitting it would move steps out of the order they happen in.
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
    diagnostics: bool = False,
    report_progress: Callable[[int, int], Awaitable[None]] = _no_progress,
    on_alive: Callable[[float, int, float, int, int, float | None], Awaitable[None]] | None = None,
    on_priced: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_pool: Callable[[int | None], None] | None = None,
    rate_history: RateHistory | None = None,
    expected_concurrency: int = 1,
    concurrency_now: Callable[[], Awaitable[int]] | None = None,
    on_turn_done: Callable[[TurnDiagnostic, str, float], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_token: Callable[[], None] | None = None,
    tick_sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> AgenticDispatch:
    """Turns, until the model answers or the budget runs out.

    One turn is one model reply plus any tools it called. The loop ends when a reply
    carries no tool calls -- that reply is the answer -- or when the last turn is reached,
    which is declared with no tools at all so the model cannot end on a call nobody will
    run. `hit_turn_limit` says which of the two happened, because an answer written under a
    withdrawn toolset is worth reading differently from one the model chose to give.

    It is `turn == turns` and nothing else. It does not require a tool call on that final
    reply, which a backend offered no tools does not produce -- requiring one would leave
    the flag false in exactly the case it exists to report, and true only when a backend
    ignores the withdrawal. A delegation that finishes on its last turn reports the limit
    even if it would have stopped there anyway; that direction costs a reader one look at
    `max_turns`, where the other costs a truncated answer read as a complete one.

    Recovery from an answer that came back empty is `dispatch_with_recovery`'s, per turn,
    unchanged from the one-shot path. The loop does not reimplement retry, backoff or
    step-down; it supplies a builder and counts what came back.

    **One deadline covers the whole delegation.** It is taken once here and passed to every
    turn, so `dispatch_timeout` bounds the delegation rather than each turn -- a per-turn
    budget would make the real bound the timeout times `max_turns`, which at the defaults
    is a day and a half.

    `report_progress` is called once per turn and `on_alive` runs on a timer beside the
    loop, and both reset the client's stdio idle timer (ADR-0018): without them a
    delegation that outlasts it is abandoned by the client while the server is still
    working. Both are injected rather than imported -- `loop.py` holds no MCP imports, and
    a test needs to see the calls without a client. Why: ADR-0018.

    Effort reported is the level of the *last* turn, which is the one that produced the
    answer. A step-down on turn three does not persist into turn four: the next turn is a
    different request, and re-deriving the level from the caller's argument each time is
    what keeps one bad turn from silently downgrading the rest of the delegation.
    """
    resolved_effort = resolve_effort(cfg, entry, effort)
    turns = resolve_max_turns(cfg, max_turns, allowed)
    specs = declared_tools(cfg, allowed)
    deadline = clock() + cfg.dispatch_timeout
    chunks = 0
    reasoning_chunks = 0
    last_progress: float  # bound below the seed, where the stall clock starts

    def stall_left() -> float:
        return cfg.stall_timeout - (clock() - last_progress)

    def token_arrived(kind: str = "answer") -> None:
        """The second progress signal, and the finer of the two (ADR-0072).

        Turn completion still counts -- nothing here replaces it -- but it cannot see inside
        a turn, so on its own it would kill a pass that had completed many turns in one long
        one; token arrival can, and unlike the notification and the keepalive it is real.
        `kind` names which half of the reply a frame carried, so the reasoner can tally
        thinking separately from answering, and the default keeps a backend that still calls
        `on_token()` with no argument working. Why: ADR-0072.
        """
        nonlocal last_progress, chunks, reasoning_chunks
        last_progress = clock()
        chunks += 1
        if kind == "reasoning":
            reasoning_chunks += 1
        if on_token is not None:
            on_token()

    def streamed() -> tuple[int, int, float | None]:
        """What has arrived this delegation, and how long since. Chunks, not tokens.

        Frames, because that is what the wire carries one of and the only honest token
        count lands in the final usage frame. Not reset per turn: a reader watching a
        delegation wants to know it is still producing, and a counter that restarted at
        every turn boundary would read as though it had stopped.

        The reasoning count rides beside the total so a watcher can tell thinking from
        answering; the two halves are the point of the split.
        """
        return chunks, reasoning_chunks, None if not chunks else clock() - last_progress

    bash_policy = policy or BashPolicy()

    history: list[Message] = [Message("user", (TextBlock(delegation.render()),))]

    cached: dict[tuple[str, str], _CachedResult] = {}
    watch = _Watch(diagnostics=diagnostics)
    guard = _OverflowGuard(cfg, entry)
    dispatch: Dispatch | None = None
    text_before_nudge = ""
    nudge_pending = False
    turn = 0

    async def turn_done(text: str, backend_seconds: float) -> None:
        """One finished turn, for anything watching this delegation as it runs.

        Called from two places because a turn ends in two ways: with tool calls, once
        their outcomes are attached, and without them, at the break. A single call site
        would have to choose one and would silently omit the other -- and the turn that
        ends without tool calls is the one carrying the final answer, so omitting it
        would drop the part a reader most wants.

        The text is a parameter rather than read from the enclosing `dispatch`, which is
        rebound every iteration: closing over it would be correct only for as long as
        every call stayed inside the turn that set it.
        """
        nonlocal last_progress
        # Before the guard, and outside it. A turn finished whether or not anybody is
        # listening, and putting this inside the `on_turn_done` branch would mean the
        # stall deadline only advanced for callers that happened to pass a callback --
        # so a delegation with no observer would be killed mid-progress.
        last_progress = clock()
        if on_turn_done is not None and watch.turns:
            await on_turn_done(watch.turns[-1], text, backend_seconds)

    # One scrape, before the first turn, and never again: every turn after this replaces
    # the seed with what this delegation itself achieved (ADR-0055). Eagerly rather than
    # lazily inside the first turn, so the scrape is not charged to a turn that did not
    # ask for it -- which is a claim about accounting only, and holds because the stall
    # clock below starts after this line rather than before it.
    decode_rate = await seed_decode_rate(
        backend, rate_history, expected_concurrency, on_pool=on_pool,
        label_trusted=cfg.admission_idle_hold > 0,
        fallback=cfg.rate_fallback_tok_s,
    )

    # When a turn last *finished*, deliberately not when one last started (ADR-0047): the
    # notification fires at the top of a turn, so it would reset the clock on entry to the
    # very turn that then wedges, and the keepalive proves liveness on a timer regardless
    # of progress, which a no-progress deadline must not count as progress. Set below the
    # seed, not above it: started above, the clock was already running while the scrape was
    # awaited, so a slow endpoint spent turn 1's silence budget on metrics before turn 1
    # began. Why: ADR-0047.
    last_progress = clock()

    # The heartbeat, beside the loop rather than inside it. `run_one_shot` has one too
    # (ADR-0018); this path reports only at the top of each turn, so a single long turn is
    # silent for its whole duration and is bounded by nothing tighter than the delegation
    # deadline -- far past the client's stdio idle timeout, at which point the client
    # abandons the call, nothing reaches the server, and the slot is held to the end.
    #
    # Created as `None` rather than early-returning the way the one-shot does, because
    # there the guarded part is one `await` and here it is the whole loop: duplicating it
    # to avoid a nullable task would be two copies of the turn lifecycle. Why: ADR-0018.
    tools_running = False
    beat = asyncio.create_task(_keepalive(
        cfg, on_alive, clock,
        # Deliberately not `budget_seconds`, though the shapes are close. That one sizes
        # one reply and counts the delegation deadline only, because a stream that is
        # producing is not silent and must not be charged the stall clock. A countdown
        # shown to a reader has the opposite job: name whichever deadline will actually
        # end the run, and since ADR-0099 that is usually the stall one -- except while
        # tools run, when nothing reads the stall clock and only the deadline can fire.
        lambda: (max(deadline - clock(), 0.0)
                 if tools_running
                 else max(min(stall_left(), deadline - clock()), 0.0)),
        streamed,
    )) if on_alive else None
    current = expected_concurrency
    try:
        while turn < turns:
            turn += 1
            watch.turn = turn
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
            # The half that was missing. Eviction rewrote the history and the dedup cache
            # never heard, so a repeat handed the whole result straight back into the
            # window the trim had just made room in (ADR-0080). Same diff the ledger reads,
            # so the two cannot disagree about what was dropped.
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
                    # Always offered, and forbidden rather than withdrawn on the final turn
                    # (ADR-0057). The intent is unchanged -- a model that ends on a tool call
                    # nobody will run has spent the whole delegation and returned nothing
                    # readable -- but withdrawing changes the front of the prompt, and the
                    # stack caches prefixes, so dropping the tool block would re-prefill the
                    # whole prompt on the one turn that must fit a whole answer in one
                    # deadline. Why: ADR-0057.
                    tools=specs,
                    tool_choice="none" if _final else "auto",
                )

            backend_started = clock()
            # Re-derived every turn rather than once. `stall_left` is reset by the previous
            # turn completing, so this tracks the clock the model is actually racing, and
            # the rate below tracks what the cluster is giving us as other tenants come and
            # go. A ceiling fixed at entry would be a guess about the rest of the run.
            ceiling = decode_rate.ceiling(cfg, budget_seconds(
                cfg, dispatch_left=deadline - clock()
            ))
            # Recorded before the call, not after it. A turn killed at a deadline having
            # finished nothing writes no `turn` event, so pricing reported afterwards is
            # reported only for the turns that never needed explaining.
            # Resolved once, per turn, so the priced row and the send are the same number:
            # the row is emitted before the dispatch, and passing the budget down stops
            # `dispatch_with_recovery` from resolving it a second way.
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
            # The backend call alone, separate from the turn's wall clock. Tokens per second
            # over the whole turn would fold tool execution and any retry wait into the
            # divisor and report the cluster as slower than it is -- and a throughput number
            # that is quietly measuring the wrong interval is worse than none, because it
            # gets believed.
            backend_seconds = max(clock() - backend_started, 0.0)
            # What this turn actually achieved, which replaces the cluster's lifetime mean
            # for every turn after it. The token count comes from one attempt (ADR-0014), so
            # it is divided by the answering attempt, not `backend_seconds`, which spans
            # every recovery stage and transport retry inside them: the halved rate would
            # seed the next delegation's first turn -- the one with no observation of its own
            # and the only one that can die before making any. The interval is the tokens'
            # own where the adapter could time it and the whole attempt where it could not:
            # `decode_seconds` is last token minus first and excludes prefill, while
            # `answered_seconds` includes it, so on a short answer over a large prompt the
            # rate describes the queue rather than the decoder (ADR-0070). `None` means the
            # adapter does not stream, not that the interval was zero. Why: ADR-0014,
            # ADR-0070.
            measured = dispatch.response.decode_seconds
            interval = measured or dispatch.answered_seconds or backend_seconds
            decode_rate.observe(dispatch.response.output_tokens, interval)
            # And remembered past this delegation, tagged with how contended it was --
            # but only where nothing else is filling that memory. `RateSampler` files one
            # sample per scrape, and a completed turn files one per *stream*, so leaving
            # both on would put several readings on one moment at one concurrency and one
            # at another: the bucket widths stop being comparable, which is precisely the
            # defect the sampler exists to remove. Off by default, therefore, and back the
            # moment an operator turns sampling off, because a memory with no feeder at all
            # is the cold start that costs every first turn.
            if rate_history is not None and cfg.rate_sample_seconds <= 0:
                # Tokens and the interval, not the quotient. Dividing here is what leaves the
                # memory unable to tell a throughput measurement from a turn too short to
                # be one, and its own floor is stricter than `DecodeRate`'s above.
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

            # Off the event loop, which is what lets the heartbeat above fire at all during
            # a tool call. `_run_calls` is synchronous and `run_bash` reaches `subprocess.run`
            # in `sandbox.py`, so on the loop a command bounded by `run_bash_timeout` would block it
            # for its whole duration -- no timer task, no `report_progress`, and no stdio
            # traffic for any other delegation admitted alongside it. A heartbeat that goes
            # quiet during the longest blocking operation in the system is the check that
            # cannot fail, in its heartbeat form.
            #
            # One thread for the whole batch, not one per call: `_run_calls` runs them in
            # order and that order is what the model sees. The only thing running beside
            # it is the timer, which touches neither `cached` nor `watch`.
            tools_running = True
            results, outcomes = await asyncio.to_thread(
                _run_calls, cfg, calls, allowed, cached, watch, policy=bash_policy
            )
            tools_running = False
            watch.turn_tools(outcomes)
            await turn_done(dispatch.response.text, backend_seconds)
            results.append(TextBlock(countdown_line(turns - turn)))

            nudge_pending = guard.nudge_due(turn=turn)
            if nudge_pending:
                # Appended alongside the countdown, never instead of it: one counts turns and
                # the other counts room, and a delegation can be short of either alone.
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
        # The only place these numbers exist on a failure. `watch` is a local of this
        # function and is discarded as the exception propagates, and the `AgenticDispatch`
        # above -- which does carry `turns` and `tool_calls` -- is only ever built when the
        # loop finishes. Without this, a delegation that runs out of time would report which
        # setting fired and nothing about what it had achieved, which at the call site is
        # indistinguishable from an endpoint that never answered -- and the honest response
        # to that, to stop using the server, is the wrong call.
        #
        # Wrapped here rather than at the five raise sites inside `complete_with_retry`,
        # which is one handler instead of five and keeps those signatures free of a counter
        # they have no way to read.
        #
        # `watch.turn` is the turn *now running*, so completed turns is one fewer. It is
        # deliberately not clamped to the stall window: the stall clock resets on turn
        # completion, so "no turn completed" means none within `stall_timeout`, not none
        # ever, and reporting the total is what separates a task too large to finish a turn
        # from a backend that produced nothing at all.
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
                # Ours, not the caller's -- as in `run_one_shot`. A cancelled delegation
                # reaches this `finally` too, and re-raising would replace whatever
                # actually ended it.
                pass
