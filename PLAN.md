<!-- BUDGET: 300
     Reset from 738 on 2026-09-06, when 35 completed items moved to
     archive/PLAN-milestones.md. The raise history that stood here ran to sixty lines and
     said itself that the reason for each raise is in the CHANGELOG.md section for the
     pull request that made it, so it was two copies of the same thing and this is the
     one that was redundant. -->
# Plan

Open work, one line per item, status first so the file scans.

`✅` done, dated · `🔄` in progress · `⬜` not started — queued under **Open**, on hold
under **Deferred** · `❌` cancelled, with date and **reason** — cancelled items stay,
because the fact that something was considered and dropped is worth more than a tidy list.
A struck-through entry with **no marker** is the original of a ticked item, kept beside it
where its reasoning turned out to be wrong in an instructive way. It travels with its item
when that item is archived, so one left here belongs to something still open.

**Completed items are archived, not kept.** `✅` marks an item until the end of the session
that finished it; then it moves to
[archive/PLAN-milestones.md](archive/PLAN-milestones.md) with its struck original, and this
document holds open work only. Until 2026-09-06 they stayed here "as a record of recent
work", which was a convention rather than a decision — the ADR-0003 it cited is about size
budgets and says nothing about retention, and twenty-eight of them had accumulated to 36%
of a document headed *Open*.

**Ask at the end of a session.** Not on a threshold: ADR-0033 already tried one and
retired it, because a warning with no remedy the reader can apply fires until it stops
being read. This is a question someone answers, and the end of a session is when the answer
is cheap — the work is fresh enough to annotate and finished enough to move.

---

## Open — hardening, testing and troubleshooting

The milestone plan closed with M7; this is what is queued now. Ordered within each
group by what the item's own annotation says it costs.

### Security review, 2026-09-02

- ⬜ Content-level detection for a renamed secret — every path-policy layer inspects the
  path and none the bytes, so `config.json` holding a private key passes all of them and is
  inlined, and `run_bash` can read one the mount-level scan did not match by name. One
  finding, not two: fixing the detection fixes both ends. **Not** by pointing `scan_text` at
  it, which the 2026-09-02 review recommended — that scanner looks for RFC1918 addresses,
  private-DNS suffixes and non-allowlisted emails, and would false-positive on the source a
  review delegation exists to read. A narrow, high-precision check for key material instead
  (PEM armour, `BEGIN OPENSSH PRIVATE KEY`, cloud key prefixes)
### Limits and admission, 2026-09-03

- ⬜ **Admission has no anti-starvation, and 2026-09-05's measurements make it sharper.**
  `_binding` ends with a queue-position check refusing any waiter with `ahead > 0`, where
  `ahead` counts only earlier-ticketed waiters *that currently fit*. Of the four rules only
  `max_inflight_large_prefills` is guarded by `is_large`, so a small request never tests it
  — and a large request parked on that cap therefore counts every later small request as
  ahead of it, and they go first. Deliberate, and the docstring says why: strict ticket
  order would reintroduce head-of-line blocking. But there is **no aging, no reservation
  and no barrier.** The only escape is the caller's `admission_wait_timeout`, a bail-out
  rather than a guarantee, and the ticket staleness is a crash backstop.
  - **What changed:** the eviction half was reasoned, not measured, and now can be. The
    KV pool is `kv_cache_size_tokens` from the endpoint's own metric, and
    `vllm:kv_cache_usage_perc` says how full it is, so "the prefix a starved request was
    queued to reuse gets evicted meanwhile" is now a question with an instrument. Decide
    after step 5 lands the reader, not before.
  - **What is settled:** the related worry that `max_inflight_large_prefills = 2` trades
    cache hits for pipelining is **answered and dead.** Three concurrent large prefills
    over a shared prefix cost the same as three serial ones and hit cache identically, and
    the engine runs one at a time, touching two only at the handoff — which is exactly
    what "one running plus one staged" describes. Keep the setting and the value.
  - **What session 2 adds: `is_large` is decided once and never revisited.** `admit()` reads
    `prefill_tokens` from the *opening* estimate and holds the lease for the whole
    delegation, so an audit opening under `large_prefill_tokens` is filed as small for life
    — while later turns re-prefill 40-70k each and the slots file still reads `large: 0`.
    The rule that exists to serialise large prefills has never been tested by the workload
    it was written for, and the same staleness understates `kv_token_budget`. **The eviction
    item above is what makes a delegation's prefill grow**, so fix that first and re-measure;
    re-deriving `is_large` per turn is cheap but may then be unnecessary
