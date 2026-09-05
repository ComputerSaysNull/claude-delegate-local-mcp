"""The backend seam: one canonical message shape, and the protocol adapters implement.

Why this file exists (ADR-0008): only the OpenAI-compatible adapter ships, but the shape
kept *inside* the server is the Anthropic one -- content blocks, tool-use, tool-result.
Nothing above the backend layer then knows which wire protocol is in play, so adding an
Anthropic adapter later is one new file rather than a refactor. `registry.py` already
promises this seam by name when it refuses `api_format = "anthropic"`; this is the file
that makes the promise real.

Three conditions keep it cheap, and breaking any one turns the next adapter back into a
refactor:

  a. The canonical shape stays block-structured and is never flattened to strings.
     Flattening is what an adapter is *for*, and it happens only at the wire edge.
  b. SSE accumulation lives per adapter, behind one contract. No streaming ships in v1
     (ADR-0018), so `complete()` is request/response -- but that is a method on the
     protocol, not a shape baked into the caller.
  c. Model selection is a registry lookup, never a reintroduced prefix function
     (ADR-0009). Nothing in this layer inspects a model name to decide anything.

What this file deliberately does NOT do: retry, step-down on reasoning exhaustion,
empty-answer handling, context-overflow recovery. That is the response state machine, M3.
What it does instead is make that machine buildable on top -- `finish_reason` and the
token counts come back raw and *uninterpreted*, and the error kinds below are
distinguishable. In particular a reply with `content: null` and `finish_reason: "length"`
is a valid response carrying no text blocks, not an error (ADR-0014). Deciding what to do
about that is M3's job, and mapping `finish_reason` onto some tidier vocabulary here
would be exactly the interpretation this layer must not perform.

Not a port. This file is new.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import EFFORT_LEVELS

ROLES = ("user", "assistant")


# --- errors ----------------------------------------------------------------------------
#
# Four kinds, because the caller acts differently on each and a single BackendError would
# force it to parse a message to find out which happened. M3 builds retry on this
# distinction: Unavailable is worth retrying, Refused usually is not, ProtocolError never
# is, and CanonicalShapeError is our own bug and must never be retried at all.


class BackendError(Exception):
    """Base for everything this layer raises."""


class CanonicalShapeError(BackendError):
    """A malformed canonical request or message. Our bug, not the server's.

    Raised at construction, never at first use -- the rule config.py and registry.py
    already follow, for the same reason: a shape error discovered thirty minutes into a
    delegation is far worse than one that refuses to build.
    """


class BackendUnavailable(BackendError):
    """The endpoint could not be reached: connect failure, DNS, or timeout."""


class BackendRefused(BackendError):
    """The endpoint answered with a non-2xx status.

    Carries `status` and `body` rather than only a message, because ADR-0017 requires a
    specific 400 to stay feature-detectable: the serving stack's own docs named the wrong
    boot flag for `thinking_token_budget`, and only the live response body says which
    switch actually gates it. A caller that cannot read the body cannot feature-detect.

    `retry_after` is the header verbatim and unparsed -- the string the endpoint sent, or
    None. Parsing it is interpretation, and this layer does not interpret: the same rule
    that keeps `finish_reason` raw keeps this raw, and `loop.py` owns both. It is carried
    here rather than re-read from the response because the response object does not
    survive the exception, and a retry that ignores what the server asked for is not
    honouring it.
    """

    def __init__(
        self, status: int, body: str, url_path: str = "", retry_after: str | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.url_path = url_path
        self.retry_after = retry_after
        where = f" from {url_path}" if url_path else ""
        super().__init__(f"backend refused with HTTP {status}{where}: {body[:600]}")


class BackendProtocolError(BackendError):
    """A 2xx response that is not the shape the API promises."""


# --- content blocks -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    """Reasoning the model emitted, kept as its own block rather than folded into text.

    `Config.resend_reasoning` decides whether it goes back on the next turn, and that
    choice is only expressible if the reasoning survives as something distinguishable
    this far up.
    """

    text: str


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, object]


@dataclass(frozen=True, slots=True)
class BashOutcome:
    """What the server saw a shell command do, apart from what the model says about it.

    Carried on the result block rather than parsed back out of `content`, because a trailer
    regex over text the model also reads is a check that stops firing the day the wording
    changes, and nothing reports that it stopped. ADR-0007 rests on these being measured.

    `exit_code` is None when nothing exited: killed on timeout, or refused before a process
    ever started. Those are distinguished by `timed_out`, and neither is 0 -- which is a
    real exit code a command can return and must not collide with either.

    Lives here rather than in `tools.py` because `tools.py` imports this module, and the
    block has to be able to name the type it carries.
    """

    exit_code: int | None
    timed_out: bool = False
    ran: bool = False


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    # None for every tool but run_bash, so no other construction site changes.
    bash: BashOutcome | None = None


ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock

# isinstance() needs a tuple of runtime classes; the union above is for annotations.
BLOCK_TYPES = (TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock)


# --- messages and requests ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """One turn. `content` is always a tuple of blocks -- never a bare string.

    The string case is rejected rather than coerced, and rejected loudly, because a
    coerced string is ADR-0008 condition (a) failing silently: the canonical shape would
    still typecheck while having quietly become the OpenAI one.
    """

    role: str
    content: tuple[ContentBlock, ...]

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise CanonicalShapeError(
                f"role={self.role!r} is not one of {ROLES}. A tool result is carried as a "
                "ToolResultBlock on a user message, not as a role of its own -- that is "
                "the OpenAI shape, and translating to it is the adapter's job."
            )
        if isinstance(self.content, (str, bytes)):
            raise CanonicalShapeError(
                "content must be a tuple of blocks, not a string. The canonical shape "
                "stays block-structured (ADR-0008); flattening happens in the adapter, "
                "at the wire edge, and nowhere else."
            )
        if not isinstance(self.content, tuple):
            raise CanonicalShapeError(
                f"content must be a tuple, got {type(self.content).__name__}. A list is "
                "rejected too: these dataclasses are frozen so they can be shared and "
                "compared, which a mutable member would quietly break."
            )
        for block in self.content:
            if not isinstance(block, BLOCK_TYPES):
                raise CanonicalShapeError(
                    f"{type(block).__name__} is not a content block. Expected one of "
                    f"{[t.__name__ for t in BLOCK_TYPES]}."
                )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool as declared to the model, in canonical form."""

    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    """One backend call, fully specified.

    `max_tokens`, `effort` and `temperature` carry no defaults, deliberately. Config
    defaults live only in config.py and per-model overrides only in the registry; a
    default here would be a second copy of a fact, and the docs gate exists because
    second copies drift. The caller resolves them -- `ModelEntry.effective_effort(cfg)`
    and `ModelEntry.cap_tokens()` are already there for it -- and passes what it decided.

    Message order is the caller's and is never rearranged downstream: system prompt,
    agent body, files block, task last, so the cached prefix stays bit-identical
    (ADR-0011). The adapter translates; it does not schedule.
    """

    system: str
    messages: tuple[Message, ...]
    max_tokens: int
    effort: str
    temperature: float
    tools: tuple[ToolSpec, ...] = ()

    def __post_init__(self) -> None:
        if self.effort not in EFFORT_LEVELS:
            raise CanonicalShapeError(
                f"effort={self.effort!r} is not one of {EFFORT_LEVELS}. These are this "
                "project's levels; the adapter translates them to the server's own "
                "vocabulary, and an unlisted one has no translation."
            )
        if not isinstance(self.messages, tuple):
            raise CanonicalShapeError(
                f"messages must be a tuple, got {type(self.messages).__name__}."
            )
        if not self.messages:
            raise CanonicalShapeError("messages is empty; there is nothing to send.")
        for message in self.messages:
            if not isinstance(message, Message):
                raise CanonicalShapeError(
                    f"messages contains {type(message).__name__}, not a Message."
                )
        if self.max_tokens < 1:
            raise CanonicalShapeError(
                f"max_tokens={self.max_tokens} must be at least 1."
            )
        if not 0.0 <= self.temperature <= 2.0:
            raise CanonicalShapeError(
                f"temperature={self.temperature} is outside the accepted range 0.0-2.0."
            )


