"""MCP wiring, and nothing else.

Registering tools, validating arguments, owning the backend cache, and translating
exceptions into something a model can act on; the delegation itself is `loop.py`. The
tool descriptions are the model-facing contract, so rewording one changes behaviour and
carries a CHANGELOG entry.

**stdout is the wire protocol**: anything printed there silently corrupts every later MCP
message, so diagnostics go to stderr (`main.py` holds the other half of that rule).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from typing import Annotated, Any, NamedTuple, Protocol

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from .backends.base import (
    Backend,
    BackendError,
    BackendProtocolError,
    BackendRefused,
    BackendUnavailable,
    CanonicalShapeError,
    TURN_LIMIT_BANNER,
    answer_of,
    duplicate_line_share,
)
from .backends.openai_compat import OpenAICompatBackend
from .admission import Admission, AdmissionError, AdmissionLease, AdmissionTimedOut
from .agents import AgentError, AgentSpec, load_agent
from .agents import survey_agents as discover_agents
from .config import EFFORT_INHERIT, EFFORT_LEVELS, Config, ConfigError
from .context import FileEntry, FileRequest, estimate_text_tokens, prefetch, skip_from_refusal
from .handles import Handles, NotWaitingError, Pause, UnknownHandle
from .loop import (
    AgenticDispatch,
    AliveCallback,
    RunningCalls,
    ContextOverflowAborted,
    Delegation,
    Dispatch,
    DispatchTimedOut,
    InvalidDelegation,
    RateHistory,
    RateSampler,
    resolve_max_turns,
    run_agentic_loop,
    run_one_shot,
)
from .paths import (
    LAYER_FORM,
    PathPolicyError,
    PathRefused,
    Refusal,
    RepoSettingsError,
    expand_globs,
    has_magic,
    load_repo_ext_allowlists,
    narrow_roots,
    resolve_files,
    resolve_workdir,
    resolved_roots,
    resolved_workdir_roots,
)
from .wsl import file_uri_to_posix
from .registry import ModelEntry, Registry, RegistryError
from .slots import (
    SharedSlots,
    SlotsUnavailable,
    build_slots,
    cross_process_status,
    rate_history_path,
)
from . import ledger, provision, transcript
from .tools import READ_ONLY_TOOL_NAMES, BashPolicy, resolve_allowed

SERVER_NAME = "delegate-local"

# The closed vocabulary backend_status() reports. `backend_unreachable` is not ours to
# rename: docs/TROUBLESHOOTING.md promises it to anyone diagnosing a symptom.
STATUS_OK = "ok"
STATUS_UNREACHABLE = "backend_unreachable"
STATUS_AUTH_FAILED = "auth_failed"
STATUS_REFUSED = "backend_refused"
STATUS_PROTOCOL_ERROR = "backend_protocol_error"
STATUS_MISCONFIGURED = "misconfigured"

# What the `answer` tool hands the run when the caller leaves the choice to the model. The
# transcript's `answer` event says so by comparing the text it records against this same
# sentence, so a reader can tell a caller's words from the best-reading instruction.
BEST_READING_REPLY = "The caller left the choice to you: proceed on your best reading."


class ProgressSink(Protocol):
    """Where a delegation reports progress: an MCP `Context`, or a `_CallProgress`."""

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None,
    ) -> None: ...


class _CallProgress:
    """Progress for one read-only call whose run may outlive it.

    A call that returns early with a question leaves its run waiting, so the run must stop
    reporting to a request that has ended: `close` makes this quiet. And the run's turns
    and the call's keepalive share one request, whose progress must rise every time, so
    this numbers both rather than trusting two counters.
    """

    def __init__(self, ctx: Context | None) -> None:
        self._ctx = ctx
        self._count = 0

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None,
    ) -> None:
        if self._ctx is None:
            return
        self._count += 1
        await self._ctx.report_progress(progress=self._count, total=total, message=message)

    def close(self) -> None:
        self._ctx = None


class QuestionNotAnswered(Exception):
    """A run asked its caller a question and no answer came within the limit."""

    def __init__(self, limit_seconds: float) -> None:
        super().__init__(
            f"question not answered within {limit_seconds:g}s"
        )


def _ended(failure: BaseException | None) -> str:
    """How the run ended, as one of `transcript.ENDINGS`, from the exception that ended it.

    `None` is a finished run; a cancellation is a stop; otherwise the exception carries the
    ending where one applies, and anything else is an error.
    """
    if failure is None:
        return "finished"
    if isinstance(failure, asyncio.CancelledError):
        return "stopped"
    if isinstance(failure, AdmissionTimedOut):
        return "queue_timeout"
    ended = getattr(failure, "ended", None)
    if ended in transcript.ENDINGS:
        return ended
    return "error"


def _partial_result(
    error: BaseException, prefetched: Any, agent: AgentSpec | None
) -> dict[str, Any] | None:
    """What a delegation decoded before its deadline kills it, or `None` (ADR-0078).

    `None` unless tokens arrived: an empty partial adds nothing to the deadline message and
    would make a clean failure look like an answer. A *reduced* dict with no tally, since
    zeroes would report a run that used no tools rather than one never counted; the schema
    requires nothing, so that is legal.
    """
    partial = getattr(error, "partial", None)
    if partial is None:
        return None
    answer, answer_is_reasoning = answer_of(partial)
    turns = getattr(error, "turns", None)
    tool_calls = getattr(error, "tool_calls", None)
    return {
        **prefetched.accounting(),
        **({"agent": agent.name, "agent_source": agent.source_path} if agent else {}),
        "answer": answer,
        "answer_is_reasoning": answer_is_reasoning,
        "empty_response": answer == "",
        # Matters most here: whether a run cut off at a deadline was looping or slow.
        "duplicate_line_share": duplicate_line_share(answer),
        # Both keys stop this reading as a success, for callers filtering on either.
        "partial": True,
        "error": str(error),
        "model": partial.model,
        "finish_reason": partial.finish_reason,
        "input_tokens": partial.input_tokens,
        "output_tokens": partial.output_tokens,
        **({"turns": turns} if turns is not None else {}),
        **({"tool_calls": tool_calls} if tool_calls is not None else {}),
    }


def _loop_ledger(dispatched: Dispatch | AgenticDispatch) -> dict[str, Any]:
    """The turn-loop counters, present only when a loop actually ran.

    Absent on the one-shot path, not zero: `tool_calls: 0` would read as a model that
    declined tools it was never offered, which a caller treats differently.
    """
    if not isinstance(dispatched, AgenticDispatch):
        return {}
    return {
        "turns": dispatched.turns,
        "tool_calls": dispatched.tool_calls,
        # Split by tool, since a total cannot tell two reads from two overwrites, and with
        # `transcript_dir` empty by default the only other record is the model's prose.
        "tool_calls_by_name": dict(dispatched.tool_calls_by_name),
        "tool_errors": dispatched.tool_errors,
        "tool_calls_deduplicated": dispatched.deduped,
        "tool_results_evicted": dispatched.evicted,
        # The whole run, where `*_tokens` above is the answering turn: "was it truncated"
        # wants that, "what did it cost" wants these (ADR-0058).
        "total_input_tokens": dispatched.total_input_tokens,
        "total_output_tokens": dispatched.total_output_tokens,
        # None where the endpoint reports no caching at all, which is not a measured zero.
        "total_cached_tokens": dispatched.total_cached_tokens,
        "hit_turn_limit": dispatched.hit_turn_limit,
        # Zero, not absent, once a loop ran: offered `run_bash` and unused is a real answer.
        "bash_calls": dispatched.bash_calls,
        "bash_failures": dispatched.bash_failures,
        "bash_masked_failures": dispatched.bash_masked_failures,
        "failed_calls": dispatched.failed_calls,
        # None: nothing exited (none ran, or killed on timeout); 0 is a real code.
        # (ADR-0007)
        "last_bash_exit": dispatched.last_bash_exit,
    }


def _diagnostics_block(
    dispatched: Dispatch | AgenticDispatch, *, requested: bool
) -> dict[str, Any]:
    """The per-turn ledger, present only when the caller asked and a loop actually ran.

    Absent rather than empty, as in `_loop_ledger`. `evicted_then_reread` is the field
    worth reading: it tells a large job from one re-paying for dropped context, which a
    larger `keep_tool_results` fixes; no other number distinguishes them.
    """
    if not requested or not isinstance(dispatched, AgenticDispatch):
        return {}
    return {
        "diagnostics": {
            "turns": [
                {
                    "turn": t.turn,
                    "input_tokens": t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "cached_tokens": t.cached_tokens,
                    "attempts": t.attempts,
                    "effort": t.effort,
                    "tool_results_evicted": t.evicted,
                    "tool_calls": [c.as_json() for c in t.tool_calls],
                }
                for t in dispatched.diagnostics
            ],
            "evicted_then_reread": [
                {
                    "path": r.path,
                    "evicted_at_turn": r.evicted_at_turn,
                    "reread_at_turn": r.reread_at_turn,
                }
                for r in dispatched.rereads
            ],
        }
    }


class BackendCache:
    """One backend -- and so one httpx connection pool -- per registry entry.

    Kept for the server's life, so no rebuild discards a warm pool and `backend_status()`
    never opens a second one.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._backends: dict[str, Backend] = {}

    def get(self, entry: ModelEntry) -> Backend:
        backend = self._backends.get(entry.key)
        if backend is None:
            backend = OpenAICompatBackend(self._cfg, entry)
            self._backends[entry.key] = backend
        return backend

    async def aclose(self) -> None:
        # aclose() is documented safe to call more than once, so teardown needs no guard.
        for backend in self._backends.values():
            await backend.aclose()
        self._backends.clear()


