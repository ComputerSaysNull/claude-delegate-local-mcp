<!-- BUDGET-PER-ENTRY: 30 -->
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
