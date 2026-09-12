<!-- BUDGET: 610
     Raised from 600 on 2026-09-12: the rate memory's floor landed, which struck one
     item's premise and re-ranked the coalescing hold with what measured against it.
     Raised from 580 on 2026-09-12: verifying #172 end to end re-ranked three items on
     evidence -- the cold start, the admission bail-out, and the margin nobody had filed.
     Raised from 570 on 2026-09-12: the fan-out answered M12's eviction half, which that
     item had been waiting on a reader to make answerable at all.
     Raised from 551 on 2026-09-12: streaming's first slice landed and the audit fan-out
     surfaced the asymmetry in `expect`; the measurements are in the hand-off notes.
     Raised from 535 on 2026-09-12: three findings this session surfaced and nothing else
     records -- the audit remainder, the admission bail-out, and work too large for a turn.
     Lowered from 539 on 2026-09-12: the dispatch defects are short pointers now, with the
     measurements moved to the session hand-off notes.
     Raised from 514 on 2026-09-11: four dispatch defects found by reading the transcripts
     of nine dead delegations, none of which the roadmap had a place for.
     Raised from 505 on 2026-09-11: the metrics sampler prices the reply budget rather than
     only reporting it, the audit's root cause. Nine lines, trimmed from fifteen.
     Raised from 492 on 2026-09-09: the first nested run of this suite found a second
     over-broad cover, and five integration tests that cannot run without the network.
     Raised from 480 on 2026-09-08: a captured exit code of zero is not proof of success,
     and only the server can close that half -- measured at 3/4 against 0/4.
     Raised from 470 on 2026-09-08: the self-covering denylist file, found while measuring
     M9's exit condition, plus the tick for the read-only spike it answered.
     Raised from 460 on 2026-09-08: three M9 ticks and their reflow, plus the retention
     rule this document now states once instead of twice. Nothing is archived mid-roadmap
     any more, so a tick is paid for here rather than by an item leaving.
     Raised from 440 on 2026-09-08: an item filed for the denylist asymmetry `--init`'s
     backup coverage exposed, plus the reflow that ticking M8's last item costs. Completed
     items stay until the milestone plan they belong to closes, so nothing was archived to
     pay for it, and the ceiling carries slack rather than landing on the new size.
     Raised from 430 on 2026-09-07: where a ready measurement lives, because it lives
     somewhere untracked and so reached nobody reading this. Every line the labelling had
     left overlong was reflowed, and the tick rule now says reflowing is allowed.
     Raised from 390 on 2026-09-07: four of the six measurements this roadmap was waiting on
     came back, and each answer belongs against the item that was waiting for it. Two of
     them changed the item rather than confirming it.
     Raised from 300 on 2026-09-07: five milestone headings with their exit conditions, and
     the items M8 to M11 add. Four completed items were archived first, which paid for 46
     lines of it.
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

**Completed items stay until the roadmap closes.** A `✅` keeps its place, with its struck
original, until every milestone is finished or the owner says to archive; only then does it
move to [archive/PLAN-milestones.md](archive/PLAN-milestones.md). The roadmap is read as a
whole while it is being worked, and a closed milestone beside an open one shows what has
already been paid for.

Until 2026-09-08 this said a `✅` left "at the end of the session that finished it", while
the budget comment above said they stay until their milestone plan closes. A document with
two retention rules has none, so this is the one.

**Ticking an item changes only its marker and the date, never its text.** The body records
what was believed when the work was filed. Reflowing is not a change to it — the words stay
and only the line breaks move, which a tick makes necessary by lengthening the first line.
Where that reasoning was wrong in an instructive way, strike it and file the corrected item
beside it.

**A tick is paid for by the budget, never by trimming.** Ticking lengthens a first line and
costs a reflow, and with nothing leaving mid-roadmap the count only goes up — so raise the
budget with a one-line reason. Not on a threshold warning: ADR-0033 tried one and retired
it, because a warning with no remedy the reader can apply fires until it stops being read.

---

## Open — the roadmap to a server someone else can run

Milestones M0a to M7 closed on 2026-09-02 and are in
[archive/PLAN-milestones.md](archive/PLAN-milestones.md). These are queued now, each with
the one observable thing that ends it, ordered by what each item's annotation says it costs.

