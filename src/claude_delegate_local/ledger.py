"""An append-only token ledger: one JSON line per dispatch, never pruned (ADR-0104).

The transcripts say what one delegation did and may be aged out; this is the running total.
One `os.write` per line is the whole concurrency guarantee, so never split it or buffer it.
Every failure is swallowed to `logging`, as `transcript.write` does, and nothing here
touches stdout, which is the MCP wire.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import transcript
from .wsl import to_local

if TYPE_CHECKING:
    from .config import Config
    from .loop import AgenticDispatch, Dispatch
    from .registry import ModelEntry

log = logging.getLogger(__name__)


def path(cfg: Config) -> Path | None:
    """The ledger file, or None when switched off. Translated like `transcript_dir`.

    Raises `UntranslatablePath` for a UNC share; `append` swallows it, the doctor reports it.
    """
    configured = cfg.ledger_path.strip()
    if not configured:
        return None
    return Path(os.path.expanduser(to_local(configured)))


def append(cfg: Config, record: dict[str, Any]) -> None:
    """Append one record as one JSON line, created at 0o600 by `os.open` (ADR-0043).

    Never raises: losing a line is worth less than failing a delegation that has done its
    work, and that includes a `ledger_path` that cannot be translated.
    """
    try:
        target = path(cfg)
        if target is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (json.dumps(record, default=str, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        log.warning("could not append to the dispatch ledger", exc_info=True)


def record_for(  # noqa: PLR0913 -- one line's worth of facts, from the same scopes
    # `transcript.write` reads.
    *,
    tool: str,
    agent_name: str | None,
    entry: ModelEntry | None,
    effort: str | None,
    dispatched: Dispatch | AgenticDispatch | None,
    error: BaseException | None,
    started: float,
) -> dict[str, Any]:
    """This dispatch's line. Tokens come from `transcript._usage`, so the two records agree.

    A dispatch that failed before producing a result has null tokens, never zero, which
    would read as "measured, and it cost nothing".
    """
    record: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "tool": tool,
        "agent": agent_name,
        "model_key": entry.key if entry else None,
        "served_model_id": entry.served_model_id if entry else None,
        "effort": effort,
        "ok": error is None,
        "error": type(error).__name__ if error is not None else None,
        "turns": getattr(dispatched, "turns", None),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if dispatched is None:
        record["total_input_tokens"] = None
        record["total_output_tokens"] = None
        record["total_cached_tokens"] = None
        return record
    usage = transcript._usage(dispatched)
    # `dispatched` holds the effort the run was actually held to, which the caller-level
    # `effort` argument is not -- it is still `None` for a resolved default.
    record["effort"] = usage.get("effort") or effort
    record["total_input_tokens"] = usage.get("total_input_tokens")
    record["total_output_tokens"] = usage.get("total_output_tokens")
    record["total_cached_tokens"] = usage.get("total_cached_tokens")
    return record
