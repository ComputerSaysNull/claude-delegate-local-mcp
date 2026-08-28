# Changelog archive — up to 2026-08-28

Closed when `CHANGELOG.md` moved to one section per pull request. These entries predate
that format: 48 of them predate the pull-request numbering convention entirely, so they
could not be rewritten into per-PR sections without inventing numbers that never existed.

Moved verbatim and never edited. `check_budgets` skips any path with an `archive`
component, so nothing here is budgeted.

### Added
- 2026-08-27 The empty answer is now reproduced against the live cluster and recovered from
  there, not only against a backend that was told to return the signature. Every other test
  of this path scripts the failure, which proves the state machine reads its own inputs and
  nothing about whether a real model still produces those inputs -- and ADR-0014's recorded
  numbers had in fact already drifted (JOURNAL 2026-08-27). Two cases: the budget retry
  answering after a first dispatch too small to finish in, and the terminal verdict after a
  step-down, each asserting the mitigation was really spent rather than just the outcome.
  Both are deliberately cheap, because reproducing exhaustion needs a *small* budget: they
  run in under three minutes together, where one full-cap max-effort exhaustion takes tens
  of minutes.
  Writing it surfaced a trap worth recording, since it is the same shape as the bug the
  suite exists to prevent. The first version capped the model at the same number as the
  first budget, which -- the cap being applied last, and to everything -- also pinned the
  retry, so the stage skipped and the recovery fell through to the step-down. The test still
  passed. Then the floor was set to 4096, below the 7033 tokens this prompt actually needs at
  low effort, and it fell through again. Both times a green test was exercising a different
  stage than its name claimed, and only asserting the *level* the dispatch ended on caught
  it. The numbers now come from the measurement instead of from plausibility.
- 2026-08-27 An answer that comes back empty because reasoning ate the whole budget is now
  recovered from instead of merely labelled. The failure is measured, not hypothetical
  (ADR-0014), and until now the server named it and handed it back, with the tool
  description asking *Claude* to retry at a lower effort -- which spends cloud tokens and a
  round trip on a decision the server is better placed to make, having the budget, the level
  and the finish reason in front of it.
  Three stages: send; on the signature retry once at the larger of double the budget and
  `thinking_max_tokens_floor`, keeping the effort level; if still empty, step the level down
  once and send again. Not a cascade through every level -- each stage is a real generation
  at the largest budget the model allows, so a four-step climb down would cost more than the
  answer while the caller waits.
  The order is deliberate and is the opposite of obvious. Raising the budget keeps the
  rendered prompt identical apart from one number, so the prefix cache survives it; stepping
  the level down changes the prompt itself, because effort is part of it (measured, JOURNAL
  2026-08-26), so that dispatch misses the cache and pays a fresh prefill on top of its
  generation. The cheap mitigation goes first. The stepped budget is resolved again for the
  new level rather than inherited, since a lower level no longer needs headroom the higher
  one forced up. The step table is derived from `EFFORT_LEVELS` rather than written out: a
  second copy would stop agreeing with it silently, by stepping to a level that no longer
  exists or skipping one that does.
  The trigger is narrow on purpose -- empty text **and** a length stop. An empty answer at
  `finish_reason: "stop"` is a model that genuinely had nothing to say, and a length stop
  with text in it is ordinary truncation where an answer exists and is merely cut short.
  Firing on either would buy a full generation, twice, for nothing; both have their own
  regression test, because over-firing here is as expensive as under-firing.
  One stage is skipped rather than spent: where the model's own cap already pinned the first
  budget there is no larger budget to retry at, and re-sending the request unchanged pays a
  generation for a result that cannot differ.
- 2026-08-27 `delegate()` now returns `reasoning_exhausted` beside `empty_response`, because
  they are different claims and only the second was ever being made. `empty_response` stays
  the mechanical fact. `reasoning_exhausted` is the diagnosis, true only after a larger
  budget and a lower level have both been spent -- ADR-0014's `reasoning_exhausted_budget`,
  and the point at which the phrase is earned.
  The distinction is not cosmetic; the two states send a caller to opposite fixes. Empty
  after both mitigations means the task needs more reasoning than this model finishes inside
  its budget, so split it or send it elsewhere. Empty with the level *already* at its lowest
  means there was nothing to step down and the budget was simply too small -- and reporting
  that as exhaustion would tell the caller to lower an effort that is already lowest, which
  is a wrong instruction rather than a vague one. It has its own regression test.
