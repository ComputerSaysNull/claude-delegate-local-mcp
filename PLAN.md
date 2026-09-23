<!-- BUDGET: 1021
     Raised from 1017 (+1 for this line) on 2026-09-22: gating tool time on the calls closed the visible half, and the half it did not close needs saying or the next reader reads the gate as the whole fix.
     Raised from 1006 (+1 for this line) on 2026-09-21: a defect found while reviewing the
     cross-process burst flag has no other home, and an unfiled bug is one nobody ranks. -->
<!-- Raised from 997 (+1 for this line) on 2026-09-21: the deadline thread was proposed twice on 2026-09-20 and written down nowhere, and the question it ends with -- whether the reply budget need exist -- outranks two items already in this list. -->
<!-- Raised from 990 (+1 for this line) on 2026-09-20: read_file cannot bound a range and the sampler is off the evaluated pair, both of which cost every delegation; and a forced answer has been called clean in three audit records running. -->
<!-- Raised from 982 (+1 for this line) on 2026-09-20: the exhausted passes are a termination failure rather than a budget one, and a roadmap that files it as "needs a bigger ceiling" sends the next session after the wrong lever. -->
<!-- Raised from 967 (+1 for this line) on 2026-09-20: the rate memory's sampling and its statistic are one question with a measured answer, and the effort enum is narrower than the server's in exactly the place the audit needs. -->
<!-- Raised from 957 (+1 for this line) on 2026-09-19: the rate memory was stranded by a move and put back by hand, so the half that is still unfixed has to be an item rather than a JOURNAL line; and an orphan releases its slot rather than holding it, which is the worse half and was written backwards. -->
<!-- Raised from 951 (+1 for this line) on 2026-09-19: a cancel the cluster never hears misprices every dispatch behind it, and the lag nobody has timed belongs beside the item. -->
<!-- Raised from 947 (+1 for this line) on 2026-09-19: half of 25 is closed and the other half has a named wrong answer, which is worth more beside the item than in an ADR alone. -->
<!-- Raised from 943 (+1 for this line) on 2026-09-19: the burst under-count is fixed within a process, and which half is left belongs beside the item rather than only in a commit. -->
<!-- Raised from 936 (+1 for this line) on 2026-09-19: a slot leak that closes the gate for a server's whole life, and what it means for 40, both belong where the items are read. -->
<!-- Raised from 926 (+1 for this line) on 2026-09-19: the command-line entry point shipped unplanned, and it made two latent bugs reachable; a roadmap that omits either is the drift this file exists against. -->
<!-- Raised from 922 (+1 for this line) on 2026-09-19: the obvious fix for 40 was read out of the code and refuted, and that correction belongs beside the item. -->
<!-- Raised from 919 (+1 for this line) on 2026-09-18: 52.a argued from an ownership gap that the commit filing it had already closed, and the correction has to sit where the item is read. -->
<!-- Raised from 912 (+1 for this line) on 2026-09-18: two sub-items of 49 were measured wrong and are struck beside their corrections, and the artefact they described is filed where it actually belongs. -->
<!-- Raised from 896 (+1 for this line) on 2026-09-18: delegated writing measured at three tasks ever, all asked for, so the agent twins left Deferred and the policy half was filed beside them. -->
<!-- Raised from 893 (+1 for this line, and 895 was an arithmetic slip) on 2026-09-17: the runbook names the one failure shape that did not happen, and a caller reading empty_response alone files every failure today as a clean pass. -->
<!-- Raised from 890 on 2026-09-17: a pass succeeded at 58,959 tokens and another exhausted at 20,378 with less input, which is 44.b's claim demonstrated rather than argued. -->
<!-- Raised from 881 on 2026-09-17: expect returns a bucket of one whole when the label is trusted, which this deployment always is, and the idle-hold proposal it argues for. -->
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

Milestones M0a to M7 closed on 2026-09-02. M8, M9, M10 and M12, M11's closed items and
every ticked Unscheduled item followed on 2026-09-23. All of them are in
[archive/PLAN-milestones.md](archive/PLAN-milestones.md), with their ids.

### M11 — A call you can watch, and a cluster you can see

**Exit:** the viewer shows a running delegation and live cluster figures on a host where
nothing was configured, and no tool result changes shape.

3. ⬜ Split the running totals from the transcripts so retention and accuracy stop competing:
  an append-only ledger of one line per dispatch, never pruned, beside the fat per-dispatch
  records, which may be aged out
4. ⬜ The ledger counts *cluster* tokens, which is a fact. Calling the number a saving assumes
  what Claude would otherwise have read, which is not measured — report the facts and state
  the assumption beside any saving
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

59. ⬜ **A cancelled delegation keeps the cluster working, and admission stops seeing it.**
  The gate read `inflight_seqs: 0` against a cluster still running six, so a full burst is
  admitted on top of work nobody waits for. Wanted: a cancel that reaches the request (JOURNAL)
    - a. ✅ 2026-09-22 **Spike first, the lag is unmeasured:** wall clock of the kill against the transcript's `end`
62. ⬜ **Reasoning effort may be binary here, and the recovery ladder is built on it not being.**
  Measured only under 1k tokens, where the three looked alike; at a realistic size all of them
  spend the budget reasoning. Our four map onto the encoder's three plus off, so none is missing
69. ⬜ **A burst flag stranded by a failed close outlives its usefulness.** `_announce_burst`
  is best effort, and a live record is kept by liveness rather than by `_is_idle`, so other
  processes read an open wait nobody holds until that process exits or next goes idle
70. ⬜ **`files[]` takes whole files, so a large one is refused rather than sampled.**
  `CHANGELOG.md` is 182,236 tokens against the 140,000 cap and comes back in
  `files_skipped`, where a range was all anyone wanted. The file's own numbers, not the range's
71. ⬜ **`test_tool_time_is_the_tools_and_not_the_queue` is load-sensitive under the full
  WSL suite.** Four failures on 2026-09-22, each passing alone and on re-run; the fourth
  was a commit changing no code at all, which is what rules out every change as its cause
72. ⬜ **A first turn that runs tools still charges the dispatch's setup to them.**
  `tool_clock` starts at the grant, so turn 1's window holds budget pricing and request
  assembly as well as the tools. Needs the loop to time each call, where the number is

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