class WindowCheck:
    """Whether overflow handling may be armed for a model, and why not when it may not.

    Every threshold is a share of `context_window`, which the operator wrote and nothing
    verified; armed against a wrong one, thresholds sit where the backend never reaches,
    so the window is checked once per model. It **validates and never derives**: a
    disagreement disarms and says so, since the operator's file is the truth for the rest
    of the model and a field silently overridden is worse than one wrong in the open.

    Agrees, or no window against one the operator wrote: armed. **Answered** and disagrees,
    or no window against a defaulted one: disarmed, cached, re-checked on expiry.
    Unreachable: disarmed for this call, not cached, so an outage cannot disable overflow
    handling until restart.
    """

    def __init__(self, cfg: Config, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._clock = clock
        self._verdicts: dict[str, tuple[str, float]] = {}

    async def armed(self, backend: Backend, entry: ModelEntry) -> tuple[bool, str]:  # noqa: PLR0911 -- one return per verdict the docstring lists
        """May overflow handling act for this model? Returns the verdict and a reason."""
        if not self._cfg.context_overflow_enabled:
            return False, ""
        cached = self._verdicts.get(entry.key)
        if cached is not None and self._clock() - cached[1] < self._cfg.overflow_probe_cache_ttl:
            return not cached[0], cached[0]

        try:
            reported = await backend.probe_window()
        except BackendUnavailable:
            # Learned nothing; not cached, or an outage disables the feature until restart.
            return False, "the endpoint could not be reached to check its context window"
        except (BackendRefused, BackendProtocolError) as e:
            reason = f"the endpoint refused the context-window check ({type(e).__name__})"
            self._verdicts[entry.key] = (reason, self._clock())
            return False, reason

        if reported is not None and reported != entry.context_window:
            # Never adopted: a threshold on the wrong window is the bug this exists to
            # avoid. The advice depends on where the number came from: naming a
            # `models.toml` key that is not there sends someone to fix a missing line, and
            # when defaulted the endpoint has just told us the right value to name.
            if entry.context_window_defaulted:
                source = (
                    f"models.toml sets no context_window for {entry.key!r}, so the "
                    f"default of {entry.context_window} was assumed"
                )
                remedy = f"Set context_window={reported} in models.toml to arm it."
            else:
                source = (
                    f"models.toml gives context_window={entry.context_window} for "
                    f"{entry.key!r}"
                )
                remedy = "Correct models.toml to arm it."
            reason = (
                f"{source}, but the endpoint reports {reported}. Overflow handling "
                "stays off rather than compute every threshold against a number one of "
                f"the two disagrees with. {remedy}"
            )
            self._verdicts[entry.key] = (reason, self._clock())
            return False, reason

        if reported is None and entry.context_window_defaulted:
            # Nobody chose the number and nothing confirmed it: every threshold would be a
            # share of a guess, which is the case this check exists for.
            reason = (
                f"models.toml sets no context_window for {entry.key!r} and the endpoint "
                f"reports none, so the default of {entry.context_window} is unconfirmed. "
                "Overflow handling stays off rather than compute every threshold against "
                "it. Set context_window in models.toml to arm it."
            )
            self._verdicts[entry.key] = (reason, self._clock())
            return False, reason

        self._verdicts[entry.key] = ("", self._clock())
        return True, ""


async def arm_overflow(
    windows: WindowCheck, backend: Backend, entry: ModelEntry, cfg: Config, *, agentic: bool
) -> tuple[Config, str]:
    """The config the turn loop should run under, and why it differs if it does.

    Here, so the loop reads one switch. A *new* config, not the server's mutated, so a
    briefly unreachable model is disarmed for this delegation only.
    """
    if not agentic or not cfg.context_overflow_enabled:
        # A one-shot has nothing to overflow and must not spend a round trip learning it.
        return cfg, ""
    armed, reason = await windows.armed(backend, entry)
    if armed:
        return cfg, ""
    return replace(cfg, context_overflow_enabled=False), reason


async def dispatch_delegation(  # noqa: PLR0913 -- one seam and four resolved arguments
    cfg: Config,
    entry: ModelEntry,
    backend: Backend,
    delegation: Delegation,
    *,
    allowed: frozenset[str],
    effort: str | None,
    max_tokens: int | None,
    max_turns: int | None = None,
    policy: BashPolicy | None = None,
    prefetched_files: tuple[FileEntry, ...] = (),
    diagnostics: bool = False,
    report_progress: Callable[[int, int], Awaitable[None]],
    on_alive: AliveCallback | None = None,
    on_turn_done: Callable[[Any, str, float], Awaitable[None]] | None = None,
    on_priced: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_tools: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_pool: Callable[[int | None], None] | None = None,
    rate_history: RateHistory | None = None,
    expected_concurrency: int = 1,
    concurrency_now: Callable[[], Awaitable[int]] | None = None,
    on_token: Callable[[], None] | None = None,
    on_partial: Callable[[int, str, str], None] | None = None,
    ask: Callable[[list[str]], Awaitable[str]] | None = None,
) -> Dispatch | AgenticDispatch:
    """Run the delegation on whichever path the toolset implies, and translate its failures.

    Together because the path decides which failures are possible, and each must reach the
    caller as a `ToolError`, never a traceback. The only part of `build()` not wiring.
    """
    try:
        if allowed:
            return await run_agentic_loop(
                cfg, entry, backend, delegation,
                allowed=allowed, effort=effort, max_tokens=max_tokens,
                max_turns=max_turns, policy=policy, prefetched=prefetched_files,
                diagnostics=diagnostics, report_progress=report_progress,
                on_alive=on_alive, on_turn_done=on_turn_done, on_priced=on_priced,
                on_tools=on_tools,
                on_pool=on_pool,
                rate_history=rate_history, expected_concurrency=expected_concurrency,
                concurrency_now=concurrency_now,
                on_token=on_token, on_partial=on_partial,
                ask=ask,
            )
        # An explicitly empty toolset: the one-shot prompt tells the model it can open
        # nothing and has no second turn, true only here. `on_alive`, since a one-shot has
        # no turn budget to report progress against, only time waited.
        return await run_one_shot(
            cfg, entry, backend, delegation, effort=effort, max_tokens=max_tokens,
            on_alive=on_alive, on_priced=on_priced, on_pool=on_pool,
            rate_history=rate_history, expected_concurrency=expected_concurrency,
            on_token=on_token, on_partial=on_partial,
        )
    except ContextOverflowAborted as e:
        # Before its base class. The report is the point: an abort lands mid-work, and the
        # caller needs the server's ledger beside what git says is on disk.
        raise ToolError(f"{e}\n\n{json.dumps(e.report, indent=2, default=str)}") from e
    except InvalidDelegation as e:
        raise ToolError(str(e)) from e
    except DispatchTimedOut as e:
        # Not `_refuse`, which blames an endpoint: this is the operator's deadline, and the
        # message already says elapsed, limit and stage.
        failed = ToolError(str(e))
        # Carried across the one conversion that would drop it (ADR-0078).
        failed.partial = e.partial  # type: ignore[attr-defined]
        failed.turns = e.turns  # type: ignore[attr-defined]
        failed.tool_calls = e.tool_calls  # type: ignore[attr-defined]
        # Which deadline fired: a stall stopped producing, a deadline merely ran long.
        failed.ended = "stalled" if e.stalled else "deadline"  # type: ignore[attr-defined]
        raise failed from e
    except (BackendUnavailable, BackendRefused, BackendProtocolError) as e:
        raise _refuse(e) from e


async def probe_entry(
    cache: BackendCache, cfg: Config, entry: ModelEntry, *, is_default: bool
) -> dict[str, Any]:
    """Probe one registry entry and describe it. Never raises.

    A dead endpoint is a finding, not a failure, so one cannot take down the report. Errors
    return as data, so the gather needs no `return_exceptions=True`, which would swallow a
    real bug in a sibling. Nothing here names the endpoint. ADR-0029.
    """
    row: dict[str, Any] = {
        "key": entry.key,
        "is_default": is_default,
        "api_format": entry.api_format,
        "served_model_id": entry.served_model_id,
        "context_window": entry.context_window,
        # The operator's claim or our assumption: like `id_confirmed`, a healthy endpoint
        # the registry may still describe wrongly.
        "context_window_defaulted": entry.context_window_defaulted,
        "concurrency": entry.concurrency,
        "status": STATUS_OK,
        "id_confirmed": None,
        "detail": "",
    }

    try:
        backend = cache.get(entry)
    except (ConfigError, CanonicalShapeError) as e:
        # Settings problems that never reached the network; saying so beats "unreachable".
        row["status"] = STATUS_MISCONFIGURED
        row["detail"] = str(e)
        return row

    try:
        served = await asyncio.wait_for(backend.probe(), timeout=cfg.status_probe_timeout)
    except TimeoutError:  # asyncio.TimeoutError is an alias of this on 3.11+
        row["status"] = STATUS_UNREACHABLE
        row["detail"] = (
            f"No answer within status_probe_timeout ({cfg.status_probe_timeout}s). A "
            "refused connection fails at once, so a silent wait is a dropped route or a "
            "host that is not listening."
        )
        return row
    except BackendUnavailable as e:
        row["status"] = STATUS_UNREACHABLE
        row["detail"] = str(e)
        return row
    except BackendRefused as e:
        # A bad key means editing .env, not paging whoever owns the hardware.
        row["status"] = STATUS_AUTH_FAILED if e.status in (401, 403) else STATUS_REFUSED
        row["detail"] = f"The endpoint answered {e.status} for {e.url_path}."
        return row
    except BackendProtocolError as e:
        row["status"] = STATUS_PROTOCOL_ERROR
        row["detail"] = str(e)
        return row

    # Reachable; does it serve what we think? Exact equality, as `Registry.resolve()`: a
    # different version suffix is a different model.
    row["id_confirmed"] = entry.served_model_id in served
    if not row["id_confirmed"]:
        row["detail"] = (
            f"Reachable, but it does not serve {entry.served_model_id!r}. It lists "
            f"{len(served)} model(s). Requests to this entry will be refused by the "
            "endpoint, or worse, answered by a different model than the one configured."
        )

    # The cluster's own figures. Never changes the status: an endpoint with no metrics is
    # healthy with nothing to say, and calling it degraded would raise false alarms.
    try:
        row["cluster"] = await asyncio.wait_for(
            backend.probe_cluster(), timeout=cfg.status_probe_timeout
        )
    except (TimeoutError, BackendError):
        row["cluster"] = None
    return row


@asynccontextmanager
async def sampling(sampler: RateSampler) -> AsyncIterator[None]:
    """Run the rate sampler for the life of the block, and never a moment past it.

    Started here because a task needs a running loop and `build` has none. Nested inside
    the backend cache's teardown, so the ticker stops before its pool closes. Cancelled
    *and awaited*, since `cancel()` only schedules: without the await one more scrape can
    run after shutdown, a traceback from a monitoring read. Public so a test can assert
    the task is gone.
    """
    ticker = asyncio.create_task(sampler.run()) if sampler.enabled else None
    try:
        yield
    finally:
        if ticker is not None:
            ticker.cancel()
            with suppress(asyncio.CancelledError):
                await ticker


def admission_deadline(cfg: Config) -> float | None:
    """When to stop waiting for a slot, or None to wait for as long as the work takes.

    Public because it is the whole of ADR-0093's behaviour; `acquire` treats `None` as no
    deadline.
    """
    if cfg.admission_wait_timeout <= 0:
        return None
    return time.monotonic() + cfg.admission_wait_timeout


def _refuse(e: Exception) -> ToolError:
    """Translate a backend failure into something the caller can act on.

    FastMCP passes a ToolError through verbatim, so these strings are contract and what
    docs/TROUBLESHOOTING.md indexes by. The distinctions are the point: unreachable is
    someone else's hardware, refused usually a wrong path, a protocol error the wrong stack.
    """
    if isinstance(e, BackendUnavailable):
        return ToolError(f"{STATUS_UNREACHABLE}: {e}")
    if isinstance(e, BackendRefused):
        word = STATUS_AUTH_FAILED if e.status in (401, 403) else STATUS_REFUSED
        return ToolError(f"{word}: the endpoint answered {e.status} for {e.url_path}.")
    if isinstance(e, BackendProtocolError):
        return ToolError(f"{STATUS_PROTOCOL_ERROR}: {e}")
    return ToolError(str(e))


class _OneShotTurn:
    """A one-shot dispatch, shaped like the per-turn record the stream expects.

    A one-shot never builds a `TurnDiagnostic`, so rather than teach the stream a second
    shape it gets this: turn one, the dispatch's tokens, no tool calls.
    """

    __slots__ = ("answered_decode_seconds", "attempts", "cached_tokens", "decode_seconds",
                 "effort", "evicted", "input_tokens", "output_tokens", "prefill_seconds",
                 "reasoning_seconds", "tool_calls", "turn")

    def __init__(self, dispatched: Any) -> None:
        self.turn = 1
        # Zero, a measurement: a one-shot has no history to evict from.
        self.evicted = 0
        self.input_tokens = dispatched.response.input_tokens
        self.output_tokens = dispatched.response.output_tokens
        self.cached_tokens = dispatched.response.cached_tokens
        self.effort = dispatched.effort
        self.attempts = dispatched.attempts
        self.tool_calls = ()
        # A one-shot retries an empty answer too, so these are the loop's sums off the same
        # `Dispatch`; the rate divides by the answering attempt's decode span.
        self.prefill_seconds = dispatched.prefill_seconds
        self.decode_seconds = dispatched.decode_seconds
        self.answered_decode_seconds = dispatched.response.decode_seconds
        # Measured per response, not summed: only the answering attempt's reasoning counts.
        self.reasoning_seconds = dispatched.response.reasoning_seconds


def tool_ms_for_turn(diagnostic: Any) -> int:
    """What one finished turn spent running the model's tools, per the calls' own clocks.

    Summed from each call's own `ms`. Turn wall clock minus the backend call would charge
    the slot grant, pricing and request assembly to the first turn's tools.
    """
    return sum(c.ms or 0 for c in getattr(diagnostic, "tool_calls", ()) or ())


# Every timing figure `run_delegation` reports is read from this, never from `time`
# directly, so a test can drive them from a fake clock.
_clock: Callable[[], float] = time.monotonic

# How often a queued delegation's `waiting` event reaches the transcript. Half the viewer's
# once-a-minute repeat, so its line never lags by more than one interval.
_WAITING_EVERY_SECONDS = 30.0


async def _shared_concurrency_now(
    slots: SharedSlots | None, *, cap: int, fallback: int
) -> int:
    """The cluster's concurrency now, or the admission figure when it cannot say.

    From admission's own snapshot, where this request is already in `seqs`: no `+ 1`.
    """
    if slots is None:
        return fallback
    try:
        totals, _, _ = await slots.snapshot()
    except SlotsUnavailable:
        return fallback
    return min(max(totals.seqs + totals.waiting, 1), cap)


async def run_delegation(  # noqa: PLR0913, PLR0915, PLR0912 -- one tool's arguments,
    # one dispatch; each branch is an ending that must close the stream and write a record.
    cfg: Config,
    registry: Registry,
    cache: BackendCache,
    windows: WindowCheck,
    admission: Admission,
    *,
    # Keyword-only, so a sixth positional cannot be transposed silently at a call site.
    rates: RateHistory,
    task: str,
    title: str | None = None,
    files: list[str | FileRange] | None = None,
    model: str | None = None,
    effort: str | None = None,
    allowed_tools: list[str] | None = None,
    max_tokens: int | None = None,
    max_turns: int | None = None,
    agent: AgentSpec | None = None,
    workdir: str | None = None,
    diagnostics: bool = False,
    ctx: ProgressSink | None = None,
    tool_name: str = "delegate",
    ask: Callable[[list[str]], Awaitable[str]] | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """One delegation, from arguments to the result dict, for every tool that runs one.

    The tools differ only in where arguments come from; a second dispatch path is how the
    halves of a precedence rule drift, and precedence is what an agent file is. The agent
    arrives loaded and validated, and `workdir` resolved and root-checked; this applies
    them.
    """
    # The only place `"inherit"` exists, spent before the agent merge: that merge is
    # `effort or agent.effort`, and the truthy string would skip the tier it defers to.
    # Refused here, where the message can name it (ADR-0045).
    if effort is not None and effort != EFFORT_INHERIT and effort not in EFFORT_LEVELS:
        raise ToolError(
            f"effort={effort!r} is not one of {EFFORT_LEVELS}, and is not "
            f"{EFFORT_INHERIT!r}. These are this project's levels, which the adapter "
            "translates into the server's own vocabulary, and an unlisted one has no "
            f"translation. Pass {EFFORT_INHERIT!r} to defer to the agent file, the "
            "registry row and then the configured default, in that order."
        )
    if effort == EFFORT_INHERIT:
        effort = None
    # Precedence, once: call argument, agent file, registry row, global default. One
    # `ModelEntry` serves the cache key, the dispatch and the concurrency count, so a
    # request cannot be counted against one endpoint and sent to another.
    try:
        entry = registry.resolve(model or (agent.model if agent else None))
    except RegistryError as e:
        raise ToolError(str(e)) from e  # already names the registered keys

    if agent is not None:
        # Call argument beats agent file, `max_turns` included; the hard cap still binds.
        effort = effort or agent.effort
        max_tokens = max_tokens or agent.max_tokens
        max_turns = max_turns or agent.max_turns
        if allowed_tools is None and agent.allowed_tools is not None:
            allowed_tools = list(agent.allowed_tools)
        if agent.keep_tool_results is not None:
            # `replace`, as `arm_overflow` does, since `run_agentic_loop` is at its argument
            # limit. The tightened figure moves too: `Config` refuses one above the ordinary
            # (tightening must narrow), so keeping fewer would otherwise fail mid-call.
            cfg = replace(
                cfg,
                keep_tool_results=agent.keep_tool_results,
                overflow_tightened_keep_tool_results=min(
                    cfg.overflow_tightened_keep_tool_results, agent.keep_tool_results
                ),
            )

    # Before the backend lookup, so a refused path costs nothing whatever the cluster's
    # state. A ranged entry with a bad shape or a glob is refused alone, never clamped,
    # into `files_skipped`. On a thread: it stats, globs, runs git and reads files.
    plain: list[str] = []
    ranged: list[tuple[str, int, int | None]] = []  # path, start_line, end_line
    range_refusals: list[Refusal] = []
    for raw in files or []:
        if isinstance(raw, str):
            plain.append(raw)
            continue
        path = raw.path
        start = raw.start_line
        end = raw.end_line
        if has_magic(path):
            range_refusals.append(Refusal(
                given=path, layer=LAYER_FORM,
                reason="it is a glob, and a ranged entry names one file, not many.",
                remedy="Name the single file, and let the range choose the part of it.",
            ))
        elif start < 1:
            range_refusals.append(Refusal(
                given=path, layer=LAYER_FORM,
                reason=f"start_line {start} is not a line number; lines are counted from 1.",
                remedy="Give a 1-based start_line, or name the file plainly without a range.",
            ))
        elif end is not None and end < start:
            range_refusals.append(Refusal(
                given=path, layer=LAYER_FORM,
                reason=f"end_line {end} is before start_line {start}, so the range is empty.",
                remedy=(
                    "Give an end_line at or after start_line, or omit it to read to the "
                    "end of the file."
                ),
            ))
        else:
            ranged.append((path, start, end))

    try:
        # Expanded, then resolved, so a match meets the same four layers; one `prefetch`
        # over it all, since per-batch prefetches would each get the full budget. ADR-0097
        named, glob_refusals = await asyncio.to_thread(expand_globs, cfg, plain)
        plain_resolved, plain_refusals = await asyncio.to_thread(resolve_files, cfg, named)
    except PathPolicyError as e:
        raise ToolError(f"{STATUS_MISCONFIGURED}: {e}") from e

    # One at a time, since `resolve_files` deduplicates by path, and every range of a file
    # must reach `prefetch`, which merges them knowing their sizes (ADR-0106).
    requests: list[FileRequest] = [FileRequest(entry=r) for r in plain_resolved]
    ranged_refusals: list[Refusal] = []
    for path, start, end in ranged:
        try:
            survivors, path_refusals = await asyncio.to_thread(resolve_files, cfg, [path])
        except PathPolicyError as e:
            raise ToolError(f"{STATUS_MISCONFIGURED}: {e}") from e
        if path_refusals:
            ranged_refusals.extend(path_refusals)
            continue
        if not survivors:
            continue
        requests.append(FileRequest(entry=survivors[0], start_line=start, end_line=end))

    # A pattern's refusal first: it explains files missing entirely.
    refusals = list(glob_refusals) + range_refusals + list(plain_refusals) + list(ranged_refusals)

    # Fatal only when every path is refused: a prompt with none of its context wastes the
    # delegation, and the caller has one mistake to fix. Otherwise the rest still serve.
    # Why: ADR-0061.
    if refusals and not requests:
        raise ToolError(str(PathRefused(list(refusals), total=len(files or []))))

    prefetched = await asyncio.to_thread(prefetch, cfg, tuple(requests))
    if refusals:
        # Ahead of the budget skips: the caller's own error, and the list is read top-down.
        prefetched = replace(
            prefetched,
            skips=tuple(skip_from_refusal(r) for r in refusals) + prefetched.skips,
        )

    try:
        backend = cache.get(entry)
    except (ConfigError, CanonicalShapeError) as e:
        raise ToolError(f"{STATUS_MISCONFIGURED}: {e}") from e

    delegation = Delegation(
        task=task,
        files_block=prefetched.block(),
        agent_body=agent.body if agent else "",
        workdir=workdir,
    )
    allowed = resolve_allowed(allowed_tools, cfg)
    policy = BashPolicy(
        workdir=workdir,
        network=agent.network if agent else False,
        extra_binds=agent.extra_binds if agent else (),
    )

    notified = 0

    async def progress(turn: int, of: int, message: str | None = None) -> None:
        """ADR-0018: what stops the client abandoning a running delegation.

        The client's idle timer is 1800s against a 3600s default `dispatch_timeout`.
        Nothing renders it or cancels through it; resetting the timer is its job. One
        counter for turns and heartbeats, because MCP requires the value to rise each time.
        No `total`: the turn cap is a ceiling, not a count, and `0` reads as "zero of
        zero". What happened goes in `message`.
        """
        nonlocal notified
        if ctx is not None:
            notified += 1
            text = message or (f"turn {turn} of {of}" if of else None)
            await ctx.report_progress(progress=notified, message=text)

    loop_cfg, overflow_off_because = await arm_overflow(
        windows, backend, entry, cfg, agentic=bool(allowed)
    )

    # Sized before anything is sent, as two numbers: the prompt is prefilled, and prompt
    # plus reply occupies the KV pool. Fixed per delegation, an approximation (see
    # `admission`). The limit comes from the same resolved `entry` as the dispatch.
    prefill_estimate = (
        prefetched.total_tokens
        + estimate_text_tokens(cfg, task)
        + (estimate_text_tokens(cfg, agent.body) if agent else 0)
    )
    tokens_estimate = prefill_estimate + entry.cap_tokens(
        cfg.max_tokens if max_tokens is None else max_tokens
    )

    stream = transcript.open_stream(cfg, agent.name if agent else None)
    turns_streamed = 0
    turn_clock = _clock()
    streamed_out_tokens = 0
    streamed_backend_ms = 0
    # Summed, since every turn resends the history. `None` until reported, so an endpoint
    # without caching reads absent, not "measured, saved nothing".
    streamed_cached_tokens: int | None = None
    # Backend time, summed likewise; `None` means the adapter cannot time itself.
    streamed_prefill_seconds: float | None = None
    streamed_decode_seconds: float | None = None
    # Tool time from each call's own `ms` (`tool_ms_for_turn`).
    streamed_tool_ms = 0

    async def streamed_turn(diagnostic: Any, text: str, backend_seconds: float) -> None:
        """Each finished turn, appended while the delegation is still running.

        Timed here: a watcher wants wall clock between turns, slot wait and tools
        included, not the backend call alone.
        """
        nonlocal turns_streamed, turn_clock, streamed_out_tokens, streamed_backend_ms
        nonlocal streamed_cached_tokens
        nonlocal streamed_prefill_seconds, streamed_decode_seconds
        nonlocal streamed_tool_ms
        now = _clock()
        turns_streamed += 1
        backend_ms = int(backend_seconds * 1000)
        streamed_tool_ms += tool_ms_for_turn(diagnostic)
        streamed_out_tokens += getattr(diagnostic, "output_tokens", 0) or 0
        streamed_backend_ms += backend_ms
        cached = getattr(diagnostic, "cached_tokens", None)
        if cached is not None:
            streamed_cached_tokens = (streamed_cached_tokens or 0) + cached
        prefill = getattr(diagnostic, "prefill_seconds", None)
        if prefill is not None:
            streamed_prefill_seconds = (streamed_prefill_seconds or 0.0) + prefill
        decode = getattr(diagnostic, "decode_seconds", None)
        if decode is not None:
            streamed_decode_seconds = (streamed_decode_seconds or 0.0) + decode
        if stream is not None:
            stream.turn(diagnostic, text, ms=int((now - turn_clock) * 1000),
                        backend_ms=backend_ms, of_turns=resolved_turns)
        turn_clock = now

    # The loop's own function and arguments, so they agree; needed here because the
    # stream's head is written first. None for the one-shot path, which has no budget.
    resolved_turns = resolve_max_turns(cfg, max_turns, allowed) if allowed else None

    if stream is not None:
        stream.start(
            tool=tool_name,
            task=task, title=title, agent=agent.name if agent else None,
            model_key=entry.key, effort=effort,
            tools=allowed, prefetched=prefetched,
            max_turns=resolved_turns,
            workspace=workspace,
        )

    waiting_since = _clock()
    waiting_written: float | None = None

    async def ticked() -> None:
        """ADR-0018 again, for a queued delegation, which runs no turns to reset the idle
        timer and would be abandoned like a slow one.

        Written to the transcript too, since a queued delegation and a killed server leave
        identical files (ADR-0072). The wire hears every tick; the transcript one per
        `_WAITING_EVERY_SECONDS`, the first at once so a viewer shows `queued`. Writing
        every quarter-second poll would put megabytes into a synced folder.
        """
        nonlocal waiting_written
        now = _clock()
        await progress(0, 0, f"queued {now - waiting_since:.0f}s")
        if stream is not None and (
            waiting_written is None or now - waiting_written >= _WAITING_EVERY_SECONDS
        ):
            waiting_written = now
            stream.waiting(
                waited_seconds=now - waiting_since,
                of_seconds=cfg.admission_wait_timeout,
            )

    async def alive(elapsed_seconds: float, of_seconds: int, ends_in: float,  # noqa: PLR0913, PLR0917 -- the heartbeat's positional arity, fixed by _keepalive
                    chunks_seen: int = 0, reasoning_chunks: int = 0,
                    since_chunk: float | None = None, running: RunningCalls = ()) -> None:
        """The one-shot's heartbeat, on the wire and in the transcript (ADR-0018).

        A single backend call sends nothing between `start` and `end`, so without this the
        client's 1800s idle timeout abandons a working call, and the viewer cannot tell a
        silent stream from a dead server. `reasoning_chunks` tells thinking from answering,
        which the total cannot.
        """
        await progress(0, 0, f"working, {elapsed_seconds:.0f}s")
        if stream is not None:
            stream.alive(elapsed_seconds=elapsed_seconds, of_seconds=of_seconds,
                         ends_in_seconds=ends_in, chunks_seen=chunks_seen,
                         reasoning_chunks=reasoning_chunks,
                         since_chunk_seconds=since_chunk, running=running)

    # Captured before the attempt, never re-derived: a failure path must still have the
    # agent's name, which is in scope only here (ADR-0024).
    async def priced(row: dict) -> None:
        """What the turn about to run was allowed, and what that was calculated from.

        Written first, since a turn killed at a deadline writes no `turn` event.
        """
        if stream is not None:
            stream.priced(**row)

    async def announced(row: dict) -> None:
        """The calls a turn is about to run, before any of them does."""
        if stream is not None:
            stream.tools(**row)

    def generating(turn: int, reasoning: str, answer: str) -> None:
        """The reply so far, for a person watching (M19.5). Never the record: `turn` is."""
        if stream is not None:
            stream.partial(turn=turn, reasoning=reasoning, answer=answer)

    started = _clock()
    lease: AdmissionLease | None = None
    dispatched: Dispatch | AgenticDispatch | None = None
    failure: BaseException | None = None
    try:
        async with admission.admit(
            tokens_estimate,
            entry_key=entry.key,
            entry_limit=entry.concurrency,
            deadline=admission_deadline(cfg),
            on_wait=ticked,
        ) as lease:
            # The concurrency about to be *met*: +1 for ourselves (`seqs_at_grant` excludes
            # us) plus everyone queued, capped at what the gate permits.
            expected = min(
                lease.seqs_at_grant + lease.waiting_at_grant + 1, cfg.max_inflight_seqs
            )

            # Re-read per turn, so one that started alone and is now one of six is priced
            # at six-wide; the admission figure when the shared file cannot say.
            async def concurrency_now() -> int:
                return await _shared_concurrency_now(
                    admission._slots, cap=cfg.max_inflight_seqs, fallback=expected
                )

            # The `ask` callback, plus its transcript: the pause and its cost are events a
            # watcher reads while the run waits. Wrapped only when both halves exist -- no
            # caller to answer means nothing to record, no stream means nowhere to record
            # it -- and only then is the wait measured, so a run with neither still asks
            # exactly as before.
            loop_ask = ask
            if ask is not None and stream is not None:
                async def _asked(questions: list[str]) -> str:
                    stream.question(questions)
                    wait_started = _clock()
                    # If the question is never answered this raises and no `answer` is
                    # written: there was none. The stream then ends as it would without
                    # this wrapper, on the unanswered run.
                    text = await ask(questions)
                    stream.answer(text, _clock() - wait_started,
                                  text == BEST_READING_REPLY)
                    return text
                loop_ask = _asked

            dispatched = await dispatch_delegation(
                loop_cfg, entry, backend, delegation,
                allowed=allowed, effort=effort, max_tokens=max_tokens,
                max_turns=max_turns,
                policy=policy,
                prefetched_files=prefetched.files,
                # The caller's flag decides what the caller sees; a transcript asks for
                # itself, or the operator record would depend on the caller asking.
                diagnostics=diagnostics or transcript.enabled(cfg),
                report_progress=progress,
                on_alive=alive,
                on_turn_done=streamed_turn,
                on_priced=priced,
                on_tools=announced,
                on_partial=generating if stream is not None else None,
                # Only while the gate has not seen the pool size, so the scrape can be
                # skipped once it has. Why: ADR-0081.
                on_pool=None if admission.pool_known else admission.observe_pool,
                rate_history=rates,
                expected_concurrency=expected,
                concurrency_now=concurrency_now,
                ask=loop_ask,
            )
    except AdmissionError as e:
        # Not `_refuse`, which blames an endpoint; nothing here reached one.
        failure = e
        raise ToolError(str(e)) from e
    except PathPolicyError as e:
        # Met inside the dispatch -- declaring `search_files` reads the denylist -- and
        # reported as the prefetch reports the same fault, so it reads one way wherever met.
        failure = e
        raise ToolError(f"{STATUS_MISCONFIGURED}: {e}") from e
    except QuestionNotAnswered as e:
        # A question the caller never answered: no backend fault to blame, and nothing was
        # decoded to salvage -- a plain failure, `ok: false` with the reason.
        failure = e
        return {
            **prefetched.accounting(),
            **({"agent": agent.name, "agent_source": agent.source_path} if agent else {}),
            "answer": "",
            "empty_response": True,
            "ok": False,
            "error": str(e),
        }
    except BaseException as e:
        failure = e
        # A deadline after some decoding returns what was decoded (ADR-0078), still as a
        # failure: `partial` and `error` are set, so no timeout reads as success. With no
        # tokens, re-raised: the deadline message already describes that.
        partial = _partial_result(e, prefetched, agent)
        if partial is None:
            raise
        return partial
    finally:
        # A side effect only: no value here for the return to merge into the response.
        if stream is not None:
            # A one-shot streamed no turns, and a start and end with nothing between would
            # look exactly like a delegation that produced nothing.
            if turns_streamed == 0 and dispatched is not None:
                stream.turn(
                    _OneShotTurn(dispatched), dispatched.response.text,
                    ms=int((_clock() - turn_clock) * 1000),
                    backend_ms=int((_clock() - turn_clock) * 1000),
                )
                streamed_out_tokens = dispatched.response.output_tokens or 0
                streamed_backend_ms = int((_clock() - turn_clock) * 1000)
                streamed_cached_tokens = dispatched.response.cached_tokens
                # `getattr`: an `AgenticDispatch` keeps these per turn, though that path
                # streamed its turns and never reaches here.
                streamed_prefill_seconds = getattr(dispatched, "prefill_seconds", None)
                streamed_decode_seconds = getattr(dispatched, "decode_seconds", None)
            stream.end(
                ok=failure is None,
                ended=_ended(failure),
                # A timeout has no `dispatched`, so the exception supplies what the loop
                # managed, or the record would say a failed run did nothing. `None` for
                # every other failure, where nothing counted.
                turns=(
                    getattr(dispatched, "turns", 1) if dispatched
                    else failure.turns if isinstance(failure, DispatchTimedOut)
                    else None
                ),
                elapsed_seconds=_clock() - started,
                # A one-shot or a failure may have no budget; 0 means "not a loop" and is
                # sent as None, not rendered "6 of 0 turns".
                max_turns=getattr(dispatched, "max_turns", 0) or None,
                input_tokens=getattr(dispatched, "total_input_tokens", 0) or None,
                output_tokens=streamed_out_tokens or None,
                cached_tokens=streamed_cached_tokens,
                backend_ms=streamed_backend_ms or None,
                prefill_seconds=streamed_prefill_seconds,
                decode_seconds=streamed_decode_seconds,
                tool_seconds=streamed_tool_ms / 1000,
                # Whole, so stream and record compute the ledger once and cannot drift.
                dispatched=dispatched,
                error=str(failure) if failure is not None else None,
                finish_reason=getattr(
                    getattr(dispatched, "response", None), "finish_reason", None
                ),
            )
        transcript.write(
            cfg,
            agent_name=agent.name if agent else None,
            entry=entry,
            task=task,
            workdir=workdir,
            prefetched=prefetched,
            lease=lease,
            dispatched=dispatched,
            error=failure,
            started=started,
            tool=tool_name,
            tools=allowed,
        )
        ledger.append(
            cfg,
            ledger.record_for(
                tool=tool_name,
                agent_name=agent.name if agent else None,
                entry=entry,
                effort=effort,
                dispatched=dispatched,
                error=failure,
                started=started,
            ),
        )

    response = dispatched.response
    answer, answer_is_reasoning = answer_of(response)
    # Before the banner, so it measures what the model wrote.
    repeated = duplicate_line_share(answer)
    # The banner is for a reader who reads the answer, not the flag. Only on a non-empty
    # answer, since an empty one is already `empty_response`. `getattr` because a one-shot
    # `Dispatch` has no turns, which is also why `_loop_ledger` is gated.
    if getattr(dispatched, "hit_turn_limit", False) and answer:
        answer = TURN_LIMIT_BANNER + answer
    return {
        **prefetched.accounting(),
        # Which file shaped this: an odd delegation is usually an agent file behaving as
        # written, and with three lookup tiers the name alone does not identify it.
        **({"agent": agent.name, "agent_source": agent.source_path} if agent else {}),
        "answer": answer,
        # As the backend reported it, not as asked for (ADR-0007).
        "model": response.model,
        "finish_reason": response.finish_reason,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        # 0 is a measured miss, `None` an endpoint that does not report caching.
        "cached_tokens": response.cached_tokens,
        "total_tokens": response.total_tokens,
        "stop_reason": response.stop_reason,
        "system_fingerprint": response.system_fingerprint,
        "effort": dispatched.effort,
        # Real calls, counted by the server; the token counts are the answering attempt's.
        "attempts": dispatched.attempts,
        # Only when a loop ran: `turns: 1` on a one-shot would invite comparison with a
        # budget that never applied (ADR-0007).
        **_loop_ledger(dispatched),
        # "" must never read as success. From `answer`, not `response.text`, so it means
        # "nothing came back"; a reply that reasoned its budget away is
        # `answer_is_reasoning`.
        "empty_response": answer == "",
        # For callers who branch rather than read the banner.
        "answer_is_reasoning": answer_is_reasoning,
        # The diagnosis, earned once the mitigations are spent (ADR-0014). False beside an
        # empty answer: effort was already lowest, so the budget ran out, not the
        # reasoning. Two fixes, hence two fields.
        "reasoning_exhausted": dispatched.reasoning_exhausted,
        # The one field that tells a loop from a long answer; both end at a length stop.
        "duplicate_line_share": repeated,
        # A stale environment is withheld (see `provisioned_for`), and to the caller that
        # must not read as "never provisioned".
        **({"provisioning_stale": True}
           if workdir is not None and provision.stale_for(cfg, workdir) is not None
           else {}),
        # Only when armed and declined; absent otherwise, since then there is no decision.
        **({"overflow_disarmed": overflow_off_because} if overflow_off_because else {}),
        **_diagnostics_block(dispatched, requested=diagnostics),
    }


# ---- the result, described once, in the schema that carries it ---------------------
#
# An inferred `{"type": "object"}` says nothing, and prose is what the client cuts, so this
# names the keys. Nothing `required`, additions allowed: it can never refuse a real result.
# Why: ADR-0066.


def _num(desc: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": desc}


_DELEGATION_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "answer": {"type": "string", "description": (
            "The model's reply. Empty is never a successful answer -- read "
            "`empty_response` beside it. When `answer_is_reasoning` is true this holds the "
            "model's reasoning under a banner instead, because the reply stopped at its "
            "token limit with nothing else written."
        )},
        "handle": {"type": "string", "description": (
            "Set on the write-capable delegations: the name `collect` reads this run back by."
        )},
        "status": {"type": "string", "description": (
            "`running` from a write-capable call, which answers at once; from `collect` "
            "and `answer`, `done` with the result, `running` while it is still going, "
            "`question` while it waits on an answer, or `cancelled`."
        )},
        "tool": {"type": "string", "description": "Beside `running`: which tool started it."},
        "running_seconds": {"type": "number", "description": (
            "From `collect` while it is still going: how long the run has taken so far."
        )},
        "questions": {"type": "array", "items": {"type": "string"}, "description": (
            "From `collect` while the run waits on an answer: the questions the model "
            "asked its caller, all in one call."
        )},
        "waiting_seconds": {"type": "number", "description": (
            "From `collect` while the run waits on an answer: how long it has waited."
        )},
        "empty_response": {"type": "boolean", "description": (
            "Nothing came back at all -- neither an answer nor reasoning. Reaching this "
            "means the server already retried at a larger budget and then at a lower "
            "effort, so it is a result and not a transient -- do not simply ask again."
        )},
        "answer_is_reasoning": {"type": "boolean", "description": (
            "`answer` holds reasoning, not an answer: the reply spent its whole token "
            "budget thinking and wrote no conclusion. Working notes, so do not quote it as "
            "a result -- but it is what the work produced, and a narrower follow-up task "
            "usually finishes where this one ran out."
        )},
        "reasoning_exhausted": {"type": "boolean", "description": (
            "Why the answer was empty, which is a different claim from that it was. True: "
            "the task needs more reasoning than this model finishes inside its budget, so "
            "split it or send it elsewhere. False: the budget was too small at an effort "
            "already at its lowest. Two different fixes."
        )},
        "duplicate_line_share": {"type": "number", "description": (
            "The fraction of the answer that repeats a line it had already written, 0 to "
            "1. Read it whenever the reply filled its budget: a loop and a long answer "
            "look identical everywhere else, both ending at a length stop with the "
            "ceiling exactly spent. Above roughly 0.5 the reply is repeating itself and a "
            "bigger budget will buy more of the same -- narrow the task instead. Under "
            "0.1 is ordinary prose."
        )},
        "model": {"type": "string", "description": (
            "The model the backend reported serving, not the one asked for."
        )},
        "effort": {"type": ["string", "null"], "description": (
            "The effort actually used, after the precedence chain resolved."
        )},
        "attempts": _num(
            "Real backend calls made. More than one means something failed and was retried "
            "for you; the per-reply token counts describe the attempt that answered."
        ),
        "turns": _num(
            "Round trips the loop took. Absent on the one-shot path, where a turn budget "
            "never applied."
        ),
        "partial": {"type": ["boolean", "null"], "description": (
            "The delegation was abandoned at a deadline *after* the model had decoded "
            "some of its reply, and `answer` holds that much of it. `error` beside it "
            "carries which deadline fired and what it was set to. This is a failure with "
            "the work salvaged, not a short answer: the reply stops mid-thought, so use "
            "it as material and re-send the remainder rather than quoting it as a result. "
            "Absent when the deadline fired before any token arrived, which is the "
            "ordinary timeout and still raises."
        )},
        "hit_turn_limit": {"type": ["boolean", "null"], "description": (
            "The delegation was still calling tools when its turns ran out, so the answer "
            "is whatever it could write once tools were withdrawn. Treat it as partial and "
            "raise `max_turns` rather than re-asking."
        )},
        "tool_calls": _num("Tools that actually ran."),
        "tool_calls_by_name": {"type": ["object", "null"], "additionalProperties": True,
                               "description": (
            "The same total split by tool, because one number cannot tell a delegation "
            "that read two files from one that overwrote two."
        )},
        "tool_errors": _num("Tool calls the server refused."),
        "tool_calls_deduplicated": _num("Repeat calls the loop collapsed."),
        "tool_results_evicted": _num(
            "Tool results dropped from history to stay inside the window."
        ),
        "failed_calls": _num(
            "Tool calls that failed in any way, each counted once: refused, a command that "
            "exited non-zero or timed out, or a masked failure. `tool_errors` and "
            "`bash_failures` overlap on a failing command, so their sum overcounts."
        ),
        "bash_calls": _num("Shell commands run."),
        "bash_failures": _num(
            "Commands that exited non-zero, counted from real process exits. This may "
            "contradict the model's own account of a command it ran; believe this."
        ),
        "bash_masked_failures": _num(
            "Calls where a command exited non-zero and the exit status does not say so, "
            "because a later command in the same line succeeded -- the `; echo $?` shape. "
            "Counted inside `bash_failures` too; reported apart because a caller reading "
            "`last_bash_exit: 0` needs to know the zero is hiding something. Detected by "
            "an `ERR` trap the server prepends and, for a pipeline, by each stage's own "
            "status; a stage exiting 1 from a no-result command such as `grep` or `diff`, "
            "or 141 because a later stage stopped reading, is not counted."
        ),
        "last_bash_exit": _num(
            "Exit status of the last command, captured by the server. Null means nothing "
            "exited -- no command ran, or the last was killed on timeout -- which 0 cannot "
            "carry. It is the status of the whole shell line, so a trailing `; echo $?` or "
            "a `| tail` reports the echo's success rather than the work's: a non-zero is "
            "trustworthy because nothing invents one, a zero is not proof. Read "
            "`bash_masked_failures` beside it, which catches the first of those two."
        ),
        "files_read": {"type": ["array", "null"], "items": {"type": "object",
                                                            "additionalProperties": True},
                       "description": "The files prefetched, with what each cost."},
        "files_skipped": {"type": ["array", "null"], "items": {"type": "object",
                                                               "additionalProperties": True},
                          "description": (
            "Files named but not sent, each with the layer that refused it and what to do "
            "about it. A refused path costs the call that file and not the call, so read "
            "this before trusting an answer: the model was told the file was unavailable, "
            "and cannot tell you what it never saw."
        )},
        "prefetch_tokens": _num("Tokens the prefetch actually spent."),
        "prefetch_budget": _num("Tokens it was allowed."),
        "input_tokens": _num("Prompt tokens for the turn that answered."),
        "output_tokens": _num("Reply tokens for the turn that answered."),
        "cached_tokens": _num(
            "Prompt tokens the cluster served from cache on that turn. 0 is a measured "
            "miss; null is an endpoint that does not report caching."
        ),
        "total_tokens": _num("The answering turn's total, as the endpoint reported it."),
        "total_input_tokens": _num("Prompt tokens across every turn there was."),
        "total_output_tokens": _num("Reply tokens across every turn there was."),
        "total_cached_tokens": _num("Cached prompt tokens across every turn."),
        "finish_reason": {"type": ["string", "null"], "description": (
            "Why generation stopped, as the endpoint put it."
        )},
        "stop_reason": {"type": ["string", "null"], "description": "The canonical form."},
        "system_fingerprint": {"type": ["string", "null"], "description": (
            "The endpoint's build identifier, when it offers one."
        )},
        "diagnostics": {"type": ["object", "null"], "additionalProperties": True,
                        "description": (
            "Present only when `diagnostics` was set: the per-turn ledger, including which "
            "files were read again after the server had dropped the first read."
        )},
        "overflow_disarmed": {"type": ["string", "null"], "description": (
            "Present only when the operator armed overflow handling and this server "
            "declined to use it, with the reason."
        )},
        "provisioning_stale": {"type": "boolean", "description": (
            "Present and true when the workdir's provisioned environment is stale, so "
            "$DELEGATE_PYTHON was withheld from run_bash. Re-run provision."
        )},
    },
}

# Every hint stated: the spec defaults `destructiveHint` and `openWorldHint` to true.
# Only `delegate_to_agent` is open-world, since the sandbox has no network unless an agent
# asks and is on `agent_network_allowed`.
_READS: dict[str, Any] = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
_WRITES: dict[str, Any] = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}

