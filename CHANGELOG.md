<!-- BUDGET-PER-ENTRY: 131
     Raised to the size it had already reached on 2026-09-01: the check that should have held this
     line was disabled from 2026-08-28, when reasons moved inside this comment and the pattern
     stopped matching, so the document grew unenforced. This records where it actually is rather than
     endorsing it; the 2026-09-01 audit tracks the trim. Raised from 30 on 2026-08-29: 30 was sized
     for a single-feature pull request, and it had started deciding how work was split rather than
     how it was described -- a milestone finished in one pull request had to be broken into five to
     fit. The per-entry cap exists to stop an entry sprawling, not to cap how much one pull request
     may do. -->
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

## Unreleased — feat!: effort must be stated on every delegation

### Changed
- `effort` is required on `delegate`, `delegate_readonly`, `delegate_to_agent` and
  `delegate_batch`. **Symptom:** effort was being chosen by omission on almost every call,
  and a research delegation rerun at `high` found a defect that four earlier ones at the
  silent default had all missed. **Cause:** the argument was optional, and an absent value
  fell four links down a precedence chain — call argument, agent file, registry row,
  `thinking_default` — to `low`, so a caller who never considered the question and one who
  wanted the default looked identical. **Fix:** drop the default so the schema marks it
  required, and add `inherit` as the way a caller states that it is deferring. ADR-0045.
- `inherit` is accepted as an argument only, and refused as a value for `default_effort` in
  `models.toml` and for `DELEGATE_THINKING_DEFAULT`. Those are the two ends of the chain it
  defers along, so a value there would resolve to itself. It is normalised to `None` at the
  tool boundary, before the agent merge — a non-empty string is truthy, so leaving it in
  place would have skipped the agent file it exists to reach.
- The four tool descriptions now say what effort to pick, not only which words are legal.
  Model-facing contract, so this is a behaviour change rather than a wording one.

### Added
- Tests that the requirement can actually fail: every tool refuses a call naming no effort,
  and each of the three layers refuses `inherit` where it is not a level. Verified by
  restoring the default on one tool and watching the refusal test fail.

## #75 — 2026-09-02 — docs: archive the closed roadmap and retire the generated status file

### Changed
- M0a through M7 and `Extra` move verbatim to `archive/PLAN-milestones.md`, following
  `archive/CHANGELOG-2026-08.md`. `check_budgets` skips any path with an `archive` component,
  which is what `PLAN.md`'s own header has prescribed for a recurrence since 2026-08-28 —
  and two budget raises in one day, 413 to 439 to 477, made the recurrence undeniable.
  `PLAN.md` goes from 477 lines to 171 and its budget resets from 477 to 200. `Extra` is
  archived too: it was held apart so a milestone's counts meant what they said, and with the
  milestones gone there is nothing left for it to be apart from. Checked rather than
  eyeballed — 98 item lines before and after, and the sorted multiset of item and
  continuation lines across both files is byte-identical to the one file it came from.
- `PLAN.md`'s header drops its accumulated raise history, because the reason for each raise is
  already in the `CHANGELOG.md` section for the pull request that made it. Completed items stay
  in `PLAN.md` as a record of recent work; passing the budget is now a prompt to ask whether any
  are ready to archive rather than a requirement to move them. The hash note goes with the items
  it described —
  all fifteen commit-citing items are 2026-08-25 and now live in the archive.

### Removed
- `STATUS.md` and `scripts/gen_status.py` are retired (ADR-0044). The file answered a real
  question while `PLAN.md` was 477 lines; at 171 lines of open work `PLAN.md` is its own
  snapshot, and a generated summary of a short document is the second copy this project's
  ownership rule exists to prevent. Retired with them: the generator pair in
  `docs_gate.py`, the `--check` step in `.github/workflows/ci.yml`, and three regression
  tests, 19 tests in total.
- One of those tests is the reason this is a retirement rather than a repair.
  `test_status_no_vcs_position.py` existed because a *generated* file had recorded a branch
  name and a commit hash, which a squash merge deletes, and because `gen_status --check`
  compared only the region above `## Repository`, so nothing examined the rest. That hazard
  belonged to the artefact rather than to the plan. `test_next_up_shows_the_item_name.py`
  went too, one pull request after it was written: the truncation it fixed was real, and
  finding it was what showed the file had not been read in long enough for three cut
  sentences to sit there unnoticed.
- The counts are gone with it. 73 done and 7 cancelled are no longer stated anywhere
  derived, and live in `CHANGELOG.md`, the archive and git. Accepted deliberately rather
  than replaced with a hand-maintained number, which would drift — the failure this
  repository's generators exist to prevent.

### Fixed
- The three agent definitions that read `PLAN.md` to tell built from not-yet-built now name
  `archive/PLAN-milestones.md` as well. This would have broken quietly: "not in `PLAN.md`"
  stopped meaning "not built" the moment completed items left it, so `researcher` would have
  begun reporting shipped modules as missing, in the one answer it exists to get right.
- `README.md` sent a new reader to `STATUS.md` first and `PLAN.md` second. It now points at
  the open plan and the archive.
- `CONTRIBUTING.md`'s "When this document splits" section said its budget was "now 210" when
  it was 249, two lines below a sentence warning that this very sentence had gone stale twice
  before. Third time. The count is removed rather than corrected, which is what the sentence
  itself already recommended and did not do.

## #74 — 2026-09-02 — docs: split the plan's backlog from what is on hold and what was dropped

### Changed
- `PLAN.md`'s single `Deferred and cancelled` section becomes three: `Open — hardening,
  testing and troubleshooting`, `Deferred`, and `Cancelled`. Of the thirteen open items filed
  under it, twelve were not deferred at all -- seven were findings from the 2026-09-02 review,
  three were documentation-accuracy gaps and two were improvements. Only the
  Anthropic-compatible adapter is on hold. One heading was serving three purposes because the
  milestone plan closing left the live work with nowhere else to go, and `PLAN.md` is this
  project's only forward-looking tracker.
- The symptom was in the generated file: `STATUS.md` read `Current phase: all planned work
  complete` two lines above `Overall: 73 done, 13 open, 7 cancelled`. `NOT_A_PHASE` excludes
  any heading starting `Deferred`, so with every milestone closed there was no phase left to
  point at. `Open` is deliberately not excluded, and the phase pointer now names it with its
  count, and `Next up` fills from it instead of saying nothing is queued.
- No change to how `gen_status.py` selects a phase, which is what made the split cheap.
  `NOT_A_PHASE` matches by prefix, so the shortened `Deferred` stays excluded; `Cancelled`
  holds only cancelled items and the selector tests the mark before the prefix, so it can
  never be chosen. Its two comments described the old arrangement and are corrected -- they
  had `Deferred` as the backlog, which is now what `Open` is.
- Items moved verbatim, and it was checked rather than eyeballed: 93 item lines before and
  after, and the sorted multiset of item and continuation lines is byte-identical. Within
  each group they are ordered by what their own annotations say they cost, so the operator
  allowlist -- the one the review left with a plausible end-to-end attack -- leads, and
  `Next up` shows the three that matter rather than the three that were typed first.
- Subgroups use `###`, which the parser cannot see: it matches `^## `, and `###` fails that
  on the third character. So `Security review, 2026-09-02`, `Documentation accuracy` and
  `Improvements` organise the section for a reader without creating phantom rows in the
  progress table.
- `PLAN.md`'s budget rises 413 to 439. It was at exactly 413 of 413, so the split breached it
  before a single item moved. Raised rather than skipped, because a budget has a legitimate
  raise path and a `Docs-Gate-Skip` trailer would have been the wrong instrument for a change
  that genuinely needs the lines. The open item asking for the trim the 2026-09-01 audit
  listed stays open: this is headroom for a restructure, not that trim, and closing it here
  would have retired an item nobody did.

- Five items added under `Open`, all of them about the surface the local model is given.
  `read_file`, `write_file` and `run_bash` are the whole registry (`tools.py` line 387), with
  no search, no globbing and no line addressing, and `delegate_readonly` fixes `allowed_tools`
  at `[]` so it has none of them -- which is why a repository-mapping task in this session went
  to a Claude subagent rather than to the free local model. Filed as: globs and a search term
  in `files[]` expanded server-side; a read-only search tool; line addressing in `read_file`;
  `edit_file`; and a measurement of whether the tool schemas sit inside ADR-0011's cached
  prefix.
