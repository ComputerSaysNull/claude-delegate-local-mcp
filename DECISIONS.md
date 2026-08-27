<!-- BUDGET-PER-ENTRY: 60 -->
<!-- ARCHIVE-AT: 900 -->
# Decisions

Numbered ADRs, newest first. **Append-only**: the body of a decision is never edited,
because the reasoning at the time is the point of the record. When a decision stops
being true, only its heading changes:

- **Superseded** — wholly replaced. Struck through, and linked forward.
  `## ~~ADR-0004 — ... ~~ — Superseded by ADR-0009`
- **Partially superseded** — part still holds, typically a measurement whose conclusion
  turned out wrong. Status only, no strikethrough: striking it would wrongly imply the
  whole entry is dead, and the surviving part is usually the expensive part.
  `## ADR-0015 — ... — Partially superseded by ADR-0019`

**The headings are the index.** `grep '^## ' DECISIONS.md` lists every decision with
its number, date, title and status. Read the headings; open a body only when that
decision is actually in play. There is deliberately no index table — a table would
be a second copy of the same facts, and second copies drift.

---

## ADR-0029 — 2026-08-27 — Tool results never carry the endpoint address — Accepted

The adapter already keeps the host out of its exception strings, on the reasoning that an
exception reaches a log and from there a pasted issue comment. `backend_status()` is the
first tool whose **return value** could carry it, and a health report is more likely to be
pasted somewhere public than a stack trace is — that is what a health report is for.

So the rule extends from exceptions to results: nothing a tool returns names the endpoint.
`backend_status()` reports the registry key, the failure layer and the HTTP status, which
is what the reader acts on anyway; the address is in `models.toml`, which is gitignored
precisely because it names a host. The probe's model list is withheld for the same reason,
a count standing in for it — a list of served ids is somewhere an internal name leaks
without anyone choosing to disclose it.

This costs something real. Diagnosing which of several endpoints failed now means reading
the registry key and looking it up, rather than seeing the URL. Accepted, because the
alternative leaks by default and is discovered only after it has been published, at which
point it cannot be withdrawn.

The rule binds future tool output too — the operator dispatch transcript in M7 most
obviously. A tool that genuinely cannot be diagnosed without the address should say so and
be argued on its own terms, rather than each author re-deriving this from first principles.

## ADR-0028 — 2026-08-26 — An amend is judged against HEAD~1, and a pass that relied on it says so — Accepted

The owning-doc check compared the staged index against HEAD. Correct for a normal commit;
wrong for `git commit --amend`, whose parent is HEAD~1. The files already inside the commit
being amended are part of what lands but are absent from the index, so a complete commit was
reported as incomplete. The documented workaround was `git reset --soft HEAD~1` and recommit
— considerable ceremony to answer a question the tool had got wrong. It fired twice while
this session's own commits were being prepared.

**It cannot be detected outright, and that is the whole difficulty.** `prepare-commit-msg`
receives the message source and the commit it came from, and `git commit --amend` arrives as
`source=commit, sha=HEAD`. So does `git commit -C HEAD`, which is *not* an amend and whose
parent is HEAD. `GIT_REFLOG_ACTION` is unset for both. All of this was measured in a
throwaway repository rather than assumed, because the first design depended on the two being
distinguishable and they are not.

Rejected: treating `source=commit` as an amend outright. It would silently misjudge
`-C HEAD`, and the failure would be a *pass* — the previous commit's document counted toward
this commit's code. A check that wrongly blocks is annoying; one that wrongly passes is the
thing this repository keeps finding.

Rejected: unioning HEAD's files into the changed set unconditionally. Same false pass, on
every commit rather than a rare one.

So both readings are evaluated. Strict first — index against HEAD. If that passes, nothing
else happens. If it blocks and the message came from HEAD, the amend reading is tried, and a
pass there is reported with a WARN naming the files it counted from the previous commit and
saying plainly what it assumed. The escape is real but never quiet, which is the same
bargain as the `Docs-Gate-Skip` trailer: an escape hatch nobody can see becomes the default
route; one that leaves a line in every run does not.