@dataclass(frozen=True, slots=True)
class CanonicalResponse:
    """One backend reply, translated but not interpreted.

    `finish_reason` is the wire value verbatim. It is not mapped onto a tidier
    vocabulary, because every such mapping is a decision about what the reply *means* --
    and those decisions belong to the response state machine in M3, which needs the raw
    value to make them. The token counts are here for the same reason: reasoning
    exhaustion is diagnosed by comparing spend against the budget (ADR-0014), so the
    layer that spots it needs the numbers rather than a verdict.

    The four optional fields follow that rule rather than bending it. `stop_reason` is
    vLLM's own answer to "which stop condition fired", distinct from `finish_reason` and
    absent on backends that do not speak it; `system_fingerprint` identifies the engine
    build and its configuration. Both are carried and neither is interpreted here.
    """

    content: tuple[ContentBlock, ...]
    finish_reason: str
    input_tokens: int
    output_tokens: int
    model: str
    # Four the endpoint already returns and this server used to discard. All optional,
    # and `None` means the endpoint did not report the field at all -- which is a
    # different fact from reporting a zero, and the distinction is the point. A
    # `cached_tokens` of 0 says the prefix missed; absent says nothing can be said about
    # caching on this backend, and collapsing the two would recreate exactly the
    # blindness that made the batch tools arguable for a fortnight (ADR-0051).
    cached_tokens: int | None = None
    total_tokens: int | None = None
    stop_reason: str | None = None
    system_fingerprint: str | None = None

    @property
    def text(self) -> str:
        """The text blocks joined. A convenience for callers, not the canonical form."""
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.content if isinstance(b, ToolUseBlock))

    @property
    def thinking(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, ThinkingBlock))


