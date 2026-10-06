"""One record per dispatch, written for the operator rather than for the caller. ADR-0024.

**Independent of the caller-facing `diagnostics` flag.** What an operator can audit must
not depend on what the calling session thought to ask for: a delegation that behaves
strangely is usually one nobody suspected, so the record has to exist already.

Two properties are acceptance criteria:

1. **A failure path keeps the agent name.** The identity is in scope only at the top of
   `run_delegation`; deeper in, a `Delegation` carries the task but not the agent's
   *name*. So the record is built from values captured **before** the attempt, and
   written from a `finally`.
2. **Nothing leaks into the response.** This module returns nothing a response could be
   built from, so a dict merge cannot put the record in one: `write` is called for its
   effect and its result is never consumed.

Off by default is not the same as inert when on, so both directions are tested: unset
writes nothing, and set writes a complete record.

**Not in a record: file contents.** Files appear as paths, byte counts, token estimates and
skip reasons, but not their text: it is recoverable by path, it is the only bulky part, and
writing it would keep every prefetched file on disk indefinitely. The task is written
verbatim, since it exists nowhere else; whoever sets this directory owns what lands in it.

**One file per dispatch, never an appended log.** Delegations run concurrently, and one
file each sidesteps append atomicity rather than reasoning about it -- on a filesystem that
may be `/mnt/c`, where that reasoning would be about the wrong one.

**Nothing here may raise into the dispatch path, or touch stdout.** A full disk must not
fail a delegation that succeeded, and on stdio a stray `print` corrupts every later MCP
message (`server.py` owns that rule). Every failure goes to `logging`, which the entrypoint
points at stderr.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, UTC
from hashlib import blake2s
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from .backends.base import answer_of as _answer_of
from .backends.base import duplicate_line_share
# The server's one notion of "this process". The module, not the name, so the call below
# reads through `slots` when it runs: a bound alias would freeze a second definition of
# process identity at import, which is what the import exists to avoid.
from . import slots
from .wsl import UntranslatablePath, to_local

if TYPE_CHECKING:
    from .admission import AdmissionLease
    from .config import Config
    from .context import Prefetch
    from .loop import AgenticDispatch, Dispatch
    from .registry import ModelEntry

log = logging.getLogger(__name__)

# Written on `start`; `transcript.schema.json` beside this file defines it and moves with
# it. Minor for an addition, major for a removal, rename or changed meaning (ADR-0111).
FORMAT = "1.8"

# The fixed values `end.ended` may hold, so a reader can branch on the ending rather than
# parse the words in `error`. Kept beside FORMAT because it is the same contract.
ENDINGS = ("finished", "stopped", "queue_timeout", "deadline", "stalled", "error")

# Anything outside this is replaced in a filename. An agent name reaches us from a file on
# disk, and a name is not a promise about path separators.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Enough to make two records written in the same millisecond by concurrent calls distinct.
_COUNTER = {"n": 0}

# Not enough alone, being per *process*: two processes both start at 0001, so a
# same-millisecond, same-agent pair across them would share a filename, and the record is
# written with O_TRUNC, so the second would clobber the first. `run` makes cross-process
# fan-out ordinary. The process token below supplies the rest, read once: a process keeps
# its identity, and deriving it per dispatch would invite two builders to differ.


def _process_token() -> str:
    """A short, filename-safe token that is this process and no other.

    From `slots._identity`, not a pid, uuid or random suffix invented here: that is
    already the server's answer to "which process is this" -- pid plus its incarnation, so
    a reused pid is a different process -- and a second answer is a second thing to keep
    true.

    Hashed, because the identity is spelt with a colon, which a filename should not hold.
    Six hex characters: fixed-width, so it cannot disturb the timestamp ordering before it,
    and wide enough that two concurrent processes will not collide in practice.
    """
    return blake2s(slots._identity().encode("utf-8"), digest_size=3).hexdigest()


_PROCESS = _process_token()


def _rate(tokens: int | None, ms: int | None) -> float | None:
    """Tokens per second, or None when either half is missing or the interval is zero."""
    if not tokens or not ms or ms <= 0:
        return None
    return round(tokens / (ms / 1000), 1)


def _ms(seconds: float | None) -> int | None:
    """Seconds as whole milliseconds, keeping `None` as `None` rather than as zero.

    `_rate` treats a zero interval as unmeasurable, so a sub-millisecond span rounded to
    0 reports no rate, as intended: an interval too short to measure supports no
    throughput figure.
    """
    return None if seconds is None else int(seconds * 1000)


class Stream:
    """The same dispatch, written as it happens rather than once it is over.

    `write` produces the record an operator audits afterwards; this is the one a person
    watches during. One file cannot serve both, because a record that exists only once the
    work finishes cannot say whether it is stuck. Neither derives from the other, because
    the stream must survive a dispatch that never reaches its end.

    Append-only, one JSON object per line, flushed per line, so a reader tailing it sees a
    turn the moment it lands and never parses a half-written line. Every method swallows
    its own errors, as `write` does: a transcript must never fail a delegation.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._broken = False
        # Turns whose calls a `tools` event already named, so their `turn` event can say so.
        self._announced: set[int] = set()

    def _put(self, event: dict[str, Any]) -> None:
        if self._broken:
            return
        try:
            # os.open with an explicit mode, not Path.open: the creating syscall must
            # apply it, since a chmod afterwards leaves a window at whatever the umask
            # allowed, and this stream carries full model replies (ADR-0043). umask only
            # clears bits, so 0o600 is a ceiling, not a target.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with open(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
        except OSError:
            # Once, not per turn: an unwritable directory will not start working
            # mid-delegation, and a failing write per turn is its own problem.
            self._broken = True

    def start(  # noqa: PLR0913 -- each field is a separate fact about the call, and an
        # object grouping them would put the event's shape in two places.
        self, *, tool: str, task: str, title: str | None = None,
        agent: str | None,
        model_key: str | None, effort: str | None,
        tools: Iterable[str] = (), prefetched: Prefetch | None = None,
        max_turns: int | None = None,
        workspace: str | None = None,
    ) -> None:
        """The head of the stream: which call this was, and what it was given.

        `tool` is what the caller invoked and `tools` what it resolved to, empty for a
        one-shot. Both are needed: `delegate_readonly` and `delegate(allowed_tools=[])`
        run the same path, and `delegate` alone does not say whether a loop ran.

        The files are here, not only in the record, because the record is written when
        the work is over, and "what is this delegation chewing on" is asked while it runs.
        """
        self._put({
            "t": "start", "at": datetime.now(UTC).isoformat(), "format": FORMAT, "tool": tool,
            "task": task, "agent": agent, "model_key": model_key, "effort": effort,
            # Beside `effort`, the same kind of fact: a resolved setting this run is held
            # to. None for a one-shot, which runs no loop.
            "max_turns": max_turns,
            **({"title": title} if title else {}),
            **({"workspace": workspace} if workspace else {}),
            "tools": sorted(tools),
            **_files(prefetched),
        })

    def turn(self, diagnostic: Any, text: str, *, ms: int | None = None,
             backend_ms: int | None = None, of_turns: int | None = None) -> None:
        """One completed turn, including what the model actually said in it.

        The text is here, not in `write`'s record. File *bodies* are excluded as bulky and
        recoverable by path (ADR-0039); a reply is neither, and like the task string it
        exists nowhere else (ADR-0043).
        """
        retries = getattr(diagnostic, "retries", ()) or ()
        self._put({
            "t": "turn", "at": datetime.now(UTC).isoformat(),
            "turn": getattr(diagnostic, "turn", None),
            # A `tools` event already named this turn's calls, so a reader shows results.
            "announced": getattr(diagnostic, "turn", None) in self._announced,
            # On every turn, not only the head: a reader deep in a long transcript cannot
            # see the header, and "turn 3" without "of 10" cannot be acted on.
            "of_turns": of_turns,
            "input_tokens": getattr(diagnostic, "input_tokens", None),
            "output_tokens": getattr(diagnostic, "output_tokens", None),
            "cached_tokens": getattr(diagnostic, "cached_tokens", None),
            # Beside `cached_tokens`, the only thing that explains a collapse in it: a
            # watcher sees the prefix cache fall over and the eviction that did it
            # (ADR-0056).
            "tool_results_evicted": getattr(diagnostic, "evicted", None),
            "effort": getattr(diagnostic, "effort", None),
            "attempts": getattr(diagnostic, "attempts", None),
            # Rendered by the record itself, so a field added to it reaches every site --
            # including this one, which is watched rather than asserted on and would be
            # the one to drift.
            "tool_calls": [
                call.as_json() for call in getattr(diagnostic, "tool_calls", ()) or ()
            ],
            "ms": ms,
            "backend_ms": backend_ms,
            # Where `backend_ms` went, summed over this turn's attempts: queueing and
            # prefill, then decode. Tool time is the sum of the calls' own `ms` above, never
            # `ms - backend_ms`, which also holds the dispatch's bookkeeping; a field for it
            # would be one more number able to disagree with the calls it sums.
            "prefill_seconds": getattr(diagnostic, "prefill_seconds", None),
            "decode_seconds": getattr(diagnostic, "decode_seconds", None),
            # How long the model reasoned before answering, measured per response. Always
            # written, null when it did not reason or could not be measured.
            "reasoning_seconds": getattr(diagnostic, "reasoning_seconds", None),
            # Decode rate over the *answering attempt's* decode span, not `backend_ms`,
            # which spans every attempt and retry wait. `output_tokens` comes from the one
            # attempt that answered (ADR-0014), so dividing by all of them would report a
            # retried turn at several times too low a rate. Falls back to `backend_ms` for
            # an adapter that cannot time its decoding.
            "out_tok_s": _rate(
                getattr(diagnostic, "output_tokens", None),
                _ms(getattr(diagnostic, "answered_decode_seconds", None)) or backend_ms,
            ),
            # Per turn, where the loop lives: a turn repeating itself never ends, so
            # `max_turns` cannot reach it, and the per-dispatch figure arrives only once
            # something else stops it.
            "duplicate_line_share": duplicate_line_share(text or ""),
            **({"retries": [r.as_json() for r in retries]} if retries else {}),
            "text": text,
        })

    def priced(  # noqa: PLR0913 -- one event's fields, all keyword-only
        self, *, turn: int | None, effort: str | None, max_tokens: int | None,
        budget_ceiling: int | None, decode_rate: float | None,
        requests_running: float | None, rate_source: str | None = None,
        expected_concurrency: int | None = None,
        temperature: float | None = None, top_p: float | None = None,
        of_turns: int | None = None,
        max_tokens_sent: int | None = None,
    ) -> None:
        """What a turn was allowed, and what that allowance was calculated from.

        Written *before* the turn, which is the point. A `turn` event is written when a
        turn completes, so the turns most needing explanation -- killed at a deadline
        having finished nothing -- would otherwise record no figures at all.

        `budget_ceiling` is written even when `None`: "no cap was applied" is the most
        incriminating thing this record can say, and an absent key reads as "not
        recorded". `requests_running` is here because the rate can be a since-boot mean
        over every concurrency regime, which without the load it was read against can
        only be believed, not checked.
        """
        self._put({
            "t": "priced", "at": datetime.now(UTC).isoformat(), "turn": turn,
            "effort": effort, "max_tokens": max_tokens,
            # What the first attempt sent; `max_tokens` is the caller's argument, often None.
            "max_tokens_sent": max_tokens_sent,
            "budget_ceiling": budget_ceiling, "decode_rate": decode_rate,
            "requests_running": requests_running,
            # Where the rate came from, and the concurrency it was asked for, so the
            # ceiling can be argued with: a remembered rate and a since-boot blend are
            # different claims.
            "rate_source": rate_source,
            "expected_concurrency": expected_concurrency,
            # The sampling this turn used, for the reason `effort` is here: `config.py`
            # read afterwards says what a setting holds *now*, not what the turn used.
            "temperature": temperature,
            "top_p": top_p,
            # The budget this turn is one of: `turn` alone does not say whether the end
            # is near, which decides whether a long run needs a higher cap.
            "of_turns": of_turns,
        })

    def tools(self, *, turn: int, of_turns: int | None,
              tool_calls: list[dict[str, Any]]) -> None:
        """The calls a turn is about to run, named before any of them does.

        The `turn` event lands only once they have all finished, so without this a long
        command is visible only as a heartbeat. Arguments come capped as the turn record
        caps them; the results stay in the `turn` event, which says it was announced.
        """
        self._announced.add(turn)
        self._put({
            "t": "tools", "at": datetime.now(UTC).isoformat(), "turn": turn,
            "of_turns": of_turns, "tool_calls": tool_calls,
        })

    def partial(self, *, turn: int, reasoning: str, answer: str,
                final: bool = False) -> None:
        """The reply's text since the last `partial`, while the turn is still generating.

        For a person watching; the `turn` event that follows carries the whole reply and is
        the one to keep. A retried attempt's text is here too, since it was what arrived.
        `final` is true on the turn's last partial, written just before its `turn` event:
        joined in order, a turn's partials then hold all of its reasoning and reply text
        (format 1.8). Only a final partial carries the key, so earlier events are unchanged.
        """
        event: dict[str, Any] = {
            "t": "partial", "at": datetime.now(UTC).isoformat(), "turn": turn,
            "reasoning": reasoning, "answer": answer,
        }
        if final:
            event["final"] = True
        self._put(event)

    def question(self, questions: list[str]) -> None:
        """A run paused on its caller: the question it asked, before anyone answers it.

        Written by the `ask` callback the moment the loop hands the questions over, so a
        watcher sees the pause -- not only the tool call that triggered it -- and, paired
        with `answer`, how long the caller took.
        """
        self._put({
            "t": "question", "at": datetime.now(UTC).isoformat(),
            "questions": questions,
        })

    def answer(self, text: str, waited_seconds: float, best_reading: bool) -> None:
        """The caller's reply, and how long the run was held waiting for it.

        `best_reading` is true when the caller left the choice to the model, in which case
        `text` is the sentence the `answer` tool hands over rather than anything the caller
        typed; a reader must be able to tell the two apart.
        """
        event: dict[str, Any] = {
            "t": "answer", "at": datetime.now(UTC).isoformat(),
            "waited_seconds": round(waited_seconds, 1),
            "text": text,
        }
        if best_reading:
            event["best_reading"] = True
        self._put(event)

    def waiting(self, *, waited_seconds: float, of_seconds: int) -> None:
        """Still queued at the admission gate, having reached no backend at all.

        Written from the tick that resets the client's idle timer during a wait. Without
        it a queued delegation and a killed one leave identical files, both silent and
        unfinished.

        Only the server can tell them apart: the file says nothing either way, and a pid
        would mean something only on the machine that wrote it, which is not where these
        are read. So the fact is recorded when known, not inferred later from silence.
        """
        self._put({
            "t": "waiting", "at": datetime.now(UTC).isoformat(),
            "waited_seconds": round(waited_seconds, 3), "of_seconds": of_seconds,
        })

    def alive(self, *, elapsed_seconds: float, of_seconds: int,  # noqa: PLR0913 -- one event's fields, all keyword-only
              ends_in_seconds: float | None = None,
              chunks_seen: int = 0, reasoning_chunks: int = 0,
              since_chunk_seconds: float | None = None,
              running: tuple[dict[str, Any], ...] = ()) -> None:
        """A delegation is still running, and what it is doing (ADR-0072).

        Every other event marks something that happened. On the one-shot path nothing
        happens between `start` and `end` -- one backend call, no turns -- so without this
        a delegation working perfectly writes nothing, indistinguishable from one whose
        server was killed.

        `chunks_seen` counts frames that carried generated output and `since_chunk_seconds`
        is how long since the last, which together separate a delegation that is
        producing from one gone quiet. `reasoning_chunks` splits the count into thinking
        and answering, which the total alone cannot. `answer_chunks` is derived, not
        recorded, so the three cannot disagree.

        Chunks, not tokens, and named so: a frame usually carries one token here but is
        not promised to, and the only real count arrives in the final usage frame.

        `running` names the tool calls in flight, which is why nothing is arriving while
        they run; written only then, so a beat with none keeps its old shape.
        """
        event: dict[str, Any] = {
            "t": "alive", "at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 3), "of_seconds": of_seconds,
            # Beside `of_seconds`, the delegation ceiling, which stays because transcripts
            # already carry it. This is the one a reader needs -- how long until the
            # tightest deadline fires -- since the ceiling is least likely to end the run.
            "ends_in_seconds": (
                None if ends_in_seconds is None else round(ends_in_seconds, 3)
            ),
            "chunks_seen": chunks_seen,
            "reasoning_chunks": reasoning_chunks,
            "answer_chunks": chunks_seen - reasoning_chunks,
            "since_chunk_seconds": (
                None if since_chunk_seconds is None else round(since_chunk_seconds, 3)
            ),
        }
        if running:
            event["running"] = [dict(call) for call in running]
        self._put(event)

    def end(  # noqa: PLR0913 -- one event's fields, all keyword-only
            self, *, ok: bool, turns: int | None, elapsed_seconds: float,
            ended: str,
            output_tokens: int | None = None, cached_tokens: int | None = None,
            backend_ms: int | None = None, error: str | None = None,
            finish_reason: str | None = None, max_turns: int | None = None,
            input_tokens: int | None = None,
            prefill_seconds: float | None = None,
            decode_seconds: float | None = None,
            tool_seconds: float | None = None,
            dispatched: Dispatch | AgenticDispatch | None = None) -> None:
        """The totals, which are a different figure from any turn's rate.

        `out_tok_s` here is over backend time summed across turns: the rate the cluster
        sustained for this delegation. `elapsed_seconds` is everything, the admission wait
        included, so the gap between them is time spent not generating.

        `finish_reason` is carried verbatim so a reader can see a reply was cut off. `ok`
        is true for a truncated dispatch -- nothing failed -- so without it the stream
        would say "done" about a reply that stopped mid-sentence.

        `dispatched` is taken whole, so the counts are `_ledger`'s, the same the record
        carries: counting tool calls a second way is how two files come to disagree about
        one delegation.
        """
        # A typo in `ended` must reach a reader as a broken file, not silently as a new
        # ending, so refuse it here.
        if ended not in ENDINGS:
            raise ValueError(f"unknown ended value {ended!r}")
        # `.get`, because a one-shot ran no loop and `_ledger` is empty for it. The keys
        # are emitted anyway: as an absent key, "no loop" and "not recorded" read alike.
        ledger = _ledger(dispatched)
        self._put({
            "t": "end", "at": datetime.now(UTC).isoformat(), "ok": ok,
            "ended": ended,
            "turns": turns,
            # What `turns` could reach: only the pair says whether the cap ended the run.
            "max_turns": max_turns,
            "elapsed_seconds": round(elapsed_seconds, 3),
            # Both halves, so the last line a reader sees says what the delegation cost.
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            **({"finish_reason": finish_reason} if finish_reason else {}),
            # Summed over the delegation's turns: what the cluster did not recompute.
            "cached_tokens": cached_tokens,
            "backend_ms": backend_ms,
            "out_tok_s": _rate(output_tokens, backend_ms),
            # The turn events' split, summed. With `elapsed_seconds` these account for
            # the delegation -- prefill, decode, tools, admission wait -- so a reader can
            # say where a slow run spent its time.
            "prefill_seconds": (
                None if prefill_seconds is None else round(prefill_seconds, 3)
            ),
            "decode_seconds": (
                None if decode_seconds is None else round(decode_seconds, 3)
            ),
            # Carried, unlike on a turn event: here `elapsed_seconds - backend_ms` is tool
            # time *plus the admission wait*, so subtracting the obvious pair would report
            # a queued delegation as an expensive toolset. Summed on the server's own
            # clock, from when the slot was granted.
            "tool_seconds": (
                None if tool_seconds is None else round(tool_seconds, 3)
            ),
            # What the loop did, for a reader following the stream, which is what a run
            # summary follows; the record arrives only once the work is over.
            "tool_calls": ledger.get("tool_calls"),
            "tool_errors": ledger.get("tool_errors"),
            "bash_calls": ledger.get("bash_calls"),
            "bash_failures": ledger.get("bash_failures"),
            "failed_calls": ledger.get("failed_calls"),
            **({"error": error} if error else {}),
        })


