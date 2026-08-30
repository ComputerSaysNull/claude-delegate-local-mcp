<!-- BUDGET-PER-ENTRY: 95      Raised from 30 on 2026-08-29: 30 was sized for a
     single-feature pull request, and it had started deciding how work was split rather
     than how it was described -- a milestone finished in one pull request had to be
     broken into five to fit. The per-entry cap exists to stop an entry sprawling, not
     to cap how much one pull request may do. -->
# Changelog

Newest first, **one section per pull request**. The heading carries the number, the merge
date and the pull request's own title, so the record and the thing it describes are named
the same way and a section can be found from either.

Under each, the subsections `Added`, `Changed` and `Fixed`, in that order. A subsection
with nothing in it is left out rather than left empty.

**A merged section is never edited afterwards.** It records what one pull request did, and
that stops being true the moment a later one revises it. A correction is a new section that
says what changed and why, exactly as an ADR supersedes rather than overwrites. The only
text written after the fact is the number in the heading, which cannot be known until the
pull request exists.

Entries carry the **why**, not just the what: the symptom that prompted the change, the
cause, and the fix. A terse one-liner is not enough -- in six months the reason is the only
part still worth having.

No commit hash, deliberately. An entry lives in the commit it describes and so cannot know
its own hash, and `main` is squash-merged, so a branch hash names an object that never
reaches anyone else's clone. The pull request number survives the squash and is the thing
worth citing.

Older entries, in the previous flat format, are in
[archive/CHANGELOG-2026-08.md](archive/CHANGELOG-2026-08.md).

## #42 — 2026-08-30 — feat: the admission gate, and the four settings that were inert

### Added
- `admission.py`: the ADR-0012 gate every delegation now passes before it reaches a
  backend. `max_inflight_seqs`, `kv_token_budget`, `large_prefill_tokens` and
  `max_inflight_large_prefills` have been in `config.py` since M0b, rendered into the
  generated reference, and read by nothing. They now change behaviour: work that used to
  run immediately may queue.
- One `asyncio.Condition` over plain counters, checked as a single atomic predicate --
  not four semaphores acquired in turn. A request that took a sequence slot and then
  blocked on the large-prefill cap would hold capacity it is not using for the whole
  wait, starving smaller requests that fit every rule; ADR-0012 makes exactly that the
  normal case for big tasks. Proven by building the wrong version: parking a waiter with
  its sequence slot held makes `test_a_blocked_large_prefill_holds_no_other_capacity`
  fail, and nothing else.
- `admission_wait_timeout`, defaulting to 600s. `dispatch_timeout` cannot bound the wait:
  its deadline is computed inside `run_one_shot` and `run_agentic_loop`, which do not run
  until a slot has been granted. The two stack rather than dividing one budget (ADR-0038).
- `backend_status` reports live gauges, three high-water marks, the wait totals, the
  longest single wait and the count of waits that hit the limit. ADR-0012's own reason:
  oversubscription announces itself as latency where idle capacity is silent, so a
  ceiling set too low is invisible unless something counts it. A tool description changed,
  which is a behaviour change to the model-facing contract, not a wording fix.
- `context.estimate_text_tokens`, for a string with no file behind it.

### Changed
- The endpoint's own `concurrency` is now the gate's fourth rule instead of a semaphore
  local to `delegate_batch` (ADR-0037). That semaphore bounded a batch against itself and
  nothing else: two batches, or a batch beside a plain `delegate`, could exceed the limit
  it was reading, and a single `delegate` was never checked against it at all --
  while `max_inflight_seqs`' own description already claimed both were checked. Now true
  on every path, and there is one gate rather than two that can disagree.
- Zero in any of the four admission settings is refused at load. It does not mean
  unlimited; it means nothing is ever admitted, so the gate queues every delegation until
  the wait times out and reports congestion for a setting that is simply wrong.

### Fixed
- The large-prefill classification counted the reply allowance, which is decode and not
  prefill. `max_tokens` defaults to 65536 against a 32768 threshold, so *every*
  delegation was classified large and rule 3 silently bounded the whole server at
  `max_inflight_large_prefills` -- while every other rule read as though it were the one
  binding. Found because the batch-concurrency test still passed with the endpoint rule
  deleted: it had been measuring the large-prefill cap all along. A request is now sized
  by two numbers, its KV footprint and its prefill, and both directions are tested.
- `docs_ownership.toml` described `sandbox.py` as "not written yet (M5)" and explained
  that the claim was kept to force the document to be updated when the module landed. It
  landed in `#35`; the comment outlived what it described.

## #41 — 2026-08-30 — feat: the five tools, and a batch that shares its prompt

### Added
- `delegate_to_agent`, `delegate_batch` and `list_agents`. That is five MCP tools, which is
  the number `docs/AGENTS.md` has promised since M0b, and a test now asserts the exact set
  rather than membership -- a sixth tool added without argument would otherwise pass, and
  the cost of one is paid by every caller whose tool list grows.
- `run_delegation`, the shared body all four delegating paths go through. They differ only
  in where their arguments come from, so they resolve and then reuse it. A second dispatch
  path is how the halves of a precedence rule drift apart, and precedence is the whole of
  what an agent file is.
- `delegate_batch` shares one agent and one `files[]` across many tasks (ADR-0037). That is
  the shape ADR-0011's prompt order was already describing -- system, agent body, files,
  task last, so the task is the part that varies. Everything before it is identical between
  items and the cluster serves it from its prefix cache: eight questions about one module
  cost roughly one read of it. Items run concurrently, bounded by the endpoint's own
  `concurrency`, which the registry has declared since M1 and nothing enforced until now,
  because nothing until now ran two requests at once. Both directions of that bound are
  tested -- exceeding it fails, and so does running sequentially.
- One item failing does not fail the batch. Each result carries its own `ok`, and a failed
  one carries `error` where its answer would be. Failing the whole call would discard
  compute already spent on items that worked.
- The result of an agent delegation names the file that shaped it. The lookup has three
  tiers, so the agent's name alone does not identify what was read, and a delegation that
  behaved unexpectedly is usually a file behaving exactly as written.