- ⬜ **`kv_token_budget` is 1.66x the real KV pool, and the number to fix it is now
  readable.** The setting defaults to 2,400,000 and its help text says it "sits just under
  the measured KV pool". The endpoint reports `kv_cache_size_tokens = 1,444,236`, so it
  sits well over. Nothing has failed, because the setting protects latency rather than
  correctness — over-admitting queues and preempts rather than erroring — which is exactly
  why it drifted unnoticed. Likely cause is the 2026-09-04 model swap: a vision model
  carries more weights, so less memory is left for KV and the pool shrank underneath a
  constant measured against the old model.
  - **Do not fix it with a new constant.** An `.env` override is right today and drifts on
    the next swap; changing the default bakes one deployment's hardware into the
    repository. Derive it from what `backend_status` now reads, the way `WindowCheck`
    already derives per process — a server guessing at a figure the cluster publishes is
    the whole argument of the 2026-09-05 session.
  - Deferred deliberately on 2026-09-05: our delegations run ~45k tokens, so six of them
    is 19% of the pool and the gap is not currently reachable.

Raised by a documentation audit that was truncated by its own turn budget, then rerun. The
three settings below were each sized against a constraint that has since moved, and none of
them was re-derived when it did.

### Documentation accuracy

- ⬜ TOO VERBOSE across `docs/ARCHITECTURE.md` and `docs/DISPATCH.md` — the one check class
  the 2026-09-06 audit did not run at all. 1,457 lines needing a trim proposal per
  candidate, deferred rather than done badly, and the only class of that audit with no
  coverage. It is audit work, with a method a feature session does not have
### Improvements

- ✅ 2026-09-06 A read-only form of `delegate_to_agent` — the agent tool with its set fixed
  to whatever declares no write, exactly as `delegate_readonly` is to `delegate`.
  Shipped as `delegate_to_agent_readonly` (ADR-0059), and it took the `workdir`/`project`
  split with it: the correspondence the annotation rests on — a workdir is a read-write
  bind, so a read-only tool cannot offer one — was prose, and `list_agents` had already
  falsified it. It is a test now. **The ramp is measured**: two arms issued in one message
  started 2.2s apart, against the 120s a chained tool predicts (JOURNAL 2026-09-06). The justification is
  ADR-0042's and unchanged: a client decides before the call runs and never sees arguments,
  so narrowing with `allowed_tools` cannot buy the declaration. `#91`'s note that "what
  `delegate_readonly` has no equivalent of is the agent" is this same gap seen from the
  other side, recorded there as context for a finished item rather than as work.
  **What makes it worth doing now is measured.** A `readOnlyHint` tool runs on independent
  clocks while `delegate_to_agent` is released one arm per 120s, so the 2026-09-06 audit
  paid 120s x (n-1) of pure client ramp on passes that were `read_file`-only anyway — every
  one after its second wave. It would also keep the agent file, whose accumulated
  false-positive guardrails are most of that agent's value and which `delegate_readonly`
  cannot carry. Two caveats to state rather than discover: the annotation's causation is
  correlated and not proven, testable with two calls timed against their own issue stamps;
  and it buys nothing against admission, which is server-side and starves a read-only
  fan-out identically on `max_inflight_large_prefills`