Most of what makes this server usable lives outside it, in a private CLAUDE.md and in
session memories that reach nobody else and are gated by nothing. M8 to M11 move that
knowledge into the server; M12 fixes the one thing those notes exist to work around.

### M8 — The server can say why it will not work

**Exit:** `--doctor` prints a pass or fail line per check and exits non-zero on any failure.

- ✅ 2026-09-07 `uv` is absent on this host, so `probe_toolchain_binds` probed and bound
  nothing — and `config.py` already called that "the single most likely first-run sandbox
  failure". Nothing checks the environment at startup, so it went undiagnosed for a session
  and was written up as an architectural limit instead
- ✅ 2026-09-07 Measure that the server really does start with a missing workspace root,
  no `bwrap` and a dead endpoint. Read from code rather than run, and it is the
  justification for the item below
- ✅ 2026-09-07 `--doctor`, one line per check: each root exists, `bwrap` runs a trivial command, the
  provisioned toolchain is current, the head node resolves by name *inside WSL*, every
  registry entry answers with `id_confirmed`, `transcript_dir` is writable, `cross_process`
  slots are live. Refuses to run outside WSL, where `bwrap` and DNS would answer wrongly.
  `sandbox.available()`, `limiter_available()` and `probe_entry()` exist, uncalled at startup
- ✅ 2026-09-08 `--init`, writing `.env` and `models.toml` from answers and *printing* the
  client registration it cannot write, and accepting a pasted Windows path wherever a path
  is asked
- ✅ 2026-09-07 `transcript_dir` skips `to_posix`, unlike `workspace_roots`, `toolchain_binds` and
  `sandbox_home`, so a Windows path in it has to be hand-converted to `/mnt/c` form
- ✅ 2026-09-07 One refused path in `files[]` kills the whole call. Prefetch what resolves, fill the
  `files_skipped` the reply already carries, and name the path and the root it missed

### M9 — A delegation can run the project's tests

**Exit:** a delegation runs this repository's suite in a workdir and the server captures a
real non-zero exit for a failing test and a real zero for a passing one.

Measured 2026-09-07 before any code: inside `bwrap --unshare-all`, a venv under
`sandbox_home` carrying this repository's dev dependencies and an editable install ran the
whole WSL suite — **1237 passed, 4 skipped, 1 deselected in 207s at exit 0**, and exit 1
with that test included. The criterion above is already satisfiable; only provisioning is left.

- ✅ 2026-09-08 `provision <project>` — build the interpreter and dev dependencies under
  `sandbox_home`, bound read-write, persistent and *outside* the workspace. Outside is
  mandatory rather than tidy: ADR-0041 records that inside a virtualenv `*secret*` and
  `*credential*` match ordinary library filenames and the scan covers each with
  `/dev/null`, breaking the environment it just read. It runs server-side, where the
  network is, so no `network: true` grant is needed — and `uv` is no substitute, binding
  only its binary and leaving its cache outside by design, so with no network it resolves
  nothing
- ✅ 2026-09-08 Hash the project's dependency declaration beside the provisioned venv and
  have `--doctor` report a mismatch, or a delegation tests stale dependencies and returns
  the clean exit code ADR-0007 says to trust
- ✅ 2026-09-08 Invoke the venv's interpreter by absolute path rather than changing
  `SANDBOX_PATH`, which is hardcoded to `/usr/bin:/usr/sbin`; the measurement needed no
  PATH change
- ✅ 2026-09-08 A per-project list of tests that cannot run nested. Measured:
  `test_network_is_reachable_by_address_when_shared` fails because `--unshare-all` denies
  the network it asserts, and without the list the exit condition above is unreachable
- ✅ 2026-09-08 Network stays off, with an ADR. `--share-net` re-shares the host's whole
  namespace with no allowlist or destination list, and this host reaches the cluster and
  the LAN
