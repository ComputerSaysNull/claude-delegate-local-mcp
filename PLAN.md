<!-- BUDGET: 298
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

Completed items are annotated and stay here (ADR-0003), as a record of recent work.
[archive/PLAN-milestones.md](archive/PLAN-milestones.md) holds the closed milestone roadmap
M0a through M7 and the work found alongside it; anything else moves there only when someone
decides it should.

---

## Open — hardening, testing and troubleshooting

The milestone plan closed with M7; this is what is queued now. Ordered within each
group by what the item's own annotation says it costs.

### Security review, 2026-09-02

- ⬜ Operator allowlist for an agent's `network` and `extra_binds` — the only validation is
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
- ⬜ ~~Validate the opened inode, not the path~~ — `resolve_all` then `open` is check-then-use
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
- ⬜ Resource limits inside the sandbox — `build_argv` emits no `--rlimit`, no process cap
  and no `--size` on either tmpfs, and the module never imports `resource`. A fork bomb or
  a runaway allocation is bounded only by `run_bash_timeout` and `--die-with-parent`. This
  machine's page file is capped by choice, so a demand-side OOM is the live failure mode
  rather than a theoretical one
- ⬜ Escape a file's own END marker in the files block — `context.py` wraps each file in
  `--- BEGIN FILE <path> ---` / `--- END FILE <path> ---`, deliberately not a markdown
  fence, but the body is not escaped against those markers. A hostile file being reviewed
  can forge an end-of-file boundary and speak as the prompt. Concrete and testable, unlike
  prompt injection in general; the tool-level policy remains the real defence
- ⬜ Refuse a non-stdio transport outright — `config.py` already says adding the HTTP
  transport "is a real integration task, not a flag flip", yet setting it runs a server. A
  knob advertised as unfinished that still starts is the shape ADR-0034 deleted
  `sandbox_enabled` for. The review called it unauthenticated *and* unbound; measured,
  FastMCP defaults its host to loopback and `main.py` passes only a port, so the reachable
  surface is other local processes rather than the network, and no token is the true half
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
- ⬜ Admission waits, but it does not queue — the prerequisite for raising
  `admission_wait_timeout`, not a separate item. `acquire` is a re-test loop: a waiter wakes,
  calls `_try_take`, and either fits or waits again. No ticket, no ordering, so a request
  that has waited almost the whole timeout has no claim ahead of one arriving at that
  instant. Across two sessions on two projects it is worse than unordered — a release in
  another process notifies nothing, so cross-process waiting is polling a shared file and
  the winner is whoever polls at the right moment. Raising the timeout alone therefore buys
  a longer unfair wait and turns a bounded failure into possible starvation. Fairness first,
  through the same shared file the counters already use; then a timeout long enough that a
  busy cluster queues instead of refusing. The module docstring's "oversubscription queues
  rather than fails" is true about waiting and false about a place in line, and is where the
  wrong expectation comes from — correct it in the same change
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
- ⬜ ~~The documentation trim the 2026-09-01 audit listed~~ — `docs/ARCHITECTURE.md`,
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
- ⬜ ~~Say in `docs/DISPATCH.md` that the admission wait stacks on the dispatch deadline~~ —
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
- ⬜ A read-only git tool, because audit and review work is historical and cannot reach it —
  `.git/**` is on the secret denylist, so `.git` is covered by a tmpfs and every git command
  inside a delegation exits 128. Whether a claim is older than the code it describes, when
  the waivers landed, which document has gone longest untouched: none of it is reachable,
  and those are the questions an audit is for. The denylist entry is right and stays —
  `.git` holds everything ever committed, including what was later removed from the
  worktree, so a read-only bind would expose strictly more than the worktree does. The shape
  that fits is a server-process tool with a fixed subcommand allowlist, running outside the
  sandbox as `read_file` already does and putting any path argument through the same path
  policy. Ranks beside the search tool above and shares its justification. Without it, an
  audit agent cannot run unsupervised: the caller has to gather the history and hand it over,
  which is what the 2026-09-03 audit did
- ✅ 2026-09-03 Linked an agent body to the limitations it encodes (#88) — the
  `agent-capability` gate check. The sandbox half is derived from the denylist rather than
  hardcoded, and the two agent formats are separated because only a server-format agent's
  shell is confined. **The narrow case only**, and the limit is stated in the check and in
  CONTRIBUTING.md: the motivating case, `python3 scripts/docs_gate.py`, needs git
  *indirectly* and is invisible from the text. Writing its meta-test found more than the
  check did — a mislabelled finding name, and three gate checks with no negative test at
  all. Original entry follows.
- ⬜ ~~Link an agent body to the server limitations it encodes~~ — second sighting of one class.
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

## Deferred

On hold for weeks or months. Not cancelled, and not queued.

- ⬜ Anthropic-compatible adapter — the seam and canonical shape are kept so this is
  additive, roughly 150 to 220 lines in one new file (ADR-0008)

## Cancelled

- ❌ 2026-08-25 Streaming in v1 — cancelled. MCP tool calls are request/response, so
  Claude sees nothing incrementally either way. Progress notifications, which are
  required for the idle timeout, cover the part that actually matters. ADR-0018
  — **worth revisiting: the premise moved.** That reasoning weighed one consumer, the
  caller, and it still holds for the caller. ADR-0043 added a second one six days later:
  the transcript stream, which a person reads *while* the delegation runs. Streaming
  would let it carry tokens as they are generated rather than a heartbeat saying only
  how long it has been. Not reopened here, because nobody has yet wanted it enough —
  recorded so the next reader knows the cancellation was decided without this consumer
  in view rather than despite it.
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
