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

from fastmcp import FastMCP

from .backends.base import (
    Backend,
    BackendProtocolError,
    BackendRefused,
    BackendUnavailable,
    CanonicalShapeError,
)
from .backends.openai_compat import OpenAICompatBackend
from .config import Config, ConfigError
from .registry import ModelEntry, Registry

SERVER_NAME = "delegate-local"

# The closed vocabulary backend_status() reports. `backend_unreachable` is not ours to
# rename: docs/TROUBLESHOOTING.md already promises it to anyone diagnosing a symptom.
STATUS_OK = "ok"
STATUS_UNREACHABLE = "backend_unreachable"
STATUS_AUTH_FAILED = "auth_failed"
STATUS_REFUSED = "backend_refused"
STATUS_PROTOCOL_ERROR = "backend_protocol_error"
STATUS_MISCONFIGURED = "misconfigured"


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