### Fixed
- The workdir was used to look an agent up **before** it was root-checked. The lookup reads
  `<workdir>/.claude/agents/`, so an unvalidated caller path was driving a filesystem read
  and the root check was merely something that happened afterwards -- which is not a check.
  Found by a test asserting the refusal for an out-of-root workdir, which instead reported
  the agent as missing: the lookup had already gone looking in a directory nobody allowed.
  Verified by restoring the old order and watching the test fail again.
- `PathRefused` described every refusal as "path(s) in files[]", including the workdir,
  which is a different surface and is one path rather than a list. It now names the surface
  it refused.

## #40 — 2026-08-30 — feat: a delegation can be given a workspace, and the timeout that makes usable

### Added
- `paths.resolve_workdir`: layer 1 applied to the `workdir` argument, which is a separate
  surface from the files read inside it. Checked against `workdir_roots`, which falls back
  to `workspace_roots` when unset -- reading a project and being able to work in it are
  separable grants, and a workdir is bound **writable** for the whole call. Resolved before
  it is compared, so a symlink sitting inside a root but pointing out of it is refused on
  where it lands rather than where it sits. Proven by making the check compare the path as
  written instead: the escape test stops failing, which is what a written-path check buys.
- `tools.BashPolicy` carries the workdir, the network flag and the extra binds from the
  delegation to `SandboxRequest`. All three were hardcoded in `_run_bash` -- `workdir=None`
  most consequentially, which meant no delegation could reach a repository at all. Per
  delegation and never a config field, for the same reason `allowed_tools` is not one.
- Real-sandbox tests for the three: a bound workdir is the working directory and is
  writable, a secret inside an `extra_binds` directory cannot be read, and an ordinary file
  in the same directory still can. Read from a running shell rather than asserted against
  argv -- `#36`'s lesson was that a flag in the argv proves it was passed, not that anything
  acts on it.

### Changed
- `run_bash_timeout` 120s → 600s. The figures the plan carried for this decision were stale
  and disagreed with numbers recorded in the same pull request that produced them, so the
  suite was re-measured: **281s serially in WSL**, which is the environment `run_bash`
  actually runs in. 120s therefore sat below the median legitimate command rather than above
  the slowest, which is the only place a timeout can tell a hung command from a slow one.
  The two errors are not symmetric. Too high wastes wall clock on a hung command and is
  bounded by `dispatch_timeout` anyway; too low kills real work and reports it as a non-zero
  exit, and a model handed that reasons about it as a test failure and repairs passing code
  -- corrupting the exact ground truth ADR-0007 rests on.
- The secret scan now covers `extra_binds` as well as HOME and the workdir (ADR-0036). The
  exclusion was deliberate and recorded, and both of its reasons stopped holding: the value
  is "an operator's choice" no longer, because an agent file supplies it, and "a read-only
  bind protects nothing more" was never right for a credential, where being read *is* the
  threat. Demonstrated before it was claimed: a planted key in an `extra_binds` directory is
  readable from inside a real sandbox without this change, and returns nothing with it.

### Fixed
- `DELEGATE_WORKDIR_ROOTS` rendered as **Inert** while being read, because the scan looks
  for a field name in code outside `config.py` and `paths.py` reaches this one through the
  `effective_workdir_roots` property -- where the "empty means reuse workspace_roots"
  fallback lives, and where it has to live, since inlining it at the call site would put a
  config default outside `config.py`. The scan now follows one level of accessor. Not a
  harmless over-mark: the marker's stated meaning is that the setting "does nothing, because
  the subsystem it controls is not built", so a reader deciding whether to set it was told
  the opposite of the truth. Both failure directions are tested -- an accessor nobody calls
  cannot launder a dead field, and the hop does not chain.

## #39 — 2026-08-30 — feat: agent files are found, validated, and actually bind

### Added
- `agents.py`: the three-tier lookup (`<workdir>/.claude/agents/<name>.md`, then
  `<workdir>/.claude/skills/<name>/SKILL.md`, then the configured personal directory, first
  match wins) and the frontmatter validator behind it. `load_agent` is the whole surface a
  caller needs; `list_agents` enumerates what is visible, dropping a name shadowed by a
  nearer tier rather than reporting a choice the lookup does not offer.
- The frontmatter is parsed by hand rather than by adding PyYAML. The field set is fixed and
  known, the runtime is otherwise `fastmcp` and `httpx`, and what this parser cannot read --
  nested maps, block scalars, block lists -- it refuses rather than reading as something
  else. That refusal is the point: a hand-written parser that guesses is worse than a
  dependency.
- Every frontmatter rule refuses rather than defaults, because the ancestor bug this format
  exists to avoid is frontmatter that was loaded and then ignored. An unknown key is refused
  (a `mode:` for `model:` would otherwise cost the setting in silence), `effort: medium` is
  refused by name because it is Claude Code's vocabulary and not this one (ADR-0031), a tool
  this server does not implement is refused, and a `name:` that disagrees with the filename
  is refused because callers look agents up by filename and one of the two is then a lie.
- `Delegation` gained `agent_body`, and the prompt order moved onto a `render` method.

### Changed
- An agent file asking for more turns than `max_turns_hard_cap` is refused when it loads,
  where a caller passing the same number is still clamped in silence. The asymmetry is
  deliberate and is now written down in both places it can be met: a call argument is gone
  when the call returns, but a file is committed, read again and believed, so clamping there
  would leave a wrong number in it indefinitely -- running correctly, and reading as though
  it were in effect.

### Fixed
- `Delegation`'s docstring claimed the prompt ordering rule "lives in exactly one place"
  while the one-shot builder and the turn loop each concatenated the parts themselves. The
  claim was false before the agent body existed and would have become a third segment to
  keep in step across two sites. Both now call `Delegation.render`, which is what the
  docstring always said.
- Two regression tests named a setting that had just gone live. Each asserts the inert
  marker can appear, and each named `agents_dir` as its specimen -- which `agents.py` now
  reads. Repointed at a field that is still unbuilt rather than loosened: the assertion
  moving is the marker working, and a version of either test that stopped naming a real
  field would pass forever, which is the failure both were written against.

