<!-- BUDGET-PER-ENTRY: 55 -->
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
sees. **These numbers live here and nowhere else** — every other document links to this
entry, because a measurement copied is a measurement that drifts:

| file type | bytes/token |
|---|---|
| JSON, punctuation-heavy | 1.78 |
| TOML lockfile | 2.08 |
| markdown prose | 3.42 |
| minified python | 3.55 |
| python source | 3.89 |
| python, dense docstrings | 4.16 |

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

## 2026-08-26 — The reasoning-effort field, measured, and a comment that had the reason backwards

Nothing in this repository recorded how the `off/low/high/max` enum reaches the wire: the
M0a spike scripts were deliberately not shipped, and no sample request body survived. So
it was measured.

`reasoning_effort` is the field, it is real, and the server validates it. Its own 400 names
the accepted set: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. Our vocabulary
differs from it in exactly one word — we say `off`, it says `none` — so the adapter's
translation table has one row and every other level goes out verbatim. ADR-0013's refusal
to remap the top level down therefore costs nothing: `max` is a value this server really
takes.

`none` does what it says: zero characters of reasoning, against roughly 25,000 for the same
prompt with the field absent, finishing in 32 completion tokens instead of exhausting the
budget.

The surprise was in our own tree. `config.py` justified validating the enum by asserting
that vLLM silently ignores an unrecognised value, so a typo would cost you the setting with
no error. It does not: `reasoning_effort: "banana"` comes back as a 400 with a pydantic
literal error. The conclusion — validate rather than pass through — survives, because our
vocabulary is not the server's and an untranslated level would be refused after the prefill
was already paid for. The stated reason did not survive, and has been corrected in place.
A comment that is confidently wrong about a mechanism is worse than no comment, because the
next person builds on it.

Two things nearly went unnoticed:

- **The first measurement was worthless, and looked like a result.** With `max_tokens=2048`
  every case returned `finish_reason: "length"` with null content, so `completion_tokens`
  was 2048 in all seven — identical across control and every candidate, which reads exactly
  like "the field is ignored". Only raising the budget to 6144 separated them. A saturated
  experiment is indistinguishable from a negative one, and this stack's reasoning is long
  enough to saturate a budget that looks generous.
- **Effort is part of the cached prefix.** `prompt_tokens` moves with the level — 62 at
  `none` and `low`, 141 at `high`, 154 with the field absent — so the value rewrites the
  rendered prompt rather than only steering generation. ADR-0011's bit-identical prefix
  therefore holds *within* one effort level and not across them, and switching level
  mid-session discards the cache. Worth knowing before anything starts varying effort per
  turn.

`chat_template_kwargs.enable_thinking` was the other plausible candidate and does nothing
here: both polarities gave `prompt_tokens` identical to the control, with reasoning
unchanged. It is not sent.

ADR-0017's 400 was re-confirmed unchanged along the way, still naming
`VLLM_USE_V2_MODEL_RUNNER=0` rather than the boot flag its own documentation advertises.

---

## 2026-08-27 — `wsl.exe -e` brings the Windows working directory with it

Cost most of the end-to-end launch step, and the symptom pointed at the wrong file.

The server reads `models.toml` and `.env` relative to its working directory. Launched
through `wsl.exe -d Ubuntu-24.04 -e claude-delegate-local-mcp`, that directory is not the
install and not the WSL home — it is whatever the *Windows* parent's directory was,
translated. So the same registration works when Claude Code happens to be open on this
repository and fails with `Model registry not found` from any other project, naming a file
that exists and is correct.

Measured rather than reasoned about: `wsl.exe -d Ubuntu-24.04 -e pwd` prints the translated
Windows cwd, and changing directory on the Windows side changes what it prints.

`--cd` fixes it, with a trap of its own: `--cd /mnt/c/...` is rejected with
`ERROR_PATH_NOT_FOUND`, while the same directory given in Windows form is accepted. Both
were tried; only the second works.

Second finding from the same session, same shape: a venv's `bin/` is not on the PATH that
`wsl.exe -e` resolves against, so the bare console-script name fails with `No such file or
directory` — which reads as "not installed" when it is installed and on no relevant PATH.

---

## 2026-08-27 — At max effort this model does not answer, at any budget

ADR-0014 recorded that a 512-token budget returned null content and that "at 4096 it
answers but still truncates". The second half no longer reproduces, and building the empty-
answer recovery on it would have got the mitigation backwards.

Measured against the live endpoint, max effort, one hard prompt, budget varied:

| max_tokens | finish   | content | reasoning chars | completion tokens |
|------------|----------|---------|-----------------|-------------------|
| 512        | length   | null    | 1,482           | 512               |
| 6,144      | length   | null    | 18,006          | 6,144             |
| 16,384     | length   | null    | 48,115          | 16,384            |
| 32,768     | length   | null    | 91,753          | 32,768            |
| 65,536     | length   | null    | 231,977         | 65,536            |
| 16,384 at **low** effort | **stop** | **1,781 chars** | 18,629 | 7,033 |