def directory(cfg: Config) -> Path:
    """Where records go: the one place `transcript_dir` becomes a path.

    Translated with `to_local`, because the setting is written on the Windows side and
    read inside WSL. Untranslated, a Windows path is a
    legal *single-component* POSIX filename, so `C:\\Users\\me\\t` is relative, and
    `mkdir(parents=True)` would silently make a directory of that literal name under the
    server's working directory, passing every check meant to catch the mistake.

    Raises `UntranslatablePath` for a UNC share, which has no mount point here. Callers on
    the dispatch path must swallow it -- this module may not raise into a delegation.
    """
    return Path(os.path.expanduser(to_local(cfg.transcript_dir)))


def open_stream(cfg: Config, agent_name: str | None) -> Stream | None:
    """A stream for this dispatch, or None when transcripts are switched off."""
    if not enabled(cfg):
        return None
    try:
        target = directory(cfg)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        return Stream(target / _filename(agent_name, ".jsonl"))
    except (OSError, UntranslatablePath):
        return None


def _filename(agent_name: str | None, suffix: str) -> str:
    """The name one dispatch's stream or record is written under.

    Three discriminators, none redundant. The stamp orders the directory and pairs a
    stream with its record. The counter separates two dispatches *this* process began in
    one millisecond. `_PROCESS` separates two processes, which the counter cannot, both
    having started at one.

    One function, not the f-string twice: the stream and the record must stay the same
    shape, and two copies is how one gets a new discriminator and the other does not.
    """
    _COUNTER["n"] += 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%f")[:-3]
    return f"{stamp}-{_PROCESS}-{_COUNTER['n']:04d}-{_slug(agent_name)}{suffix}"


