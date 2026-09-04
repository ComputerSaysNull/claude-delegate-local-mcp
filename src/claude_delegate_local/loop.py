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

What this module already owned, and still does, is resolution -- which model, which
effort, which budget -- because that has to happen exactly once and be sent explicitly,
and assembly: the order of the parts of the prompt, which is load-bearing for the
cluster's prefix cache and so is decided here rather than wherever a part happens to be
produced. M2 grew this file with the files block; M4 grows it again rather than
replacing it.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
from pathlib import Path

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

# The system prompt is a constant, and must stay one. The cluster caches prefixes, so a
# single dynamic byte -- a timestamp, a session id, a turn counter -- silently disables
# that with no error and no symptom beyond slower prefill. Dynamic content goes in the
# tail, inside the message. ADR-0011.
#
# One constant, covering both the files and no-files shapes rather than one for each. Two
# prompts would be two prefixes, and a caller that alternates between the shapes would
# miss the cache on every other call -- for wording that has nothing to do with the
# difference. "Any files" is simply vacuous when there are none.
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


# The second static prompt, and deliberately not a variant of the first. The one-shot
# prompt tells the model it has no tools and no second turn, which would be a lie here --
# and the two shapes are never alternated by one caller the way the files and no-files
# shapes are, so they cost nothing by being separate prefixes.
#
# Static, byte for byte, exactly as ADR-0011 requires: no turn number, no counter, no
# budget. The countdown the model needs lives in the tail, on the message carrying tool
# results, where a changed byte costs nothing that was not already changing.
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
        # `stage` carries its own preposition. It used to be interpolated after a literal
        # "while", which read correctly for the three waiting stages and produced "while
        # with no turn completed" for the stall -- the one message a reader is most likely
        # to be staring at.
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
        return DispatchTimedOut(
            self.elapsed, self.limit, self.stage, self.setting, self.remedy,
            turns=turns, tool_calls=tool_calls, last_tool=last_tool,
        )


