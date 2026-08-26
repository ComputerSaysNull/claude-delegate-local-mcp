<!-- BUDGET-PER-ENTRY: 55 -->
<!-- ARCHIVE-AT: 600 -->
# Journal

Things that took real work to figure out, so the next person does not pay for them
twice. Append-only, newest last. Not a diary: routine work leaves no entry.

The bar is roughly "this cost more than an hour, or it was surprising enough that I
would have got it wrong again in six months".

---

## 2026-08-25 — A generated doc can be validated against code that no longer exists

Found while testing the docs gate, by accident, and it would have quietly undermined the
whole anti-drift mechanism.

The generator imports `config.py` and compares its output to the committed markdown.
Changing a default from `6` to `9` and back again inside one second produced a
`__pycache__` entry that Python considered valid — because it validates a `.pyc` on
**(mtime, size)**, and `6` and `9` are the same byte length. The generator then read the
cached compile and reported the document as current against a version of the code that
had already been replaced.

The trap within the trap: `python -B` does **not** fix it. That flag stops Python
*writing* bytecode; it happily *reads* an existing stale cache. I "fixed" it that way
first and the test still returned the wrong value, which is the only reason I noticed.

What works is redirecting the cache somewhere empty for the run, so a fresh compile is
forced:

    sys.pycache_prefix = tempfile.mkdtemp(prefix="cdl-gen-pyc-")

Applies to every generator, not just this one. Any tool whose entire job is comparing a
committed artefact against live source must not trust a cached compile of that source.

## 2026-08-25 — The serving stack's documentation and its running behaviour disagree

Three instances found in one afternoon, so treat this as the norm rather than bad luck.
Probe the endpoint; do not trust the repository.

- `thinking_token_budget` is documented as gated by a GPU-hotfix boot flag. The server's
  actual 400 response names a completely different switch: `VLLM_USE_V2_MODEL_RUNNER=0`.
- The example environment file ships maximum reasoning effort, while two of the stack's
  own documents still recommend the low setting. A changelog entry shows the default was
  raised and those documents were never updated.
- Published prefill figures are pessimistic against measurement: 32K tokens is
  documented at 23-61s, measured at 17s. Probably measured under concurrency, but the
  gap matters when sizing a budget against it.

Worth noting this is the same failure class this project's own documentation strategy is
built to prevent, observed in the wild, in the stack we depend on.

## 2026-08-25 — Rationing context by bytes is the wrong unit, and the error is not small

The prefetch budget started in bytes because that is what a filesystem hands you. Bytes
per token turn out to vary by more than 2x across the file types a delegation actually
sees: 1.78 for punctuation-heavy JSON, 2.08 for a TOML lockfile, 3.89 for Python source,
4.16 for Python with dense docstrings.

Two consequences, both missed until measured. A `bytes / 3` estimator that was described
as conservative under-counts JSON by 41%. And a per-file cap in bytes silently allows
twice as much *context* for a data file as for a source file, which is the opposite of
what anyone wants — data files are the ones worth truncating.

Denominating in estimated tokens, with per-extension ratios rounded down from
measurement, fixes both. See ADR-0019.

The general lesson: when rationing a resource, denominate the budget in the unit the
resource is actually consumed in, even when a cheaper proxy is right there.

## 2026-08-25 — bwrap in WSL2: two traps, and an error message that lies

Provisioning Ubuntu 24.04 under WSL2 and standing up the sandbox turned up two failures
that would each have cost an afternoon at M5.

**The lib64 symlink is mandatory, not optional, and the error blames the wrong thing.**
An empty-root sandbox with `--ro-bind /usr /usr` and `--symlink usr/bin /bin` fails with:

    bwrap: execvp /usr/bin/echo: No such file or directory

The binary is present and readable. What is missing is `/lib64/ld-linux-x86-64.so.2`, the
ELF interpreter — and when the kernel cannot find a binary's interpreter it returns ENOENT,
which surfaces as "No such file or directory" against the *executable*. Every diagnostic
instinct points at the wrong file.

So `--symlink usr/lib64 /lib64` is required on x86-64. The planning notes had it as
"if present on the distro", which is wrong: without it nothing dynamically linked runs at
all. Add `--symlink usr/sbin /sbin` for the same reason.