- 2026-08-27 A dispatch now survives a failure that was never about the request. Until now
  a single dropped route, a restarting endpoint or a 503 during a model reload ended the
  delegation, and the caller's only recourse was to ask Claude to try again -- which spends
  Claude tokens to work around someone else's transient hardware. `loop.py` retries above
  the adapter, so the adapter stays a translator and there is exactly one place that
  decides what a failure means.
  Selective, not blanket: unreachable is always retried, a refusal only for 429, 500, 502,
  503 and 504, and a protocol error never. Those statuses are a module constant rather than
  a setting, because which HTTP codes mean *temporary* is a fact rather than a preference,
  and the set is exact -- 400, 401, 403 and 404 describe the request, and sending the same
  request again cannot change the answer. base.py always said a refusal was "usually" not
  worth retrying; this is the code that makes that word mean something. On exhaustion the
  last real exception propagates unchanged, because the four error kinds are distinguishable
  precisely so the layer above can act on them and a "gave up after N" wrapper would throw
  that away.
  `Retry-After` is honoured in both spellings RFC 7231 allows -- a count of seconds and an
  HTTP-date. Reading only one would honour the header by luck, since which arrives is the
  server's choice. An unparseable or absent header falls back to exponential backoff rather
  than failing the call; it is a hint from someone else's machine, not a contract. An
  honoured delay is used as sent and deliberately not jittered: jitter decorrelates clients
  that are guessing, and a server that named a time has removed the guess. Ordinary backoff
  is jittered across the full range.
  New `retry_max_delay`, capping any single wait including an honoured one. Not decoration:
  the wait sits between requests, where no HTTP timeout reaches it, so an uncapped header
  stalls the delegation for as long as it likes. It is deliberately small because two
  bounds that would otherwise cover this do not exist yet -- `dispatch_timeout` is declared
  and enforced nowhere, and the per-turn progress notification that holds off the client's
  30-minute stdio idle timeout is ADR-0018 and lands with the turn loop. Both gaps are
  written down in ARCHITECTURE.md rather than left for a reader to assume are covered.
  `BackendRefused` carries the `Retry-After` header verbatim and unparsed, for the same
  reason it already carried the body and `finish_reason` is still raw: the adapter
  translates and does not interpret. The response object does not survive the exception, so
  a retry that could not see the header could not honour it.
  The result gains `attempts` -- real calls made, counted by the server rather than inferred
  (ADR-0007). The token counts beside it describe the attempt that answered, not the sum, so
  a quiet success that actually took three tries is visible instead of invisible.
  One of the six checks written for this could not fail when first written: the test
  asserting an honoured `Retry-After` is *not* jittered used a jitter stand-in returning the
  top of its range, so a wrongly-jittered 7 was still 7 and the assertion held either way.
  Found by mutating the implementation and watching the suite stay green, not by reading it.
  The stand-in now returns a value that could never be mistaken for an honoured one.
- 2026-08-27 `delegate(files=[...])`: the server reads the files itself and gives them to
  the local model, so their contents never enter Claude's context. This is the thing the
  repository is for -- until now the only way to have a file reviewed was to read it into
  Claude's context first and paste it into the task, which spends exactly what delegating
  was meant to save.
  Per file, in this order, because the point is never to read a file only to find out it
  was unusable: `stat` against the byte ceiling, then a token estimate computed from the
  stat size and the extension (ADR-0019), then the per-file cap, then the running total,
  and only then the read. A file over any cap is left out **whole** -- source cut
  mid-function is worse than absent, because the model will confidently repair code it
  never saw -- and the total budget stops the list at the first file that does not fit
  rather than packing in whatever happens to be small enough, which would make the result
  depend on a size mix nobody can predict from the request.
  Files are sorted by resolved path *before* anything is accumulated, so the same set in a
  different order produces byte-identical output; the cluster's prefix cache is the whole
  reason (ADR-0011), and it fails silently, costing prefill with no error to attribute it
  to. The prompt shows the resolved path rather than the caller's spelling for the same
  reason: a symlink and its target would otherwise render as two prompts for one file.
  Everything skipped is named in the prompt *and* in the result. The model needs it or it
  answers about a file it never saw; the caller needs it because an answer covering five
  of six files reads exactly like one covering six.
- 2026-08-27 Binary detection: a NUL byte in the first 8 KiB, plus a strict UTF-8 decode.
  Extension cannot answer this -- the allowlist admits `.json` and `.md`, and nothing stops
  either being UTF-16. The decode is strict on purpose: `errors="replace"` would hand the
  model a page of replacement characters and present it as source. ADR-0030.

