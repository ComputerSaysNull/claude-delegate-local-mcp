"""The delegation itself: build a request, send it, hand back what came back.

M1 is the one-shot path only. There is no turn loop here yet (M4) and no response
state machine (M3): nothing retries, backs off, steps down on reasoning exhaustion, or
decides what an empty answer means. Those need the raw `finish_reason` and token counts
to decide anything, which is why the adapter passes them through uninterpreted and why
this module does not start interpreting them.

What it does own is resolution -- which model, which effort, which budget -- because
that has to happen exactly once and be sent explicitly, and assembly: the order of the
parts of the prompt, which is load-bearing for the cluster's prefix cache and so is
decided here rather than wherever a part happens to be produced. M2 grew this file with
the files block; M4 grows it again rather than replacing it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backends.base import Backend, CanonicalRequest, CanonicalResponse, Message, TextBlock
from .config import EFFORT_LEVELS, Config
from .registry import ModelEntry

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


async def run_one_shot(
    cfg: Config,
    entry: ModelEntry,
    backend: Backend,
    delegation: Delegation,
    *,
    effort: str | None = None,
) -> tuple[CanonicalResponse, str]:
    """Resolve, send, return. Also returns the effort actually used, which the caller
    reports: what was asked for and what was sent are not always the same thing."""
    resolved = resolve_effort(cfg, entry, effort)
    request = build_one_shot_request(
        delegation=delegation,
        effort=resolved,
        max_tokens=resolve_max_tokens(cfg, entry, resolved),
        temperature=cfg.one_shot_temperature,
    )
    return await backend.complete(request), resolved