## #38 — 2026-08-30 — fix: declare run_bash only where a sandbox can run it

### Fixed
- `run_bash` was declared to the model on hosts with no bubblewrap, and then refused every
  call it was asked to make. M5 emptied `WITHHELD_TOOL_NAMES` to open the route, but
  `available_tool_names()` took no `Config` and so had no way to ask whether *this* host
  could confine a shell -- it subtracted a constant decided at import time from a set decided
  at import time. The symptom was a wasted turn: a Windows diagnostic recorded a delegation
  spending turn 4 of 6 calling `run_bash` and being told no, and ADR-0016 already measured
  that the first turn is often lost to orientation, which made this the second.
  `available_tool_names` and `resolve_allowed` now take a `Config` and ask
  `sandbox.available` -- the same condition `sandbox.run` refuses on, checked where the tool
  set is resolved rather than after a round trip has paid for it.
- The comment deleted when the withhold set was emptied had argued exactly this case about
  the withholding it was justifying. The reasoning outlived the code it was attached to, so
  it is written down again where it now belongs: `WITHHELD_TOOL_NAMES` stays empty and stays
  in place, because it is for a tool withheld *everywhere*, and a per-host fact is not
  something a constant can answer.
- Three documentation lines falsified by `#36` and missed when it landed: the module table
  in ARCHITECTURE.md still marked `sandbox.py` *(built, not yet reached)* while the same
  document's own "The route, now open" section said otherwise; AGENTS.md said `run_bash`
  "refuses every call until `sandbox.py` exists"; and AGENTS.md described TOOLS.md as
  something that would be generated "once the tools exist".

### Added
- A test that the tool leaves every set when `bwrap_bin` names something that does not
  resolve, and its counterpart at the server surface, where the declared list is read off
  the wire rather than out of a function. Both were run against the unfixed code first and
  both failed, which is the only thing that makes them worth having.
- A test that the *executor* still refuses `run_bash` on such a host for its own reason.
  Narrowing what is declared is advisory -- a model can call a tool it was never offered --
  and the two sites are meant not to trust each other. That one passes with or without this
  change, deliberately: it guards the site that was never broken.

## #37 — 2026-08-29 — test: isolate roster tests, add pytest-xdist, record timing measurements

### Fixed
- `test_agent_roster_is_generated.py` edited the real `.claude/agents/*.md` and
  `CONTRIBUTING.md`, then put them back in a fixture teardown. That teardown runs on an
  assertion failure but not on a killed process, so an interrupted run left the working tree
  corrupt with nothing to say so. It now copies the generator and its inputs into a throwaway
  tree and points the generator at that, which is the pattern five sibling regression tests
  already use -- the generator resolves its root from its own location, so a copy *is* the
  repository under test.
- The same mutation made the suite unsafe to run in parallel: another worker running the
  read-only roster check saw the perturbed files and failed. Found by running `pytest -n
  auto`, which failed on roughly half of its runs and passed on the rest. Five consecutive
  clean runs since, in the configuration that used to fail.

### Added
- `pytest-xdist` as a dev dependency, opt-in rather than in `addopts`. `-n 8` takes the
  non-live Windows suite from 181s to 60s, and the full WSL suite from 235s to around 100s.
  Spawning processes rather than running the tests is the dominant cost on both. What is left
  varies between 64s and 145s from run to run, and that spread is the live model generating,
  not the harness -- the rest is steady. Parallelism also turns out to be a check in its own
  right: it fails on any test that mutates shared state, which is exactly how the defect above
  surfaced.
- A test that regenerating makes the roster check pass again. The two tests either side of it
  assert the check *fails* on a perturbation; without this one, a `--check` that failed
  unconditionally would satisfy both and be useless.

### Changed
- Recorded against M6's workspace-bind item: binding a workspace makes the 120s
  `run_bash_timeout` wrong for the first thing a delegated model will try. This suite needs
  157s in WSL and 361s on Windows, so a model asked to run the tests would report a kill
  rather than a result. Not a bug today only because `workdir` is `None` for every caller, so
  no delegation can reach the repository at all.

## #36 — 2026-08-29 — feat: run_bash confined, its secrets covered, and its exits measured

### Added
- A behavioural test for `--die-with-parent`. The flag has shipped since `sandbox.py` was
  written and the suite asserted it was present in the argv, which proves it was passed and
  not that anything acts on it. The claim worth holding is that a sandboxed command cannot
  outlive the server that started it — otherwise a crashed or restarted server leaves shells
  running against the workspace with nothing left to reap them.
- Its negative half, which is what makes the first able to fail: the same fixture with the
  flag stripped, asserting the orphaned command *does* survive. Without it, a positive result
  is equally consistent with a command that died for some unrelated reason.
- `_orphaned_run`, which starts bwrap from a throwaway shell that then exits on its own.
  `sandbox.run` blocks until the command finishes, so it cannot express this: the parent
  whose death matters is the server, and a test cannot kill its own interpreter. No signal is
  sent — `start_new_session=True` puts the sandbox in its own process group, so a test that
  killed the group would prove its own teardown instead of the flag. The shell lingers before
  exiting, because otherwise bwrap can be reparented to init before it installs the
  parent-death signal, which looks exactly like the flag failing.

- The secret denylist is now enforced at the **mount level** for `run_bash`, the last thing
  standing between the sandbox and being connected. An empty root means a denylist cannot
  work by subtraction — a secret is visible only because it sits inside a tree that had to be
  bound whole — so each match is covered up instead: `--tmpfs` over a directory,
  `--ro-bind /dev/null` over a file, emitted after every bind because a shadow needs its tree
  to exist first. ADR-0035.
- `paths.py` gains `secret_match`, lifted out of `_check_secret` and shared with the sandbox.
  Sharing only the denylist *file* would have left two readings of it, so a pattern could
  refuse `read_file` while the same file stayed readable from a shell, with nothing to report
  the disagreement.