The marker is consumed when read, and the hook rewrites it on every commit, so a commit
abandoned between the two hooks cannot leave state behind that changes how the next one is
judged.

## ADR-0027 — 2026-08-26 — config.load() reads .env; an MCP client's env key cannot reach the server — Accepted

The README has said `cp .env.example .env` since the first commit. Nothing read it.
`config.load()` consulted `os.environ` and nothing else, there is no dotenv dependency,
and the `mcpServers` block in the README carries no `env` key. Creating the file did
nothing, reported nothing, and left every setting at its default — a documented setup step
that was a no-op, which is worse than an undocumented one because it is believed.

**The alternative does not work here.** Putting an `env` key in the MCP client's
configuration looks like the idiomatic fix, and on a native-Linux install it is. This
project's documented topology is not that: the client runs on Windows and launches
`wsl.exe -d Ubuntu-24.04 -e claude-delegate-local-mcp` (ADR-0002, ADR-0020). An `env` key
sets variables for **wsl.exe, on the Windows side** — one hop short of the Linux process
that reads them. Carrying a variable across that boundary additionally requires `WSLENV` to
name it, set in the Windows environment before launch. Forgetting that produces no error
and no symptom beyond the setting being ignored, which is the same failure this decision
exists to remove.

So `config.load()` reads the file itself, in about fifteen lines, with no new dependency:
`pyproject.toml` pins fastmcp and httpx deliberately, and a dotenv package to read
`KEY=VALUE` would cost more than it explains.

Three choices inside it are deliberate:

- **The real environment wins over the file.** Standard convention, and it keeps an
  explicit one-off override working. The file fills what the environment omits.
- **A file named explicitly and missing is an error**, while a discovered `<repo>/.env`
  that is absent is not. Not having one is normal; asking for a specific one is a promise,
  and silently substituting defaults for a named file is precisely the bug above.
- **Passing `environ` suppresses discovery.** Otherwise the test suite reads whatever
  `.env` happens to sit in the working tree, and passes or fails according to a file
  nobody in the test wrote.

There is no escape processing. `DELEGATE_WORKSPACE_ROOTS` on Windows is a path full of
backslashes, and unescaping it would corrupt it without saying so. A leading `export ` is
stripped and a name that cannot be an environment variable raises, because both otherwise
parse to a key no setting matches — read, accepted, ignored.

## ADR-0026 — 2026-08-26 — main is protected by a checked-in ruleset, with no bypass and zero required reviews — Accepted

`main` is protected by a ruleset checked in as `.github/ruleset.json`, applied with
`gh api -X POST repos/OWNER/REPO/rulesets --input .github/ruleset.json`. Checking it in
makes the configuration reproducible rather than a thing someone once clicked, and a
review of it a diff rather than a tour of a settings page.

Two of its values are not self-explanatory, and JSON carries no comments, so they are
recorded here rather than in the file or in CONTRIBUTING.md — which describes what
contributors must do, not why the repository is configured as it is.

**`bypass_actors` is empty.** Direct pushes to `main` are refused for everyone including
the owner. Rulesets permit this; classic branch protection did not, which is part of why
a ruleset was chosen at all. A protection the owner can step over protects against
accidents only, and the accidents are the owner's.

**`required_approving_review_count` is 0, deliberately.** GitHub will not let anyone
approve their own pull request, so on a single-maintainer repository requiring one review
does not raise the bar — it locks the only maintainer out of their own repository
permanently, with no self-service way back. The four required checks carry the weight
instead: the gate, tests on 3.11 and 3.12, and the secret scan. None of those can be
satisfied by asserting that the change is fine.

This is the value most likely to be "corrected" by someone reading it as an oversight.
It is not. Raise it to 1 on the day a second person can approve, and not before.

## ADR-0025 — 2026-08-26 — Upstream reviews live in docs/reviews/; only a documentation audit resets the audit clock — Accepted

ADR-0023 put upstream reviews in `docs/audits/` without checking what already read that
directory. The audit-due check did: it took the alphabetically last `*.md` there as the last
recorded documentation audit and counted commits since. Audits are named
`YYYY-MM-DD-audit.md`, so a filename beginning with a letter sorts after every one of them
and wins permanently.

