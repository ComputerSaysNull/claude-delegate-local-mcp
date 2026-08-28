"""The delegation itself: build a request, send it, hand back what came back.

Still the one-shot path only -- there is no turn loop here yet (M4). What M3 adds is the
response state machine, in two halves that compose. Below, a dispatch that survives a
dropped route, a refusal the endpoint calls temporary, and a `Retry-After` it asks us to
honour. Above that, recovery from a reply that arrived intact and empty because reasoning
consumed the whole budget (ADR-0014): retry once at a larger budget, then step effort down,
then say plainly that everything was tried.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime

from .backends.base import (
    Backend,
    BackendRefused,
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
from .config import EFFORT_LEVELS, Config
from .registry import ModelEntry
from .tools import REGISTRY, declared_tools, execute_tool

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


class DispatchTimedOut(Exception):
    """The whole delegation outlived `dispatch_timeout`.

    Distinct from every backend failure on purpose. `BackendUnavailable` says the endpoint
    did not answer; this says the endpoint may well be answering and the delegation has
    still taken longer than the operator allows. Sending a caller to check the cluster on
    a deadline they set themselves would be the wrong diagnosis, and ADR-0007's argument
    for server-captured truth applies to *which* failure this was as much as to exit codes.
    """

    def __init__(self, elapsed: float, limit: int, stage: str) -> None:
        self.elapsed = elapsed
        self.limit = limit
        self.stage = stage
        super().__init__(
            f"Delegation abandoned after {elapsed:.1f}s, past the "
            f"DELEGATE_DISPATCH_TIMEOUT of {limit}s, while {stage}. Raise that setting if "
            "the work legitimately takes this long, or shorten the task."
        )


@dataclass(frozen=True, slots=True)
class Delegation:
    """What to delegate: the task, and the material assembled for it.

    A value object rather than another parameter, because these are the parts of one
    prompt and M4 adds a third (the agent body). Keeping them together is what lets the
    ordering rule live in exactly one place -- `build_one_shot_request` renders them in
    the fixed order, instead of each caller being trusted to concatenate correctly.
    """

    task: str
    files_block: str = ""


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


def resolve_max_tokens(cfg: Config, entry: ModelEntry, effort: str) -> int:
    """The reply budget, with the per-model cap applied last.

    Reasoning is generated against this same budget, so a high effort with a low cap
    produces an answer that is empty because it thought until it ran out. ADR-0014.
    """
    budget = cfg.max_tokens
    if effort in ("high", "max"):
        budget = max(budget, cfg.thinking_max_tokens_floor)
    return entry.cap_tokens(budget)


def build_one_shot_request(
    *, delegation: Delegation, effort: str, max_tokens: int, temperature: float
) -> CanonicalRequest:
    """One user message, no tools, and a system prompt that does not vary.

    Order inside the message is files then task, which with the static system prompt
    ahead of both gives the sequence ADR-0011 fixes: system, agent body (M6), files, task
    last. The task is the part that varies most between calls, so it goes where a changed
    byte costs the least -- at the end, after everything a second call might share.
    """
    task = delegation.task
    if not task or not task.strip():
        raise InvalidDelegation("task is empty. There is nothing to delegate.")
    body = f"{delegation.files_block}\n\n{task}" if delegation.files_block else task
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
        return clock() - started

    while True:
        left = remaining()
        if left is not None and left <= 0:
            raise DispatchTimedOut(spent(), cfg.dispatch_timeout, "waiting on the backend")
        attempts += 1
        try:
            if left is None:
                return await backend.complete(request), attempts
            # The per-attempt ceiling. `turn_timeout` already bounds one call inside the
            # adapter's client, but it is a fixed budget that knows nothing about how much
            # of the delegation is left, so without this the deadline could still be
            # overshot by a whole turn.
            return await asyncio.wait_for(backend.complete(request), timeout=left), attempts
        except TimeoutError as e:
            raise DispatchTimedOut(
                spent(), cfg.dispatch_timeout, "waiting on the backend"
            ) from e
        except (BackendUnavailable, BackendRefused) as e:
            if not _is_retryable(e) or attempts >= cfg.retry_max_attempts:
                raise
            wait = _delay_before_retry(cfg, e, attempts, jitter)
            left = remaining()
            if left is not None and wait >= left:
                # Sleeping first would burn the rest of the budget and then report the
                # deadline, naming a wait this function chose rather than the work.
                raise DispatchTimedOut(
                    spent() + wait, cfg.dispatch_timeout, "waiting to retry"
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
    asked_budget = resolve_max_tokens(cfg, entry, effort)
    attempts = 0

    response, spent = await complete_with_retry(
        cfg, backend, build(effort, asked_budget),
        sleep=sleep, deadline=deadline, clock=clock,
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
            sleep=sleep, deadline=deadline, clock=clock,
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
        cfg, backend, build(stepped, resolve_max_tokens(cfg, entry, stepped)),
        sleep=sleep, deadline=deadline, clock=clock,
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
    Claude Code's 1800s stdio idle timeout, so a delegation can still be abandoned by the
    client while this is perfectly happy. ADR-0018's per-turn progress notification is what
    addresses that, and there are no turns here to report -- which is the honest reason this
    path cannot hold the client open, and a reason to prefer the loop for long work.
    """
    resolved = resolve_effort(cfg, entry, effort)
    deadline = clock() + cfg.dispatch_timeout

    def request_at(level: str, budget: int) -> CanonicalRequest:
        return build_one_shot_request(
            delegation=delegation,
            effort=level,
            max_tokens=budget,
            temperature=cfg.one_shot_temperature,
        )

    return await dispatch_with_recovery(
        cfg, entry, backend, request_at,
        effort=resolved, sleep=sleep, deadline=deadline, clock=clock,
    )


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
    tool_errors: int = 0
    deduped: int = 0
    evicted: int = 0
    hit_turn_limit: bool = False


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

    result = execute_tool(cfg, call, allowed)
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


