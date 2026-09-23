<!-- BUDGET: 420 -->
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

Milestones M0a to M7 closed on 2026-09-02. M8, M9, M10 and M12, M11's closed items and
every ticked Unscheduled item followed on 2026-09-23. All of them are in
[archive/PLAN-milestones.md](archive/PLAN-milestones.md), with their ids.

M13 to M18 come from the [2026-09-22 review](docs/reviews/review-2026-09-22.md), cited by its
own ids, R1 to R26 and P1 to P7. M19 and M20 hold what M11 left open. Ordered security,
correctness, contract, cost, housekeeping, then the two features waiting on a viewer.

### M13 — A delegation cannot reach past the path policy

**Exit:** every route around the path policy that the review found is refused, each shown
by a test that took the route first.

1. ✅ 2026-09-23 **`read_git` returns what the path policy refuses** (R21). `show <rev>:<path>`, `diff`,
  `blame` and blob ids reach denylisted, gitignored and unlisted content, history included.
  Revisions must be commits, and paths go through layers 2 and 3
2. ✅ 2026-09-23 **Subprocesses inherit the server's stdin, which is the MCP stream** (R1), so a command
  that reads stdin eats protocol frames. `stdin=DEVNULL` in `sandbox.run`, `paths._git`,
  `doctor.py` and `provision.py`, as `tools._run_git` already does
3. ✅ 2026-09-23 **The gitignore layer fails open on git errors** (R10): any exit 128 reads as "not
  ignored". Only git's own "not a git repository" means outside; anything else refuses
4. ✅ 2026-09-23 **Two path checks answer "does it exist" before "is it allowed"** (R11):
  `resolve_search_root` and `resolve_workdir`, where `_resolve_one` does the reverse
5. ⬜ **`search_files` cannot see suffix-less names that `read_file` allows** (R12), such as
  `Makefile` and `.gitignore`, so a search reports absent what a read would show
6. ⬜ **Spike: what the sandbox's write access must not reach, and how host-side git runs**
  (R3, R4, R5). A short design note before the three items below, which is CONTRIBUTING's
  rule for `sandbox.py` and `paths.py`
7. ⬜ **The read-write workdir reaches files the host acts on** (R3): `.claude/` settings,
  agents and skills, `CLAUDE.md`, editor task files. Bind them read-only in the sandbox and
  refuse them in the write tools
8. ⬜ **Host-side git trusts repository config a delegation could have written** (R4). Pass
  hardening `-c` overrides on every host-side git call, and pin them with a test
9. ⬜ **`provision` runs project-controlled build steps on the host** (R5). Show the change to
  the dependency declaration since the last build, or refuse an uncommitted one
    - a. ⬜ The old environment is deleted before the new build succeeds: build aside, rename.
    - b. ⬜ `HASH_SOURCE` is only `pyproject.toml`, so a `setup.cfg` change goes unseen.
    - c. ⬜ A project without `pyproject.toml` builds but can never be current.
    - d. ⬜ An `OSError` from `rmtree` or the record write surfaces as a traceback.

### M14 — A cancel stops its own work, and nobody else's

**Exit:** cancelling a delegation stops its request on the cluster, shown by the endpoint's
running count, and fails no sibling that joined its burst wait.

Items 1 and 5 were Unscheduled.59 and Unscheduled.69, moved unchanged on 2026-09-23.