- ❌ 2026-09-05 A batch returns nothing until its slowest item settles — **wrong when
  filed**, and moot besides: `#103` removed both batch tools (ADR-0051). `asyncio.gather`
  withheld only the final dict. Each item's `run_delegation` wrote `stream.end` and its
  transcript in its own `try/finally` as that item settled, turns streamed live through
  `on_turn_done`, and per-item progress fired before the gather returned. An as-completed
  drain would have passed its tests and improved nothing. Original entry follows.
- ~~A batch returns nothing until its slowest item settles~~ — `asyncio.gather` over the
  items, so one that stalls for the whole deadline withholds results that finished minutes
  earlier. Purely latency and usability: slots are released per item as each `admit` context
  exits, so the cluster gets its capacity back promptly and only the caller waits. Measured
  on 2026-09-04, where two of three items were ready and unusable for 35 minutes. Progress
  notifications already flow per item, so the missing piece is handing back what is done —
  and the shape has to keep the per-item `ok`/`error` contract that `#45` exists to protect,
  because shielding or restructuring the gather is what silently restored a lockout before
- ⬜ Server-format twins for the four Claude Code agents — `#72` made it visible that
  `code-reviewer`, `docs-audit`, `researcher` and `test-writer` load only in Claude Code,
  so `delegate_to_agent` can reach one of five agents in this repository. `docs-audit-local`
  is the shape to copy (`#67`). CONTRIBUTING.md already records the two-format arrangement
  as temporary; this is what it costs
- ⬜ Globs in `files[]`, expanded server-side — a shorthand for naming many files, not a
  way to look for anything. Its original justification, that expanding before the call keeps
  `delegate_readonly` toolless and loopless, no longer holds now the fork is settled the
  other way, so this is a convenience and ranks below the search tool. The work is in the
  budget rather than the matching — a glob hitting two hundred files has to skip and account
  for them the way `context.prefetch` already does, not spend `prefetch_budget` silently
- ⬜ **A delegation returns a handle, and a second call collects it** — the client backs an
  MCP call into the background after 120s. ~~and issues the next tool call only then, so
  firing `n` delegations in one message costs `120s x (n-1)` of stagger before the last one
  starts.~~ **Corrected 2026-09-06 (`#118`), then narrowed the same day.** Four
  calls issued in one message started within 5.6s of each other, all four outlasting 120s
  and being backgrounded together, so the 120s is when the client stops *waiting* rather
  than when it issues the next call. **That holds for `delegate_readonly` only.** Those four
  arms carried `readOnlyHint`; six `delegate_to_agent` arms issued the same way chained at
  exactly 120s intervals, the last landing at +688s. So the stagger is real for the two
  write-capable tools and the justification `#118` removed is restored for them — **re-rank
  it back up.** A read-only form of the agent tool, filed under Improvements, would remove
  the ramp for the audit case without this item; this one remains the general answer.
  **That tool shipped on 2026-09-06 and the ramp it removes is now measured** — two arms
  2.2s apart where a chained tool predicts 120s (JOURNAL 2026-09-06). This item is still
  the general answer, for the two tools that must keep the annotation they have. Measured 2026-09-05: four passes issued together started at 20:24:30, 20:26:32,
  20:28:32 and 20:30:32. They do run concurrently once started — ~~`seqs=4, large=0` in the
  shared slots file — so the fan-out works; it is only the ramp that is wasted.~~
  - **CORRECTED 2026-09-05 (session 2): the struck line had it backwards twice.** The
    fan-out is worth more than the ramp — the aggregate lever is real (JOURNAL 2026-09-05)
    — but `stall_timeout` is wall-clock **per delegation**, so the batch takes the gain
    while each run pays the per-sequence penalty, which is what tips one over the deadline.
    Handles do not change that; they only stop it being caller-visible. And `large=0` was
    not evidence of health — see the admission item for why that counter cannot see a
    fan-out at all
  - **Server-side rather than a client setting**, deliberately. `MCP_TOOL_TIMEOUT` might
    shorten the ramp, but it is per-machine setup that does not travel, and it is not known
    here whether it backgrounds or kills — untested, and the failure mode is severe.
  - **Shape that keeps the common case cheap:** block for a short grace window and return
    the result inline if it finishes, so a single fast delegation stays one call; otherwise
    return a handle. `collect(handle, wait_seconds)` blocks up to just under the client's
    threshold, which costs nothing because the work is already running.
  - Moves the admission wait behind the handle, so `admission_wait_timeout` stacking on
    `dispatch_timeout` stops being caller-visible wall time (ADR-0038).
  - Restructures the model-facing tool contract, so it is a behaviour change with an ADR,
    not a wording fix. **Related to streaming but not blocked on it** — streaming is
    token-level liveness inside a turn, this is call-level detachment. Say so in the ADR.