_AGENT_LIST_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "agents": {"type": "array", "items": {"type": "object", "additionalProperties": True},
                   "description": (
            "The usable agents: the `name` to pass, the `description` the file gives "
            "itself, the `model` and `effort` it binds, and the `source` it was read from."
        )},
        "count": {"type": "integer", "description": "How many are usable."},
        "skipped": {"type": "array", "items": {"type": "object",
                                               "additionalProperties": True},
                    "description": (
            "Files present but unusable, with why. A broken definition is skipped rather "
            "than fatal, so this is the list that needs fixing."
        )},
        "other_format": {"type": "array", "items": {"type": "object",
                                                    "additionalProperties": True},
                         "description": (
            "Claude Code's own agent files sharing the directory. Not broken, and not "
            "runnable here."
        )},
        "old_location": {"type": "array", "items": {"type": "object",
                                                    "additionalProperties": True},
                         "description": (
            "Usable agents read from `.claude/agents/`, where Claude Code also loads them as "
            "its own subagents. Each still runs, for one release; move the file to "
            "`move_to`."
        )},
    },
}

_CANCEL_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "handle": {"type": "string", "description": "The delegation this was asked to stop."},
        "cancelled": {"type": "boolean", "description": (
            "Whether this call stopped it. False when it had already finished or been stopped."
        )},
        "stopped": {"type": "boolean", "description": (
            "Whether the run has ended. A run inside a shell command can take a few seconds "
            "longer, and `collect` reports `cancelled` once it has."
        )},
    },
}

