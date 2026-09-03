"""MCP wiring, and nothing else.

The tools declared here are the model-facing contract: Claude Code reads these
descriptions and decides from them what to delegate. Rewording one changes runtime
behaviour, so they are treated as behaviour and carry a CHANGELOG entry.

What lives here is wiring -- registering tools, validating arguments, owning the
backend cache, and translating an exception into something a model can act on. What
does not live here is the delegation itself: that is `loop.py`.

One rule governs the whole module. On stdio, **stdout is the wire protocol.** Anything
printed there corrupts every subsequent MCP message, with no error and no symptom
beyond the client reporting a dead server. Diagnostics go to stderr. `main.py` holds
the other half of that rule.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from .backends.base import (
    Backend,
    BackendProtocolError,
    BackendRefused,
    BackendUnavailable,
    CanonicalShapeError,
)
from .backends.openai_compat import OpenAICompatBackend
from .admission import Admission, AdmissionError, AdmissionLease
from .agents import AgentError, AgentSpec, load_agent
from .agents import survey_agents as discover_agents
from .config import EFFORT_INHERIT, EFFORT_LEVELS, Config, ConfigError
from .context import estimate_text_tokens, prefetch
from .loop import (
    AgenticDispatch,
    ContextOverflowAborted,
    Delegation,
    Dispatch,
    DispatchTimedOut,
    InvalidDelegation,
    run_agentic_loop,
    run_one_shot,
)
from .paths import PathPolicyError, PathRefused, resolve_all, resolve_workdir
from .registry import ModelEntry, Registry, RegistryError
from .slots import build_slots, cross_process_status
from . import transcript
from .tools import BashPolicy, resolve_allowed

SERVER_NAME = "delegate-local"

# The closed vocabulary backend_status() reports. `backend_unreachable` is not ours to
# rename: docs/TROUBLESHOOTING.md already promises it to anyone diagnosing a symptom.
STATUS_OK = "ok"
STATUS_UNREACHABLE = "backend_unreachable"
STATUS_AUTH_FAILED = "auth_failed"
STATUS_REFUSED = "backend_refused"
STATUS_PROTOCOL_ERROR = "backend_protocol_error"
STATUS_MISCONFIGURED = "misconfigured"


def _loop_ledger(dispatched: Dispatch | AgenticDispatch) -> dict[str, Any]:
    """The turn-loop counters, present only when a loop actually ran.

    Absent rather than zero-filled on the one-shot path. `tool_calls: 0` alongside an
    answer would read as a model that chose not to use its tools, when in fact it was
    never offered any -- a distinction the caller acts on differently.
    """
    if not isinstance(dispatched, AgenticDispatch):
        return {}
    return {
        "turns": dispatched.turns,
        "tool_calls": dispatched.tool_calls,
        "tool_errors": dispatched.tool_errors,
        "tool_calls_deduplicated": dispatched.deduped,
        "tool_results_evicted": dispatched.evicted,
        "hit_turn_limit": dispatched.hit_turn_limit,
        # Present at zero, not absent, once a loop ran: these share the gate above rather
        # than getting a narrower one of their own. A delegation that was offered run_bash
        # and did not use it is a real answer, and the same one `tool_calls: 0` gives.
        "bash_calls": dispatched.bash_calls,
        "bash_failures": dispatched.bash_failures,
        # None means nothing exited -- no command ran, or the last was killed on timeout.
        # 0 is a real exit code and cannot carry either meaning (ADR-0007).
        "last_bash_exit": dispatched.last_bash_exit,
    }


def _diagnostics_block(
    dispatched: Dispatch | AgenticDispatch, *, requested: bool
) -> dict[str, Any]:
    """The per-turn ledger, present only when the caller asked and a loop actually ran.

    Absent rather than empty, following `_loop_ledger` above and for the same reason: an
    empty `diagnostics` alongside an answer would read as a delegation that took no turns,
    when in fact none was recorded. The one-shot path has no turns to report at all.

    `evicted_then_reread` is the field worth reading. Turn costs say a delegation was
    expensive; this says whether it was expensive because the work was large or because it
    kept paying again for context this server had dropped. Those have different fixes --
    a bigger job versus a larger `keep_tool_results` -- and no other number distinguishes
    them.
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
                    "attempts": t.attempts,
                    "effort": t.effort,
                    "tool_results_evicted": t.evicted,
                    "tool_calls": [{"name": n, "outcome": o} for n, o in t.tool_calls],
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

    Built lazily and kept for the life of the server. Rebuilding per call would throw
    away the pool and its connection warmup on every delegation, and `backend_status()`
    would open a second pool alongside `delegate()`'s for the same endpoint.
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
        # aclose() is documented safe to call more than once, so teardown does not need
        # to guard against having already run.
        for backend in self._backends.values():
            await backend.aclose()
        self._backends.clear()