- Two of them carry findings rather than just intentions. The line-addressing gap was found in
  review and never reached this file, because it was absorbed as a workaround instead --
  `.claude/agents/docs-audit-local.md` tells that one agent to cite by quotation and warns it
  is never shown a line number, which closed the symptom for one caller and left the tool gap
  open for every other. And `edit_file` is sequenced behind the inode item deliberately:
  `_one_path` returns a resolved string while each handler opens for itself, so a
  read-modify-write tool would validate once and use the path twice across two `open` calls,
  against an adversary who holds a read-write bind under `run_bash` and can retry. It converts
  a filed finding into an enabling condition, and one validate-and-open helper closes both.
- `PLAN.md`'s budget rises 439 to 477, the second raise in a day. Recorded as the signal it is:
  the header comment has said since 2026-08-28 that the answer to a recurrence is `archive/`,
  which `check_budgets` skips, rather than another raise. Every milestone is now closed, so
  that option is available for the first time and the instrument is measuring nothing until
  someone takes it.

### Fixed
- `PLAN.md`'s legend promised `✅ done, with date and commit` when it has been date-only for
  some time -- commit hashes caused more trouble than they were worth, and the fifteen items
  that still cite one are all from 2026-08-25. It now says so and points at `CHANGELOG.md`'s
  header for the reason rather than restating it, since per-PR provenance lives there. Six M4
  items carry no date at all; that was not recoverable after the fact, so the legend says
  that too instead of implying every item has one.
- `STATUS.md`'s queued lists rendered an item's first physical line rather than its name,
  so the first real queue put three sentences cut mid-clause into a generated file
  `README.md` points a new reader at: "Operator allowlist for an agent's `network` and
  `extra_binds` -- the only validation is". `parse_plan` reads one line per item while
  `PLAN.md` wraps its annotations, so `text` has only ever been as much as fitted on the
  marker line. Latent because it was unreachable: `In progress` has never had an entry and
  `Next up` reads from the current phase, which had nothing queued between M7 closing and
  this pull request. `name_of` splits on the em dash `PLAN.md` uses throughout, including
  the case where the line wraps immediately after it and there is no trailing space to
  split on. Negative-tested: reintroducing the bug fails four of the five new tests, and
  the fifth -- an item that is only a name -- is meant to pass either way.

- Two section leads described states the project has since left. M3's said the four
  context-economics items it shed "read conversation history, evictions and an action ledger,
  none of which exist until the turn loop and `tools.py` do" -- all four are done in M4 and
  both modules exist, so it asserted in the present tense something that stopped being true.
  M0a's "Run before any scaffolding" read as an instruction for four spikes that all carry a
  2026-08-25 completion date.

## #73 — 2026-09-02 — docs: record the review findings not being built yet

### Added
- The eleven findings from the 2026-09-02 review that are not being built are now deferred
  items in `PLAN.md`, each carrying the reason. Kept there rather than restated here or in
  the pull request: that document owns them, and several are security items, for which a
  surface read while it is recent is the wrong place for a list of gaps nobody has closed.
  Three entries record where the review's own recommended fix would have been the wrong
  one; two items it never raised are recorded beside them.
- Two operational facts worth a plan rather than a conversation: four of five agents in this
  repository load only in Claude Code, so `delegate_to_agent` reaches one — `#72` made that
  visible rather than caused it — and the 2026-09-01 audit's documentation trim is overdue,
  this series having needed three budget raises across two documents to state facts the
  code had just acquired.

### Changed
- `PLAN.md`'s budget rises to 413, and `STATUS.md` is regenerated: deferred goes 2 to 13.
  Worth knowing for the trim: that check *does* fire on `PLAN.md`. The audit recorded it as
  disabled and a delegated summary repeated that, but `#62` repaired the anchor exactly as
  the file's header claims — established by adding the lines and watching the gate block,
  not by reading either account.

## #72 — 2026-09-02 — feat: list_agents separates a broken agent from one in the other format

### Fixed
- Agent discovery skipped any file it could not read and said nothing — one `except
  (AgentError, OSError): continue`, in a module importing no logging at all — so "no such
  agent" and "that agent is broken" were one answer. The advice for the first case needed
  the name that the omission hid: ask for it by name and read the error.
- `AgentError` and `OSError` are caught separately. A malformed definition and an
  unreadable file are fixed differently, and the reason now says which.

### Added
- `list_agents` returns `skipped` (what needs fixing, with each file's claimed name and
  reason) and `other_format` (Claude Code's format sharing the directory — `tools` where
  this format says `allowed_tools`), beside `agents`. Absent from all three means it does
  not exist. Skipping stays non-fatal. A result-shape and description change, so a
  behaviour change rather than a wording fix.
- The `other_format` split came from running the tool, not from planning it. Against this
  repository it reported five skips, four being its own agents — `code-reviewer`,
  `docs-audit`, `researcher`, `test-writer` all carry `tools`, deliberately, as ADR-0031
  predicted and #67 worked around. Nothing regressed; they were never loadable here. But
  folding them into "broken" leaves that list permanently non-empty in this repository, and
  a list that is never empty is one nobody reads — the failure mode #69 argued against a
  pull request earlier by keeping `scan-coverage` silent about binary files. The split
  protects one property: a non-empty `skipped` always means something to fix.
- The foreign-key check runs before validation, which would otherwise refuse `tools` as an
  unknown key and report the file as broken — the conflation the category undoes. A typo in
  a file that is otherwise this format is still broken, asserted in the other direction.

### Changed
- `survey_agents` returns a named `AgentListing` rather than three parallel tuples, the
  shape a caller eventually unpacks in the wrong order. `list_agents` keeps its name and
  signature for callers that only want specs.
- `docs/AGENTS.md` rises to 327 and `docs/ARCHITECTURE.md` to 633. That is three documents
  at their ceiling across this series and the second raise to ARCHITECTURE today; the
  2026-09-01 audit's trim is overdue.

## #71 — 2026-09-02 — fix: a default written twice and a deadline measured from the wrong clock

### Fixed
- `DispatchTimedOut` reported one turn's elapsed time against the whole delegation's limit.
  A saturated cluster returned "abandoned after 1372.8s, past the DELEGATE_DISPATCH_TIMEOUT
  of 3600s", which it is not. `complete_with_retry` measured from its own entry while the
  deadline is taken once per delegation, and the turn loop enters that function fresh every
  turn. The message is the whole value of this exception — ADR-0007's server-captured truth
  is what makes it believable — and its remedy, "raise that setting", is advice the number
  did not support. Elapsed is derived from the deadline now, so there is no second origin
  to disagree with it. Found by running delegations, not by reading code.
- A `TimeoutError` where `deadline is None` blamed `DELEGATE_DISPATCH_TIMEOUT`, though only
  the adapter's own client budget bounded that attempt. It names `DELEGATE_TURN_TIMEOUT`.
- `131072` was written twice in `registry.py`, as the dataclass default and as the parser's
  fallback — two copies of a default drift, and the one that drifts is whichever the reader
  did not check. One `DEFAULT_CONTEXT_WINDOW` now.
- An omitted `context_window` was adopted silently, which `docs/MODELS.md` had already
  called "the local form of the same bug". The overflow mismatch report therefore said
  "models.toml gives context_window=131072" about a file with no such key — advice to
  correct a line that was never there, and the default is 8x below the window this cluster
  serves. `ModelEntry` records `context_window_defaulted`, and the report branches on it:
  a stated window is the operator's to correct, while an assumed one is not a claim at all,
  and the endpoint has just reported the value the remedy can now name.

### Changed
- `backend_status` rows carry `context_window_defaulted` beside the window: the same class
  of fact as `id_confirmed`, where the endpoint is healthy and the registry's account of it
  may still not be what anyone meant. A result-shape change.

### Added
- `tests/regression/test_timeout_reported_against_the_wrong_clock.py`. It records why the
  suite missed this: every existing test set `deadline = clock() + dispatch_timeout` at the
  moment of the call, so both origins coincided and agreed by construction. Nothing set a
  deadline *before* entering, which is exactly what the turn loop does.

## #70 — 2026-09-02 — fix: transcripts took the default umask and read_file took the whole file