# How long `cancel_delegation` waits for the run to wind down: a bound, not a promise,
# since a shell command in its thread cannot be interrupted.
_CANCEL_SETTLE_SECONDS = 5.0

_BACKEND_STATUS_RESULT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "default": {"type": "string", "description": "The registry key used when none is named."},
        "models": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "status": {"type": "string", "description": "`ok`, or why the entry is unusable."},
                "id_confirmed": {"type": ["boolean", "null"], "description": (
                    "Whether the endpoint lists this entry's served model id; null when it "
                    "could not be asked.")},
                "detail": {"type": "string", "description": "What went wrong, or empty."},
                "cluster": {"type": ["object", "null"], "additionalProperties": True,
                            "description": "The serving stack's own numbers, or null."},
            },
        },
                   "description": (
            "One row per registry entry: `status`, `id_confirmed`, `detail`, and a "
            "`cluster` block of the serving stack's own numbers. `id_confirmed: false` "
            "beside an ok `status` means the endpoint is healthy but is not serving the "
            "model that entry names. A null `cluster` means the endpoint publishes no "
            "metrics, which is not ill health."
        )},
        "admission": {"type": "object", "additionalProperties": True, "description": (
            "This process's own gauges, and under `cross_process` whether the four rules "
            "are counted across every server process on the machine. `active: false` there "
            "means each client is bounded separately, so real load is higher than these "
            "numbers suggest."
        )},
    },
}