- ⬜ **A transcript says a tool errored, never what it was asked or why it refused** — the
  ledger is `{name, outcome}` per call, so a pass reporting `tool_errors: 1` across twelve
  `read_git` calls on 2026-09-05 could not be diagnosed *even with transcripts enabled*.
  Searching the whole record for refusal text returns nothing. This is ADR-0039's own
  argument applied to half the record: it reasons that a transcript which "cannot say what
  was asked does not answer the question a transcript is opened to answer", and settles
  that for the task while leaving tool arguments out. Add the arguments and the refusal
  message to the ledger entry — mindful that arguments can carry paths, which is why the
  ADR excluded file *contents* and not identifiers
  - **CONFIRMED and widened 2026-09-05 (session 2).** The same gap hid a second thing, and
    that one already has its reader: `evicted_then_reread` caught the model re-reading a
    file eviction had just dropped, on three consecutive turns, and nothing surfaces it
    because `diagnostics` defaults off. The fix is not only richer ledger entries but
    deciding what an *operator* record carries without being asked — ADR-0024's own
    reasoning, which is why `transcript_dir` is already independent of the caller's flag
  - **And the live stream is missing a field the final record has.** `Stream.turn` writes
    the token counts but not `tool_results_evicted`, which `write` emits per turn. So a
    stream shows the cache collapsing and cannot show the eviction that caused it — the one
    field that would have made the bug above self-evident while watching. One line to add