### Fixed
- Transcripts were created at whatever the umask allowed, measured at `0o644`. Since
  ADR-0043 the stream carries the model's full reply text, and a record carries the task
  and the paths it was handed, so this was at-rest exposure left to chance. Both files are
  now created at `0o600` and both directories at `0o700`, by `os.open` with an explicit
  mode rather than a `chmod` afterwards — a `chmod` leaves a window in which the file
  already exists at the umask's permissions, and for an append-per-turn stream that window
  reopens every turn. `mkdir` applies its mode to the leaf only, so intermediate parents of
  a nested `transcript_dir` keep the default, and an existing directory is left alone
  rather than re-permissioned under the operator.
- `read_file` called `fh.read()` with no argument and applied `max_read_chars` afterwards,
  as a slice of an already-decoded string, so a file inside a workspace root was loaded
  whole on every call to return a 50k window. `max_file_read_bytes` already describes
  itself as "checked by stat() BEFORE reading" and `context.prefetch` honoured it; this
  consumer did not. It now stats first and refuses past that same ceiling — one setting,
  one meaning, both consumers, rather than a second knob for the same idea. Refused rather
  than paged: above the ceiling a file needs more calls than `max_turns` allows, so
  pagination would spend the delegation getting nowhere, and the refusal points at
  `run_bash` with `sed`, `head` or `grep` instead. The tool description is deliberately
  unchanged, so `docs/TOOLS.md` regenerates byte-identical: the refusal carries its own
  remedy, and descriptions sit in the cached prefix where each line is paid for per call.

### Changed
- `docs/ARCHITECTURE.md`'s budget rises to 630 to state the permission fact, which it
  could not before because nothing set it. Noted for whoever trims next: the budget counts
  its own reason lines, so a verbose justification inflates what it is justifying.

## #69 — 2026-09-02 — fix: the gate advertised a deleted knob and skipped files silently

### Fixed
- `.env.example` offered `DELEGATE_SANDBOX_ENABLED`, described as "an explicit choice to run
  shell commands with no confinement" — very nearly the words ADR-0034 used while deleting
  that field, on the grounds that a control switchable from outside the code is one whose
  state has to be checked to be believed. `config.py` defines 56 settings and no sandbox
  toggle, so the line was advice nobody could follow and a foot-gun already removed. To
  every other check the example file is prose, which is why nothing caught it.
- `scannable_files` skipped anything over `MAX_SCAN_BYTES` and anything `open()` refused,
  both with a bare `continue`, so `email-content` and `host-identifier` reported a clean
  pass over files they had never read — a check that cannot fail, the fifth found here.
  The skips are returned and reported now. Binary stays silent, excluding it being the
  intent rather than a gap, and a warning on every image would be read past.

### Added
- An `env-example` check: every `DELEGATE_*` name in `.env.example` must be a setting that
  exists. The allowed set is read from `docs/CONFIGURATION.md` rather than from a list in
  the gate or an import of `config.py` — this CI job installs no package, and a list here
  would be the second copy that caused the drift. That document is generated from the
  dataclass and `check_generated_docs` blocks while it is stale. It refuses to pass when
  that document yields no names at all, an empty allowed set otherwise meaning "nothing is
  known, so nothing is wrong".
- A `scan-coverage` check reporting what the content scanners left out. WARN, not BLOCK:
  the byte cap is a defensible cost decision and only its invisibility was the fault. No
  tracked file is over it today, so it stays quiet until one is.

### Changed
- Six negative tests, each direction asserted separately as that file requires. All six
  were run against the previous gate first: the three fires-on-violation cases fail there,
  while the three silent-on-clean cases pass vacuously against a check that does not exist
  — which is exactly why one direction is not a test.

## #68 — 2026-09-02 — fix: the transcript list showed a week instead of the newest twenty

### Fixed
- The list reached back seven days under a 200-row cap, so its length was set by how busy
  the week had been rather than by what fits on a screen: a busy week was unreadable, a
  quiet one nearly empty, and the cap sat far enough above a normal day that it never bound
  anything. It is now the newest 20, and the count is the only rule. Nothing is deleted — a
  stream past the twentieth stays on disk and still opens by path.
- Dropping the window cost the one cheap filter. An age could be read off the filename
  before any file was opened; ordering cannot, since it comes from the `start` event and
  needs the parsed row. The `(mtime, size)` cache bounds the work instead, which is already
  what keeps the unattended redraw from re-reading every file over `/mnt/c`.

### Changed
- Two tests were inverted rather than deleted, the behaviour they pinned now being the
  opposite: an eight-day-old stream must appear, and must still sort as old when its mtime
  says otherwise. A third pins the cap at 20 rather than at a monkeypatched value, because
  the number is the request. All four were run against the pre-change viewer and fail
  there — the pty one included, under WSL, where that half of the suite is not skipped.

## #67 — 2026-09-01 — feat: the documentation audit, in the format the local model can load

- `.claude/agents/docs-audit.md` is Claude Code's format and this server cannot load it —
  ADR-0031 names this repository's own agent files as the counter-example to portability.
  Running the audit on the local model therefore meant restating its criteria in every call.
  `docs-audit-local` is the same job in this server's format, so the criteria live in a file
  that is reviewed once instead of retyped.
- `effort: high`, which is not the obvious setting for comparison work. An A/B on the same
  four documents, same prompt, only effort differing: both runs quoted accurately, but `low`
  returned four instances of one violation while `high` returned three classes across three
  documents, including a configuration default restated in prose. Comparison is retrieval
  and `low` does it well; an audit is a search across violation types.
- `allowed_tools: [read_file, run_bash]` so it can run the gate and check its own
  quotations. It is told to match on normalised whitespace, because a passage wrapped
  across a line break is invisible to a search for the contiguous phrase — the mistake that
  turned four true quotations into a fabrication claim during the 2026-09-01 audit.
- It is told to cite by quotation and not by line number. Neither route shows it one:
  prefetch inlines raw text, and `read_file` paginates by character offset. Measured, the
  numbers it estimates run 20-30% low and drift further with depth, while the quoted text
  was exact every time.
- Two corrections carried in from that audit: `docs/TROUBLESHOOTING.md`'s contract forbids
  only a default, a schema or a value — reading it as "owns zero facts" produced seven false
  positives — and the waiver count is anchored per commit rather than grepped over the log.
- Both formats now sit in one directory, and **the two readers fail differently, both
  quietly**. This server skips what it cannot parse, which is how the delegated route looked
  unavailable to begin with. Claude Code does not skip this server's format: it loads the
  file and ignores the frontmatter it does not know, so `allowed_tools` goes unapplied and
  the named model is not the one that runs — the agent appears to work while running with a
  budget and a toolset nobody chose. The agent body says to stop if it finds itself holding
  tools it did not ask for. `CONTRIBUTING.md` records both failure modes, that the caller
  says which agent they want, and that the arrangement is temporary. Its budget rises for
  that note, with the reason in the header — which the check can read again since #62.
- Smoke-tested through `delegate_to_agent` rather than only parsed: three turns, two shell
  calls, and the server-captured ledger agreeing with the model's own account. One of those
  calls failed, which is how the example in the agent came to say `python3` — the sandbox
  has no `python` on PATH, and the first run spent a turn discovering it.

## #66 — 2026-09-01 — docs: two documents that restated what another one owns

- README's "How it works" carried five product-plane facts in the words their owners use —
  the server-side read, the agentic tool list and its zero cloud cost, the empty-root
  sandbox and default-denied network, the refusal when bubblewrap is missing, and the
  server-captured exit codes. Every copy agreed with its owner, so nothing was stale and no
  reader was misled; the cost was drift, which is the whole reason the ownership scheme
  exists. The manifest lets README name a fact and forbids restating it, so it now names
  them and links. Three lines of headroom come back with it.
- PLAN.md argued three decisions it does not own: why a condition variable rather than
  semaphores acquired in turn (ADR-0038), why keying the slots file by endpoint digest was
  rejected (ADR-0040), and why four items moved out of M3. Its contract is what is
  intended, done or dropped, plus a pointer to the decision that explains it. Each now
  points. What landed is still recorded — only the argument moved to where it is owned.

## #65 — 2026-09-01 — docs: three instructions that told an agent something false