Effect, measured: the counter went from 14 -- the whole history, correct, because no
documentation audit has ever run here -- to 0. A warning due at commit 60 would not have
appeared until 74, and each further review would have pushed it out again. The check was
disarmed, silently, by a record that is not an audit at all, in the same change that removed
two other checks which could not fail.

Two faults, and it needed both. The directory mixed two kinds of record, and the check dated
a filename instead of asking git. Fixing either alone leaves the other armed, so both change:

- Upstream reviews move to `docs/reviews/`. `docs/audits/` holds documentation audits and
  nothing else, which is what the counter has always claimed to measure.
- The check asks git for the most recent commit touching `docs/audits/`. Filename order is
  only ever correct while a single naming scheme is in use, and nothing enforced that.

Only the location in ADR-0023 changes. Its substance -- that the review mechanism must be an
artefact inside the repository rather than an issue in a tracker that can be deleted -- is
untouched, and is why this is a partial supersede rather than a replacement.

The general lesson is worth more than the fix: adding a file to a directory is a write to
every check that reads that directory. Nothing here treats a directory as an interface, and
this is the second time a policy file has collided with a check that scanned it -- after the
secret-glob list matching its own `*secret*` pattern.

Not chosen: making the counter ignore filenames it does not recognise. That is a denylist,
and it fails open -- the next unrecognised name resets the clock again. Asking git for
recency cannot be fooled by naming at all.

---

## ADR-0024 — 2026-08-26 — The upstream review of 2026-08-26: adopt six, reject two as already covered — Accepted

First review under ADR-0023. The primary ancestor had nothing new — its turn countdown and
real-exit-code capture are already credited in NOTICE. Everything below comes from the
fork, covering 2026-08-21 to 2026-08-25. Full evidence, per item, in
`docs/audits/upstream-review-2026-08-26.md`.

Adopted as planned work, not as code:

- **Context-overflow handling.** Promoted out of Deferred into M3. They shipped what this
  project parked, and our deferred wording had already converged on the same detection
  signal independently. The design transfers; so do five bugs it cost them, now carried as
  constraints on the item.
- **Diagnostics on success, not only on failure**, with an evicted-then-reread correlation.
  This is ADR-0007 extended from exit codes to context economics: the same argument that
  server-captured truth beats the model's account of it.
- **A steer at the risky call.** Shell text-patching gets an advisory note appended to that
  tool call's own result, gated on the write tool actually being available to the agent.
  Their evidence is that a prompt instruction alone did not stop the pattern on retry.
- **An operator-level dispatch transcript**, independent of any caller-facing flag.
- Two smaller constraints: negative caches expire, and a nudge reply concatenates rather
  than overwrites what the model already said.

Rejected, both because this project already went further:

- An env var for the generation ceiling — `config.py` has that field, at the same value.
  Only the precedence ordering transfers, as an M4 constraint: an operator lowering the
  ceiling must not suppress the per-model bump that exists to stop heavy-reasoning models
  returning empty output.
- An explicit override for the wire-format guess, added upstream after a model name matched
  no recognised prefix and sent every dispatch to the wrong endpoint shape. ADR-0009 removed
  prefix matching entirely, so the override has nothing to override. Recorded because a
  rejection that names its reason is what stops the next review re-litigating it.

Also recorded, and worth more than most adoptions: their early-cancellation investigation
closed with no action possible. The client does not surface progress notifications to the
session, and an in-flight synchronous tool call cannot be cancelled. That independently
confirms ADR-0018's narrow claim — those notifications buy an idle-timeout reset and
nothing else — and closes a question this project would otherwise have paid to reopen.

---

## ADR-0023 — 2026-08-26 — The upstream review is a dated file in this repository, not an issue in someone's tracker — Partially superseded by ADR-0025

ADR-0001 requires upstream fixes to be read and reimplemented, and sets a six-to-twelve
month half-life on the exercise. It named no artefact. CONTRIBUTING filled that gap with
"candidates are tracked in a pinned issue", which was true until the repository was
deleted and recreated on 2026-08-25 to unpublish a leaked identifier. The issue went with
it. A policy whose only named artefact lives outside the repository can be destroyed
without a diff, and nobody notices, because there is nothing to notice.