@dataclass(frozen=True, slots=True)
class Delegation:
    """What to delegate: the task, the material assembled for it, and the agent's own prompt.

    A value object rather than three parameters, because these are the parts of one prompt
    and the order they go in is load-bearing (ADR-0011). `render` is what keeps that rule in
    one place. It has to be a method rather than a convention: the parked version of this
    docstring claimed the ordering lived in exactly one place while two call sites -- the
    one-shot builder and the turn loop -- each concatenated the parts themselves, and the
    agent body would have been a third segment to add to both. Two sites that must agree and
    are not made to are a drift waiting to happen.
    """

    task: str
    files_block: str = ""
    agent_body: str = ""

    def render(self) -> str:
        """The user message: agent body, then files, then task. Never the system prompt.

        The system prompt is a byte-for-byte constant that the cluster caches, so nothing
        that varies per delegation may enter it -- the agent body included, however much it
        reads like one (ADR-0011). Task last because it varies most between calls, so a
        changed byte invalidates the least.
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


def resolve_max_tokens(
    cfg: Config, entry: ModelEntry, effort: str, explicit: int | None = None
) -> int:
    """The reply budget: the caller's number, else the configured one raised at high effort.

    Reasoning is generated against this same budget, so a high effort with a low cap
    produces an answer that is empty because it thought until it ran out. ADR-0014.

    Precedence, most specific first: the call argument, then the agent's frontmatter (M6,
    which resolves into `explicit` when it exists), then the configured default -- which
    comes **last** rather than first. ADR-0024: an operator lowering the ceiling must not
    suppress the floor that stops heavy-reasoning models returning nothing, so the floor is
    applied as a `max()` over the configured value and not as an alternative to it.

    An explicit number is *not* raised to that floor. It is the most specific instruction
    there is, and silently multiplying it by thirty would make the argument advisory. The
    caller who asks for a small budget at max effort still gets ADR-0014's recovery, which
    retries at the floor -- so the cost of being wrong here is one extra dispatch, and the
    cost of overriding them is an argument that does not mean what it says.

    The per-model cap applies to every path, last, because it is what the wire will accept.
    """
    if explicit is not None:
        if explicit < 1:
            raise InvalidDelegation(
                f"max_tokens={explicit} must be at least 1. A budget of nothing cannot "
                "produce an answer."
            )
        return entry.cap_tokens(explicit)
    budget = cfg.max_tokens
    if effort in ("high", "max"):
        budget = max(budget, cfg.thinking_max_tokens_floor)
    return entry.cap_tokens(budget)


def build_one_shot_request(
    *, delegation: Delegation, effort: str, max_tokens: int, temperature: float
) -> CanonicalRequest:
    """One user message, no tools, and a system prompt that does not vary.

    The static system prompt ahead of `Delegation.render` gives the sequence ADR-0011
    fixes: system, agent body, files block, task last. The ordering itself lives on
    `Delegation`, so this function and the turn loop cannot disagree about it.
    """
    body = delegation.render()
    return CanonicalRequest(
        system=SYSTEM_PROMPT_ONE_SHOT,
        messages=(Message("user", (TextBlock(body),)),),
        max_tokens=max_tokens,
        effort=effort,
        temperature=temperature,
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


async def complete_with_retry(  # noqa: PLR0913 -- four of the seven are test seams
    cfg: Config,
    backend: Backend,
    request: CanonicalRequest,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
    deadline: float | None = None,
    stall_left: Callable[[], float] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[CanonicalResponse, int]:
    """Send until it answers, a failure is not worth repeating, or the attempts run out.

    Returns the response and how many real calls it took. That count is server-captured
    ground truth about what this dispatch actually cost, in the spirit of ADR-0007, and it
    is the only honest way for a caller to see that a quiet success was really three tries.

    On exhaustion the last real exception propagates unchanged. Wrapping it in something
    that says "gave up after N" would hide which failure it actually was, and the four
    kinds are distinguishable precisely so the layer above can act on them.

    `sleep` and `jitter` are injected for the reason `client` is injected into the adapter
    and `cache` into the server: without that, a test of the retry logic sleeps for real,
    and a test of the cap cannot pin the random factor.

    `deadline` is an absolute reading of `clock`, set by the caller that owns the whole
    delegation, and it bounds the sum this function used to leave unbounded: attempts, and
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
        Measuring from `started` therefore reported one turn's elapsed time beside the
        whole delegation's limit: "abandoned after 1372.8s, past the ... of 3600s" --
        a number that is not past it, under a remedy ("raise that setting") the number
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
        """The tighter of the two deadlines, which is what each attempt is given."""
        s = None if stall_left is None else stall_left()
        both = [x for x in (remaining(), s) if x is not None]
        return min(both) if both else None

    while True:
        left = remaining()
        if left is not None and left <= 0:
            raise DispatchTimedOut(spent(), cfg.dispatch_timeout, "while waiting on the backend")
        stalled()
        attempts += 1
        try:
            cap = ceiling()
            if cap is None:
                return await backend.complete(request), attempts
            # The per-attempt ceiling. `turn_timeout` already bounds one call inside the
            # adapter's client, but it is a fixed budget that knows nothing about how much
            # of the delegation is left, so without this the deadline could still be
            # overshot by a whole turn.
            return await asyncio.wait_for(backend.complete(request), timeout=cap), attempts
        except TimeoutError as e:
            # Which of the two expired decides the diagnosis, so ask before blaming the
            # ceiling: a stall inside a raised `dispatch_timeout` would otherwise be
            # reported as a delegation that ran too long, sending an operator to raise a
            # setting that had nothing to do with it.
            stalled()
            if deadline is None:
                # Nothing bounded this attempt but the adapter's own client budget, so
                # the delegation deadline is not what expired. Naming it would send an
                # operator to raise a setting that had no part in this.
                raise DispatchTimedOut(
                    spent(), cfg.turn_timeout, "while waiting on one turn",
                    setting="DELEGATE_TURN_TIMEOUT",
                ) from e
            raise DispatchTimedOut(
                spent(), cfg.dispatch_timeout, "while waiting on the backend"
            ) from e
        except (BackendUnavailable, BackendRefused) as e:
            if not _is_retryable(e) or attempts >= cfg.retry_max_attempts:
                raise
            wait = _delay_before_retry(cfg, e, attempts, jitter)
            left = ceiling()
            if left is not None and wait >= left:
                # Which deadline made the wait impossible decides the diagnosis, exactly
                # as it does for a timed-out attempt above. `ceiling()` is the tighter of
                # the two, so reaching here says nothing about which one it was -- and
                # this branch used to answer "the ceiling" unconditionally.
                stalled()
                # Sleeping first would burn the rest of the budget and then report the
                # deadline, naming a wait this function chose rather than the work.
                raise DispatchTimedOut(
                    spent() + wait, cfg.dispatch_timeout, "while waiting to retry"
                ) from e
            await sleep(wait)