- CLAUDE.md said the head-node rule was enforced "on all four" surfaces "by one scanner",
  and named `scan_text` as the thing to extend. There are five surfaces, and two scanners:
  `scan_text` reads the commit message and a pull request title and body, while
  `check_host_identifiers` reads files with its own matching loops. An agent following the
  instruction literally would add a rule that never runs against code, docs or tests and
  believe the opposite. The shared primitives are now named as the extension point, since
  they are the only edit that reaches both.
- CLAUDE.md gains the citation convention this audit needed and did not have. The
  `host:port` predicate matches a four- or five-digit port, so citing a line in a file long
  enough to have one is refused. The tempting fix — exempting names that end in a source
  suffix — is refused here too, and the reason is written down: `.py` is a live TLD, so the
  exemption would admit exactly the shape the predicate exists to catch. The convention is
  the cheap side of that trade, and the entry cannot show the shape it forbids.
- The docs-audit agent's waiver count was an unanchored `grep -c` over two hundred commit
  bodies. It counted prose *about* the trailer as a trailer — six reported against three
  written — and a total could not answer the question it was asked, which is per document.
  Now read one commit at a time, anchored to the start of a line, and grouped.

## #64 — 2026-09-01 — fix: the delegate tool told the model it had no shell

- `delegate`'s description said "It cannot run shell commands." `resolve_allowed(None, cfg)`
  returns everything available, which includes `run_bash` wherever bubblewrap is present, so
  a plain `delegate()` call always could. The tool descriptions are the model-facing
  contract, so this was a behaviour defect and not a wording one: a model that believes it
  has no shell does not ask for one, and the turn it would have spent proving a change works
  is spent describing it instead.
- The description now says what the shell is and what it costs to use — a sandbox holding
  nothing of the caller's and no network unless asked, `workdir` to bind something real to
  build in, `allowed_tools` to take it away again, and absent entirely on a host without
  bubblewrap. `delegate_readonly` is unaffected; it fixes an empty tool set by construction.
- `docs/ARCHITECTURE.md` was right about the code and is unchanged on that point. Its
  transcript section was not: it said nothing happens between `start` and `end` on the
  one-shot path, which stopped being true when #54 began writing a synthetic turn so the
  record could not be mistaken for the empty shape a failed delegation leaves.

## #63 — 2026-09-01 — fix: the turn limit was reported as unreached in the case it reports

- The last turn is declared with no tools so the model cannot end on a call nobody will
  run. `hit_turn_limit` also required a tool call on that reply — which a backend offered
  no tools does not make. It was false whenever the backend behaved, and true only when one
  ignored the withdrawal, so a delegation truncated by its budget reported that it was not
  and a caller had nothing to tell a partial answer from a complete one.
- Three documents stated the intended meaning and all three were wrong about the code, one
  a troubleshooting entry indexing a string a reader would never see.
- The suite could not have caught it. Every scripted backend replied from a list without
  reading the request, so none could honour the withdrawal the bug depended on; the test
  covering the flag scripted tool calls on both turns, including the tool-less one, and
  asserted true — exercising only the path where a backend misbehaves.
- A second test was misnamed rather than wrong: "an answer given freely" answered on the
  final turn, under a withdrawn toolset, which is the opposite of freely. Its scenario now
  answers before the last turn, which is what its name always claimed.
- Now `turn == turns` and nothing else. A delegation that would have finished on its last
  turn anyway reports the limit too; that costs a reader one look at `max_turns`, where the
  old reading cost them a truncated answer read as a whole one.
- `tests/regression/test_turn_limit_unreported_to_a_compliant_backend.py` supplies the
  backend the suite lacked — one that answers when offered no tools — and keeps the
  ignored-withdrawal case so the fix cannot quietly drop what the old flag did catch.
  Verified to fail against the unfixed flag first.
- `docs/DISPATCH.md`'s budget rises to fit the corrected description. Its header stays on
  one line deliberately: a reasoned multi-line header is unreadable to the check until #61
  lands, and this branch must not depend on merge order to stay enforced.

## #62 — 2026-09-01 — fix: a budget header that recorded its reason stopped being enforced

- `check_budgets` searched for the number followed by the comment terminator with only
  whitespace between them. #23 introduced the convention of recording each raise's reason
  *inside* the same comment, which puts prose between the two, so the search failed and
  `if not m: continue` skipped the document without a word. The commit that made budget
  changes auditable is the commit that stopped them being enforced.
- Silent in every direction available to a reader. The `SKIP` branch fires only when *no*
  document declares a budget, so five losing theirs produced no signal; the gate reported
  PASS throughout; and a reasoned header looks more careful than a bare one, not less.
- Five documents, not the three the 2026-09-01 audit was able to see. It tested only the
  total-budget pattern, and `BUDGET-PER-ENTRY` carried the same anchor: `CHANGELOG.md`'s
  longest entry had reached 131 lines against a cap of 95, and `docs/AGENTS.md` was over a
  cap the audit recorded as absent. Both instruments, one anchor.
- Found by the audit only because the negative test was run: closing a header by hand made
  the check fire at once, which distinguished a disabled check from a satisfied one.
- Fixed by dropping the closing anchor from both patterns. `BUDGET:` still cannot match
  `BUDGET-PER-ENTRY:` — the colon holds them apart — and that is asserted rather than
  assumed, because a relaxed pattern reading the per-entry header as a total cap would
  trade a silent instrument for a loud and wrong one.
- `tests/regression/test_budget_header_carrying_its_reason.py` covers both instruments in
  both directions, and was verified to fail against the unfixed gate before being kept.
- The five budgets are raised to the sizes they had already reached, with the reason in the
  header. That records where the documents are and re-arms the check today; it is not an
  endorsement of the sizes, and the audit's findings track the trim. `CONTRIBUTING.md` also
  had a header whose prose said it was raised to 220 while the number still read 210 — a
  typo that only survived because nothing was reading the number.


## #60 — 2026-09-01 — docs: file the loop's missing heartbeat where a plan can be read

### Added
- `PLAN.md` records the one hole this week's measurements found and did not close: the
  agentic loop reports at the top of each turn, so a single turn is silent for its whole
  duration, bounded only by `turn_timeout` — which defaults to exactly the client's idle
  timeout. It was written down in `#59`'s description and nowhere a plan can be read from,
  which is the same mistake that raised this file's own budget once before.
- The entry names the fix and the trap in it. Lowering the default would kill legitimate
  work — one call has been measured generating for 1645s — so the answer is to give the
  loop the one-shot's heartbeat, not a smaller budget. `#59`'s guard bounds an interval and
  this path has no heartbeat to bound, so it cannot reach it.

## #59 — 2026-09-01 — fix: refuse a keepalive that cannot hold the client's idle timer off

### Fixed
- `DELEGATE_KEEPALIVE_INTERVAL` above half the client's stdio idle timeout is now refused at
  startup, beside the existing `turn_timeout`/`dispatch_timeout` rule. A one-shot sends
  nothing but this heartbeat, and `#57` measured what happens when it does not arrive: the
  caller abandons the call, **nothing reaches the server**, and the dispatch runs on holding
  its admission slot until the work ends on its own. The server cannot detect that, so the
  interval is the only thing standing between a long delegation and the lockout — which
  makes it a correctness setting and startup the only place it can be checked.
- Half rather than all of the timeout, so a beat lands twice inside every window and a late
  one is still early. Both directions are tested: the largest legal value is accepted, since
  a check that also refused it would be one nobody could satisfy by reading its error.

### Added
- `CLIENT_STDIO_IDLE_TIMEOUT`, a constant rather than a setting. It is a property of the
  client, not of this server, and it was measured rather than read off a document.

## #58 — 2026-09-01 — docs: the stdio idle timeout, measured, and what the server never learns

### Added
- `tests/regression/test_a_cancelled_batch_holds_its_slots.py`. The incident behind `#45`
  held two slots for 3616 seconds, and the abort measured clean on 2026-08-31 was a single
  one-shot — the incident's shape was a batch, whose items run concurrently under
  `asyncio.gather`, and nothing tested whether a cancellation reaches them. It does. Three
  things make it work — `gather` is unshielded, the item wrapper catches only `ToolError`,
  and `Admission.admit` releases in a `finally` — and shielding the items would look like a
  fix for interleaved failures while silently restoring the lockout.