Reasoning grows to fill whatever it is given and the answer never arrives. Every row at max
effort is the exhaustion signature, and completion tokens equal the budget exactly in all
five — the saturation trap JOURNAL 2026-08-26 already warned about, here as the *result*
rather than an artefact of it. The same prompt at low effort answered comfortably and
stopped on its own, using 7,033 of 16,384.

So **raising the budget is not the mitigation for this failure; lowering the effort is.**
The recovery order in `loop.py` puts the budget retry first because it keeps the prompt
prefix intact and the step-down second because changing the level discards the prefix cache.
That ordering is still right on cache grounds, but it would be pure waste here — and it
turns out not to fire at all with production numbers, for an arithmetic reason worth
stating rather than discovering later. High and max effort already resolve the budget to
`thinking_max_tokens_floor` (131072), and the model's own `max_tokens_cap` is 131072, so
"the larger of double the budget and the floor" caps back to the budget already sent. The
guard that skips a byte-identical retry therefore skips the whole stage, and the level steps
down on the second call. `test_at_max_effort_the_budget_retry_is_skipped_and_it_steps_down_immediately`
pins that arithmetic, because if the floor ever drops below the cap the stage silently comes
back and costs the model's entire cap to fail again.

Two things to carry forward. A max-effort delegation that ends in exhaustion now costs two
dispatches at 131072 tokens; the 65536-token call alone ran for over ten minutes, so the
pair sits near or past the client's 30-minute stdio idle timeout, which nothing holds off
until ADR-0018's per-turn notification lands with the turn loop. And the default effort
being `low` is doing more work than it looks: it is the difference between an answer and
nothing on this prompt, not a cost preference.

ADR-0014's decision stands — reproduce and guard rather than avoid — and its recorded
numbers are simply older than the deployment. Not edited, per the rule that ADR bodies are
not rewritten; this entry is the correction.

---

## 2026-08-27 — `thinking_token_budget` is still refused, and its error names no parameter

Re-probed before deciding whether to build M3's feature detection for it, since ADR-0017's
measurement was two days old and that ADR's own lesson is to prefer a probe over a doc.

Still a 400, with the same message naming `VLLM_USE_V2_MODEL_RUNNER=0` — the boot flag the
serving stack's own documentation does not mention. Unchanged, so ADR-0017 holds.

The useful part is the shape of the error, which would have quietly broken the detector as
designed:

    {"error": {"message": "thinking_token_budget is not yet supported by the V2 model
     runner. Run vLLM with VLLM_USE_V2_MODEL_RUNNER=0 to use thinking_token_budget.",
     "type": "BadRequestError", "param": null, "code": 400}}

`param` is **null**. The plan was to feature-detect structurally on
`error.param == "thinking_token_budget"` and fall back to a text match only if that failed.
Structural detection would have matched nothing, every time, and the fallback would have
been carrying the whole feature while looking like a safety net. Worth knowing before
writing a detector for any other optional field on this stack: the parameter name is in the
prose and nowhere else.

## 2026-08-29 — The serving stack does report its own window, and it agrees with ours

Measured before designing the context-overflow probe, because PLAN.md's item 4 speaks of
"a transport failure while probing" without saying what is probed, and this project has
no probe of context size at all. Two readings were possible and the measurement decides
between them, so guessing would have built the wrong thing.

`GET {base_url}/v1/models` returns, per model entry:

    "max_model_len": 1048576

which matches the operator-set `context_window` in `models.toml` exactly. So the probe
can **validate** rather than derive: the operator's number stays authoritative, and the
probe's only job is to notice when it disagrees with the server. That distinction is the
whole lesson of upstream's bug 4, where an auto-probe read the model file's architecture
maximum instead of the configured window and every threshold was computed against a
ceiling the backend would never reach.

A trivial completion also confirms usage is reported and usable:

    "usage": {"prompt_tokens": 103, "completion_tokens": 16, "total_tokens": 119,
              "prompt_tokens_details": {"cached_tokens": 0, ...}}

This mattered because `openai_compat.py` turns a missing `usage` block into `0` with no
error, which is indistinguishable from a genuinely tiny prompt — a silent zero would have
made the plateau detector unfalsifiable. It is not missing here.

Two incidental notes, neither load-bearing but both cheap to record:

- `prompt_tokens` was 103 for a nine-word message. That is chat-template overhead, and it
  means a per-turn growth threshold measured in tens of tokens sits inside the noise of a
  single message envelope.
- `prompt_tokens_details.cached_tokens` is reported separately and does **not** reduce
  `prompt_tokens`. Prefix caching therefore cannot masquerade as a plateau, which is one
  fewer false-positive source for the retroactive check to defend against.