**DNS inside the sandbox needs a WSL-specific bind.** With the network deliberately
re-shared for an agent that opted in, connections by IP succeeded and connections by
hostname failed. The cause: under WSL, `/etc/resolv.conf` is a symlink to
`/mnt/wsl/resolv.conf`. Binding `/etc` therefore binds a *dangling* symlink, and name
resolution fails while connectivity is fine. The fix is to bind `readlink -f
/etc/resolv.conf` at its own real path as well.

**A test worth keeping: verify network denial by IP, not by hostname.** With resolv.conf
unbound, a hostname request fails whether or not the network namespace is actually
isolated. Testing by hostname alone would have reported the sandbox as network-tight when
it might merely have had broken DNS. Only a by-IP attempt distinguishes the two, and only
the by-IP result is evidence.

Verified working after the fixes: commands run, python3 resolves, network denied by
default (000 by IP), the real HOME absent rather than read-only, `~/.ssh` absent, a bound
workdir writable, and network available when explicitly re-shared.

## 2026-08-25 — The 9p bridge, measured: the penalty is real but it is not uniform

The plan flagged `/mnt/c` performance as an open risk. Measured, same tree, same machine,
warm caches, WSL2 Ubuntu 24.04:

    operation                       /mnt/c (9p)      ext4     penalty
    git status                            0.43s     0.00s      ~100x
    read every tracked file               0.16s     0.02s         8x
    write 300 small files                 1.96s     0.01s      ~200x
    create a venv                        68.92s     2.51s        27x
    pytest, full suite (avg of 3)          7.11s     0.59s        12x
    pytest, one file                       0.56s     0.20s        2.8x

The shape matters more than the headline. Ratios look alarming; absolute numbers are
mostly survivable. A 12x penalty on the full suite is 7 seconds, not 70, and the
self-verification loop still costs zero Claude tokens. Metadata-heavy work is where 9p
genuinely hurts: venv creation at 69 seconds is the one number that stings, and it is
rare.

