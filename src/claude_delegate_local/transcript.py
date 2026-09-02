"""One record per dispatch, written for the operator rather than for the caller. ADR-0024.

**Independent of the caller-facing `diagnostics` flag.** What an operator can audit should
not depend on what the calling session thought to ask for. A delegation that behaved
strangely is usually one nobody suspected in advance, so the record has to already exist
by the time anyone wants it.

Two bugs upstream shipped here are the acceptance criteria, not trivia:

1. **Failure paths lost the agent name**, so the very dispatches the transcript existed to
   explain were logged as `unknown`. The cause is structural: the identity is only in scope
   at the top of `run_delegation`, and anything assembling the record further in has a
   `Delegation` that carries the task but not the agent's *name*. So the record is built
   from values captured **before** the attempt, and written from a `finally`.
2. **The success path leaked its whole payload into ordinary responses** once the directory
   was set, contradicting the design's own claim to leave the response untouched. That is a
   dict-merge accident, so this module returns nothing a response could be built from.
   `write` is called for its effect and its result is never consumed.

"Off by default is not the same as inert when on." Both directions are tested: unset writes
nothing, and set writes a complete record.

**What is not in a record: file contents.** Files reach a record as paths, byte counts,
token estimates and skip reasons -- everything about what the delegation was *given* -- but
not their text. The text is recoverable from the repository by path, it is the only bulky
part, and writing it would put every prefetched file on disk at rest indefinitely. The task
is written verbatim, because it exists nowhere else; that is a deliberate acceptance that
whoever sets this directory owns what lands in it.

**One file per dispatch, never an appended log.** `delegate_batch` runs its items
concurrently, so a batch has as many writers as items. Per-file sidesteps append atomicity
entirely rather than reasoning about it -- and the filesystem here may be `/mnt/c`, where
reasoning about it would be reasoning about the wrong one.

**Nothing here may raise into the dispatch path, and nothing may touch stdout.** A full
disk must not fail a delegation that already succeeded, and on stdio a stray `print`
corrupts every subsequent MCP message (`server.py` owns that rule). Every failure is
swallowed to `logging`, which the entrypoint has already pointed at stderr.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .admission import AdmissionLease
    from .config import Config
    from .context import Prefetch
    from .loop import AgenticDispatch, Dispatch
    from .registry import ModelEntry

log = logging.getLogger(__name__)

# Anything outside this is replaced in a filename. An agent name reaches us from a file on
# disk, and a name is not a promise about path separators.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Enough to make two records written in the same millisecond by one batch distinct.
_COUNTER = {"n": 0}


def _rate(tokens: int | None, ms: int | None) -> float | None:
    """Tokens per second, or None when either half is missing or the interval is zero."""
    if not tokens or not ms or ms <= 0:
        return None
    return round(tokens / (ms / 1000), 1)


class Stream:
    """The same dispatch, written as it happens rather than once it is over.

    `write` below produces the record an operator audits afterwards. This produces the one
    a person watches during, which is a different question and cannot be answered by the
    same file: a record that exists only once the work is finished cannot say whether the
    work is stuck. Both are written; neither is derived from the other, because the stream
    must survive a dispatch that never reaches its end.

    Append-only, one JSON object per line, flushed per line. A reader tailing the file
    therefore sees a turn the moment it lands, and a half-written line is never possible
    for it to parse. Every method swallows its own errors for the reason `write` does: a
    transcript is an operator convenience and must never be able to fail a delegation.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._broken = False

    def _put(self, event: dict[str, Any]) -> None:
        if self._broken:
            return
        try:
            # os.open with an explicit mode, not Path.open: the mode has to be applied
            # by the creating syscall. A chmod afterwards leaves a window in which the
            # file exists at whatever the umask allowed, and since ADR-0043 this stream
            # carries full model replies. umask can only clear bits, so 0o600 is a
            # ceiling rather than a target.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with open(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
        except OSError:
            # Once, not per turn: a directory that cannot be written to will not start
            # working mid-delegation, and a failing write per turn is its own problem.
            self._broken = True

    def start(  # noqa: PLR0913 -- one head of one stream, and every field of it is a
        # separate fact about the call. Grouping them into an object would put the
        # shape of the event in two places.
        self, *, tool: str, task: str, agent: str | None,
        model_key: str | None, effort: str | None,
        tools: Iterable[str] = (), prefetched: Prefetch | None = None,
    ) -> None:
        """The head of the stream: which call this was, and what it was given.

        `tool` is the tool the caller actually invoked, and `tools` is what that call
        resolved to -- empty for a one-shot. Both are needed: `delegate_readonly` and
        `delegate(allowed_tools=[])` run the identical path, so the shape does not say
        which was called, and `delegate` alone does not say whether a loop ran.

        The files are here rather than only in the record because the record is written
        when the work is over. A reader asking "what is this delegation chewing on" is
        asking while it runs, which is the one moment the record cannot answer.
        """
        self._put({
            "t": "start", "at": datetime.now(UTC).isoformat(), "tool": tool,
            "task": task, "agent": agent, "model_key": model_key, "effort": effort,
            "tools": sorted(tools),
            **_files(prefetched),
        })

    def turn(self, diagnostic: Any, text: str, *, ms: int | None = None,
             backend_ms: int | None = None) -> None:
        """One completed turn, including what the model actually said in it.

        The text is here and not in `write`'s record on purpose. ADR-0039 excluded file
        *bodies* because they are bulky and recoverable from the repository by path; a
        reply is neither, and is in the same category as the task string that ADR already
        writes verbatim -- it exists nowhere else. See ADR-0043.
        """
        self._put({
            "t": "turn", "at": datetime.now(UTC).isoformat(),
            "turn": getattr(diagnostic, "turn", None),
            "input_tokens": getattr(diagnostic, "input_tokens", None),
            "output_tokens": getattr(diagnostic, "output_tokens", None),
            "effort": getattr(diagnostic, "effort", None),
            "attempts": getattr(diagnostic, "attempts", None),
            "tool_calls": [
                {"name": name, "outcome": outcome}
                for name, outcome in getattr(diagnostic, "tool_calls", ()) or ()
            ],
            "ms": ms,
            "backend_ms": backend_ms,
            # Decode rate, over the backend call alone. `ms` is the turn's wall clock and
            # includes tool execution, so dividing by it would report the cluster as
            # slower than it is. Both are kept because they answer different questions:
            # is the cluster slow, and is this delegation making progress.
            "out_tok_s": _rate(getattr(diagnostic, "output_tokens", None), backend_ms),
            "text": text,
        })

    def alive(self, *, elapsed_seconds: float, of_seconds: int) -> None:
        """A one-shot is still running. The only event that reports no work done.

        Every other event marks something that happened. This one exists because on the
        one-shot path nothing happens between `start` and `end` -- one backend call, no
        turns -- so a delegation that is working perfectly writes nothing for as long as
        it takes, and a reader cannot tell it from a delegation whose server was killed.

        It carries elapsed and the deadline it is elapsed against, and deliberately not a
        description of what the model is doing: there is no streaming (ADR-0018), so the
        server genuinely does not know. Reporting a guess would be worse than reporting
        the two numbers it actually has.
        """
        self._put({
            "t": "alive", "at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 3), "of_seconds": of_seconds,
        })

    def end(  # noqa: PLR0913 -- one event's fields, all keyword-only
            self, *, ok: bool, turns: int | None, elapsed_seconds: float,
            output_tokens: int | None = None, backend_ms: int | None = None,
            error: str | None = None) -> None:
        """The totals, which are a different figure from any turn's rate.

        `out_tok_s` here is over summed backend time across turns, so it is the rate the
        cluster actually sustained for this delegation. `elapsed_seconds` is everything,
        including waiting for an admission slot -- the gap between the two is what the
        delegation spent not generating.
        """
        self._put({
            "t": "end", "at": datetime.now(UTC).isoformat(), "ok": ok,
            "turns": turns, "elapsed_seconds": round(elapsed_seconds, 3),
            "output_tokens": output_tokens,
            "backend_ms": backend_ms,
            "out_tok_s": _rate(output_tokens, backend_ms),
            **({"error": error} if error else {}),
        })