### Changed
- 2026-08-27 (#19) `docs/ARCHITECTURE.md` splits, and `docs/DISPATCH.md` takes `loop.py` and
  `backends/` with the four sections that describe them. It sat at 423 of 425 lines with
  M4's turn loop -- also `loop.py` -- about to land on top of it, which both audits of the
  day flagged and the second called overdue.
  The interesting part is that the obvious cut does not survive the gate. Splitting along
  the prose seam, narrative from reference, produces a document with the same audience as
  its parent and a subset of its owned code, which `check_split_dodge` blocks as a budget
  being evaded rather than a split -- correctly. The check turns "where does the prose
  divide" into "which module does the new document own", which has exactly one answer here.
  Ownership moves with the prose, so neither document describes code it does not own.
  ARCHITECTURE.md's budget drops 425 → 330 rather than keeping the headroom that produced
  the problem, and DISPATCH.md is budgeted at 180 against 137 used, sized for the turn loop
  it will now receive instead. ADR-0032.
- 2026-08-27 (#18) `check_split_dodge` no longer takes a changed-file list it never read. The
  parameter had been there since the check was written, ignored on every call: the check
  needs the files being *added*, and `changed_files()` reports that a path changed rather
  than how, so it queries git itself. An argument that is accepted and discarded is worse
  than either having it or not -- the signature describes a function this is not, and it
  invites a later reader to "fix" the check by wiring the wrong list into it. The docstring
  now says why the list is not taken, so the next person to notice stops there. Found while
  writing the check's first negative test, which is the usual way an unused argument
  surfaces: nothing exercises it, so nothing contradicts it.
- 2026-08-27 (#18) Six gate checks now have negative tests, so the gate is trusted for a
  reason rather than by habit. `check_budgets`, `check_orphan_docs`,
  `check_manifest_docs_exist`, `check_split_dodge`, `check_secret_paths` and
  `check_pr_text` had none: the existing suite covers the four checks already caught
  validating nothing, by name, and nothing covered these. Two of them guard the pull
  request title and body -- the surface no commit hook can see -- so they had been running
  unverified on every pull request this repository has ever opened, including the two that
  landed the audit which found this.
  Each is asserted in both directions, because one alone is not a test: a check that always
  fires passes a fires-on-violation test, and a check that never fires passes a
  silent-on-clean test. Only the pair tells them apart. `check_secret_paths` had exactly
  that half-test already -- one case proving it does *not* flag the policy list, and
  nothing proving it flags anything.
  The tests were then themselves negative-tested, which is the step the project convention
  exists to force: each check was neutered to `return []` in turn and the suite re-run, and
  every one produced failures matching its assertion count. Two tests written for an
  earlier self-defeating check had passed against the bug before being rewritten, so
  proving the check fires is not the same as proving the test would notice if it did not.
- 2026-08-27 (#17) The generated configuration reference now marks settings that nothing reads.
  Nineteen of forty-two -- the sandbox, the model-facing tools, admission control -- were
  rendered identically to settings that work, so a third of the reference read as live
  knobs. Worse than a documentation fault, because those settings are still validated at
  load: the server can refuse to boot over a value for a subsystem that does not exist.
  The marker is computed by `gen_config_docs.py` scanning the source for readers, not kept
  by hand: a hand-kept list would be a second copy of `PLAN.md` and would rot the day a
  subsystem landed, where a scan clears itself in the commit that starts using the setting.
  Negative-tested in both directions, including a test that the marker can actually vanish,
  because a marker that cannot clear is a stale list wearing a generator's costume.
- 2026-08-27 The second documentation audit of the day, and the eighteen findings it
  landed. Two fault classes account for most of them, and the first audit had found one
  instance of each without seeing either as a class. Facts restated outside their owning
  document: the bytes/token measurement existed in six places in copies that had already
  diverged, and now lives only in JOURNAL 2026-08-25 with everything mutable linking to it.
  Machinery described in the present tense before it exists: nine impossible symptoms in
  `docs/TROUBLESHOOTING.md`, two `CLAUDE.md` invariants about unwritten modules, and a
  format line in this file promising a `(#PR)` citation that no entry carries -- written,
  as it happens, by the commit that fixed the same fault in the entry above it.
  Two gate constants were being restated as prose and one had already gone stale; they are
  now described rather than copied, because making three copies agree is not a fix.
  ADR-0031 corrects ADR-0005's claim that agent files are portable to Claude Code -- they
  are not, and this repository's own `.claude/agents/` were the counter-example all along.
  Findings and the three rejected on verification are in `docs/audits/2026-08-27-audit-2.md`.
- 2026-08-27 (#16) `scripts/release.py` is cancelled before being written, and the CHANGELOG format
  no longer promises a commit hash. The script's only job was to backfill each entry's sha at
  tag time -- and the sha is the wrong thing to cite here, because `main` is squash-merged, so
  a branch hash names an object that never leaves the clone it was made in. The PR number
  survives the squash and is written by hand in the same edit as the entry, so there was
  nothing left for a script to do. Removed from `docs_ownership.toml`, which had pre-registered
  a path that will now never exist.
  This surfaced from a question about why the project squash-merges at all, and it was the same
  fault as the entry above: the format line described the script in the present tense while it
  did not exist. The 2026-08-27 audit missed it, and the audit record now says so, along with
  the reason the gate could not have caught it -- `check_manifest_docs_exist` verifies that
  owning *documents* exist, and nothing verifies the code paths beside them.
  PLAN.md's five unreachable hashes now cite their pull requests instead. Mapped by content
  against the squashes on `main` rather than assumed: `9e03113` is PR #4 by identical title and
  file set, `be4abc7` and `d4a4fad` are the two halves of PR #11. The M0 hashes are left alone
  because those commits really are reachable on `main`; only the ones a squash discarded were
  changed.
- 2026-08-27 (#16) The audit-due counter now fires at 15 commits rather than 60, and the first
  recorded audit lives in `docs/audits/`. Sixty was chosen when the repository had fourteen
  commits, where it read as "a long while"; at the rate this one actually moves it is months,
  and a whole milestone can land and go stale inside one interval. The evidence is the audit
  itself: run at 26 commits — well under half the old threshold, so the counter had not asked
  — it found two wrong entries in `docs/TROUBLESHOOTING.md`, one of which had been wrong since
  the day it was written. A threshold that only fires after the damage is one that never fires.
  The test for this check had its own copy of the number, written as a literal 60. Every case
  in it churns threshold-plus-one commits, so lowering the gate's value would have left the
  tests green while testing a boundary that had moved, and raising it would have left them
  green while testing nothing near a boundary at all — a check that cannot fail, in the file
  whose whole subject is a check that could not fail. It now reads the constant from the gate,
  verified by temporarily changing the gate and watching the test follow it.
- 2026-08-27 (#16) Two stale entries fixed in `docs/TROUBLESHOOTING.md`, found by the audit above.
  A symptom entry claimed an `HTTP 400 ... thinking_token_budget` was "feature-detected and
  dropped automatically" — nothing ever detected it, because the adapter has never sent the
  field, so the symptom cannot arise through this server. Deleted rather than reworded: this
  document owns no facts, and PLAN.md and ADR-0017 carry why the field is untouched. It was
  wrong on the day it was written, and M3's probe is what exposed it.
  The other indexed a symptom as `reasoning_exhausted_budget`, which is ADR-0014's name for
  the decision and not the field a caller sees. A symptom index keyed on a string that appears
  in no output cannot be searched by the person who needs it. It is now `reasoning_exhausted`,
  split into the two cases M3 created, because they have opposite fixes.
- 2026-08-27 M3 is scoped down and its four context-economics items move to M4, because
  they read conversation history, evictions and an action ledger that only the turn loop and
  `tools.py` produce. Building them here would have been four commits whose only caller was
  a test, with an `off by default` flag controlling nothing -- and "off by default" buys
  safety for an operator upgrading, not for code with no call site. The design work is
  recorded under M4 rather than deferred blank, including the finding that the denominator
  must be `ModelEntry.context_window` and never the dataclass's own 131072 fallback that an
  entry omitting the field inherits silently, and that the reserve has to be a fraction of
  the window rather than a flat count.
  One of upstream's five negative tests was evaluated and dropped: it needs a local-only
  gate that also blocks an explicit override on a cloud backend, and ADR-0008 ships no cloud
  backends while `context_window` is always operator-set, so there is no two-branch gate to
  break. A synthetic two-tier fixture would have passed whether or not the code had the
  flaw, which is the kind of check this repository already knows is worse than none. Named
  and dismissed in PLAN.md rather than left as a silent gap.
- 2026-08-27 Feature-detecting the `thinking_token_budget` rejection is cancelled, not
  deferred, and PLAN.md keeps the item with the reason. There is nothing to detect: the
  adapter never sends the field, which is the strongest available form of ADR-0017's "never
  rely on it", so no 400 ever arrives. A detector for a request we do not make is machinery
  guarding nothing.
  Re-probed before deciding, per ADR-0017's own lesson that this stack's docs and its
  behaviour disagree. Still refused, still naming `VLLM_USE_V2_MODEL_RUNNER=0`. The probe
  earned its keep anyway: the error body carries `param: null`, so the structural check the
  item was going to key on (`error.param == "thinking_token_budget"`) would have matched
  nothing on every call, with the text fallback silently carrying the whole feature while
  looking like a safety net. ADR-0017 stands unedited; JOURNAL 2026-08-27 has the body.
- 2026-08-27 `delegate()`'s tool description again, and again as a behaviour change rather
  than a wording one (CLAUDE.md's invariant). It used to tell the caller to retry an empty
  answer at a lower effort. The server now does exactly that itself, so the instruction
  became false *and* wasteful -- a caller following it would repeat, at cloud-token cost,
  work that had already been done. It now says plainly not to retry, that a dropped route
  and an empty answer are both handled below it, and how to read `empty_response` and
  `reasoning_exhausted` together, since those two point at different fixes. It also
  documents `attempts`.
- 2026-08-27 `delegate()`'s tool description, which is the model-facing contract and so is
  a behaviour change rather than a wording one. It previously told the model it could not
  read any file and to paste code into the task; both halves are now false. It says to name
  files instead of pasting them, that a refused path fails the whole call before anything is
  dispatched, and that a skipped one does not -- and it tells the caller to read
  `files_skipped` before trusting an answer, because the model cannot report what it never
  saw.
- 2026-08-27 The one-shot system prompt, for the same reason: it asserted "you have no tools
  and no access to any file", which stopped being true. One constant now covers both the
  files and no-files shapes rather than one for each -- two prompts would be two cached
  prefixes, so a caller alternating between the shapes would miss the cache on every other
  call, over wording that has nothing to do with the difference. ADR-0011.
- 2026-08-27 `paths.py` and `wsl.py`: the four-layer path policy, and the one place a
  Windows path becomes a POSIX one. Nothing calls them yet -- `files[]` is the next
  commit -- but they land first and separately, because a policy reviewed on its own is
  reviewed, and one that arrives inside the feature it guards is skimmed. Layer 1
  `realpath`s the candidate *and* the roots before comparing them, which is the whole of
  the symlink defence rather than an optimisation of it: a link inside a root pointing out
  of it passes a check on the path as written. Layer 2 matches a suffix, then -- for a file
  that has none -- the whole filename, because `Path(".gitignore").suffix` is empty and
  suffix matching alone silently refused the four allowlist entries that are filenames
  rather than extensions. Layer 3 matches every trailing run of path components, not the
  basename and the absolute path: `.git/**` is a suffix match and neither of those, so the
  obvious reading left five shipped patterns unable to fire at all. Layer 4 batches one
  `git check-ignore --stdin -z` per work tree instead of a subprocess per file, and treats
  git's exit 128 outside a repository as not-ignored rather than as an error.
  Two of the layers read something outside the process, so both can be absent: a missing
  denylist file and a git that is not installed both raise rather than returning "nothing
  matched", because that result is indistinguishable from a clean pass in every log and
  every test. Every refusal names its layer and a remedy, and *all* of them are reported
  together -- a first-failure-wins policy makes a five-file review cost five dispatches to
  discover. A refused path fails the whole call; skips, which proceed, arrive with
  `files[]`. ADR-0006, ADR-0010.
- 2026-08-27 `delegate()`: one task in, one answer back, from a model running on your own
  hardware. The first thing in this repository that does the job the repository is for.
  Effort resolves explicit argument, then registry row, then global default, and is always
  sent rather than inherited from whatever the cluster was booted with; an unlisted level
  is refused before dispatch, because it has no translation into the server's vocabulary
  and finding that out mid-call wastes the call. The reply budget takes the reasoning floor
  at high effort and the per-model cap last, the cap being what the wire will actually
  accept. `finish_reason` and the token counts come back raw: M3's state machine needs the
  unread values to decide anything, so M1 declines to decide for it.
  The one hazard M1 cannot duck is the reply that is valid, empty, and stopped on length --
  the budget spent on reasoning with nothing left to answer with. Returned bare, that reads
  as a model with nothing to say and the caller reports a false result, so the result
  carries `empty_response` as a mechanical fact and the tool description says what an empty
  answer at a length stop means. It is not called `reasoning_exhausted_budget`: that word
  means every mitigation was tried, and in M1 none of them exist yet. ADR-0011, ADR-0013,
  ADR-0014.
- 2026-08-27 The MCP server, and the first tool on it. Until now nothing called the backend
  adapter: the whole package was configuration and a seam. `server.py` declares the tools
  and owns one backend -- and so one connection pool -- per registry entry for the life of
  the process; `main.py` fills the console-script entrypoint `pyproject.toml` has pointed at
  since M0b and which, until this commit, failed on import. Startup failures go to stderr
  and exit non-zero, because on stdio stdout is the wire protocol and a traceback written
  there corrupts every message after it with no symptom beyond a client reporting a dead
  server. The FastMCP banner is suppressed: it is drawn to stderr, but drawing it calls PyPI
  for a version check, and an outbound request on every launch is the wrong default for a
  tool whose point is that inference stays on hardware you control.
- 2026-08-27 `backend_status()`, which answers what a stack trace cannot: is the model I was
  told to use actually there. It probes `/v1/models` for every registry entry at once,
  bounded by the new `status_probe_timeout` rather than the generation-sized `turn_timeout`,
  so one blackholed endpoint cannot stall the report on the others, and each probe returns
  its failure as data so a dead model does not take the report down for the healthy ones.
  Reachability is the easy half. The half worth having is `id_confirmed`: an endpoint that
  is up and serving a *different* model than the registry names is invisible to every other
  check, and either refuses the delegation or answers it with a model nobody chose. That
  case reports `status: "ok"` with `id_confirmed: false`, because the endpoint is healthy
  and the configuration is not. Six status words, chosen so that each sends the reader
  somewhere different -- `auth_failed` is a `.env` edit, `backend_unreachable` is somebody
  else's hardware. The result never names the endpoint: a health report is what gets pasted
  into a bug report, which is exactly why it must be safe to paste. ADR-0029.
- 2026-08-26 The backend seam, and the one adapter behind it. `registry.py` has been
  refusing `api_format = "anthropic"` since M0b with an error naming a `Backend` protocol
  that did not exist -- a promise made in shipped code and payable by nothing. It now
  exists: a canonical, Anthropic-shaped request and response of content blocks, and an
  OpenAI-compatible adapter that is the only place a wire format is known. Failures arrive
  as four kinds the caller can tell apart, and `finish_reason` and the token counts come
  back uninterpreted, because the response state machine in M3 needs the raw values to
  decide anything and would be built on sand otherwise. ADR-0008, ADR-0013, ADR-0014.
- 2026-08-26 The upstream review has an artefact: a dated file under `docs/audits/`
  recording every upstream change considered and its verdict, rejections included. ADR-0001
  required the review and named nothing to hold it; CONTRIBUTING filled that gap with a
  pinned issue, which was destroyed along with the repository on 2026-08-25 and took the
  policy's only mechanism with it. The rejections are the point -- without them a later
  review cannot tell what was examined and dismissed from what was never opened. ADR-0023.
- 2026-08-26 First review under it, covering 2026-08-21 to 2026-08-25. Six items adopted as
  planned work, two rejected as already covered, and one negative result recorded: upstream
  established that in-flight synchronous tool calls cannot be cancelled and progress
  notifications are not surfaced to the calling session, which confirms ADR-0018's narrow
  claim and closes a question this project would otherwise have paid to reopen. ADR-0024.
- 2026-08-25 Project scaffold: configuration, model registry, generated configuration
  reference, and the docs/secrets gate. Configuration is a single frozen dataclass and is
  the only place a default exists anywhere in the repository; the reference document is
  rendered from it and the gate fails when the two disagree. This is a direct response to
  the ancestor project, which documents one setting as three different values in three
  places. ADR-0004.
- 2026-08-25 Model registry replacing name-prefix routing. The serving stack runs one
  model per inference instance, so a second model means a second base URL -- which a
  prefix table cannot express at all. One explicit row per model also collapses five
  separate prefix-keyed tables that previously had to be kept mutually consistent by
  hand. ADR-0009.
- 2026-08-25 Commit-authorship check. The global git identity on this machine is an
  employer address and this repository is public, so without it the first commit would
  publish that address permanently. Content scanners never see the author field, which is
  why this is a separate check rather than a pattern.

### Fixed
- 2026-08-27 The documented MCP registration could not start the server from anywhere but
  this repository, and the failure named the wrong thing. `wsl.exe -e` carries the *Windows*
  working directory across the boundary, so the server looked for `models.toml` inside
  whichever project Claude Code was open on and refused to boot with `Model registry not
  found` — naming a file that exists and is correct. The registration now pins `--cd`, which
  has a trap of its own: the Windows form of the path is accepted and the `/mnt/c/...` form
  is rejected with `ERROR_PATH_NOT_FOUND`. It also now names the console script by absolute
  path, because a venv's `bin/` is not on the PATH `wsl.exe -e` resolves against and the
  bare name fails with `No such file or directory` — which reads as "not installed" when it
  is installed. Both measured, not reasoned about. JOURNAL 2026-08-27.
- 2026-08-27 The install instructions told Windows users to create `.venv`, which on the
  WSL2 topology overwrites the Windows virtualenv of the same name in place; the result
  looks like a corrupted install rather than a collision. README now puts the WSL
  environment elsewhere, and on the native filesystem rather than `/mnt/c` (ADR-0020).
- 2026-08-27 README claimed the server emits a progress notification every turn to hold off
  the 30-minute stdio idle timeout. It does not: that is M4. A document describing unbuilt
  behaviour in the present tense is worse than one that omits it, because it stops anyone
  from looking for the cause when the timeout fires.
- 2026-08-26 `git commit --amend` was judged against the wrong parent. The owning-doc
  check compared the staged index against HEAD, which is right for a normal commit and wrong
  for an amend: the amend's parent is HEAD~1, and the files already inside the commit being
  amended are part of what lands while being absent from the index. A complete commit was
  reported as incomplete, and the workaround was to undo the commit and remake it. It fired
  twice while preparing this session's own commits.

  It cannot be detected outright — `git commit --amend` and `git commit -C HEAD` reach a
  hook identically as `source=commit, sha=HEAD`, with `GIT_REFLOG_ACTION` unset for both,
  measured in a throwaway repository rather than assumed. Treating that signal as "amend"
  would misjudge `-C HEAD`, and the failure would be a *pass*. So both readings are
  evaluated, strict first, and a pass that depended on the amend reading emits a warning
  naming the files it counted from the previous commit. ADR-0028.

  A `prepare-commit-msg` hook records the signal; the marker is consumed when read and
  rewritten on every commit, so an abandoned commit cannot leave state that changes the
  verdict on the next one. Five tests: three fail against the unfixed gate, and two assert
  the block still fires — an amend missing its document, and an ordinary commit — because a
  change that merely stopped blocking amends would have disarmed the check.
- 2026-08-26 Two checks in the gate and the suite could not fail for the reason they
  claimed. `HOSTPORT_ALLOWED` in `scripts/docs_gate.py` listed `127.0.0.1` twice and
  `0.0.0.0` once, all three unreachable: `HOSTPORT_RE` begins `[a-z]`, so a numeric host
  never matches and the allowlist is never consulted for one. Loopback in an example was
  passing because the pattern cannot see it, not because it was permitted — three entries
  that looked load-bearing and governed nothing. Removed, with the reason recorded where
  they were.

  `test_config_is_frozen_so_nothing_mutates_it_mid_delegation` asserted
  `pytest.raises(Exception)`, which accepts any failure whatever, including ones unrelated
  to frozenness. It now names `FrozenInstanceError`. Worth recording what the check
  disproved: assigning a *misspelled* field also raises `FrozenInstanceError`, not
  `AttributeError`, so the obvious escape route was never open — the assertion should still
  name the thing it proves.

  Both were surfaced by ruff, which is configured in `pyproject.toml` and has never run in
  CI.
- 2026-08-26 `.env` was never read. The README has said `cp .env.example .env` since the
  first commit, but `config.load()` consulted `os.environ` and nothing else — no dotenv
  dependency, and no `env` key in the README's `mcpServers` block either. Creating the file
  did nothing, reported nothing, and left every setting at its default: a documented setup
  step that was a no-op, which is worse than an undocumented one because it is believed.

  `config.load()` now reads it, in about fifteen lines with no new dependency. The real
  environment wins over the file, so an explicit override still works. Putting an `env` key
  in the MCP client's configuration was rejected and ADR-0027 records why: this project's
  topology launches the server as `wsl.exe -e claude-delegate-local-mcp`, so that key sets
  variables for `wsl.exe` on the Windows side, one hop short of the Linux process reading
  them — crossing the boundary needs `WSLENV` too, which fails silently when forgotten.

  Three behaviours exist to remove silent failures rather than add features: a file named
  explicitly and missing raises, while an absent `<repo>/.env` does not, because asking for
  a specific file is a promise; passing `environ` suppresses discovery, so the suite never
  reads whatever `.env` sits in the working tree; and a leading `export ` is stripped while
  a name that cannot be an environment variable raises, since both otherwise parse to a key
  no setting matches — read, accepted, ignored. Values are taken literally, because
  `DELEGATE_WORKSPACE_ROOTS` on Windows is full of backslashes. All fourteen tests were
  confirmed failing against the unfixed loader.
- 2026-08-26 The documentation gate judged the commit it was not making. In pre-commit
  mode both message-dependent checks read `.git/COMMIT_EDITMSG`, which git writes *after*
  the pre-commit hook returns — so both read the previous commit's message. The
  host-literal scan therefore passed on text nobody was proposing to commit while the
  message actually being written went unscanned, and the `Docs-Gate-Skip` waiver parser
  applied one commit's waiver to the next, an escape hatch firing on a commit that never
  asked for it. Confirmed on this repository: the first line of the stale file was byte
  for byte the subject of the commit already made.

  The gate now runs as a **commit-msg** hook, which git hands the real message file — the
  first stage at which the staged index and the message both exist. `--mode pre-commit`
  remains for running the structural checks by hand and reports the message check as
  skipped rather than guessing. Installing removes a superseded pre-commit hook so the two
  cannot disagree. `changed_files` had to learn the new mode too: without that it fell
  through to the CI branch, diffed against a non-existent upstream and scanned every
  tracked file — a whole-repo audit wearing the costume of a per-commit check.

  Two of the six regression tests initially passed against the unfixed gate, and were
  rewritten. One asserted `"SKIP" in output and "commit-message" in output`, which an
  unrelated skip elsewhere plus a block on this check satisfied independently; it now
  reads the levels reported for that specific check. The other asserted no waiver was
  applied while staging nothing for a waiver to suppress, so it held vacuously; it now
  stages a real owning-doc violation first. All six were confirmed failing against the
  unfixed gate before being trusted.
- 2026-08-26 A dropped route stalled for the whole turn timeout. The OpenAI adapter built
  one `httpx.Timeout(turn_timeout)`, which bounds connect, read, write and pool identically
  — 1800s by default. The comment defending that construction argued no separate connect
  bound was needed because a refused connection already fails immediately. That is true and
  it is beside the point: a refused connection sends RST and fails in milliseconds, while a
  dropped or blackholed route sends nothing at all, so nothing bounded the connect phase.
  Measured against the unfixed code: refused 0.02s, dropped still connecting after 40s. The
  symptom was a test suite that went from 22s to 294s whenever the route was unavailable.
  Connect is now bound by its own setting, `DELEGATE_CONNECT_TIMEOUT`, defaulting to 30s,
  and validated to be positive and no longer than the call it belongs to. Deriving it from
  `turn_timeout` was rejected: that puts a number outside `config.py`, where the reference
  table is generated from and checked against the dataclass.

  The regression test needed a second look. Its first threshold was 30s, which *passed*
  against the bug: an operating system abandons a dropped SYN on its own — near 21s on
  Windows, near 130s on Linux — so the observed failure never reached `turn_timeout` and
  slipped under a loose ceiling. The threshold now sits below both floors, and a companion
  test asserts the connect bound is installed on the client rather than inferring it from
  timing, so neither can pass for the wrong reason. Both were confirmed failing against the
  unfixed code before being trusted.
- 2026-08-26 A rationale had been copied into four error messages and a test. The claim was
  that the backend silently ignores an unrecognised reasoning-effort value; measured against
  the live server it does the opposite, validating the field and answering a bad value with a
  400 naming the accepted set. Falsity is not the defect though, only what made it visible.
  Of the six places stating it, exactly one was supposed to -- `ARCHITECTURE.md`, which owns
  the backend layer. Three were error messages, whose job is to name the variable, the bad
  value and the accepted set rather than explain a remote system, and the sixth was a test
  requiring the rationale to appear in the message, which turns prose into a tested
  requirement: correcting the message made a test go red instead of a bug go away. The fix is
  one copy in the document that owns the subject, and a local fact everywhere else. No new
  check guards it: "do not put a justification in an error message" is not mechanically
  checkable, and a scanner for the one sentence would guard the specimen rather than the
  class. The ownership rule is the defence. JOURNAL 2026-08-26, ADR-0013.
- 2026-08-26 `MODELS.md` said no level is silently remapped, which the translation of `off`
  to the server's `none` had just made untrue as written. Qualified: nothing is remapped
  downward, one level is renamed, and the strength never changes.
- 2026-08-26 `ARCHITECTURE.md`'s prefix-caching section named only the system prompt as a
  way to break byte-identity. The reasoning level breaks it too -- setting it rewrites the
  rendered prompt, so `prompt_tokens` moves with the level and the cache is per level, not
  per session. ADR-0011, JOURNAL 2026-08-26.
- 2026-08-26 The audit-due counter was disarmed by the upstream review committed alongside
  it. The staleness check took the alphabetically last file in `docs/audits/` as the last
  recorded documentation audit; audits are named `YYYY-MM-DD-audit.md`, so a name beginning
  with a letter sorts after all of them and wins permanently. The counter read 0 when the
  true figure was 14, the whole history, because no documentation audit has ever run here --
  deferring a warning due at commit 60 to 74, and further with every future review. Two
  faults, both now closed: reviews move to `docs/reviews/` so one directory holds one kind of
  record, and the check asks git for the most recent commit touching `docs/audits/` instead
  of dating a filename. Caught by asking whether the docs-audit agent had run, not by any
  check -- adding a file to a directory is a write to every check that reads it. ADR-0025.
- 2026-08-26 STATUS.md recorded where the repository happened to be when it was generated:
  a branch deleted on merge and a hash the squash rewrote, both naming something that did
  not exist, in a generated file whose whole appeal is that it can be trusted. The cause is
  that `--check` compares only the text above `## Repository`, correctly, because counts
  move with every commit -- which leaves everything below it verified by nothing. Rather
  than regenerate more often, the volatile facts are gone: counts stay, VCS position and the
  recent-commits list do not. A count that drifts is off by one; a branch name that drifts
  points at nothing. The fourth check found here that could not fail.
- 2026-08-26 The `docs/TOOLS.md` generator sat in M0b, which could never close: the
  document renders from a tool registry that M4 has not written yet, so the manifest named
  a phantom and warned about it on every run -- and a permanent warning is one nobody reads.
  Moved to M4, beside `tools.py`, and its manifest entry parked with the milestone that
  restores it. Deliberately not done to `docs/AGENTS.md`, which warns for the same reason:
  that document exists, and its claim on the unwritten code is the only thing that will
  force it to be updated when the code lands.
- 2026-08-25 The documentation gate could validate a generated file against code that no
  longer existed. Python validates a `.pyc` on `(mtime, size)`, so changing a default
  from `6` to `9` -- identical byte length -- inside one filesystem timestamp tick left a
  stale cache in place, and the generator then reported the document as current. Found by
  accident while testing. `python -B` does **not** fix it: that stops *writing* bytecode,
  not *reading* a stale cache, and the first attempted fix was therefore useless. Now
  every generator redirects `sys.pycache_prefix` to an empty temporary directory, forcing
  a real compile. JOURNAL 2026-08-25.
- 2026-08-25 The supersede-link check could never fail. It searched the whole document
  for the referenced ADR number, but the heading being validated contains that number
  itself, so the check always found its own needle. Now validated against numbers
  actually declared by headings. Found by negative-testing the gate rather than reading
  it -- the check had been reporting success since it was written.
- 2026-08-25 The secret-path check flagged `security/secret_globs.txt`, which matches its
  own `*secret*` pattern. A pattern list that describes itself trips on itself. The
  policy files are now exempt; `forbidden_strings.txt` cannot be affected because it is
  never tracked.

### Changed
- 2026-08-26 PLAN.md gained an `## Extra` section, and M1 stopped overstating itself.
  Six repository-tooling fixes made in passing had been filed under M1 because that is
  where the cursor was, so STATUS.md reported "M1 — 5 of 8 items done" while only two of
  the five were backend-call work. STATUS.md is the first thing read to decide where the
  project is, and it was reading further along than the truth. M1 now shows 2 of 5.

  Moving them introduced a quieter hazard, fixed in the same change: `gen_status` picks the
  current phase as the first section holding anything unfinished, and a new top-level
  section is a candidate like any other. Every Extra item is done today, so nothing would
  have gone wrong today — the pointer would have moved to Extra silently, the first time
  anyone recorded unfinished work there. `NOT_A_PHASE` now excludes it, alongside the
  existing exclusion for the deferred backlog.

  One of the seven tests written for this passed against the unguarded code and was
  rewritten: it appended an unfinished Extra item to a plan that already had open
  milestones, and `render()` returns the *first* unfinished item it meets, so the milestones
  masked it. It now makes Extra the only unfinished work. Third instance this session of a
  test that could not fail.
- 2026-08-26 ruff runs in CI, on a rule set that is written down rather than inherited.
  It was configured in `pyproject.toml` and had never run anywhere — believed to be
  enforcing something while enforcing nothing. Wiring it up as it stood would have been
  worse: `[tool.ruff]` declared no `select` and the dependency was `ruff>=0.6` unbounded,
  so the enforced set was whatever the installed version happened to default to. Measured
  on this repository at ruff 0.16.4: **3 findings under ruff's classic defaults, 45 under
  the installed version's**, with no line of the repository having changed between them. A
  lint job on a moving rule set fails a build nobody broke.

  So the set is pinned explicitly and the dependency bounded to `>=0.16,<0.17`. Each
  `ignore` carries its reason: literal comparisons stay (`PLR2004`) because naming `200`
  moves the number without explaining it; `PLW1510` and `PLC0415` are ignored under
  `tests/**` because those tests run a subprocess *expecting* failure and assert on the
  exit code themselves; `PLR0912` is ignored for `docs_gate.py`, whose check functions
  branch once per rule they enforce.

  The remaining findings are fixed. Two were worth more than tidiness: a `match=` pattern
  in `test_registry.py` had unescaped dots, so it matched more than its author intended,
  and a `for` variable in the new `.env` parser was being overwritten by its own strip.
  Parenthesising an implicit string concatenation went wrong once on the way and turned two
  list items into a tuple inside `gen_config_docs.py`; caught by reading the result, and
  every generator was then checked to produce byte-identical output.

  `lint` is added to the ruleset's required checks, so it gates rather than advises.
- 2026-08-26 A skipped live test no longer reads as a passing one. The two tests that
  touch the backend skipped with "endpoint unreachable" and nothing else, so a run that
  exercised none of the real path was indistinguishable at a glance from one that exercised
  all of it — `2 skipped` at the end of a green run. The skip reason now states that the
  backend is UNPROVEN BY THIS RUN and names the layer that stopped it, and reachability is
  established **by address**: resolution and connection are attempted separately and
  reported apart, because a combined failure cannot tell broken DNS from a missing route
  (ADR-0021). `BackendUnavailable` after reachability reports ok is now a failure rather
  than a skip; it means the guard and the client disagree. The address never appears in the
  reason — a skip reason reaches CI logs, and the layer is the useful part anyway.

  The connectivity entry in `docs/TROUBLESHOOTING.md` was wrong in a way that would have
  walked a reader past this project's own recent outage: it told you to check that the name
  resolves inside the guest, when the failure was resolution *succeeding* through a search
  domain and returning a different host entirely. It now asks whether both sides resolve to
  the **same** address. `docs/MODELS.md` carried the same advice and is corrected too.

  One vendor's product name appeared in three tracked files, including a gate finding
  message that prints into CI logs. All three now say "overlay VPN". `docs_gate.py` already
  stated the principle two lines below the offending label: not one vendor, because it
  should not advertise which kind of network sits behind it.
- 2026-08-26 The build-time agent roster in CONTRIBUTING.md is generated from
  `.claude/agents/*.md` rather than typed a second time. Model and effort for four agents
  existed in the frontmatter and were described again in a hand-written table; every value
  agreed, which is not the same as being kept in agreement. `scripts/gen_agents_docs.py`
  renders it between GEN markers and the gate runs its `--check`, the same anti-drift
  mechanism as the configuration reference (ADR-0004). A missing `effort` key renders as a
  visible `-- missing --` rather than a plausible default, because the runner ignores a
  misspelling in silence and bills the default tier. Four tests assert the check fires: on
  a changed effort, on a new agent file, and on a key renamed to `reasoning_effort`.
- 2026-08-26 CONTRIBUTING.md trimmed to its audience, and its budget lowered 215 -> 190
  by removing content rather than raising a ceiling. The manifest already said this
  document does not cover the documentation strategy — that lives in CLAUDE.md — yet it
  carried a "short version" of those rules under a link to CLAUDE.md saying so. A second
  copy under a pointer to the first is the drift the ownership scheme exists to prevent.
  The negative-testing rule was likewise restated with the same three examples CLAUDE.md
  already holds; it is now one sentence and a link. An audit also found the document
  miscounting its own history: it claimed its budget had been raised twice when the real
  sequence was 130, 175, 190, 215 — three raises, so the split it promised was already
  overdue. That sentence is corrected and the trip-wire re-armed against the real number.

  CLAUDE.md's "a check that cannot fail" invariant now records four instances rather than
  three. The fourth is the gate scanning the previous commit's message, and the note adds
  that two tests written for that fix passed against the bug before being rewritten — the
  rule applies to the tests as much as to the checks.
- 2026-08-26 The branch-protection rationale moved from CONTRIBUTING.md to ADR-0026.
  Two ruleset values are not self-explanatory and JSON carries no comments: `bypass_actors`
  is empty so direct pushes are refused for the owner too, and
  `required_approving_review_count` is 0 because GitHub forbids approving your own pull
  request — requiring one review would lock a single maintainer out of their own repository
  rather than raise the bar. That is the value most likely to be "corrected" as an
  oversight, and CONTRIBUTING.md is written for contributors doing work, not for explaining
  why the repository is configured as it is. The section there is now a pointer, 24 lines
  down to 8.
- 2026-08-26 Context-overflow handling promoted out of Deferred into M3. The upstream fork
  shipped it, and this project's parked wording had independently converged on the same
  detection signal, which is the reason to trust the design rather than re-derive it. It
  arrives with the five bugs it cost upstream attached as required negative tests; three of
  them are one bug -- a threshold computed against the wrong denominator. ADR-0024.
- 2026-08-25 The context prefetch budget is denominated in estimated tokens rather than
  bytes, with per-extension ratios measured against the model's own tokenizer. Bytes were
  the wrong unit and the error was not small: measured bytes-per-token ranges from 1.78
  for punctuation-heavy JSON to 4.16 for densely commented Python, so a byte cap silently
  allowed twice as much *context* for a data file as for a source file -- the opposite of
  what anyone wants. The previous `bytes / 3` estimator was described as conservative but
  under-counted JSON by 41%. Net effect for code: the per-file allowance rose from 128 KiB
  to about 145 KiB of Python. Prompted by a challenge to the original figure. ADR-0019.
- 2026-08-25 The unknown-extension token ratio is derived from the ratio table rather than
  written down separately, after the hardcoded fallback (1.8) drifted above the densest
  table entry (1.7) and stopped being the worst case it claimed to be.