- ✅ 2026-09-08 **Spike answered** — weigh covering read-only, which would make a discarded
  write fail loudly instead of exiting 0. Measure first: `__pycache__` and `.pytest_cache`
  are on the same list and a test run writes to both, so read-only may break what this
  milestone exists to enable. Any change supersedes a line of ADR-0041
  - **Measured 2026-09-07: it is viable, and the timid version is unnecessary.** With eight
    directories covered by `--tmpfs` plus `--remount-ro`, a write into a covered path is
    refused with exit 1 while the same write into the workdir succeeds, and the full suite
    still passes — 1237 passed, 4 skipped, exit 0. Python tolerates an unwritable
    `__pycache__` and pytest degrades to a cacheprovider warning, so **both** the secret
    shadows and the bulk list can be read-only rather than only the former
  - The first probe asserted read-only against a directory it had not covered, and passed.
    It needed a control write into the workdir to show the assertion could fail at all
- ~~**`workdir` cannot verify Python work, which is the one thing it exists for.**~~ It binds a
  directory writable so `run_bash` can run what the delegation wrote, and ADR-0007's
  self-verification rests on real captured exit codes. There are none here: measured
  2026-09-07, no `python` on `PATH`, `import pytest` raises, `/tmp/vv` is absent because the
  WSL venv lives on the host's `/tmp`, and no network to install one.
  - **The cause rules out the obvious fix.** `.venv` is covered, not empty — a 64k `tmpfs`
    over it where the repository is `v9fs`, deliberately and by name (ADR-0035). Committing a
    Linux venv would be covered too, so a fix must use a path the cover-up does not name.
  - **And a write into a covered path exits 0 and is then discarded** — `touch .venv/probe`
    succeeds and is gone by the next `run_bash`, where a workdir write persists. ADR-0007
    says trust the captured exit; here it is 0 and wrong. Decide separately.

### M10 — Knowledge that travels with the package

**Exit:** on a host holding only the package, a caller can write a valid agent file for
their own project without ever reading this repository.

- ✅ 2026-09-07 Three facts are missing from the tool descriptions, which is the only channel the
  protocol delivers by itself: one question per delegating call, an unprefetched call is the
  dearest rather than the cheapest, and prefetch what is already known to be needed. A
  behaviour change with a CHANGELOG entry, not a wording fix
- ✅ 2026-09-09 An `@mcp.prompt` entry point carrying the orchestration discipline — sizing
  a pass by expected findings, two large calls at a time, splitting a multi-part ask. The
  server registers six tools and no prompts or resources at all
  - **Done as a resource, not a prompt, and the difference is the whole finding.** A prompt
    is user-controlled by specification, so a person must invoke one; a resource is pulled
    by the *model*. `delegate://orchestration` carries the discipline and costs nothing
    until read (ADR-0066). A prompt remains defensible later as a *user-facing* briefing,
    which is a different job and no longer this item's
- ✅ 2026-09-10 `install-skills`, shipping `write-delegate-agent`: it writes an agent file in this
  server's format and validates it by calling `list_agents` and checking the name lands
  under `agents` rather than `skipped` or `other_format`
- ✅ 2026-09-10 Move the caller-side half of `docs-audit-local.md` into a repository-local skill, so
  the agent body keeps only what one pass reads — the file says exactly that of the section
  itself. It stays local rather than shipping, because the check list, the ownership map and
  the gate integration are specific to this repository
- ✅ 2026-09-10 **Spike answered** — does Claude Code consume skills served over MCP through FastMCP's
  `SkillsDirectoryProvider`? If it does, that replaces `install-skills` outright
  - **Server half measured 2026-09-07: it serves them.** A `SkillsDirectoryProvider` over a
    directory holding one skill advertises the `resources` capability and lists two entries
    per skill under a `skill://` scheme — the `SKILL.md` and a `_manifest` — both readable
    through `resources/read`, plus one resource template
  - **Answered 2026-09-07: it does not, so `install-skills` stands.** Claude Code 2.1.263
    finds MCP skills through a paginated `skills/list`, gated on the server declaring
    `io.modelcontextprotocol/skills` under capability `extensions` and on a client flag off
    by default; FastMCP 3.4.7 declares `io.modelcontextprotocol/ui`, no skills key and no
    handler, so two gates fail before the flag is reached. Measured twice: the client binary
    against a negative control (`mcp__` 278 matches, `SKILL.md` 94, `skill://` 0), then live
    — provider wired in, client reconnected, `resources/list` returning both `skill://`
    entries while no skill was offered. Resources present and skill absent is the
    discriminator, so the null result is not a mis-wired provider