Two further holes were open the whole time. The `upstream` remote had never been fetched
once, so no local ref existed to review against. And it points at the primary ancestor,
which has been dormant since 2026-08-21, rather than at the fork that is actually moving.
The review this ADR mandates had, in practice, no implementation at all.

So: findings land in a dated file under `docs/audits/`, one per review, recording every
upstream change considered and its verdict. The value is not the adopted items — those
become PLAN entries and leave anyway. It is the rejected ones. Without them, the next
review cannot distinguish what was examined and dismissed from what was never looked at,
and re-examines everything or nothing.

Reviewing read-only through the API rather than by fetching is deliberate, and stronger
than the disabled push URL: with no upstream object in the repository there is no
cherry-pick to reach for under time pressure. `docs/audits/*` is already exempt from the
email-content scan, so a review may quote upstream verbatim where that is the evidence.

Not chosen: watching the fork with a scheduled job. It would produce a feed nobody reads
between reviews, and the half-life in ADR-0001 says the correct cadence is occasional and
deliberate. Also not chosen: repointing `upstream` at the fork. The remote records
ancestry, which NOTICE ties licence obligations to; liveness is a property of the review,
not of the remote.

---

## ADR-0022 — 2026-08-25 — Size budgets differ by document class: total lines for mutable, per-entry for append-only — Accepted

Found by hitting the limit. `DECISIONS.md` reached 372 lines against a 400 budget after a
day's work, and the only available response would have been to raise the number — again
next week, and the week after.

That reveals the rule was wrong for the file, not the file wrong for the rule. ADR-0003
justified budgets as a prompt to ask whether content still earns its place. For an
append-only record, it always does: history does not become less true. A total cap on such
a file generates friction and never once produces a useful decision.

So budgets now come in two kinds:

- `BUDGET: n` — total lines, for **mutable** documents. Unchanged, and still blocking with
  the same three resolutions.
- `BUDGET-PER-ENTRY: n` — longest `## ` section, for **append-only** documents. Keeps each
  entry terse, which is the quality actually at risk, while letting the file grow.
- `ARCHIVE-AT: n` — optional, warns rather than blocks, suggesting older entries be split
  into a dated file. Splitting history is fine; trimming it is not.

`CHANGELOG.md` gets an archive threshold and no per-entry cap, because its `## ` sections
are releases that legitimately accumulate entries across a cycle. Its discipline — every
entry carries the why — is a review matter that no line count can enforce.

The mechanism worked as designed, which is the point worth recording: the budget forced a
review, and the review concluded the budget itself was misapplied. A rule that can be
found wrong by its own enforcement is a good rule.

## ADR-0021 — 2026-08-25 — The sandbox argv, corrected against a running kernel — Accepted

Two corrections to the planned bubblewrap invocation, both found by running it rather
than reading it. Recorded as a decision because both are easy to reintroduce and one
produces a false sense of security.

`--symlink usr/lib64 /lib64` is **mandatory** on x86-64, not "if present on the distro"
as planned. Without it the ELF interpreter is absent and nothing dynamically linked runs.
The failure is actively misleading: the kernel returns ENOENT for a missing interpreter,
so bwrap reports "No such file or directory" against the *executable*, which is present
and readable. `--symlink usr/sbin /sbin` for the same reason.

DNS inside the sandbox requires binding the real path of `/etc/resolv.conf`. Under WSL
that file is a symlink to `/mnt/wsl/resolv.conf`, so binding `/etc` binds a dangling
symlink: connections by address succeed while connections by name fail. The sandbox
builder resolves the link and binds its target at its own path whenever network access is
granted.

The consequence for testing is the part worth keeping: **network denial must be verified
by address, never by hostname.** With resolv.conf unbound, a hostname request fails
whether or not the network namespace is genuinely isolated, so a hostname-only test would
report a tight sandbox that might merely have broken name resolution. Only an
address-based attempt is evidence. This is the same shape as the self-defeating checks
already recorded: a test that cannot fail for the reason it claims to test.

