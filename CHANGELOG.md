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
