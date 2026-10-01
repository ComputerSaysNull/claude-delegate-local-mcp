"""The backend seam: one canonical message shape, and the protocol adapters implement.

Why this file exists (ADR-0008): only the OpenAI-compatible adapter ships, but the shape
kept *inside* the server is the Anthropic one -- content blocks, tool-use, tool-result.
Nothing above the backend layer knows which wire protocol is in play, so an Anthropic
adapter would be one new file rather than a refactor. `registry.py` names this seam when it
refuses `api_format = "anthropic"`.

Three conditions keep it cheap, and breaking any one turns the next adapter back into a
refactor:

  a. The canonical shape stays block-structured and is never flattened to strings.
     Flattening is what an adapter is *for*, and it happens only at the wire edge.
  b. SSE accumulation lives per adapter, behind one contract. The transport streams and
     the contract does not (ADR-0070): `complete()` takes one request and returns one
     whole `CanonicalResponse`, accumulating inside the adapter. Being a method on the
     protocol, not a shape baked into the caller, is what keeps that a one-file change.
  c. Model selection is a registry lookup, never a prefix function (ADR-0009). Nothing
     in this layer inspects a model name to decide anything.

What this file does NOT do: retry, step-down on reasoning exhaustion, empty-answer
handling, context-overflow recovery. Those are `loop.py`'s, built on this layer's raw
facts: `finish_reason` and the token counts come back *uninterpreted*, and the error kinds
below are distinguishable. A reply with `content: null` and `finish_reason: "length"` is a
valid response carrying no text blocks, not an error (ADR-0014); mapping `finish_reason`
onto a tidier vocabulary here would be the interpretation this layer must not perform.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import EFFORT_LEVELS

ROLES = ("user", "assistant")


# --- errors ----------------------------------------------------------------------------
#
# Four kinds, because the caller acts differently on each and a single BackendError would
# make it parse a message to find out which. Retry rests on this: Unavailable is worth
# retrying, Refused usually is not, ProtocolError never is, and CanonicalShapeError is our
# own bug and must never be retried at all.


class BackendError(Exception):
    """Base for everything this layer raises.

    `partial` is what the turn had decoded when it failed, if anything: a whole
    `CanonicalResponse`, not a string, so everything above reads it with `answer_of` as it
    reads a completed one. `None` means no token arrived, a different fact from an empty
    answer that must not merge with it -- a turn that produced nothing is what a stall
    reports.

    On the base, not `BackendUnavailable`, because the accumulator dies the same way
    whatever ends the stream, `CancelledError` included; that one is a `BaseException` and
    carries the attribute by assignment.
    """

    partial: CanonicalResponse | None = None


class CanonicalShapeError(BackendError):
    """A malformed canonical request or message. Our bug, not the server's.

    Raised at construction, never at first use, as in config.py and registry.py: a shape
    error found thirty minutes into a delegation is far worse than one that will not build.
    """


class BackendUnavailable(BackendError):
    """The endpoint could not be reached: connect failure, DNS, or timeout.

    `while_generating` separates two shapes. A connect failure delivered and spent nothing;
    a read timeout means the request arrived and the endpoint took its whole allowance.
    Both are "unavailable" to a caller asking whether the endpoint works, but not to one
    deciding whether to retry: the second has already spent the time a retry would need.
    """

    def __init__(self, *args: object, while_generating: bool = False) -> None:
        super().__init__(*args)
        self.while_generating = while_generating


class BackendRefused(BackendError):
    """The endpoint answered with a non-2xx status.

    Carries `status` and `body`, not only a message, because ADR-0017 needs a specific 400
    to stay feature-detectable: only the live body says which switch gates
    `thinking_token_budget`, since the serving stack's docs name the wrong boot flag.

    `retry_after` is the header verbatim, or None. Parsing it is interpretation, which
    `loop.py` owns, as for `finish_reason`. Carried here because the response object does
    not survive the exception, and a retry must honour what the server asked for.
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

    `Config.resend_reasoning` decides whether it goes back on the next turn, a choice
    expressible only if the reasoning stays distinguishable this far up.
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

    Carried on the result block, not parsed back out of `content`: a regex over text the
    model also reads stops firing the day the wording changes, and nothing reports it.
    ADR-0007 rests on these being measured.

    `exit_code` is None when nothing exited: killed on timeout, or refused before a process
    started, told apart by `timed_out`. Neither is 0, a real exit code that must not
    collide with them.

    Here, not in `tools.py`, because `tools.py` imports this module and the block must
    name the type it carries.
    """

    exit_code: int | None
    timed_out: bool = False
    ran: bool = False
    # A command in the line exited non-zero while `exit_code` reads 0, because a later
    # command in the same line succeeded -- the `; echo $?` shape ADR-0007 names as the
    # reason a zero is not proof. False when the status already reports the failure.
    masked_failure: bool = False
    # Pipelines (2+ stages) in which a stage exited non-zero, recorded for the operator
    # transcript only; the model-facing text carries none of it.
    stages: tuple[tuple[tuple[str, int], ...], ...] = ()


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

    A string is rejected loudly, not coerced: coercion would be ADR-0008 condition (a)
    failing silently, the canonical shape still typechecking while it became the OpenAI one.
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


# Two values, not the wire's full set. "required" and naming a tool are real options in
# both formats with no caller here, and adding them would invent an untested contract.
TOOL_CHOICES = ("auto", "none")


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    """One backend call, fully specified.

    `max_tokens`, `effort`, `temperature` and `top_p` carry no defaults. Config defaults
    live only in config.py and per-model overrides only in the registry; a default here
    would be a second copy of a fact, and second copies drift. The caller resolves them
    with `ModelEntry.effective_effort(cfg)` and `ModelEntry.cap_tokens()`.

    Message order is the caller's and never rearranged downstream: system prompt, agent
    body, files block, task last, so the cached prefix stays bit-identical (ADR-0011). The
    adapter translates; it does not schedule.
    """

    system: str
    messages: tuple[Message, ...]
    max_tokens: int
    effort: str
    temperature: float
    top_p: float
    tools: tuple[ToolSpec, ...] = ()
    # "auto" lets the model call; "none" offers the tools and forbids calling them, rather
    # than withdrawing `tools` (ADR-0057). Our own vocabulary, translated per adapter as
    # `effort` is (ADR-0013): the wire formats spell it differently, and neither spelling
    # belongs in the canonical shape.
    tool_choice: str = "auto"

    def __post_init__(self) -> None:
        if self.tool_choice not in TOOL_CHOICES:
            raise ValueError(
                f"tool_choice={self.tool_choice!r} is not one of {TOOL_CHOICES}."
            )
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
        # Narrower than temperature on purpose: the endpoint answers 400 to a top_p above
        # 1.0 while accepting temperature to 2.0.
        if not 0.0 <= self.top_p <= 1.0:
            raise CanonicalShapeError(
                f"top_p={self.top_p} is outside the accepted range 0.0-1.0."
            )


@dataclass(frozen=True, slots=True)
class CanonicalResponse:
    """One backend reply, translated but not interpreted.

    `finish_reason` is the wire value verbatim. Mapping it onto a tidier vocabulary would
    decide what the reply *means*, which is `loop.py`'s, and it needs the raw value. The
    token counts are here for the same reason: reasoning exhaustion is diagnosed by
    comparing spend against the budget (ADR-0014), which needs numbers, not a verdict.

    The optional fields follow that rule. `stop_reason` is vLLM's answer to "which stop
    condition fired", distinct from `finish_reason` and absent on backends that do not
    speak it; `system_fingerprint` identifies the engine build and its configuration. Both
    are carried and neither is interpreted.
    """

    content: tuple[ContentBlock, ...]
    finish_reason: str
    input_tokens: int
    output_tokens: int
    model: str
    # All optional, and `None` means the endpoint did not report the field, a different
    # fact from a zero. A `cached_tokens` of 0 says the prefix missed; absent says nothing
    # can be said about caching on this backend, and collapsing the two would make cache
    # behaviour unmeasurable (ADR-0051).
    cached_tokens: int | None = None
    total_tokens: int | None = None
    stop_reason: str | None = None
    system_fingerprint: str | None = None
    # How long the tokens took to arrive: last token minus first, nothing else. What the
    # rate estimators want and no non-streaming adapter can supply, so `None` means "time
    # the attempt instead", not "zero". Dividing output by the whole attempt charges
    # prefill to the decoder, and on a short answer over a large prompt prefill is most of
    # the interval (ADR-0070).
    decode_seconds: float | None = None
    # The other half of the clock: request sent to *first* token -- queueing and prefill,
    # nothing the decoder did. Prefill grows with the prompt rather than the answer, the
    # half a run summary is trying to see. `None` on an adapter that cannot stream.
    prefill_seconds: float | None = None

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


REASONING_ONLY_BANNER = (
    "[no answer was written: the reply stopped at its token limit with every token spent "
    "reasoning. What follows is that reasoning, returned in place of the empty string it "
    "used to be. Treat it as working notes, not a conclusion.]\n\n"
)

TURN_LIMIT_BANNER = (
    "[this answer was written on the delegation's last turn, with its tools forbidden. It "
    "may be complete, or it may be what could be written when the turns ran out -- if it "
    "reads unfinished, raise max_turns or narrow the task rather than treating it as a "
    "conclusion.]\n\n"
)


def duplicate_line_share(text: str) -> float:
    """How much of a reply is lines it has already said. 0.0 to 1.0.

    The one number that separates a loop from work. A turn looping inside itself and a
    healthy long answer both end `finish_reason: length` with `reasoning_exhausted`
    false, both filling their ceiling exactly -- which reads as "needs a bigger budget"
    and is not.

    Lines, not tokens or n-grams, because the failure repeats whole sentences and a line
    is the unit a reader points at. Stripped, with blank lines and code fences dropped, so
    prose that breathes is not repetition. An empty reply is 0.0, not an error:
    `empty_response` already reports it.

    Reported, not acted on: aborting a turn on it needs a threshold nobody has chosen, and
    one chosen from a few measurements could not be defended.
    """
    lines = [
        stripped for stripped in (line.strip() for line in text.splitlines())
        # A code fence is markup, not something said twice: four code blocks would
        # otherwise read as repeats.
        if stripped and not stripped.startswith(("```", "~~~"))
    ]
    if not lines:
        return 0.0
    return (len(lines) - len(set(lines))) / len(lines)


def answer_of(response: CanonicalResponse) -> tuple[str, bool]:
    """The text a caller is handed, and whether it is reasoning rather than an answer.

    A reply that spent its whole budget thinking parses into a `ThinkingBlock` and no
    `TextBlock`, and `text` joins text blocks only, so the answer would be the empty string
    while every token produced sat in the response, dropped. A length stop is neither a
    timeout nor a cancellation, so a partial returned at a deadline would not rescue it.

    Under a banner, not bare: reasoning is working notes, and a caller that cannot tell it
    from a conclusion is worse off than with the empty string. The flag is what a caller
    branches on; the banner is what a reader sees.

    Only when there is no text at all. Appending reasoning to a reply that *has* an answer
    would change every successful dispatch, and whether thinking travels onward is
    `resend_reasoning`'s question, about history, not about what the caller is handed.

    Here, not in `server.py`, because `transcript.py` needs the same answer and must not
    import the server: a record disagreeing with the reply it records would call these
    replies empty.
    """
    text = response.text
    if text:
        return text, False
    thinking = response.thinking
    if not thinking.strip():
        # Whitespace is what an emptied reasoning stream leaves. A banner over nothing
        # would make `empty_response` unreachable, the one signal a caller has that the
        # dispatch produced nothing.
        return "", False
    return REASONING_ONLY_BANNER + thinking, True


# --- the protocol ---------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """What an adapter must provide. One file per wire format, nothing else changes.

    Async because httpx and fastmcp are: a sync seam would have to be unpicked the moment
    the agentic loop needs two calls in flight.
    """

    async def complete(
        self,
        request: CanonicalRequest,
        *,
        on_token: Callable[[str, str], None] | None = None,
    ) -> CanonicalResponse:
        """Send one request. Raises a BackendError subclass; never returns a partial.

        `on_token` fires each time the wire carries generated output: how anything above
        this layer learns that decoding has begun. It is *alongside* the return value, so
        the contract above holds, and an adapter that cannot stream never calls it.

        Synchronous, and given the frame's kind -- `"reasoning"` or `"answer"` -- and its
        text, so a consumer can tell thinking from answering, and show it, as it happens.
        The text is empty for a frame carrying only a tool-call fragment. It runs on the read loop
        once per frame, so awaiting would put latency between two tokens; a consumer that
        must act once, like the admission lease, guards that itself.
        """
        ...

    async def probe(self) -> tuple[str, ...]:
        """Model ids this endpoint reports serving. The health check, per MODELS.md."""
        ...

    async def probe_window(self) -> int | None:
        """The context window this endpoint reports for its served model, if it says.

        `None` means the endpoint answered without one -- a confirmed absence, not a
        failure to ask. A transport failure raises `BackendUnavailable` instead, so a
        caller never caches "this backend cannot tell me" because the network blinked.

        This CHECKS the operator's `context_window`, never supplies it: an auto-derived
        window would put every threshold against a model file's architecture maximum
        rather than the window actually served.
        """
        ...

    async def probe_cluster(self) -> dict[str, float | int | str | None] | None:
        """What the serving stack says about its own load, if it publishes anything.

        `None` is a confirmed absence, as in `probe_window` and for the same reason; a
        transport failure raises `BackendUnavailable` instead.

        These are the cluster's numbers, not ours. `admission`'s figures are estimates
        from what this machine has admitted, blind to other clients; this is the real
        thing. Values are
        reported, never interpreted, as `finish_reason` is: deciding that a hit rate is
        "bad" is policy, and this layer has none.
        """
        ...

    async def aclose(self) -> None:
        """Release the transport. Safe to call more than once."""
        ...