Verified on Ubuntu 24.04 under WSL2, bubblewrap 0.9.0: commands run, python3 resolves,
network denied by default with 000 by address, the real HOME absent rather than
read-only, `~/.ssh` absent, a bound workdir writable, and network reachable when
explicitly re-shared.

## ADR-0020 — 2026-08-25 — Topology A confirmed: server in WSL2, workspace on the Windows drive — Accepted

Spike C measured the 9p bridge instead of speculating about it. Same tree, warm caches:
git status ~100x slower, small writes ~200x, venv creation 27x (69s against 2.5s), and
the number that actually matters, a full pytest run, 12x — 7.11s against 0.59s.

The ratios read as alarming and the absolute numbers mostly do not. Five iterations of
the self-verification loop is about 35 seconds of wall clock and **zero** Claude tokens,
against roughly 10-25k tokens for the same work under the no-shell alternative. Cost was
the stated priority for this project, so the trade is clearly worth 7-second test runs.
Venv creation at 69s is the one genuinely poor number, and it is a one-off.

Two alternatives were measured and rejected rather than assumed:

Moving only the *workspace* onto ext4 while keeping Claude Code on Windows does not work.
Windows `CreateProcess` refuses a `\wsl$\` UNC path as a working directory, so any shell
command Claude runs in the repository fails outright without first mapping a drive letter.

Moving development entirely into WSL removes every penalty at once — native test speed
*and* free self-verification, with no path translation — and the measurements make it
materially stronger than it looked during planning. It remains rejected only because it
is a workflow change the user does not want, not because the engineering disfavours it.
That keeps ADR-0002's trigger live: if development moves onto Linux for independent
reasons, this becomes strictly best, and the server code is identical either way.

## ADR-0019 — 2026-08-25 — Denominate the prefetch budget in tokens, not bytes, with measured per-extension ratios — Accepted

Challenged during implementation on two grounds: that a 30% over-estimate was wasteful,
and that 128 KiB is not much of a file when programming. Measuring instead of arguing
showed the second point was right and the first was wrong in an interesting way.

Bytes per token, measured against the model's own tokenizer:

    JSON, punctuation-heavy   1.78      minified python   3.55
    TOML lockfile             2.08      python source     3.89
    markdown prose            3.42      dense-docstring   4.16

So the previous `bytes / 3` estimator did not over-estimate at all for structured data —
it **under**-estimated JSON by 41% and a lockfile by 31%. The claim that it erred in the
safe direction held only for Python, which is the only thing ADR-0015 measured.

The deeper error was the unit. A cap in bytes buys 33K tokens of Python or 72K tokens of
JSON — the same limit means wildly different context and latency depending on file type,
so bytes were only ever a proxy for the thing being rationed.

Therefore: cap on estimated **tokens** (40K per file, 140K per call), estimate via a
per-extension ratio table rounded down from the measurements, and default an unknown
extension to the worst case observed. A byte ceiling survives only as a pre-read guard
so a huge file is never loaded to discover it is huge.

Net effect for code, which is the common case: the per-file allowance rose from 128 KiB
to about 145 KiB of Python, and a full prefetch holds about 506 KiB of it. JSON
correctly tightens to 66 KiB. The limit now adapts to type instead of penalising source
to stay safe for data.

Prefill latency was measured at the same time: 1900-2600 tok/s, so 33K tokens is about
17s and 136K about 56s. The 140K budget is therefore roughly a minute of prefill, paid
once per distinct prefix — which is why ADR-0011's stable prompt ordering matters.

Partially supersedes ADR-0015: the measurement stands, the conclusion drawn from it did
not.

## ADR-0018 — 2026-08-25 — Emit a progress notification every turn, to defeat the stdio idle timeout — Accepted

Claude Code's MCP wall-clock timeout (`MCP_TOOL_TIMEOUT`) defaults to about 28 hours,
so our 3600s `DISPATCH_TIMEOUT` is never the binding limit. The real hazard is the
**idle** timeout: 30 minutes on stdio (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`), which
aborts a call that has sent nothing at all in that window. A 25-turn loop can be
silent for longer — a single max-effort turn measured 82s, and long cold prefills are
slower still.