### Changed
- `docs/DISPATCH.md` no longer says a client abandoning a silent delegation is unreproduced.
  It was reproduced on 2026-09-01 with a slow loopback stub standing in for the model, so
  the wall clock was exact rather than a guess at how long a real model would choose to
  talk — two earlier attempts missed at 1645s and 744s, the second because the model simply
  finished the job. At 1800s the client aborts and **nothing reaches the server**: not a
  cancellation, not an EOF. It held both admission slots for 304 seconds more until the work
  finished on its own, then carried on serving the same session. So `keepalive_interval` is
  a correctness setting rather than a convenience — the server cannot discover that nobody
  is listening, and sending something is the only guard. Two journal entries carry the
  detail, along with a third on a `.wslconfig` that answers a recurring `0xc0000142` on
  unrelated Windows programs while WSL exhausts the commit limit.
- `DELEGATE_TURN_TIMEOUT` defaults to 1800s, which is the quantity under test, so a one-shot
  would be cut at the same instant the client gives up. Recorded in the journal because it
  would have sunk a sixth attempt as quietly as it nearly sank the fifth.

### Fixed
- The first version of the cancellation test passed against a cancellation that never
  reached the server. Cancelling the *client's* task looks like an abort but sends nothing:
  the MCP SDK emits `notifications/cancelled` only from an explicit `Client.cancel`, never
  on task cancellation and never on a read timeout — measured, both slots still held six
  seconds later, the items cancelled only during event-loop shutdown, after the assertions
  had been satisfied. The test now sends a real cancelled notification and snapshots every
  fact inside the running loop. Every check in both of these sections was negative-tested
  against the bug it names, per `CLAUDE.md`.
- A negative test invalidated by its own bytecode cache, recorded in `JOURNAL.md`. The
  harness that proves a check can fail edits a file in place and reverts it; one mutation
  came to exactly the same byte count as the line it replaced and the revert landed inside
  the same mtime tick, so Python validated the mutated `.pyc` against the restored source.
  A test that had passed twenty minutes earlier then failed against a file that was correct
  on disk. The verdicts were as suspect as the failure, so the whole negative-test result
  was taken again with a fresh `PYTHONPYCACHEPREFIX` per run. It is the `(mtime, size)`
  trap of `JOURNAL 2026-08-25` in a new place: anything that mutates a file and restores it
  belongs in that category, not only generators comparing an artefact against its source.

## #56 — 2026-09-01 — feat: a transcript says which call it came from and what it was handed

### Added
- Both halves of a transcript — the stream and the record — now carry `tool`, the tool the
  caller actually invoked, and `tools`, the set that call resolved to. Two fields because
  neither answers the other's question: `delegate_readonly` is `delegate` with the tool set
  fixed empty, so the two run an identical path and the shape cannot say which was called,
  while `delegate` alone does not say whether a loop ran. Before this, a read-only call, a
  `delegate_batch` item and a plain `delegate` wrote byte-identical transcripts, so a
  directory of them could not be counted by kind — which is what the records exist for.
- The stream's `start` event also carries the prefetch accounting: every file read with its
  cost, and every file skipped with its reason. The record has held this since M4, and the
  record is written when the work is over; a reader asking what a delegation is chewing on
  is asking while it runs. Paths and cost only, never text (ADR-0039).
- The viewer names the kind of each call in the listing — `delegate`, `readonly`, `agent`,
  `batch`, or `one-shot` for a `delegate` handed no tools — and, on opening one, prints the
  resolved tools and every file it was given. A skipped file is the one line in that block
  rendered loud rather than dim: a file the caller believes it passed and the model never
  saw is worth interrupting a reader for.

### Changed
- A stream's `tool` was derived from whether an agent was bound, so `delegate(agent_name=…)`
  reported itself as `delegate_to_agent`. Each of the four tools now passes its own name and
  the agent is recorded separately, which it always was.
- Absent and empty are kept apart everywhere downstream. A missing `tools` is a transcript
  written before the field existed and reads `?` in the listing; an empty one is a one-shot.
  Defaulting an old row to `delegate` would have reproduced exactly the confusion the column
  ends, since every call once wrote `delegate` whether or not it was one.

## #52 — 2026-09-01 — chore: the test suite runs in parallel by default

### Changed
- `-n auto` moved into `addopts`. Parallelism was deliberately opt-in, on the reasoning
  that a plain `pytest` should read normally and the speed was there when wanted. It then
  went unwanted for a whole session — 488s serial against 183s with workers, the same
  suite, run the slow way four times — because remembering a flag is not a default.
  Anything that depends on being remembered is not a rule.
- The trade the old decision was protecting is real and is now inverted rather than
  denied: one small file costs about 7s of worker startup where it used to be instant.
  `-n 0` restores the serial, readable run and is the documented way to work on a single
  test. The default now favours the case where getting it wrong is expensive over the
  case where it is cheap.
- CI picks this up with no change to its command, so it stops paying the serial cost too.

### Fixed
- `test_a_batch_never_exceeds_the_endpoints_declared_concurrency` inferred overlap from a
  0.02s sleep, which is a race the scheduler usually wins and, on CI with workers
  competing for two cores, stopped winning: the second item was not scheduled inside the
  window, so the test read `peak == 1` and failed on both Python versions. It now holds
  each item on an `Event` until a second has arrived, making the overlap a fact rather
  than a probability. Bounded, so a gate that genuinely serialises still fails the
  assertion instead of hanging. Exactly the latent flakiness the pyproject note said
  parallelism exists to surface — found by turning it on.
- Three tests shared the machine's real `slots_dir`, which is the deeper cause and the one
  that actually broke CI. Admission slots are counted machine-wide on purpose, so once the
  suite runs in parallel the other pytest workers are competing for them: a batch that
  should overlap two items saw a peak of one, and a delegation made to wait emitted an
  extra `progress(0, 0)` from `ticked` that an exact-sequence assertion counted. Each now
  runs in a directory of its own, the way the `backend_status` test already did — that
  test's docstring had explained the hazard, and three others had not taken it.

## #54 — 2026-09-01 — feat: a one-shot delegation says it is still running

### Added
- A one-shot now reports itself on a timer, to the client as a progress notification and
  to the transcript as an `alive` event carrying elapsed and the deadline elapsed is
  measured against. ADR-0018 hangs its notification on a turn, and a one-shot is a single
  backend call with no turns — so it is silent for its whole duration. Measured: one ran
  1645s with nothing on the wire and nothing in its transcript between `start` and its
  answer, which is also 27 minutes of a healthy delegation that the viewer would have
  shown as `quiet`. `run_one_shot`'s own docstring had named this gap since M4.
- **Not** measured, and the wording here was corrected to stop claiming otherwise: whether
  a client actually abandons such a call. A shortened `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`
  had no observable effect, and the run finished 155s short of the 1800s default, so the
  timeout was never reached in either direction. What justifies this feature is the
  silence, which is measured, plus the 3616s incident behind PR #45 — not a reproduction.
- `DELEGATE_KEEPALIVE_INTERVAL`, default 60s. Not a deadline: nothing is cancelled when it
  passes.
- The `alive` event deliberately does not describe what the model is doing. There is no
  streaming, so the server does not know, and a guess would be worse than the two numbers
  it actually has. Recorded in `PLAN.md` against the cancelled streaming item: that
  decision weighed only what the *caller* sees, and the transcript stream — a second
  consumer, read while the delegation runs — was decided six days later.

### Changed
- `dispatch_delegation` was already given `report_progress` and dropped it on the one-shot
  branch. It now forwards `on_alive` instead, which is a different shape on purpose: the
  loop reports a turn number out of a turn budget, and a one-shot has neither.
- One heartbeat serves the wire and the transcript, because a stream silent for forty
  minutes and a wire silent for forty minutes are the same problem from two sides — and
  the first is how the viewer came to call an abandoned delegation live.

### Fixed
- The heartbeat is the only concurrency in `loop.py`, and everything from `run_one_shot`
  to the backend call is one sequential chain of awaits, so it runs beside that chain and
  is torn down with it in a `finally`. A callback that raises stops the heartbeat and
  nothing else: it exists to stop a long delegation being abandoned, and one that killed a
  delegation instead — over a notification that could not be delivered, which is not even
  evidence the client has gone — would be strictly worse than none.
- Three of the six tests written for this passed against deliberately broken code before
  being rewritten. Counting notifications through the MCP wire cannot see a leaked
  heartbeat, because a stray `report_progress` after the request finishes goes nowhere; a
  progress handler that raises on the *client* side never reaches the server's callback at
  all; and the third asserted nothing whatsoever. All three now drive `run_one_shot`
  directly and were confirmed to fire against the behaviour they replace.