- ⬜ **Spike** — find the cause behind withholding `run_bash` on a verifying pass, rather
  than writing the workaround down. It took an audit from 26 turns to 1 at no cost in
  accuracy, and 24 of its 29 calls were verification — so if verification bought nothing,
  stop instructing the agent to verify by shelling out. Measure that first; a
  `verify_quote` tool is only the fallback
  - **Attempted 2026-09-07 and inconclusive, because the experiment was designed wrong.** A
    read-only pass has no shell, which is the condition under test, and one was run over a
    dense document for the TOO VERBOSE class. It correctly found nothing — so it produced no
    quotations, and there was nothing whose accuracy could be checked
  - The cost half did reproduce: **one turn, zero tool calls**, against the 26 turns the
    with-shell pass took. What is still unmeasured is whether accuracy holds, and measuring
    it needs a task that *necessarily* quotes — a STALE pass over a document and the code it
    describes, against a known discrepancy, rather than a class that may legitimately return
    an empty list

### M11 — A call you can watch, and a cluster you can see

**Exit:** the viewer shows a running delegation and live cluster figures on a host where
nothing was configured, and no tool result changes shape.

- ⬜ `transcript_dir` falls back to the server's own state directory when unset, so the
  viewer and the cost record work without setup. ADR-0024 already argues that what an
  operator can audit should not depend on the caller's flag
- ⬜ Split the running totals from the transcripts so retention and accuracy stop competing:
  an append-only ledger of one line per dispatch, never pruned, beside the fat per-dispatch
  records, which may be aged out
- ⬜ The ledger counts *cluster* tokens, which is a fact. Calling the number a saving assumes
  what Claude would otherwise have read, which is not measured — report the facts and state
  the assumption beside any saving
- ⬜ A sampler polling the metrics reader on an interval into a windowed series, because it
  derives only since-boot figures and a lifetime average cannot say how the cluster is doing
  now. `backend_status` keeps the output it has, so no client behaviour changes
  - **Raised 2026-09-11 from reporting to correctness, which re-ranks it.** The windowed rate
    is what the *reply budget* should be priced from: `DecodeRate` seeds from the since-boot
    mean and only a **completed** turn replaces it, so a delegation sized for an idle cluster
    that cannot then finish never corrects itself and dies at `stall_timeout` at zero turns.
    Seven did in one audit session, endpoint healthy throughout
  - `vllm:generation_tokens_total` is published and **not** in the allowlist; differenced over
    a window, over `num_requests_running`, it is a live per-request rate. The histogram in use
    records only on *completion*, so it is blind during the stall it must detect. And
    ADR-0055's "conservative" blend is flattering whenever the present is busier than history
- ✅ 2026-09-07 **Spike answered** — a `status` subcommand printing one plain-text block, since a TUI
  cannot run inside an agent's shell. Measure whether a detached terminal window can be
  launched from one; if not, print the command to paste
  - **Measured 2026-09-07: a detached launch works, so the fallback is not needed.**
    `wt.exe -- wsl.exe -d <distro> -e <cmd> <args>` opens a window, runs the command, and
    outlives the tool call that started it; the window closes when the command exits, so a
    long-running viewer persists
  - **The trap, which cost four wrong readings:** `wt.exe` consumes `;` as its own pane
    separator, so a compound command loses everything after the first statement and the
    fragments are launched as executables. Avoid `;` or escape it `\;`. Separately,
    `Start-Process -ArgumentList @(...)` against `wsl.exe` exits 0 and runs nothing at all,
    while the same arguments as one string work
  - And a lesson about the probe rather than the feature: a marker file written *first* in
    the command reported success while the tail was being mangled. Write the marker last
- 🔄 **Streaming, reopened 2026-09-05 with a scope.** **Slice 1 landed 2026-09-12 (#172,
  ADR-0070)**: the transport streams, `complete()` is unchanged, and the decode interval no
  longer contains prefill. Slices 2-4 remain, in this order — `stall_left` reset on token
  arrival, deltas feeding `stream.turn`, a partial returned at `dispatch_timeout`.
  Filed and cancelled the same day,
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
  - **Re-ranked below streaming on 2026-09-07.** `delegate_to_agent_readonly` removed the
    ramp for read-only work and the client backgrounds a long call by itself, so what is left
    is the ramp on the two write-capable tools and hiding the admission wait.