- ⬜ **Streaming, reopened 2026-09-05 with a scope.** Filed and cancelled the same day,
  2026-08-25, on the grounds that MCP tool calls are request/response so the caller sees
  nothing incrementally either way. That is still true of the caller and was never the
  whole picture: it never had its own ADR (ADR-0018 is about the progress notifications
  offered as the substitute), and it was annotated once during the ADR-0043 work as
  *"worth revisiting: the premise moved"* — because the cancellation weighed one consumer
  and there are two. The second is the transcript stream a person reads *while* a
  delegation runs.
  - **Lost work is the argument that was missing.** A task too big to finish a turn is
    abandoned at the tighter of `stall_timeout` and `turn_timeout`, and everything
    generated is discarded: `_decode()` needs the whole body as one JSON object, and
    `complete()` states it never returns a partial. The tokens exist only on the backend.
    Nor is the work re-done — `except TimeoutError` always raises, and retry covers only
    an unavailable or refusing backend.
  - **It makes the stall deadline honest, which beats returning partials.** ADR-0047 chose
    turn completion because every other signal was fake: the per-turn notification fires at
    the *top* of a turn and the keepalive is a timer, so both reset the clock on the very
    turn that wedged. Token arrival is real liveness. `stall_left` resetting on token
    arrival means a call producing tokens is never killed and a call producing nothing
    still dies — **this supplies the signal ADR-0047 lacked rather than contradicting it**,
    which is the argument that would supersede its heading.
  - **Scope, in order of value:** a `stream` key in `wire_body()`; an SSE accumulator
    behind the existing `complete()` contract, so *"never returns a partial"* stays true
    and the stall path reads the accumulator rather than `complete()`; `stall_left` reset
    on token arrival; deltas feeding `stream.turn`; and last, a partial returned at
    `dispatch_timeout`, marked partial so the calling conversation can decide whether to
    ask for the rest.
  - **Cheaper than it looks**, because the seam was preserved for it — `base.py` says SSE
    accumulation lives per adapter behind one contract, a method on the protocol rather
    than a shape baked into the caller. **Costly** because `_decode()` requires one whole
    JSON object, so there is no line-by-line path to extend, and the adapter's *"a single
    non-streaming call **is** the turn"* is a claim streaming invalidates.
  - **2026-09-05 supplied the evidence this was missing, and session 2 confirmed it.** Five
    stalls in one session, every
    one with `backend_status` reporting the endpoint healthy and idle — zero preemptions,
    KV under 4%, no admission wait. Turn completion is the only liveness signal there is, so
    a model reasoning productively inside one long turn is indistinguishable from a wedged
    one, and the deadline kills both. Prompt shape shifts the odds and does not remove the
    failure: one pass died having completed **29 turns**, in its thirtieth. ~~Nothing bounds
    the length of a single turn, and no prompt can. `stall_left` resetting on token arrival
    is the only fix here that is not a guess about how a model will decompose its work.
    Tentative because the session's own shape experiments then contradicted each other —
    a prefetched pass with one turn boundary survived while the same shape with many died
    at zero turns — so the stalls are real and the mechanism proposed for them is not
    established. A second session was investigating the same failures independently; read
    its findings before treating any of this as the argument.~~
    - **Corrected:** something does bound a single turn — `max_tokens` — and it is set
      2.6x above what the deadline can decode, so the bound never binds before the kill
      does. That makes streaming a **liveness** fix rather than *the* fix: capping the
      budget stops the deaths, and `stall_left` resetting on token arrival is what stops
      a legitimately long turn being killed once the budget is honest. Keep both, in that
      order, and drop "only". The shape experiments that read as contradictory were
      measuring turn boundaries against a budget nobody had priced
  - **The trap, recorded with it:** token flow is not turn completion. The `alive`
    heartbeat must not report streamed tokens as liveness without the deadline change
    above, or it would call the 2026-09-04 stalls healthy. ~~Open question to measure: a
    model looping while emitting tokens is bounded by `max_tokens`, but whether a reasoning
    model's thinking tokens count against it is unknown here.~~ **Answered 2026-09-05
    (session 2): they do.** A turn whose entire visible answer was the word `DONE` reported
    `output_tokens: 697` at `effort: low`. So `max_tokens` does bound a looping model — it
    is simply set far above the deadline today.

## Deferred

On hold for weeks or months. Not cancelled, and not queued.

- ⬜ Anthropic-compatible adapter — the seam and canonical shape are kept so this is
  additive, roughly 150 to 220 lines in one new file (ADR-0008)

## Cancelled

- ❌ 2026-08-25 Run Claude Code inside WSL — cancelled on workflow grounds, not
  engineering ones. It would delete the path-translation module outright and remove the
  12x test penalty. ADR-0002 keeps the trigger: if development moves onto Linux for
  independent reasons, revisit immediately. ADR-0020
- ❌ 2026-08-25 Dedicated Linux box beside the cluster — cancelled. Solves sandboxing but
  the workspace would reach it only over a share, a sync tool, or a clone, each worse
  than the local bridge and each adding a failure the bridge does not have. ADR-0020
- ❌ 2026-08-25 Scheduled docs-audit workflow — cancelled. It needs an API key, which is
  standing billing exposure for a job that fires whether or not anything changed, and a
  calendar measures the wrong thing. Replaced by the gate's `audit-due` signal
- ❌ 2026-08-25 Collapse reasoning effort to three levels — cancelled. Saves one enum
  value, does not shrink the state machine, and would make our API disagree with the
  backend's documented values. ADR-0013