@dataclass(frozen=True, slots=True)
class Dispatch:
    """What one delegation actually did, as opposed to what it was asked to do.

    A value object rather than a widening tuple. `effort` was already not always the level
    the caller asked for, `attempts` is not always one, and `reasoning_exhausted` is a
    verdict only this module is in a position to reach. Returning a shape lets each of
    those be named at the call site instead of positioned.

    `reasoning_exhausted` is deliberately narrow. It is true only when the answer was still
    empty at a length stop *after* a larger budget and a lower effort had both been tried --
    ADR-0014's `reasoning_exhausted_budget`, and the only case where the phrase is earned.
    An empty answer that nothing was tried on is a mechanical fact, not this verdict.
    """

    response: CanonicalResponse
    effort: str
    attempts: int
    reasoning_exhausted: bool = False


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
    max_tokens: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dispatch:
    """One dispatch, plus the two mitigations for an answer that ran out of room.

    Three stages, following ADR-0014: send; on the exhaustion signature retry once at a
    larger budget; if that is still empty, step effort down one level and send once more.
    Deliberately not a cascade through every level -- each stage is a real dispatch at the
    largest budget the model permits, and a four-stage climb down would cost more than the
    answer is worth while the caller waits.

    Stepping effort down is not the cheap fallback it looks like. The level is part of the
    rendered prompt, so `prompt_tokens` moves with it (measured, JOURNAL 2026-08-26): a
    stepped-down dispatch misses the cluster's prefix cache entirely and pays a fresh
    prefill on top of the generation. That is why it is the last resort rather than the
    first, and why the budget retry -- which keeps the level, and the prefix -- comes first.

    `build` takes the effort level and the budget and returns the request to send at them,
    which is what lets one-shot and one turn of the agentic loop share this. Everything
    that differs between them -- the system prompt, the history, whether tools are declared
    -- is decided by the caller's builder, and everything that differs between the *stages*
    is decided here. Turning this into two copies is how the turn loop would end up
    diagnosing exhaustion differently from the one-shot path.

    Attempts accumulate across all three stages and every transport retry inside them, so
    the number reported is what this dispatch really cost. What is *not* summed is the
    token counts: those come from the attempt that answered. ADR-0014 says the retry must
    not charge the turn budget, so a turn is charged for the answer it got.
    """
    asked_budget = resolve_max_tokens(cfg, entry, effort, max_tokens)
    attempts = 0

    response, spent = await complete_with_retry(
        cfg, backend, build(effort, asked_budget),
        sleep=sleep, deadline=deadline, stall_left=stall_left, clock=clock,
    )
    attempts += spent
    if not is_empty_at_length(response):
        return Dispatch(response, effort, attempts)

    # Stage 2: the same level, more room. `thinking_max_tokens_floor` documents itself as
    # the size retried after an empty answer, so there is no second setting for it.
    floor = entry.cap_tokens(max(2 * asked_budget, cfg.thinking_max_tokens_floor))
    if floor > asked_budget:
        response, spent = await complete_with_retry(
            cfg, backend, build(effort, floor),
            sleep=sleep, deadline=deadline, stall_left=stall_left, clock=clock,
        )
        attempts += spent
        if not is_empty_at_length(response):
            return Dispatch(response, effort, attempts)
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
        return Dispatch(response, effort, attempts)

    response, spent = await complete_with_retry(
        cfg, backend, build(stepped, resolve_max_tokens(cfg, entry, stepped, max_tokens)),
        sleep=sleep, deadline=deadline, stall_left=stall_left, clock=clock,
    )
    attempts += spent
    return Dispatch(response, stepped, attempts, is_empty_at_length(response))