`max_tokens: 16` returned `finish_reason: "length"` with `content: null` — the same
reasoning-exhaustion shape recorded on 2026-08-27, reproduced here incidentally at a
budget far below the floor. Not a new finding; noted only so the transcript is not
mistaken for a failed probe.


## 2026-08-29 — bwrap on a GitHub runner fails at loopback, not at the user namespace

Planned as a two-minute spike and worth the entry only because the expected answer was
wrong, in the direction that would have produced a confident false statement in a document.

The question was whether the sandbox tests could drop their `integration` mark and guard
every pull request. The expected obstacle was AppArmor: Ubuntu has restricted unprivileged
user namespaces since 23.10, `ubuntu-latest` is 24.04, and that is exactly the mechanism
bubblewrap needs. Reasoning stopped there, and reasoning was wrong.

What the runner actually prints:

    bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted

The user namespace is created without complaint. What fails is one step later --
`--unshare-all` implies a new *network* namespace, and bwrap brings up `lo` inside it, which
needs `CAP_NET_ADMIN` that the runner does not grant. Identical on 3.11 and 3.12.

Two things worth keeping. The obstacle was real but not the one predicted, so a plan written
from the prediction would have described the wrong fix -- and "AppArmor blocks userns on CI"
is the kind of plausible sentence that survives into documentation unchallenged. And the
first attempt measured nothing at all: the step went into the `lint` job, which does not
install bubblewrap, printed `command not found`, and passed anyway because it was marked
`continue-on-error`. A check that cannot fail, in a spike written to answer whether a check
could run. Read the log, not the tick.

## 2026-08-29 — Delegation latency measured, and it is generation that varies, not tools

Five identical delegations of each shape against `deepseek-v4-flash`, through
`server.dispatch_delegation` — the same function the MCP tool calls, skipping only the
stdio hop and `arm_overflow`.

| Shape | mean | median | min | max | stdev |
| --- | --- | --- | --- | --- | --- |
| Tool-using, WSL (2 turns, 1 `read_file`) | 2.23s | 2.22s | 2.19s | 2.30s | **0.04s** |
| One-shot prose, WSL | 52.44s | 33.63s | 16.67s | **127.42s** | 46.22s |
| One-shot prose, Windows | 15.62s | 18.53s | 7.95s | 22.99s | 6.89s |

Tool use is not the expensive part. The tool task is 20x faster than the prose task while
doing strictly more, because its answer is 82 characters and largely determined. The prose
task varies 8x between runs on identical input, and the slowest run returned the *shortest*
output of its five — so it is not length, it is model load.

Do not read the Windows/WSL gap as a host difference. With a stdev of 46s on five samples
the two populations overlap heavily; separating host from noise needs far more runs than
this. One sample would have been worthless in either direction, which is the point.

For choosing timeouts: a turn is normally seconds to about two minutes. 1800s per turn is
the right order of magnitude, and nothing here justifies lowering it — a full-cap max-effort
run is already recorded elsewhere in this journal as taking tens of minutes.

## 2026-08-29 — Running the server on Windows refuses every path, and says the wrong reason

Cost half an hour and a 30-minute hang before it was noticed, because the symptom names
something else entirely.

A timing harness was pointed at the server from **Windows** Python with
`workspace_roots=("C:/Users/.../ClaudeLocalMCP",)`. Every `read_file` came back with *"its
resolved path lies outside the configured workspace root"*. The path was plainly inside the
root, which is what made it confusing. The path policy normalises to POSIX, so a Windows
root never matches a resolved path — the server is meant to run in WSL, and the suite's
`posix_only` markers already encode that, which is why no test catches it.

The delegation did not fail fast. The model retried a call that could never succeed for the
whole turn budget, six turns, and only then answered that it could not read the file. Five
repetitions of that is what looked like a hang. The loop behaved correctly throughout; there
was nothing to fix in it.

Worth knowing twice over: nothing refuses to start the server on Windows, and the refusal a
delegated model receives blames the workspace configuration rather than the platform.

## 2026-08-29 — Emptying the withhold set offers `run_bash` on hosts that cannot run it

Found while chasing the above, and it is a real regression rather than a curiosity.

`available_tool_names()` subtracts `WITHHELD_TOOL_NAMES`, which M5 emptied, and it takes no
`Config` — so it never asks `sandbox.available(cfg)`. On a host with no bubblewrap,
`run_bash` is now declared to the model and refuses every call. The Windows diagnostic above
shows the cost directly: turn 4 of 6 spent calling `run_bash` and being told no.

The comment deleted when the set was emptied had argued exactly this, about the withholding
it was justifying: *"Declaring it anyway spends a turn to learn that — and ADR-0016 measured
that the first turn is already often wasted on orientation, so this would be the second."*
The reasoning survived the code it was attached to.

