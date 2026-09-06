<!-- BUDGET: 663
     Raised from 647 on 2026-09-06: a second cache bug found by measurement rather
     than filed, so it arrives already ticked and its reasoning has nowhere else to
     live.
     Raised from 629 on 2026-09-06: the eviction item ticked, and its original kept
     beside it because the three directions it listed as alternatives turned out to be
     two properties you need together.
     Raised from 608 on 2026-09-06: the reply-budget item ticked, and its original kept
     beside it because "backend_status is the reader" was wrong in the instructive
     direction -- the reader had to be built, past a documented refusal to report the
     figure it now consumes.
     Raised from 520 on 2026-09-05: session 2's findings landed -- two new items at the head of the queue, and five tentative or wrong diagnoses struck in place rather than deleted, which costs the original and the correction both.
     Raised from 505 on 2026-09-05: this session's audit items marked tentative pending a second session's findings, and the marker itself needs explaining in the legend.
     Raised from 492 on 2026-09-05: the streaming item gained the evidence its argument lacked -- five stalls in one session, backend healthy each time, one of them after 29 completed turns.
     Raised from 480 on 2026-09-05: the audit item's diagnosis was corrected in flight -- forcing a tool call is not the fix, the tool has to be on the critical path, and a forced stat() proved it.
     Raised from 472 on 2026-09-05: the transcript item split in two once the records were actually read -- the ledger cannot say why a tool refused, and the sync hazard is documented only in an ADR.
     Raised from 429 on 2026-09-05: three items filed from one session's measurements -- the 120s fan-out ramp, the audit agent's stalling shape, and transcripts being off when a tool error needed explaining.
     Raised from 410 on 2026-09-05: the sandbox limits item ticked, kept beside its original because the original named a bwrap flag that does not exist, and because measuring the process cap took three attempts.
     Raised from 392 on 2026-09-05: the allowlist item ticked, kept beside its original because
     three of the four things it turned out to need are ones the filing could not have known --
     including a fix that would have made the filed feature decorative without it.
     Raised from 374 on 2026-09-05: kv_token_budget is 1.66x the real KV pool, filed now
     that backend_status reads the pool size and the fix has a number to use.
     Raised from 351 on 2026-09-05: admission's missing anti-starvation filed, with the half
     that is now measurable separated from the half that measurement has just closed.
     Raised from 320 on 2026-09-05: streaming moved out of Cancelled with the scope and the
     justification its 2026-08-25 cancellation was decided without -- lost work, and a stall
     deadline that can finally be honest.
     Raised from 320 on 2026-09-05: kv_token_budget is 1.66x the real KV pool, filed now that
     backend_status reads the pool size and the fix has a number to use.
     Raised from 312 on 2026-09-05: the batch item closed as wrong when filed, and the wrong
     original is the part worth keeping.
     Raised from 298 on 2026-09-04: two items JOURNAL 2026-09-04 named while recording a stall that looked like an outage -- a batch withholding finished results, and a stall failure that cannot be told from an unreachable endpoint.
     Raised from 286 on 2026-09-03: one item ticked, kept beside its original because the original's O_NOFOLLOW judgement turned out to be wrong and that is the part worth keeping.
     Raised from 265 on 2026-09-03: five items ticked with what they turned out to be, one new item, and
     two annotations corrected where they had stopped being true.
     Raised from 200 on 2026-09-03: six new items, not accumulated completed ones. A
     documentation audit that was truncated by its own turn budget found that three
     settings had each been sized against a constraint that later moved, and that the
     history an audit needs is unreachable from a delegation. The 2026-09-02 completed
     items were checked for archiving first, as the note below asks, and deliberately kept
     — they are one day old and are the reminder that note describes.
     Reset on 2026-09-02 from 477, when M0a-M7 and Extra moved to
     archive/PLAN-milestones.md. The raise history it replaces described a document more
     than twice this size, and the reason for each raise is in the CHANGELOG.md section for
     the pull request that made it. Completed items stay here: they are a useful reminder of
     what the last session did. Passing this line is a prompt to check whether any of them
     are ready to archive, not a requirement to move them, and not a reason to raise. -->
# Plan

Open work, one line per item, status first so the file scans.