### M12 — Admission that queues instead of starving

**Exit:** a waiter parked on `kv_token_budget` is no longer overtaken indefinitely by later
smaller ones, shown by a test that reproduces the starvation first. Met 2026-09-11.

Needed before a second person shares the cluster, and worth having alone.

Raised by a documentation audit that was truncated by its own turn budget, then rerun. The
three settings below were each sized against a constraint that has since moved, and none of
them was re-derived when it did.

- 🔄 **Admission has no anti-starvation, and 2026-09-05's measurements make it
  sharper.** `_binding` refuses any waiter with `ahead > 0`, where `ahead` counts only
  earlier-ticketed waiters *that currently fit*. Deliberate — strict ticket order would
  reintroduce head-of-line blocking — but there was **no aging, no reservation, no barrier.**
  - **Barrier landed 2026-09-11, and the rule named here was the wrong one, measured.**
    `max_inflight_large_prefills` is held by other *large* requests, which queue by ticket,
    so a waiter on it is admitted the moment the blocker releases — bounded, not starvation.
    `kv_token_budget` is held by the newcomers themselves, so a waiter short of it is never
    feasible when they ask. Aging in `rival_fits`, ADR-0068. **`is_large` keeps this open.**
  - **What changed:** the eviction half was reasoned, not measured, and now can be. The
    KV pool is `kv_cache_size_tokens` from the endpoint's own metric, and
    `vllm:kv_cache_usage_perc` says how full it is, so "the prefix a starved request was
    queued to reuse gets evicted meanwhile" is now a question with an instrument. Decide
    after step 5 lands the reader, not before.
    - **Measured 2026-09-12, and the eviction worry is answered for this workload.** Read
      during a six-way fan-out with two passes actually queued: `kv_cache_used_fraction`
      **0.047**, `preemptions` **0**, `kv_cache_size_tokens` 1,467,988. Nothing was evicted
      because the pool was never near full — six passes at ~45k are about 19% of it, which
      is the figure the deferral below already predicted. So the eviction half does not
      block `is_large`, and `is_large` is what remains
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

### Unscheduled — open, real, and in no milestone

Neither queued nor deferred: real work not yet ranked against a milestone.

- ⬜ Content-level detection for a renamed secret — every path-policy layer inspects the
  path and none the bytes, so `config.json` holding a private key passes all of them and is
  inlined, and `run_bash` can read one the mount-level scan did not match by name. One
  finding, not two: fixing the detection fixes both ends. **Not** by pointing `scan_text` at
  it, which the 2026-09-02 review recommended — that scanner looks for RFC1918 addresses,
  private-DNS suffixes and non-allowlisted emails, and would false-positive on the source a
  review delegation exists to read. A narrow, high-precision check for key material instead

- ⬜ **A captured exit code of zero is not proof of success, and only the server can close
  that.** `last_bash_exit` is the status of the whole shell line the model composed, so a
  trailing `; echo $?` or a `| tail` replaces the status of the work with the status of the
  echo. Measured 2026-09-08 over four trials each: the `run_bash` description now says the
  code is recorded for you, which took a real non-zero from 0/4 to 3/4 — an improvement and
  not a guarantee, and a wording change can never be one. A non-zero is still trustworthy
  because nothing invents one; a zero is ambiguous, which is the half ADR-0007 needs.
  The server-side answer is a signal for *any* command in the line exiting non-zero, beside
  the last one's — and `/bin/sh` is dash here, so whether that can be had without changing
  what a compound command means is the thing to measure first

- ✅ 2026-09-09 **The denylist file matches itself, so layer 3 is unusable inside the sandbox.**
  `security/secret_globs.txt` matches its own `*secret*` entry, so the scan covers it with
  `--ro-bind /dev/null` — and inside the sandbox it is then a character device owned by
  `nobody`, which reads as `Permission denied` rather than as empty. Any nested run that
  exercises layer 3 fails: 42 of `test_tools.py` on 2026-09-08, and it is why this repository's
  own suite cannot run nested in full. It fails *closed*, so it is a usability bug and not a
  hole. Covering it protects nothing either way — the file holds patterns, not secrets, and is
  tracked in git — so the fix is to exempt the configured `secret_globs_file` and
  `opaque_globs_file` from being shadowed at all