Progress notifications do **not** extend the wall clock, but they **do** reset the
idle timer. So progress reporting is a correctness requirement, not the optional UX
nicety it was filed as during planning. One notification per turn, carrying turn
number and turn budget.

Consequences: M4 owns this, not a later polish pass. `docs/TROUBLESHOOTING.md` gets
the per-server `timeout` field (milliseconds, minimum 1000) as the documented knob.
Also noted: calls background automatically after 2 minutes, so nearly every
delegation will background — expected, not a fault.

## ADR-0017 — 2026-08-25 — Do not depend on `thinking_token_budget`; the docs named the wrong gate — Accepted

The serving repo's docs say the field is gated by a `DSPARK_ENABLE_ISSUE31_GPU_HOTFIX`
boot flag. The live server disagrees. Its actual 400 response says the field needs
`VLLM_USE_V2_MODEL_RUNNER=0` — a different switch entirely.

Decision unchanged in substance but now correctly grounded: feature-detect the 400,
log once, degrade, never rely on the field. Reasoning is bounded by `max_tokens` plus
the retry-and-step-down guard instead.

Wider lesson, recorded because it will recur: **this stack's documentation and its
running behaviour disagree in places.** Prefer a probe over a doc. The same repo also
ships a maximum-reasoning default in its example environment file while two of its own
docs still recommend the lower setting.

## ADR-0016 — 2026-08-25 — The agentic loop is viable; build M4 to M6 as planned — Accepted

The single largest unverified assumption in the plan was whether this model does
OpenAI-format tool calling well enough to justify the loop at all. Spikes A and B
answered it directly against the live cluster.

Single-call: `finish_reason: "tool_calls"`, one well-formed call, arguments valid
JSON, schema respected. Multi-turn: four turns, each emitting a valid call, correctly
consuming the `tool_result` fed back to it, ending with an accurate synthesis.
Temperature is honoured — two `temperature=0` calls returned byte-identical output —
so the tool-call temperature setting is a live knob and stays.

One behaviour worth recording as encouraging: asked for a *real* exit code, the model
spontaneously reached for shell constructs that capture and echo the exit status
rather than trusting its own reading of the output. It reaches for ground truth on its
own. The server still captures exit codes itself — trust is not the mechanism — but
the model is not fighting us.

One flaw, also recorded: the first turn was a wasted directory listing. That is the
exact cost the `files[]` prefetch and the scope-hint prompt exist to remove, and it is
now measured rather than assumed.

Consequence: M4's ~450 lines are no longer contingent. The fallback of shipping only
the one-shot path is not needed.

## ADR-0015 — 2026-08-25 — Prefetch budget 512 KiB total, 128 KiB per file, from a measured 3.9 bytes per token — Partially superseded by ADR-0019

Measured on real Python source via the server's own tokenize endpoint: 3.64
bytes/token at 10 KB, 3.74 at 50 KB, 3.89 at 123 KB — converging near 3.9 as sample
size grows.

So 512 KiB is roughly 134K tokens, comfortably inside a 1M window alongside the
system prompt, agent body, tool schemas, history and output budget, and small enough
that one request cannot monopolise the shared KV pool.

The admission estimator keeps `bytes / 3`, which over-estimates by roughly 23%
against the measured 3.9. That is deliberate and the safe direction: over-estimating
costs a little idle capacity, under-estimating costs a queued request that times out.

Supersedes the planning-stage figure of 2 MiB, which was wrong — it was paired with a
claim of 160 to 220K tokens that implied about 10 bytes per token, nonsense for source
code. The real figure for 2 MiB would have been around 540K tokens, over half the
context window.

## ADR-0014 — 2026-08-24 — Reproduce and guard the reasoning-exhaustion failure rather than avoiding it — Accepted

At maximum effort with a 512-token budget, a hard prompt returns `content: null` and
`finish_reason: "length"` — reproduced on the live cluster. At 4096 it answers but
still truncates, having spent about 15,600 characters reasoning. Low effort truncated
at 512 too, so the hazard is not exclusive to the top setting.