`✅` done, dated · `🔄` in progress · `⬜` not started — queued under **Open**, on hold
under **Deferred** · `❌` cancelled, with date and **reason** — cancelled items stay,
because the fact that something was considered and dropped is worth more than a tidy list.
A struck-through entry with **no marker** is the archived original of the ticked item above
it, kept where its reasoning turned out to be wrong in an instructive way. Four of them
carried `⬜` until 2026-09-04, which read as open work and cost a delegation real turns.

**`⚠️ TENTATIVE` is a fifth state, added 2026-09-05 and meant to be temporary.** It marks an
item filed from one session's observations while a second session was investigating the same
failures from another angle. The observations are real; the *diagnoses* attached to them are
not yet trustworthy, and at least one was already contradicted by its own follow-up
experiment before the day ended. Do not act on a tentative item. Re-read it once the other
session's findings land, then either promote it to `⬜` with a corrected annotation or delete
it — an item that keeps this marker indefinitely is one nobody went back to.

Completed items are annotated and stay here (ADR-0003), as a record of recent work.
[archive/PLAN-milestones.md](archive/PLAN-milestones.md) holds the closed milestone roadmap
M0a through M7 and the work found alongside it; anything else moves there only when someone
decides it should.

---

## Open — hardening, testing and troubleshooting

The milestone plan closed with M7; this is what is queued now. Ordered within each
group by what the item's own annotation says it costs.

### Delegation cost and deadlines, 2026-09-05 (session 2)

Two findings from reading the operator transcripts, both measured, both new. They are the
head of the queue because everything else in this section gets cheaper once they land, and
because between them they explain the stalls that several items above were filed against.
JOURNAL 2026-09-05 has the numbers; `tests/regression/test_eviction_threw_away_the_prefix_cache.py`
reproduces the second without a cluster.

- ✅ 2026-09-06 The reply budget is derived from the deadline and a measured decode rate
  (`#113`, ADR-0055) — **"`backend_status` is the reader" was wrong**, and that is the half
  worth keeping. `probe_cluster` scraped seven metrics and not one was a rate, and
  `read_metrics` refused histograms by *documented decision* — so this had to argue past a
  refusal rather than read a number. The exception it earned is narrow and named after the
  `prefix_cache_hit_rate_since_boot` precedent that made the shape defensible; without that
  precedent the right answer would have been to leave the module alone. Three things the
  filing could not have known. The rate cannot be learned from experience alone: a one-shot
  completes no turns, and the tools-withdrawn final turn is a first turn for this purpose,
  so an estimator with no seed leaves uncapped exactly the two shapes that die — and the
  seed is the since-boot mean the module declined to *report*, which turns out to be a
  different act from consuming it. The ceiling had to bind an explicit `max_tokens` where
  ADR-0014's floor deliberately does not, because a floor is a preference and a deadline is
  not. And three `test_server.py` doubles pop one canned reply per request whatever the URL,
  so one extra GET ate the first turn's answer and they reported a *missing turn* rather
  than an extra request — found by the full suite, not by the files the change touched.
  Original entry follows.
- ~~`max_tokens` is set to a budget no deadline can pay, so productive turns are killed as
  stalls~~ — `effort: high` raises it to `thinking_max_tokens_floor` = 131,072, which is
  2.6x what `stall_timeout` can decode solo and ~4x when sharing. A model that uses what it
  was given cannot finish, and reasoning counts against the same budget — so it binds
  hardest at exactly the effort an audit wants.
  - **The two settings are in different units and nothing relates them**, which is why this
    survived. Derive the bound from `stall_timeout` and a decode rate read at runtime, not
    a new constant: the rate belongs to the deployment and moved once already this week.
    `backend_status` is the reader.
  - **Do not fix it by raising `max_turns`.** `final = turn == turns` withdraws tools on the
    last turn, so a delegation that exhausts its budget ends on the one shape that must fit
    a whole answer in one deadline; raising the count postpones the death and makes it
    dearer. Respect ADR-0014's floor — it exists so heavy reasoning does not return nothing,
    and a cap ignoring it re-creates that bug from the other side