## #51 — 2026-09-01 — fix: the gate blocked a correct amend without saying why

### Fixed
- `git commit --amend -m` was blocked by `owning-doc` even when the commit it produced did
  update its owning document, and the block said nothing about amends — so the escape
  hatch got spent on it. One `Docs-Gate-Skip` on `docs/ARCHITECTURE.md` is exactly that.
  The gate now names the cause and the remedy whenever `owning-doc` blocks, at commit-msg
  and at a hand-run pre-commit: `git commit --amend` with the editor is reported to the
  hook as an amend *and* lets the message change, which is the whole of what `--amend -m`
  was wanted for.
- The header counted the staged set and called it "changed file(s)", so an amend — which
  stages only its increment — reported "1 changed file(s)" for a commit holding six. It
  now names what it compared against, which is what made the number look like a bug.

### Changed
- No attempt is made to detect the undetectable case, and the measurements saying so are
  recorded rather than left for someone to redo. Git reports `--amend --no-edit`,
  `--amend -C HEAD` and an editor amend as source `commit`/`HEAD`; it reports
  `--amend -m` and `--amend -F` exactly as it reports an ordinary `-m`. `GIT_REFLOG_ACTION`
  is unset in every one of them. Reading the parent's argv from `/proc` does work under
  WSL and would close the gap there, but not where these hooks actually run: Git for
  Windows hands the hook a PPID of 1 with no readable entry. Measured on git 2.43.0.

### Added
- `tests/regression/test_a_real_amend_reaches_the_gate.py`. The existing amend test plants
  `.git/docs-gate-reused-message` itself, which proves the gate does the right thing given
  the marker and nothing at all about whether a real amend produces one — and that was the
  half that broke. This one installs the hooks and runs real commits through them, pinning
  each amend form to the reading it gets. Every assertion was negative-tested against the
  behaviour it replaces.

## #50 — 2026-09-01 — fix: three documents disagreed about whether the sandbox works

### Fixed
- `README.md` claimed *"the sandbox and the agent roster do not [work end to end]"*.
  `docs/AGENTS.md` said the opposite two files away, and the prose in the README itself
  described `run_bash` being confined by bubblewrap. The banner is gone rather than
  corrected: status belongs in `STATUS.md`, which is generated from `PLAN.md`, and a
  hand-written second copy of it on the first page a reader sees is a copy that goes
  stale — which is exactly what happened. The pointer to `STATUS.md` and `PLAN.md` that
  followed it stays.
- `docs/TROUBLESHOOTING.md` opened its sandbox section with *"`sandbox.py` is built;
  nothing calls it yet. `run_bash` still refuses every call until the secret denylist is
  enforced at the mount level"*. That has been false since ADR-0035, and it was a fact
  restated in the one document that owns no facts — so it was wrong twice over, and the
  second kind is the reason the rule exists. The symptom entries under it are unchanged;
  they were always right.
- The same document told readers that entries marked *(not built)* describe symptoms they
  cannot hit yet, when no entry carries that marker any more. Reworded to say everything
  below is reachable, keeping the convention documented for the next subsystem rather than
  implying it is in use.

## #49 — 2026-09-01 — fix: the delegation viewer is a session, not a single transcript

### Changed
- `q` now leaves a transcript and returns to the list instead of ending the process. The
  viewer was built to watch one dispatch: choosing a transcript was a one-way door, and
  watching a second meant re-running the command and losing the list. One run now watches
  a whole session, and only the list exits.
- Reaching the `end` event no longer parks in `while True: sleep(3600)` waiting for
  Ctrl-C. It stays on the finished transcript and takes a keypress — deliberately not an
  automatic return, because the last thing a dispatch writes is usually the thing that was
  being waited for, and taking the screen away at that moment is the one behaviour a
  watcher must not have.
- The list is ordered by when each dispatch **started**, not by mtime. mtime moves every
  time a turn lands, so a long-running older dispatch climbed back to the top on every
  turn and reshuffled the list under someone reading it. The start clock comes from the
  `start` event's `at`, falling back to the timestamp `transcript.py` already puts in the
  filename. Neither `st_ctime` nor `st_birthtime` was an option: on Linux the first is the
  inode-change time, and the second is not there at all.
- The list is capped at the last seven days rather than the newest twenty-five, and shows
  the last-write clock beside the start clock — which is the end time once a dispatch has
  finished. Both are rendered in local time; `at` is UTC and an mtime is not, so the two
  columns would otherwise have sat an hour or two apart while describing one dispatch.
- The list redraws itself every two seconds, and `r` forces it. A watcher that only
  learned about a new dispatch when you pressed a key was the wrong shape for the thing
  it watches.
- Terminal input moved from `raw` to `cbreak`, held for the whole run rather than set and
  restored per keypress. `raw` also clears `OPOST`, which is survivable while only the
  picker reads keys but stair-steps every line down the screen once the follow view does.
  `cbreak` also leaves `ISIG` alone, so Ctrl-C stays a signal and still quits from a
  blocking read.
- The screen is cleared with `ESC[H ESC[0J` rather than `ESC[2J`, and never `ESC[3J`. The
  follow view prints and never repaints, so the terminal's own scrollback is how a
  transcript is read back — including after returning to the list — and the wrong clear
  sequence throws exactly that away.

### Fixed
- A stream with no `end` event was shown as `live`, which is a claim the file cannot
  support. Found by measurement: closing the editor mid-dispatch takes the whole process
  tree with it, including the server, so the stream simply stops — and the viewer went on
  calling it live long after the cluster had finished with it. There are now three states.
  `ok`/`fail` for a stream that ended, `live` for one written to within `STALL_SECONDS`,
  and `quiet <age>` for one that has neither.
- It deliberately does not try to say "dead". A writer pid in the stream would be
  meaningful only on the machine that wrote it, and a transcript directory is routinely
  synchronised; and a one-shot delegation is legitimately silent between its start and its
  end, so silence alone condemns nothing. The state reports what is known and names the
  age, which is the part a reader can actually judge.
- The state column padded a string that already contained colour escapes, so the width
  counted the escape bytes and the column did not line up. The plain word is padded now
  and coloured after.
- An arrow key intermittently quit the viewer instead of moving the highlight. Keys were
  read through `sys.stdin`, which fills its own buffer from the descriptor and hands back
  one character: the three bytes of an arrow key arrived together, `[A` stayed in that
  buffer, and the `select` used to tell an arrow from a bare Escape — which can only see
  the descriptor — reported nothing waiting. The escape was therefore read as Escape, and
  Escape quits. Keys now come from `os.read` on the descriptor, so the thing being polled
  and the thing being read are the same thing. Found by driving the viewer through a real
  pty; no test that stubs stdin can see it, and it survived a hand-run because it depends
  on how the terminal happens to batch the bytes.
- A line still being appended when the viewer read it was parsed as JSON, failed, and was
  discarded — and its remainder then arrived as a second unparseable fragment, so a whole
  event vanished from the view rather than arriving late. The reader now rewinds unless it
  has a complete line.
- Following a finished transcript re-read the entire file every 300ms to ask whether it
  had ended, having already rendered the `end` event it was looking for.

### Added
- `tests/test_watch_delegations.py`, in two halves. One calls the file-reading functions
  directly — the window, the ordering, the cache. The other spawns the viewer on a pty and
  types at it, because the seam between a terminal and the code is where the failures
  above lived and nothing that stubs stdin reaches it; that half skips on Windows and runs
  under WSL and in CI, which is both places the viewer is used. Every check was
  negative-tested by restoring the behaviour it replaces and confirming it fires. The
  local-time assertion skips rather than passes on a machine set to UTC, where it could
  not tell a converted clock from a raw one.
- The POSIX-only imports are guarded so the module imports on Windows, where the suite
  also runs; `main` refuses there with a message naming WSL rather than an `ImportError`.

## #48 — 2026-08-31 — feat: watch a delegation while it runs, not only after

### Added
- A dispatch now appends a JSON line per event as it happens — `start`, one `turn` per
  completed turn, `end` — beside the record it already wrote at the finish. A record that
  appears only once the work is over cannot answer the question worth asking during it,
  and the two files are independent because the stream has to survive a dispatch that
  never reaches an end. (ADR-0043)