- ✅ 2026-09-08 `models.toml` is not on the layer-3 denylist, though it names the host that
  `.env` is denied for. Found while covering `--init`'s backups on 2026-09-08:
  `.gitignore` and `NEVER_TRACK` both hold it, so it cannot be committed, but
  `security/secret_globs.txt` has no entry for it and so `read_file` will hand it to a
  delegated model. Adding one line fixes it and changes what a delegation may read, which
  is why it is an item rather than a detail of that commit

- ✅ 2026-09-10 **`.env.example` is covered by `.env.*`, so a tracked example file is unreadable.**
  Found 2026-09-09 by running this repository's suite nested for the first time, which the
  ADR-0065 fix made possible. Inside the sandbox `.env.example` is a character device owned
  by `nobody` and reads as `Permission denied`, while `models.toml.example` is readable —
  because the denylist names `models.toml` exactly and `.env.*` matches every suffix. That
  asymmetry contradicts `security/secret_globs.txt`'s own comment, which says the example
  file stays readable and is "the control on this pair". It fails closed, so it is the same
  usability class as ADR-0065 rather than a hole, and it is the one test still failing a
  nested run. Changing it changes what a delegated model may read, which is why it is an
  item and not a detail of another commit — the same reasoning the `models.toml` entry used

- ⬜ Globs in `files[]`, expanded server-side — a shorthand for naming many files, not a
  way to look for anything. Its original justification, that expanding before the call keeps
  `delegate_readonly` toolless and loopless, no longer holds now the fork is settled the
  other way, so this is a convenience and ranks below the search tool. The work is in the
  budget rather than the matching — a glob hitting two hundred files has to skip and account
  for them the way `context.prefetch` already does, not spend `prefetch_budget` silently

These came out of nine delegations dying at the stall deadline with zero turns. The
measurements, what each fix does and does not cover, and what the next session should start
with are in the hand-off notes — `~/.claude/plans/handoff-dispatch-budget.md`, untracked and
local, because they are working notes rather than a product fact.

- ✅ 2026-09-12 **The reply budget is priced while the cluster is idle and spent while it is busy.**
  Admission serialises a fan-out, so every sibling prices against a cluster that has not
  filled yet. Priced from admission's own counters since #169; a burst's *first* member
  still cannot know the burst is coming
- ⬜ **Hold a delegation briefly when the gate is idle**, so a burst's first member prices
  against the burst. ~~Buys the one case above; costs latency on every solo large call.
  Default off.~~ **Re-ranked 2026-09-12 and the "large" in the title was backwards**: six
  large calls are already coalesced by `max_inflight_large_prefills` — admitted in three
  waves of two, at 0s/28.9s/57.1s — while six *small* ones ran together and priced 1,2,3,4,5,6
  - **What it actually buys, which is more than a first member's ceiling.** `expect` keeps a
    minimum over every sample at that concurrency *or busier* precisely because the label
    cannot be trusted, and that costs a genuinely solo call 2.5x: dispatch 0027 decoded
    **alone at 65.6 tok/s** and is priced at 26.57. No guard fixes that — `expect(1)` is
    min-over-everything by design. A hold makes the label true, and a true label is the
    precondition for `expect` returning anything less pessimistic. **That is the item**
  - Hold only while the gate is idle, so it costs nothing when concurrency is already known
    and 10s when it is not. The arrival distribution measured that day is bimodal, which is
    what makes a fixed window work: six probes inside 8.5s, or one alone
- ⬜ **`RateHistory.expect` falls through to the since-boot blend when it has nothing at the
  asked concurrency**, which is the optimistic answer to the busier question. Measured
  2026-09-12: six probes priced from that fall-through at 34.85 tok/s and then decoded at
  27.1 six-way — a 1.29x overestimate, not the 1.8x recorded before the instrument was
  fixed. A 37,638-token ceiling still decodes inside an 1,800s turn at that rate, so the
  fall-through alone did not kill the fan-out at `effort: low`; the dying passes ran at
  `high`, and the memory is keyed on concurrency only, never on effort. That is the half
  that remains
  - **The other half was a different defect and shipped in #177.** "A low expectation
    searches widely and keeps the worst" is the design working, not the asymmetry: a
    six-way sample bounds a solo question from below, which is what already prices a
    burst's first member at the six-way floor. What made it look broken was that the
    memory had no floor of its own, so a 237-token turn reading 16.64 tok/s entered it and
    became permanent. Guarded at the memory; `expect` deliberately untouched
