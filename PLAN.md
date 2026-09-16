<!-- BUDGET: 840 -->
<!-- Raised from 838 on 2026-09-15: a cancelled item states its reason on the marker line, and the body it cancels is kept beside it. -->
<!-- Raised from 834 on 2026-09-15: a dead parameter found while measuring and nearly lost with the scratch files it was found in. -->
<!-- Raised from 831 on 2026-09-15: two of M11.5's sub-items argued from a premise RateHistory retired and one had the sign backwards; the corrections cost more than the lines. -->
<!-- Raised from 827 on 2026-09-15: a refusal costs a round trip, which is cheap in seconds and dear in turns, and that trade needs revisiting rather than remembering. -->
<!-- Raised from 823 on 2026-09-15: a scan cap spent inside a gitignored directory answers from nothing, and that is a correctness item rather than a speed one. -->
<!-- Raised from 820 on 2026-09-15: ADR-0076 exit condition checked at last and failed, so the item it was unproven against is filed. -->
<!-- Lowered from 930 on 2026-09-15: the three-line cap took 57 lines and the raise history stopped being a third copy of itself; slack a document has not earned is where the next accretion goes. -->
<!-- Earlier raises are in this file's git history, and each one's reason is in the
     CHANGELOG.md section for the pull request that made it. This opener is load-bearing:
     line 1 self-closes, so without a `<!--` here every line below would render as body
     text. Trimmed on 2026-09-06 for exactly this reason, then regrown from that trim to 27
     reasons and 64 lines in nine days -- so a trim does not hold on its own, and what is
     kept here is the six most recent raises rather than the history. -->
# Plan

Open work, status first so the file scans.

`✅` done, dated · `🔄` in progress · `⬜` not started — queued under **Open**, on hold
under **Deferred** · `❌` cancelled, with date and **reason** — cancelled items stay,
because the fact that something was considered and dropped is worth more than a tidy list.
A struck-through entry with **no marker** is the original of a ticked item, kept beside it
where its reasoning turned out to be wrong in an instructive way. It travels with its item
when that item is archived, so one left here belongs to something still open.

Items are numbered — `1.` at the top, `- a.` beneath, marker after it — so a plan cites
`M11.10` rather than a title. Ids are section-scoped and never reused; every sub-bullet takes
a letter and a marker (`✅` if it holds no task), and a parent is `✅` only once all are.

**Three lines a bullet**, measured to the first blank line, with sub-bullets carrying their
own three. History goes to whichever of CHANGELOG, DECISIONS, JOURNAL or the hand-off
notebook owns it. `✅` and `❌` bodies are frozen and therefore exempt.

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

1. ✅ 2026-09-07 `uv` is absent on this host, so `probe_toolchain_binds` probed and bound
  nothing — and `config.py` already called that "the single most likely first-run sandbox
  failure". Nothing checks the environment at startup, so it went undiagnosed for a session
  and was written up as an architectural limit instead
2. ✅ 2026-09-07 Measure that the server really does start with a missing workspace root,
  no `bwrap` and a dead endpoint. Read from code rather than run, and it is the
  justification for the item below
3. ✅ 2026-09-07 `--doctor`, one line per check: each root exists, `bwrap` runs a trivial command, the
  provisioned toolchain is current, the head node resolves by name *inside WSL*, every
  registry entry answers with `id_confirmed`, `transcript_dir` is writable, `cross_process`
  slots are live. Refuses to run outside WSL, where `bwrap` and DNS would answer wrongly.
  `sandbox.available()`, `limiter_available()` and `probe_entry()` exist, uncalled at startup
4. ✅ 2026-09-08 `--init`, writing `.env` and `models.toml` from answers and *printing* the
  client registration it cannot write, and accepting a pasted Windows path wherever a path
  is asked
5. ✅ 2026-09-07 `transcript_dir` skips `to_posix`, unlike `workspace_roots`, `toolchain_binds` and
  `sandbox_home`, so a Windows path in it has to be hand-converted to `/mnt/c` form
6. ✅ 2026-09-07 One refused path in `files[]` kills the whole call. Prefetch what resolves, fill the
  `files_skipped` the reply already carries, and name the path and the root it missed

### M9 — A delegation can run the project's tests

**Exit:** a delegation runs this repository's suite in a workdir and the server captures a
real non-zero exit for a failing test and a real zero for a passing one.

Measured 2026-09-07 before any code: inside `bwrap --unshare-all`, a venv under
`sandbox_home` carrying this repository's dev dependencies and an editable install ran the
whole WSL suite — **1237 passed, 4 skipped, 1 deselected in 207s at exit 0**, and exit 1
with that test included. The criterion above is already satisfiable; only provisioning is left.

1. ✅ 2026-09-08 `provision <project>` — build the interpreter and dev dependencies under
  `sandbox_home`, bound read-write, persistent and *outside* the workspace. Outside is
  mandatory rather than tidy: ADR-0041 records that inside a virtualenv `*secret*` and
  `*credential*` match ordinary library filenames and the scan covers each with
  `/dev/null`, breaking the environment it just read. It runs server-side, where the
  network is, so no `network: true` grant is needed — and `uv` is no substitute, binding
  only its binary and leaving its cache outside by design, so with no network it resolves
  nothing
2. ✅ 2026-09-08 Hash the project's dependency declaration beside the provisioned venv and
  have `--doctor` report a mismatch, or a delegation tests stale dependencies and returns
  the clean exit code ADR-0007 says to trust
3. ✅ 2026-09-08 Invoke the venv's interpreter by absolute path rather than changing
  `SANDBOX_PATH`, which is hardcoded to `/usr/bin:/usr/sbin`; the measurement needed no
  PATH change
4. ✅ 2026-09-08 A per-project list of tests that cannot run nested. Measured:
  `test_network_is_reachable_by_address_when_shared` fails because `--unshare-all` denies
  the network it asserts, and without the list the exit condition above is unreachable
5. ✅ 2026-09-08 Network stays off, with an ADR. `--share-net` re-shares the host's whole
  namespace with no allowlist or destination list, and this host reaches the cluster and
  the LAN
6. ✅ 2026-09-08 **Spike answered** — weigh covering read-only, which would make a discarded
  write fail loudly instead of exiting 0. Measure first: `__pycache__` and `.pytest_cache`
  are on the same list and a test run writes to both, so read-only may break what this
  milestone exists to enable. Any change supersedes a line of ADR-0041
    - a. ✅ **Measured 2026-09-07: it is viable, and the timid version is unnecessary.** With eight
    directories covered by `--tmpfs` plus `--remount-ro`, a write into a covered path is
    refused with exit 1 while the same write into the workdir succeeds, and the full suite
    still passes — 1237 passed, 4 skipped, exit 0. Python tolerates an unwritable
    `__pycache__` and pytest degrades to a cacheprovider warning, so **both** the secret
    shadows and the bulk list can be read-only rather than only the former
    - b. ✅ The first probe asserted read-only against a directory it had not covered, and passed.
    It needed a control write into the workdir to show the assertion could fail at all