- ✅ 2026-09-06 The final turn forbids tool calls instead of withdrawing the tools (`#115`,
  ADR-0057) — **never filed, and found by measuring rather than by reading.** Looking at why
  turn 12 of two delegations cached zero while turns 2-11 cached fine turned up a second,
  independent cache bug sitting on top of the eviction one: `tools=()` on the last turn moves
  the first difference to the front of the prompt. Corpus evidence was 3 of 7 runs and
  confounded with the eviction collapse, so it was carried as a hypothesis and settled with a
  four-arm probe — 36,018 tokens re-prefilled to avoid sending 321, with a repeat arm proving
  the cache was still warm. The probe was also allowed to *stop* the fix: if the chat template
  had dropped the tool block under `tool_choice: "none"` the divergence would have returned
  unchanged, and that is a fact about the server's template rather than one the spec implies.
  Moving the tools to the end of the prompt was the other candidate and is worse — it trades
  the cross-delegation `system prompt + tools` prefix, which every run shares, for a
  once-per-run saving, and gives up native tool-call parsing to do it
- ✅ 2026-09-06 Eviction carries its boundary instead of recomputing it every turn (`#114`,
  ADR-0056) — **the three candidate directions were not alternatives**, which is what
  "measure before choosing" turned up and no amount of reading would have. Modelled over the
  alternation the loop appends: the per-turn boundary reuses 2.9% of each prompt, gating on
  projected share alone reaches 5.1%, gating *plus* a stepped boundary reaches 79.2%, and an
  unbounded history reaches 93.0%. So the stickiness is the load-bearing half and the gate is
  what makes the common case free — filed as an either/or, delivered as both. Two things the
  filing could not have known. **`context_overflow_enabled` is off by default**, so the
  obvious arrangement — gate the whole policy on the flag — would have left the default
  configuration bounding nothing at all, trading a cache bug for an unbounded history; the
  stepping is therefore unconditional and only the holding is gated. And that was caught by a
  test asserting a stub had actually been produced, which failed `0 > 0` while the pressure
  test beside it passed by evicting nothing — the fourth-and-fifth-check problem, found in
  the act. `docs/DISPATCH.md` had also asserted the opposite of the fix, that no arrangement
  of the tail could be cache-stable. Original entry follows.
- ~~Eviction rewrites the history mid-stream, and the prefix cache pays every turn~~ —
  `evict_stale_tool_results` stubs the oldest surviving tool result *inside* the history,
  one per turn; the stack caches **prefixes**, so the divergence point moves toward the
  front and everything after it is recomputed. JOURNAL 2026-09-05 had already priced this
  lever at 30x and concluded "cache first, concurrency second"; nothing had checked whether
  the loop was pulling it.
  - **The policy is right and the accounting is wrong.** The saving is denominated in tokens
    *sent*; the cluster charges for tokens it cannot *reuse*. It also fires at ~7% of a 1M
    window, where there is nothing to relieve — `_OverflowGuard.__init__` sets `self.keep`
    outside the `armed` check and the loop reads that attribute directly, so the guard
    documented as "entirely inert unless `context_overflow_enabled`" is inert in its methods
    and live in the one thing that costs.
  - **Second-order cost, instrumented and invisible:** `evicted_then_reread` caught the
    model re-reading what eviction had just dropped, three turns running. The dropped
    content returns as a fresh full-size result, which evicts the next one.
  - Candidate directions, unranked: gate `keep` on projected share; evict in batches so one
    rewrite amortises; or evict from the front once and never re-touch. **Measure before
    choosing** — the regression test compares prefix stability without a cluster, and the
    live check is `cached_tokens` in the transcript

### Security review, 2026-09-02

- ✅ 2026-09-05 Operator allowlist for an agent's `network` and `extra_binds` (`#110`,
  ADR-0053) — `workdir_roots` was the pattern to copy for the containment and the wrong
  pattern for the rest. Three things the filing could not have known. The two fields want
  **different** gates: a bind pins to a root and `--share-net` has nothing to pin to, so
  egress needs the agent named *and* the file in `agents_dir` — a name alone is forgeable,
  since the workspace tiers are searched first and a repository can ship a file under any
  name. The allowlist would have been **decorative** without a fix underneath it: the bind
  was emitted as the frontmatter string, so a link inside an approved root was resolved by
  the kernel at mount time having never met the check. And the reserved-mount half taught
  the most: reordering the argv so nothing can shadow `/usr` also wipes every bind inside
  `/tmp`, where every temporary directory lives — an existing integration test caught it in
  one run, so the narrow case is refused instead, and four of the nine reserved targets are
  symlinks into `usr/` that are correctly *allowed*. One enforcement site, not two:
  `tools.py` merges agent binds with the operator's `toolchain_binds` into one untagged
  tuple, so nothing downstream can tell whose bind it is. Original entry follows.