- `DELEGATE_SECRET_SHADOW_MAX_ENTRIES` and `DELEGATE_SECRET_SHADOW_MAX_DEPTH` bound the scan.
  Exhausting either refuses the command rather than running with part of a tree covered, for
  the same reason a missing denylist file is fatal: once the command is running, partial
  coverage is indistinguishable from full coverage. The bound is also a latency ceiling — the
  scan runs per call, on `/mnt/c`. This repository scans in 230 entries and 0.21s.
- Tests proving the file is unreadable **from inside the sandbox**, not merely that a flag
  reached the argv — an argv assertion passes with the mount at the wrong path, which is the
  one mistake this feature can actually make. Paired with a control using a denylist that
  matches nothing, without which an unreadable file is equally consistent with a broken bind
  or a wrong HOME.

- `bash_calls`, `bash_failures` and `last_bash_exit` exist. They were described in four
  documents and implemented in none — `run_bash` refused every call, so there was no process
  exit to capture and nothing to count. ADR-0007's original subject, and the last of its
  claims that was still only a claim.
- `run_bash` is wired to `sandbox.py` and runs commands. Its result carries what the server
  measured as a field on the result block, never parsed back out of the text the model also
  reads: a trailer regex over prose stops firing the day the wording changes, and nothing
  reports that it stopped.
- `DELEGATE_MAX_BASH_OUTPUT_CHARS` caps combined output, keeping the **tail** rather than the
  head and stating the true length. A build says what went wrong on its last line. stdout and
  stderr are labelled separately, because a command that printed nothing and one that printed
  a warning are different facts a model cannot distinguish once they are merged.
- A timeout says it timed out, in words, and reports no exit code. A model summarising its own
  run must not be able to read "no exit code" as success.

- A steer from shell text-patching toward `write_file`, appended to that `run_bash` call's
  own result. When a command rewrites file text -- an in-place `sed`, a redirect into a path,
  `tee`, `patch` -- the note says `write_file` replaces a file whole and avoids the quoting
  and partial-match mistakes an in-place edit fails silently on. On the result rather than in
  the system prompt because upstream found a prompt instruction did not stop the pattern on
  retry: it has to arrive next to the evidence, in the turn that decides what to do next.
  Advisory and never blocking -- the error flag and the measured outcome are identical with
  and without it. ADR-0024.
- It is gated on the **resolved** tool set the executor enforces, never the declared list.
  Steering toward a tool the same function would then refuse is worse than staying quiet.
- Seven patterns that must fire and six that must not, tested separately from the wiring. A
  note appended to every command is one the model learns to skip, which is the same as no
  note, so the negative half is what makes the steer worth having at all.
- Tests pinning that setup finishes before the command starts, so "covered up" never means
  "readable for a moment". A shadow that fails to mount aborts bubblewrap with the command
  never running -- an `echo` would have printed if the two overlapped -- and a shadowed secret
  read as the very first instruction, repeatedly, never appears. Written because the wording
  invited the question, and an invariant nobody can check is one that rots.

### Changed
- **`run_bash` is declared, and runs commands.** It has been withheld since M4, refusing
  every call, waiting on a sandbox that could confine it and a denylist that could cover
  secrets inside what that sandbox binds. Both landed above, so `WITHHELD_TOOL_NAMES` is
  empty and the route is open.
- Its description is reworded in the same commit, necessarily: a tool offered while its own
  description says `CURRENTLY REFUSES EVERY CALL` is worse than one not offered at all. That
  string is the model-facing contract, so this is a behaviour change and not a wording fix.
  It now states the confinement, the timeout, that the server reports the real exit code,
  and that `write_file` is the better way to change a file's text.
- `WITHHELD_TOOL_NAMES` is kept rather than deleted. Withholding is how this server says "a
  tool exists and cannot work today", which is a different statement from a caller narrowing
  one delegation, and rebuilding the mechanism under pressure is worse than keeping an empty
  set. It is now tested against a *synthetic* entry: an empty set makes every assertion about
  it pass for the wrong reason, and the next thing implemented before it is safe would find
  the mechanism quietly broken.
- `run_bash` no longer refuses unconditionally — but it is **still withheld from
  declaration**, so no model can reach it. The route is not open; this commit only makes the
  capture path real, and it is reachable without a mock because `execute_tool` takes its
  allowed set as a parameter and never consults the withholding.
- Three tests that pinned the unconditional refusal are replaced rather than deleted. The one
  asserting a working bubblewrap does not by itself open the route now asserts what actually
  holds it shut, the withholding. That test's own predecessor turned the sandbox off in
  config, a setting since removed. Each time the guard moved the test had to move with it,
  which is the argument for naming a test after the guard rather than after the tool.

### Fixed
- A directory named `.ssh` does not match the pattern `.ssh/**` — only its children do — so
  the first version of the scan shadowed no directory at all while believing it had. Every
  directory entry in the denylist was decoration. Directories are now matched by asking the
  question the pattern was written to answer, about a child rather than the directory itself.
  Found by running the integration tests in WSL, not by reading the walk; the three tests that
  caught it are the ones that had to fail first.
- Symlinks are skipped by the scan. Measured against a real bwrap: a shadow op on a symlink
  node does not follow it and does not create it, it aborts the whole invocation with
  `Can't mount tmpfs on ...: No such file or directory`. That is fail-closed and leaks
  nothing, but a `~/.ssh` symlinked into a dotfiles repository would have killed every
  `run_bash` call with an error naming neither the denylist nor the link. The earlier note
  that bwrap creates a missing mountpoint holds only for a plain path, not a link.
- The new bubblewrap tests carried a `skipif` on a missing bubblewrap but not the
  `integration` marker, so CI ran them and they failed. Either guard alone is insufficient
  and for different reasons: the skipif is what keeps them quiet on Windows, while CI
  *installs* bubblewrap and excludes by marker instead -- and there the sandbox cannot bring
  up loopback without CAP_NET_ADMIN, so every invocation exits 1. `needs_bwrap` is now one
  decorator applying both, because that is exactly the pair that got separated. Caught by
  CI, which is the backstop working rather than the gate; verified by reproducing CI's own
  selection against a real bubblewrap.