7. ❌ 2026-09-08 ~~**`workdir` cannot verify Python work, which is the one thing it exists
  for.**~~ **Cancelled by the three items above:** a venv provisioned under `sandbox_home`
  ran this repository's whole suite offline at a real exit code, so the workdir does verify.
    - a. ✅ **Measured 2026-09-07, before provisioning:** no `python` on `PATH`, `import
    pytest` raises, `/tmp/vv` absent (the WSL venv is on the host's `/tmp`), no network to
    install one — so ADR-0007 had no captured exit to trust.
    - b. ✅ **The cause ruled out the obvious fix.** `.venv` is covered, not empty — a 64k
    `tmpfs` over it where the repository is `v9fs`, deliberately and by name (ADR-0035).
    Committing a Linux venv would be covered too, so a fix had to use an unnamed path.
    - c. ✅ **A covered write exits 0 and is discarded** — `touch .venv/probe` is gone by the
    next `run_bash` where a workdir write persists, so the exit ADR-0007 trusts was wrong.

### M10 — Knowledge that travels with the package

**Exit:** on a host holding only the package, a caller can write a valid agent file for
their own project without ever reading this repository.

1. ✅ 2026-09-07 Three facts are missing from the tool descriptions, which is the only channel the
  protocol delivers by itself: one question per delegating call, an unprefetched call is the
  dearest rather than the cheapest, and prefetch what is already known to be needed. A
  behaviour change with a CHANGELOG entry, not a wording fix
2. ✅ 2026-09-09 An `@mcp.prompt` entry point carrying the orchestration discipline — sizing
  a pass by expected findings, two large calls at a time, splitting a multi-part ask. The
  server registers six tools and no prompts or resources at all
    - a. ✅ **Done as a resource, not a prompt, and the difference is the whole finding.** A prompt
    is user-controlled by specification, so a person must invoke one; a resource is pulled
    by the *model*. `delegate://orchestration` carries the discipline and costs nothing
    until read (ADR-0066). A prompt remains defensible later as a *user-facing* briefing,
    which is a different job and no longer this item's
3. ✅ 2026-09-10 `install-skills`, shipping `write-delegate-agent`: it writes an agent file in this
  server's format and validates it by calling `list_agents` and checking the name lands
  under `agents` rather than `skipped` or `other_format`
4. ✅ 2026-09-10 Move the caller-side half of `docs-audit-local.md` into a repository-local skill, so
  the agent body keeps only what one pass reads — the file says exactly that of the section
  itself. It stays local rather than shipping, because the check list, the ownership map and
  the gate integration are specific to this repository
5. ✅ 2026-09-10 **Spike answered** — does Claude Code consume skills served over MCP through FastMCP's
  `SkillsDirectoryProvider`? If it does, that replaces `install-skills` outright
    - a. ✅ **Server half measured 2026-09-07: it serves them.** A `SkillsDirectoryProvider` over a
    directory holding one skill advertises the `resources` capability and lists two entries
    per skill under a `skill://` scheme — the `SKILL.md` and a `_manifest` — both readable
    through `resources/read`, plus one resource template
    - b. ✅ **Answered 2026-09-07: it does not, so `install-skills` stands.** Claude Code 2.1.263
    finds MCP skills through a paginated `skills/list`, gated on the server declaring
    `io.modelcontextprotocol/skills` under capability `extensions` and on a client flag off
    by default; FastMCP 3.4.7 declares `io.modelcontextprotocol/ui`, no skills key and no
    handler, so two gates fail before the flag is reached. Measured twice: the client binary
    against a negative control (`mcp__` 278 matches, `SKILL.md` 94, `skill://` 0), then live
    — provider wired in, client reconnected, `resources/list` returning both `skill://`
    entries while no skill was offered. Resources present and skill absent is the
    discriminator, so the null result is not a mis-wired provider

### M11 — A call you can watch, and a cluster you can see

**Exit:** the viewer shows a running delegation and live cluster figures on a host where
nothing was configured, and no tool result changes shape.

1. ✅ 2026-09-15 **`transcript_dir`'s fallback needs a state directory that does not exist yet, and the
  argument the item cites does not reach it.** **Decide the durability and the privacy
  question before writing the fallback.** Original:
    - a. ✅ `slots.default_dir()`, the only server-owned directory helper, is tmpfs in both
    branches — right for ephemeral slots, wrong for an audit record that must survive a
    reboot.
    - b. ✅ ADR-0024 argues an operator's audit should not depend on the *caller's* flag, where
    `transcript_dir` is the operator's, so it does not support defaulting this on. Doing so
    also writes task text to disk unchosen — "a trade rather than a rule", per its own help.
2. ❌ 2026-09-15 **Refused: it carries task text, which is the caller's material rather than the
  server's own behaviour, so it stays the operator's choice (ADR-0086).** Original:
  `transcript_dir` falls back to the server's own state directory when unset, so the
  viewer and the cost record work without setup. ADR-0024 already argues that what an
  operator can audit should not depend on the caller's flag
3. ⬜ Split the running totals from the transcripts so retention and accuracy stop competing:
  an append-only ledger of one line per dispatch, never pruned, beside the fat per-dispatch
  records, which may be aged out
4. ⬜ The ledger counts *cluster* tokens, which is a fact. Calling the number a saving assumes
  what Claude would otherwise have read, which is not measured — report the facts and state
  the assumption beside any saving
5. ⬜ **`RateHistory` answered most of this; the post-reboot cold start is what is left.**
  `seed_decode_rate` consults the memory first and reaches the since-boot mean only when
  nothing has been seen that busy — measured 2026-09-15, 0 of 94 `priced` events did
    - a. ✅ **Raised 2026-09-11 from reporting to correctness, which re-ranks it.** `DecodeRate`
    seeds from the since-boot mean and only a **completed** turn replaces it, so a delegation
    sized for an idle cluster never corrects itself and dies at `stall_timeout` at zero turns.
    - b. ✅ **Obsolete as filed.** The seven deaths it cites were priced from the since-boot
    mean, which a persisted per-concurrency memory replaced (ADR-0075). The reply budget is
    already priced from observations rather than from a lifetime blend.
    - c. ⬜ `vllm:generation_tokens_total` is published and **not** in the allowlist; differenced over
    a window, over `num_requests_running`, it is a live per-request rate. The histogram in use
    records only on *completion*, so it is blind during the stall it must detect.
    - d. ⬜ **Backwards as filed, and unmeasured.** `expect` takes a minimum, which is pessimistic.
    The blend is "conservative" only if history is at least as contended as the moment being
    priced, and nothing has measured that. A spike rather than an item.
6. ✅ 2026-09-07 **Spike answered** — a `status` subcommand printing one plain-text block, since a TUI
  cannot run inside an agent's shell. Measure whether a detached terminal window can be
  launched from one; if not, print the command to paste
    - a. ✅ **Measured 2026-09-07: a detached launch works, so the fallback is not needed.**
    `wt.exe -- wsl.exe -d <distro> -e <cmd> <args>` opens a window, runs the command, and
    outlives the tool call that started it; the window closes when the command exits, so a
    long-running viewer persists
    - b. ✅ **The trap, which cost four wrong readings:** `wt.exe` consumes `;` as its own pane
    separator, so a compound command loses everything after the first statement and the
    fragments are launched as executables. Avoid `;` or escape it `\;`. Separately,
    `Start-Process -ArgumentList @(...)` against `wsl.exe` exits 0 and runs nothing at all,
    while the same arguments as one string work
    - c. ✅ And a lesson about the probe rather than the feature: a marker file written *first* in
    the command reported success while the tail was being mangled. Write the marker last
7. ✅ 2026-09-14 **Streaming, reopened 2026-09-05 with a scope.** **Slice 1 landed 2026-09-12 (#172,
  ADR-0070)**: the transport streams, `complete()` is unchanged, and the decode interval no
  longer contains prefill. **Slices 2-3 landed 2026-09-13 (ADR-0072)**: `stall_left` resets
  on token arrival against a live ceiling, and the heartbeat carries what has arrived.
  **Slice 4's partial landed 2026-09-14 (ADR-0078)**: a failing turn carries what it decoded
  and a deadline returns it as `partial: true`. **What remains of slice 4**: the retry split,
  still deferred for its own evidence, and showing the stream itself. Narrower than it looked. Two
  populations were conflated: a *length stop* returns normally with its tokens already parsed
  into the response, fixed above without streaming; a *cancelled* turn leaves them in the
  adapter's accumulator, which is what this still owns. Since #172 they are no longer "only on
  the backend" as the scope note below says — they are in `_StreamAccumulator`, discarded on
  the exception path — plus the retry split
  streaming makes available (a read timeout *before* first token is prefill or queueing, *after* is
  slow decode; untouched in #172), and showing the stream itself. **Weigh a non-terminal
  viewer first**: `follow` never repaints and making it is the expensive half, where a
  browser over the same `.jsonl` gets repaint, scrollback and selection for nothing.
  Filed and cancelled the same day,
  2026-08-25, on the grounds that MCP tool calls are request/response so the caller sees
  nothing incrementally either way. That is still true of the caller and was never the
  whole picture: it never had its own ADR (ADR-0018 is about the progress notifications
  offered as the substitute), and it was annotated once during the ADR-0043 work as
  *"worth revisiting: the premise moved"* — because the cancellation weighed one consumer
  and there are two. The second is the transcript stream a person reads *while* a
  delegation runs.
    - a. ✅ **Lost work is the argument that was missing.** A task too big to finish a turn is
    abandoned at the tighter of `stall_timeout` and `turn_timeout`, and everything
    generated is discarded: `_decode()` needs the whole body as one JSON object, and
    `complete()` states it never returns a partial. The tokens exist only on the backend.
    Nor is the work re-done — `except TimeoutError` always raises, and retry covers only
    an unavailable or refusing backend.
    - b. ✅ **It makes the stall deadline honest, which beats returning partials.** ADR-0047 chose
    turn completion because every other signal was fake: the per-turn notification fires at
    the *top* of a turn and the keepalive is a timer, so both reset the clock on the very
    turn that wedged. Token arrival is real liveness. `stall_left` resetting on token
    arrival means a call producing tokens is never killed and a call producing nothing
    still dies — **this supplies the signal ADR-0047 lacked rather than contradicting it**,
    which is the argument that would supersede its heading.
    - c. ✅ **Scope, in order of value:** a `stream` key in `wire_body()`; an SSE accumulator
    behind the existing `complete()` contract, so *"never returns a partial"* stays true
    and the stall path reads the accumulator rather than `complete()`; `stall_left` reset
    on token arrival; deltas feeding `stream.turn`; and last, a partial returned at
    `dispatch_timeout`, marked partial so the calling conversation can decide whether to
    ask for the rest.
    - d. ✅ **Cheaper than it looks**, because the seam was preserved for it — `base.py` says SSE
    accumulation lives per adapter behind one contract, a method on the protocol rather
    than a shape baked into the caller. **Costly** because `_decode()` requires one whole
    JSON object, so there is no line-by-line path to extend, and the adapter's *"a single
    non-streaming call **is** the turn"* is a claim streaming invalidates.
    - e. ✅ **2026-09-05 supplied the evidence this was missing, and session 2 confirmed it.** Five
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
    - f. ✅ **Corrected:** something does bound a single turn — `max_tokens` — and it is set
      2.6x above what the deadline can decode, so the bound never binds before the kill
      does. That makes streaming a **liveness** fix rather than *the* fix: capping the
      budget stops the deaths, and `stall_left` resetting on token arrival is what stops
      a legitimately long turn being killed once the budget is honest. Keep both, in that
      order, and drop "only". The shape experiments that read as contradictory were
      measuring turn boundaries against a budget nobody had priced
    - g. ✅ **The trap, recorded with it:** token flow is not turn completion. The `alive`
    heartbeat must not report streamed tokens as liveness without the deadline change
    above, or it would call the 2026-09-04 stalls healthy. ~~Open question to measure: a
    model looping while emitting tokens is bounded by `max_tokens`, but whether a reasoning
    model's thinking tokens count against it is unknown here.~~ **Answered 2026-09-05
    (session 2): they do.** A turn whose entire visible answer was the word `DONE` reported
    `output_tokens: 697` at `effort: low`. So `max_tokens` does bound a looping model — it
    is simply set far above the deadline today.
8. ✅ 2026-09-15 **The retry split streaming made available, and half of it is already in.** The
  `httpx` read-timeout path still lumps prefill/queueing together with slow decode, and that
  is the half that remains.
    - a. ✅ A timeout *before* the first token is prefill or queueing; *after* it is slow decode.
    A slow decoder will likely be slow again, so retrying spends the budget discovering
    that, where a queueing timeout may sail through immediately.
    - b. ✅ **Half landed 2026-09-14 (ADR-0078):** the adapter's whole-turn bound reports
    `while_generating=first is not None` rather than an unconditional `True`, so that path
    distinguishes them.
    - c. ✅ Split out of streaming, where `openai_compat.py` deferred it as "would change what
    #167 retries, which wants its own evidence rather than arriving as a side effect" — so
    measure what each population costs before changing the retry rule.
9. ⬜ **Showing the stream itself, for a person watching a delegation run.** The `.jsonl`
  has carried everything needed since slices 2-3, and the shape was already settled: **a
  non-terminal viewer first**.
    - a. ⬜ `follow` never repaints and making it repaint is the expensive half, where a browser
    over the same `.jsonl` gets repaint, scrollback and selection for nothing.
    - b. ⬜ The consumer is not the caller — an MCP tool call is request/response either way — it
    is the person reading the transcript stream while the work happens.

10. ⬜ **A delegation returns a handle, and a second call collects it** — the 120s is when
  the client stops *waiting*, not when it issues the next call, so what is left to remove is
  the ramp on the two write-capable tools and the caller-visible admission wait.
    - a. ⬜ **The ramp is real for the write-capable tools only**, which is what re-ranked this
    below streaming on 2026-09-07: four `readOnlyHint` arms started within 5.6s, six
    `delegate_to_agent` arms chained at 120s with the last at +688s (`#118`, narrowed).
    - b. ✅ **Measured 2026-09-05:** four passes issued together started 20:24:30, 20:26:32,
    20:28:32, 20:30:32, and ran concurrently once started. `seqs=4, large=0` was *not*
    evidence of health — see the admission item for why that counter cannot see a fan-out.
    - c. ⬜ **The fan-out is worth more than the ramp, and handles do not change that.**
    `stall_timeout` is wall-clock per delegation, so the batch takes the gain while each run
    pays the penalty; a handle only stops it being caller-visible (JOURNAL 2026-09-05).
    - d. ⬜ **Server-side rather than a client setting**, deliberately. `MCP_TOOL_TIMEOUT` might
    shorten the ramp, but it is per-machine setup that does not travel, and whether it
    backgrounds or kills is untested with a severe failure mode.
    - e. ⬜ **Shape that keeps the common case cheap:** a short grace window returning inline if it
    finishes, otherwise a handle. `collect(handle, wait_seconds)` blocks just under the
    client's threshold, which costs nothing because the work is already running.
    - f. ⬜ Moves the admission wait behind the handle, so `admission_wait_timeout` stacking on
    `dispatch_timeout` stops being caller-visible wall time (ADR-0038).
    - g. ⬜ Restructures the model-facing tool contract, so it is a behaviour change with an ADR.
    **Related to streaming but not blocked on it** — streaming is token-level liveness
    inside a turn, this is call-level detachment. Say so in the ADR.

### M12 — Admission that queues instead of starving

**Exit:** a waiter parked on `kv_token_budget` is no longer overtaken indefinitely by later
smaller ones, shown by a test that reproduces the starvation first. Met 2026-09-11.

Needed before a second person shares the cluster, and worth having alone.

Raised by a documentation audit that was truncated by its own turn budget, then rerun. The
three settings below were each sized against a constraint that has since moved, and none of
them was re-derived when it did.

1. ✅ 2026-09-13 **Admission has no anti-starvation, and 2026-09-05's measurements make it
  sharper.** `_binding` refuses any waiter with `ahead > 0`, where `ahead` counts only
  earlier-ticketed waiters *that currently fit*. Deliberate — strict ticket order would
  reintroduce head-of-line blocking — but there was **no aging, no reservation, no barrier.**
    - a. ✅ **Barrier landed 2026-09-11, and the rule named here was the wrong one, measured.**
    `max_inflight_large_prefills` is held by other *large* requests, which queue by ticket,
    so a waiter on it is admitted the moment the blocker releases — bounded, not starvation.
    `kv_token_budget` is held by the newcomers themselves, so a waiter short of it is never
    feasible when they ask. Aging in `rival_fits`, ADR-0068. **`is_large` keeps this open.**
    - b. ✅ **What changed:** the eviction half was reasoned, not measured, and now can be. The
    KV pool is `kv_cache_size_tokens` from the endpoint's own metric, and
    `vllm:kv_cache_usage_perc` says how full it is, so "the prefix a starved request was
    queued to reuse gets evicted meanwhile" is now a question with an instrument. Decide
    after step 5 lands the reader, not before.
    - c. ✅ **Measured 2026-09-12, and the eviction worry is answered for this workload.** Read
      during a six-way fan-out with two passes actually queued: `kv_cache_used_fraction`
      **0.047**, `preemptions` **0**, `kv_cache_size_tokens` 1,467,988. Nothing was evicted
      because the pool was never near full — six passes at ~45k are about 19% of it, which
      is the figure the deferral below already predicted. So the eviction half does not
      block `is_large`, and `is_large` is what remains
    - d. ✅ **`is_large` closed 2026-09-13** (#184). Re-deriving it per turn is moot: the only rule
      that reads it for a *decision* is the gate the measurement condemned, so the question
      leaves with the setting rather than being answered on its own
    - e. ✅ **What is settled:** the related worry that `max_inflight_large_prefills = 2` trades
    cache hits for pipelining is **answered and dead.** Three concurrent large prefills
    over a shared prefix cost the same as three serial ones and hit cache identically, and
    the engine runs one at a time, touching two only at the handoff — which is exactly
    what "one running plus one staged" describes. Keep the setting and the value.
    - f. ✅ **What session 2 adds: `is_large` is decided once and never revisited.** `admit()` reads
    `prefill_tokens` from the *opening* estimate and holds the lease for the whole
    delegation, so an audit opening under `large_prefill_tokens` is filed as small for life
    — while later turns re-prefill 40-70k each and the slots file still reads `large: 0`.
    The rule that exists to serialise large prefills has never been tested by the workload
    it was written for, and the same staleness understates `kv_token_budget`. **The eviction
    item above is what makes a delegation's prefill grow**, so fix that first and re-measure;
    re-deriving `is_large` per turn is cheap but may then be unnecessary

2. ✅ 2026-09-13 **`kv_token_budget` is 1.64x the real KV pool, and the number to fix it is
  now readable.** The setting defaults to 2,400,000 and its help text says it "sits just under
  the measured KV pool". The endpoint reports `kv_cache_size_tokens = 1,467,988`, re-read
  2026-09-13, so it sits well over. Nothing has failed, because the setting protects latency rather than
  correctness — over-admitting queues and preempts rather than erroring — which is exactly
  why it drifted unnoticed. Likely cause is the 2026-09-04 model swap: a vision model
  carries more weights, so less memory is left for KV and the pool shrank underneath a
  constant measured against the old model.
    - a. ✅ **Do not fix it with a new constant.** An `.env` override is right today and drifts on
    the next swap; changing the default bakes one deployment's hardware into the
    repository. Derive it from what `backend_status` now reads, the way `WindowCheck`
    already derives per process — a server guessing at a figure the cluster publishes is
    the whole argument of the 2026-09-05 session.
    - b. ✅ Deferred deliberately on 2026-09-05: our delegations run ~45k tokens, so six of them
    is 19% of the pool and the gap is not currently reachable.
- **Correction to the ticked item above**, filed beside it rather than edited into it:
  `WindowCheck` **validates and never derives**, and says why, so it is not the precedent
  that entry claims. What allows this is narrower — the two numbers are ceilings on the same
  physical thing, and the lower of two ceilings overrules neither (#192)

### Unscheduled — open, real, and in no milestone
Neither queued nor deferred: real work not (yet) ranked against a milestone.

1. ⬜ **Spike** — find the cause behind withholding `run_bash` on a verifying pass, rather
  than writing the workaround down. It took an audit from 26 turns to 1 at no cost in
  accuracy, and 24 of its 29 calls were verification. A `verify_quote` tool is the fallback.
    - a. ✅ **Attempted 2026-09-07 and inconclusive: the experiment was designed wrong.** A
    read-only pass has no shell, which is the condition under test, and it ran over a dense
    document for TOO VERBOSE — correctly finding nothing, so nothing could be checked.
    - b. ⬜ The cost half did reproduce: **one turn, zero tool calls** against 26. Whether accuracy
    holds is still unmeasured, and needs a task that *necessarily* quotes — a STALE pass
    against a known discrepancy, not a class that may legitimately return an empty list.
2. ✅ 2026-09-13 **Reasoning was generated, paid for, and then discarded** — nine dispatches of 446
  reported empty at a *length stop* holding 265,092 output tokens, because `answer` joined text
  blocks only. Bannered now, with `answer_is_reasoning` (#188)
3. ✅ 2026-09-14 **Eviction and dedup undo each other** — a stubbed 34KB result is handed straight back on the
  next identical call, plus the turn spent asking. Neither half can see the other
4. ⬜ **Eviction is sized in tokens and cannot see what a result cost** — it will drop a 657s read
  to save a few thousand
5. ⬜ **A delegation's budget pays for server-side tool time** — 1,135.7s of 1,271.7s, 89.3%,
  cluster idle throughout (2026-09-13). Feeds the third-liveness-state item
6. ✅ 2026-09-14 **The M10 spike is misfiled and stale** — it does not move M10's exit, and the workaround it
  meant to avoid writing down is now in CLAUDE.md. Its accuracy half is still unmeasured
    - a. ✅ Re-filed rather than run. M10's exit is about a caller on a host holding only the
    package writing a valid agent file, and withholding `run_bash` from a verifying pass
    moves none of it. It stays open as an **Unscheduled** item, where its accuracy half —
    a task that *necessarily* quotes, against a known discrepancy — can be measured on its
    own merits instead of being paid for out of a milestone it does not serve
7. ✅ 2026-09-14 **The docs gate measures the last append-only entry a line short** — length runs to the next
  heading, so the newest entry omits its trailing separator and an over-budget one lands, then
  blocks whoever appends next. Found by being that next person
8. ✅ 2026-09-14 **Nothing stops a conflict marker reaching `main`.** Two did, in Markdown, where no test or
  lint looks and the gate checks ownership and budgets rather than content. One line in the
  gate; the nested shape is the one to write the negative test against
9. ✅ 2026-09-13 **Eviction fired at 4% of the window against a 50% gate** — the pressure check
  was gated on a setting that ships off, so it stubbed on count alone. It now applies wherever
  the window was declared, and `keep_tool_results` is 16 (JOURNAL 2026-09-13, #191)
10. ✅ 2026-09-14 **A retained *count* is the wrong unit for a history.** One run's 36 results ranged from
  200 bytes to 50,068, so `keep_tool_results` prices a one-line refusal and a 50KB file
  identically. A share of the window is the unit; the count becomes its floor
    - a. ✅ **Corrected 2026-09-14, twice, before any code was written.** *A share of the window
    cannot be the unit*: the denominator is `entry.context_window`, which is a silent
    131,072 default whenever `models.toml` omits the key — `context_window_defaulted` exists
    precisely to say so, and it is why `evict_upto` consults `share()` only as a gate today.
    The numerator is part estimate as well. The unit that is always known is the **retained
    bytes themselves**, with the count as the floor
    - b. ✅ *And the selection must not change*: eviction is prefix-ordered on purpose (ADR-0056),
    because a boundary that moves costs the cache everything after it. So this is a
    size-based **boundary** — advance the oldest-first cut until retained bytes fit a budget
    — never size-based *selection*. "Evict the large ones and keep the small ones" would
    trade the whole prefix cache for a few thousand tokens
11. ⬜ **Arming the preventive half is unmeasured.** Tighten, nudge, abort and the plateau
  check still wait for `context_overflow_enabled`. Arming them everywhere failed 31 tests on
  doubles reporting a 7-token prompt.
    - a. ⬜ That says those doubles are unrealistic, not that the change is wrong, and settling
    which needs a real delegation rather than an argument.
12. ✅ 2026-09-13 **The priced rate climbed past what the cluster can physically decode** — 88.53
  tok/s and a 95,612-token ceiling against a benchmark of 44.1. ADR-0070's move to
  `decode_seconds` inverted the short-turn defect ADR-0071 had fixed in only one of the two
  estimators; one shared floor now, and the cost is tested rather than hidden (ADR-0073, #181)
13. ✅ 2026-09-15 **`rate_source` names where the *seed* came from, not the number beside it.** It is set once
  in `__init__`, so `observed_at_concurrency` can label an EMA no observation at it produced
14. ⬜ **A quoting turn measures the accept path, not the decoder, and no floor catches it.**
  Measured 2026-09-13 at one concurrency: quoting a prefetched file read 63.75 tok/s against
  41.83 for generated prose and a 44.1 benchmark, from a turn clear of ADR-0073's floor.
    - a. ⬜ `expect`'s minimum contains it — immune to *fast* samples, vulnerable only to slow ones.
15. ✅ 2026-09-13 **An unscoped `search_files` cost 490-572s, and the contract recommended it** —
  `glob` claimed to be the speed lever and the bad-`path` refusal said "omit it to search
  everywhere", which one delegation did, at 239s. Scope is worth ~100x; `read_file` was never
  the problem. Schema, result note and refusal all fixed (ADR-0074, #182)
16. ✅ 2026-09-15 **A turn's independent tool calls run serially.** `_run_calls` holds one thread to preserve
  result order, but returned order need not be executed order and reads do not affect each
  other. `run_bash` stays ordered; the work is locking `cached` and `watch`
17. ✅ 2026-09-13 **Scoping was asked for and never shown** — `path` is required with an
  `_unscoped_` escape, and the description carries the workspace layout, folders and files
  (ADR-0076, #189). Unproven until a delegation's *first* search names a subdirectory
18. ✅ 2026-09-16 **The path policy is 65-75% of a search and it is all `lstat`.** Profiled: 80.7s in
  26,942 `lstat` (13.5 a candidate -- `realpath` re-walks shared prefixes), 37.1s in 4,000
  `stat` (`isfile` then `exists`), 20.5s in 403 `check-ignore`, matching 1.1%
19. ⬜ **The deadline counts down while the *server* works on the delegation's behalf.** A
  third liveness state ADR-0072 does not name: producing, silent, and producing nothing on
  the wire because a tool is running.
    - a. ⬜ Measured 2026-09-13 — 505s of one turn with `chunks_seen` frozen and `ends_in_seconds`
    falling 60s per minute, then jumping back on completion. The event loop is *not* blocked
    (`_run_calls` goes through `asyncio.to_thread`), so this bounds the delegation only.
    - b. ✅ **Corrected 2026-09-14: "killed while working" is false; this is a reporting defect.**
    `turn_done` sets `last_progress` unconditionally after the tool batch returns and
    `stalled()` is evaluated at the next dispatch, so tool time cannot kill by stall.
    - c. ⬜ What it does consume is the whole-delegation `dispatch_timeout`, a wall clock that
    arguably should. What is left is real but smaller — a watcher is told a delegation is
    dying while it works. **Re-rank accordingly.**
20. ✅ 2026-09-13 **The viewer states true things in ways that read false.** Four findings, one
  branch:
  `requests_running` in a `priced` row is the lease's grant-time concurrency echoed, not live
  cluster state, and the viewer renders it as "N running"; a turn boundary collapses into one
  displayed second, so a budget line attaches to the wrong turn (turn 15's end and turn 16's
  pricing were 5ms apart); the state column is 9 wide against a 2-wide gutter, so "queued 38s"
  has no room and "queued 120s" overflows — count minutes past 59s; and a queued delegation
  paints a row per second, which should be once a minute plus one line when it ends
21. ✅ 2026-09-13 **Two more the branch above found, filed here rather than rewriting it** — a
  selected row carrying an emoji over-padded and wrapped, because `len()` counts characters
  and a cell is not one; and a cut-off reply read as a finished one, `ok` being true of both.
  Its "queued 120s" is impossible and was corrected in the CHANGELOG, not in its body
22. ✅ 2026-09-16 **The registry's `concurrency` default is 5** while this deployment's `models.toml` sets 6. Carried out of the session hand-off notes, which are not a document anyone else reads
23. ✅ 2026-09-13 **An empty `name:` is accepted where the document says it is refused.**
  `agents.py` line
  330 reads `if declared and declared != name`, so a bare `name:` with no value is falsy and
  treated as absent, while `docs/AGENTS.md` says a `name` that is present "must equal the
  filename, or the file is refused". Either the code distinguishes present-but-empty from
  absent, or the document stops promising it does — the second is cheaper and probably right.
  Found by a STALE pass during the 2026-09-12 verification fan-out
    - a. ✅ **Closed the other way, 2026-09-13** (#186). The code moved, not the document: the
    unknown-key refusal eleven lines above calls a silently-ignored setting "the bug this
    format was rewritten to prevent", and a bare `name:` is that shape. Refusing costs
    nothing — a matching `name` is redundant and a disagreeing one was already refused
24. ⬜ Content-level detection for a renamed secret — every path-policy layer inspects the
  path and none the bytes, so `config.json` holding a private key passes all of them, and
  `run_bash` can read one the mount-level scan did not match by name.
    - a. ⬜ One finding, not two: fixing the detection fixes both ends. A narrow, high-precision
    check for key material.
    - b. ⬜ **Not** by pointing `scan_text` at it, as the 2026-09-02 review recommended — that
    scanner looks for RFC1918 addresses, private-DNS suffixes and non-allowlisted emails,
    and would false-positive on the source a review delegation exists to read.

25. ⬜ **A captured exit code of zero is not proof of success, and only the server can close
  that.** `last_bash_exit` is the status of the whole shell line the model composed, so a
  trailing `; echo $?` or a `| tail` replaces the work's status with the echo's.
    - a. ⬜ Measured 2026-09-08 over four trials each: the `run_bash` description now says the code
    is recorded for you, taking a real non-zero from 0/4 to 3/4 — an improvement, and a
    wording change can never be a guarantee.
    - b. ⬜ A non-zero is still trustworthy because nothing invents one; a zero is ambiguous, which
    is the half ADR-0007 needs.
    - c. ⬜ The server-side answer is a signal for *any* command in the line exiting non-zero
    beside the last one's. `/bin/sh` is dash here, so measure first whether that can be had
    without changing what a compound command means.

26. ✅ 2026-09-09 **The denylist file matches itself, so layer 3 is unusable inside the sandbox.**
  `security/secret_globs.txt` matches its own `*secret*` entry, so the scan covers it with
  `--ro-bind /dev/null` — and inside the sandbox it is then a character device owned by
  `nobody`, which reads as `Permission denied` rather than as empty. Any nested run that
  exercises layer 3 fails: 42 of `test_tools.py` on 2026-09-08, and it is why this repository's
  own suite cannot run nested in full. It fails *closed*, so it is a usability bug and not a
  hole. Covering it protects nothing either way — the file holds patterns, not secrets, and is
  tracked in git — so the fix is to exempt the configured `secret_globs_file` and
  `opaque_globs_file` from being shadowed at all

27. ✅ 2026-09-08 `models.toml` is not on the layer-3 denylist, though it names the host that
  `.env` is denied for. Found while covering `--init`'s backups on 2026-09-08:
  `.gitignore` and `NEVER_TRACK` both hold it, so it cannot be committed, but
  `security/secret_globs.txt` has no entry for it and so `read_file` will hand it to a
  delegated model. Adding one line fixes it and changes what a delegation may read, which
  is why it is an item rather than a detail of that commit

28. ✅ 2026-09-10 **`.env.example` is covered by `.env.*`, so a tracked example file is unreadable.**
  Found 2026-09-09 by running this repository's suite nested for the first time, which the
  ADR-0065 fix made possible. Inside the sandbox `.env.example` is a character device owned
  by `nobody` and reads as `Permission denied`, while `models.toml.example` is readable —
  because the denylist names `models.toml` exactly and `.env.*` matches every suffix. That
  asymmetry contradicts `security/secret_globs.txt`'s own comment, which says the example
  file stays readable and is "the control on this pair". It fails closed, so it is the same
  usability class as ADR-0065 rather than a hole, and it is the one test still failing a
  nested run. Changing it changes what a delegated model may read, which is why it is an
  item and not a detail of another commit — the same reasoning the `models.toml` entry used

29. ⬜ Globs in `files[]`, expanded server-side — a shorthand for naming many files, not a
  way to look for anything. A convenience ranking below the search tool, now the fork is
  settled the other way and the toolless-`delegate_readonly` justification no longer holds.
    - a. ⬜ The work is in the budget rather than the matching: a glob hitting two hundred files
    has to skip and account for them the way `context.prefetch` already does, not spend
    `prefetch_budget` silently.

These came out of nine delegations dying at the stall deadline with zero turns. The
measurements, what each fix does and does not cover, and what the next session should start
with are in the hand-off notes — `~/.claude/plans/handoff-notebook.md`, untracked and
local, because they are working notes rather than a product fact.

30. ✅ 2026-09-12 **The reply budget is priced while the cluster is idle and spent while it is busy.**
  Admission serialises a fan-out, so every sibling prices against a cluster that has not
  filled yet. Priced from admission's own counters since #169; a burst's *first* member
  still cannot know the burst is coming
31. ✅ 2026-09-15 **Hold a delegation briefly when the gate is idle**, so a burst's first member prices
  against the burst. ~~Buys the one case above; costs latency on every solo large call.
  Default off.~~ **Re-ranked 2026-09-12; the "large" in the title was backwards.**
    - a. ✅ **Re-evidenced 2026-09-15; the 65.6 figure it cited was withdrawn 2026-09-12.** Over 94
    `priced` events `expect` returned **10.95** at concurrency 1, 2 and 3 alike, 16.09 at 4,
    20.98 at 5 -- a rate rising with contention, and **4.0x** pessimistic against 44.1 solo.
    - b. ✅ **Bucketing without the hold was tried 2026-09-15 and reverted.** A burst's first member
    is labelled 1 and decodes at six-way, so trusting the label prices it at 44 to decode at
    19 -- the fatal direction. The hold comes first; a regression test already says so.
    - c. ✅ Hold only while the gate is idle, so it costs nothing when concurrency is already known
    and 10s when it is not. The arrival distribution measured that day is bimodal, which is
    what makes a fixed window work: six probes inside 8.5s, or one alone.
    - d. ✅ **The coalescing evidence this rested on died with #194; the item did not.** That gate
    is gone (ADR-0077), so large calls now arrive uncoalesced exactly as small ones do,
    which *widens* what a hold covers. The mechanism never depended on the gate.
32. ✅ 2026-09-13 **`RateHistory.expect` falls through to the since-boot blend when it has
  nothing at the asked concurrency**, which is the optimistic answer to the busier question. Measured
  2026-09-12: six probes priced from that fall-through at 34.85 tok/s and then decoded at
  27.1 six-way — a 1.29x overestimate, not the 1.8x recorded before the instrument was
  fixed. A 37,638-token ceiling still decodes inside an 1,800s turn at that rate, so the
  fall-through alone did not kill the fan-out. **Both of those figures came from a task that
  reproduced its own prompt and are withdrawn** — see the tick below. Against the rates that
  stand, the cold start is not marginal but decisive: the blend reads 34.96 where six
  concurrent delivers just under **20 tok/s**, so it is **1.75x** optimistic, and the 37,756
  tokens it authorises need **1,888s of an 1,800s turn**. Two of four died there at zero
  turns. An honest rate prices 20,952 — which is itself below what a STALE pass needs, so
  the rate and the margin have to be fixed together or neither helps. That is what remains
    - a. ✅ **Persisting the memory across restarts would have saved all four** at no cluster cost,
    unlike a synthetic warm-up, because the samples are real work at real concurrency and
    effort. `slots.default_dir()` is tmpfs — wrong for an audit record, right for a rate —
    and a `served_model_id` stamp discards it on a swap rather than a constant
33. ✅ 2026-09-12 **Every rate measured from a synthetic probe is an artefact, and the cause is
  confirmed.** Seven probes reproducing README.md verbatim read 65.6 tok/s solo and 27.1 at
  six concurrent — faster at six than the 23 measured at four, which cannot be true of one
  decoder, because contention only slows. The served model has a speculative-decoding module
  attached, and copying in-prompt text is close to a best case for acceptance. **A benchmark
  task must not be answerable from its own prompt**; the figures that stand are the owner's,
  44.1 solo, ~23 at four and just under 20 at six
    - a. ✅ **The other half was a different defect and shipped in #177.** "A low expectation
    searches widely and keeps the worst" is the design working, not the asymmetry: a
    six-way sample bounds a solo question from below, which is what already prices a
    burst's first member at the six-way floor. What made it look broken was that the
    memory had no floor of its own, so a 237-token turn reading 16.64 tok/s entered it and
    became permanent. Guarded at the memory; `expect` deliberately untouched
34. ✅ 2026-09-11 **`turn_timeout` is absent from the ceiling's `min`**, so the budget
  authorises a reply one attempt cannot deliver (#166)
35. ✅ 2026-09-12 **A generation overrun is retried as though it were a network blip**, with
  the same budget against a fraction of the clock (#167)
36. ✅ 2026-09-12 **The heartbeat names a deadline that will not kill the turn**, reporting
  `dispatch_timeout` while a tighter one fires (#168)
37. ✅ 2026-09-12 **The docs gate dies while printing a finding that is not pure ASCII** (#173).
  The hooks run under Git Bash, where stdout is cp1252, and the printing loop covers warnings
  and runs before the verdict — so a non-ASCII warning killed a commit that was about to pass.
  Invisible because the agent's own shell exports UTF-8
38. ✅ 2026-09-13 **The 2026-09-11 audit's remaining findings**: the `read_metrics` note that
  calls the
  blend conservative when it is flattering under load, five cross-plane duplications, and two
  unsourced claims in `docs/ARCHITECTURE.md`. The three runbook findings are closed — CLAIMS
  in #171, the concurrency remedy re-derived in #175, and the effort column deliberately left
  alone there because its premise reproduced only under the pricing defect #172 fixed
    - a. ✅ **Closed 2026-09-13** (#185). The note is corrected with the number that refutes it;
    three duplications resolved by making `CLAUDE.md` delegate rather than restate, and one
    by `README.md` linking. **Two findings were themselves wrong** — both "unsourced" claims
    are sourced, in `PLAN.md` and `CHANGELOG.md`, and the audit missed them by searching for
    its own phrasing. The fifth duplication is a deliberate non-fix: ARCHITECTURE's copy is
    in a `BUDGET` comment justifying a past raise, which is a record, not competing prose
39. ✅ 2026-09-14 **The rate memory surviving a reconnect switched off the KV-pool reading, so the
  token budget is 1.63x the pool again.** `seed_decode_rate` returns early when the history
  remembers a rate for this concurrency, and `on_pool` is called *after* that return — so
  the scrape that reports `kv_cache_size_tokens` to admission only ever ran when the memory
  was cold. ADR-0075 made it warm on every reconnect and thereby retired #192 without
  touching it. Measured 2026-09-14 after nine delegations on a freshly reconnected server:
  every dispatch priced `observed_at_concurrency`, `kv_cache_size_tokens_seen` **null**, and
  `kv_token_budget_effective` **2,400,000** against a reported pool of **1,467,988**. The
  fix is to report the pool before the early return, or to scrape regardless; the trap is
  that the better the rate memory gets, the less often the pool is seen. Also worth a
  negative test that the *pairing* holds, since each half passes its own tests today
40. ⬜ **`admission_wait_timeout` bails out after 30 minutes having produced nothing**, and
  its help text says it was sized for an era when the queue was unordered — a premise
  tickets and the starvation barrier removed, with nobody re-deriving the number.
    - a. ✅ **Re-measured 2026-09-14: no reachable path on this workload.** A five-wide fan-out of
    ~40k-token prefills, `peak_inflight_seqs` 5, `peak_inflight_tokens` 498,392, and
    `admission_wait_count`, `admission_wait_seconds_total` and `queued_waiters` all **0**.
    - b. ⬜ So the question is no longer what the number should be but whether the bail-out has any
    purpose left. `peak_inflight_seqs` is the honest gauge: `admission_timeouts` counts
    *completed* waits and reads 0 both when nothing waits and when everything still is.
    - c. ✅ **It had fired twice on 2026-09-12**, the first time on this deployment — two passes of
    a six-way fan-out waited the full 1800s on `max_inflight_large_prefills` and were
    refused having produced nothing. Not latent, that morning.
    - d. ✅ **Then twice more that evening with two passes answering**, killing the theory that a
    working budget dissolves it: a 2-wide gate holding each slot for a whole delegation.
    3,600s of waiting at `kv_cache_used_fraction` **0.031**, 0 preemptions, idle cluster.
    - e. ⬜ Every wait it ever fired on was on that gate, and the gate is gone (ADR-0077, #184),
    which is what makes the 2026-09-14 reading the current word. **Re-rank on that**, not on
    the 09-12 firings, and measure the item below before moving 1800.
41. ✅ 2026-09-13 **Release the *large* half of an admission lease at first token, not at the
  end of the run.** `admit()` holds it for the whole delegation, and the prefill it serialises
  is over once decoding starts. Measured 2026-09-12 at `effort: high`: time to first token
  **64.1s**, delegations running 271-847s, and the two that died holding a slot for 2,100s —
  4x to 33x longer than the work it protects, which is what made two passes wait out
  `admission_wait_timeout` behind a limit of 2 on a cluster at 3% KV
    - a. ✅ **Only reachable since ADR-0070**, which made first-token arrival observable
    - b. ✅ **It probably retires `max_inflight_large_prefills` rather than competing with it.** Once
    a slot is held only while a request is prefilling, the number held at any moment is the
    number the *engine* is prefilling — one running plus one staged — so a limit of 6 can
    never bind and even 2 would rarely. The setting goes inert, which is a cleaner answer
    than tuning it and is why both belong in one ADR
    - c. ✅ **A slot is taken on the estimate, not on the work, measured 2026-09-12.** At limit 2,
    four of six calls waited 28.9s and 57.1s for a large-prefill slot *while doing no cold
    prefill at all* — the prefix cache served 98% of each. `is_large` reads `prefill_tokens`
    from the opening estimate, which cannot know that. Hit rate here is 59%, and a fan-out
    over shared documents is the shape that triggers it most reliably
    - d. ✅ **Measured properly 2026-09-12, over twelve disjoint cold-prefill sets: the gate is
    pure overhead here.** Limit 2 against 6 — 4 of 6 queued against none, 286.3s of
    aggregate waiting against zero, batch 12.1s slower, each call 10.2s slower, nothing
    bought. Limit 6's span is the floor the engine sets by serialising prefills itself, and
    limit 2 cannot beat it. **So the setting is a candidate for removal, not retuning**, and
    that decision belongs in the same ADR as the first-token release above — a slot released
    at first token may make the question moot either way
42. ✅ 2026-09-13 **Remove `max_inflight_large_prefills`, or find the fan-out where it earns
  its keep.**
  Carried out of the ticked item above, whose body is frozen. 2026-09-13, three delegations
  on the first-token release: the gate never bound, and the one still holding a slot after
  two minutes was held by the *engine* queueing its prefill, not by the rule. Evidence of
  redundancy, not a decision — three calls on one machine is not the case that matters, so
  re-run it wider at the shipped limit of **2**. **`admission_wait_timeout` and M12's
  `is_large` both wait on this**: 1,800s is re-derivable only once the gate that made it
  reachable is settled, and `is_large` is read by this rule alone
    - a. ✅ **Answered 2026-09-13, and the answer is remove** (#184). Two arms of six over disjoint
    cold sets: limit 2 queued 4 of 6 for 217.9s; limit 6 queued none and finished 10.6s
    sooner, at 0.034 KV and zero preemptions either way. "Inert at 6" is confirmed; "even 2
    would rarely" is not. Inert where set, harmful at the shipped default — so no value earns
    its keep
43. ✅ 2026-09-13 **Remove the setting the measurement condemned.** Five consumers, one a
  decision: the
  `_binding` rule; ADR-0072's early release, which goes vestigial; `rival_fits`'s ordering;
  the server instructions and `delegate://orchestration`, making it a contract change with
  an ADR; the cross-process counter and two gauges. `peak_inflight_large_prefills` counts
  *leases*, never prefills the cluster ran — the engine queues those itself
- **Correction to the ticked item above**, filed beside it rather than edited in:
  `large_prefill_tokens` does not survive as that entry assumed. It classified for the rule
  and for nothing else, so it went with it (ADR-0077, #194)
44. ⬜ **Work that does not fit one turn** — the splitting premise was refuted by this
  entry's own evidence (#204). What remains is the `reply_budget_margin` half, which is
  arithmetic rather than an instrument reading and still binds.
    - a. ⬜ **7.6 tok/s was an arithmetic artefact, not a rate:** one attempt's tokens over two
    attempts' wall time. Its concurrent sibling produced more tokens in 663s. Transcript
    rows, both turns and the independent corroboration are in #204.
    - b. ⬜ **`reply_budget_margin` is the binding constraint, measured 2026-09-12.** A STALE pass
    wants ~23,700 output tokens; an 1,800s turn at the six-way 19.4 tok/s authorises
    `1800 x 19.4 x 0.6` = **20,952**, so the margin alone puts the task out of reach.
    - c. ⬜ An intermediate reading of 27.1 tok/s concluded it fitted at 28,698. That came from
    probes reproducing their own prompt against a speculative-decoding module, so it was an
    artefact; the owner's benchmark puts six concurrent just under 20, restoring 19.4.
    - d. ✅ **What the re-derivation settled:** the denominator is `turn_timeout`, not the
    `stall_timeout` the help text named, and "a turn also prefills" is wrong twenty-fold —
    prefill measured ~2% of a turn.
    - e. ⬜ The 0.6 is untouched because the *rate* was wrong. It would need to be about 0.57 for
    the cold-start ceiling to fit, and fitting a constant to a wrong rate is the mistake
    this roadmap already records against `kv_token_budget`.
45. ✅ 2026-09-15 **A `path` naming a whole workspace root is refused.** Required and
  supplied since ADR-0076, then satisfied with the root: 8 of 9 first searches named a root
  or the sentinel and 1 named a subdirectory. The refusal carries its children (ADR-0082)

46. ✅ 2026-09-16 **A gitignored directory eats the scan cap, so the search answers from nothing.**
  `_search_candidates` prunes symlinks and secrets, never gitignore, so a root walk spends
  all 2000 on `.venv` and returns 2 lines where 351 exist. Prune in the walk, never admit

47. ✅ 2026-09-16 **A refused root costs a turn, and a turn is ~20% of a short delegation.**
  ADR-0082 trades wall-clock for a round trip, which is cheap against 391.8s and dear against
  a five-turn budget. Revisit once 18 and 46 land and an unscoped walk is no longer ruinous

48. ✅ 2026-09-16 **`prefill_tokens` is threaded through `acquire` and `admit` and read by nothing.**
  It existed only to enforce `max_inflight_large_prefills`, removed with that gate (ADR-0077).
  Every caller still computes and passes an estimate the predicate never sees

## Deferred

On hold for weeks or months. Not cancelled, and not queued.

1. ⬜ Anthropic-compatible adapter — the seam and canonical shape are kept so this is
  additive, roughly 150 to 220 lines in one new file (ADR-0008)
2. ⬜ Packaging for other people, on hold until wanted — a real version and a publishable
  wheel, since `version` is `0.0.0` and installing means a clone; whether it travels as a
  wheel or a repository URL; and a version a colleague's `--doctor` can report
3. ⬜ Cluster-wide queueing across machines. Admission counts one machine (ADR-0040) and
  cross-process slots need a POSIX lock, so two hosts coordinate not at all — each admits
  its own `max_inflight_seqs` and `max_inflight_large_prefills` against one endpoint
4. ⬜ Per-user identity on the endpoint. `api_key_env` is empty, so there is no auth, no
  quota and no fair share
5. ⬜ Server-format twins for the four Claude Code agents — `code-reviewer`, `docs-audit`,
  `researcher` and `test-writer` load only in Claude Code, so `delegate_to_agent` reaches
  one of five agents here.
    - a. ⬜ Deferred from M10 on 2026-09-10: the entry named `docs-audit-local` as the shape to
    copy and `#159` split it into an agent plus a runbook, so that shape no longer exists.
    Re-derive what a twin is before committing four of them.

## Cancelled

1. ❌ 2026-09-05 A batch returns nothing until its slowest item settles — **wrong when
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

2. ❌ 2026-08-25 Run Claude Code inside WSL — cancelled on workflow grounds, not
  engineering ones. It would delete the path-translation module outright and remove the
  12x test penalty. ADR-0002 keeps the trigger: if development moves onto Linux for
  independent reasons, revisit immediately. ADR-0020

3. ❌ 2026-08-25 Dedicated Linux box beside the cluster — cancelled. Solves sandboxing but
  the workspace would reach it only over a share, a sync tool, or a clone, each worse
  than the local bridge and each adding a failure the bridge does not have. ADR-0020

4. ❌ 2026-08-25 Scheduled docs-audit workflow — cancelled. It needs an API key, which is
  standing billing exposure for a job that fires whether or not anything changed, and a
  calendar measures the wrong thing. Replaced by the gate's `audit-due` signal

5. ❌ 2026-08-25 Collapse reasoning effort to three levels — cancelled. Saves one enum
  value, does not shrink the state machine, and would make our API disagree with the
  backend's documented values. ADR-0013