Also learned, and it constrains the obvious workaround: **Windows cannot use a `\wsl$\`
UNC path as a process working directory.** `CreateProcess` refuses it outright. So
"keep Claude Code on Windows but move the repo onto ext4" does not work as stated —
Windows-side tools that need a cwd, including any shell command Claude runs, cannot
operate there without mapping a drive letter first.

## 2026-08-25 — CI found two bugs that a local run structurally could not

The first pull request failed three of four checks, and both root causes were invisible
to any amount of local testing.

**A generated document embedded a platform-specific separator.** Tuple-valued defaults
were rendered with `os.pathsep`, which is `;` on Windows and `:` on Linux. So the file
generated on the development machine could never match one generated in CI: not merely
failing, but *unsatisfiable*. A reproducibility bug inside the mechanism whose entire job
is guaranteeing reproducibility.

Local runs could not find it because they are all one platform. The generated file now
renders lists with a fixed separator and the header states how they are actually parsed.

**The commit-identity check flagged GitHub's own merge commit.** For a `pull_request`
event, Actions checks out a synthetic merge commit authored by `noreply@github.com`. That
is not a contribution, and refusing it would have blocked every pull request forever — a
gate that blocks everything is exactly as useless as one that blocks nothing, and rather
more annoying. Fixed with `--no-merges`; safe here because the repository squash-merges,
so real merge commits never appear.

**Then the fix tripped the gate**, because the comments explaining it contained the
literal address. The dodge was to reword the comment. The actual problem was that one
list was serving two different policies: commit authorship, which must be strict, and
addresses appearing in file content, which must tolerate documentation placeholders and
service accounts. Allowing a service address to author commits would defeat the identity
check; refusing it inside a comment explaining that check is pointless friction. Two
lists now.

The wider lesson, and the third instance of this shape today: a check whose false
positives are annoying gets weakened, and a weakened check is how the hole arrives. When
a check fires on something legitimate, ask whether it is asking the right question before
adjusting its answer.

## 2026-08-25 — A hand-maintained invariant drifted within the hour of being written

CONTRIBUTING said the gate's email allowlist and gitleaks' "must stay in step -- the gate
reads one file, gitleaks reads the other". Two hours later the gate was fixed for a false
positive, gitleaks was not, and CI failed on exactly that divergence.

An hour is about as fast as an invariant can fail, and it failed while the person
maintaining it was actively thinking about it. That is the argument against this whole
category of rule: "remember to keep X and Y in step" is not a policy, it is a bet on
attention, and attention is the resource under most pressure precisely when the invariant
matters.

The lists are now data files, and `scripts/gen_gitleaks_config.py` renders one from the
other with the freshness check covering it. Same shape as the configuration reference:
the fact lives in one place and every consumer is generated from it.

Worth noticing that this project's own documentation contained the anti-pattern it was
written to prevent, in a section explaining how to prevent it. Rules are easier to write
than to follow, including for the person who wrote them.

The substantive design point underneath: one list was serving two different policies.
Commit authorship must be strict, because an address that can sign commits is an identity
claim. File content must tolerate documentation placeholders and service accounts, because
refusing to let a comment name the address it is explaining buys nothing. Conflated, they
forced a choice between a false positive on prose and a hole in the identity check --
and the tempting fix, rewording the comment, would have preserved the flaw while hiding
the symptom.

## 2026-08-25 — Measuring the wrong population made a real protection look impossible

A personal identifier was written into a test file as an illustrative example. The
forbidden-strings list held only its full form, and single words of a multi-word entry do
not match, so nothing caught it. Review did.

The first response was to declare the fragment unlistable: it appeared roughly two
hundred times, so listing it would fire constantly and the check would end up ignored.
That reasoning was wrong, and the error generalises. **Every one of those occurrences was
inside a virtualenv, which is not tracked and which the gate never reads.** Measured over
the files actually scanned, the case-sensitive whole word appeared *zero* times.

Counting the wrong population turned a cheap fix into an apparent impossibility. Before
rejecting a control as too noisy, measure the noise on the set the control actually sees.

The fix is a `word:` prefix selecting case-sensitive whole-word matching, so a fragment
that is also ordinary vocabulary can be listed without firing on ordinary usage.

Two further lessons, both about claims rather than code. The statement that "the system
would now catch it" was false when written -- the control covered a different fragment
from the one that had actually been typed. And describing *why* a control was hard is its
own disclosure risk: an explanation precise enough to be useful ("it collides with a
common term in this domain") can narrow the value to a single candidate. Write the
mechanism, not the specimen.

## 2026-08-25 — A pull request body is a public surface no hook can gate

A personal identifier was published in a pull request body. Every check in this project
passed, because a pull request body is written outside git: the pre-commit hook cannot
see it, and no file or commit-message scan covers it. The identifier had been correctly
kept out of the code, the tests and the commit messages, and went out through the one
door nobody had thought to watch.

Two consequences, and the second is the one that cost something.

CI now scans the title and body from the Actions event payload. That is a backstop, not a
gate: it runs after publication, so the text is already public for however long the run
takes. Nothing can gate this surface before the fact, which is why the rule in CLAUDE.md
matters more than the check -- **write the mechanism, never the specimen**. A worked
example with a real value is the leak; a description of the shape is not.

Editing the body does not undo it. GitHub retains edit history for pull request
descriptions, visible to anyone who can read the repository, and users cannot delete a
pull request. The remedy was to delete the repository and push the same commits to a
fresh one, which was cheap only because the repository was a day old and its reasoning
lives in DECISIONS, JOURNAL and CHANGELOG rather than in pull request threads. It would
not have been cheap a month later.

One artefact of that: squash-merge subjects carry `(#1)` through `(#5)`, referring to
pull requests in a repository that no longer exists. Numbering restarted at 1, so those
suffixes now point at nothing, and a future `(#1)` will be a different change entirely.
Left as-is deliberately -- rewriting the subjects would change every hash, and PLAN.md
cites hashes.

Also worth recording, because it is the same failure this project keeps finding: a script
in this session echoed "deleted" after a delete that had returned 403. It reported success
without checking the exit code. A check that cannot fail is worse than no check, and that
applies to the throwaway line in a shell script as much as to the gate.

## 2026-08-26 — A policy required a review that had no implementation at all

ADR-0001 says upstream fixes are read and reimplemented rather than cherry-picked, and sets
a six-to-twelve-month half-life on watching upstream. Sound reasoning, written down, cited
from three documents. It had never once run.

Three things were wrong at the same time, and each hid the others:

- The tracking mechanism was "candidates are tracked in a pinned issue" — no number, no
  link. The repository was deleted and recreated on 2026-08-25 to unpublish a leaked
  identifier, and the issue went with it. The prose survived, because prose does not have
  a foreign key.
- The `upstream` remote had never been fetched. Not stale — empty. There were no
  `refs/remotes/upstream/*` at all, so there was nothing to review against, and no error
  either: nobody had asked.
- The remote pointed at the primary ancestor, dormant since 2026-08-21. The fork that had
  been shipping steadily for a week was not configured anywhere, and is not mentioned in
  any tracked file except NOTICE, where it appears as a licence attribution.

What made this hard to see is that every individual artefact looked healthy. The ADR is
well argued. CONTRIBUTING names the remote and warns about the push URL. NOTICE credits
the fork feature by feature. Nothing was missing; nothing was connected.

The lesson generalises past upstream: **a rule whose only artefact lives outside the
repository can be deleted without a diff.** No hook fires, no check fails, no review
notices, because the thing that vanished was never in the tree. The repository already
knows that a check which cannot fail is worse than no check — this is the same failure one
level up, where the check was never written because the prose sounded like one.

The fix that matters is not the audit file. It is that the artefact is now *inside* the
repository, where deleting it is a diff someone has to approve.

## 2026-08-26 — The unverified region of a generated file is where the wrong facts live

STATUS.md named branch `docs/repo-recreated` and commit `cdff819`. Both had stopped
existing: the branch was deleted on merge, the hash rewritten by the squash. A generated
file, wrong for a day, and generated files are precisely the ones you stop checking.

The interesting part is why nothing caught it. `gen_status.py --check` compares only the
text above `## Repository`:

    def stable(text): return text.split("## Repository")[0]

That cut is *correct*. Commit counts move with every commit, so a byte comparison would
fail constantly and teach everyone to pass `--no-verify`. But the consequence had never
been stated: everything below that heading is verified by nothing, forever, and that is
exactly where the generator was writing a branch name and a hash.

So the rule is not "regenerate more often" — that is a manual step, and generated documents
exist to remove manual steps. The rule is about what may live in an unverifiable region:

**Only facts whose staleness is harmless.** A count that drifts is off by one and nobody is
misled. A branch name or a commit hash that drifts names something that does not exist, and
is read as authoritative because the file says GENERATED at the top. The recent-commits
list went too: it duplicated `git log`, cited hashes the squash rewrites, and contradicted
the generator's own docstring, which says a status file that accumulates is a second
changelog with none of the discipline.

Fourth check found in this repository that could not fail, and the first where the gap was
in the *comparison's scope* rather than in the comparison. Worth looking for elsewhere: any
check that excludes a region for a good reason, and never says what is now unguarded.

## 2026-08-26 — A directory is an interface, and writing to it is a write to every check that reads it

The upstream review had to be stored somewhere. `docs/audits/` existed, was empty, and the
name fitted. Nothing checked what already read that directory, and something did: the
audit-due check took the alphabetically last `*.md` there as the last recorded documentation
audit and counted commits since.

Documentation audits are named `YYYY-MM-DD-audit.md`. `upstream-review-2026-08-26.md` begins
with a letter, so it sorts after every one of them, for good. The counter went from 14 — the
whole history, correct, because no documentation audit has ever run here — to 0. A warning
due at commit 60 would not have arrived until 74, and every future review would have pushed
it further out.

Three things make this worth writing down.

**It was invisible by construction.** No check failed. The gate passed, CI passed, 104 tests
passed, and the counter read zero, which is what a healthy counter also reads. The only
symptom was the absence of a warning that was never going to appear. It surfaced because
someone asked whether the docs-audit agent had run — a question, not a test.

**It shipped in the same change that removed two checks that could not fail.** Reading
carefully about that failure mode all afternoon provided no protection against committing a
fresh instance of it. Knowing the pattern is not the same as checking for it, and the check
that would have caught this did not exist because the directory looked inert.

**Two faults were needed, and only one was mine.** Selecting by `sorted(...)[-1]` was already
wrong — it dates a filename rather than asking git, which is right only while one naming
scheme is in use, with nothing enforcing that. It had simply never been exercised, because
the directory was empty from the day it was created. Fixing only my half would have left the
trap armed for whoever wrote the next file there.

The rule that generalises: **treat a directory as an interface.** Before putting a file in
one, grep for what reads it. Here the readers were an email-scan exemption, a size-budget
exclusion, a gitleaks path rule, and the staleness counter — four consumers of a path that
looked like nothing more than a place to keep files.

Second time this project has found a policy artefact colliding with a check that scanned it,
after the secret-glob list matched its own `*secret*` pattern. That is a pattern now, not a
coincidence.