- `docs/AGENTS.md` said the denylist was enforced "by never binding matching paths into the
  sandbox". That was never how it could work, for the reason above.
- `last_bash_exit` survived a timeout with the previous command's exit code standing, so a
  killed command could report the `0` of the one before it — precisely the misreport ADR-0007
  exists to catch. A command killed on timeout did run, so it is the last one and reports no
  exit code; a *refusal* never started a process, so the previous exit code is still the true
  answer. The result block carries `ran` to separate the two. Caught by a test written for
  the semantics before the code was, which is the only reason the two were compared at all.


## #35 — 2026-08-29 — feat: a sandbox that is built, proven, and still not connected

### Added
- `sandbox.py`: the bubblewrap invocation `run_bash` has been refusing calls without since
  M4. Empty root, both mandatory `usr/lib64` and `usr/sbin` symlinks (ADR-0021), network
  denied unless asked for, and a bound persistent HOME so the real one stays absent rather
  than read-only. `build_argv` is pure, so bind order is asserted on Windows where no
  bubblewrap exists; what only a kernel can show is proven under WSL. Nothing calls it yet:
  `run_bash` stays refused and withheld until the mount-level denylist lands, adding a layer
  without opening the route.
- Bind order as a stated rule: HOME before the workdir, read-only toolchain binds before the
  read-write one. Invisible until two paths overlap, and the second's failure names the wrong
  cause — a read-only filesystem inside the directory the operator chose. ADR-0034.
- Network denial proven **by address, never by hostname**: a lookup fails whether or not the
  namespace is isolated, so the obvious test reports a sealed sandbox that merely had broken
  DNS. Verified against a real violation — with isolation removed it sees a live response.

### Changed
- `DELEGATE_SANDBOX_ENABLED` is **removed**. It promised that 0 was an explicit choice to run
  commands unconfined, but nothing downstream could see that choice, so a server with the
  sandbox off was indistinguishable from one with it on until something had already run
  unconfined. A no-op would still render as a working knob. ADR-0034. Its two regression
  tests now use `agents_dir` as their inert-setting specimen and say why one is needed: they
  are that scanner's negative tests, and one naming no real field would pass forever.

### Fixed
- `--dir` created the sandbox HOME *inside* the sandbox while the bind still needed a host
  source, so every command on a fresh install failed with `Can't find source path`, which
  reads as a mistyped setting. Found by running the tests, not by reading the argv.

## #34 — 2026-08-29 — feat: a delegation that notices it is running out of room

### Added
- Context-overflow handling, off by default, closing M4. A delegation that fills its window
  does not fail today — it keeps answering from a history the backend has quietly begun
  dropping. Now 70/85/95% of projected use tightens retention, nudges the model to wrap up,
  then aborts; and a prompt that stops growing while the loop is still appending reads as
  silent truncation, but only once this server's own eviction is ruled out by a count it
  recorded where it evicted, never by anything the model said (ADR-0007). On abort, a report
  puts that ledger beside `git status`, unreconciled: the disagreement is the finding.
- A window check before any of it arms: `context_window` was the operator's word and nothing
  verified it. The endpoint is asked once per model and **validates, never derives** — a
  disagreement disarms and names both numbers rather than adopting the endpoint's, since an
  auto-derived window is how upstream came to threshold against an architecture maximum.
- `diagnostics=true` on `delegate()`, a model-facing contract change: per-turn costs, and
  which files were re-read after this server dropped them. The ledger says a delegation was
  expensive; only this says whether the work was large or paid twice for the same bytes.

### Changed
- The reply reserve is a **fraction** of the window, never a token count, read in one
  function: a flat reserve worth holding on a 1M window is over 95% of an 8K one.
- The negative cache expires and only a *confirmed refusal* writes to it. Upstream's did
  neither, so one transient outage disabled the feature until a restart.
- A wrap-up nudge concatenates and never overwrites: the loop ends on any reply with no tool
  calls, so "understood, wrapping up" would have replaced findings already written.

### Fixed
- `attempts` accumulated at the end of the turn body, which the answering turn never reaches
  — a delegation that retried twice then answered reported `attempts: 0`.

## #33 — 2026-08-28 — docs: the audit M4 was owed, and the sentences it made false

### Fixed
- Six places described M4's machinery as unbuilt after it shipped: README's opening
  paragraph called the agentic loop and `files[]` non-working, and three TROUBLESHOOTING
  entries kept a *(not built)* marker whose contract says the server cannot produce them —
  so the index denied symptoms a reader can hit today. The inverse of what the previous two
  audits found, and harder to see: nothing goes looking for sentences a merge falsified.
- `docs/DISPATCH.md` put agent frontmatter, unstarted M6, at the front of reasoning-effort
  precedence and omitted the call argument that `resolve_effort` checks first.
  `docs/ARCHITECTURE.md` had it right, so the two disagreed and the owner of `loop.py` was
  the wrong one.
- Two row renderers in `gen_config_docs.py` disagreed: the leftover "Other" loop dropped the
  unit suffix, the **required** marker and the **Inert.** prefix, so two timeouts showed a
  bare number where every sibling said "seconds". Worse latently — the footer counts inert
  fields across all rows, so an inert setting in "Other" would be counted in the total and
  unmarked in its own row. One renderer now, and the three orphaned fields have sections.
  The gate could not see any of it: it compares the committed file against this generator,
  and the two agreed.
- A config docstring justified `retry_max_delay`'s cap with "nothing yet emits a progress
  notification". One does now, and the cap survives for a better reason the docstring gives
  instead: the notification fires at the top of a turn, and a retry wait sits inside one.
- `CLAUDE.md` omitted `gen_tools_docs.py` from its command list; `CONTRIBUTING.md`'s budget
  history was stale about its own subject for the second time; and a sentence explaining the
  owns-no-facts rule sat in the *(not built)* paragraph, appearing to justify the wrong rule.

## #30 — 2026-08-28 — feat: a caller can name the reply budget, and be believed

