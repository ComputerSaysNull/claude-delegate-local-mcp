"""The delegation itself: build a request, send it, hand back what came back.

M1 is the one-shot path only. There is no turn loop here yet (M4) and no response
state machine (M3): nothing retries, backs off, steps down on reasoning exhaustion, or
decides what an empty answer means. Those need the raw `finish_reason` and token counts
to decide anything, which is why the adapter passes them through uninterpreted and why
this module does not start interpreting them.

What it does own is resolution -- which model, which effort, which budget -- because
that has to happen exactly once and be sent explicitly, and because M2 and M4 grow this
file rather than replacing it.
"""

from __future__ import annotations

from .backends.base import Backend, CanonicalRequest, CanonicalResponse, Message, TextBlock
from .config import EFFORT_LEVELS, Config
from .registry import ModelEntry

# The system prompt is a constant, and must stay one. The cluster caches prefixes, so a
# single dynamic byte -- a timestamp, a session id, a turn counter -- silently disables
# that with no error and no symptom beyond slower prefill. Dynamic content goes in the
# tail, inside the message. ADR-0011.
SYSTEM_PROMPT_ONE_SHOT = (
    "You are answering a single delegated task for another engineer, who will read your "
    "reply directly and act on it.\n\n"
    "You have no tools and no access to any file. Everything you can use is in the "
    "message. If the task cannot be answered from what is there, say precisely what is "
    "missing rather than guessing at it or describing what you would do given access.\n\n"
    "Answer the task as asked. Do not restate it, do not narrate your approach, and do "
    "not close by summarising what you just said."
)


class InvalidDelegation(ValueError):
    """A caller's argument is wrong. Raised before anything is sent to a backend."""


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
    *, task: str, effort: str, max_tokens: int, temperature: float
) -> CanonicalRequest:
    """One user message, no tools, and a system prompt that does not vary."""
    if not task or not task.strip():
        raise InvalidDelegation("task is empty. There is nothing to delegate.")
    return CanonicalRequest(
        system=SYSTEM_PROMPT_ONE_SHOT,
        messages=(Message("user", (TextBlock(task),)),),
        max_tokens=max_tokens,
        effort=effort,
        temperature=temperature,
    )


async def run_one_shot(
    cfg: Config, entry: ModelEntry, backend: Backend, task: str, *, effort: str | None = None
) -> tuple[CanonicalResponse, str]:
    """Resolve, send, return. Also returns the effort actually used, which the caller
    reports: what was asked for and what was sent are not always the same thing."""
    resolved = resolve_effort(cfg, entry, effort)
    request = build_one_shot_request(
        task=task,
        effort=resolved,
        max_tokens=resolve_max_tokens(cfg, entry, resolved),
        temperature=cfg.one_shot_temperature,
    )
    return await backend.complete(request), resolved