- ~~Operator allowlist for `network` and `extra_binds`~~ — the only validation is
  `os.path.isabs` and a boolean parse, so a markdown file in a repository you delegate over
  can bind any absolute host path read-only and turn on egress for that call. The
  mount-level secret scan covers matches afterwards (ADR-0036) rather than refusing the
  bind. Of everything the 2026-09-02 review raised this is the one with a plausible
  end-to-end attack, and `workdir_roots` is the pattern to copy
- ✅ 2026-09-03 `open_resolved`, which opens and proves in one operation (#93, ADR-0049) —
  it returns a handle rather than a path, so there is no string left for a handler to
  reopen; that is what closes the item rather than the comparison itself. Three call sites,
  not two: `prefetch` had the same shape. **The `O_NOFOLLOW` judgement below was wrong** —
  the objection holds for the path a caller wrote and not for the resolved one, which
  `realpath` has already collapsed, so it is safe here and each half was asserted to catch
  the swap without the other. Two things the filing could not have known. The write path
  needed `O_TRUNC` removed and the truncation moved after the proof, or it detected a
  destruction it had already performed. And what this catches is redirection, not
  substitution: every layer is a function of the path, so a different regular file at an
  approved path fires neither check, measured, and is not a bypass. Original entry follows.
- ~~Validate the opened inode, not the path~~ — `resolve_all` then `open` is check-then-use
  in both file tools and in prefetch, with no re-validation. The adversary is the delegated
  model itself, which holds a read-write workdir bind under `run_bash` and can retry, so
  this is not the passive window the review described. `O_NOFOLLOW` is the wrong fix: it
  would refuse legitimate symlinked checkouts, since `realpath` has already collapsed them
  by design. Compare `realpath("/proc/self/fd/N")` against the roots after opening
- ⬜ Content-level detection for a renamed secret — every path-policy layer inspects the
  path and none the bytes, so `config.json` holding a private key passes all of them and is
  inlined, and `run_bash` can read one the mount-level scan did not match by name. One
  finding, not two: fixing the detection fixes both ends. **Not** by pointing `scan_text` at
  it, which the 2026-09-02 review recommended — that scanner looks for RFC1918 addresses,
  private-DNS suffixes and non-allowlisted emails, and would false-positive on the source a
  review delegation exists to read. A narrow, high-precision check for key material instead
  (PEM armour, `BEGIN OPENSSH PRIVATE KEY`, cloud key prefixes)
- ✅ 2026-09-05 Resource limits inside the sandbox (`#111`, ADR-0054) — **"emits no
  `--rlimit`" presumed a flag bwrap does not have.** bubblewrap 0.9.0 has none, so the caps
  come from a `prlimit` launcher in front of it; `resource` is still not imported, and
  should not be, since `preexec_fn` is a deadlock risk once tool calls run in threads. Three
  things the filing could not have known. `RLIMIT_AS` is address space, not resident memory,
  so it over-counts and a Go runtime will trip it — the accurate control is a cgroup memory
  limit, and `systemd-run --user` has no bus here, though the `pids` controller is present
  and would be the better process cap if it were reachable. The process cap ships
  compromised on purpose: counted per uid so concurrent delegations share it, unusable below
  ~64 because bwrap cannot then make its namespaces, and silent when it binds because the
  shell cannot fork to report it. And measuring it took three attempts — a sequential loop
  never exceeded two concurrent, then a loop counting iterations rather than successes
  reported 400 under a cap of 256, because `cmd &` succeeds whether or not the fork did.
  Two plausible experiments agreed and were both wrong. Mutation testing then caught a
  fifth check that could not fail: an integration test asserting no core file is left
  behind passed with `--core=0` removed. Original entry follows.
- ~~Resource limits inside the sandbox~~ — `build_argv` emits no `--rlimit`, no process cap
  and no `--size` on either tmpfs, and the module never imports `resource`. A fork bomb or
  a runaway allocation is bounded only by `run_bash_timeout` and `--die-with-parent`. This
  machine's page file is capped by choice, so a demand-side OOM is the live failure mode
  rather than a theoretical one
- ✅ 2026-09-04 Escaped a file's own boundary markers in the files block — matched by
  shape and for **any** path, not the entry's own: a forged `BEGIN FILE` naming a file the
  server never opened was the case the item did not name, and the worse one. Neutralised
  rather than dropped, because a review delegation reads source and `context.py` quotes
  those markers itself. The tool-level policy remains the real defence (`#96`)
- ✅ 2026-09-04 Refused a non-stdio transport outright, and deleted the port only it used
  — the field is **kept** rather than deleted, diverging from ADR-0034's remedy for a
  measured reason: `load` reads only variables matching a field, so deleting it would turn
  a configuration error into silence. `sandbox_enabled` differed in that the value being
  ignored was the one the operator wanted anyway. No token was the true half of the review
  finding; the loopback default made the rest of it moot (`#97`)
- ✅ 2026-09-03 `security/secret_globs.txt` does have the reach its header claims — this
  item was wrong when filed and is closed without work. The git half is `check_secret_paths`
  in `scripts/docs_gate.py`, which loads the globs and blocks any tracked file matching one;
  it landed in the first scaffold commit, well before this item was written. `NEVER_TRACK`
  is a separate belt-and-braces set of three files, and the gate says so where it is
  defined. Both cited demonstrations are deliberate, commented exemptions: `.env.example` is
  skipped by an `.example` suffix rule, and the list matching its own `*secret*` is skipped
  by `POLICY_FILES`, annotated there as the gate's first self-inflicted false positive. Left
  as a closed entry rather than deleted, because an open item describing a hole that is not
  there invites someone to rederive a check that is already derived

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

- ✅ 2026-09-03 `max_turns` overridable per call, and the hard cap at 100 (#83) — the merge
  read `= agent.max_turns` where its three neighbours read `x or agent.x`. Sharper than
  filed: `delegate_to_agent`'s description already promised "every explicit argument here
  wins over the agent file", so the contract was right and the code was wrong. Not on
  `delegate_readonly`, which had no turns until #86 gave it some
- ✅ 2026-09-03 One prefetch budget (#84, ADR-0046) — per-file cap now equals the total.
  The open question resolved as keep-it: it survives as an operator's way of re-tightening
  below the total, which is the only job it has left. The accepted cost is that one large
  file can now spend the whole budget and end the list, which the existing skip accounting
  already reports. Two test helpers had to follow the total down, one of them in a file
  with nothing to do with prefetch — found by running the whole suite rather than the two
  files the change obviously touched, which is how a coupling a change *creates* shows up
- ✅ 2026-09-04 Admission queues in order, and the wait timeout went 600s to 1800s with it
  (`#98`) — tickets in the same shared file the counters use, dropped from a `finally` so a
  timeout, a cancellation or any raise gives the place back. Two things the item did not
  foresee. A waiter counts as ahead of you only if it could be admitted **now**: strict
  ticket order reintroduces the head-of-line blocking the single predicate exists to
  prevent, and the existing large-prefill test failed against a first attempt that used it.
  And the schema is additive rather than versioned — resetting the file would zero the slots
  an older process on the machine still holds, which `/mcp` Reconnect makes normal. The
  docstring is corrected, here and in ARCHITECTURE.md. Ordering decides who goes next, not
  how fast anyone finds out: cross-process waiting is still polling
- ✅ 2026-09-03 A stall deadline, and a re-derived ceiling (#85, ADR-0047) — `stall_timeout`
  at 2100 does the killing; `dispatch_timeout` rose to 14400 and is now only a ceiling. The
  two had to land together: raising a bound that cannot see progress makes a stall more
  expensive for everyone queued behind it. The signal is turn *completion* — the per-turn
  notification fires at the top of a turn and the keepalive is a timer, so both would have
  reset the clock on the very turn that wedged. A one-shot completes no turns, so its
  deadline runs from entry and its bound becomes the tighter of the two. The lower bound was
  written strict and was wrong, since `turn_timeout == dispatch_timeout` is permitted here

### Documentation accuracy

- ⬜ The documentation trim the 2026-09-01 audit listed — **no longer blocking, and that is
  a decision rather than an outcome.** 2026-09-03 chose to raise budgets with a stated
  reason instead, which is one of the three ways out `check_budgets` names and what
  ADR-0003 means by "budgets block, but never delete": trimming prose is audit work, with a
  method a feature session does not have. Six raises across five documents, each in the
  commit that needed it. Still worth doing, still by an audit, and no longer in the way —
  the annotation below described it as the thing blocking the queue, which it has stopped
  being. Original text follows.
- ~~The documentation trim the 2026-09-01 audit listed~~ — `docs/ARCHITECTURE.md`,
  `docs/DISPATCH.md`, `docs/AGENTS.md`, `CONTRIBUTING.md` and `PLAN.md` all sit at their
  ceilings, and the review fixes of 2026-09-02 needed three budget raises across two
  documents to state facts the code had just acquired. **Now the thing blocking the queue
  rather than an item in it:** the three improvements of 2026-09-02 hit the ceiling on three
  different documents, and paid for two of them by deleting duplication found on the spot —
  ARCHITECTURE.md was restating the generated cell for `admission_wait_timeout` and
  DISPATCH.md's `alive` paragraph. Both were real, and finding them while blocked was luck
  rather than method: that is the item being done opportunistically, a paragraph at a time,
  to land something else. Each feature pays interest; do this deliberately, and first
- ✅ 2026-09-03 Said in `docs/DISPATCH.md` that the admission wait stacks on the dispatch
  deadline (#85) — folded into the deadline section while it was open for the stall work,
  which is the owning document being made correct about code that changed rather than a
  separate errand. Original entry follows.
- ~~Say in `docs/DISPATCH.md` that the admission wait stacks on the dispatch deadline~~ —
  the deadline is taken after a slot is granted (ADR-0038), so the caller-visible worst
  case is both settings added. `admission_wait_timeout` does not appear in that document at
  all, and its deadline section enumerates three enforcement points without mentioning
  admission. Recorded in M7 and in the ADR, which is not where a reader looks. More pressing
  since 2026-09-02: ARCHITECTURE.md's copy of the rationale was deleted as duplication, so
  the only prose statement of it now lives in a generated config cell
- ✅ 2026-09-04 Re-measured `BYTES_PER_TOKEN` per tokenizer and said so in
  `docs/MODELS.md` — the conservative direction held, and for the predicted reason rather
  than because ratios are stable: `.json` moved 47% against the first tokenizer and `.py`
  not at all, yet all five measurable here still sit below their entries. No value changed;
  the defect was a comment calling the table a property of file types. JOURNAL 2026-09-04
- ✅ 2026-09-02 Measure whether the tool schemas sit inside the cached prefix — they are.
  Sent cold, one reworded tool *description* cached zero tokens, exactly like a reworded
  system prompt, where the unchanged prefix cached 4096 of 5946. So rewording one costs a
  full prefill. Two things worth more than the answer: latency could not measure it at all,
  every case landing within 20ms including the cold control, and the first token-based run
  was wrong in the flattering direction because the variant had been sent minutes earlier
  and was measuring its own echo. ADR-0011's body is untouched; JOURNAL 2026-09-02 has it,
  and `declared_tools` now cites the measurement rather than the ADR

### Improvements

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
- ✅ 2026-09-04 A stalled delegation says what it had managed (`#102`) — turns, tool calls
  and the last tool, so a wedged task shows work behind it where a dead endpoint shows zero
  of both. **"Reachable without new plumbing" was wrong**: nothing on the error path could
  see the counters, since `_Watch` is a local of the loop and `AgenticDispatch` is built only
  on success. One handler wrapping the loop covers all five raise sites. Absent stays
  distinct from zero, so a one-shot's message is unchanged. The find was elsewhere: the
  template's literal "while" had been rendering "while with no turn completed" all along
- ✅ 2026-09-02 A heartbeat for the agentic loop — `#58`'s silence closed, and the timer
  alone would not have closed it: `_run_calls` is synchronous and `run_bash` reaches
  `subprocess.run`, so a command held the event loop and no timer could be scheduled during
  exactly the window a long delegation spends there. Tool calls now go through
  `asyncio.to_thread`, which also stops one command freezing the transport for every
  delegation admitted beside it. The regression test asserts a beat *during* a tool call and
  was verified to fail without that half while the slow-backend test passed
- ✅ 2026-09-02 Line addressing in `read_file` — `start_line`, and every returned line
  numbered. `docs-audit-local`'s instruction to cite by quotation is withdrawn, and
  CONTRIBUTING.md now records the trap it came from: an agent body can encode a workaround
  for a server limitation, and nothing links the two
- ✅ 2026-09-02 `effort` is required on all four delegation tools — not previously filed
  here, and found in use rather than in review: four research delegations in one session ran
  at the silent default because none named a level, and the one rerun at `high` found the
  blocking-subprocess defect above that the others had missed. `inherit` is how a caller
  defers on purpose, which is what keeps an agent file's own `effort:` reachable now that
  the argument cannot be omitted. ADR-0045
- ⬜ Server-format twins for the four Claude Code agents — `#72` made it visible that
  `code-reviewer`, `docs-audit`, `researcher` and `test-writer` load only in Claude Code,
  so `delegate_to_agent` can reach one of five agents in this repository. `docs-audit-local`
  is the shape to copy (`#67`). CONTRIBUTING.md already records the two-format arrangement
  as temporary; this is what it costs
- ✅ 2026-09-03 `search_files`, and a read-only delegation that can use it (#86, ADR-0048) —
  `delegate_readonly` now offers `read_file` and `search_files`, and so runs the loop. The
  read-only set is **derived** from a `writes` declaration on each tool rather than listed,
  which is the half worth keeping: injecting the mistake it exists to catch — a writing tool
  that forgets to declare it — fails four tests including the one asserting the executor
  refuses a write. `paths.py` gained a second *disposition* over one policy, not a second
  policy: `resolve_permitted` drops what fails, and is only for paths nobody named. Widest
  blast radius of the session, eight existing tests across four files
- ✅ 2026-09-04 `read_git`, a read-only git tool in the server process (`#99`) — the
  denylist entry stays and the sandbox is untouched. Two things the item's shape got wrong.
  Path *arguments* cannot go through the path policy: it validates a path that exists now,
  and history is about files that were deleted, so they are checked for not leaving the
  repository instead. And the repository needs validating **twice** — `-C` makes git
  discover a repo by walking up, so a validated directory can resolve to one above the
  root. `open_resolved`'s guarantee is unavailable here by construction and the CHANGELOG
  says so. Two usability gaps came from running it, not reading it: `git log -1` and
  `rev-list --count` were both refused, and the model reached for both first
- ✅ 2026-09-03 Linked an agent body to the limitations it encodes (#88) — the
  `agent-capability` gate check. The sandbox half is derived from the denylist rather than
  hardcoded, and the two agent formats are separated because only a server-format agent's
  shell is confined. **The narrow case only**, and the limit is stated in the check and in
  CONTRIBUTING.md: the motivating case, `python3 scripts/docs_gate.py`, needs git
  *indirectly* and is invisible from the text. Writing its meta-test found more than the
  check did — a mislabelled finding name, and three gate checks with no negative test at
  all. Original entry follows.
- ~~Link an agent body to the server limitations it encodes~~ — second sighting of one class.
  The `read_file` line-addressing work recorded the first: an agent body carrying a
  workaround for a server limitation, with nothing connecting the two, so the workaround
  outlives the limitation. `docs-audit-local` then carried the inverse — an instruction to
  run the gate first, which the sandbox cannot satisfy at all — and every invocation spent a
  turn and a failed command discovering that. Both instances are fixed; the pattern is not.
  A full check is not automatable, but the narrow case is: an agent body naming a command
  its `allowed_tools` and sandbox cannot run
- ✅ 2026-09-03 A read-only form of `delegate_batch` (#91) — `delegate_batch_readonly`,
  sharing one `_run_batch` body with the sixth tool. Narrowing `delegate_batch` with
  `allowed_tools` was never the alternative: ADR-0042 again, since a client decides before
  the call runs and never sees arguments. What `delegate_readonly` has no equivalent of is
  the agent — adversary-controlled markdown in the repository being reviewed — which cannot
  widen the set, now asserted. Two documents still called it `allowed_tools=[]` since #86
- ✅ 2026-09-03 Per-tool counts in the result (#92) — `tool_calls_by_name`, cheaper than
  filed since `called` already held the name. The find was elsewhere: a one-shot test
  asserting an *absence* of notifications shared the machine's real slots, so under load it
  queued and read its own admission wait as a heartbeat that would not stop
- ⬜ Globs in `files[]`, expanded server-side — a shorthand for naming many files, not a
  way to look for anything. Its original justification, that expanding before the call keeps
  `delegate_readonly` toolless and loopless, no longer holds now the fork is settled the
  other way, so this is a convenience and ranks below the search tool. The work is in the
  budget rather than the matching — a glob hitting two hundred files has to skip and account
  for them the way `context.prefetch` already does, not spend `prefetch_budget` silently
- ✅ 2026-09-03 `edit_file`, addressing text rather than lines (#94, ADR-0050) — the
  ordering held: #93 landed first, so this tool holds one `"r+b"` descriptor for the whole
  read-modify-write and there was never a second `open` to make safe. The shape changed from
  what was filed. Line addressing had made a line range look obvious and it is the worse
  half of the choice, because a stale line number overwrites a different region silently
  while a stale quotation cannot — so `old_string` must match exactly once, and zero or two
  matches are refusals that leave the file byte-identical. The find was elsewhere: three
  documents each kept their own prose list of the tools the local model gets, none of them
  the document that owns `tools.py`, and one addition made all three wrong at once

- ⬜ **A delegation returns a handle, and a second call collects it** — the client backs an
  MCP call into the background after 120s, and issues the next tool call only then, so
  firing `n` delegations in one message costs `120s x (n-1)` of stagger before the last one
  starts. Measured 2026-09-05: four passes issued together started at 20:24:30, 20:26:32,
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
- ⬜ **`docs-audit-local` assumes one big prefetched pass, and that shape now stalls** —
  its body says a prefetched audit finishes "in one turn with zero tool calls", measured
  2026-09-03. The served model swapped to a vision model on 2026-09-04 and the measurement
  was not re-taken. On 2026-09-05 a twelve-document, seven-class call died at 2100s with
  **0 turns and 0 tool calls**, and two of four smaller passes went the same way — while
  the two that called tools completed 3 and 9 turns and finished.
  - **The predictor is whether tools sit on the critical path, not whether any are called.**
    A tool call ends a turn and resets the stall clock, so a pass with nothing to call must
    fit its whole answer inside one turn and ADR-0047's deadline becomes a wall clock. But
    *mandating* a call does not fix it: a forced `stat()` closed turn 1 in 12 seconds and
    the model then spent 1068 seconds in one silent turn, dying anyway. The passes that
    survive are the ones that cannot proceed without reading the next thing — 12 and 21
    turns, none longer than 282 seconds. ~~**What is not yet isolated is which change earns
    that**, and the comparison run varied three things at once: it dropped the prefetch,
    read in `start_line` pieces, *and* was told to emit each document's findings before
    reading the next, where the run it replaced was told to withhold everything until the
    end. Incremental output is at least as plausible a cause as incremental reading, and
    the two imply opposite advice about prefetching — one of them keeps a real optimisation,
    the other throws it away. Isolate it before rewriting the body.~~
  - **RESOLVED 2026-09-05 (session 2): neither, and no prompt change is the fix.** The
    struck paragraph hunts a prompt property; the cause is the reply budget — see
    "`max_tokens` is set to a budget no deadline can pay". A tool call helps only because it
    *ends a turn*, which is why "on the critical path" predicted survival while a forced
    `stat()` did not: it ended turn 1 and left turn 2 holding the same budget, and its 1068
    silent seconds are ~30,000 tokens of decode, not a wedge. **Prefetching is exonerated.**
    Fix the budget and re-take the measurement rather than rewriting the body
  - ~~**The 2026-09-03 optimisation is what now kills it.**~~ **It is the turn count, not
    the prefetch** (session 2). "Prefetched, one turn, zero tool calls, four findings" was a
    real measurement and a real improvement; what became fatal is the *one turn with zero
    tool calls*, which must fit a whole audit inside one deadline. The prefetch is what made
    that turn cheap and is worth keeping. Re-take the measurement against the model actually
    serving once the budget is fixed, and say in the body that the answer is model-dependent
    rather than settled.
  - Also correct two things the split exposed: `read_git` is available now and is how
    MISSING and ESCAPE ABUSE get done, and waiver counting must be line-anchored on
    `Docs-Gate-Skip:` — matching the substring counted a commit whose prose *described* a
    past waiver, over-reporting one document as being at the threshold.
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
- ⚠️ TENTATIVE (the only one left — session 2 read the transcripts but not this)
  **The transcript's sync hazard is documented where nobody setting it will look** —
  the case is named in the streaming ADR: a directory inside a synchronised or backed-up
  location is one whose contents leave the machine, "on someone else's schedule and to
  someone else's storage", and the record holds the task verbatim plus, with the stream,
  replies. But `transcript_dir`'s own help text says none of that, and the generated
  reference is the only thing an operator reads while choosing a value. Put it in the
  setting's description, where the choice is actually made — **as a trade, not a
  prohibition.** This machine deliberately points at a synced Windows directory, and the
  reason is one the ADR never weighed: a WSL distribution is disposable and `/mnt/c`
  survives it, so the durable choice and the leaky one are the same choice. The advice a
  reader needs is what each direction costs, not which to pick
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