- The stream carries the model's reply text, which the finished record does not. That
  extends ADR-0039 rather than reversing it: that decision excluded file *bodies* as bulky
  and recoverable from the repository by path, and a reply is neither — it is small and
  exists nowhere else, the same argument ADR-0039 used to write the task verbatim. What is
  newly exposed is named in ADR-0043 rather than left implicit: a model may quote a file,
  so fragments can reach the stream, and a transcript directory that is synchronised or
  backed up is one whose contents leave the machine.
- `scripts/watch_delegations.py`: lists what is in the transcript directory, follows the
  one you choose with arrow keys, and renders turns as a conversation with timestamps and
  colour rather than as JSON. Standard library only, no lock, writes nothing — so a second
  terminal can watch a second delegation.
- The viewer resolves the transcript directory the way the server does: the environment
  first, then `<repo>/.env`. The server reads that file itself, so a directory configured
  there is set for the server and for nothing else -- not for a shell. Requiring the
  variable to be exported by hand would have made the viewer a second place to configure
  the path, and the second place is the one that goes stale. Found by running it.
- Fenced code in a reply is rendered verbatim rather than re-wrapped on whitespace. The
  first live delegation through the viewer asked the model to quote a function and the
  viewer flattened it into prose, which is the one thing a reader of quoted code cannot
  use.
- `on_turn_done` in the agentic loop, called from both places a turn can end. A single call
  site would have to pick one, and the turn ending without tool calls is the one carrying
  the final answer.

### Changed
- Tokens per second is measured over the backend call, not the turn. The turn's wall clock
  includes tool execution and any wait for a slot, so a rate taken from it reports the
  cluster as slower than it is — and a throughput figure quietly measuring the wrong
  interval is worse than none, because it gets believed. Both intervals are written so the
  gap between them stays visible.
- A one-shot delegation, which runs no turns, emits a synthetic one carrying its answer.
  Without it the common read-only case streamed a start and an end with nothing between —
  the exact shape of a delegation that produced nothing, and indistinguishable from one.

### Fixed
- `test_nothing_is_streamed_when_no_transcript_directory_is_set` was written so it could
  not fail: with the setting empty there is no configured directory, so asserting that
  nothing appeared in a temporary one checked somewhere the stream was never going to be
  written, and it passed with the switch removed. It now asserts the switch directly, both
  ways. Found by negative-testing the five new tests rather than by reading them, which is
  the fifth time that has been the difference here.

## #47 — 2026-08-31 — feat: read-only tools declare themselves to the client

### Added
- `backend_status` and `list_agents` carry the MCP `readOnlyHint` and `idempotentHint`
  annotations. Claude Code's plan mode gates MCP calls regardless of an allow-list, because
  a permission rule matches on tool name and cannot know whether a given call writes. With
  the annotation it does know, and stops asking. Verified by measurement in both directions:
  the same call in the same mode prompts without the annotation and does not with it.
- `delegate_readonly`: `delegate` with `allowed_tools` fixed at `[]` rather than accepted
  as an argument, declared `readOnlyHint` truthfully. One turn, `files[]` and nothing else,
  no `write_file` and no `run_bash`. It exists because the annotation is per tool and the
  permission layer never inspects arguments, so a read-only *call* cannot be expressed --
  only a read-only tool. Sixth tool on the model-facing contract, paid because the
  alternative was annotating something untrue.
- ADR-0042, the argument ADR-0005 asked for before a sixth tool could arrive, and the
  reason the count moved rather than the rule. `test_exactly_five_tools_are_declared`
  became `test_exactly_six_tools_are_declared`; ADR-0005's heading records the
  supersession and its body is untouched. The gap being closed is that approving a
  delegation in plan mode approves the *call*, not its contents -- the server is never
  told which mode the client is in, and a plain `delegate` approved during planning
  writes to the repository. Demonstrated by delegating a write and finding the file,
  before the ADR was written.

- Two tests for it, both shown failing against deliberate breakage first: that it declares
  no tools on the wire, checked in the dispatched request rather than in the resolved set;
  and that its schema exposes no `allowed_tools`, since a parameter that could widen it
  would make the annotation falsifiable by the caller.

- A guard that the three delegate tools never carry that annotation. They hand the local
  model `write_file` and `run_bash` whenever `allowed_tools` is unset, so a read-only claim
  would be false in the way hardest to notice -- the client stops asking, the write still
  happens, and nothing reports the contradiction. The guard was shown failing against a
  deliberately mis-annotated `delegate` before being kept.

## #46 — 2026-08-31 — feat: an ADR heading may name more than one successor

### Changed
- The ADR status grammar accepts a list: `Superseded by ADR-0002 and ADR-0003`, and the
  same for `Partially superseded by`. One decision can be overtaken by several later ones
  on different clauses -- ADR-0005 lost its portability claim to ADR-0031 and its tool
  count to ADR-0042 -- and a heading admitting one reference could name only the newest,
  leaving the earlier correction to survive in whatever prose happened to mention it.
  Widened, not opened: a status the gate cannot parse still blocks, and a test says so.

### Fixed
- The dangling-reference check validated only the first ADR named in a heading. It used
  `re.search`, which stops at one match, so widening the grammar without widening this
  would have left every later target unvalidated -- first reference real, the rest
  anything at all, gate silent. Found while making the grammar change rather than after,
  and it would have been a fifth check that runs, reports success and cannot fail.
- `check_adr_format` takes its text as an argument instead of only ever reading
  `DECISIONS.md`, and `tests/regression/test_gate_self_defeating_checks.py` now drives
  that function rather than a reimplementation of it. The copy existed for hermeticity —
  the real function could only be exercised by mutating a tracked file — and the price was
  that those tests passed whether or not the rule they described still worked. In the file
  named after checks that cannot fail, that was one.


## #45 — 2026-08-31 — fix: delegate_batch reports progress every turn, not only when an item lands

### Fixed
- `delegate_batch` reported progress only when an item finished, so a batch whose items
  each ran longer than the client's 1800s stdio idle timeout sent nothing at all across
  the turns that took the time. The client aborted; the server does not learn that the
  caller has gone, so it carried on to `dispatch_timeout` and held
  `max_inflight_large_prefills` at its ceiling of 2 for the remainder. Measured on a real
  two-item batch: 3616 seconds from admission to release, of which the last half hour was
  spent generating an answer nobody was listening for, while every other session on the
  machine was locked out of large delegations.
- The cause was a deliberate choice with one half right. `ctx=None` was passed into each
  item because interleaved turn counts from items running at once "would describe nothing
  a reader could act on" — true, and irrelevant to what the notification is for.
  `run_delegation`'s own docstring already said resetting the client's idle timer "is the
  whole of its job", so withholding the notification to protect the number it carries
  traded the mechanism for the display.
- `run_delegation` now takes an `on_turn` hook that displaces the turn numbers without
  displacing the notification. `delegate_batch` passes one that reports its own
  completed-item count, so the client is notified on every turn of every item and never
  sees a turn number. The constraint the original `ctx=None` protected is kept.

### Changed
- `test_backend_status_says_whether_the_budget_is_machine_wide` now runs against an
  isolated `slots_dir`. At the default it read the machine's live counters and asserted
  that nothing anywhere on the box was delegating, so it failed whenever another session
  was — describing the machine rather than the code. It was found failing exactly that
  way, mid-session, by the batch this entry is about. What it tests is that the
  cross-process block is reported and internally consistent, and that holds in a
  directory of its own; `active` is asserted true there, so the gauge checks still run
  rather than passing through the inactive branch.

### Added
- `tests/regression/test_a_batch_delegation_loses_its_keepalive.py`, named after the bug.
  Shown failing against the unfixed code before being kept — two notifications for two
  items, both arriving after the turns that would have timed out. Its handler answers
  from the conversation rather than a scripted list, because two concurrent items popping
  one shared sequence make the count under test a function of interleaving. A second case
  guards the constraint rather than the bug: every number the client sees is a
  completed-item count, never a turn.

## #44 — 2026-08-30 — fix: run_bash was refused on every project carrying a virtualenv