class WindowCheck:
    """Whether overflow handling may be armed for a model, and why not when it may not.

    Every context-overflow threshold is a share of `ModelEntry.context_window`, which is a
    number the operator wrote in `models.toml` and which nothing has ever verified --
    `docs/MODELS.md` says as much: "used for budgeting headroom, not enforced against the
    server". Arming a graduated abort against an unverified denominator is precisely how
    upstream came to compute every threshold against a ceiling the backend would never
    reach. So the window is checked once per model before the feature is allowed to act.

    The check **validates and never derives**. A disagreement disarms overflow handling and
    says so; it does not quietly adopt the server's number, because the operator's file is
    the source of truth for everything else about that model and a config that is silently
    overridden in one field is worse than one that is wrong in the open.

    Three verdicts, and the distinction between the last two is the point of the class:

    - the endpoint agrees, or reports no window at all -> armed;
    - the endpoint **answered** and disagrees -> disarmed, cached, and re-checked when the
      entry expires;
    - the endpoint could not be **reached** -> disarmed for this call and NOT cached.

    Upstream's negative cache had no expiry and was populated by any failure, so one
    transient outage disabled overflow handling until the server was restarted. Both halves
    of that are fixed here: a transport failure never writes to the cache, and what does get
    written expires.
    """

    def __init__(self, cfg: Config, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._cfg = cfg
        self._clock = clock
        self._verdicts: dict[str, tuple[str, float]] = {}

    async def armed(self, backend: Backend, entry: ModelEntry) -> tuple[bool, str]:
        """May overflow handling act for this model? Returns the verdict and a reason."""
        if not self._cfg.context_overflow_enabled:
            return False, ""
        cached = self._verdicts.get(entry.key)
        if cached is not None and self._clock() - cached[1] < self._cfg.overflow_probe_cache_ttl:
            return not cached[0], cached[0]

        try:
            reported = await backend.probe_window()
        except BackendUnavailable:
            # Reached nothing, so learned nothing. Deliberately not cached: this is the
            # transient outage that used to disable the feature until a restart.
            return False, "the endpoint could not be reached to check its context window"
        except (BackendRefused, BackendProtocolError) as e:
            reason = f"the endpoint refused the context-window check ({type(e).__name__})"
            self._verdicts[entry.key] = (reason, self._clock())
            return False, reason

        if reported is not None and reported != entry.context_window:
            # Never adopted. The mismatch is reported and the feature stays off, because a
            # threshold computed against the wrong window is the bug this exists to avoid.
            # Which of the two numbers came from the operator decides the advice, so
            # the report has to distinguish them. It used to say "models.toml gives
            # context_window=..." even when models.toml gives no such key, sending
            # someone to correct a line that was not there. When it was defaulted the
            # endpoint has just told us the right value, so the remedy can name it.
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

        self._verdicts[entry.key] = ("", self._clock())
        return True, ""


async def arm_overflow(
    windows: WindowCheck, backend: Backend, entry: ModelEntry, cfg: Config, *, agentic: bool
) -> tuple[Config, str]:
    """The config the turn loop should run under, and why it differs if it does.

    Decided here rather than inside the loop so that `run_agentic_loop` has one switch to
    read instead of a switch and a verdict. Returns a *new* config rather than mutating the
    server's: the disarm applies to this delegation, and a model whose endpoint was briefly
    unreachable must not stay disarmed for every other model in the registry.
    """
    if not agentic or not cfg.context_overflow_enabled:
        # The one-shot path has no turns, no history and nothing to overflow, so it must
        # not spend a round trip discovering that.
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
    diagnostics: bool = False,
    report_progress: Callable[[int, int], Awaitable[None]],
    on_alive: Callable[[float, int], Awaitable[None]] | None = None,
    on_turn_done: Callable[[Any, str], Awaitable[None]] | None = None,
) -> Dispatch | AgenticDispatch:
    """Run the delegation on whichever path the toolset implies, and translate its failures.

    Both halves belong together: which path ran decides which failures are possible, and
    every one of them has to reach the caller as a `ToolError` rather than a traceback. Out
    of `build()` because it is the only part of that function that is not wiring.
    """
    try:
        if allowed:
            return await run_agentic_loop(
                cfg, entry, backend, delegation,
                allowed=allowed, effort=effort, max_tokens=max_tokens,
                max_turns=max_turns, policy=policy,
                diagnostics=diagnostics, report_progress=report_progress,
                on_alive=on_alive, on_turn_done=on_turn_done,
            )
        # An explicitly empty toolset. Not the loop with nothing declared: the one-shot
        # prompt tells the model plainly that it cannot open anything and has no second
        # turn, which is true here and is not true in the loop.
        # `on_alive` and not `report_progress`: the loop reports a turn number out of a
        # turn budget, and a one-shot has neither. What it can report is how long it has
        # been waiting and what it is waiting against, which is a different shape.
        return await run_one_shot(
            cfg, entry, backend, delegation, effort=effort, max_tokens=max_tokens,
            on_alive=on_alive,
        )
    except ContextOverflowAborted as e:
        # Before the plain InvalidDelegation branch, which is its base class. The report is
        # the whole point: an abort lands mid-work, and what the caller needs is what the
        # server watched happen beside what git says is on disk, not the message.
        raise ToolError(f"{e}\n\n{json.dumps(e.report, indent=2, default=str)}") from e
    except InvalidDelegation as e:
        raise ToolError(str(e)) from e
    except DispatchTimedOut as e:
        # Not routed through _refuse: that names an endpoint, and this failure is a deadline
        # the operator set. The message already carries the elapsed time, the limit and
        # which stage was running when it expired.
        raise ToolError(str(e)) from e
    except (BackendUnavailable, BackendRefused, BackendProtocolError) as e:
        raise _refuse(e) from e