# The client's cut, not ours: Claude Code slices a description at 2048 characters with no
# way to fetch the rest. So a description is the index a tool is *found* by, never where a
# contract goes. Why: ADR-0066.
_DESCRIPTION_LIMIT = 2048

# What review holds a description to. The gap is deliberate: at 2000 characters one passes
# the length check while failing its purpose.
_DESCRIPTION_TARGET = 700

# ---- the arguments, described once, in the schema that carries them ----------------
#
# Once here, not in four docstrings: each property's `description` sits beside its
# argument, on its own budget, where contract prose belongs (a tool description is only
# the index it is found by). `effort`'s enum derives from `config.py`, so the two cannot
# disagree; it is the schema half of a check `_resolve_effort` also makes, and neither
# trusts the other, since a client may not enforce an enum and a schema cannot explain.

Task = Annotated[
    str,
    Field(description=(
        "One self-contained question or instruction. A task carrying several either stalls "
        "without completing a turn or returns an empty answer with `empty_response: true`, "
        "so send several as several calls -- they share the cached prefix anyway."
    )),
]

Effort = Annotated[
    str,
    Field(
        description=(
            "Reasoning effort, required because it changes both what the call costs and how "
            "good the answer is. 'low' for summarising, quoting and mechanical edits; "
            "'high' when the answer depends on reasoning across what it was given; 'max' "
            "only for something that came back thin at 'high'; 'off' to disable reasoning; "
            "'inherit' to defer to the agent file, then the registry row, then the "
            "configured default."
        ),
        json_schema_extra={"enum": [*EFFORT_LEVELS, EFFORT_INHERIT]},
    ),
]

class FileRange(BaseModel):
    """One files[] entry naming a line range of a single file.

    Uses `read_file`'s argument names and rules: 1-based, inclusive, and an `end_line`
    that is omitted, null or past the end of the file means the end of the file. No range
    is validated here -- a bad range is refused for that entry and lands in `files_skipped`
    rather than failing the call, so this model only carries the shape.
    """

    path: str
    start_line: int
    end_line: int | None = None


Files = Annotated[
    list[str | FileRange] | None,
    Field(description=(
        "Absolute paths the server reads itself and hands to the model, so their contents "
        "never enter your own context. Naming files here is the cheapest shape there is; "
        "omitting them is the dearest, because the delegation's turns then re-read what one "
        "prefetch would have supplied once. A head start, not a limit -- it can still go "
        "looking. Windows paths are translated. A refused path costs the call that file and "
        "not the call, and comes back in `files_skipped`. An entry may be a glob -- "
        "`/repo/src/**/*.py` -- which is shorthand for naming the matches, not a search: "
        "it expands before the path policy runs, the part before the first wildcard must "
        "sit inside a workspace root, and a pattern matching nothing or matching more than "
        "the cap is refused rather than quietly contributing less than you expected. An "
        "entry may instead be an object naming a line range of one file, which is judged by "
        "the range's own size rather than the file's -- so a file over the per-file cap can "
        "still be prefetched in part. Several entries for one file -- ranges, or the file "
        "whole -- are merged where they overlap or touch, so no line is sent twice; a "
        "merge over the per-file cap falls back to the ranges as named, and what still "
        "does not fit is skipped with the read_file range that reaches it."
    )),
]