- ✅ 2026-09-11 **`turn_timeout` is absent from the ceiling's `min`**, so the budget
  authorises a reply one attempt cannot deliver (#166)
- ✅ 2026-09-12 **A generation overrun is retried as though it were a network blip**, with
  the same budget against a fraction of the clock (#167)
- ✅ 2026-09-12 **The heartbeat names a deadline that will not kill the turn**, reporting
  `dispatch_timeout` while a tighter one fires (#168)
- ✅ 2026-09-12 **The docs gate dies while printing a finding that is not pure ASCII** (#173).
  The hooks run under Git Bash, where stdout is cp1252, and the printing loop covers warnings
  and runs before the verdict — so a non-ASCII warning killed a commit that was about to pass.
  Invisible because the agent's own shell exports UTF-8
- 🔄 **The 2026-09-11 audit's remaining findings**: the `read_metrics` note that calls the
  blend conservative when it is flattering under load, five cross-plane duplications, and two
  unsourced claims in `docs/ARCHITECTURE.md`. The three runbook findings are closed — CLAIMS
  in #171, the concurrency remedy re-derived in #175, and the effort column deliberately left
  alone there because its premise reproduced only under the pricing defect #172 fixed
- ⬜ **`admission_wait_timeout` bails out after 30 minutes having produced nothing**, and
  its own help text says it was sized for an era when the queue was unordered. Tickets and
  the starvation barrier removed that premise and nobody re-derived the number. Fail fast
  on a queue too deep to serve, or do not fail at all
  - **It has now fired, twice, on 2026-09-12** — the first time on this deployment. Two
    passes of a six-way fan-out waited the full 1800s on `max_inflight_large_prefills` and
    were refused having produced nothing. Not latent. `admission_timeouts` reads 0 while
    waiters are still waiting because it counts *completed* waits, so that counter cannot
    be used to argue the bail-out is unreachable — it was, that morning, and wrongly
- ⬜ **Work that does not fit one turn.** Two turns produced 13,268 and 16,909 output
  tokens, the first needing 1,750s at 7.6 tok/s. No budget makes that fit an 1,800s
  attempt; it is a splitting problem, not a pricing one
  - **`reply_budget_margin` is the binding constraint, measured 2026-09-12, and was not
    filed as one.** A STALE pass wants ~23,700 output tokens. An 1,800s turn at the real
    six-way rate of 19.4 authorises `1800 x 19.4 x 0.6` = **20,952** — so the margin alone
    puts the task out of reach whatever the rate estimate does. Below it a pass returns
    empty at length; above it the clock cannot decode what was authorised. Re-derive the
    margin before splitting anything, or the split will be sized against the wrong number

## Deferred

On hold for weeks or months. Not cancelled, and not queued.

- ⬜ Anthropic-compatible adapter — the seam and canonical shape are kept so this is
  additive, roughly 150 to 220 lines in one new file (ADR-0008)
- ⬜ Packaging for other people, on hold until wanted — a real version and a publishable
  wheel, since `version` is `0.0.0` and installing means a clone; whether it travels as a
  wheel or a repository URL; and a version a colleague's `--doctor` can report
- ⬜ Cluster-wide queueing across machines. Admission counts one machine (ADR-0040) and
  cross-process slots need a POSIX lock, so two hosts coordinate not at all — each admits
  its own `max_inflight_seqs` and `max_inflight_large_prefills` against one endpoint
- ⬜ Per-user identity on the endpoint. `api_key_env` is empty, so there is no auth, no
  quota and no fair share
- ⬜ Server-format twins for the four Claude Code agents — `code-reviewer`, `docs-audit`,
  `researcher` and `test-writer` load only in Claude Code, so `delegate_to_agent` reaches
  one of five agents here. Deferred from M10 on 2026-09-10: the entry named
  `docs-audit-local` as the shape to copy, and `#159` split it into an agent plus a runbook,
  so that shape no longer exists. Re-derive what a twin is before committing four of them

## Cancelled

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
