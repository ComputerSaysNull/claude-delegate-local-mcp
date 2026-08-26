<!-- ARCHIVE-AT: 700 -->
# Changelog

Newest first. Entries carry the **why**, not just the what: the symptom that prompted the
change, the cause, and the fix. A terse one-liner is not enough -- in six months the
reason is the only part still worth having.

Format: `- YYYY-MM-DD (#PR) [sha] what changed, and why.` The date and PR are written by
hand at merge time; `scripts/release.py` backfills the squash-merge sha at tag time,
because an entry lives in the commit it describes and cannot know its own hash.

## [Unreleased]

### Added
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