def enabled(cfg: Config) -> bool:
    """Whether a transcript is being written at all.

    Needed before dispatching, not only after: the loop records per-turn diagnostics only
    when told to, so a transcript must ask for them rather than hope the caller did.
    """
    return bool(cfg.transcript_dir.strip())


def _slug(name: str | None) -> str:
    if not name:
        # Not "unknown". A delegation with no agent is an ordinary `delegate` call, and
        # naming it like one whose identity was lost would make that loss invisible.
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

    The estimate is a guess made before the work; this is what the work cost. Summing the
    `total_*` fields across records is the only way to answer what the cluster has spent:
    they describe every turn, where `input_tokens`, `output_tokens` and `cached_tokens`
    describe only the turn that answered (ADR-0058). An estimate, or a per-turn figure
    under a lifetime name, would quietly poison that total.

    A one-shot `Dispatch` carries no totals, and its single turn *is* the run, so there
    the answering turn's figures are the totals.

    `total_cached_tokens` says what the cluster did not have to compute, where the totals
    say what was sent. `None` means the endpoint reported no such field, which is not a
    zero -- do not sum it as one.
    """
    if dispatched is None:
        return {}
    response = dispatched.response
    total_input = getattr(dispatched, "total_input_tokens", None)
    total_output = getattr(dispatched, "total_output_tokens", None)
    total_cached = getattr(dispatched, "total_cached_tokens", None)
    if total_input is None:
        # A `None` test, not a type check: exactly the one-shot `Dispatch` lacks the
        # attributes, and an `AgenticDispatch` always has them, defaulting to a real zero.
        total_input, total_output, total_cached = (
            response.input_tokens, response.output_tokens, response.cached_tokens
        )
    return {
        "model": response.model,
        "finish_reason": response.finish_reason,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cached_tokens": response.cached_tokens,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cached_tokens": total_cached,
        "total_tokens": response.total_tokens,
        "stop_reason": response.stop_reason,
        "system_fingerprint": response.system_fingerprint,
        "effort": dispatched.effort,
        "attempts": dispatched.attempts,
        "reasoning_exhausted": dispatched.reasoning_exhausted,
        "answer_chars": len(response.text),
        # From `answer_of`, the helper the result dict uses, so the record and the reply
        # cannot disagree about whether anything came back. `response.text` alone would
        # call a reasoning-only answer empty.
        "empty_response": _answer_of(response)[0] == "",
        "answer_is_reasoning": _answer_of(response)[1],
        "reasoning_chars": len(response.thinking),
        # From the same `answer_of`, for the same reason.
        "duplicate_line_share": duplicate_line_share(_answer_of(response)[0]),
    }


def _ledger(dispatched: Dispatch | AgenticDispatch | None) -> dict[str, Any]:
    """The loop's counters. ADR-0007: what the server watched, not what was claimed."""
    if dispatched is None or not hasattr(dispatched, "turns"):
        return {}
    # Narrowed for the checker only; `hasattr` is the test so a double with `turns` passes.
    dispatched = cast("AgenticDispatch", dispatched)
    return {
        "turns": dispatched.turns,
        "tool_calls": dispatched.tool_calls,
        "tool_errors": dispatched.tool_errors,
        "tool_calls_deduplicated": dispatched.deduped,
        "tool_results_evicted": dispatched.evicted,
        "hit_turn_limit": dispatched.hit_turn_limit,
        "bash_calls": dispatched.bash_calls,
        "bash_failures": dispatched.bash_failures,
        "bash_masked_failures": dispatched.bash_masked_failures,
        "failed_calls": dispatched.failed_calls,
        "last_bash_exit": dispatched.last_bash_exit,
        "overflow_tightened_at": dispatched.overflow_tightened_at,
        "overflow_nudged_at": dispatched.overflow_nudged_at,
        "per_turn": [
            {
                "turn": t.turn,
                "input_tokens": t.input_tokens,
                "output_tokens": t.output_tokens,
                "cached_tokens": t.cached_tokens,
                "attempts": t.attempts,
                "effort": t.effort,
                "tool_results_evicted": t.evicted,
                "tool_calls": [c.as_json() for c in t.tool_calls],
                "retries": [r.as_json() for r in getattr(t, "retries", ()) or ()],
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

    `agent_name` and `entry` are passed, not derived, so the failure path names the same
    delegation the success path would: on a failure there is no `dispatched` to derive
    from (the module docstring's first criterion).
    """
    if not enabled(cfg):
        return
    try:
        target = directory(cfg)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)

        path = target / _filename(agent_name, ".json")

        record: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            # Which delegating tool this was and what it resolved to, so a directory of
            # records can be counted by kind, which is what they exist for.
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
        # Broad, and silent to the caller: the delegation has done its work, losing its
        # record costs less than failing it, and an operator sees any failure here in
        # the empty directory. Never to stdout: that is the MCP wire.
        log.warning("could not write a dispatch transcript", exc_info=True)