### Added
- `max_tokens` on `delegate()`, threaded through both dispatch paths and through every
  stage of the empty-answer recovery. There was no way for a caller to name a budget at
  all: the configured default was the only source, so a task needing a short reply paid
  the reasoning floor's 131072-token ceiling like everything else.
- A caller's number is honoured as given, up to the per-model cap, and is deliberately
  *not* raised to `thinking_max_tokens_floor` at high effort. Raising it would make the
  argument advisory, and ADR-0014's recovery already covers a caller who guessed too low
  -- at the cost of one extra dispatch, against an argument that would otherwise not mean
  what it says.

### Fixed
- The step-down stage re-resolves the budget for its new effort level, which made it the
  one place an explicit number could be silently replaced by the configured default. It
  now carries through. Proved by reintroducing the bug and watching the test fail with
  50000 against the 4096 that was asked for, rather than trusting a passing test.

### Changed
- Half of ADR-0024's constraint turned out to hold already, and the plan said otherwise.
  The floor has always been a `max()` over the configured value, so an operator lowering
  the ceiling never could suppress it, and there is no per-model bump -- only
  `max_tokens_cap`, applied last. Recorded in `PLAN.md` so the next reader does not go
  looking for a defect that was never there. The property now has a test that can fail.

## #32 — 2026-08-28 — docs: an invariant that described itself as unbuilt after it was built

### Fixed
- `CLAUDE.md` still said `allowed_tools` was "Also M4/M6 and unwritten; when you add one
  site, add the other". Both sites landed in M4 (#26), so the instruction addressed a
  reader who no longer exists, and the trap it was protecting had quietly changed shape:
  the risk is no longer forgetting to build the second site but editing one of the two and
  not the other, which returns enforcement to asymmetric with nothing to notice it. The
  bullet now names `declared_tools` and `execute_tool` and says which mistake is now the
  live one.
- Recorded there too that withholding a tool from declaration -- as `run_bash` now is --
  narrows only what is offered and is never a substitute for the execution check, since
  that is exactly the misreading the two-site rule exists to prevent.
- The adjacent `paths.py`/`sandbox.py` bullet reads as stale and is not: `sandbox.py` is
  genuinely unwritten, so the trap stands. What it gained is the consequence of the
  withholding above -- nothing reaches the sandbox path at all today, so writing that
  module makes live a route no test covers end to end.
- No mechanism found this. `docs_ownership.toml` registers `CLAUDE.md` with `owns = []`,
  so no gate ties it to the code it describes, and nothing will catch the next one either.
  Worth knowing rather than assuming the gate has it covered.

## #28 — 2026-08-28 — feat: the turn loop, and a delegation that can read for itself

### Added
- The agentic turn loop (M4). A delegation is turns now, not one shot: the model calls
  tools, the server runs them and returns results, ending on the first reply with none.
  `max_turns_default` and `max_turns_hard_cap` were fields nothing read.
- The final turn is declared with **no tools**, so a delegation cannot spend its budget and
  end on a call nobody will run. `hit_turn_limit` keeps that partial answer from reading as
  a chosen one.
- History eviction, honouring `keep_tool_results`: every turn resends what came before, so
  an untrimmed history costs the square of the length. The block and its `tool_use_id`
  survive behind a stub, as some backends reject a tool use with no matching result.
- Dedup of byte-identical calls, always on: that a repeat cannot change its answer is a
  fact, not a preference. A side-effecting tool clears the cache, since a file read before
  a write and again after differs. Gap: a re-read at another offset is not caught.
- One progress notification per turn (ADR-0018). Rendered nowhere, and not cosmetic: it
  resets the client's 1800s idle timer, which `dispatch_timeout` at 3600s outlives, so the
  client abandoned long delegations the server was still working on.

### Changed
- **`delegate()` is agentic by default.** Its description is the model-facing contract, so
  this is behaviour: it no longer promises a model with no tools, offers `read_file` and
  `write_file`, and reports the server's ledger rather than the model's account of its own
  work (ADR-0007). `allowed_tools` narrows the set; empty takes the one-shot path.
- Empty-answer recovery moved out of `run_one_shot` into a function the loop calls per
  turn; copying it would have been two diagnoses of exhaustion, drifting apart.
- `run_bash` is no longer declared: it refuses every call until the sandbox exists
  (ADR-0010), and ADR-0016 measured the first turn as often already wasted. Withheld
  server-wide; the refusal at execution stays, as a model can call what it was not offered.
## #31 — 2026-08-28 — fix: one scanner for the identifier checks, not two that disagree

### Fixed
- The identifier checks ran from two implementations rather than one, and they had
  drifted: a string one of them refused was accepted by the other. One caller carried a
  near-duplicate of `scan_text`, which had described itself in its own docstring as the
  shared implementation since before it was one. The copy never picked up everything the
  original grew, and nothing made it, because nothing compared them.
- Both callers now go through `scan_text`, and it and the file scan share one predicate.
  The label and the check name were all that genuinely differed, so they are all that is
  parameterised.
- Guarded in both directions on each surface: the checks are asserted to fire, and
  legitimate placeholders are asserted still to pass, because a scanner that refused
  everything would satisfy the first half on its own. A structural test asserts there is
  exactly one implementation -- every behavioural test would pass again if someone
  reintroduced a copy that happened to be correct that day, and it is the copy rather
  than its current contents that is the defect.
- Proved by reintroducing the fault and watching the right tests fail, rather than
  trusting a green run.
- Everything already published was re-run through the corrected scanner and is clean, so
  nothing needed changing. The audit was itself checked against planted specimens first:
  a zero-finding result means nothing until the thing reporting it has been shown to
  detect anything at all.
- `CLAUDE.md` overstated the previous coverage. It now describes the arrangement
  accurately and says to extend the shared scanner rather than copy beside it.

## #27 — 2026-08-28 — fix: the WSL virtualenv had two names and one of them was fictional

### Fixed
- `CONTRIBUTING.md` said `~/.venvs/cdl` and `README.md` said `~/.venvs/delegate`, for what
  is one environment, and only the second existed. All seven references now name the one
  that does.
