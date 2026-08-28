<!-- BUDGET: 280 -->
<!-- Raised from 180 on 2026-08-28: the turn loop landed and brought five
     mechanisms with it -- turns, eviction, dedup, countdown, progress. The room the
     2026-08-27 split left was measured against a loop that had not been written.
     Then to 280 for budget precedence (ADR-0024). Two raises in one day is the
     signal ADR-0003 means it to be: the next addition should be weighed against a
     split rather than a third raise. -->
<!-- Split out of ARCHITECTURE.md on 2026-08-27 at 423/425 lines. Sized to leave room for
     M4's turn loop, which is also loop.py and would otherwise land back in the file this
     split relieved. ADR-0032. -->
# Dispatch and the response state machine

What the server sends to a model, and what it does with what comes back. Everything here
is `loop.py` and `backends/`; the orchestration around it -- prefetch, path checks, the
backend cache, the MCP wiring -- is in [ARCHITECTURE.md](ARCHITECTURE.md).

Settings are in [CONFIGURATION.md](CONFIGURATION.md), which is generated, and the decisions
behind these mechanisms have their own record in [../DECISIONS.md](../DECISIONS.md). This
explains the shape they produced.

Both paths are here: the one-shot dispatch, and the turn loop above it. They share the
retry and empty-answer machinery, which is why they share a document.

## One wire format, behind a seam

Only the OpenAI-compatible adapter ships. But the *canonical* message shape kept
internally is the Anthropic one — content blocks, tool-use and tool-result — because
nothing outside the backend layer then knows which wire protocol is in play. Adding an
Anthropic adapter later is one new file rather than a refactor.

Three conditions keep that true, and breaking any turns it back into a refactor: the
canonical shape stays block-structured and is never flattened to strings; SSE accumulation
lives per adapter behind one contract; model selection is a registry lookup and never a
reintroduced prefix function. (ADR-0008)

Built. The seam holds three things the layer above depends on. Flattening lives in the
adapter and nowhere else, so the canonical side stays block-structured. Failures arrive as
four distinguishable kinds — unreachable, refused with its status *and body* intact so a
specific 400 stays feature-detectable (ADR-0017), a 2xx that is not the promised shape, and
a malformed canonical request, which is our own bug and must never be retried — because
retry is only buildable on that distinction. And `finish_reason` and the token counts come
back **uninterpreted**: null content with `finish_reason: "length"` is a valid response
carrying no text, not an error. Deciding what that means belongs to the response state
machine in M3, which needs the raw value to decide it.

The adapter's single HTTP client bounds **connect and request separately**. One timeout for
both looks harmless, because the failure everyone pictures is a refused connection — and a
refused connection sends RST and fails in milliseconds regardless. A *dropped* route sends
nothing, so a shared bound leaves the connect phase held open for the entire request
timeout. The operating system's own SYN-retransmission limit is the only thing underneath,
and it varies by platform, so the stall is long and its length is not ours to predict.
Connect is therefore bound by its own, much shorter setting.

## Retry sits above the adapter, and honours what the endpoint asks for

The four error kinds exist so retry can be selective, and `loop.py` is where that selection
happens — never in the adapter, which stays a translator. Unreachable is always worth
another attempt; a refusal only for an exact set of statuses, 429 and 500/502/503/504.
Everything else — 400, 401, 403, 404 — describes the request, and sending it again cannot
change the answer. The set is a module constant, not a setting: which codes mean *temporary*
is a fact rather than a preference. A protocol error is never retried. On exhaustion the
last real exception propagates unchanged, because a "gave up after N" wrapper would discard
the very distinction this layer exists to keep.

`Retry-After` is honoured in both spellings RFC 7231 allows — a count of seconds and an
HTTP-date — since which one arrives is the server's choice and reading one honours the
header by luck. An unparseable or absent header falls back to exponential backoff rather
than failing the call: it is a hint from someone else's machine. An honoured delay is used
as sent and deliberately *not* jittered, jitter being for clients that are guessing;
ordinary backoff is jittered across the full range.

Both are capped by [`retry_max_delay`](CONFIGURATION.md), which is not decoration: the wait
sits outside every HTTP call, so no request timeout reaches it and an uncapped header stalls
the delegation as long as it likes. The whole-delegation deadline below now covers this too,
but the cap stays: it keeps a single honoured `Retry-After` from spending a budget that the
rest of the delegation still needs.

The result reports `attempts`: real calls made, counted by the server, while the token
counts beside it describe the attempt that answered rather than the sum. (ADR-0007)

## One deadline covers the whole delegation

[`dispatch_timeout`](CONFIGURATION.md) is taken once, at the top of a delegation, and
passed down. Every stage of the empty-answer recovery and every transport retry inside them
share it. A fresh budget per stage would mean the setting really bounded three times what it
says, and no test of a single stage would have seen it.

It is enforced at three points, because a deadline checked in only one of them is a deadline
that can be walked past:

- **Before an attempt**, so an expired budget costs nothing.
- **As a ceiling on the attempt**, from what is left. `turn_timeout` already bounds one call
  inside the adapter's client, but it is a fixed budget that knows nothing of how much
  delegation remains, so without this the deadline could be overshot by a whole turn.
- **Against the backoff wait**, before sleeping. A wait that would end past the deadline
  ends the delegation instead — sleeping first would spend the rest of the budget and then
  report a deadline reached by a wait this server chose rather than by the work.

`DispatchTimedOut` is deliberately not one of the backend failures. Those say the endpoint
did not answer; this says the endpoint may be answering perfectly and the delegation has
still outlived what the operator allows. Sending someone to check the cluster over their own
deadline is the wrong diagnosis, and the message names the elapsed time, the setting, and
which stage was running.

**What this does not do** is keep a delegation inside the client's idle timeout. The default
is 3600s against Claude Code's 1800s stdio idle timeout, so the client can abandon a
delegation this bound is still happy with. The per-turn progress notification is what
addresses that (ADR-0018), and it is emitted by the turn loop below -- so the one-shot path,
having no turns, cannot hold the client open at all. This bound turns an unbounded wait into
a bounded one, attributed to the setting that caused it, and claims nothing more.

## The reply budget is resolved once, most specific first

Call argument, then agent frontmatter (M6), then the configured default **last**. That
ordering is ADR-0024's surviving constraint: an operator lowering the ceiling must not
suppress the floor that stops heavy-reasoning models returning nothing, so at high and max
effort the floor is a `max()` over the configured value rather than an alternative to it.

A caller's own number is *not* raised to that floor — it is the most specific instruction
there is, and multiplying it by thirty would make the argument advisory. Guessing too low
still gets the recovery below, which retries at the floor, so being wrong costs one extra
dispatch. The per-model cap applies last everywhere: it is what the wire accepts.

## Reasoning is controlled per request, never inherited

The cluster's reasoning default is set at boot and is not ours to assume — the stack we
run against ships one value in its example environment while two of its own documents
recommend another. So every request states its reasoning level explicitly. Resolution:
agent frontmatter, then the registry row, then the global default.

An unrecognised value is refused rather than passed through — but not because the backend
would ignore it. It does not: it validates `reasoning_effort` and answers a bad one with a
400, measured rather than assumed (JOURNAL 2026-08-26). The refusal here is simply earlier
and cheaper. These are *this project's* levels, not the server's, so the adapter translates
them — `off` is our word for the server's `none` — and a level with no translation would be
refused only after its prefill had been paid for. (ADR-0013)

There is a real failure mode here, reproduced rather than assumed: at high effort with a
small reply budget, reasoning consumes the whole allowance and the response comes back with
null content and a length stop. What the server does about it is below. (ADR-0014)

## An empty answer is recovered from before it is reported

The signature is narrow on purpose: empty text **and** a length stop, together. Either
alone is a different thing — an empty answer at `finish_reason: "stop"` is a model that
genuinely had nothing to say, and a length stop with text in it is ordinary truncation
where an answer exists and is merely cut short. Both must be left alone, because every
mitigation below costs a full generation at the largest budget the model allows.

Three stages, and deliberately not a cascade through every level. Send. If the signature
appears, retry once at the larger of double the budget and
[`thinking_max_tokens_floor`](CONFIGURATION.md), keeping the effort level — this stage
keeps the rendered prompt identical apart from the budget, so the prefix cache survives it.
If that is still empty, step the level down once and send again. The step table is derived
from the level vocabulary rather than written out, so it cannot drift into stepping to a
level that no longer exists.

Stepping down is the last resort rather than the first because it is the expensive one: the
level is part of the rendered prompt, so `prompt_tokens` moves with it (measured, JOURNAL
2026-08-26) and a stepped dispatch misses the prefix cache entirely, paying a fresh prefill
on top of its generation. The stepped budget is resolved again for the new level rather than
inherited, since a lower level no longer needs the headroom the higher one forced up.

One stage is skipped rather than spent: when the model's own cap already pinned the first
budget there is no larger budget to retry at, and re-sending the request unchanged buys a
full generation for a result that cannot differ. That is not a rare edge — high and max
effort already resolve to `thinking_max_tokens_floor`, so where the model cap equals it the
budget stage never fires and the level steps down on the second call. Measured, and the
measurement is why the skip matters rather than being a tidiness: at max effort raising the
budget is not the mitigation at all, and lowering the level is (JOURNAL 2026-08-27, which
also notes what an exhausted max-effort delegation costs in wall clock against the idle
timeout above). A test pins the arithmetic so the stage cannot quietly return.

The two terminal states are different diagnoses, and conflating them would send the caller
to the wrong fix. Still empty after both mitigations is ADR-0014's exhaustion —
`reasoning_exhausted: true`, meaning the task needs more reasoning than this model finishes
inside its budget. Still empty with the level *already* at its lowest is not: there was
nothing left to disable, so the budget was too small for the answer, and reporting that as
exhaustion would tell the caller to lower an effort that is already lowest.