# --- the protocol ---------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """What an adapter must provide. One file per wire format, nothing else changes.

    Async because httpx is, because fastmcp is, and because `asyncio_mode = "auto"` is
    already set for the test suite -- a sync seam here would have to be unpicked the
    moment the agentic loop needs two calls in flight.
    """

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse:
        """Send one request. Raises a BackendError subclass; never returns a partial."""
        ...

    async def probe(self) -> tuple[str, ...]:
        """Model ids this endpoint reports serving. The health check, per MODELS.md."""
        ...

    async def probe_window(self) -> int | None:
        """The context window this endpoint reports for its served model, if it says.

        `None` means the endpoint answered and did not mention one -- a confirmed absence,
        not a failure to ask. A transport failure raises `BackendUnavailable` instead, and
        the difference matters: a caller caching "this backend cannot tell me" must never
        cache it because the network was down for a moment.

        This exists to CHECK the operator's `context_window`, never to supply it. An
        auto-derived window is how upstream came to compute every threshold against a
        model file's architecture maximum rather than the window actually served.
        """
        ...

    async def probe_cluster(self) -> dict[str, float | int | str | None] | None:
        """What the serving stack says about its own load, if it publishes anything.

        `None` is a confirmed absence -- the endpoint answered and offers no such surface
        -- exactly as in `probe_window`, and for the same reason: a caller must not cache
        "this backend cannot tell me" because the network blinked. A transport failure
        raises `BackendUnavailable` instead.

        These are the cluster's numbers, not ours. `admission` estimates queue depth from
        what this process has dispatched, which is a guess that cannot see other clients;
        this is the real thing. Values are reported, never interpreted -- the same rule
        `finish_reason` follows, because deciding that a hit rate is "bad" is a policy
        question and this layer has no policy.
        """
        ...

    async def aclose(self) -> None:
        """Release the transport. Safe to call more than once."""
        ...