- The cost was not a stale instruction. `test_paths.py`, `test_context.py` and
  `test_tools.py` skip on Windows with a message telling the reader to prove the skipped
  test under WSL, and that message named `~/.venvs/cdl/bin/python` -- a command that fails
  before it reaches pytest. Those messages exist precisely so a skip cannot be read as a
  pass, so one naming an unrunnable command sends the reader away with the skip still
  unproven. Found the hard way: the tools tests needed WSL to exercise path layer 1, and
  the documented interpreter was not there.
- `README.md`'s setup installs the runtime only, with no `[dev]`, which is why the
  environment had no pytest at all. Correct for someone running the server, so the fix is
  to say where the test dependencies come from rather than to add them there.
- Guarded by a regression test asserting the copies agree, since five of them existed and
  nothing compared them. Negative-tested both ways: reintroducing the split fires, and so
  does dropping the path from a skip message, which would otherwise satisfy an
  agreement-only check by naming nothing.

## #26 — 2026-08-28 — feat: the model-facing tools, and both allowed_tools sites

### Added
- `tools.py`: `read_file`, `write_file` and `run_bash`, with `allowed_tools` enforced when
  the list is declared to the model *and* again when a call arrives. Filtering only the
  declared list is advisory -- a model can call a tool it was never offered -- so the
  execution site does the work, and both live side by side so a new tool cannot reach only
  one. The permitted set is a parameter, never a config field.
- A refusal returns an error result rather than raising: `PathRefused` ending the call is
  right for prefetch, but mid-loop it would discard every turn already paid for.
- `run_bash` refuses every call: `sandbox.py` is M5, and ADR-0010 is explicit that
  unconfined is not the fallback -- a control that degrades to nothing is worse than one
  that is absent, because it is believed. Still registered, so the refusal is testable and
  the model is told rather than left to infer it; reads neither `sandbox_enabled` nor
  `run_bash_timeout`, which stay inert.
- `docs/TOOLS.md`, generated from the registry, because a description is the model-facing
  contract and rendering it from the strings actually sent is what stops the document
  describing a tool the model never got.

### Changed
- `paths.resolve_all` takes `must_exist`, `False` only for `write_file`, which creates. It
  relaxes the missing-file branch and nothing else: the parent must exist, a directory is
  still refused, and every other layer still runs -- writing to a secret path is worse than
  reading one, not better.
- The gate now checks that every document the manifest marks `generated` is in the
  freshness list. That list is hand-written, so a forgotten pair leaves a generated document
  unchecked, passing because nothing looked; proven by removing the new pair. The parked
  `docs/TOOLS.md` ownership entry is restored, `covers_not` corrected to DISPATCH.md since
  ADR-0032 moved `loop.py` there.

## #25 — 2026-08-28 — feat: check Conventional Commits on both surfaces that reach main

### Added
- The gate now refuses a subject that is not a Conventional Commit, on the pull request
  title and on every commit subject. CLAUDE.md and CONTRIBUTING.md have both required the
  convention since the first commit and nothing read either, so it held only by habit --
  and habit lapsed: five pull request titles carried `M1:`, `M2:` and `M3:` prefixes, and
  two of those subjects reached `main`, across eleven pull requests before anyone noticed.
- Both surfaces are checked because each is decisive in a different case. A squash takes
  its subject from the pull request title for a multi-commit branch and from the commit
  itself for a single-commit one, so guarding one leaves half of what lands unchecked.
  That split is visible in the drift: #12 and #14 merged with correct `feat:` subjects
  while their titles said `M2:`, and #11 and #15 carried the prefix into `main`. A title
  check alone would have missed the second pair's subjects; a commit check alone would
  have left every title wrong.
- It blocks rather than warns. A malformed title is repaired by editing the pull request,
  which is the difference between this and the secret scan on the same text: a leak is
  already published by the time CI sees it, so that stays a backstop, while this is a real
  gate. `Merge` and `Revert` subjects are exempt -- git writes them, so nobody had the
  chance to apply a convention.
- `CONVENTIONAL_TYPES` lives in the gate and CONTRIBUTING.md names the same six in prose
  for a human to read. A test asserts the two agree, because two copies of one fact is the
  drift the documentation scheme exists to prevent, and this one would otherwise be
  discovered the next time somebody added a seventh type to only one of them.
- Negative-tested in both directions: stubbed to report nothing, 3 failures; stubbed to
  refuse everything, a different 3. It also caught an existing fixture whose synthetic
  subject was the bare word `msg`, which is the cheapest evidence that it reads real input.
  The five historical titles were corrected in place, which a pull request title permits
  and a merged subject does not.

## #24 — 2026-08-28 — docs: one changelog section per pull request, and no size threshold

### Changed
- `CHANGELOG.md` is now one `## ` section per pull request, newest first, with `Added` /
  `Changed` / `Fixed` beneath it. ADR-0022 says append-only documents cap each entry rather
  than the total, and this one could not: `check_budgets` splits entries on `^## `, and the
  file's only section was `[Unreleased]`, so the marker would have read 600 lines as one
  entry and blocked at once. The 2026-08-27 audit found exactly that and withdrew the
  finding, concluding `ARCHIVE-AT` was the right instrument instead -- accepting a limit of
  the tool as a fact about the document. Sections per pull request remove the limit rather
  than working around it, so `BUDGET-PER-ENTRY: 30` now applies with no change to the gate.
  A merged section is never edited afterwards; a correction is a new section, as an ADR
  supersedes rather than overwrites.
- `ARCHIVE-AT` is removed outright, from the gate and from all three documents that carried
  it. It warned past a line count and pointed at a procedure that split by year, and
  `CHANGELOG.md` crossed the threshold with every entry in the same year -- no older year to
  move, and no action the warning could be answered with. It then fired on every commit,
  which is how a warning stops being read. Archiving is now asked for by a person.
  `check_budgets` still skips any path with an `archive` component. ADR-0033.
