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
import random
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
    Message,
    TextBlock,
)
from .config import EFFORT_LEVELS, Config
from .registry import ModelEntry

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


class InvalidDelegation(ValueError):
    """A caller's argument is wrong. Raised before anything is sent to a backend."""


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


async def complete_with_retry(
    cfg: Config,
    backend: Backend,
    request: CanonicalRequest,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
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

    No outer deadline is enforced here, and that is a gap rather than a decision.
    `dispatch_timeout` exists in config and is consumed nowhere; each individual attempt is
    bounded by `turn_timeout` inside the adapter's client, but the sum of attempts plus the
    waits between them is bounded only by `retry_max_attempts` and `retry_max_delay`.
    Whole-delegation enforcement, and the per-turn progress notification that keeps a long
    wait from tripping the client's own idle timeout (ADR-0018), both arrive with the turn
    loop. Until then the small `retry_max_delay` default is the whole mitigation.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            return await backend.complete(request), attempts
        except (BackendUnavailable, BackendRefused) as e:
            if not _is_retryable(e) or attempts >= cfg.retry_max_attempts:
                raise
            await sleep(_delay_before_retry(cfg, e, attempts, jitter))


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


async def run_one_shot(  # noqa: PLR0913 -- see the note below the docstring
    cfg: Config,
    entry: ModelEntry,
    backend: Backend,
    delegation: Delegation,
    *,
    effort: str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Dispatch:
    """Resolve, send, and recover from an answer that is empty because it ran out of room.

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

    Attempts accumulate across all three stages and every transport retry inside them, so
    the number reported is what this delegation really cost. What is *not* summed is the
    token counts: those come from the attempt that answered. ADR-0014 says the retry must
    not charge the turn budget, and with no turn accounting yet (M4) that is what the rule
    means here -- report the successful attempt, and let `attempts` carry the cost.
    """
    resolved = resolve_effort(cfg, entry, effort)
    asked_budget = resolve_max_tokens(cfg, entry, resolved)
    attempts = 0

    def request_at(level: str, budget: int) -> CanonicalRequest:
        return build_one_shot_request(
            delegation=delegation,
            effort=level,
            max_tokens=budget,
            temperature=cfg.one_shot_temperature,
        )

    response, spent = await complete_with_retry(
        cfg, backend, request_at(resolved, asked_budget), sleep=sleep
    )
    attempts += spent
    if not is_empty_at_length(response):
        return Dispatch(response, resolved, attempts)

    # Stage 2: the same level, more room. `thinking_max_tokens_floor` documents itself as
    # the size retried after an empty answer, so there is no second setting for it.
    floor = entry.cap_tokens(max(2 * asked_budget, cfg.thinking_max_tokens_floor))
    if floor > asked_budget:
        response, spent = await complete_with_retry(
            cfg, backend, request_at(resolved, floor), sleep=sleep
        )
        attempts += spent
        if not is_empty_at_length(response):
            return Dispatch(response, resolved, attempts)
    # Otherwise the model's own cap already pinned the first budget, and "retry at a larger
    # budget" would send a byte-identical request. Skipped rather than spent: an identical
    # dispatch cannot produce a different outcome at temperature zero, and even where it
    # might, paying a full generation for the chance is not a mitigation.

    stepped = _STEP_DOWN.get(resolved)
    if stepped is None:
        # Effort is already off. There is nothing left to disable, so this is a budget too
        # small for the answer -- NOT reasoning exhaustion. Reporting it as exhaustion
        # would be a diagnosis the caller could act on wrongly, sending them to lower the
        # effort that is already lowest instead of raising the budget or shortening the task.
        return Dispatch(response, resolved, attempts)

    response, spent = await complete_with_retry(
        cfg, backend, request_at(stepped, resolve_max_tokens(cfg, entry, stepped)), sleep=sleep
    )
    attempts += spent
    return Dispatch(response, stepped, attempts, is_empty_at_length(response))
