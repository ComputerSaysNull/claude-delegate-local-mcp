<!-- BUDGET: 517 -->
<!-- Raised from 494 on 2026-09-05: this document now owns the endpoint capture and diff scripts, which record what comes back. -->
<!-- Raised from 488 on 2026-09-05: the per-turn diagnostic and the end event now carry what the prefix cache saved. -->
<!-- Raised from 477 on 2026-09-05: the adapter now carries four fields the endpoint always returned and this server discarded. -->
<!-- Raised from 463 on 2026-09-04: a deadline failure now reports what the delegation
     had managed, and the paragraph has to say why the loop attaches it rather than the
     raise sites -- and why a one-shot reports nothing rather than zero. Own text cut by
     roughly half first. -->
<!-- Raised from 430 on 2026-09-03: a second deadline this document owns and that did
     not previously exist -- the no-progress deadline, plus the admission wait that
     stacks on both and was stated only in an ADR and a generated cell. -->
<!-- Raised from 420 on 2026-09-02: the turn loop's half of the keepalive the raise below
     was for. Duplication removed first, here and in ARCHITECTURE.md; the split is rejected
     again for the reason below. Second raise this feature needed in one session. -->
<!-- Raised from 400 on 2026-08-31: the one-shot keepalive, which is a mechanism this
     document owns and did not previously exist. Two paragraphs of duplication were
     removed first -- the 3600s/1800s pair had been stated in two sections -- and the
     addition still did not fit. Weighed against a split and rejected: the keepalive is
     the same subject as the per-turn notification it stands in for, and separating them
     would put one idle-timeout answer in each of two documents. -->
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

## The loop reports a finished turn as well as a starting one

`report_progress` fires as a turn begins and carries a counter, because its job is resetting
the client's idle timer (ADR-0018). `on_turn_done` fires as one ends and carries what the
turn produced: its ledger entry and the model's reply. They are separate hooks because they
answer to different callers — one to the MCP session, one to whatever is recording the
dispatch — and because the useful moments are not the same moment.

It is called from both places a turn can end: after tool outcomes are attached, and at the
break taken when a turn returns no tool calls. A single call site would have to pick one,
and the turn that ends without tool calls is the one carrying the final answer, so picking
the other would silently drop the part a reader most wants. (ADR-0043)

The backend call is timed separately from the turn, so a throughput figure divides by the
interval that was actually spent generating rather than by one that also contains tool
execution.


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

Four more fields ride the same rule, carried and never interpreted: the cached prompt-token
count, the total, vLLM's `stop_reason` — its own answer to *which* stop condition fired,
distinct from `finish_reason` and absent on backends that do not speak it — and the engine's
`system_fingerprint`. **Absent is not zero for any of them:** a cached count of `0` is a
measured miss, `None` an endpoint that reports nothing, and a sum folding the two claims
every call missed. The `or 0` idiom used for the required counts is wrong here, and a test
asserts it. Without the cached count nothing about prefix reuse is observable from inside
this server, which is how a tool came to be argued for on an effect nobody here could see
(ADR-0051).

## What the endpoint returns, recorded

`scripts/probe_endpoint.py` writes a dated capture of the response surface to
`local/endpoint-captures/`, which is **untracked and on the secret denylist** (ADR-0052).
It answers "do we already get this number?" by looking, rather than by re-probing or by
computing it ourselves. Three shapes are elicited, because the adapter parses all three
and one plain completion misses two: a normal reply, one truncated at the token cap, and
one that calls a tool.

`scripts/diff_endpoint_captures.py` compares two captures and reports **structure only** —
fields added, removed or type-changed, and metric names that appeared or vanished. It
needs no endpoint, which is the half that keeps a cluster outage from looking like a
repository failure, and it exists as a script rather than a delegation because the
denylist that protects the captures also stops the local model reading them. That cost is
deliberate; ADR-0052 says why.

What may never be recorded is a rule rather than a list of today's big fields: anything
that echoes the prompt, anything that could name a host, and metric label values, which
are stripped as a rule rather than as a side effect of wanting names. **No scanner
protects these files** — they are never tracked, so the gate never sees them — which makes
the probe's allowlist the only control.

The per-turn diagnostic carries the cached count too, and per turn is the level that
matters: turn one pays for the whole prefix and every turn after it should not, so a single
figure for a delegation would hide the case worth seeing — a cache going cold mid-run. The
`end` event carries their sum, which is what a reader and the savings report both want.

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

