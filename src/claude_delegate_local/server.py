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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from .config import Config, ConfigError
from .context import prefetch
from .loop import (
    AgenticDispatch,
    Delegation,
    Dispatch,
    DispatchTimedOut,
    InvalidDelegation,
    run_agentic_loop,
    run_one_shot,
)
from .paths import PathPolicyError, PathRefused, resolve_all
from .registry import ModelEntry, Registry, RegistryError
from .tools import resolve_allowed

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


def build(cfg: Config, registry: Registry, cache: BackendCache | None = None) -> FastMCP:
    """Construct the server. Pure wiring; no I/O beyond what the tools do when called.

    `cache` is injectable for the same reason `OpenAICompatBackend` takes a `client`:
    without it a test of the tool surface opens a real socket and waits out a real
    timeout, which is slow, flaky, and not what the test is about.
    """
    cache = cache or BackendCache(cfg)

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
        files: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        allowed_tools: list[str] | None = None,
        *,
        # Keyword-only because it is not an argument at all: fastmcp injects it by type,
        # it never appears in the schema the model reads, and a caller has nothing to put
        # here. Positionally it would just be a sixth slot nobody may fill.
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Hand one self-contained task to a local model and get its answer back.

        For bulk, mechanical or read-heavy work whose reasoning is modest: drafting,
        first-pass review, mechanical rewrites, explaining something. It runs on hardware
        the user hosts, so it costs no cloud tokens -- prefer it whenever the work does
        not need your own judgement.

        **Name files in `files[]` rather than pasting them into `task`.** The server
        reads them itself and gives them to the model directly, so their contents never
        enter your context and cost you nothing. Reading a file yourself in order to
        paste it here defeats the entire point of this tool. Give absolute paths, in
        whatever form you already have them -- Windows paths are translated for you.

        The model works in turns, and can read and write files in the workspace itself.
        So `files[]` is a head start rather than the whole world: name what it obviously
        needs, and let it find the rest. It cannot run shell commands. Path rules are the
        same ones that govern `files[]`, and a write it is refused comes back to it as a
        refusal it can correct, not as a failed call.

        `model` names a registered model, defaulting to the configured one; `effort` sets
        reasoning effort explicitly, one of "off", "low", "high", "max". `allowed_tools`
        narrows what the model may use -- omit it for everything available, or pass an
        empty list for a single-turn answer with no tools at all, which is the cheapest
        shape when the task is self-contained and `files[]` already holds everything.

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
        try:
            entry = registry.resolve(model)
        except RegistryError as e:
            raise ToolError(str(e)) from e  # already names the registered keys

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

        delegation = Delegation(task=task, files_block=prefetched.block())
        allowed = resolve_allowed(allowed_tools)

        async def progress(turn: int, of: int) -> None:
            """ADR-0018: this is what stops the client abandoning a delegation still running.

            The client's stdio idle timer is 1800s and `dispatch_timeout` defaults to 3600s,
            so without a per-turn notification a long delegation is dropped by the caller
            while the server works on. Nothing renders it and it cannot be cancelled through
            -- resetting that timer is the whole of its job.
            """
            if ctx is not None:
                await ctx.report_progress(progress=turn, total=of)

        try:
            if allowed:
                dispatched = await run_agentic_loop(
                    cfg, entry, backend, delegation,
                    allowed=allowed, effort=effort, report_progress=progress,
                )
            else:
                # An explicitly empty toolset. Not the loop with nothing declared: the
                # one-shot prompt tells the model plainly that it cannot open anything and
                # has no second turn, which is true here and is not true in the loop.
                dispatched = await run_one_shot(cfg, entry, backend, delegation, effort=effort)
        except InvalidDelegation as e:
            raise ToolError(str(e)) from e
        except DispatchTimedOut as e:
            # Not routed through _refuse: that names an endpoint, and this failure is a
            # deadline the operator set. The message already carries the elapsed time, the
            # limit and which stage was running when it expired.
            raise ToolError(str(e)) from e
        except (BackendUnavailable, BackendRefused, BackendProtocolError) as e:
            raise _refuse(e) from e

        response = dispatched.response
        answer = response.text
        return {
            **prefetched.accounting(),
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
        }

    @mcp.tool
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

        Endpoint addresses are deliberately never included in the result.
        """
        rows = await asyncio.gather(
            *(
                probe_entry(cache, cfg, entry, is_default=key == registry.default_key)
                for key, entry in registry.entries.items()
            )
        )
        return {"default": registry.default_key, "models": list(rows)}

    return mcp
