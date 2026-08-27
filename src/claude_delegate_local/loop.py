"""The delegation itself: build a request, send it, hand back what came back.

Still the one-shot path only -- there is no turn loop here yet (M4). What M3 adds is the
transport half of the response state machine: a dispatch that survives a dropped route,
a refusal the endpoint says is temporary, and a `Retry-After` it asks us to honour. This
is the first code in the project to *interpret* what a backend handed back, which is why
the adapter below it goes on passing `finish_reason`, the token counts and the raw
`Retry-After` string through untouched. Interpretation happens here, in exactly one place.

Empty-answer recovery -- retry at the floor, then step effort down -- is the other half
and lands next; nothing here decides yet what an empty answer means.

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
from datetime import datetime, timezone
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
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
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
    the caller asked for; `attempts` is now not always one, and the next commit adds a
    third fact of the same kind. Returning a shape lets each of those be named at the call
    site instead of positioned.
    """

    response: CanonicalResponse
    effort: str
    attempts: int


async def run_one_shot(
    cfg: Config,
    entry: ModelEntry,
    backend: Backend,
    delegation: Delegation,
    *,
    effort: str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Dispatch:
    """Resolve, send with retry, and report what it took as well as what came back."""
    resolved = resolve_effort(cfg, entry, effort)
    request = build_one_shot_request(
        delegation=delegation,
        effort=resolved,
        max_tokens=resolve_max_tokens(cfg, entry, resolved),
        temperature=cfg.one_shot_temperature,
    )
    response, attempts = await complete_with_retry(cfg, backend, request, sleep=sleep)
    return Dispatch(response=response, effort=resolved, attempts=attempts)