Therefore: retry once at the larger of double the budget or the configured floor,
without charging the turn budget, then step effort down one level for the remainder,
then fail with an explicit `reasoning_exhausted_budget` rather than an empty answer.
Admission accounting uses the retry's larger size, not the original request's.

## ADR-0013 — 2026-08-24 — All four reasoning levels are supported; the top level is not remapped — Accepted

Collapsing to three levels and remapping the top one down was considered. Rejected:
it saves one enum value, the retry guard is needed for the second-highest level
anyway so the state machine does not shrink, and remapping makes our API disagree with
the backend's own documented values — a caller asking for maximum effort would
silently get something else.

Instead the default is low, and `docs/AGENTS.md` says plainly that the high settings
are rarely right for the bulk mechanical work this tool exists for.

## ADR-0012 — 2026-08-24 — Admission control by token budget, not a flat concurrency cap — Accepted

The per-request context ceiling and the concurrent-sequence ceiling are ceilings, not
reservations; the real constraint is that summed live tokens stay under the KV pool,
measured at about 2.49M tokens. Six simultaneous full-context requests are impossible;
six fifth-size requests fit comfortably.

Oversubscription queues rather than failing, so this protects latency, not
correctness — and it can be severe: large cold prefills serialise behind a
1024-token threshold, giving roughly an 8 tok/s decode floor.

Three rules: total in-flight sequences, summed token estimate against budget, and a
separate cap on concurrent large prefills. That last one is what actually binds for
big tasks, and it is deliberate — the engine permits one in-flight long prefill, so
sending five makes all five slow.

Undersubscription is invisible where oversubscription announces itself, so the status
tool reports high-water marks and admission-wait totals. The constants are then
tunable from evidence instead of guessed.

## ADR-0011 — 2026-08-24 — Prompt order is fixed and the system prompt is static, to preserve prefix caching — Accepted

The cluster enables prefix caching, so identical leading tokens are served from cache
— saving prefill time and leaving more KV pool free. Order is therefore system
prompt, agent body, files block, task last, with the file list sorted
deterministically.

This only pays if the leading tokens are bit-identical, so the system prompt is
**static by construction**: no timestamp, session id, turn number or counter. A single
dynamic byte disables the cache silently, with no error and no symptom beyond slower
prefill. All dynamic content goes in the tail, inside tool results.

## ADR-0010 — 2026-08-24 — Refuse to run shell commands when the sandbox is unavailable — Accepted

Upstream logs a warning and runs the command unconfined. We refuse instead, naming the
fix. A security control that silently degrades to nothing is worse than one that is
absent, because it is believed.

Corollary made explicit because it will otherwise be filed as a bug: `read_file` and
`write_file` are governed by the path policy and never enter the sandbox. Only
`run_bash` is confined. The policy is a sufficient control for calls the server itself
makes, and insufficient only once an arbitrary shell exists.

## ADR-0009 — 2026-08-24 — An explicit model registry replaces prefix matching — Accepted

The serving stack runs one model per inference instance; a second model means a second
container on a second port, and its own base URL. Prefix-matching a model *name*
cannot express that at all.

Upstream keeps five prefix-keyed tables that must stay mutually consistent by hand,
resolved longest-prefix-wins, and its own comments record the resulting near-misses.
One row per model replaces all five with strictly less state, and makes the API format
a field lookup instead of a string heuristic.

Supersedes upstream's routing approach entirely.

## ADR-0008 — 2026-08-24 — Ship the OpenAI adapter only, but keep the Anthropic-shaped canonical format — Accepted

Upstream's internal representation is already Anthropic-shaped (content blocks,
tool-use and tool-result blocks), with OpenAI converted at the edge, and nothing
outside the backend layer knows which wire format is in play.

So: delete the roughly 154 lines of Anthropic wire code, which is unused and untested
here and would rot, but keep the canonical shape and a `Backend` protocol seam. Adding
an Anthropic adapter later is then about 150 to 220 lines in one new file.

Three conditions keep that cheap, and violating any turns it into a refactor: the
canonical shape stays content-block structured and is never flattened to strings; SSE
accumulation lives per adapter behind one contract; model-to-backend selection is a
registry lookup and never a reintroduced prefix function.