### Fixed
- The mount-level secret scan walks the workdir before every `run_bash` call and refuses
  once it passes `secret_shadow_max_entries`. That refusal is correct — a partial denylist
  is indistinguishable from a complete one, so it fails closed — but the default was
  measured on a checkout of this repository that had no virtualenv in it, and `config.py`
  said so in its own help text: "This repository scans in 230." With `.venv` present it
  walks 10,586 entries, so every shell command a delegated model tried was refused.
- Found by running the tool rather than the suite. Every existing scan test builds a tree
  of one to twelve files by hand, and the two that exercise the budget do it by *lowering*
  the cap to five; none ever asked whether a realistic tree passes the shipped default,
  which is the only question a user's first `run_bash` actually asks.

### Added
- `security/opaque_globs.txt` and `opaque_globs_file`: machine-generated directories that
  are covered with the same tmpfs a matched secret directory gets, and pruned from the walk
  for the same reason. Measured on this repository, on /mnt/c: 10,586 entries in 66s
  walked, against 248 in 0.7s covered. The scan runs per call, so that 66s was the whole
  per-command cost, not a slow start.
- **Covering is what makes skipping safe.** A secret inside such a directory is hidden by
  the mount over its parent whether or not the walk ever looked. Pruning *without* covering
  is a hole, and it is the tempting mistake — "skip whatever gitignore ignores" would drop
  `.env`, which is gitignored, from the scan that exists to find it. Both wrong builds were
  constructed and shown failing; prune-without-cover is caught from inside a real sandbox by
  a shell reading the file, since no argv assertion can catch a path the walk never visited.
- A missing opaque list warns rather than refusing, unlike the denylist, whose absence must
  stop the server. Two files rather than one so that a slow scan is never fixable by editing
  the security list (ADR-0041).
- `tests/regression/test_a_project_with_a_virtualenv_is_scannable.py`: scans a
  project-shaped tree with the shipped lists and a budget just above what the project is
  worth without its virtualenv, so it passes only while the shipped file still names one.
  Deleting that line fails three tests.

### Changed
- `secret_shadow_max_entries`' description carries the measured numbers and says that
  raising it is almost never the right answer. Raising it is worse than slow: inside a
  virtualenv `*secret*` and `*credential*` match ordinary library filenames —
  `keyring/credentials.py`, `certifi/cacert.pem` — and the scan mounts `/dev/null` over
  each, so a raised cap spends a minute and then breaks the imports and TLS roots of the
  environment it just read. Twenty of twenty-two matches here were of that kind.
- The refusal message now names the opaque list as the lever, instead of only the cap.
- `node_modules` is on the default list, from measurement rather than principle: a
  32,184-entry project here is refused on every call without it and scans in 3s with it,
  while raising the cap instead means a ~3 minute walk before every command. `npm test`
  inside the sandbox is therefore not something the entry costs -- it is unreachable at
  any setting. The tmpfs covering a directory is writable, so a command that writes into
  one appears to succeed and leaves nothing behind.

## #43 — 2026-08-30 — feat: admission counts every server process, not just this one

### Added
- `slots.py`: the four admission rules now count every server process on this machine,
  not just this one. The symptom was invisible from inside a session and ordinary to
  reach: the transport is stdio, so the MCP client starts one server process per
  registration, and two editor windows open on two projects are two `Admission` objects
  with independently zeroed counters against one KV pool. Every rule bounded a session,
  so the cluster saw `kv_token_budget`, `max_inflight_seqs` and
  `max_inflight_large_prefills` multiplied by the number of windows open. The cause is
  visible in the module's own docstring, which called itself the global budget "by
  construction" -- true within a process, and that was exactly the scope that did not
  matter. The fix is one `flock`ed file of per-process records on tmpfs, which
  `admission.py` reads instead of its own attributes; the policy is untouched.
- The predicate is evaluated **inside** the lock. Reading the totals, testing the four
  rules and publishing the result are one critical section, because returning the totals
  and deciding afterwards is a time-of-check race in which two processes both see room
  and both take it -- and it widens exactly when the cluster is saturated. `admit()`
  therefore takes the predicate as a callable rather than returning numbers.
- Records are keyed by `(pid, start_time)` and reclaimed by liveness, so a `kill -9`d
  editor window leaks nothing, with no heartbeat to miss and no timeout to wait out, and
  a recycled PID cannot inherit a dead process's slots. The staleness timeout beside it
  is a backstop for platforms with no `/proc`, never the primary mechanism.
- `cross_process_slots` and `slots_dir` in `config.py`. Keying the file by a digest of
  the endpoint was designed and rejected: it shares correctly until one project's
  registry drifts, at which point the digest changes and two installations *silently stop
  sharing* -- this same bug again, with no symptom. Separation is explicit instead.

### Changed
- `backend_status` gains an `admission.cross_process` block: whether the shared budget is
  actually active, the machine-wide totals, and how many processes hold slots. It reports
  what it can observe rather than what was configured, because those differ exactly when
  something is wrong. A tool-description change, so a behaviour change (ADR-0040).
- `Admission` takes an optional `SharedSlots`. Without one it behaves exactly as before
  along the same code path rather than a parallel one -- which is what Windows gets,
  where the suite runs but the server never does, and it says so rather than appearing
  healthy.
- Waiting is now also polling, since another process's release cannot notify this one's
  condition. A local release still wakes waiters immediately and does not wait the poll
  out.

## #42 — 2026-08-30 — feat: enforce the admission budget, and write an operator transcript

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
- `transcript.py`: one operator record per dispatch, written when `transcript_dir` is set
  and not at all when it is not. ADR-0024 adopted this in M4 and nothing was built; there
  was no disk-writing telemetry anywhere in `src/` before it.
- The transcript is independent of the caller's `diagnostics` argument, which is the
  requirement rather than a detail: what an operator can audit should not depend on what
  the calling session thought to ask for. That has a cost the design pays instead of
  assuming -- `_Watch` keeps per-turn records only when told to, so a configured
  transcript asks for them itself, and the caller's flag still decides separately what
  the reply carries. Both directions are tested.
- Records carry **real** token usage as the backend reported it, not the estimate
  admission sized the request with. Summing usage across records is the only way to say
  what the cluster has actually spent, and an estimate standing in would be the right
  shape and the wrong number. Records hold paths, accounting and the task -- never file
  contents, which are recoverable by path and are all the bulk (ADR-0039).
- `context.estimate_text_tokens`, and `AdmissionLease` carries its own wait so a record
  can say whether *this* delegation was slow because it queued.

### Changed
- The endpoint's own `concurrency` is now the gate's fourth rule instead of a semaphore
  local to `delegate_batch` (ADR-0037). That semaphore bounded a batch against itself and
  nothing else: two batches, or a batch beside a plain `delegate`, could exceed the limit
  it was reading, and a single `delegate` was never checked against it at all -- while
  `max_inflight_seqs`' own description already claimed both were checked. Now true on
  every path, and there is one gate rather than two that can disagree.
- Zero in any of the four admission settings is refused at load. It does not mean
  unlimited; it means nothing is ever admitted, so the gate queues every delegation until
  the wait times out and reports congestion for a setting that is simply wrong.
- `docs/CONFIGURATION.md`'s line budget 150 → 175. The file is generated, one row per
  setting, so its length is the count of settings; trimming it means deleting a setting or
  its description. The cap catches a table growing prose, which a row cannot do.

### Fixed
- The large-prefill classification counted the reply allowance, which is decode and not
  prefill. `max_tokens` defaults to 65536 against a 32768 threshold, so *every*
  delegation was classified large and rule 3 silently bounded the whole server at
  `max_inflight_large_prefills` -- while every other rule read as though it were the one
  binding. Found because the batch-concurrency test still passed with the endpoint rule
  deleted: it had been measuring the large-prefill cap all along. A request is now sized
  by two numbers, its KV footprint and its prefill, and both directions are tested.
- Neither transcript bug the ancestor shipped, reproduced and then defended. The record is
  assembled from identity captured **before** the attempt and written from a `finally`,
  because the agent's name is in scope only at the top of a delegation -- assemble it
  deeper and a failure has nothing to name, which is how the dispatches the transcript
  existed to explain came to log as `unknown`. And the writer returns nothing, so the
  response dict -- still assembled from conditional `**` spreads, the shape that leaked --
  has no value to pick up. Setting the directory is asserted to leave the whole response
  byte-identical, compared as a whole rather than by checking for known field names: a
  leak of a field nobody thought to list is the leak that would happen. Both were
  confirmed caught by reintroducing them.
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