## Two deadlines: a ceiling, and a no-progress deadline

[`dispatch_timeout`](CONFIGURATION.md) is taken once, at the top of a delegation, and
passed down. Every stage of the empty-answer recovery and every transport retry inside them
share it. A fresh budget per stage would mean the setting really bounded three times what it
says, and no test of a single stage would have seen it.

It bounds total time, and total time cannot tell a delegation that is **merely long** from
one that is **wedged** — the case that must not be killed from the case that must be. So it
is a ceiling, and a second deadline does the killing:
[`stall_timeout`](CONFIGURATION.md) is how long a delegation may run without *completing a
turn*. Both bound every attempt and the tighter one wins; without that, the ceiling would
let a single wedged call sit for its whole duration, which is the failure the pair exists
to split apart. (ADR-0047)

The progress signal is turn **completion**, and which signal is a real design constraint
rather than a detail. The per-turn progress notification fires at the *top* of a turn, so
it would reset the clock on entry to the very turn that then wedges; the keepalive proves
liveness on a timer regardless of progress, which is precisely what must not count. A
one-shot completes no turns at all, so its no-progress deadline runs from entry and its
effective bound becomes the tighter of the two settings — no special case, and the failure
still names whichever setting actually expired.

Both are enforced at the same three points, because a deadline checked in only one of them
is a deadline that can be walked past:

- **Before an attempt**, so an expired budget costs nothing.
- **As a ceiling on the attempt**, from what is left. `turn_timeout` already bounds one call
  inside the adapter's client, but it is a fixed budget that knows nothing of how much
  delegation remains, so without this the deadline could be overshot by a whole turn.
- **Against the backoff wait**, before sleeping. A wait that would end past the deadline
  ends the delegation instead — sleeping first would spend the rest of the budget and then
  report a deadline reached by a wait this server chose rather than by the work.

Each of the three asks *which* deadline expired before reporting one, and that is not
symmetry for its own sake: the remedy differs. For the ceiling, "raise it or shorten the
task" is right. For a stall it is actively wrong — raising a no-progress deadline buys a
longer wait for a run that has already stopped progressing — so that failure says to check
the endpoint instead. The retry-wait branch reported the ceiling unconditionally until a
regression test said otherwise.

`DispatchTimedOut` is deliberately not one of the backend failures. Those say the endpoint
did not answer; this says it may be answering perfectly and the delegation has still
outlived what the operator allows — so sending someone to check the cluster is the wrong
diagnosis. The message names the stage, the setting, and the elapsed time, which is the
*delegation's*, derived from the deadline, so it can never read as less than that limit.

**It also names what the delegation had managed** — turns completed, tool calls, the last
tool — because that is the distinction a reader must be able to make: a wedged task shows
work behind it where a dead endpoint shows zero of both. Without it a stall read exactly
like an unreachable endpoint, whose honest remedy is to stop using the server, and twice on
2026-09-04 that was the wrong call. The turn loop attaches the counts on the way out, in one
handler, because the raise sites cannot see its ledger and the dispatch record that also
carries them exists only on success. A one-shot supplies none and reads as it always did:
**absent and zero are different facts**, and the one path that cannot stall must not report
itself as having done so.

**The admission wait stacks on top of both**, rather than being contained by either. A
delegation waits up to [`admission_wait_timeout`](CONFIGURATION.md) for a slot, and only
then is a deadline taken inside the loop that slot admitted — so the caller-visible worst
case is that setting plus the ceiling, added (ADR-0038). Recorded here because this is
where a reader looks for what bounds a delegation, and it was previously stated only in the
ADR and in a generated config cell.

**What this does not do** is keep a delegation inside the client's idle timeout -- the
client can abandon one this bound is still happy with. That is what the notification and
keepalive below are for. This turns an unbounded wait into a bounded one, attributed to the
setting that caused it, and claims nothing more.

## The reply budget is resolved once, most specific first

Call argument, then agent frontmatter, then the configured default **last**. That
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
the call argument, then the agent file, then the registry row, then the global default.

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
not.

An **agent file** asking for more than the cap is refused instead, at load. The asymmetry is
deliberate: a call argument is transient, so clamping it costs nobody anything, while a file
is committed, read again and trusted. Clamping there would leave a wrong number sitting in
the file indefinitely, running correctly and reading as though it were in effect.