- The migration is a cut rather than a rewrite. Of 59 entries only 11 carried a number --
  the convention began at #16 -- so the rest could not become numbered sections without
  inventing the one field the new heading exists to carry. They move verbatim to
  `archive/CHANGELOG-2026-08.md`, and the new format starts at #20. The #22 entry was
  trimmed to fit the cap it introduces, which is the rule being paid for rather than
  grandfathered.

## #23 — 2026-08-28 — docs: the budget ledgers stopped tracking their own budgets

### Changed
- Four documents carry a comment recording why their line budget moved -- the four whose
  budgets have moved -- and `docs/ARCHITECTURE.md`'s had drifted in two ways. It narrated
  300 to 340 to 375 to 425 and stopped, while the marker above it reads 330: ADR-0032
  lowered it after the split and the ledger was never told, so a reader following it landed
  on a number that is not there. The entry recording the drop is added.
- The other drift is the interesting one. A ledger records why lines were spent, which is
  bookkeeping about the document; this one had slipped into asserting the state of the
  system -- "the two bounds that do NOT yet exist" -- in a file whose whole job is to be
  what is true now, and enforcing `dispatch_timeout` had just made half of it false.
  Rewritten to say what the lines were spent on rather than what is currently missing,
  which is the only tense a ledger can hold without going stale.
- PLAN.md gains the entry it should have had when its budget was raised 220 to 245 earlier
  the same session -- the convention being broken while it was being audited.

## #22 — 2026-08-28 — feat: enforce dispatch_timeout, a gap rather than a decision

### Added
- `dispatch_timeout` is enforced. It was declared, validated against `turn_timeout`,
  documented as a working knob, and read by no module -- `loop.py`'s own docstring called it
  "a gap rather than a decision". Each attempt was bounded by `turn_timeout` inside the
  adapter's client, but the sum of attempts, the three empty-answer recovery stages, and the
  backoff waits between them was bounded only by `retry_max_attempts` and `retry_max_delay`,
  neither of which is a time. An exhausted max-effort delegation is measured at tens of
  minutes (JOURNAL 2026-08-27), so the unbounded case was reachable rather than theoretical.
- One deadline is taken at the top of `run_one_shot` and shared by every stage below it.
  Per-stage budgets would have made the setting bound three times what it says, and no test
  of a single stage would have noticed. It is enforced at three points, because a deadline
  checked in one of them can be walked past: before an attempt, as a ceiling on that attempt
  taken from what is left, and against each backoff wait before sleeping. The third matters
  most -- sleeping first spends the remaining budget and then reports a deadline reached by a
  wait this server chose rather than by the work.
- `DispatchTimedOut` is deliberately not a backend failure. Those say the endpoint did not
  answer; this says it may be answering perfectly and the delegation has outlived what the
  operator allows, which sends the caller to a different fix.
- Scope stated plainly, because the obvious reading is wrong: this does **not** keep a
  delegation inside Claude Code's 1800s stdio idle timeout. The default is 3600s, twice
  that. Only ADR-0018's per-turn progress notification addresses the idle timeout, and it
  arrives with the turn loop. The setting's own description claimed a notification "is
  emitted every turn", present tense for something unbuilt; corrected in the same edit.
- Negative-tested by neutering the deadline: 5 failures, with the three cases asserting an
  unaffected delegation still passing. That run took 30 seconds against 0.3, because the
  hanging-backend case really does wait once the per-attempt ceiling is gone.

## #21 — 2026-08-28 — fix: the inertness scan counted a mention in prose as a use

### Fixed
- The inert marker in the generated configuration reference no longer counts a mention in
  prose as a use, so a setting nothing reads stops rendering as a live knob. `_unread_fields`
  scanned each module as raw text and collected every identifier-shaped word, which made a
  name written in a comment or a docstring indistinguishable from a name the code actually
  reads. It now parses each module and collects the names and attributes the syntax tree
  actually references: a comment is not in the tree at all and a string literal -- docstring
  included -- is a constant, so no identifier is read out of either.
- Parsing rather than tokenising is load-bearing, and CI caught why. Keeping NAME tokens
  gives the right answer only from Python 3.12, where PEP 701 split f-strings into their
  parts; on 3.11 an f-string is a single STRING token, so `f"{cfg.some_field}"` is a real
  read that reads as prose. This feeds a *generated* file, so that would have made the
  rendered document depend on which interpreter rendered it -- the 3.11 leg of the matrix
  failed on exactly that while 3.12 passed.
- Exactly one setting was affected, and the way it was affected is the point.
  `dispatch_timeout` is read by no module, and is named in two comments that say so. The
  sentences documenting the setting as dead were the sole reason the reference presented it
  to an operator as working. A truthful comment suppressing the marker that repeats it is
  the same shape as the four checks this project has already found reading the wrong thing,
  and it fails in the direction `_unread_fields`'s own docstring calls the dangerous one:
  over-marking is visible and gets fixed, under-marking restores the bug.
- Measured rather than reasoned about: 19 settings marked against 20 genuinely unread. The
  count in the rendered table moves 19 to 20 and nothing loses its marker. The regression
  test asserts against a synthetic source tree, never the live one -- a test pinning
  `dispatch_timeout` would stop testing the moment the field is enforced while still passing.

## #20 — 2026-08-28 — docs: move the exit-code item to M5, and give PLAN.md room to annotate M4

### Changed
- M4's real-exit-code item moves to M5, beside `sandbox.py`. ADR-0010 has `run_bash` refuse
  rather than run unconfined, and `sandbox.py` is M5, so through the whole of M4 there is no
  process exit for the server to capture. Left where it was, the item could only have been
  built against a mock, with no path a caller could reach -- a test that cannot fail, which
  is the exact shape this repository has now found five times and the reason the rule
  against it exists. M4 goes to 10 items and M5 to 8, with the totals unchanged.
- PLAN.md's budget rises 220 to 245. It is raised rather than trimmed because PLAN.md grows
  monotonically by design: ADR-0003 keeps completed items with their annotations and
  cancelled items with their reasons, so its length tracks the milestone count and a fixed
  ceiling is the wrong instrument. It was at exactly 220 of 220, so annotating a single M4
  item on completion would have blocked.