1. ✅ 2026-09-23 **A cancelled delegation keeps the cluster working, and admission stops seeing it.**
  The gate read `inflight_seqs: 0` against a cluster still running six, so a full burst is
  admitted on top of work nobody waits for. Wanted: a cancel that reaches the request (JOURNAL)
    - a. ✅ 2026-09-22 **Spike first, the lag is unmeasured:** wall clock of the kill against the transcript's `end`
    - b. ✅ **The cause is `_until_deadline`** (R2, and the review's P6): an outer cancel leaves its
    inner task streaming, so the HTTP stream never closes. Cancel and await it on every exit
    that is not a normal return.
2. ⬜ **Cancelling a burst wait's opener fails every sibling that joined it** (R18). Its
  `CancelledError` is stored in the shared future, and each joiner re-raises it as its own
3. ⬜ **Two openers in one process can orphan a burst wait** (R19): `_holding` is read, awaited
  across, then written, so whoever joined the first future is never settled
4. ⬜ **One `admit` call has no `SlotsUnavailable` fallback** (R20), so a lock held too long
  fails the delegation instead of falling back to per-process counting
5. ⬜ **A burst flag stranded by a failed close outlives its usefulness.** `_announce_burst`
  is best effort, and a live record is kept by liveness rather than by `_is_idle`, so other
  processes read an open wait nobody holds until that process exits or next goes idle

### M15 — The contract says what the server does

**Exit:** no model-facing text names an argument, key or behaviour the server lacks, and a
test fails when an agent or skill body names one.

1. ✅ 2026-09-23 **Agent and skill bodies carry stale tool facts** (R8): `docs-audit-local`, the
  `docs-audit-dispatch` skill, `code-reviewer`, `docs-audit` and `researcher`, each with a
  sentence the review quotes against the code that contradicts it
2. ⬜ **Tool descriptions name what does not exist** (R8): an `ok: true` in the `task`
  argument and the orchestration resource, and "a workspace root is refused" for
  `search_files`
3. ⬜ **Nothing checks that agent and skill bodies name real tool arguments** (R8). Test the
  names and their required-ness against the declared schemas, so a rename cannot strand one
4. ⬜ **The turn-limit banner fires on a run that finished** (R9): `hit_turn_limit` is
  `turn == turns`, and the banner claims more than that flag knows
5. ⬜ **Progress notifications are not monotonic** (R7): heartbeats send `progress(0, 0)`
  between turns. One rising counter per call, no unknown `total`, the words in `message`
6. ⬜ **Tool annotations are left to defaults** (review §5): no `title`, and the two writing
  tools do not state `destructiveHint` or `openWorldHint`
7. ⬜ **Three caller rules live only in the operator's memory**: narrow a verifying pass to
  reading, bound a delegation with `max_turns`, and expect writing calls to be serialised.
  Their home is `delegate://orchestration`
8. ⬜ **The read-only tools cannot narrow their own toolset**, so a pass limited to
  `read_file` needs `delegate`, a writing tool. Accept an `allowed_tools` that can only
  narrow within the read-only set
9. ⬜ **Server-format agent files are also loaded as Claude Code subagents, with every tool**
  (R6), because both read `.claude/agents/`. A directory of their own, the old one read for a
  release, and the shipped skill updated
    - a. ⬜ A Claude Code file in the project tier hides a valid personal agent of the same name.

### M16 — Cluster time goes to answers

**Exit:** each lever is measured on the same multi-turn tasks before and after, both arms
counted the same way, and kept only where it wins.

Item 2 was Unscheduled.62, moved unchanged on 2026-09-23.

1. ⬜ **The model can be shown its earlier reasoning, and is not** (R25, corrected). Measured
  2026-09-23: with `tools` in the request the endpoint renders it, 338 to 619 prompt tokens;
  without them it does not, and that is all the review's probe sent
    - a. ⬜ A/B `resend_reasoning` on fixed tasks, then set its default and fix its description.
    - b. ⬜ Visible working notes, only if 1.a loses: one static sentence in the system prompt.
2. ⬜ **Reasoning effort may be binary here, and the recovery ladder is built on it not being.**
  Measured only under 1k tokens, where the three looked alike; at a realistic size all of them
  spend the budget reasoning. Our four map onto the encoder's three plus off, so none is missing
    - a. ⬜ The review's per-level figures are not controlled (P1). Run the same tasks at `off`,
    `low` and `high`; `off` was twelve times cheaper at the median.
3. ⬜ **A reasoning-token budget at the server** (P2). ADR-0017 found it refused on the build
  then deployed. Re-check the current one: a cap would cut the tail and most runs that end
  with no answer
4. ⬜ **Price from the bucket median, not the mean** (review §9): a few prefill-window
  samples pull a bucket 4 to 9% low. Agreed with the operator; the widening rule stays
5. ⬜ **The priced label is frozen at admission, and the real cap is not recorded** (R24).
  Re-read concurrency from the shared totals at every turn, and carry the `max_tokens` sent
6. ⬜ **Speculative decoding is on, and its gain when six-wide is unmeasured** (P3,
  corrected: the endpoint already drafts six tokens). Measure acceptance at one and six
  streams before changing the draft length
7. ⬜ **Long prompts may decode slower** (P5): 29 against 44 tok/s at one stream, from five
  and six samples. Benchmark it before prefetching less
8. ⬜ **Width past six is unmeasured** (P7), and aggregate still rises at six. Measure
  eight-wide with real prompt sizes before raising `max_inflight_seqs` and the endpoint's
9. ⬜ **Topology** (P4): tensor parallelism spans both machines, an all-reduce every decode
  step. Compare one replica per machine behind one endpoint, if a quantised copy fits
10. ⬜ **`analyse_transcripts.py concurrency` understates the cluster by about a third**
  (review §9): it spreads a turn's tokens over its prefill too. Use `out_tok_s` or retire it
11. ⬜ **A watcher cannot tell thinking from answering** (review §9): the `alive` event counts
  chunks of both. The accumulator knows which, so split the count

### M17 — Skills follow the Agent Skills specification

**Exit:** every skill here and in the package meets the specification's hard limits under a
gate check that fails on a planted violation, and each description has passed a trigger eval.

Sources: the [specification](https://agentskills.io/specification),
[best practices](https://agentskills.io/skill-creation/best-practices) and
[optimising descriptions](https://agentskills.io/skill-creation/optimizing-descriptions).

1. ⬜ **Nothing checks a skill against the specification's hard limits**: `name` format and
  directory match, `description` at most 1,024 characters, body under 500 lines. All five
  pass today, so the negative test plants a violation
2. ⬜ **`docs-audit-dispatch` loads its whole runbook every time** (review §5). Keep the
  steps and gotchas in `SKILL.md`; move the check-class definitions and sizing evidence to
  `references/`, each with when to read it
3. ⬜ **No description has been tested for triggering.** Rewrite each as "Use when…", then a
  trigger eval: about twenty queries, half near-misses, three runs each, a fixed 60/40 split
    - a. ⬜ Start with the shipped `write-delegate-agent`, the one skill other people load.

### M18 — Code and docs describe what is, and records own the rest

**Exit:** the rules below each have one home, an AST check proves every comment pass
changed no code, and the gate warns on a date or a TODO in a `src/` comment.

1. ✅ 2026-09-23 **The owner's answers to the review's four questions** (review §4). Comments
  and product docs describe what is; history goes to JOURNAL and CHANGELOG, a design's
  reason to an ADR, a TODO to this file. The explanations are shortened too
    - a. ✅ Scope is `src/` and `docs/`, `Config` descriptions included. CHANGELOG, JOURNAL,
    DECISIONS, PLAN, `archive/`, `docs/audits/` and `docs/reviews/` own history and are exempt.
    - b. ✅ A BUDGET header holds only its number, and a raise's reason goes in the commit. A
    new ADR needs a structural decision; a tuned value's reason is a JOURNAL measurement.
2. ⬜ **Each rule gets one home and the rest point at it**: CONTRIBUTING.md for comments, the
  ADR bar and budget raises, and JOURNAL's header admits a tuning measurement. CLAUDE.md,
  the session-plan skill, this file, ARCHITECTURE and the gate's messages link
    - a. ⬜ The gate counts header lines, so each budget drops to its file's new size.
3. ⬜ **An AST check makes a comment pass provably behaviour-free**: each module's `ast.dump`
  with docstrings stripped is identical before and after. Negative-tested on one literal
4. ⬜ **One comment pass per module, largest first** (review §4)
    - a. ⬜ `loop.py`, which has more prose than code.
    - b. ⬜ `server.py`.
    - c. ⬜ `sandbox.py`, cutting history but keeping its security reasoning.
    - d. ⬜ `paths.py`, likewise.
    - e. ⬜ `tools.py`.
5. ⬜ **Product docs describe the current state**: `docs/` without its history, and each
  `Config` description cut to what the setting does plus a link. Some run to 250 words
6. ⬜ **Stop it regrowing**: the gate warns on a date, a `TODO` or future-work phrasing in a
  `src/` comment, and reports each module's prose ratio without blocking

### M19 — A browser viewer, and the ledger it reads

**Exit:** the viewer shows a running delegation and live cluster figures on a host where
nothing was configured, and no tool result changes shape.

M11's exit, carried. Items 1 to 3 were M11.3, M11.4 and M11.9, moved unchanged on
2026-09-23, to be picked up with the browser viewer.

1. ⬜ Split the running totals from the transcripts so retention and accuracy stop competing:
  an append-only ledger of one line per dispatch, never pruned, beside the fat per-dispatch
  records, which may be aged out
2. ⬜ The ledger counts *cluster* tokens, which is a fact. Calling the number a saving assumes
  what Claude would otherwise have read, which is not measured — report the facts and state
  the assumption beside any saving
3. ⬜ **Showing the stream itself, for a person watching a delegation run.** The `.jsonl`
  has carried everything needed since slices 2-3, and the shape was already settled: **a
  non-terminal viewer first**.
    - a. ⬜ `follow` never repaints and making it repaint is the expensive half, where a browser
    over the same `.jsonl` gets repaint, scrollback and selection for nothing.
    - b. ⬜ The consumer is not the caller — an MCP tool call is request/response either way — it
    is the person reading the transcript stream while the work happens.
### M20 — A call returns a handle

**Exit:** a delegation longer than the client's window returns a handle, and a second call
collects the answer with no caller-visible ramp or admission wait.

Item 1 was M11.10, moved unchanged on 2026-09-23.

1. ⬜ **A delegation returns a handle, and a second call collects it** — the 120s is when
  the client stops *waiting*, not when it issues the next call, so what is left to remove is
  the ramp on the two write-capable tools and the caller-visible admission wait.
    - a. ✅ 2026-09-19 **The ramp is real for the write-capable tools only**, which is what re-ranked this
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
4. ⬜ **Eviction is sized in tokens and cannot see what a result cost** — it will drop a 657s read
  to save a few thousand. **Its example died 2026-09-16:** the 657s read was the unscoped
  search, now ~4.6s (#219, #220). Tokens landed in #199 — the *cost* half is what remains
5. ❌ 2026-09-19 **Premise collapsed — a 0.5% median, not 89.3%**, re-measured over 27 dispatches
  and 55 turns; max 72.7% and one above 20%, so the tail is real and the rule is not, and the
  search that made the figure is #219 (JOURNAL 2026-09-19). ~~budget pays for tool time~~
11. ⬜ **Arming the preventive half is unmeasured.** Tighten, nudge, abort and the plateau
  check still wait for `context_overflow_enabled`. Arming them everywhere failed 31 tests on
  doubles reporting a 7-token prompt.
    - a. ⬜ That says those doubles are unrealistic, not that the change is wrong, and settling
    which needs a real delegation rather than an argument.
14. ⬜ **A quoting turn measures the accept path, not the decoder, and no floor catches it.**
  Measured 2026-09-13 at one concurrency: quoting a prefetched file read 63.75 tok/s against
  41.83 for generated prose and a 44.1 benchmark, from a turn clear of ADR-0073's floor.
    - a. ❌ 2026-09-16 **Disproved: a minimum is immune to a fast sample only while its bucket
    holds a slower one, and a bucket of one *is* that sample (JOURNAL).** Original: `expect`'s
    minimum contains it — immune to *fast* samples, vulnerable only to slow ones.
    - b. ⬜ **Both constant-free ceilings failed 2026-09-16:** the engine's windowed aggregate is
    too loose (63.75 hides under an 80 tok/s total) and its per-request mean refuses a
    faster-than-average stream. Observe the window against real turns before choosing a figure
    - c. ⬜ **`expect(1)` = 60.590 on 2026-09-18 against a 44.1 solo benchmark**, alone in its
    bucket. `reply_budget_margin` absorbs it: a ceiling fails only above 1.667x, so it would
    take 73.5 tok/s. Latent, not live — and a quoting turn may genuinely decode this fast.
19. ⬜ **The deadline counts down while the *server* works on the delegation's behalf.** A
  third liveness state ADR-0072 does not name: producing, silent, and producing nothing on
  the wire because a tool is running.
    - a. ⬜ Measured 2026-09-13 — 505s of one turn with `chunks_seen` frozen and `ends_in_seconds`
    falling 60s per minute, then jumping back on completion. The event loop is *not* blocked
    (`_run_calls` goes through `asyncio.to_thread`), so this bounds the delegation only.
    - b. ✅ **Corrected 2026-09-14: "killed while working" is false; this is a reporting defect.**
    `turn_done` sets `last_progress` unconditionally after the tool batch returns and
    `stalled()` is evaluated at the next dispatch, so tool time cannot kill by stall.
    - c. ⬜ What it consumes is the whole-delegation `dispatch_timeout`, which arguably it should.
    **Re-ranked down 2026-09-19**: 19.a's 505s was the unscoped search, now ~4.6s, and the median
    dispatch spends 0.5% of its wall time in tools, so the watcher is told rarely and briefly
25. ⬜ **A captured exit code of zero is not proof of success, and only the server can close
  that.** `last_bash_exit` is the status of the whole shell line the model composed, so a
  trailing `; echo $?` or a `| tail` replaces the work's status with the echo's.
    - a. ⬜ Measured 2026-09-08 over four trials each: the `run_bash` description now says the code
    is recorded for you, taking a real non-zero from 0/4 to 3/4 — an improvement, and a
    wording change can never be a guarantee.
    - b. ⬜ A non-zero is still trustworthy because nothing invents one; a zero is ambiguous, which
    is the half ADR-0007 needs.
    - c. ✅ 2026-09-19 The server-side answer is a signal for *any* command in the line exiting non-zero
    beside the last one's. `/bin/sh` is dash here, so measure first whether that can be had
    without changing what a compound command means.
    - d. ⬜ **The `| tail` half is still open**, and `pipefail` is not the way: measured
    2026-09-19 it marks `grep <absent> | head` and `yes | head -1` as failures. The `ERR`
    trap covers the sequence shape only, so the count undercounts by design (ADR-0095).

70. ⬜ **`files[]` takes whole files, so a large one is refused rather than sampled.**
  `CHANGELOG.md` is 182,236 tokens against the 140,000 cap and comes back in
  `files_skipped`, where a range was all anyone wanted. The file's own numbers, not the range's
71. ⬜ **`test_tool_time_is_the_tools_and_not_the_queue` is load-sensitive under the full
  WSL suite.** Four failures on 2026-09-22, each passing alone and on re-run; the fourth
  was a commit changing no code at all, which is what rules out every change as its cause
72. ⬜ **A first turn that runs tools still charges the dispatch's setup to them.**
  `tool_clock` starts at the grant, so turn 1's window holds budget pricing and request
  assembly as well as the tools. Needs the loop to time each call, where the number is
73. ⬜ **No type checker runs in CI** (R13), and one would have caught `complete_with_retry`
  declaring two return values where it returns three. Pyright or mypy in basic mode, `src/`
74. ⬜ **Synchronous file work blocks the event loop** (R14): `expand_globs`, `resolve_files`
  and `prefetch` run inline in `run_delegation`, 0.54 to 0.62s measured. Tolerable today
75. ⬜ **Documentation defects a user would hit** (R15)
    - a. ⬜ `README.md` puts a Windows path in JSON with single backslashes, illegal escapes.
    - b. ⬜ ARCHITECTURE lists "no standalone CLI" as a non-goal and documents the `run` CLI.
    - c. ⬜ ARCHITECTURE nests "Read-only tools" as an H2 inside another H2.
    - d. ⬜ A `slots.py` comment still reasons about the cap ADR-0077 removed.
76. ⬜ **Build and supply chain** (R16)
    - a. ⬜ GitHub Actions are pinned by tag rather than by commit.
    - b. ⬜ No lock file, so CI resolves every dependency fresh.
    - c. ⬜ Local runs include `integration` tests, which the marker says are skipped.
77. ⬜ **The transcript viewer trusts line shapes** (R17): a JSON line that is not an object
  takes down `summarise` and `follow`, and `follow` piped to another program crashes
78. ⬜ **The split-dodge check cannot fire in CI** (R22): it reads `git diff --cached`, and a
  CI checkout stages nothing. Use the `--diff` range the other checks use
79. ⬜ **The gate's `run()` turns a failed git command into an empty answer** (R23), so a check
  built on it passes having looked at nothing. Raise on a non-zero exit where empty is clean

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