Tool calls carry a `BashPolicy` alongside the allowed set: where `run_bash` runs, whether it
has a network, and what else is bound. It travels with the turn rather than sitting in
configuration, for the reason `allowed_tools` does — it belongs to one delegation, and a
server-wide default for it would be a config default living outside `config.py`. A
delegation that names none gets a sandbox that can reach nothing of the caller's, which is
the right way for the default to fail.

The last turn is declared **with no tools at all**. Without that short-circuit a delegation
can end on a tool call nobody will run, having spent its whole budget and returned nothing
readable. Withdrawing the tools leaves the model one thing it can still do, which is answer.
The result reports `hit_turn_limit` so the caller can tell the two endings apart: an answer
written under a withdrawn toolset is a partial one, and worth reading differently from an
answer the model chose to give. It is exactly "the loop reached its last turn". It once also
required a tool call on that final reply, which a backend offered no tools does not make, so
it was false in precisely the case it names and true only for a backend that ignored the
withdrawal. A delegation that would have finished on its last turn anyway now reports the
limit too; that costs a reader one look at `max_turns`, where the old reading cost them a
truncated answer read as a whole one.

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

Known gap, recorded rather than papered over: a re-read of the same file from a different
`start_line` is a different argument set and is not caught. Closing it needs range tracking,
which is its own piece of work. Upstream's version has the same hole.

## The countdown is in the tail, and the progress notification is not decoration

The model is told how many turns remain, and the last one is announced as the last. That
text goes on the message carrying the tool results, never in the system prompt: a turn
counter in the prefix changes one byte of it per turn and silently costs a full prefill
every time, with no error and no symptom beyond slower answers (ADR-0011). The tail is where
dynamic content is free, because the tool results beside it were never cacheable anyway.

Separately, and for a different reason, the server emits one **progress notification per
turn**. Nothing renders it and the client cannot cancel a synchronous tool call through it,
so it looks cosmetic and is not: it resets Claude Code's stdio idle timer, which expires
well inside [`dispatch_timeout`](CONFIGURATION.md). (ADR-0018)

One per turn is not enough, and the one-shot path has no turns to hang it on at all, so
both report on a timer as well, every [`keepalive_interval`](CONFIGURATION.md). A turn's own
duration is bounded only by `turn_timeout`, which defaults to exactly the client's idle
timeout, so one slow turn outlasts it unaided -- a one-shot was measured running 1645s with
nothing sent between its start and its answer, and a turn may do the same.

**Measured on 2026-09-01**, against a deliberately silenced two-item batch: at 1800s the
client aborts and **nothing reaches the server** -- no cancellation, no EOF. It held both
admission slots for 300s longer, until the work finished on its own, then carried on serving
the same session. So `keepalive_interval` is a correctness setting rather than a convenience:
the server cannot discover that nobody is listening, and sending something is the only guard.
The same heartbeat writes an `alive` event to the stream, a silent stream and a silent wire
being one problem from two sides. It carries elapsed and the deadline it is measured
against, and **not** what the model is doing: there is no streaming, so the server does not
know. Nothing is cancelled when the interval passes.

Each runs beside the work rather than inside it, cancelled and awaited in a `finally`
covering every exit including the raised ones, so none outlives its dispatch. A callback
that raises stops the heartbeat alone: one that killed a delegation over an undeliverable
notification would be worse than none.

A timer fires only while the event loop is free, which is why the turn loop runs its tool
calls through `asyncio.to_thread`. `_run_calls` is synchronous and `run_bash` reaches
`subprocess.run`, so a command held the loop for its whole duration -- no heartbeat, no
per-turn notification, no stdio traffic for anything admitted alongside it -- while looking
present in any test that only slows the backend.

The notification is injected into `loop.py` as a callable rather than imported, so the
dispatch layer holds no MCP imports and a test can watch the calls without a client — the
same seam as the injected clock and sleep.

## The loop reports what the server watched, not what the model says it did

`turns`, `tool_calls`, `tool_errors`, the calls deduplicated and the results evicted are all
counted where they happen, and `tool_calls_by_name` splits `tool_calls` per tool — one number
cannot tell a delegation that read two files from one that overwrote two. ADR-0007 argues
this for exit codes and the same argument covers the loop's economics: a model's summary of
its own work is a claim, these are observations. All are absent rather than zeroed on the
one-shot path, where `tool_calls: 0` would read as a model that chose not to use its tools.

`bash_calls`, `bash_failures` and `last_bash_exit` are the subject ADR-0007 was written
about, and are now counted the same way. The exit code reaches the ledger as a field on the
result block, never parsed back out of the text the model also reads: a trailer regex over
prose stops firing the day the wording changes, and nothing reports that it stopped.