## ADR-0007 — 2026-08-24 — The server captures real exit codes, separately from what the model claims — Accepted

Models misreport command outcomes, and upstream ships a dedicated test because of it.
Bash call counts, failure counts and the last exit code are computed by the server from
actual process exits and reported as distinct result fields, with the tool description
telling the model not to contradict them.

The whole self-verification design rests on this. Without it, "the tests pass" is an
assertion rather than a measurement.

## ADR-0006 — 2026-08-24 — Four-layer path policy, allowlist first — Accepted

Workspace roots, then an extension allowlist, then a secret denylist, then gitignore.
A pure allowlist cannot work for file *contents* — you cannot enumerate every source
file you will ever delegate — so the extension allowlist is the practical allowlist,
and the denylist and gitignore are second and third nets for what passes it: an
extensionless key, a local environment file, a committed config full of tokens.

The reference implementation of this feature has no validation whatsoever and will
read a private SSH key on request. Every refusal here returns an actionable message so
the caller can retry with a valid path.

## ADR-0005 — 2026-08-24 — Task shaping lives in agent definition files, not in more MCP tools — Accepted

Five tools total. A new kind of delegated task is a markdown file, not a code change
and a release, and Claude is not shown a tool list that grows without bound. The files
use the same format Claude Code already uses for its own subagents, so they are
portable.

## ADR-0004 — 2026-08-24 — Two documentation planes, split by location, with generated files where facts live in code — Accepted

Project plane at the repo root (where are we, why did we choose this); product plane
under `docs/` (how does it work). A fact may appear in exactly one plane.

Config reference and tool reference are **generated** from the code that defines them,
so they cannot disagree with it. Status is generated from the plan and git. Decisions,
journal and changelog are append-only, so they cannot rot. That leaves only five
documents both mutable and hand-written.

The motivating evidence is upstream, where one setting is documented as one value in
the README, a different value in the configuration reference, and is a third value in
code — and the serving stack, where two docs recommend a default that was changed a
release ago.

## ADR-0003 — 2026-08-24 — Size budgets block, but never delete — Accepted

A hard cap that forces deletion is worse than no cap; a review prompt with no teeth is
ignored. So exceeding a budget blocks, with exactly three resolutions: trim real
redundancy, split for a valid reason, or raise the budget with a one-line
justification in the same commit.

A split needs a genuine reason — different audience, different owned code, or
reference separated from narrative — or it is a budget dodge that produces sprawl.
That is machine-checkable: a new doc whose audience and owned globs are both subsets
of its parent's is refused.

## ADR-0002 — 2026-08-24 — Server in WSL2, Claude Code stays on Windows; topology reviewed after the filesystem benchmark — Accepted

Bubblewrap is Linux-only and Windows has no cheap equivalent — Windows Sandbox is a
disposable desktop VM, AppContainer has no CLI and would mean hand-written Win32
security code, and Docker Desktop uses the WSL2 backend anyway.

Running Claude Code itself inside WSL2 would delete the path-translation module
entirely and was seriously considered; the user chose to keep Claude Code on Windows
in VS Code. A dedicated Linux box beside the cluster was also considered and rejected:
it solves sandboxing but makes the workspace reachable only over a network share, a
sync tool, or a clone — each worse than the local bridge, and each adding an
availability or divergence failure the bridge does not have.

The condition that would flip this: if development ever moves onto Linux for
independent reasons, the native-Linux topology becomes strictly best.

## ADR-0001 — 2026-08-24 — This is a rewrite, not a port, and that changes obligations not at all — Accepted

Two new subsystems, a registry replacing five prefix tables, and every model-facing
string re-authored in English. Calling it a rewrite is honest about the engineering:
upstream fixes get read and reimplemented, not cherry-picked, and the test suite is
ours to own.

It changes the licence position not at all. MIT's condition triggers on copying
substantial portions, not on what the result is called, and we are plainly a
derivative work — a clean-room rewrite stopped being possible the moment upstream's
source was read. Both ancestors are MIT under the same copyright line, verified.
See NOTICE for what came from where.

Review point: the usefulness of watching upstream has a half-life of roughly six to
twelve months. Revisit rather than watching a remote indefinitely.