async def probe_entry(
    cache: BackendCache, cfg: Config, entry: ModelEntry, *, is_default: bool
) -> dict[str, Any]:
    """Probe one registry entry and describe it. Never raises.

    A dead endpoint is a finding, not a failure: one unreachable model must not take
    the report down for every other one. Each probe therefore catches its own errors
    and returns them as data, which is also why the gather below does not need
    `return_exceptions=True` -- that flag would swallow a genuine bug in a sibling.

    Nothing returned here names the endpoint. ADR-0029.
    """
    row: dict[str, Any] = {
        "key": entry.key,
        "is_default": is_default,
        "api_format": entry.api_format,
        "served_model_id": entry.served_model_id,
        "context_window": entry.context_window,
        # Whether that number is the operator's claim or this server's
        # assumption. Same class of fact as id_confirmed: the endpoint is fine,
        # and what the registry says about it may still not be what was meant.
        "context_window_defaulted": entry.context_window_defaulted,
        "concurrency": entry.concurrency,
        "status": STATUS_OK,
        "id_confirmed": None,
        "detail": "",
    }

    try:
        backend = cache.get(entry)
    except (ConfigError, CanonicalShapeError) as e:
        # Never reached the network: an unset api_key_env, or a format no adapter
        # implements. Both are settings problems, and saying so beats "unreachable".
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
        # A bad key is not a dead cluster, and telling them apart is the difference
        # between editing .env and paging whoever owns the hardware.
        row["status"] = STATUS_AUTH_FAILED if e.status in (401, 403) else STATUS_REFUSED
        row["detail"] = f"The endpoint answered {e.status} for {e.url_path}."
        return row
    except BackendProtocolError as e:
        row["status"] = STATUS_PROTOCOL_ERROR
        row["detail"] = str(e)
        return row

    # Reachable. The remaining question is whether it serves what we think it serves.
    # Exact equality, matching Registry.resolve() and docs/MODELS.md's "exactly as the
    # server reports it" -- a version suffix that differs is a different model.
    row["id_confirmed"] = entry.served_model_id in served
    if not row["id_confirmed"]:
        row["detail"] = (
            f"Reachable, but it does not serve {entry.served_model_id!r}. It lists "
            f"{len(served)} model(s). Requests to this entry will be refused by the "
            "endpoint, or worse, answered by a different model than the one configured."
        )
    return row


def _refuse(e: Exception) -> ToolError:
    """Translate a backend failure into something the caller can act on.

    A ToolError raised here reaches the model verbatim -- FastMCP re-raises its own
    error type rather than masking it -- so these strings are part of the contract, and
    the words are the ones docs/TROUBLESHOOTING.md indexes by. The distinctions are the
    point: unreachable is somebody else's hardware, refused is usually a wrong path,
    and a protocol error means the endpoint answered but is not the stack we meant.
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

    Not a `TurnDiagnostic`: the loop builds those and a one-shot never enters the loop.
    Rather than teach the stream about two shapes, the one shape it knows is presented
    here -- turn one, the tokens the dispatch reported, and no tool calls, because there
    were none to make.
    """

    __slots__ = ("attempts", "effort", "input_tokens", "output_tokens", "tool_calls",
                 "turn")

    def __init__(self, dispatched: Any) -> None:
        self.turn = 1
        self.input_tokens = dispatched.response.input_tokens
        self.output_tokens = dispatched.response.output_tokens
        self.effort = dispatched.effort
        self.attempts = dispatched.attempts
        self.tool_calls = ()