async def run_agentic_loop(  # noqa: PLR0913 -- three of the nine are test seams
    cfg: Config,
    entry: ModelEntry,
    backend: Backend,
    delegation: Delegation,
    *,
    allowed: frozenset[str],
    effort: str | None = None,
    max_turns: int | None = None,
    report_progress: Callable[[int, int], Awaitable[None]] = _no_progress,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> AgenticDispatch:
    """Turns, until the model answers or the budget runs out.

    One turn is one model reply plus any tools it called. The loop ends when a reply
    carries no tool calls -- that reply is the answer -- or when the last turn is reached,
    which is declared with no tools at all so the model cannot end on a call nobody will
    run. `hit_turn_limit` says which of the two happened, because an answer written under a
    withdrawn toolset is worth reading differently from one the model chose to give.

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

    Effort reported is the level of the *last* turn, which is the one that produced the
    answer. A step-down on turn three does not persist into turn four: the next turn is a
    different request, and re-deriving the level from the caller's argument each time is
    what keeps one bad turn from silently downgrading the rest of the delegation.
    """
    resolved_effort = resolve_effort(cfg, entry, effort)
    turns = resolve_max_turns(cfg, max_turns)
    specs = declared_tools(allowed)
    deadline = clock() + cfg.dispatch_timeout

    task = delegation.task
    if not task or not task.strip():
        raise InvalidDelegation("task is empty. There is nothing to delegate.")
    body = f"{delegation.files_block}\n\n{task}" if delegation.files_block else task
    history: list[Message] = [Message("user", (TextBlock(body),))]

    cached: dict[tuple[str, str], str] = {}
    attempts = tool_calls = tool_errors = deduped = evicted = 0
    dispatch: Dispatch | None = None
    turn = 0

    while turn < turns:
        turn += 1
        await report_progress(turn, turns)
        final = turn == turns

        trimmed, dropped = evict_stale_tool_results(tuple(history), cfg.keep_tool_results)
        evicted += dropped
        history = list(trimmed)

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

        dispatch = await dispatch_with_recovery(
            cfg, entry, backend, build,
            effort=resolved_effort, sleep=sleep, deadline=deadline, clock=clock,
        )
        attempts += dispatch.attempts
        calls = dispatch.response.tool_uses
        if final or not calls:
            break

        history.append(Message("assistant", _assistant_blocks(cfg, dispatch.response)))
        results: list[ContentBlock] = []
        for call in calls:
            block, outcome = _run_one_call(cfg, call, allowed, cached)
            tool_calls += 1
            tool_errors += outcome == "error"
            deduped += outcome == "repeat"
            results.append(block)
        results.append(TextBlock(countdown_line(turns - turn)))
        history.append(Message("user", tuple(results)))

    if dispatch is None:  # unreachable: turns >= 1, so the body ran at least once
        raise InvalidDelegation("max_turns resolved to zero turns.")
    return AgenticDispatch(
        response=dispatch.response,
        effort=dispatch.effort,
        attempts=attempts,
        reasoning_exhausted=dispatch.reasoning_exhausted,
        turns=turn,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        deduped=deduped,
        evicted=evicted,
        hit_turn_limit=turn == turns and bool(dispatch.response.tool_uses),
    )
