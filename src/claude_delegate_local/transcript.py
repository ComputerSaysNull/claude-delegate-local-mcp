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

**One file per dispatch, never an appended log.** Delegations run concurrently, so at
any moment there are as many writers as calls in flight. Per-file sidesteps append atomicity
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
from hashlib import blake2s
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backends.base import answer_of as _answer_of
from .backends.base import duplicate_line_share
# The one notion of "this process" this server has. The module rather than the name, so
# the call below reads through `slots` at the moment it runs: a bound alias would freeze
# this module's idea of process identity at import, which is exactly the second
# definition the import exists to avoid.
from . import slots
from .wsl import UntranslatablePath, to_local

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

# Enough to make two records written in the same millisecond by concurrent calls distinct.
_COUNTER = {"n": 0}

# And not enough on its own, because it is per *process*: two processes both start at
# 0001, so a same-millisecond, same-agent pair across two of them produced one filename --
# and the record is written with O_TRUNC, so the second silently clobbered the first. The
# `run` subcommand makes cross-process fan-out ordinary, so that is live data loss rather
# than a hypothetical.
#
# The discriminator that supplies it is below, read once: a process does not change its
# identity, and re-deriving it per dispatch would only invite the two builders to derive
# it differently.


def _process_token() -> str:
    """A short, filename-safe token that is this process and no other.

    Derived from `slots._identity` rather than from a pid, a uuid or a random suffix
    invented here. That string is already this server's answer to "which process is
    this" -- pid plus the incarnation of that pid, so a reused pid is a different
    process -- and a second answer to the same question is a second thing to keep true.

    Hashed rather than used verbatim because the identity is spelt with a colon, which
    has no business in a filename. Six hex characters: fixed-width, so it cannot disturb
    the timestamp ordering it sits behind, and wide enough that a collision between two
    concurrent processes is not the failure mode anyone meets.
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

    `_rate` treats a zero interval as unmeasurable, so rounding a sub-millisecond span
    down to 0 declines to report a rate rather than dividing by it. That is the intended
    reading: an interval too short to measure cannot support a throughput figure.
    """
    return None if seconds is None else int(seconds * 1000)


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
        max_turns: int | None = None,
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
            # Beside `effort` because it is the same kind of fact: a resolved setting
            # this run will be held to, known before any of it happens. None for a
            # one-shot, which runs no loop and so has no budget to be held to.
            "max_turns": max_turns,
            "tools": sorted(tools),
            **_files(prefetched),
        })

    def turn(self, diagnostic: Any, text: str, *, ms: int | None = None,
             backend_ms: int | None = None, of_turns: int | None = None) -> None:
        """One completed turn, including what the model actually said in it.

        The text is here and not in `write`'s record on purpose. ADR-0039 excluded file
        *bodies* because they are bulky and recoverable from the repository by path; a
        reply is neither, and is in the same category as the task string that ADR already
        writes verbatim -- it exists nowhere else. See ADR-0043.
        """
        retries = getattr(diagnostic, "retries", ()) or ()
        self._put({
            "t": "turn", "at": datetime.now(UTC).isoformat(),
            "turn": getattr(diagnostic, "turn", None),
            # Repeated on every turn rather than left to the head of the stream: a reader
            # scrolling a long transcript is not looking at the header any more, and
            # "turn 3" without "of 10" is the half that cannot be acted on.
            "of_turns": of_turns,
            "input_tokens": getattr(diagnostic, "input_tokens", None),
            "output_tokens": getattr(diagnostic, "output_tokens", None),
            "cached_tokens": getattr(diagnostic, "cached_tokens", None),
            # Beside `cached_tokens` because it is the only thing that explains a collapse
            # in it. The final record carried this per turn and the live stream did not, so
            # someone watching a delegation could see the prefix cache fall over and not
            # see the eviction that did it -- the one field that would have made ADR-0056's
            # bug self-evident while it was happening.
            "tool_results_evicted": getattr(diagnostic, "evicted", None),
            "effort": getattr(diagnostic, "effort", None),
            "attempts": getattr(diagnostic, "attempts", None),
            # Rendered by the record itself rather than here. This was the third of three
            # sites each unpacking the pair by hand, so a field added to the record used to
            # mean an edit in three places -- and the one that drifted would have been this
            # one, because a live stream is watched and not asserted on.
            "tool_calls": [
                call.as_json() for call in getattr(diagnostic, "tool_calls", ()) or ()
            ],
            "ms": ms,
            "backend_ms": backend_ms,
            # Where `backend_ms` went, summed over this turn's attempts. Queueing and
            # prefill first, then decode; the remainder against `ms` is tool execution,
            # which is why no field carries that -- it is `ms - backend_ms` and adding it
            # would be a fourth number that can disagree with the other three.
            "prefill_seconds": getattr(diagnostic, "prefill_seconds", None),
            "decode_seconds": getattr(diagnostic, "decode_seconds", None),
            # Decode rate, over the *answering attempt's* decode span -- not `backend_ms`,
            # which spans every attempt and every retry wait between them. `output_tokens`
            # comes from the one attempt that answered (ADR-0014), so dividing it by all
            # of them reports a turn that returned empty and was sent again at several
            # times too low a rate: a recorded 7.6 tok/s was exactly that. Falls back to
            # `backend_ms` for an adapter that cannot time its own decoding, where the
            # whole call is the only interval there is.
            "out_tok_s": _rate(
                getattr(diagnostic, "output_tokens", None),
                _ms(getattr(diagnostic, "answered_decode_seconds", None)) or backend_ms,
            ),
            # Per turn, because that is where the loop lives: a turn repeating itself
            # never ends, so `max_turns` cannot reach it and the per-dispatch figure
            # arrives only if something else stopped it first.
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

        Written *before* the turn rather than with it, which is the whole point. A `turn`
        event is produced when a turn completes, so the turns that most need explaining --
        the ones killed at a deadline having finished nothing -- are exactly the ones that
        record no figures at all. Nine did in one session, and the diagnosis had to be
        rebuilt afterwards from a benchmark run separately.

        `budget_ceiling` is written even when it is `None`, because "no cap was applied"
        is the most incriminating thing this record can say and an absent key reads as
        "not recorded". `requests_running` is here for the same reason `decode_rate`
        alone would not do: the rate is a since-boot mean over every concurrency regime
        the engine has served, so without the load it was read against it cannot be
        checked afterwards, only believed.
        """
        self._put({
            "t": "priced", "at": datetime.now(UTC).isoformat(), "turn": turn,
            "effort": effort, "max_tokens": max_tokens,
            # What the first attempt sent; `max_tokens` is the caller's argument, often None.
            "max_tokens_sent": max_tokens_sent,
            "budget_ceiling": budget_ceiling, "decode_rate": decode_rate,
            "requests_running": requests_running,
            # Where the rate came from, and the concurrency it was asked for. Without
            # these the ceiling can be read but not argued with: a remembered rate and a
            # since-boot mean are different claims, and the second is a blend over every
            # regime the engine has served.
            "rate_source": rate_source,
            "expected_concurrency": expected_concurrency,
            # The sampling this turn was drawn at, for the reason `effort` is here: a
            # setting recovered from `config.py` afterwards is the value it holds *now*,
            # not the one the turn used. Five audit passes looped at temperature 0.2 and
            # no transcript said so, which is why the diagnosis took three sessions.
            "temperature": temperature,
            "top_p": top_p,
            # The resolved budget this turn is one of. `turn` alone says where a
            # delegation is and not whether that is near the end, which is the half that
            # decides whether the answer to a long run is to raise the cap.
            "of_turns": of_turns,
        })

    def waiting(self, *, waited_seconds: float, of_seconds: int) -> None:
        """Still queued at the admission gate, having reached no backend at all.

        Written from the same tick that resets the client's idle timer while a delegation
        waits. Without it a queued delegation and a killed one leave identical files --
        both silent, both unfinished -- and the viewer called the pair `quiet`, which is
        true of both and useful about neither.

        The server is the only thing that can tell them apart. A reader cannot: the file
        says nothing either way, and a pid in the stream would only mean something on the
        machine that wrote it, which is not where these are read. So the fact is recorded
        when it is known rather than inferred later from silence.
        """
        self._put({
            "t": "waiting", "at": datetime.now(UTC).isoformat(),
            "waited_seconds": round(waited_seconds, 3), "of_seconds": of_seconds,
        })

    def alive(self, *, elapsed_seconds: float, of_seconds: int,  # noqa: PLR0913 -- one event's fields, all keyword-only
              ends_in_seconds: float | None = None,
              chunks_seen: int = 0, reasoning_chunks: int = 0,
              since_chunk_seconds: float | None = None) -> None:
        """A delegation is still running, and -- since ADR-0072 -- what it is doing.

        Every other event marks something that happened. This one exists because on the
        one-shot path nothing happens between `start` and `end` -- one backend call, no
        turns -- so a delegation that is working perfectly writes nothing for as long as
        it takes, and a reader cannot tell it from a delegation whose server was killed.

        It once carried elapsed and its deadline and deliberately nothing about the model,
        because there was no streaming (ADR-0018) and the server genuinely did not know.
        It does now. `chunks_seen` counts frames that carried generated output and
        `since_chunk_seconds` is how long since the last, which together separate a
        delegation that is producing from one that has gone quiet -- the distinction the
        event was invented for and could not previously make.

        `reasoning_chunks` splits the count so a reader can tell thinking from answering,
        which the total alone cannot. `answer_chunks` is derived rather than recorded, so
        the three can never disagree: the total is what was counted and the split is what
        it was split into.

        Chunks rather than tokens, and named so. A frame usually carries one token on this
        stack and is not promised to, and the only real count arrives in the final usage
        frame. Reporting frames as tokens would be the guess this docstring used to refuse.
        """
        self._put({
            "t": "alive", "at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 3), "of_seconds": of_seconds,
            # Added rather than replacing `of_seconds`, which stays the delegation
            # ceiling: that is true, and transcripts already carry it. This is the one a
            # reader needs -- how long until the tightest deadline fires -- because the
            # ceiling is the deadline least likely to be what ends the run.
            "ends_in_seconds": (
                None if ends_in_seconds is None else round(ends_in_seconds, 3)
            ),
            "chunks_seen": chunks_seen,
            "reasoning_chunks": reasoning_chunks,
            "answer_chunks": chunks_seen - reasoning_chunks,
            "since_chunk_seconds": (
                None if since_chunk_seconds is None else round(since_chunk_seconds, 3)
            ),
        })

    def end(  # noqa: PLR0913 -- one event's fields, all keyword-only
            self, *, ok: bool, turns: int | None, elapsed_seconds: float,
            output_tokens: int | None = None, cached_tokens: int | None = None,
            backend_ms: int | None = None, error: str | None = None,
            finish_reason: str | None = None, max_turns: int | None = None,
            input_tokens: int | None = None,
            prefill_seconds: float | None = None,
            decode_seconds: float | None = None,
            tool_seconds: float | None = None,
            dispatched: Dispatch | AgenticDispatch | None = None) -> None:
        """The totals, which are a different figure from any turn's rate.

        `out_tok_s` here is over summed backend time across turns, so it is the rate the
        cluster actually sustained for this delegation. `elapsed_seconds` is everything,
        including waiting for an admission slot -- the gap between the two is what the
        delegation spent not generating.

        `finish_reason` is carried verbatim so a reader can see a reply was cut off rather
        than finished. `ok` is true for a truncated dispatch -- nothing failed -- so without
        this the stream says "done" about a reply that stopped mid-sentence, and the one
        state most worth spotting is the one it cannot show.

        `dispatched` is taken whole rather than as four counts, so the numbers here are
        `_ledger`'s -- the same ones the record carries. Counting tool calls a second way
        for the benefit of a second reader is how two files come to disagree about one
        delegation, and the stream is the file anything following a run reads.
        """
        # The counts, from the ledger the record already builds. `.get` rather than
        # indexing because a one-shot ran no loop and `_ledger` is empty for it, and the
        # keys are emitted anyway: "no loop" and "not recorded" read identically as an
        # absent key, and only one of them is true here.
        ledger = _ledger(dispatched)
        self._put({
            "t": "end", "at": datetime.now(UTC).isoformat(), "ok": ok,
            "turns": turns,
            # What `turns` was allowed to reach. Six turns and six of six turns are the
            # same run and different news, and only the pair says whether the cap ended it.
            "max_turns": max_turns,
            "elapsed_seconds": round(elapsed_seconds, 3),
            # Both halves. Every turn line above reports what it sent, and the summary
            # under them reported only what came back -- so the one line a reader is
            # left looking at was the one that could not say what the delegation cost.
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            **({"finish_reason": finish_reason} if finish_reason else {}),
            # Summed over the delegation's turns: what the cluster did not recompute.
            "cached_tokens": cached_tokens,
            "backend_ms": backend_ms,
            "out_tok_s": _rate(output_tokens, backend_ms),
            # The same split the turn events carry, summed over the run. With
            # `elapsed_seconds` above these account for the delegation: prefill, decode,
            # tool execution and the admission wait, and a reader can now say which of
            # them a slow run actually spent its time in rather than guess.
            "prefill_seconds": (
                None if prefill_seconds is None else round(prefill_seconds, 3)
            ),
            "decode_seconds": (
                None if decode_seconds is None else round(decode_seconds, 3)
            ),
            # Carried rather than left to be derived, unlike on a turn event. There
            # `ms - backend_ms` is tool time; here `elapsed_seconds - backend_ms` is tool
            # time *plus the admission wait*, which happens before any turn runs -- so a
            # reader subtracting the obvious pair would report a queued delegation as an
            # expensive toolset. The server sums it from its own clock, started when the
            # slot was granted.
            "tool_seconds": (
                None if tool_seconds is None else round(tool_seconds, 3)
            ),
            # What the loop did, for a reader following the stream. These reached only
            # the per-dispatch record, which is written once the work is over -- so the
            # file a run summary follows could see every turn and not one tool call.
            "tool_calls": ledger.get("tool_calls"),
            "tool_errors": ledger.get("tool_errors"),
            "bash_calls": ledger.get("bash_calls"),
            "bash_failures": ledger.get("bash_failures"),
            "failed_calls": ledger.get("failed_calls"),
            **({"error": error} if error else {}),
        })


def directory(cfg: Config) -> Path:
    """Where records go: the one place `transcript_dir` becomes a path.

    Translated with `to_posix` for the same reason `workspace_roots` and `sandbox_home`
    are: the setting is written on the Windows side and read inside WSL. Skipping it here
    was not a conversion the caller had to do by hand, it was a silent one. A Windows path
    is a legal *single-component* POSIX filename, so `C:\\Users\\me\\t` is relative, not
    absolute; `mkdir(parents=True)` therefore succeeded against a directory of that literal
    name under the server's working directory, records landed in it, and every check that
    existed to catch the misconfiguration passed.

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

    Three discriminators, and none of them is redundant. The stamp orders the directory
    and is what a reader pairs a stream with its record by. The counter separates two
    dispatches *this* process began in the same millisecond. `_PROCESS` separates two
    processes, which the counter cannot, both of them having started at one.

    One function rather than the same f-string twice: the stream and the record are named
    a moment apart and must stay the same shape, and two copies is how one of them gets
    the new discriminator and the other does not.
    """
    _COUNTER["n"] += 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%f")[:-3]
    return f"{stamp}-{_PROCESS}-{_COUNTER['n']:04d}-{_slug(agent_name)}{suffix}"


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

    The estimate is a guess made before the work; this is what the work cost. Summing the
    `total_*` fields across records is the only way to answer what the local cluster has
    actually spent: they describe every turn, where `input_tokens`, `output_tokens` and
    `cached_tokens` describe only the turn that answered (ADR-0058). An estimate standing
    in for a real figure -- or a per-turn figure under a lifetime name -- would quietly
    poison that total.

    A one-shot `Dispatch` carries no totals of its own (they exist only on the agentic
    dispatch), and its single turn *is* the whole run, so there the answering turn's own
    figures are the totals.

    `total_cached_tokens` is the same argument one level down: summing the totals says what
    was sent, and only the cached figure says what the cluster had to compute. A `None`
    here means the endpoint reported no such field, which is not a zero -- do not sum it
    as one.
    """
    if dispatched is None:
        return {}
    response = dispatched.response
    total_input = getattr(dispatched, "total_input_tokens", None)
    total_output = getattr(dispatched, "total_output_tokens", None)
    total_cached = getattr(dispatched, "total_cached_tokens", None)
    if total_input is None:
        # A one-shot `Dispatch` carries no totals; its one turn is the whole run. The
        # `total_input is None` test rather than a type check because it is exactly the
        # one-shot `Dispatch` that is missing the attributes -- an `AgenticDispatch`
        # always has them, defaulting to a real zero.
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
        # Both derived from `answer_of`, the same helper the result dict uses, so the
        # record and the reply cannot disagree about whether anything came back. Reading
        # `response.text` here instead is what made nine dispatches look empty in this
        # directory while the reasoning they produced sat in the response unread.
        "empty_response": _answer_of(response)[0] == "",
        "answer_is_reasoning": _answer_of(response)[1],
        "reasoning_chars": len(response.thinking),
        # Derived from the same `answer_of` as the two above, for the same reason: the
        # record and the reply must not disagree about the text they describe.
        "duplicate_line_share": duplicate_line_share(_answer_of(response)[0]),
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

    `agent_name` and `entry` are passed rather than derived so the failure path names the
    same delegation the success path would have. Deriving either from `dispatched` would
    reintroduce bug 1 exactly: on a failure there is no `dispatched` to derive from.
    """
    if not enabled(cfg):
        return
    try:
        target = directory(cfg)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)

        path = target / _filename(agent_name, ".json")

        record: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            # Which delegating tool this was, and what it resolved to. Until these were
            # recorded, a `delegate_readonly` call and a plain `delegate` wrote the
            # identical record, so a directory of them could not be counted by kind -- and
            # the totalling is what the records exist for.
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
        # Deliberately broad, and deliberately silent to the caller. The delegation has
        # already done its work; losing its record is worth strictly less than failing it,
        # and there is no failure here an operator could not also see in the empty
        # directory. Never to stdout: that is the MCP wire.
        log.warning("could not write a dispatch transcript", exc_info=True)