async def run_delegation(  # noqa: PLR0913, PLR0915, PLR0912 -- one tool's arguments,
    # one dispatch. The branches are the paths a dispatch can end on, and each one has
    # to close the transcript stream as well as write the record.
    cfg: Config,
    registry: Registry,
    cache: BackendCache,
    windows: WindowCheck,
    admission: Admission,
    *,
    task: str,
    files: list[str] | None = None,
    model: str | None = None,
    effort: str | None = None,
    allowed_tools: list[str] | None = None,
    max_tokens: int | None = None,
    max_turns: int | None = None,
    agent: AgentSpec | None = None,
    workdir: str | None = None,
    diagnostics: bool = False,
    ctx: Context | None = None,
    on_turn: Callable[[], Awaitable[None]] | None = None,
    tool_name: str = "delegate",
) -> dict[str, Any]:
    """One delegation, from arguments to the result dict. Shared by every tool that runs one.

    `delegate`, `delegate_to_agent` and each item of `delegate_batch` differ only in where
    their arguments come from, so they resolve and then reuse this rather than each growing
    a dispatch path of its own. A second path is how the two halves of a precedence rule
    drift apart, and precedence is exactly what an agent file is.

    The agent, if there is one, is already loaded and validated -- this applies it, and
    does not go looking for it. `workdir` arrives already resolved and root-checked.
    """
    # The tool boundary, and the only place `"inherit"` exists. It has to be spent *before*
    # the agent merge below, not inside `resolve_effort`: that merge is `effort or
    # agent.effort`, and a non-empty string is truthy, so leaving `"inherit"` in place would
    # skip the very tier it means to defer to. Refused here rather than four calls later,
    # where the message would name the levels without naming this one (ADR-0045).
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
    # Precedence, once, here: explicit call argument, then the agent file, then the
    # registry row, then the global default (docs/AGENTS.md). Resolved to a single
    # `ModelEntry` that is then used for everything -- the backend cache key, the dispatch,
    # and whatever counts concurrency against the endpoint. The ancestor bug this guards is
    # two of those resolving separately and disagreeing, so a request is counted against one
    # endpoint and sent to another.
    try:
        entry = registry.resolve(model or (agent.model if agent else None))
    except RegistryError as e:
        raise ToolError(str(e)) from e  # already names the registered keys

    if agent is not None:
        # Every one of these is `call argument or agent file`, and `max_turns` was the
        # exception until it stopped being one. It read `= agent.max_turns`, so the single
        # setting that truncates a run was the single setting an agent file could not be
        # overruled on -- while `delegate_to_agent` sells per-call override as the point.
        # The clamp still binds either way: `resolve_max_turns` caps whatever arrives here.
        effort = effort or agent.effort
        max_tokens = max_tokens or agent.max_tokens
        max_turns = max_turns or agent.max_turns
        if allowed_tools is None and agent.allowed_tools is not None:
            allowed_tools = list(agent.allowed_tools)
        if agent.keep_tool_results is not None:
            # `replace` rather than another parameter through `run_agentic_loop`, which is
            # already at its argument limit. The same seam `arm_overflow` uses below.
            #
            # The tightened value moves with it. `Config` refuses to load when the overflow
            # figure exceeds the ordinary one, and rightly -- crossing a threshold must
            # narrow the history, never widen it. An agent asking to keep fewer results than
            # the configured overflow value would otherwise fail validation here, mid-call,
            # naming a setting the caller never touched.
            cfg = replace(
                cfg,
                keep_tool_results=agent.keep_tool_results,
                overflow_tightened_keep_tool_results=min(
                    cfg.overflow_tightened_keep_tool_results, agent.keep_tool_results
                ),
            )

    # Before the backend is even looked up: a refused path must cost nothing, and
    # must not depend on whether the cluster happens to be reachable today.
    try:
        resolved = resolve_all(cfg, files or [])
    except PathRefused as e:
        raise ToolError(str(e)) from e
    except PathPolicyError as e:
        raise ToolError(f"{STATUS_MISCONFIGURED}: {e}") from e

    prefetched = prefetch(cfg, resolved)

    try:
        backend = cache.get(entry)
    except (ConfigError, CanonicalShapeError) as e:
        raise ToolError(f"{STATUS_MISCONFIGURED}: {e}") from e

    delegation = Delegation(
        task=task,
        files_block=prefetched.block(),
        agent_body=agent.body if agent else "",
    )
    allowed = resolve_allowed(allowed_tools, cfg)
    policy = BashPolicy(
        workdir=workdir,
        network=agent.network if agent else False,
        extra_binds=agent.extra_binds if agent else (),
    )

    async def progress(turn: int, of: int) -> None:
        """ADR-0018: this is what stops the client abandoning a delegation still running.

        The client's stdio idle timer is 1800s and `dispatch_timeout` defaults to 3600s,
        so without a per-turn notification a long delegation is dropped by the caller
        while the server works on. Nothing renders it and it cannot be cancelled through
        -- resetting that timer is the whole of its job.

        `on_turn` exists because a caller can need the timer reset while wanting nothing
        to do with this delegation's turn numbers -- `delegate_batch` runs several at
        once, where interleaved counts describe nothing. It reports what it likes; what
        matters is that something is sent on every turn of every item.
        """
        if on_turn is not None:
            await on_turn()
            return
        if ctx is not None:
            await ctx.report_progress(progress=turn, total=of)

    loop_cfg, overflow_off_because = await arm_overflow(
        windows, backend, entry, cfg, agentic=bool(allowed)
    )

    # Sized here, where the cost is known and nothing has been sent yet. Two numbers,
    # because the gate needs two: the prompt is what gets prefilled, and the prompt plus
    # the reply the endpoint is allowed to generate is what occupies the KV pool. Fixed
    # for the whole delegation -- see `admission` for why that is an approximation and
    # how you would find out it was a bad one. The same resolved `entry` that will serve
    # the request supplies the per-endpoint limit, because counting against one endpoint
    # and dispatching to another is the exact bug the single resolution above prevents.
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
    turn_clock = time.monotonic()
    streamed_out_tokens = 0
    streamed_backend_ms = 0

    async def streamed_turn(diagnostic: Any, text: str, backend_seconds: float) -> None:
        """Each finished turn, appended while the delegation is still running.

        Timing is measured here rather than taken from the loop because what a watcher
        wants is wall-clock between turns landing -- including the wait for a slot and the
        tool execution -- not the backend call alone.
        """
        nonlocal turns_streamed, turn_clock, streamed_out_tokens, streamed_backend_ms
        now = time.monotonic()
        turns_streamed += 1
        backend_ms = int(backend_seconds * 1000)
        streamed_out_tokens += getattr(diagnostic, "output_tokens", 0) or 0
        streamed_backend_ms += backend_ms
        if stream is not None:
            stream.turn(diagnostic, text, ms=int((now - turn_clock) * 1000),
                        backend_ms=backend_ms)
        turn_clock = now

    if stream is not None:
        stream.start(
            tool=tool_name,
            task=task, agent=agent.name if agent else None,
            model_key=entry.key, effort=effort,
            tools=allowed, prefetched=prefetched,
        )

    async def ticked() -> None:
        """ADR-0018 again, one layer earlier.

        A queued delegation runs no turns, so nothing else resets the client's idle
        timer while it waits. Without this a delegation that is merely queued is
        abandoned by the caller exactly as a slow one used to be.
        """
        await progress(0, 0)

    async def alive(elapsed_seconds: float, of_seconds: int) -> None:
        """ADR-0018 once more, for the path that has no turns to hang it on.

        A one-shot is a single backend call, so nothing lands between `start` and `end`
        and the client hears nothing for however long that takes -- measured as the only
        remaining shape that can reach the 1800s stdio idle timeout and be abandoned
        while working perfectly.

        It writes to the transcript as well as the wire, and for the same reason in a
        different place: a stream that is silent for forty minutes is indistinguishable
        from one whose server was killed, and the viewer was calling both of them live.
        One heartbeat answers both, because both are the same question.
        """
        await progress(0, 0)
        if stream is not None:
            stream.alive(elapsed_seconds=elapsed_seconds, of_seconds=of_seconds)

    # Captured before anything is attempted, and never re-derived afterwards. The upstream
    # bug this shape exists to prevent is a failure path with no agent name to report, so
    # the very dispatches the transcript explains logged as unknown -- and the only place
    # the name is in scope is here, before the attempt. ADR-0024.
    started = time.monotonic()
    lease: AdmissionLease | None = None
    dispatched: Dispatch | AgenticDispatch | None = None
    failure: BaseException | None = None
    try:
        async with admission.admit(
            tokens_estimate,
            prefill_tokens=prefill_estimate,
            entry_key=entry.key,
            entry_limit=entry.concurrency,
            deadline=time.monotonic() + cfg.admission_wait_timeout,
            on_wait=ticked,
        ) as lease:
            dispatched = await dispatch_delegation(
                loop_cfg, entry, backend, delegation,
                allowed=allowed, effort=effort, max_tokens=max_tokens,
                max_turns=max_turns,
                policy=policy,
                # The caller's flag decides what the *caller* is shown, below. A transcript
                # asks for the per-turn records itself, because the loop only keeps them
                # when told to -- and an operator record that depended on the calling
                # session having asked would not be an operator record.
                diagnostics=diagnostics or transcript.enabled(cfg),
                report_progress=progress,
                on_alive=alive,
                on_turn_done=streamed_turn,
            )
    except AdmissionError as e:
        # Not routed through `_refuse`, for the same reason `DispatchTimedOut` is not:
        # that names an endpoint as the thing at fault, and nothing here reached one.
        failure = e
        raise ToolError(str(e)) from e
    except BaseException as e:
        failure = e
        raise
    finally:
        # A side effect whose result nothing reads. The other upstream bug was the record
        # reaching the response through a dict merge, so there is deliberately no value
        # here for the return below to pick up.
        if stream is not None:
            # A one-shot delegation runs no turns, so nothing streamed the answer. Without
            # this the common read-only case would show a start and an end with nothing
            # between them -- which is exactly the shape of a delegation that produced
            # nothing, and indistinguishable from one.
            if turns_streamed == 0 and dispatched is not None:
                stream.turn(
                    _OneShotTurn(dispatched), dispatched.response.text,
                    ms=int((time.monotonic() - turn_clock) * 1000),
                    backend_ms=int((time.monotonic() - turn_clock) * 1000),
                )
                streamed_out_tokens = dispatched.response.output_tokens or 0
                streamed_backend_ms = int((time.monotonic() - turn_clock) * 1000)
            stream.end(
                ok=failure is None,
                turns=getattr(dispatched, "turns", 1) if dispatched else None,
                elapsed_seconds=time.monotonic() - started,
                output_tokens=streamed_out_tokens or None,
                backend_ms=streamed_backend_ms or None,
                error=str(failure) if failure is not None else None,
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

    response = dispatched.response
    answer = response.text
    return {
        **prefetched.accounting(),
        # Which file shaped this, when one did. A delegation that behaved unexpectedly is
        # usually an agent file behaving as written, and the caller cannot check that
        # without knowing which file was read -- the lookup has three tiers, so the name
        # alone does not identify it.
        **({"agent": agent.name, "agent_source": agent.source_path} if agent else {}),
        "answer": answer,
        # The model as the backend reported it, not as the caller asked for it.
        # Server-captured ground truth is the only kind worth reporting. ADR-0007.
        "model": response.model,
        "finish_reason": response.finish_reason,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "effort": dispatched.effort,
        # Real backend calls made, counted by the server rather than inferred. More
        # than one means something failed and was retried without the caller having to
        # care; the token counts above describe the attempt that answered, not the sum.
        "attempts": dispatched.attempts,
        # The loop's ledger, and only when there was a loop. Reporting turns: 1 for the
        # one-shot path would be a number the caller could compare against a budget that
        # never applied to it. ADR-0007: what the server watched, not what was claimed.
        **_loop_ledger(dispatched),
        # Still the mechanical fact, and still reported on its own: "" must never read
        # as a successful reply. What changed is that reaching here means the state
        # machine already tried a larger budget and a lower effort.
        "empty_response": answer == "",
        # The diagnosis, which is a different claim and only earned once the
        # mitigations have actually been spent. True means every one was tried and the
        # answer is still empty at a length stop -- ADR-0014's reasoning_exhausted_budget.
        # False alongside an empty answer means the effort was already at its lowest,
        # so nothing was left to step down and the budget, not the reasoning, is what
        # ran out. Two different fixes, which is why they are two different fields.
        "reasoning_exhausted": dispatched.reasoning_exhausted,
        # Present only when the operator armed overflow handling and this server
        # declined to use it. Absent means it was off, or on and working -- the two the
        # caller has no decision to make about.
        **({"overflow_disarmed": overflow_off_because} if overflow_off_because else {}),
        **_diagnostics_block(dispatched, requested=diagnostics),
    }


def build(  # noqa: PLR0915 -- the statement count is the tool count; see the docstring
    cfg: Config, registry: Registry, cache: BackendCache | None = None
) -> FastMCP:
    """Construct the server. Pure wiring; no I/O beyond what the tools do when called.

    `cache` is injectable for the same reason `OpenAICompatBackend` takes a `client`:
    without it a test of the tool surface opens a real socket and waits out a real
    timeout, which is slow, flaky, and not what the test is about.
    """
    cache = cache or BackendCache(cfg)
    # One per server, beside the backend cache and for the same reason: the verdict is
    # per model and per process, and re-probing every delegation would spend a round
    # trip to re-learn something that changes only when the operator edits a file.
    windows = WindowCheck(cfg)
    # One per process, beside the cache and the window check so every tool closure below
    # shares the same object rather than each counting against a gate of its own. That is
    # necessary and was never sufficient: the object is global only within this process,
    # and stdio gives every connected client a process of its own. `slots` is what makes
    # the budget global across them (ADR-0040); without it the four rules bound one editor
    # window each. `build_slots` returns None when the platform cannot lock, and the
    # reason is reported rather than swallowed -- a gate that has quietly narrowed its
    # scope looks exactly like one that is working.
    slots, slots_reason = build_slots(cfg)
    admission = Admission(cfg, slots)

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            await cache.aclose()

    mcp: FastMCP = FastMCP(name=SERVER_NAME, lifespan=lifespan)

    @mcp.tool
    async def delegate(  # noqa: PLR0913 -- ctx is injected, not an argument the caller sees
        task: str,
        # Required, and sitting here only because a parameter without a default cannot
        # follow one that has it. Effort changes both what a call costs and how good the
        # answer is, and it was previously chosen by saying nothing -- four links down a
        # precedence chain to `thinking_default`, without the caller ever seeing the
        # decision. `"inherit"` is how a caller defers on purpose (ADR-0045).
        effort: str,
        files: list[str] | None = None,
        model: str | None = None,
        *,
        # Keyword-only from here, and not merely to satisfy a lint: MCP passes every
        # argument by name, so positional order is a promise to nobody.
        allowed_tools: list[str] | None = None,
        max_tokens: int | None = None,
        max_turns: int | None = None,
        diagnostics: bool = False,
        # Not an argument at all -- fastmcp injects it by type, and it never appears in
        # the schema the model reads.
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Hand one self-contained task to a local model and get its answer back.

        For bulk, mechanical or read-heavy work whose reasoning is modest: drafting,
        first-pass review, mechanical rewrites, explaining something. It runs on hardware
        the user hosts, so it costs no cloud tokens -- prefer it whenever the work does
        not need your own judgement.

        Set `diagnostics=true` to get a per-turn breakdown back alongside the answer:
        what each turn's prompt cost, what it evicted, which tools it ran, and -- the part
        worth asking for -- which files were read again after the server had dropped the
        first read from the history. Use it when a delegation was slower or more expensive
        than the work justified and you want to know which. It makes the reply larger and
        changes nothing about how the work is done, so leave it off by default.

        **Name files in `files[]` rather than pasting them into `task`.** The server
        reads them itself and gives them to the model directly, so their contents never
        enter your context and cost you nothing. Reading a file yourself in order to
        paste it here defeats the entire point of this tool. Give absolute paths, in
        whatever form you already have them -- Windows paths are translated for you.

        The model works in turns, and can read and write files in the workspace itself.
        So `files[]` is a head start rather than the whole world: name what it obviously
        needs, and let it find the rest. It can also run shell commands, confined to a
        sandbox that holds nothing of yours and has no network unless an agent asks for
        one -- pass `workdir` to bind a directory it can actually build or test in. Narrow
        that with `allowed_tools` when you want it reading only, and expect the shell to be
        absent on a host without bubblewrap. Path rules are the same ones that govern
        `files[]`, and a write it is refused comes back to it as a refusal it can correct,
        not as a failed call.

        `effort` is required, because it changes both what the call costs and how good the
        answer is, and there is no sensible value to pick on your behalf: one of "off",
        "low", "high", "max", or "inherit" to defer to the configured default. Match it to
        the task -- "low" for summarising, quoting, or anything mechanical; "high" when the
        answer depends on reasoning across what it was given, such as a review, a trace
        through several files, or a design question. "max" is for the rare case that has
        already come back thin at "high".

        `model` names a registered model, defaulting to the configured one. `allowed_tools`
        narrows what the model may use -- omit it for everything available, or pass an
        empty list for a single-turn answer with no tools at all, which is the cheapest
        shape when the task is self-contained and `files[]` already holds everything.

        `max_tokens` caps one reply. Leave it alone unless you have a reason: the default
        is already raised at high and max effort so reasoning does not consume the whole
        allowance and return nothing, and naming a small number here opts out of that
        headroom rather than saving anything. It is honoured as given, up to whatever the
        model itself accepts.

        `max_turns` caps how many round trips it gets. Raise it for work that genuinely
        iterates -- a wide audit, a refactor across many files -- and read
        `hit_turn_limit: true` as the sign you should have. It is clamped to a ceiling the
        operator sets, silently, so asking for more than that is not an error.

        A path in `files[]` that is not allowed fails the whole call before anything is
        sent, and the error names every rejected path, the layer that rejected it, and
        what to do. A file that is allowed but too large or not text is **skipped**: the
        call proceeds without it, and `files_skipped` says which and why. Read that field
        before trusting an answer; the model was told the file was unavailable, but it
        cannot tell you what it never saw.

        Returns `answer` plus what the server watched happen, rather than the model's
        account of it: `turns` is how many round trips it took, `tool_calls` how many
        tools actually ran, `tool_errors` how many of those were refused, and `attempts`
        the real number of backend calls, which exceeds `turns` when something failed and
        was retried for you. A model's summary of its own work is not evidence; these are.

        `hit_turn_limit: true` means it was still calling tools when its turns ran out.
        The answer is whatever it could write once tools were withdrawn, so treat it as
        partial -- and verify any file it claims to have written, because the count of
        tools that ran does not say what they did.

        Do not retry this call yourself to work around an empty answer or a flaky
        endpoint. The server already does both, and repeating it here spends your context
        to redo work that was done. A dropped route or a temporary refusal is retried with
        backoff; an answer that comes back empty at a length stop -- reasoning having
        consumed the whole reply budget -- is retried at a larger budget and then at a
        lower effort before you are told about it.

        So `empty_response: true` means those were already tried and it is still empty.
        Read `reasoning_exhausted` alongside it. True: the task needs more reasoning than
        this model can finish inside its budget, so split it or send it somewhere else --
        asking again, or at a lower effort, will not help. False: the budget was simply
        too small for the answer at an effort that is already the lowest, so a shorter
        task or a model with a larger cap is the fix. Either way, report the empty result
        rather than presenting it as a model with nothing to say.

        If the call fails outright, `backend_status()` will say whether the model is down,
        misconfigured, or serving something other than it should.
        """
        return await run_delegation(
            cfg, registry, cache, windows, admission,
            task=task, files=files, model=model, effort=effort,
            allowed_tools=allowed_tools, max_tokens=max_tokens, max_turns=max_turns,
            diagnostics=diagnostics, ctx=ctx, tool_name="delegate",
        )

    @mcp.tool(annotations={"readOnlyHint": True})
    async def delegate_readonly(  # noqa: PLR0913 -- one tool's arguments, one dispatch
        task: str,
        effort: str,
        files: list[str] | None = None,
        model: str | None = None,
        *,
        max_tokens: int | None = None,
        diagnostics: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Hand one self-contained task to a local model that has no tools at all.

        `delegate` with `allowed_tools=[]`, fixed rather than asked for. What it can do is
        identical; what differs is what a caller can promise about it *before* it runs. It
        is declared read-only, so a client that gates writes on that declaration can run it
        where `delegate` has to stop and ask.

        Use it for the read-heavy majority: explaining, summarising, first-pass review,
        drafting an answer about code named in `files[]`. The model answers in one turn
        from `files[]` and nothing else, so name everything it needs -- it cannot go
        looking, and a gap shows up as a worse answer rather than as a request.

        Reach for `delegate` when the work needs the model to read further on its own,
        write a file, or run a command. This is not a cheaper `delegate`; it is a
        narrower one, and asking it to edit something will produce a description of the
        edit instead.

        `effort` is required: one of "off", "low", "high", "max", or "inherit" to defer to
        the configured default. Read-only does not mean undemanding -- "low" suits
        summarising and quoting, but a review or a trace across several files needs "high",
        and answering it at "low" spends the call for a worse answer rather than a cheaper
        one.
        """
        return await run_delegation(
            cfg, registry, cache, windows, admission,
            task=task, files=files, model=model, effort=effort,
            # Fixed, never defaulted, and not exposed as an argument. `[]` resolves to the
            # empty set where `None` resolves to everything available, and a caller has no
            # way to widen it back. That is what makes the annotation above a property of
            # the tool rather than a claim about how it is usually called -- an annotation
            # a caller could falsify by passing an argument would be exactly the check
            # that cannot fail.
            allowed_tools=[], max_tokens=max_tokens,
            diagnostics=diagnostics, ctx=ctx, tool_name="delegate_readonly",
        )

    def _load(agent_name: str, workdir: str | None) -> AgentSpec:
        try:
            return load_agent(cfg, agent_name, workdir)
        except AgentError as e:
            raise ToolError(str(e)) from e

    def _workdir(given: str | None) -> str | None:
        """Resolved and root-checked once, here, so every tool below gets the same answer."""
        if given is None:
            return None
        try:
            return resolve_workdir(cfg, given)
        except PathRefused as e:
            raise ToolError(str(e)) from e
        except PathPolicyError as e:
            raise ToolError(f"{STATUS_MISCONFIGURED}: {e}") from e

    @mcp.tool
    async def delegate_to_agent(  # noqa: PLR0913 -- ctx is injected, not a caller argument
        agent_name: str,
        task: str,
        files: list[str] | None = None,
        workdir: str | None = None,
        *,
        model: str | None = None,
        effort: str,
        allowed_tools: list[str] | None = None,
        max_tokens: int | None = None,
        max_turns: int | None = None,
        diagnostics: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Delegate to a named agent -- a markdown file that shapes how the task is done.

        Use this over `delegate` when the work has a *kind*: writing tests, reviewing a
        diff, migrating an API. The agent file carries the instructions, the model, the
        reasoning effort and the tools that kind of work needs, so you send the task and not
        the preamble. `list_agents()` shows what is available.

        `workdir` is what makes an agent able to work rather than only read. It binds that
        directory into the sandbox, writable, so `run_bash` can run the project's tests or
        its linter there. Without it the model can still read files you name and write
        through `write_file`, but a shell has nothing of yours to run against. Give the
        repository root, in whatever path form you already have.

        Every explicit argument here wins over the agent file, so you can send one hard case
        to `test-writer` at a larger model without editing the file. Omit them and the file
        decides.

        `effort` is the exception: it cannot be omitted, because a value chosen by silence
        is what this argument exists to stop. Pass "inherit" to let the agent file decide --
        which is usually right here, since a well-written agent binds the effort its kind of
        work needs -- or one of "off", "low", "high", "max" to overrule it for this call.

        The reply is `delegate`'s, plus `agent` naming the file that was used. Read
        `bash_failures` and `last_bash_exit` over the model's own prose: those are real
        process exits the server captured, and a model summarising its own test run is not
        evidence (ADR-0007).
        """
        # The workdir is checked *before* it is used to look anything up. The agent lookup
        # reads `<workdir>/.claude/agents/`, so passing the caller's argument to it
        # unchecked would let an unvalidated path drive a filesystem read -- the root check
        # would then be a thing that happened afterwards, which is not a check at all.
        resolved_workdir = _workdir(workdir)
        agent = _load(agent_name, resolved_workdir)
        return await run_delegation(
            cfg, registry, cache, windows, admission,
            task=task, files=files, model=model, effort=effort,
            allowed_tools=allowed_tools, max_tokens=max_tokens, max_turns=max_turns,
            agent=agent, workdir=resolved_workdir,
            diagnostics=diagnostics, ctx=ctx, tool_name="delegate_to_agent",
        )

    @mcp.tool
    async def delegate_batch(  # noqa: PLR0913 -- ctx is injected, not a caller argument
        tasks: list[str],
        agent_name: str | None = None,
        files: list[str] | None = None,
        workdir: str | None = None,
        *,
        model: str | None = None,
        effort: str,
        allowed_tools: list[str] | None = None,
        max_tokens: int | None = None,
        max_turns: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run several tasks sharing one agent and one set of files, and get all the answers.

        The point is the sharing. Every task gets the same agent body and the same `files[]`
        block, and only the task itself differs -- so the whole prompt up to the task is
        identical between them and the cluster serves it from cache. A batch of eight
        questions about one module costs roughly one read of that module, not eight. Eight
        separate `delegate` calls pay for it eight times.

        So use it when the tasks genuinely share context: several questions about the same
        files, one review applied under different criteria, one migration described once and
        applied to a list of call sites. When they do not share context, call `delegate` for
        each -- an unrelated batch saves nothing and delays the first answer until the last
        one is done.

        Items run concurrently, bounded by what the model's registry entry says its endpoint
        will take, so a large batch queues rather than swamping it.

        `effort` is required and applies to every item: one of "off", "low", "high", "max",
        or "inherit" to defer to `agent_name`'s file and then the configured default. It is
        one value for the whole batch, so tasks needing different depths belong in different
        batches -- which is the same rule as `files[]`, for the same reason.

        `max_turns` is one value for the whole batch too, and applies to each item
        separately rather than being shared out between them.

        **One item failing does not fail the batch.** Each result carries its own `ok`, and a
        failed one carries `error` instead of an answer, with the `index` and `task` that
        produced it. Work already done is never discarded because a later item was refused.
        Read `failed` before trusting any summary of the whole thing.
        """
        if not tasks:
            raise ToolError("tasks[] is empty. There is nothing to delegate.")
        if len(tasks) > cfg.max_batch_size:
            raise ToolError(
                f"{len(tasks)} tasks exceeds DELEGATE_MAX_BATCH_SIZE "
                f"({cfg.max_batch_size}). Split it, or raise the setting. The cap exists "
                "because every item holds a slot on a shared endpoint while it runs."
            )

        # Checked before it is used to look anything up, as in `delegate_to_agent`.
        resolved_workdir = _workdir(workdir)
        agent = _load(agent_name, resolved_workdir) if agent_name else None

        # Resolved once here so an unknown model fails the batch rather than every item in
        # it identically. What this does *not* do any more is bound the batch: the
        # endpoint's declared limit is one of the four rules `admission` checks, so items
        # are held there alongside every other delegation in the process. A semaphore here
        # bounded a batch against itself and nothing else -- two batches, or a batch beside
        # a plain `delegate`, could still exceed the very limit it was reading.
        try:
            registry.resolve(model or (agent.model if agent else None))
        except RegistryError as e:
            raise ToolError(str(e)) from e

        done = 0

        async def beat() -> None:
            """ADR-0018's keepalive, restored to the batch.

            The count reported is the batch's own -- items finished out of items asked
            for -- so nothing a reader sees interleaves. What it costs is one
            notification per turn per item instead of one per item, and that is the
            point: the client's idle timer is reset by the notification arriving, not by
            the number in it. Reporting only on completion left a batch whose items each
            run longer than the 1800s idle timeout sending nothing at all, and the caller
            abandoned work the cluster then carried on doing for the rest of
            `dispatch_timeout`, holding the machine-wide budget the whole time.
            """
            if ctx is not None:
                await ctx.report_progress(progress=done, total=len(tasks))

        async def run(index: int, one: str) -> dict[str, Any]:
            nonlocal done
            try:
                outcome = await run_delegation(
                    cfg, registry, cache, windows, admission,
                    task=one, files=files, model=model, effort=effort,
                    allowed_tools=allowed_tools, max_tokens=max_tokens,
                    max_turns=max_turns,
                    agent=agent, workdir=resolved_workdir,
                    # No `ctx`, so this delegation's own turn numbers never reach the
                    # client -- interleaved counts from items running at once would
                    # describe nothing a reader could act on. `on_turn` keeps the
                    # notification itself, which is what holds the idle timer open.
                    diagnostics=False, ctx=None, on_turn=beat,
                    tool_name="delegate_batch",
                )
            except ToolError as e:
                # Caught rather than raised, which is the whole contract: an item that
                # failed must not discard the ones that worked. Every failure inside
                # `run_delegation` has already been translated into a `ToolError`.
                outcome = {"ok": False, "error": str(e)}
            else:
                outcome = {"ok": True, **outcome}
            done += 1
            if ctx is not None:
                await ctx.report_progress(progress=done, total=len(tasks))
            return {"index": index, "task": one, **outcome}

        results = await asyncio.gather(*(run(i, t) for i, t in enumerate(tasks)))
        return {
            "results": list(results),
            "count": len(results),
            "failed": [r["index"] for r in results if not r["ok"]],
            **({"agent": agent.name} if agent else {}),
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def list_agents(workdir: str | None = None) -> dict[str, Any]:
        """List the agents available to `delegate_to_agent`, and where each was found.

        Call this before guessing an agent name. Each row carries the `name` to pass, the
        `description` the file gives itself, the `model` and `effort` it binds, and the
        `source` it was read from.

        `workdir` matters. Agents are looked for in that project first and in your personal
        directory second, so a repository can ship one that knows its own conventions. Pass
        the same `workdir` you intend to delegate with, or this list will not match what a
        delegation would actually find.

        Three lists, because "not there", "there and broken" and "there but not mine" need
        different answers and used to give the same one -- a file that did not parse was
        simply left out, which is indistinguishable from a name that does not exist.

        `skipped` is what needs fixing: a file meant for this server that could not be read
        as an agent, with the name it claimed and why it failed. Non-empty always means
        something is wrong.

        `other_format` is Claude Code's own agent format sharing the directory. Those files
        are not broken and are not for this server -- the tool list is spelled `tools`
        there and `allowed_tools` here -- so they are named rather than either hidden or
        called faulty. `delegate_to_agent` cannot run one.

        A name absent from all three does not exist.
        """
        listing = discover_agents(cfg, _workdir(workdir))
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
        }

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def backend_status() -> dict[str, Any]:
        """Report whether each configured local model is reachable and serving what it should.

        Probes every model in the registry at once and returns one row each. Use this
        first whenever a delegation fails, before retrying or giving up: it separates a
        model that is down from one that is misconfigured, and from a key that is wrong.

        Each row carries a `status` of "ok", "backend_unreachable", "auth_failed",
        "backend_refused", "backend_protocol_error" or "misconfigured", and an
        `id_confirmed` flag. `id_confirmed: false` alongside `status: "ok"` is the case
        worth reading closely -- the endpoint is healthy but is not serving the model
        this entry names, so delegating to it will not do what the registry claims.

        The `admission` block reports this process's own gauges and peaks, and, under
        `admission.cross_process`, whether the four rules are being counted across every
        server process on the machine and what that machine-wide total currently is.
        `active: false` there means each connected client is being bounded separately, so
        the real load on the cluster is higher than this process's numbers suggest.

        Endpoint addresses are deliberately never included in the result.
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
        }

    return mcp