`attempts` counts every real call across all three stages and every transport retry inside
them, and across every turn when this runs inside the loop. The token counts describe the
attempt that answered, not the sum: ADR-0014 requires the retry not to charge the turn
budget, so a turn is charged for the answer it got rather than for what recovering it
cost.

## Turns, and what ends them

A delegation is a loop, and one turn is one model reply plus any tools it called. The loop
ends on the first reply that carries no tool calls — that reply *is* the answer — or when
the turn budget runs out. [`max_turns_default`](CONFIGURATION.md) sets the budget and
[`max_turns_hard_cap`](CONFIGURATION.md) bounds what a caller may ask for; the cap is
applied silently rather than refused, because the work is legitimate and only the number is
not. An agent file will resolve into the same precedence in M6.

The last turn is declared **with no tools at all**. Without that short-circuit a delegation
can end on a tool call nobody will run, having spent its whole budget and returned nothing
readable. Withdrawing the tools leaves the model one thing it can still do, which is answer.
The result reports `hit_turn_limit` so the caller can tell the two endings apart: an answer
written under a withdrawn toolset is a partial one, and worth reading differently from an
answer the model chose to give.

Recovery from an empty answer is per turn and is the same code as the one-shot path — the
cascade below lives in one function that both call. Two copies would be two diagnoses of
exhaustion, drifting apart at whichever one was next edited.

One deadline still covers the whole delegation, not each turn. Per-turn budgets would make
the real bound `dispatch_timeout` times `max_turns`, which at the defaults is a day and a
half rather than an hour.

## The history is resent every turn, so it is trimmed every turn

Each turn resends everything before it, so an untrimmed history makes a delegation cost the
square of its length — the tenth turn paying again for the first nine tool results.
[`keep_tool_results`](CONFIGURATION.md) keeps the most recent few intact and collapses the
rest to a one-line stub. Oldest-first by count, which is what the setting says it is; a
size-aware policy would evict differently and is not what was promised.

What goes is the *content*. The block and its `tool_use_id` stay, because some backends
validate that every tool use has a matching result, and dropping the block outright would
turn a long delegation into a wire-level failure rather than a model that has forgotten
something. The stub says plainly that the result was dropped and can be fetched again.

This is also the honest limit of prefix caching in the loop. The static system prompt, the
files block and the task are stable across turns and stay cached; everything after them
changes by construction, since each turn appends to the history and eviction rewrites part
of what is already there. Only the leading prefix is cache-stable, and no arrangement of the
tail changes that.

## A repeated tool call is answered, not re-run

A model that repeats a call byte for byte usually does so because it is stuck, and paying
for the same work twice does not unstick it. An identical call — same tool, same arguments,
compared with the keys sorted so insertion order cannot defeat it — is served from what the
first one returned, marked plainly as a repeat. Saying nothing and returning identical bytes
would teach the model nothing; the marker is what breaks the cycle.

Two limits, both deliberate. Only tools declared cacheable are served this way, and any tool
that is *not* clears the cache: a file read before a write and read again after it has two
different correct answers, and serving the first one twice would hand the model a file from
before its own overwrite. Refusals are never cached either, since several are transient by
nature — a file that does not exist yet is the obvious one — and caching one would make it
permanent for the rest of the delegation.

Known gap, recorded rather than papered over: a re-read of the same file at a different
offset is a different argument set and is not caught. Closing it needs range tracking, which
is its own piece of work. Upstream's version has the same hole.

## The countdown is in the tail, and the progress notification is not decoration

The model is told how many turns remain, and the last one is announced as the last. That
text goes on the message carrying the tool results, never in the system prompt: a turn
counter in the prefix changes one byte of it per turn and silently costs a full prefill
every time, with no error and no symptom beyond slower answers (ADR-0011). The tail is where
dynamic content is free, because the tool results beside it were never cacheable anyway.

Separately, and for a different reason, the server emits one **progress notification per
turn**. Nothing renders it and the client cannot cancel a synchronous tool call through it,
so it looks cosmetic and is not: it resets Claude Code's stdio idle timer. That timer is
1800s against a `dispatch_timeout` defaulting to 3600s, so without the notification a long
delegation is abandoned by the client while the server is still working on it. This is what
the one-shot path above cannot do, and the reason to prefer the loop for long work.
(ADR-0018)

The notification is injected into `loop.py` as a callable rather than imported, so the
dispatch layer holds no MCP imports and a test can watch the calls without a client — the
same seam as the injected clock and sleep.

## The loop reports what the server watched, not what the model says it did

`turns`, `tool_calls`, `tool_errors`, the number of calls deduplicated and the number of
results evicted are all counted where they happen. ADR-0007 argues this for exit codes; the
same argument covers the economics of the loop, and for the same reason — a model's summary
of its own work is a claim, and these are observations. They are absent rather than zeroed
on the one-shot path, where `tool_calls: 0` beside an answer would read as a model that
chose not to use tools it was in fact never offered.