Model = Annotated[
    str | None,
    Field(description=(
        "A key from the model registry. Omit for the configured default."
    )),
]

Title = Annotated[
    str | None,
    Field(max_length=120, description=(
        "A short name for this delegation, written into its transcript for a viewer "
        "to show. Omit it and a viewer names the delegation from the task's first line."
    )),
]

AllowedTools = Annotated[
    list[str] | None,
    Field(description=(
        "Narrows what the delegated model may use. Omit for everything the tool offers; "
        "pass an empty list for a single-turn answer with no tools at all, which is the "
        "cheapest shape when `files[]` already holds everything."
    )),
]

MaxTokens = Annotated[
    int | None,
    Field(description=(
        "Cap on one reply. Leave it unset unless you have a reason: the default is already "
        "raised at high and max effort so reasoning cannot consume the whole allowance and "
        "return nothing. If you do set it, it is honoured as given rather than raised to "
        "that floor -- so a small cap at high effort risks an answer that is empty because "
        "it thought until it ran out, which the server then retries at a larger budget. "
        "Raising it past what the clock can decode has no effect."
    )),
]

MaxTurns = Annotated[
    int | None,
    Field(description=(
        "Cap on round trips. Omitted, a toolset that can write gets a larger default than "
        "one that only reads. Raise it for work that genuinely iterates, and read "
        "`hit_turn_limit` as the sign you should have. Clamped to an operator ceiling, so "
        "asking for more than that is not an error."
    )),
]

Workdir = Annotated[
    str | None,
    Field(description=(
        "A directory to bind into the sandbox read-write, so `run_bash` can build or test "
        "in it. Writing never needed one -- `write_file` and `edit_file` run against the "
        "workspace roots -- so what this adds is the *running* half of a write-then-verify "
        "loop. Omit it to leave the shell nothing of yours at all."
    )),
]

Diagnostics = Annotated[
    bool,
    Field(description=(
        "Return a per-turn breakdown beside the answer: what each turn cost, what it "
        "evicted, and which files were read again after the server had dropped the first "
        "read. Ask for it when a call was dearer than the work justified."
    )),
]

HandleArg = Annotated[
    str,
    Field(description=(
        "The `handle` a delegating call returned. It names a run in this server process, "
        "so a reconnect forgets it."
    )),
]

WaitSeconds = Annotated[
    float | None,
    Field(description=(
        "How long to wait for the run to finish before returning where it has got to. "
        "Omit for the server's default, which answers within a client's usual patience; "
        "a client that backgrounds a slow read-only call and notifies on completion, as "
        "Claude Code does, can pass the whole remaining run instead. A long wait reports "
        "progress while it waits, so the client's idle timeout does not drop it."
    )),
]

AgentName = Annotated[
    str,
    Field(description=(
        "An agent from `list_agents`. The file carries the instructions, the model, the "
        "effort and the tools that kind of work needs."
    )),
]

Project = Annotated[
    str | None,
    Field(description=(
        "Where to look for the agent file. Binds nothing, and defaults to `workdir`. Pass "
        "it separately only to run one project's agent against another."
    )),
]


ProjectLookup = Annotated[
    str | None,
    Field(description=(
        "Which project's agents to list. Pass the same value you intend to delegate with: "
        "a project's own agents are found before your personal ones, so a different "
        "project lists agents a delegation would not actually reach."
    )),
]

AnswerText = Annotated[
    str | None,
    Field(description="The reply, read by the model as the result of its `ask_caller` call."),
]

BestReading = Annotated[
    bool,
    Field(description=(
        "Instead of `text`: tell the model the choice is its own, to proceed on its best "
        "reading of the task."
    )),
]

MayAsk = Annotated[
    bool,
    Field(description=(
        "Offer the model `ask_caller`: one call per task, holding every question it has, "
        "when it cannot tell which reading of the task is meant. `collect` then reports "
        "`status: \"question\"`, and `answer` replies. False for a caller that will not "
        "answer."
    )),
]


def _server_instructions(cfg: Config) -> str:
    """The few rules that decide a call before the caller has chosen a tool.

    Short, since the client cuts this at 2048 too, and everything with a better home has
    gone to it: only what no schema shows (delegation is cheap, the concurrency ceiling is
    real, a resource helps size a pass). The ceiling comes from `Config`, where defaults
    live. The returned text is delivered; this docstring is not.
    """
    return """
Delegation to a local model, on hardware the user hosts: it costs no cloud tokens, so
prefer it for bulk, mechanical or read-heavy work and keep your own context for judgement.

Name files in `files[]` rather than pasting them -- the server reads them, so their
contents never enter your context. Send one question per call.

Calls do not contend on prefetch size: the serving engine serialises cold prefills itself,
so issuing several at once costs no more than issuing them one at a time.

Read the `delegate://orchestration` resource before a wide pass -- sizing, fan-out and the
failure modes worth knowing, kept there rather than here so it costs nothing until wanted.
""".strip()


class SessionScope(NamedTuple):
    """The configuration one call runs under, narrowed to its session's folders.

    `workdir_ok` is False when `workdir_roots` is configured and none of the session's
    folders is inside it: an empty `workdir_roots` means "reuse workspace_roots", so the
    empty narrowing cannot be written into the config without silently widening it.

    `workspace` is the *name* of the project folder the session runs in -- the last
    component of the first folder the client lists inside the configured ceiling -- so the
    stream's `start` event can say which project a delegation was in. None when the client
    lists no roots, which is when the configured roots apply unchanged.
    """

    cfg: Config
    workdir_ok: bool
    workspace: str | None


def _workspace_name(ceiling: Sequence[str], roots: Sequence[str]) -> str | None:
    """The name of the first client-listed root inside the ceiling, or None.

    The client's own order, never `narrow_roots(ceiling, roots)`'s: that re-sorts into the
    ceiling's order, so its head is the first *configured* root, which is not the same as
    the first folder the client listed. The name is the last component of the narrowed root
    -- the folder that actually gets read from -- not the path.
    """
    for r in roots:
        narrowed = narrow_roots(ceiling, (r,))
        if narrowed:
            return os.path.basename(narrowed[0].rstrip("/")) or None
    return None


async def _client_roots(ctx: Context | None) -> list[str] | None:
    """The folders the client lists for this session, resolved; None if it lists none.

    A client that declares the capability and then fails to answer is refused rather than
    treated as one without it: it has said it knows where it works, so falling back to the
    configured roots would widen exactly the session that asked to be narrowed.
    """
    if ctx is None:
        return None
    try:
        params = ctx.session.client_params
    except RuntimeError:  # no live request, as when a handler is called directly
        return None
    if params is None or params.capabilities.roots is None:
        return None
    try:
        listed = await ctx.list_roots()
    except Exception as e:
        raise ToolError(
            f"This client declares MCP roots but listing them failed ({e}), so the session's "
            "folders are unknown and nothing is reached. Retry the call."
        ) from e
    found = (file_uri_to_posix(str(r.uri)) for r in listed)
    return [os.path.realpath(p) for p in found if p is not None]