Two distinctions in those three numbers are worth stating, because both are easy to get
backwards. **Attempts, not completions** — a call refused before a process started still
happened, and `tool_calls` beside it counts the same way; a model refused ten times has not
run zero commands. **`last_bash_exit` moves only when something ran**, which is why the
result block carries `ran` separately from the exit code. A command killed on timeout *did*
run, so it becomes the last one and reports no exit code — leaving the previous `0` standing
would let a kill read as a success, which is the exact misreport ADR-0007 exists to catch. A
refusal never started a process, so the last command that ran is still the previous one, and
its exit code is still the true answer. `None` carries "nothing exited"; `0` cannot, being a
real exit code a command can return.

## Context economics, off by default

A delegation that runs out of room does not fail. It keeps answering, from a history the
backend has quietly begun dropping, and the answer looks exactly like one written with
everything in view. `context_overflow_enabled` turns on two checks that notice, and it is
off by default for a reason given below.

Every threshold is a share of `ModelEntry.context_window` and of nothing else. Four of the
five bugs this cost the ancestor project were one shape — a threshold computed against the
wrong denominator — so the window is read in exactly one function,
`loop.projected_fraction`, and the reserve held back for the reply is a **fraction** of it
rather than a token count. That is not tidiness: a flat reserve large enough to matter on a
million-token window is more than 95% of an 8K one, so the same constant that is prudent for
one model reports the other as full before it has done anything.

**Preventively**, projected use is compared against three points on one escalation:
retention tightens, then the model is told to wrap up, then the delegation aborts. The
stages are module constants rather than settings, because they name one ordered escalation
and an operator free to move them independently can invert it. Retention only ever tightens;
a delegation that wins back headroom by evicting has not stopped growing.

**Retroactively**, a prompt that stops growing while the loop is still appending to the
history is evidence that something upstream is dropping it. The catch is that this server's
own eviction is by far the likeliest explanation, so the check subtracts it first — and
subtracts it by reading a count the loop recorded at the point it evicted, never by reading
`finish_reason` or the model's account of what it still remembers. That is ADR-0007 applied
to context rather than to exit codes. There is a small token slop on the comparison, because
a backend that trims a token between turns must not read as truncation.

On abort the caller gets a state report rather than a bare refusal: the server's ledger of
what it ran, beside `git status --porcelain` for the repositories the delegation wrote into.
The two are deliberately not reconciled for the reader — where they disagree, the
disagreement is the finding. The git call is server-side, exactly as the path policy's layer
4 already runs git, and is not a route into the sandbox (ADR-0010).

### The window is checked before any of it is armed

`context_window` is whatever the operator wrote in `models.toml`, and `docs/MODELS.md` is
explicit that nothing enforces it against the server. Arming a graduated abort against an
unverified denominator is how the ancestor came to compute every threshold against a ceiling
its backend would never reach. So the first agentic delegation to a model asks the endpoint
what window it is serving and compares.

The check **validates and never derives**. On a disagreement overflow handling stays off and
says which two numbers disagree; it does not adopt the endpoint's figure, because the
operator's file is authoritative for everything else about that model. An endpoint that
reports no window at all is answering correctly and does not block anything.

The verdict is cached per model, and the cache expires — that expiry is the whole point of
it. The ancestor's equivalent never expired and was written on any failure, so one transient
outage disabled overflow handling until someone restarted the server. Here a **confirmed
refusal** is cached and a **transport failure** is not: an endpoint that answered has told us
something about itself, and one we could not reach has not.

### Diagnostics, per call

`diagnostics=true` adds a per-turn breakdown to the reply: what each turn's prompt cost,
what it evicted, which tools it ran and how each ended. Metadata only — tool results are not
carried, since a diagnostic that embedded what it was measuring would become the expensive
payload it exists to explain.

The field worth asking for is `evicted_then_reread`: files the model read again after this
server had dropped the first read from the history. The aggregate ledger can already say a
delegation was expensive. Only this says whether it was expensive because the work was large
or because it kept paying twice for the same bytes — and those have different fixes, one of
them being a larger `keep_tool_results`. It is a prerequisite for sizing eviction rather
than a report about it.

Correlation is on the path argument the model supplied, not the path the policy resolved:
`tools.py` never hands the resolved one back, and the raw argument is what a reader
reconciling the report against a working tree is looking at anyway.
