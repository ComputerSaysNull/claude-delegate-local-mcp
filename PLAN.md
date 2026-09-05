<!-- BUDGET: 429
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

Completed items are annotated and stay here (ADR-0003), as a record of recent work.
[archive/PLAN-milestones.md](archive/PLAN-milestones.md) holds the closed milestone roadmap
M0a through M7 and the work found alongside it; anything else moves there only when someone
decides it should.

---

## Open — hardening, testing and troubleshooting

The milestone plan closed with M7; this is what is queued now. Ordered within each
group by what the item's own annotation says it costs.

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
  - **The trap, recorded with it:** token flow is not turn completion. The `alive`
    heartbeat must not report streamed tokens as liveness without the deadline change
    above, or it would call the 2026-09-04 stalls healthy. Open question to measure: a
    model looping while emitting tokens is bounded by `max_tokens`, but whether a reasoning
    model's thinking tokens count against it is unknown here.

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