The fix is not to re-withhold statically. It is that declaration should ask whether the
sandbox can actually run, the same way `sandbox.run` already refuses when it cannot — one
condition, checked where the tool set is resolved rather than remembered as a constant.

## 2026-08-30 — The suite timings that sized `run_bash_timeout`, re-measured

`PLAN.md` and `CHANGELOG.md` both carried "157s in WSL and 361s on Windows" for this
suite, and the same pull request that recorded those also recorded 181s on Windows and 235s
in WSL for what reads like the same thing. The two sets disagree about *which host is
slower*, so at least one was measuring a different population -- most likely one included
the live-model tests and the other did not, though nothing written down says which.

Re-measured before setting the timeout, since a number nobody can reproduce is not evidence.

| Run | Windows | WSL |
| --- | --- | --- |
| Serial | not taken | **281s** |
| `-n 8` | 95s - 213s | 100s - 187s |

Only the WSL serial figure actually matters for the setting. `run_bash` executes inside
bwrap inside WSL, and a model told "run the tests" runs `pytest`, not `pytest -n 8`. So 281s
is the number, and the old 120s default sat below it by more than a factor of two.

The parallel figures are quoted as ranges because that is what they are: five runs across
the session spanned 95-213s on Windows for an unchanged tree. `#37` recorded 60s and
attributed the run-to-run spread to the live model generating rather than the harness, which
is consistent -- but it means a single `-n 8` sample is worth very little, and the 3x
speed-up it claims is a best case rather than a typical one.

The general lesson is not about pytest. Two figures for the same quantity, in the same
commit, and nobody noticed until the number had to be used for something. A measurement
recorded without saying what was measured decays into a number that looks authoritative and
cannot be checked.

## 2026-08-31 — What a client actually does when it abandons a delegation

`#45` fixed a batch that looked dead. The open question it left was whether a client
giving up ever reaches the running dispatch, and the honest answer written down at the
time was "no evidence either way". Five paths, four measured against the live server and
one still inferred:

| the client | cancel sent | server process | slot | the work |
| --- | --- | --- | --- | --- |
| Esc, or stopping the task | yes | survives | freed in <2s | record written |
| its tool timeout fires | yes | survives | freed at 119.7s | record written |
| its window is closed | no | killed with the tree | orphaned, inert | lost |
| its terminal is closed | no | killed with the tree | orphaned, inert | lost |
| its stdio idle timeout | *never reached* | — | — | — |

Cancellation works, completely. `notifications/cancelled` reaches the coroutine as
`CancelledError`, so `Admission.admit`'s `finally` releases and `transcript.write` runs:
one record read `ok: false, error_type: CancelledError, elapsed_seconds: 119.714` with the
cancel attributed to `ServerSession._receive_loop`. Nothing needs building for that.

A disconnect, separately, reaches the coroutine as **nothing at all** — no exception, no
`finally`. FastMCP does notice the EOF and starts shutting down, but waits for the
in-flight tool first: idle it exits in ~0.2s, with a 6s tool running it exits the instant
that tool returns. `ctx.report_progress` also keeps succeeding after the pipes are closed,
so it is not a liveness signal and guarding it buys nothing. Probed with a throwaway
one-tool stdio server, which is the only way to see any of this.

**The conclusion that matters is a negative one.** A capacity leak needs the server to stay
alive while nobody is waiting, and every measured way a client departs *kills* the server —
so no EOF ever arrives at a surviving process, and the drain above cannot bite. Killing it
is clean besides: the backend stops generating, and the orphaned slot record is inert
because readers drop dead records, with the next real admission rewriting the file.

So the only shape that can still leak is a client that lives and stops caring, which is the
idle timeout alone — and the reason a one-shot can reach it is that `run_one_shot` sends no
progress at all, unlike the batch and turn paths `#45` covered.

**The idle timeout resisted being measured**, and the attempt is worth recording so the
next one starts further along. `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` set to 120000 had no
observable effect: a one-shot went 1645s with nothing on the wire and finished normally,
155s short of the 1800s default. One hypothesis, from a third-party summary rather than a
probe, is that a per-server `timeout` acts as a *floor* so idleness cannot kill sooner —
though this server's entry sets no such field, which would make it a default floor rather
than a configured one. Unresolved. What is certain is that the 3616s incident happened, and
that nothing reproduced here explains it yet.

Two things this cost, worth not repeating. Reasoning about which of `--amend`'s forms, or
which disconnect, "obviously" behaves like another was wrong every time it was tried; each
row above changed the plan when it landed. And the price of a killed dispatch is not the
tokens, it is that the stream holds only its `start` line — no turns, no `end`, no record —
so an abandoned delegation is unrecoverable and, until this was fixed, was listed as live
for ever.