async def run_one_shot(  # noqa: PLR0913 -- see the note below the docstring
    cfg: Config,
    entry: ModelEntry,
    backend: Backend,
    delegation: Delegation,
    *,
    effort: str | None = None,
    max_tokens: int | None = None,
    on_alive: Callable[[float, int], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
    is the one call shape measured as still able to reach that idle timeout.
    """
    resolved = resolve_effort(cfg, entry, effort)
    origin = clock()
    deadline = origin + cfg.dispatch_timeout

    def stall_left() -> float:
        """A one-shot has exactly one unit of progress to make, and makes it at the end.

        So there is no turn completion to reset this, and it counts from entry: the
        effective bound becomes the tighter of `stall_timeout` and `dispatch_timeout`
        rather than the ceiling alone, which matters because that ceiling is now four
        hours and this path has nothing else holding it (ADR-0047).
        """
        return cfg.stall_timeout - (clock() - origin)

    def request_at(level: str, budget: int) -> CanonicalRequest:
        return build_one_shot_request(
            delegation=delegation,
            effort=level,
            max_tokens=budget,
            temperature=cfg.one_shot_temperature,
        )

    async def dispatch() -> Dispatch:
        return await dispatch_with_recovery(
            cfg, entry, backend, request_at,
            effort=resolved, max_tokens=max_tokens,
            sleep=sleep, deadline=deadline, stall_left=stall_left, clock=clock,
        )

    if on_alive is None:
        return await dispatch()

    # Everything from here down to the backend call is one sequential chain of awaits, so a
    # heartbeat cannot be another `await` inside it -- it has to run beside the chain and be
    # torn down with it. The `finally` is what makes that safe: cancel, then await the
    # cancellation, so no task outlives the dispatch it was reporting on. `run_agentic_loop`
    # now does the same around its turn loop; this was once the only concurrency here.
    beat = asyncio.create_task(_keepalive(cfg, on_alive, clock))
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
    on_alive: Callable[[float, int], Awaitable[None]],
    clock: Callable[[], float],
) -> None:
    """Say the delegation is still running, on a timer, until cancelled.

    Sleeps on `asyncio.sleep` rather than the injected `sleep` seam the retry path uses.
    That seam is stubbed instantly in tests, which would turn this into a busy loop
    hammering the callback while the test waited on something else.

    A failing callback stops the heartbeat and nothing else. It exists to protect a long
    delegation from being abandoned, and a heartbeat that instead killed one -- because a
    notification could not be delivered, which is not even evidence the client is gone --
    would be strictly worse than not having it.
    """
    started = clock()
    while True:
        await asyncio.sleep(cfg.keepalive_interval)
        try:
            await on_alive(clock() - started, cfg.dispatch_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            return


# --- the turn loop ------------------------------------------------------------------------


# What an evicted tool result is replaced by. The block stays and keeps its `tool_use_id`:
# some backends validate that every tool_use has a matching result, so dropping the block
# outright would make a long delegation fail at the wire rather than merely forget.
EVICTED_STUB = "[dropped from the history to keep it bounded. Call the tool again if needed.]"

# Prefixed to a result served from the dedup cache. Silently returning the identical bytes
# would teach the model nothing, and a model that repeats a call usually does so because it
# is stuck -- saying plainly that nothing new happened is what breaks that.
REPEAT_PREFIX = "[repeat of an identical earlier call; nothing was run again]\n"


async def _no_progress(turn: int, of: int) -> None:
    """The default when nobody is listening. Tests inject a recorder, `server.py` the real one."""


def resolve_max_turns(cfg: Config, explicit: int | None = None) -> int:
    """The turn budget: the caller's number, else the configured default, capped either way.

    The hard cap is applied to both, silently, because it exists to stop a caller -- or an
    agent file in M6 -- occupying the cluster for hours, and a limit that can be argued out
    of is not one. Refusing the call instead would be worse: the work is legitimate, only
    the number is not.
    """
    if explicit is None:
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


def evict_stale_tool_results(
    messages: tuple[Message, ...], keep: int
) -> tuple[tuple[Message, ...], int]:
    """Collapse all but the most recent `keep` tool results. Returns the history and a count.

    Every turn resends the whole history, so without this the cost of a delegation grows
    with the square of its length -- the tenth turn pays for the first nine results again.
    Oldest-first by count, which is what `keep_tool_results` says it is; a size-aware policy
    would evict differently and is not what the setting promises.

    Only the *content* goes. The block and its `tool_use_id` stay (see `EVICTED_STUB`), and
    an already-evicted result is not counted twice -- the count is what this call did, not
    how much of the history is stubbed, because the caller reports it as work performed.
    """
    positions = [
        (mi, bi)
        for mi, message in enumerate(messages)
        for bi, block in enumerate(message.content)
        if isinstance(block, ToolResultBlock)
    ]
    stale = positions if keep <= 0 else positions[:-keep]
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

    Here rather than in the system prompt, and that is not a stylistic choice: a turn
    counter in the prefix would change one byte of it per turn and silently cost a full
    prefill every time (ADR-0011). The tail is where dynamic content is free, because the
    tool results next to it were never going to be cached anyway.
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
    for one model reports the other as full while its history is still empty. Upstream
    shipped the flat version and this is the bug it produced.
    """
    return round(entry.context_window * cfg.overflow_reserve_fraction)


def projected_fraction(
    cfg: Config, entry: ModelEntry, last_input_tokens: int, pending_tokens: int
) -> float:
    """Share of the window this turn is projected to occupy, in [0, ...).

    **This is the only place the denominator is chosen, and it is `entry.context_window`.**
    Not `thinking_max_tokens_floor`, which is a reply budget and a different number for a
    different purpose; not a count of what this server evicted, which measures our own
    housekeeping rather than the model's room. Four of the five bugs this cost upstream
    were one shape -- a threshold over the wrong denominator -- so the denominator is read
    once, here, and every threshold is expressed against what this returns.

    Note that a registry entry omitting `context_window` inherits a default silently
    (`registry.py`), which is the local form of upstream's "architecture maximum" bug. That
    is why `context_overflow_enabled` is off by default rather than on.
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


@dataclass(frozen=True, slots=True)
class TurnDiagnostic:
    """What one turn cost and what it did, kept only when the caller asked for it.

    ADR-0007 extended from exit codes to context economics: every field here is something
    the server watched, never something the model reported about itself. The aggregate
    ledger on `AgenticDispatch` says a delegation ran nine turns and evicted twelve results;
    this says which turn the eviction happened on and what the prompt cost either side of
    it, which is the difference between knowing a delegation was expensive and knowing why.

    Metadata only, deliberately. Tool results are not carried: a diagnostic that embedded
    what it was measuring would become the expensive payload it exists to explain.
    """

    turn: int
    input_tokens: int
    output_tokens: int
    attempts: int
    effort: str
    evicted: int
    tool_calls: tuple[tuple[str, str], ...]  # (name, outcome)


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


def newly_evicted_ids(
    before: tuple[Message, ...], after: tuple[Message, ...]
) -> tuple[str, ...]:
    """Which tool results this eviction pass replaced with the stub, by `tool_use_id`.

    A separate diff rather than a third return value from `evict_stale_tool_results`. That
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

    Four structures that were separate locals in the turn loop and are one concern: what was
    called, which file each call named, what the loop evicted, and what it cost. Together
    they are ADR-0007's ledger for context economics -- server-captured facts, never the
    model's account of itself.

    `calls` is maintained always, because an overflow abort needs it to produce a report and
    an abort is not something the caller opted into. The per-turn detail is kept only when
    `diagnostics` was asked for; the two path dictionaries are maintained regardless, since
    they cost one small entry per tool call and the correlation they feed cannot be
    reconstructed after the fact.
    """

    def __init__(self, *, diagnostics: bool) -> None:
        self.diagnostics = diagnostics
        self.calls: _Ledger = []
        self.turns: list[TurnDiagnostic] = []
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
        if result is not None and result.bash is not None:
            self._bash(result.bash, is_error)
        path = str(call.input.get("path") or "")
        self.calls.append((call.name, path, is_error))
        if path:
            # Keyed by id, because an eviction knows only ids and has to be able to name a
            # file. The raw argument rather than the resolved path: `tools.py` never hands
            # the resolved one back, and the model's own argument is what a reader
            # reconciling this against the tree is looking for anyway.
            self._paths[call.id] = path
        self._reread(path)

    def _bash(self, bash: BashOutcome, is_error: bool) -> None:
        """One shell command, as the server saw it rather than as the model reports it.

        Attempts, not completions: a call refused before a process started still happened,
        and `tool_calls` beside it counts the same way.

        `last_bash_exit` moves only when something actually ran, and `ran` is the whole
        line. A command killed on timeout *did* run, so it becomes the last one and reports
        no exit code -- leaving the previous 0 standing would let a kill read as a success,
        which is the exact misreport ADR-0007 exists to catch. A refusal never started a
        process, so the last command that ran is still the previous one and its exit code is
        still the true answer.
        """
        self.bash_calls += 1
        self.bash_failures += is_error or bash.timed_out or bash.exit_code != 0
        if bash.ran:
            self.last_bash_exit = bash.exit_code

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
        reaching the end of it. Accumulating from down there silently lost the answering
        turn's attempts from the ledger -- a delegation that retried twice and then answered
        reported `attempts: 0`, which is exactly the kind of counter that reads as fine.
        """
        self.attempts += dispatch.attempts
        if not self.diagnostics:
            return
        self.turns.append(
            TurnDiagnostic(
                turn=self.turn,
                input_tokens=dispatch.response.input_tokens,
                output_tokens=dispatch.response.output_tokens,
                attempts=dispatch.attempts,
                effort=dispatch.effort,
                evicted=evicted,
                tool_calls=(),
            )
        )

    def turn_tools(self, outcomes: tuple[tuple[str, str], ...]) -> None:
        """Attach this turn's tool outcomes to the record `turn_cost` already opened."""
        if self.diagnostics and self.turns:
            self.turns[-1] = replace(self.turns[-1], tool_calls=outcomes)


class _OverflowGuard:
    """The context economics of one delegation, kept together rather than in the loop.

    Extracted for the reason `Config._validate_retry` was: these are several statements
    serving a single concern with a name, and the turn loop reads better delegating to it
    than interleaving it. The side benefit is that the thresholds become testable without
    running a delegation at all, which matters here -- a check on this path that could not
    fire would be trusted, and this project has already found four of those.

    Entirely inert unless `context_overflow_enabled`. Every method returns the do-nothing
    answer when it is off, so the loop needs no second branch around each call.
    """

    def __init__(self, cfg: Config, entry: ModelEntry) -> None:
        self.cfg = cfg
        self.entry = entry
        # Only ever tightens. A delegation that wins back headroom by evicting has not
        # stopped growing, so relaxing again would only re-run the same climb.
        self.keep = cfg.keep_tool_results
        self.prev_input_tokens = 0  # what the backend reported for the previous request
        self.pending_tokens = 0  # what we have appended since that request
        self.tightened_at = 0
        self.nudged_at = 0

    @property
    def armed(self) -> bool:
        return self.cfg.context_overflow_enabled

    def share(self) -> float:
        return projected_fraction(
            self.cfg, self.entry, self.prev_input_tokens, self.pending_tokens
        )

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
    it had already written. Upstream shipped exactly this and lost real answers to it.

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

    Two accounts of the same events, deliberately not merged into one. The ledger is every
    tool call this server ran and whether it failed; `git status` is what is actually on
    disk. Where they disagree the disagreement is the finding, and a report that reconciled
    them for the reader would hide it.

    Scoped to the directories the delegation wrote into, never the whole workspace.
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

    The first four fields match `Dispatch` so a caller reads both the same way. The rest is
    the ledger ADR-0007 asks for, extended from exit codes to the economics of the loop:
    turns actually taken, tools actually run, results actually evicted. A model's own
    account of how many files it read is not evidence, and this is.
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
    hit_turn_limit: bool = False
    # ADR-0007. `last_bash_exit` is None for "nothing exited" -- no command ran, or the last
    # one was killed on timeout -- which 0 cannot mean, being a real exit code.
    bash_calls: int = 0
    bash_failures: int = 0
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

    Reasoning is dropped unless `resend_reasoning` says otherwise: it costs input tokens
    and prefill on every subsequent turn, and the conclusions already survive in the text
    the model wrote. Tool calls are never dropped -- a result whose `tool_use` is missing
    is a history some backends reject outright.
    """
    if cfg.resend_reasoning:
        return response.content
    return tuple(b for b in response.content if not isinstance(b, ThinkingBlock))


def _run_one_call(
    cfg: Config,
    call: ToolUseBlock,
    allowed: frozenset[str],
    cached: dict[tuple[str, str], str],
    policy: BashPolicy,
) -> tuple[ToolResultBlock, str]:
    """Execute one tool call, or serve it from what an identical earlier one returned.

    Dedup is byte-identical on name and arguments, and applies only to tools declared
    `cacheable` -- see `RegisteredTool`. Anything else not only misses the cache but
    *clears* it: a write invalidates every read taken before it, and serving a file from
    before its own overwrite is a worse failure than paying for the read again.

    Known gap, recorded rather than papered over: a re-read of the same file at a different
    offset is a different argument set and is not caught. Upstream's version has the same
    hole. Closing it needs range tracking, which is its own piece of work.
    """
    key = (call.name, _dedup_key(call))
    if key in cached:
        return (
            ToolResultBlock(tool_use_id=call.id, content=REPEAT_PREFIX + cached[key]),
            "repeat",
        )

    result = execute_tool(cfg, call, allowed, policy)
    tool = REGISTRY.get(call.name)
    if tool is None or not tool.cacheable:
        # Unknown or side-effecting. Everything cached so far may describe a world that no
        # longer exists, and there is no way from here to tell which entries those are.
        cached.clear()
    elif not result.is_error:
        # Errors are not cached. Several are transient by nature -- a file that does not
        # exist yet is the obvious one -- and caching a refusal would make it permanent for
        # the rest of the delegation.
        cached[key] = result.content
    return result, "error" if result.is_error else "ran"


def _run_calls(  # noqa: PLR0913 -- one turn's inputs; the sixth is the sandbox policy
    cfg: Config,
    calls: tuple[ToolUseBlock, ...],
    allowed: frozenset[str],
    cached: dict[tuple[str, str], str],
    watch: _Watch,
    *,
    policy: BashPolicy,
) -> tuple[list[ContentBlock], tuple[tuple[str, str], ...]]:
    """One turn's tool calls, run in order. Returns the result blocks, errors and repeats.

    Separate from `_run_one_call` because the ledger entry belongs to the turn rather than
    to the call: it records the path argument the model asked for, not the path the policy
    resolved, and that distinction is worth keeping in one visible place. The two can
    differ, and it is the model's own argument that the abort report needs -- reconciling
    what it *believed* it wrote against what is on disk is the point of that report.
    """
    results: list[ContentBlock] = []
    outcomes: list[tuple[str, str]] = []
    for call in calls:
        block, outcome = _run_one_call(cfg, call, allowed, cached, policy)
        watch.called(call, outcome, block if isinstance(block, ToolResultBlock) else None)
        outcomes.append((call.name, outcome))
        results.append(block)
    return results, tuple(outcomes)


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
    on_alive: Callable[[float, int], Awaitable[None]] | None = None,
    on_turn_done: Callable[[TurnDiagnostic, str, float], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> AgenticDispatch:
    """Turns, until the model answers or the budget runs out.

    One turn is one model reply plus any tools it called. The loop ends when a reply
    carries no tool calls -- that reply is the answer -- or when the last turn is reached,
    which is declared with no tools at all so the model cannot end on a call nobody will
    run. `hit_turn_limit` says which of the two happened, because an answer written under a
    withdrawn toolset is worth reading differently from one the model chose to give.

    It is `turn == turns` and nothing else. It once also required a tool call on that final
    reply, which a backend offered no tools does not produce -- so the flag was false in
    exactly the case it exists to report, and true only when a backend ignored the
    withdrawal. A delegation that finishes on its last turn now reports the limit even if it
    would have stopped there anyway; that direction costs a reader one look at `max_turns`,
    where the other cost them a truncated answer read as a complete one.

    Recovery from an answer that came back empty is `dispatch_with_recovery`'s, per turn,
    unchanged from the one-shot path. The loop does not reimplement retry, backoff or
    step-down; it supplies a builder and counts what came back.

    **One deadline covers the whole delegation.** It is taken once here and passed to every
    turn, so `dispatch_timeout` bounds the delegation rather than each turn -- a per-turn
    budget would make the real bound the timeout times `max_turns`, which at the defaults
    is a day and a half.

    `report_progress` is called once per turn and is not cosmetic (ADR-0018): it resets the
    client's stdio idle timer, which is 1800s against a 3600s `dispatch_timeout`, so without
    it a long delegation is abandoned by the client while the server is still working. It is
    injected rather than imported for the reason `sleep` and `clock` are -- `loop.py` holds
    no MCP imports, and a test needs to see the calls without a client.

    `on_alive` is the same protection *within* a turn, and one per turn was not enough: a
    turn's own duration is bounded only by `turn_timeout`, so a single slow one outlasts the
    idle timer on its own. It runs on a timer beside the loop rather than at a point inside
    it, because there is no point inside a turn that is guaranteed to be reached.

    Effort reported is the level of the *last* turn, which is the one that produced the
    answer. A step-down on turn three does not persist into turn four: the next turn is a
    different request, and re-deriving the level from the caller's argument each time is
    what keeps one bad turn from silently downgrading the rest of the delegation.
    """
    resolved_effort = resolve_effort(cfg, entry, effort)
    turns = resolve_max_turns(cfg, max_turns)
    specs = declared_tools(allowed)
    deadline = clock() + cfg.dispatch_timeout
    # When a turn last *finished*. Deliberately not when one last started, which is what
    # `report_progress` reports: that fires at the top of a turn, so it would reset the
    # clock on entry to the very turn that then wedges. The keepalive is no use either --
    # it proves liveness on a timer regardless of progress, which is precisely the signal
    # a no-progress deadline must not count as progress (ADR-0047).
    last_progress = clock()

    def stall_left() -> float:
        return cfg.stall_timeout - (clock() - last_progress)
    bash_policy = policy or BashPolicy()

    history: list[Message] = [Message("user", (TextBlock(delegation.render()),))]

    cached: dict[tuple[str, str], str] = {}
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

    # The heartbeat, beside the loop rather than inside it. `run_one_shot` has had one
    # since ADR-0018; this path reported only at the top of each turn, so a single long
    # turn was silent for its whole duration -- bounded by `turn_timeout`, which defaults
    # to exactly the client's stdio idle timeout. At that point the client abandons the
    # call, nothing reaches the server, and the slot is held to the end (#58).
    #
    # Created as `None` rather than early-returning the way the one-shot does, because
    # there the guarded part is one `await` and here it is the whole loop: duplicating it
    # to avoid a nullable task would be two copies of the turn lifecycle.
    beat = asyncio.create_task(_keepalive(cfg, on_alive, clock)) if on_alive else None
    try:
        while turn < turns:
            turn += 1
            watch.turn = turn
            await report_progress(turn, turns)
            final = turn == turns

            guard.begin_turn(turn=turn, turns=turns, ledger=watch.calls)
            before = tuple(history)
            trimmed, dropped = evict_stale_tool_results(before, guard.keep)
            history = list(trimmed)
            watch.evicted(before, trimmed, dropped)

            def build(
                level: str, budget: int, *, _msgs: tuple[Message, ...] = tuple(history),
                _final: bool = final,
            ) -> CanonicalRequest:
                return CanonicalRequest(
                    system=SYSTEM_PROMPT_AGENTIC,
                    messages=_msgs,
                    max_tokens=budget,
                    effort=level,
                    temperature=cfg.tool_call_temperature,
                    # Withdrawn on the final turn, so the only thing left to produce is an
                    # answer. A model that ends on a tool call nobody will run has spent the
                    # whole delegation and returned nothing readable.
                    tools=() if _final else specs,
                )

            backend_started = clock()
            dispatch = await dispatch_with_recovery(
                cfg, entry, backend, build,
                effort=resolved_effort, max_tokens=max_tokens,
                sleep=sleep, deadline=deadline, stall_left=stall_left, clock=clock,
            )
            # The backend call alone, separate from the turn's wall clock. Tokens per second
            # over the whole turn would fold tool execution and any retry wait into the
            # divisor and report the cluster as slower than it is -- and a throughput number
            # that is quietly measuring the wrong interval is worse than none, because it
            # gets believed.
            backend_seconds = max(clock() - backend_started, 0.0)
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
            # in `sandbox.py`, so a command bounded by `run_bash_timeout` blocked the loop
            # for its whole duration -- no timer task, no `report_progress`, and no stdio
            # traffic for any other delegation admitted alongside it. A heartbeat that goes
            # quiet during the longest blocking operation in the system is the check that
            # cannot fail, in its heartbeat form.
            #
            # One thread for the whole batch, not one per call: `_run_calls` runs them in
            # order and that order is what the model sees. The only thing now running beside
            # it is the timer, which touches neither `cached` nor `watch`.
            results, outcomes = await asyncio.to_thread(
                _run_calls, cfg, calls, allowed, cached, watch, policy=bash_policy
            )
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
            hit_turn_limit=turn == turns,
            bash_calls=watch.bash_calls,
            bash_failures=watch.bash_failures,
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
        # loop finishes. So a delegation that ran out of time reported which setting fired
        # and nothing about what it had achieved, which at the call site is indistinguishable
        # from an endpoint that never answered: the honest response to that is to stop using
        # the server, and twice on 2026-09-04 that was the wrong call.
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