def open_stream(cfg: Config, agent_name: str | None) -> Stream | None:
    """A stream for this dispatch, or None when transcripts are switched off."""
    if not enabled(cfg):
        return None
    try:
        directory = Path(os.path.expanduser(cfg.transcript_dir.strip()))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _COUNTER["n"] += 1
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%f")[:-3]
        return Stream(directory / f"{stamp}-{_COUNTER['n']:04d}-{_slug(agent_name)}.jsonl")
    except OSError:
        return None


def enabled(cfg: Config) -> bool:
    """Whether a transcript is being written at all.

    The caller needs this before dispatching, not only after: per-turn diagnostics are
    only *recorded* when the loop is told to record them, so a transcript has to ask for
    them itself rather than hope the caller did.
    """
    return bool(cfg.transcript_dir.strip())


def _slug(name: str | None) -> str:
    if not name:
        # Not "unknown". A delegation with no agent is an ordinary `delegate` call, and
        # naming that the same as one whose identity was lost is what made the upstream
        # bug invisible -- the records it broke read exactly like the records it did not.
        return "no-agent"
    return _UNSAFE.sub("-", name).strip("-") or "no-agent"


def _files(prefetched: Prefetch | None) -> dict[str, Any]:
    if prefetched is None:
        return {}
    account = prefetched.accounting()
    return {
        # Paths and cost, never text. See the module docstring.
        "files_read": account["files_read"],
        "files_skipped": account["files_skipped"],
        "prefetch_tokens": account["prefetch_tokens"],
        "prefetch_budget": account["prefetch_budget"],
    }