def _repo_ext_allowlists(cfg: Config) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Layer-2 additions over the scope's final roots (ADR-0116).

    A malformed `.claude/delegate-local.toml` fails the call the way a bad root does,
    naming the file, rather than silently serving a narrower reading than the repository
    asked for.
    """
    try:
        return load_repo_ext_allowlists(resolved_roots(cfg))
    except RepoSettingsError as e:
        raise ToolError(str(e)) from e


async def session_scope(cfg: Config, ctx: Context | None) -> SessionScope:
    """`cfg` with its roots narrowed to the folders this call's client lists (ADR-0110).

    Before anything reads a path, and in every handler that reads one: a handler left on
    the build-time `cfg` is a session reaching every configured root, which is the bug.
    """
    roots = await _client_roots(ctx)
    if roots is None:
        if cfg.client_roots == "require":
            raise ToolError(
                "This client lists no MCP roots, so it has not said which project this "
                "session works in, and DELEGATE_CLIENT_ROOTS=require refuses that. Use a "
                "client that lists roots, or set DELEGATE_CLIENT_ROOTS=narrow to apply "
                "DELEGATE_WORKSPACE_ROOTS as configured."
            )
        return SessionScope(
            replace(cfg, repo_ext_allowlists=_repo_ext_allowlists(cfg)), workdir_ok=True,
            workspace=None,
        )
    ceiling = resolved_roots(cfg)
    workspace = narrow_roots(ceiling, roots)
    if not workspace:
        raise ToolError(
            f"None of the folders this session lists ({', '.join(roots) or 'none'}) is "
            f"inside DELEGATE_WORKSPACE_ROOTS ({', '.join(ceiling)}), so it may reach "
            "nothing. Open the session in a project under one of those roots, or add the "
            "project to DELEGATE_WORKSPACE_ROOTS."
        )
    workspace_name = _workspace_name(ceiling, roots)
    if not cfg.workdir_roots:
        scope_cfg = replace(cfg, workspace_roots=workspace)
        return SessionScope(
            replace(scope_cfg, repo_ext_allowlists=_repo_ext_allowlists(scope_cfg)),
            workdir_ok=True,
            workspace=workspace_name,
        )
    workdir = narrow_roots(resolved_workdir_roots(cfg), roots)
    narrowed = replace(cfg, workspace_roots=workspace, workdir_roots=workdir or cfg.workdir_roots)
    return SessionScope(
        replace(narrowed, repo_ext_allowlists=_repo_ext_allowlists(narrowed)),
        workdir_ok=bool(workdir),
        workspace=workspace_name,
    )


def _ask_caller(pause: Pause, cfg: Config) -> Callable[[list[str]], Awaitable[str]]:
    """The `ask` callback a run passes to `run_agentic_loop` when the caller may answer.

    Records the questions on the pause and awaits the answer; a caller that never answers
    ends the run unanswered, which `run_delegation` turns into `ok: false`.
    """
    async def ask(questions: list[str]) -> str:
        try:
            return await asyncio.wait_for(
                pause.ask(questions), timeout=cfg.question_wait_limit)
        except TimeoutError:
            raise QuestionNotAnswered(cfg.question_wait_limit) from None
    return ask


def build(  # noqa: PLR0915 -- every tool is a closure over this one set of wiring
    cfg: Config, registry: Registry, cache: BackendCache | None = None
) -> FastMCP:
    """Construct the server. Pure wiring; no I/O beyond what the tools do when called.

    `cache` is injectable, or a test of the tool surface opens a real socket and waits out a
    real timeout: slow, flaky, and not what the test is about.
    """
    cache = cache or BackendCache(cfg)
    # One per server: the verdict changes only when the operator edits a file.
    windows = WindowCheck(cfg)
    # One per process, shared by every tool closure. `slots` makes the budget global across
    # stdio's per-client processes; when the platform cannot lock, the reason is reported,
    # since a gate that quietly narrowed looks exactly like one working. Why: ADR-0040.
    slots, slots_reason = build_slots(cfg)
    admission = Admission(cfg, slots)
    # Loads its history here and writes each accepted sample back, so a reconnect is not a
    # cold start; hence `rate_history_path`, never the runtime directory. Stamped with the
    # served model, so a swap discards it rather than pricing the new model at the old one's
    # speed. Why: ADR-0075, ADR-0094.
    rates = RateHistory(
        path=rate_history_path(cfg),
        stamp=registry.resolve(None).served_model_id,
    )

    # Fills that memory, one sample per scrape so buckets are comparable (`RateSampler`),
    # from the default entry: a memory stamped with one model must not be fed another's.
    sampler = RateSampler(
        rates,
        probe=lambda: cache.get(registry.resolve(None)).probe_cluster(),
        busy=lambda: admission.inflight_seqs > 0,
        every=cfg.rate_sample_seconds,
    )

    handles = Handles(cfg.handle_ttl_seconds)

    async def _answered(run: Awaitable[dict[str, Any]], tool: str,
                        *, pause: Pause | None = None) -> dict[str, Any]:
        """Start a write-capable delegation and answer with its handle at once (ADR-0103).

        The client releases the next call only when this returns, so answering at once lets
        several start together. The run carries on in a task this process owns.
        """
        handle = handles.start(run, tool=tool, pause=pause)
        return {"handle": handle, "status": "running", "tool": tool}

    async def _inline_or_question(
        run: Awaitable[dict[str, Any]], tool: str, pause: Pause, sink: _CallProgress,
    ) -> dict[str, Any]:
        """Run a read-only delegation inline, except a run that asks answers at once.

        A run that asks must not leave the caller blocked on a run waiting for it: it
        returns the question with its handle, and `answer` resumes the run later.
        """
        handle = handles.start(run, tool=tool, pause=pause)
        try:
            result = await _collect_wait(handle, math.inf, sink)
        finally:
            sink.close()
        if result["status"] == "question":
            return result
        if result["status"] == "cancelled":
            raise ToolError(f"delegation {handle!r} was cancelled")
        # "done": keep the inline shape, with no `status` or `handle` (ADR-0118).
        return {key: value for key, value in result.items()
                if key not in ("status", "handle")}

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            async with sampling(sampler):
                yield {}
        finally:
            await cache.aclose()

    mcp: FastMCP = FastMCP(
        name=SERVER_NAME,
        lifespan=lifespan,
        instructions=_server_instructions(cfg),
    )

    @mcp.tool(title="Delegate to the local model", annotations=_WRITES,
              output_schema=_DELEGATION_RESULT)
    async def delegate(  # noqa: PLR0913 -- one tool's arguments, one dispatch
        task: Task,
        effort: Effort,
        files: Files = None,
        model: Model = None,
        *,
        # Keyword-only from here, and not merely to satisfy a lint: MCP passes every
        # argument by name, so positional order is a promise to nobody.
        allowed_tools: AllowedTools = None,
        max_tokens: MaxTokens = None,
        max_turns: MaxTurns = None,
        workdir: Workdir = None,
        diagnostics: Diagnostics = False,
        ctx: Context | None = None,
        title: Title = None,
        may_ask: MayAsk = True,
    ) -> dict[str, Any]:
        """Delegate a task to a local model that can read, write and run commands.

        The general form, and the only one that can change anything: it gets the file
        tools and a sandboxed shell, so it can write code, run the project's tests, read
        the real failure and try again -- on hardware the user hosts, at no cloud cost.

        Reach for `delegate_readonly` when nothing needs writing; it is declared read-only,
        so a client that gates writes runs it without stopping to ask. Reach for
        `delegate_to_agent` when the work has a *kind* an agent file already shapes.

        Answers at once with a `handle`: `collect` it for the result.
        """
        # The roots are asked for while the call is live: the run outlives it, which is why
        # it is handed `ctx=None`.
        scope = await session_scope(cfg, ctx)
        pause = Pause() if may_ask else None
        ask = _ask_caller(pause, scope.cfg) if pause is not None else None
        run = run_delegation(
            scope.cfg, registry, cache, windows, admission, rates=rates,
            task=task, title=title, files=files, model=model, effort=effort,
            allowed_tools=allowed_tools, max_tokens=max_tokens, max_turns=max_turns,
            workdir=_rooted(scope, workdir),
            diagnostics=diagnostics, ctx=None, tool_name="delegate",
            ask=ask, workspace=scope.workspace,
        )
        return await _answered(run, "delegate", pause=pause)

    @mcp.tool(title="Delegate a read-only task", annotations=_READS,
              output_schema=_DELEGATION_RESULT)
    async def delegate_readonly(  # noqa: PLR0913 -- one tool's arguments, one dispatch
        task: Task,
        effort: Effort,
        files: Files = None,
        model: Model = None,
        *,
        max_tokens: MaxTokens = None,
        max_turns: MaxTurns = None,
        diagnostics: Diagnostics = False,
        ctx: Context | None = None,
        title: Title = None,
        may_ask: MayAsk = True,
    ) -> dict[str, Any]:
        """Delegate a read-only task: search, read files, read git history, change nothing.

        `delegate` with the toolset fixed, and what differs is what a caller can promise
        *before* it runs -- this is declared read-only, so a client that gates writes runs
        it where `delegate` has to stop and ask.

        The read-heavy majority belongs here: explaining, summarising, first-pass review,
        tracing a call path, "where is X handled", "does anything still call Y". It has
        turns and can go looking, so name the obvious material and let it find the rest.
        Use `delegate_to_agent_readonly` when an agent file should shape the work.
        """
        scope = await session_scope(cfg, ctx)
        if not may_ask:
            return await run_delegation(
                scope.cfg, registry, cache, windows, admission, rates=rates,
                task=task, title=title, files=files, model=model, effort=effort,
                # Fixed and intersected (`resolve_allowed`), so no caller can widen it: that
                # is what makes readOnlyHint a property of the tool, not a claim a caller
                # could falsify. Derived from which tools declare `writes`, since a list
                # here would silently go stale when a writing tool is added. Why: ADR-0048.
                allowed_tools=sorted(READ_ONLY_TOOL_NAMES), max_tokens=max_tokens,
                max_turns=max_turns,
                diagnostics=diagnostics, ctx=ctx, tool_name="delegate_readonly",
                workspace=scope.workspace,
            )
        pause, sink = Pause(), _CallProgress(ctx)
        run = run_delegation(
            scope.cfg, registry, cache, windows, admission, rates=rates,
            task=task, title=title, files=files, model=model, effort=effort,
            # Fixed, as above. A run that asks outlives this call, so it reports through
            # `sink`, which goes quiet once the call has returned its question.
            allowed_tools=sorted(READ_ONLY_TOOL_NAMES), max_tokens=max_tokens,
            max_turns=max_turns,
            diagnostics=diagnostics, ctx=sink, tool_name="delegate_readonly",
            ask=_ask_caller(pause, scope.cfg), workspace=scope.workspace,
        )
        return await _inline_or_question(run, "delegate_readonly", pause, sink)

    def _load(scope: SessionScope, agent_name: str, workdir: str | None) -> AgentSpec:
        try:
            return load_agent(scope.cfg, agent_name, workdir)
        except AgentError as e:
            raise ToolError(str(e)) from e

    def _rooted(scope: SessionScope, given: str | None) -> str | None:
        """Resolved and root-checked against the session's `workdir_roots` once, here.

        For both `workdir` (bound writable) and `project` (where agent files are looked up):
        different meanings, but neither may leave the roots, and a check in two places would
        drift in one.
        """
        if given is None:
            return None
        if not scope.workdir_ok:
            raise ToolError(
                f"{given} is outside every workdir root this session has: none of the "
                "folders the client lists is inside DELEGATE_WORKDIR_ROOTS."
            )
        try:
            return resolve_workdir(scope.cfg, given)
        except PathRefused as e:
            raise ToolError(str(e)) from e
        except PathPolicyError as e:
            raise ToolError(f"{STATUS_MISCONFIGURED}: {e}") from e

    @mcp.tool(title="Delegate to a named agent",
              annotations={**_WRITES, "openWorldHint": True},
              output_schema=_DELEGATION_RESULT)
    async def delegate_to_agent(  # noqa: PLR0913 -- ctx is injected, not a caller argument
        agent_name: AgentName,
        task: Task,
        files: Files = None,
        workdir: Workdir = None,
        *,
        project: Project = None,
        model: Model = None,
        effort: Effort,
        allowed_tools: AllowedTools = None,
        max_tokens: MaxTokens = None,
        max_turns: MaxTurns = None,
        diagnostics: Diagnostics = False,
        ctx: Context | None = None,
        title: Title = None,
        may_ask: MayAsk = True,
    ) -> dict[str, Any]:
        """Delegate to a named agent: a file that shapes how one *kind* of task is done.

        Use this over `delegate` when the work has a kind -- writing tests, reviewing a
        diff, migrating an API. The agent file carries the instructions, the model, the
        effort and the tools that kind of work needs, so you send the task and not the
        preamble. `list_agents` shows what is available.

        Can write and run commands, like `delegate`. Use `delegate_to_agent_readonly` when
        the agent's work is reading.

        Answers at once with a `handle`: `collect` it for the result.
        """
        # Both checked *before* either is used: the agent lookup reads under `project`, and
        # a root check after that read would not be a check.
        scope = await session_scope(cfg, ctx)
        resolved_workdir = _rooted(scope, workdir)
        resolved_project = (
            _rooted(scope, project) if project is not None else resolved_workdir)
        agent = _load(scope, agent_name, resolved_project)
        pause = Pause() if may_ask else None
        ask = _ask_caller(pause, scope.cfg) if pause is not None else None
        run = run_delegation(
            scope.cfg, registry, cache, windows, admission, rates=rates,
            task=task, title=title, files=files, model=model, effort=effort,
            allowed_tools=allowed_tools, max_tokens=max_tokens, max_turns=max_turns,
            agent=agent, workdir=resolved_workdir,
            diagnostics=diagnostics, ctx=None, tool_name="delegate_to_agent",
            ask=ask, workspace=scope.workspace,
        )
        return await _answered(run, "delegate_to_agent", pause=pause)

    @mcp.tool(title="Delegate a read-only task to a named agent", annotations=_READS,
              output_schema=_DELEGATION_RESULT)
    async def delegate_to_agent_readonly(  # noqa: PLR0913 -- one tool's arguments, one dispatch
        agent_name: AgentName,
        task: Task,
        files: Files = None,
        *,
        project: Project = None,
        model: Model = None,
        effort: Effort,
        max_tokens: MaxTokens = None,
        max_turns: MaxTurns = None,
        diagnostics: Diagnostics = False,
        ctx: Context | None = None,
        title: Title = None,
        may_ask: MayAsk = True,
    ) -> dict[str, Any]:
        """Delegate to a named agent, with the toolset fixed to reading.

        `delegate_to_agent` with the read-only tools, exactly as `delegate_readonly` is to
        `delegate`: declared read-only, so a client that gates writes runs it without
        asking. The default for an audit, a review, or any agent whose work is reading.

        **The agent's own `allowed_tools` is replaced, not narrowed.** It gets `read_file`,
        `search_files` and `read_git` whatever its file says -- so an agent declaring
        `run_bash` does not get it, and one declaring less gains the rest.
        """
        scope = await session_scope(cfg, ctx)
        resolved_project = _rooted(scope, project)
        agent = _load(scope, agent_name, resolved_project)
        if not may_ask:
            return await run_delegation(
                scope.cfg, registry, cache, windows, admission, rates=rates,
                task=task, title=title, files=files, model=model, effort=effort,
                # Fixed, derived from `writes`, as in `delegate_readonly` (ADR-0042).
                allowed_tools=sorted(READ_ONLY_TOOL_NAMES),
                max_tokens=max_tokens, max_turns=max_turns,
                # No `workdir`: `resolved_project` is a lookup path, and passing it would
                # bind it read-write and make the readOnlyHint a lie.
                agent=agent,
                diagnostics=diagnostics, ctx=ctx, tool_name="delegate_to_agent_readonly",
                workspace=scope.workspace,
            )
        pause, sink = Pause(), _CallProgress(ctx)
        run = run_delegation(
            scope.cfg, registry, cache, windows, admission, rates=rates,
            task=task, title=title, files=files, model=model, effort=effort,
            # Fixed, as above; reports through `sink`, as in `delegate_readonly`.
            allowed_tools=sorted(READ_ONLY_TOOL_NAMES),
            max_tokens=max_tokens, max_turns=max_turns,
            # No `workdir`: `resolved_project` is a lookup path, and passing it would bind
            # it read-write and make the readOnlyHint a lie.
            agent=agent,
            diagnostics=diagnostics, ctx=sink, tool_name="delegate_to_agent_readonly",
            ask=_ask_caller(pause, scope.cfg), workspace=scope.workspace,
        )
        return await _inline_or_question(run, "delegate_to_agent_readonly", pause, sink)

    async def _collect_wait(
        handle: str, wait_seconds: float | None, ctx: ProgressSink | None,
    ) -> dict[str, Any]:
        """Wait on a delegation's handle and return its result: `collect`'s whole body."""
        wait = cfg.collect_wait_seconds if wait_seconds is None else wait_seconds
        notified = 0

        async def keepalive(running_seconds: float) -> None:
            # The same rule as the delegating tools' heartbeat: one rising counter, no
            # `total`, the words in `message`.
            nonlocal notified
            if ctx is not None:
                notified += 1
                await ctx.report_progress(
                    progress=notified, message=f"still running after {running_seconds:.0f}s")

        try:
            return await handles.collect(
                handle, wait, keepalive=keepalive, every=cfg.keepalive_interval)
        except UnknownHandle as e:
            raise ToolError(str(e)) from e

    @mcp.tool(title="Collect a delegation", annotations={**_READS, "idempotentHint": True},
              output_schema=_DELEGATION_RESULT)
    async def collect(
        handle: HandleArg, wait_seconds: WaitSeconds = None, ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Collect a delegation by the `handle` a delegating call returned.

        Answers with the run's result once it has finished, carrying `status: done`, with
        `status: question` and the question it is waiting on when a run that asked is
        paused, or with `status: running` and how long it has run if it is still going
        when the wait is over. `answer` replies to a question. Waiting never cancels the
        run, and collecting twice returns the same result until the server forgets it. A
        run that was stopped says `cancelled`.
        """
        return await _collect_wait(handle, wait_seconds, ctx)

    @mcp.tool(title="Answer a delegation's question",
              annotations={"readOnlyHint": False, "destructiveHint": False,
                           "idempotentHint": False, "openWorldHint": False},
              output_schema=_DELEGATION_RESULT)
    async def answer(
        handle: HandleArg,
        text: AnswerText = None,
        best_reading: BestReading = False,
        wait_seconds: WaitSeconds = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Reply to a delegation that asked a question (`status: "question"`). Give
        `text`, or set `best_reading` to leave the choice to the model; the run then
        continues and this waits for it like `collect`. Answer what the task's own meaning
        settles yourself, and pass on to the person only what only they can decide: the
        run waits, its clocks paused, until you reply.
        """
        if (text is not None) == best_reading:
            raise ToolError(
                "answer takes exactly one of `text` and `best_reading: true` -- not "
                "neither, and not both.")
        reply = (
            BEST_READING_REPLY
            if best_reading else str(text)
        )
        try:
            handles.answer(handle, reply)
        except UnknownHandle as e:
            raise ToolError(str(e)) from e
        except NotWaitingError as e:
            raise ToolError(str(e)) from e
        return await _collect_wait(handle, wait_seconds, ctx)

    # Not read-only -- it stops work -- but it destroys nothing and reaches nothing outside
    # this process, and stopping a run twice is the same as stopping it once.
    @mcp.tool(title="Stop a delegation",
              annotations={"readOnlyHint": False, "destructiveHint": False,
                           "idempotentHint": True, "openWorldHint": False},
              output_schema=_CANCEL_RESULT)
    async def cancel_delegation(handle: HandleArg) -> dict[str, Any]:
        """Stop a running delegation by the `handle` a delegating call returned.

        The call that started it has already answered, so cancelling that call no longer
        reaches the work; this does, closing the stream to the model as a cancelled call
        used to. `cancelled` says whether this stopped anything, `stopped` whether the run
        has ended. Cancelling a `collect` only stops that wait, never the run.
        """
        try:
            cancelled = handles.cancel(handle)
            task = handles.task(handle)
        except UnknownHandle as e:
            raise ToolError(str(e)) from e
        if not task.done():
            await asyncio.wait({task}, timeout=_CANCEL_SETTLE_SECONDS)
        return {"handle": handle, "cancelled": cancelled, "stopped": task.done()}

    @mcp.tool(title="List agents", annotations={**_READS, "idempotentHint": True},
              output_schema=_AGENT_LIST_RESULT)
    async def list_agents(
        project: ProjectLookup = None, ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List the agents `delegate_to_agent` can reach, and where each was found.

        Call this before guessing an agent name.

        Three lists, because "not there", "there and broken" and "there but not mine" need
        different answers: `agents` are usable, `skipped` are present but did not parse,
        and `other_format` are Claude Code's own agent files sharing the directory -- not
        faulty, and not runnable here.
        """
        scope = await session_scope(cfg, ctx)
        listing = discover_agents(scope.cfg, _rooted(scope, project))
        found, skipped = listing.agents, listing.skipped
        return {
            "agents": [
                {
                    "name": a.name,
                    "description": a.description,
                    "model": a.model or "(the server default)",
                    "effort": a.effort or "(the model default)",
                    "source": a.source_path,
                }
                for a in found
            ],
            "count": len(found),
            "skipped": [
                {"name": s.name, "source": s.source_path, "reason": s.reason}
                for s in skipped
            ],
            "skipped_count": len(skipped),
            "other_format": [
                {"name": f.name, "source": f.source_path,
                 "keys": list(f.foreign_keys)}
                for f in listing.other_format
            ],
            "other_format_count": len(listing.other_format),
            "old_location": [
                {"name": name, "source": source, "move_to": move_to}
                for name, source, move_to in listing.old_location
            ],
        }

    @mcp.tool(title="Check the local models", annotations={**_READS, "idempotentHint": True},
              output_schema=_BACKEND_STATUS_RESULT)
    async def backend_status() -> dict[str, Any]:
        """Report whether each configured local model is reachable and serving what it should.

        Call this first whenever a delegation fails, before retrying or giving up: it
        separates a model that is down from one that is misconfigured, and from a key that
        is wrong. `status: "ok"` with `id_confirmed: false` is the case worth reading
        closely -- the endpoint is healthy but is not serving the model this entry names,
        so delegating to it will not do what the registry claims.

        Also reports this process's admission gauges and, per model, the serving stack's
        own numbers: queue depth, KV-cache fill, prefix-cache hit rate, preemptions.
        """
        rows = await asyncio.gather(
            *(
                probe_entry(cache, cfg, entry, is_default=key == registry.default_key)
                for key, entry in registry.entries.items()
            )
        )
        return {
            "default": registry.default_key,
            "models": list(rows),
            "admission": {
                **admission.status(),
                "cross_process": await cross_process_status(slots, slots_reason),
            },
            # A dropped window leaves no trace, so without these counts a sampler refusing
            # everything looks like a quiet cluster.
            "rate_sampling": sampler.status(),
        }

    # ---- the long form, pulled rather than pushed ---------------------------------
    #
    # A resource, which the model can list and read: free until asked, then unlimited. A
    # prompt is user-pulled and never reaches the model unasked. Only what sizing a pass
    # needs; what a schema or refusal can state goes there. Why: ADR-0066.
    @mcp.resource(
        "delegate://orchestration",
        name="Orchestrating delegations",
        description=(
            "How to size a delegation, what a call costs, how many run at once, and the "
            "failure modes worth recognising. Read it before a wide pass."
        ),
        mime_type="text/markdown",
    )
    def orchestration_guide() -> str:
        """The cost model and the failure modes, measured on this deployment."""
        return """
# Orchestrating delegations

## What a call costs

The expensive shape is a call with no `files[]`. Its turns re-read what one prefetch would
have supplied once, and admission sizes a request on its *opening* estimate and never
revisits it -- so an unprefetched call is filed as small for life while each later turn
re-prefills. Measured here: no prefetch gave 10 turns, a turn-limit stop, 12 evicted tool
results and 394k total input tokens, for a question that two turns answered with the right
file attached.

Prefetching is a head start, not a limit. The read-only tools have turns and can go
looking, so name the obvious material and let the delegation find the rest.

## Write-capable calls answer with a handle

`delegate` and `delegate_to_agent` answer at once with `status: running` and a `handle`, and
the work carries on, so several start together. Send the calls in one message, then collect
them in one message: `collect` waits up to `wait_seconds`, and a client that backgrounds a
slow read-only call and says when it finishes -- Claude Code does -- can pass the whole
remaining run and be told when it is done. `running` means wait again, never start again. A
refusal from inside the run arrives from `collect`, not from the call. `cancel_delegation`
stops a run; cancelling a `collect` only stops waiting. Until a writing run is collected,
leave its `workdir` alone: it may still be writing there. A reconnect forgets every handle.

## A run may ask you a question

A delegation that cannot tell which reading of its task is meant may ask, once, with every
question in one call. Whatever is waiting on the run -- `collect`, `answer`, or a read-only
call -- then returns at once with `status: question`, the `questions` and a `handle`. Reply
with `answer`, giving `text`, or set `best_reading` to leave the choice to the model; it then
waits like `collect`. Answer what the task's own meaning settles yourself, and pass on to the
person only what only they can decide. Never drop a question: the run keeps its admission
slot while it waits, every other delegation queues behind it, and only
`question_wait_limit` ends it. A caller that will not answer passes `may_ask` as false, or
cancels the run with `cancel_delegation`.

## How many run at once

Admission bounds concurrent sequences and the summed token estimate, not the size of any
one prefetch. A cap on concurrent large prefills was removed on 2026-09-13: measured, it
added 286.3s of aggregate waiting for a batch 12.1s slower end to end, because the engine
already serialises cold prefills itself (ADR-0077). Admission queues first-come-first-served
and, by default, waits for as long as the work ahead takes: a wide fan-out is slower than a
narrow one rather than partly refused. The rule that used to refuse one on prompt size is
gone, and so is the bail-out that used to refuse its tail.

Calls can overlap freely. Several tasks over the
same files are several calls, and they share the cached prefix however they are sent,
because the task is rendered last under a byte-constant prefix.

## One question per call

A task carrying several fails in two ways. It stalls without completing a turn, which reads
like an outage; or it completes one and returns an empty answer and no error, which
reads like success. "List every X and what each does" is enumerable and counts as many.

So check `empty_response` before trusting a short reply, and read `reasoning_exhausted`
beside it: true means the task needs more reasoning than the model can finish in its
budget, so split it; false means the budget was simply too small.

## Recognising a failure

`bash_failures` and `last_bash_exit` are real process exits the server captured. They may
contradict the model's own account of a command it ran, and the server's numbers are the
ones to believe. `last_bash_exit` is the status of the whole shell line, so a trailing
`; echo $?` or a `| tail` replaces the status of the work with the status of the echo -- a
non-zero is trustworthy because nothing invents one, a zero is not proof of success.
Ask for the outcome, never the exit code: a task that asks for the number is what gets a
model to append the `; echo $?` that replaces it.
`bash_masked_failures` closes both: an `ERR` trap counts a command that failed before the
last one, and each pipeline stage's own status counts `pytest | tail`, even when the status
reads 0. A `grep` or `diff` exiting 1, or a stage stopped by SIGPIPE, is not counted.

`hit_turn_limit` means the delegation was still calling tools when its turns ran out; the
answer is whatever it could write once tools were withdrawn, so treat it as partial and
raise `max_turns` rather than re-asking the same question.

Do not retry a failed call yourself. The server already retries an unavailable or refusing
backend with backoff, and already re-asks an empty answer at a larger budget and then a
lower effort. When a call fails outright, `backend_status()` says whether the model is
down, misconfigured, or serving something other than what the registry names.

## Choosing a tool

`delegate_readonly` and `delegate_to_agent_readonly` are declared read-only, so a client
that gates writes runs them without asking. Prefer them whenever the work is reading --
that is most work. Reach for the writing forms when the delegation must produce a file or
run a command, and pass `workdir` only then.

## Shaping a pass

A pass that verifies -- a review, an audit -- should read and report, not check itself: give
it the read-only tools, tell it not to verify its own quotations, and check them yourself
afterwards. With its files prefetched, one audit pass took one turn in seven runs of eight,
and two in the eighth, whether or not it had a shell or was told not to verify: the
read-only tools buy a pass that cannot write, not a shorter one.

Bound a pass with `max_turns`, not with prose. "Verify once, then report" in a task does
not hold; the turn cap does, because the last turn is sent with tools withdrawn and the
model has to write. Set it to what the work needs plus one -- turns are cheap -- and read
`hit_turn_limit` as a partial answer. It cannot stop a loop inside one turn, because that
turn never ends. A writing delegation needs no number as often: with none given it gets a
larger default than a reading pass, because writing work iterates.

## History is `read_git`'s, and only `read_git`'s

`run_bash` cannot see `.git` at all: the scan covers it, so a git command inside the sandbox
fails without saying why. Every question about history therefore goes to `read_git` -- when a
line changed and why, what a commit touched, which entry removed a thing.

It reads the **working tree** as well as history, so it is also how a delegation sees an
uncommitted diff: ask it to read the diff against HEAD rather than copying a patch file
somewhere for it, which cannot work anyway if that somewhere is outside the workspace roots.

Its long output is truncated on a line boundary and says so. Read that line before concluding
something is absent.
""".strip()

    return mcp