def _usage(dispatched: Dispatch | AgenticDispatch | None) -> dict[str, Any]:
    """Real token usage as the backend reported it, not the estimate admission used.

    The estimate is a guess made before the work; this is what the work cost. Summing
    these across records is the only way to answer what the local cluster has actually
    spent, so an estimate standing in for one here would quietly poison that total.
    """
    if dispatched is None:
        return {}
    response = dispatched.response
    return {
        "model": response.model,
        "finish_reason": response.finish_reason,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "effort": dispatched.effort,
        "attempts": dispatched.attempts,
        "reasoning_exhausted": dispatched.reasoning_exhausted,
        "answer_chars": len(response.text),
        "empty_response": response.text == "",
    }


def _ledger(dispatched: Dispatch | AgenticDispatch | None) -> dict[str, Any]:
    """The loop's counters. ADR-0007: what the server watched, not what was claimed."""
    if dispatched is None or not hasattr(dispatched, "turns"):
        return {}
    return {
        "turns": dispatched.turns,
        "tool_calls": dispatched.tool_calls,
        "tool_errors": dispatched.tool_errors,
        "tool_calls_deduplicated": dispatched.deduped,
        "tool_results_evicted": dispatched.evicted,
        "hit_turn_limit": dispatched.hit_turn_limit,
        "bash_calls": dispatched.bash_calls,
        "bash_failures": dispatched.bash_failures,
        "last_bash_exit": dispatched.last_bash_exit,
        "overflow_tightened_at": dispatched.overflow_tightened_at,
        "overflow_nudged_at": dispatched.overflow_nudged_at,
        "per_turn": [
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


def write(  # noqa: PLR0913 -- one record's worth of facts, from four different scopes
    cfg: Config,
    *,
    agent_name: str | None,
    entry: ModelEntry | None,
    task: str,
    workdir: str | None,
    prefetched: Prefetch | None,
    lease: AdmissionLease | None,
    dispatched: Dispatch | AgenticDispatch | None,
    error: BaseException | None,
    started: float,
    tool: str = "delegate",
    tools: Iterable[str] = (),
) -> None:
    """Write one record. Never raises, never returns anything a response could carry.

    `agent_name` and `entry` are passed rather than derived so the failure path names the
    same delegation the success path would have. Deriving either from `dispatched` would
    reintroduce bug 1 exactly: on a failure there is no `dispatched` to derive from.
    """
    if not enabled(cfg):
        return
    try:
        directory = Path(os.path.expanduser(cfg.transcript_dir.strip()))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

        _COUNTER["n"] += 1
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%f")[:-3]
        agent = _slug(agent_name)
        path = directory / f"{stamp}-{_COUNTER['n']:04d}-{agent}.json"

        record: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            # Which of the four delegating tools this was, and what it resolved to. Until
            # these were recorded, a `delegate_readonly` call, a `delegate_batch` item and
            # a plain `delegate` all wrote the identical record, so a directory of them
            # could not be counted by kind -- and the totalling is what the records exist
            # for.
            "tool": tool,
            "tools": sorted(tools),
            "agent": agent_name,
            "model_key": entry.key if entry else None,
            "served_model_id": entry.served_model_id if entry else None,
            "task": task,
            "workdir": workdir,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "ok": error is None,
            **_files(prefetched),
            **_usage(dispatched),
            **_ledger(dispatched),
        }
        if lease is not None:
            record["admission"] = {
                "estimated_tokens": lease.tokens,
                "large_prefill": lease.is_large,
                "waited_seconds": round(lease.waited, 3),
            }
        if error is not None:
            record["error"] = str(error)
            record["error_type"] = type(error).__name__

        # Same reason as the stream: created at 0o600 rather than chmod-ed into it.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, indent=2, default=str, ensure_ascii=False))
    except Exception:
        # Deliberately broad, and deliberately silent to the caller. The delegation has
        # already done its work; losing its record is worth strictly less than failing it,
        # and there is no failure here an operator could not also see in the empty
        # directory. Never to stdout: that is the MCP wire.
        log.warning("could not write a dispatch transcript", exc_info=True)
