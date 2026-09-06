# Plan archive — the milestone roadmap, closed 2026-09-02

M0a through M7 and the work found alongside them. Closed when the last milestone did,
which left `PLAN.md` holding only open work and its length tracking history rather than
anything anyone was going to do. Moved verbatim and never edited; the reasons for each
item are in the annotations, and the provenance is in `CHANGELOG.md`.

`Extra` is here too. It was kept apart from the milestones so a phase's counts meant
what they said -- M1 reading "5 of 8" while three of the five were repository tooling
made the backend work look further along than it was. With the milestones archived there
is nothing left for it to be apart from, and all seven items are done and dated inside
the milestone period.

`check_budgets` skips any path with an `archive` component, so nothing here is budgeted.

`✅` done, dated · `❌` cancelled, with date and **reason**. Fifteen 2026-08-25 items
cite a commit hash; nothing after them does, for the reason `CHANGELOG.md`'s header gives.
Six M4 items carry no date, and it was not recoverable after the fact.

---

## M0a — Spikes

Ran before any scaffolding, so a bad assumption could not become load-bearing.
Throwaway scripts, deliberately not shipped.

- ✅ 2026-08-25 Spike A — tool calling against the live cluster. Valid `tool_calls`,
  schema respected, four-turn loop consuming its own tool results, temperature honoured.
  ADR-0016 — `7650b5d`
- ✅ 2026-08-25 Spike B — tokenizer ratio measured across file types (the numbers live in
  JOURNAL 2026-08-25). Changed the prefetch budget from bytes to tokens. ADR-0019 — `7650b5d`
- ✅ 2026-08-25 Spike C — 9p versus ext4. 12x on a full test run. Topology A confirmed.
  ADR-0020 — `fb9b5a7`
- ✅ 2026-08-25 Spike D — client-side MCP timeouts. Wall clock is ~28h and irrelevant;
  the 30-minute stdio idle timeout is the real hazard. ADR-0018 — `7650b5d`

## M0b — Foundation

- ✅ 2026-08-25 Repo, licence, NOTICE with per-feature provenance, dependency pins — `7650b5d`
- ✅ 2026-08-25 Git identity scoped by directory; company identity untouched — `7650b5d`
- ✅ 2026-08-25 `config.py` — 38 settings, one frozen dataclass, validated at load — `7650b5d`
- ✅ 2026-08-25 `registry.py` — explicit per-model rows replacing five prefix tables — `7650b5d`
- ✅ 2026-08-25 Generated configuration reference, drift-proof both directions — `7650b5d`
- ✅ 2026-08-25 Docs and secrets gate, ten checks, each negative-tested — `7650b5d`
- ✅ 2026-08-25 Pre-commit hook sharing the gate's single implementation — `7650b5d`
- ✅ 2026-08-25 WSL2 Ubuntu 24.04 provisioned; bwrap verified; cluster resolves — `fb9b5a7`
- ✅ 2026-08-25 Budget rules split by document class — ADR-0022 — `fb9b5a7`
- ✅ 2026-08-25 `PLAN.md` and generated `STATUS.md` — `58f543c`
- ✅ 2026-08-25 `README.md` — install, quickstart, what this is and is not
- ✅ 2026-08-25 `CLAUDE.md` — terse invariants and traps for an agent editing this repo
- ✅ 2026-08-25 `CONTRIBUTING.md` — dev setup, the one-feature-per-commit rule
- ✅ 2026-08-25 `docs/ARCHITECTURE.md` — how the pieces fit, and why
- ✅ 2026-08-25 `docs/MODELS.md` — registry format, adding a model
- ✅ 2026-08-25 `docs/AGENTS.md` — agent frontmatter, the path policy in plain terms
- ✅ 2026-08-25 `docs/TROUBLESHOOTING.md` — symptom to cause to fix; WSL paths get a section
- ✅ 2026-08-25 `scripts/docs_ownership.toml` — activates the owning-doc, orphan and split-dodge checks. TOML, not YAML: tomllib is stdlib, so the gate runs from a bare clone
- ✅ 2026-08-25 `scripts/gen_status.py` — STATUS from this file plus git — `58f543c`
- ✅ 2026-08-25 Four build-time agents in `.claude/agents/`, models and effort set per task cost
- ✅ 2026-08-25 CI: gate, tests on 3.11 and 3.12, gitleaks with an allowlist-based email rule
- ✅ 2026-08-25 Audit trigger — the gate raises `audit-due` from git evidence: a document
  unchanged across enough commits touching its owned code, or enough commits since the last
  recorded audit. Both thresholds are constants in `scripts/docs_gate.py`; no document
  restates them. Warns, never blocks
- ✅ 2026-08-25 Public repo created, ruleset applied to `main`, secret scanning and push protection enabled. Direct push to `main` verified as refused

## M1 — One real backend call

- ✅ 2026-08-26 `backends/base.py` — `Backend` protocol, canonical request and response — PR #4
- ✅ 2026-08-26 `backends/openai_compat.py` — the only adapter shipped — PR #4
- ✅ 2026-08-27 `delegate()` one-shot, no `files[]` yet — PR #11
- ✅ 2026-08-27 `backend_status()` probing `/v1/models` per registry entry — PR #11
- ✅ 2026-08-27 Launched through `wsl.exe` from a real Claude Code config, end to end

## M2 — files[] prefetch

- ✅ 2026-08-27 `paths.py` — the four policy layers, each refusal actionable
- ✅ 2026-08-27 `wsl.py` — the Windows-to-POSIX boundary. Added to this milestone rather
  than absorbed silently: `files[]` is produced on Windows and checked in WSL, so without
  it M2 cannot work from a real Claude Code session, which is the bar M1 set
- ✅ 2026-08-27 `context.py` — prefetch, token budgeting, stable prompt ordering for prefix caching
- ✅ 2026-08-27 Binary detection, whole-file skip, per-type token accounting — ADR-0030

## M3 — Response state machine

Scoped down 2026-08-27 to what a one-shot dispatch can actually own. The four
context-economics items moved to M4: they read conversation history, evictions and an
action ledger, none of which existed until the turn loop and `tools.py` did. They are
recorded there, and they shipped there.

- ✅ 2026-08-27 Retry and backoff, honouring `Retry-After` — PR #15
- ✅ 2026-08-27 Empty-answer detection, retry at the floor, effort step-down — PR #15
- ❌ 2026-08-27 Feature-detect the `thinking_token_budget` rejection and degrade.
  **Reason: there is nothing to detect.** The adapter never sends the field, which is the
  strongest form of ADR-0017's "never rely on it", so no 400 ever arrives to feature-detect.
  Re-probed before deciding rather than reasoned about: still refused, still naming
  `VLLM_USE_V2_MODEL_RUNNER=0`, the flag the serving stack's own docs do not mention.
  Reasoning is bounded by `max_tokens` plus the retry-and-step-down guard, exactly as
  ADR-0017 said it would be. The probe also found the error body carries `param: null`, so
  the structural detection this item was going to use would have matched nothing every time
  while a text fallback silently carried the feature — JOURNAL 2026-08-27. Revisit only if
  the endpoint's boot configuration changes; ADR-0017 stands unedited
- ✅ 2026-08-27 Integration test reproducing the empty answer against the live cluster

## M4 — Agentic loop and model tools

- ✅ `tools.py` — read_file, write_file, run_bash; `allowed_tools` enforced at both sites,
  declaration and execution, since a model can call a tool it was never offered. `run_bash`
  refuses every call until `sandbox.py` (ADR-0010). `paths.py` gained `must_exist=False`
  for `write_file`, which creates: it relaxes only the missing-file branch and no other
  layer
- ✅ `docs/TOOLS.md` generator — descriptions rendered from the registered tools. Moved
  here from M0b 2026-08-26: it cannot render a registry that does not exist yet, and the
  descriptions are the model-facing contract, so they arrive with `tools.py` or not at all
- ✅ `loop.py` — turns, eviction, dedup, countdown, final-turn short-circuit. Dedup is
  byte-identical on name and arguments and clears on any side-effecting tool; the
  offset-aware case upstream also misses is recorded as a limitation in DISPATCH.md
  rather than claimed. `delegate()` became agentic by default in the same change, and
  `run_bash` stopped being declared while the sandbox is unbuilt (ADR-0010/ADR-0016)
- ✅ Progress notification per turn — required, not cosmetic (ADR-0018). Injected into
  `loop.py` as a callable, so the dispatch layer still holds no MCP imports
- ✅ `max_tokens` precedence: call argument, then frontmatter, then the per-model bump,
  then the configured default *last*. An operator lowering the ceiling must not suppress
  the bump that stops heavy-reasoning models returning empty output (ADR-0024). Found on
  2026-08-28 that half of this already held -- the floor was a `max()` over the configured
  value from the start, and there is no per-model bump, only `max_tokens_cap` applied last.
  What was missing was the call argument itself, which did not exist. A caller's number is
  honoured rather than raised to the floor, since raising it would make the argument
  advisory; the recovery cascade covers a caller who guesses low
- ✅ 2026-08-29 Context-overflow handling, off by default. Promoted from Deferred 2026-08-26 and moved
  out of M3 2026-08-27, because it consumes per-turn history this milestone produces.
  Retroactive abort when prompt size plateaus while history grows *and* this server evicted
  nothing to explain it — the explanatory variable must be a flag this server set, never
  inferred from the model's account (ADR-0007's spirit), and the plateau needs a small token
  slop or a backend trimming a token between turns reads as truncation. Preventive graduated
  response at 70/85/95% of projected usage — tighten retention, wrap-up nudge, hard abort.
  On abort, a state report reconciling the model's ledger against `git status` ground truth
- ✅ 2026-08-29 Negative tests for the bugs that cost upstream, three of one shape — a threshold
  computed against the wrong denominator: firing on this server's own evictions; a band
  firing on ordinary growth; a flat reserve alone exceeding 95% of a small window; a probe
  reading the wrong window. The denominator is `ModelEntry.context_window` and nothing
  else — not `thinking_max_tokens_floor`, which is a reply budget, and not the dataclass's
  own 131072 fallback that an entry omitting the field inherits silently, which is the
  realistic local form of upstream's "architecture maximum" bug. The reserve must be a
  fraction of the window rather than a flat count, which closes that bug class instead of
  patching one instance. Upstream's fifth test — a local-only gate also blocking the
  explicit override on cloud backends — was **evaluated 2026-08-27 and is not applicable**:
  ADR-0008 ships no cloud backends and `context_window` is always the operator-set value,
  so there is no two-branch gate to break, and a synthetic two-tier fixture would pass
  whether or not the code had the flaw. Its lesson folds into the wrong-denominator test:
  assert only one branch decides which number is the window
- ✅ 2026-08-29 Diagnostics opt-in per call — action ledger on success as well as failure, per-turn
  token and eviction breakdown, and an evicted-then-reread correlation, which is what
  separates a genuinely expensive dispatch from one re-reading what it lost. ADR-0007
  extended from exit codes to context economics, and a prerequisite for sizing eviction
- ✅ 2026-08-29 Two constraints from upstream's bugs: a negative cache expires, or one transient
  backend outage disables overflow handling until restart — and a transport failure while
  probing must never populate it, only a confirmed refusal; a nudge reply concatenates and
  never overwrites what the model already said
- ✅ Enforce `dispatch_timeout`, which was declared and consumed nowhere. M3's retry waits
  and the empty-answer stages were bounded only by their own counters, and an exhausted
  max-effort delegation measured at tens of minutes (JOURNAL 2026-08-27). One deadline is
  taken at the top of `run_one_shot` and shared by every stage, checked before each attempt,
  applied as a ceiling on it, and checked against each backoff wait. Corrected while
  landing it: this does **not** keep a delegation inside the client's idle timeout, the
  default being 3600s against 1800s — only the progress notification above does that

## M5 — Sandbox

- ✅ 2026-08-29 `sandbox.py` — empty root, the corrected symlink set, bind-order rules
- ✅ 2026-08-29 Toolchain binds so `uv` resolves inside the sandbox. `uv` is not installed in this WSL, so the probe is proven against a patched `which` and the absent case is the one proven for real
- ✅ 2026-08-29 Refuse to run when bwrap is absent — and `sandbox_enabled` deleted, so no setting runs a shell unconfined either (ADR-0034)
- ✅ 2026-08-29 Real exit codes captured by the server, reported apart from the model's
  claims (ADR-0007). Reachable without a mock while `run_bash` is still withheld, because
  `execute_tool` takes its allowed set as a parameter and never consults the withholding
- ✅ 2026-08-29 Secret denylist enforced at the mount level (ADR-0035)
- ✅ 2026-08-29 Tests: bind order for all three HOME/workdir cases; denial verified **by address**. Both proven to fail against a real violation, not merely to pass
- ✅ 2026-08-29 Test that the sandbox dies with its parent — the argv assertion proved the
  flag was passed, not that anything acts on it. Proven to fail: stripped, it survives
- ✅ 2026-08-29 Steer shell text-patching toward `write_file` — a note appended to that `run_bash`
  call's own result, not the system prompt, because upstream found a prompt instruction did
  not stop the pattern on retry. Advisory, never blocking, and only when the resolved tool
  set actually includes `write_file` — the resolved set the executor enforces, not the
  declared list (ADR-0024)
- ✅ 2026-08-29 Un-withhold `run_bash` — `WITHHELD_TOOL_NAMES` emptied and the description
  reworded in the same commit, so the tool is never offered while telling the model not to
  use it. Proven end to end through the MCP surface, the one route that could not be
  exercised before: a scripted model claims success after `exit 3` and the ledger says 3

## M6 — Agents, batching, discovery

- ✅ 2026-08-30 `agents.py` — three-tier lookup, frontmatter validated and actually binding.
  Parsed by hand rather than by adding a YAML dependency: the field set is fixed and known,
  and what a hand parser cannot read it refuses instead of guessing. An unknown key, a
  misspelt `effort` and a `name:` disagreeing with the filename are all refused, because the
  ancestor bug this format exists to avoid was frontmatter loaded and then ignored. An
  over-cap `max_turns` is refused here where a caller's is clamped — a file is committed and
  read again, so a silent clamp would leave the wrong number in it forever. `Delegation`
  gained the agent body and a `render` that both prompt-assembly sites now call: the
  docstring claimed the ordering rule lived in one place while two sites concatenated it
  themselves, and the body would have been a third segment to keep in step across both
- ✅ 2026-08-30 `delegate_to_agent`, `delegate_batch`, `list_agents`. All three resolve and
  then reuse `run_delegation`, the seam `delegate` already went through, so precedence lives
  in one place rather than once per tool. `delegate_batch` shares one agent and one
  `files[]` across many tasks, which is the shape ADR-0011's prompt order was already
  describing: everything up to the task is identical, so the cluster serves it from cache.
  Bounded by the endpoint's own `concurrency`, declared in the registry since M1 and
  enforced here for the first time because nothing until now ran two requests at once. One
  failing item does not fail the batch (ADR-0037). The workdir is root-checked **before** it
  is used to look an agent up, since the lookup reads `<workdir>/.claude/agents/` — caught
  by a test asserting the refusal, which instead reported the agent as missing
- ✅ 2026-08-30 Workdir root allowlist, symlink escape closed. `resolve_workdir` checks the
  argument against `workdir_roots`, resolving before comparing so a symlink inside a root
  pointing out of it is refused on where it lands. `network` and `extra_binds` stopped being
  hardcoded in the same commit, since they are the same three lines. `run_bash_timeout`
  raised 120s → 600s: the figures this item quoted were wrong, and re-measuring gave 281s
  for a serial WSL run, so 120s sat below the median legitimate command rather than above
  the slowest. A kill reports a non-zero exit, and a model then reasons about it as a test
  failure and repairs passing code, which corrupts the ground truth ADR-0007 rests on.
  `extra_binds` is now scanned for secrets (ADR-0036): the exclusion was justified by the
  value being an operator's choice, and an agent file choosing it ended that

- ✅ 2026-08-30 Declaration must ask whether the sandbox can run, not remember that it
  could. M5 emptied `WITHHELD_TOOL_NAMES`, and `available_tool_names()` takes no `Config`,
  so on a host without bubblewrap `run_bash` was declared and refused every call — a turn
  spent learning what the server already knew (JOURNAL 2026-08-29). Fixed where the tool
  set is resolved: `available_tool_names` and `resolve_allowed` now take a `Config` and ask
  `sandbox.available`. `WITHHELD_TOOL_NAMES` stays empty, because whether a host has bwrap
  is not something an import-time constant can answer. Found by the suite rather than by
  the change: an end-to-end server test asserted all three tools were offered, and it was
  passing on Windows only because nothing consulted the host

## M7 — Admission control and polish

- ✅ 2026-08-30 Token-budget admission — four rules, high-water marks and wait totals.
  `admission.py`, one condition variable over plain counters checked as a single atomic
  predicate; ADR-0038 records why that rather than semaphores acquired in turn. The endpoint's own
  `concurrency` became the fourth rule, replacing the semaphore local to `delegate_batch`
  that bounded a batch against itself and nothing else — a single `delegate` was never
  checked against it at all, while `max_inflight_seqs`' own description already said both
  were checked. A request is sized by two numbers, its KV footprint and its prefill, after
  classifying on the total was found to make every delegation "large" and silently bound
  the server at `max_inflight_large_prefills`; caught by the batch test still passing with
  the endpoint rule deleted. The wait has its own timeout, because `dispatch_timeout`'s
  deadline is set inside the loop it bounds and does not start until a slot is already
  granted (ADR-0038)
- ✅ 2026-08-30 Cross-process slots — the four rules now count the machine rather than
  the session. `slots.py`, a per-process record in one `flock`ed file on tmpfs that every
  server shares. The gap was not exotic: stdio starts a server per registration, so two
  editor windows on two projects were two gates with independently zeroed counters against
  one KV pool, and the configured ceiling was multiplied by however many windows were open
  — `admission.py`'s own docstring called itself the global budget "by construction" while
  that was true only within a process. The predicate is evaluated *inside* the lock, since
  reading totals and deciding afterwards lets two processes see the same room and both take
  it. Records are keyed by `(pid, start_time)` and reclaimed on liveness, so a `kill -9`d
  window leaks nothing and a recycled PID inherits nothing. Tested with two real processes
  and negative-tested against a build with sharing removed; ADR-0040 carries the rejected
  alternative and why it was rejected
- ✅ 2026-08-30 Operator-level dispatch transcript to disk, independent of any caller-facing
  flag and stripped from the response. Both upstream bugs are defended by structure rather
  than by care: the record is assembled from identity captured before the attempt and
  written from a `finally`, because the agent name is in scope only at the top of a
  delegation; and the writer returns nothing, so the response dict has no value to pick up.
  Each was reintroduced to confirm its tests fail. A configured transcript turns per-turn
  recording on for itself, since the loop keeps those records only when told to and one
  reading the caller's flag would be empty for nearly every delegation. Records hold paths,
  accounting, the task and real token usage — never file contents, which are recoverable by
  path and are all the bulk (ADR-0039)
- ✅ 2026-08-30 README launcher documentation — verified rather than rewritten. There is
  no launcher script to document: the launcher is the `claude-delegate-local-mcp` console
  script from `pyproject.toml`, invoked by the client directly on Linux or through
  `wsl.exe --cd ... -e ...` on Windows, and README already carries both registration
  snippets, the note that `--cd` and the absolute path are load-bearing, and the link to
  TROUBLESHOOTING. Checked against `pyproject.toml` and `main.py`; they agree. Closed
  without a change because writing one would have put a second copy of facts that already
  have a home, which is what ADR-0004 exists to stop
- ❌ 2026-08-27 `scripts/release.py` — cancelled. Its job was to backfill each CHANGELOG
  entry's commit hash at tag time, and the hash is the wrong thing to cite: `main` is
  squash-merged, so a branch hash names an object that never leaves the clone it was made
  in. The PR number survives and is written by hand in the same edit as the entry, so
  there is nothing left to automate. CHANGELOG.md's format line described this script in
  the present tense while it did not exist -- found by the 2026-08-27 audit

## Extra — work outside the milestone plan

Found while doing something else, and fixed rather than filed. Kept apart from the
milestones so a phase's counts mean what they say: M1 reading "5 of 8" while three of the
five were repository tooling made the backend work look further along than it was.

- ✅ 2026-08-26 `DELEGATE_CONNECT_TIMEOUT` — a dropped route stalled for the whole turn
  timeout; connect is now bound separately — `#5`
- ✅ 2026-08-26 The docs gate read the previous commit's message — moved to a commit-msg
  hook, so stale waivers no longer carry between commits — `#6`
- ✅ 2026-08-26 `.env` was documented but never read — `config.load()` reads it, ADR-0027
  for why an MCP client's `env` key cannot reach the server — `#7`
- ✅ 2026-08-26 A skipped live test read as a passing one — the skip now says the backend
  is unproven and names the layer that stopped it — `#8`
- ✅ 2026-08-26 ruff was configured and never ran; the rule set was drifting with the
  installed version. Pinned, fixed, and required in CI — `#8`
- ✅ 2026-08-26 `git commit --amend` was judged against the wrong parent — both readings
  are evaluated and a pass that relied on the amend reading announces itself (ADR-0028) — `#9`
- ✅ 2026-08-30 `run_bash` was refused on every real project — the mount-level secret scan
  walks the workdir before each call and gives up past `secret_shadow_max_entries`, and the
  default was measured on a checkout with no virtualenv in it. `config.py` said so itself:
  "This repository scans in 230." With `.venv` present it walks 10,586, in 66 seconds, per
  call. Fixed with a second list of machine-generated directories, covered with the tmpfs a
  matched secret directory already gets and pruned from the walk — 248 entries and 0.7s.
  Covering is what makes skipping safe, and the wrong build was demonstrated: pruned without
  covering, a real sandboxed shell reads a secret placed inside. Raising the budget was
  rejected as worse than slow — inside a virtualenv `*secret*` and `*credential*` match
  library source, so the scan would mount `/dev/null` over the imports of the environment it
  had just spent a minute reading. Found by running the tool, not the suite: every scan test
  built twelve files by hand (ADR-0041)

## Completed work moved from PLAN.md on 2026-09-06

Twenty-eight ticked items and the seven struck originals that travel with them,
moved verbatim and never edited. PLAN.md kept these as a record of recent work until
it stopped being recent: they were 36% of that document, filed under a heading that
said Open. The reason for each is in its own annotation; the provenance is in
CHANGELOG.md.

- ✅ 2026-09-06 The reply budget is derived from the deadline and a measured decode rate
  (`#113`, ADR-0055) — **"`backend_status` is the reader" was wrong**, and that is the half
  worth keeping. `probe_cluster` scraped seven metrics and not one was a rate, and
  `read_metrics` refused histograms by *documented decision* — so this had to argue past a
  refusal rather than read a number. The exception it earned is narrow and named after the
  `prefix_cache_hit_rate_since_boot` precedent that made the shape defensible; without that
  precedent the right answer would have been to leave the module alone. Three things the
  filing could not have known. The rate cannot be learned from experience alone: a one-shot
  completes no turns, and the tools-withdrawn final turn is a first turn for this purpose,
  so an estimator with no seed leaves uncapped exactly the two shapes that die — and the
  seed is the since-boot mean the module declined to *report*, which turns out to be a
  different act from consuming it. The ceiling had to bind an explicit `max_tokens` where
  ADR-0014's floor deliberately does not, because a floor is a preference and a deadline is
  not. And three `test_server.py` doubles pop one canned reply per request whatever the URL,
  so one extra GET ate the first turn's answer and they reported a *missing turn* rather
  than an extra request — found by the full suite, not by the files the change touched.
  Original entry follows.
- ✅ 2026-09-06 A delegation reports what the whole run cost, beside what the answering turn
  cost (`#116`, ADR-0058) — raised by reading the picker's "saved" column and not by a filed
  item. **Not the bug it looked like:** `saved_of`'s two branches agree, and the result dict
  reporting the answering attempt is documented behaviour that predates all of this. The
  defect is that the *transcript* uses the same three field names for lifetime sums, so one
  delegation honestly reported `cached_tokens: 0` while its own record said 559,872. Fixed
  additively rather than by renaming, because the two figures answer different questions.
  The second half is a presentation bug with the same root: a cumulative "saved" total grows
  fastest exactly when reuse is worst, so it was the one rendering that could not show the
  eviction bug it was measuring. It reads as a share now — 62% and 33% on the two runs that
  prompted this, both poor for an append-only history. Closes the measurement half of
  [[token-savings-report-goal]]: totalling what delegation saves is possible from tool
  results alone, which it was not before
- ✅ 2026-09-06 The final turn forbids tool calls instead of withdrawing the tools (`#115`,
  ADR-0057) — **never filed, and found by measuring rather than by reading.** Looking at why
  turn 12 of two delegations cached zero while turns 2-11 cached fine turned up a second,
  independent cache bug sitting on top of the eviction one: `tools=()` on the last turn moves
  the first difference to the front of the prompt. Corpus evidence was 3 of 7 runs and
  confounded with the eviction collapse, so it was carried as a hypothesis and settled with a
  four-arm probe — 36,018 tokens re-prefilled to avoid sending 321, with a repeat arm proving
  the cache was still warm. The probe was also allowed to *stop* the fix: if the chat template
  had dropped the tool block under `tool_choice: "none"` the divergence would have returned
  unchanged, and that is a fact about the server's template rather than one the spec implies.
  Moving the tools to the end of the prompt was the other candidate and is worse — it trades
  the cross-delegation `system prompt + tools` prefix, which every run shares, for a
  once-per-run saving, and gives up native tool-call parsing to do it
- ✅ 2026-09-06 Eviction carries its boundary instead of recomputing it every turn (`#114`,
  ADR-0056) — **the three candidate directions were not alternatives**, which is what
  "measure before choosing" turned up and no amount of reading would have. Modelled over the
  alternation the loop appends: the per-turn boundary reuses 2.9% of each prompt, gating on
  projected share alone reaches 5.1%, gating *plus* a stepped boundary reaches 79.2%, and an
  unbounded history reaches 93.0%. So the stickiness is the load-bearing half and the gate is
  what makes the common case free — filed as an either/or, delivered as both. Two things the
  filing could not have known. **`context_overflow_enabled` is off by default**, so the
  obvious arrangement — gate the whole policy on the flag — would have left the default
  configuration bounding nothing at all, trading a cache bug for an unbounded history; the
  stepping is therefore unconditional and only the holding is gated. And that was caught by a
  test asserting a stub had actually been produced, which failed `0 > 0` while the pressure
  test beside it passed by evicting nothing — the fourth-and-fifth-check problem, found in
  the act. `docs/DISPATCH.md` had also asserted the opposite of the fix, that no arrangement
  of the tail could be cache-stable. Original entry follows.
- ~~Eviction rewrites the history mid-stream, and the prefix cache pays every turn~~ —
  `evict_stale_tool_results` stubs the oldest surviving tool result *inside* the history,
  one per turn; the stack caches **prefixes**, so the divergence point moves toward the
  front and everything after it is recomputed. JOURNAL 2026-09-05 had already priced this
  lever at 30x and concluded "cache first, concurrency second"; nothing had checked whether
  the loop was pulling it.
  - **The policy is right and the accounting is wrong.** The saving is denominated in tokens
    *sent*; the cluster charges for tokens it cannot *reuse*. It also fires at ~7% of a 1M
    window, where there is nothing to relieve — `_OverflowGuard.__init__` sets `self.keep`
    outside the `armed` check and the loop reads that attribute directly, so the guard
    documented as "entirely inert unless `context_overflow_enabled`" is inert in its methods
    and live in the one thing that costs.
  - **Second-order cost, instrumented and invisible:** `evicted_then_reread` caught the
    model re-reading what eviction had just dropped, three turns running. The dropped
    content returns as a fresh full-size result, which evicts the next one.
  - Candidate directions, unranked: gate `keep` on projected share; evict in batches so one
    rewrite amortises; or evict from the front once and never re-touch. **Measure before
    choosing** — the regression test compares prefix stability without a cluster, and the
    live check is `cached_tokens` in the transcript

- ✅ 2026-09-05 Operator allowlist for an agent's `network` and `extra_binds` (`#110`,
  ADR-0053) — `workdir_roots` was the pattern to copy for the containment and the wrong
  pattern for the rest. Three things the filing could not have known. The two fields want
  **different** gates: a bind pins to a root and `--share-net` has nothing to pin to, so
  egress needs the agent named *and* the file in `agents_dir` — a name alone is forgeable,
  since the workspace tiers are searched first and a repository can ship a file under any
  name. The allowlist would have been **decorative** without a fix underneath it: the bind
  was emitted as the frontmatter string, so a link inside an approved root was resolved by
  the kernel at mount time having never met the check. And the reserved-mount half taught
  the most: reordering the argv so nothing can shadow `/usr` also wipes every bind inside
  `/tmp`, where every temporary directory lives — an existing integration test caught it in
  one run, so the narrow case is refused instead, and four of the nine reserved targets are
  symlinks into `usr/` that are correctly *allowed*. One enforcement site, not two:
  `tools.py` merges agent binds with the operator's `toolchain_binds` into one untagged
  tuple, so nothing downstream can tell whose bind it is. Original entry follows.
- ~~Operator allowlist for `network` and `extra_binds`~~ — the only validation is
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
- ~~Validate the opened inode, not the path~~ — `resolve_all` then `open` is check-then-use
  in both file tools and in prefetch, with no re-validation. The adversary is the delegated
  model itself, which holds a read-write workdir bind under `run_bash` and can retry, so
  this is not the passive window the review described. `O_NOFOLLOW` is the wrong fix: it
  would refuse legitimate symlinked checkouts, since `realpath` has already collapsed them
  by design. Compare `realpath("/proc/self/fd/N")` against the roots after opening
- ✅ 2026-09-05 Resource limits inside the sandbox (`#111`, ADR-0054) — **"emits no
  `--rlimit`" presumed a flag bwrap does not have.** bubblewrap 0.9.0 has none, so the caps
  come from a `prlimit` launcher in front of it; `resource` is still not imported, and
  should not be, since `preexec_fn` is a deadlock risk once tool calls run in threads. Three
  things the filing could not have known. `RLIMIT_AS` is address space, not resident memory,
  so it over-counts and a Go runtime will trip it — the accurate control is a cgroup memory
  limit, and `systemd-run --user` has no bus here, though the `pids` controller is present
  and would be the better process cap if it were reachable. The process cap ships
  compromised on purpose: counted per uid so concurrent delegations share it, unusable below
  ~64 because bwrap cannot then make its namespaces, and silent when it binds because the
  shell cannot fork to report it. And measuring it took three attempts — a sequential loop
  never exceeded two concurrent, then a loop counting iterations rather than successes
  reported 400 under a cap of 256, because `cmd &` succeeds whether or not the fork did.
  Two plausible experiments agreed and were both wrong. Mutation testing then caught a
  fifth check that could not fail: an integration test asserting no core file is left
  behind passed with `--core=0` removed. Original entry follows.
- ~~Resource limits inside the sandbox~~ — `build_argv` emits no `--rlimit`, no process cap
  and no `--size` on either tmpfs, and the module never imports `resource`. A fork bomb or
  a runaway allocation is bounded only by `run_bash_timeout` and `--die-with-parent`. This
  machine's page file is capped by choice, so a demand-side OOM is the live failure mode
  rather than a theoretical one
- ✅ 2026-09-04 Escaped a file's own boundary markers in the files block — matched by
  shape and for **any** path, not the entry's own: a forged `BEGIN FILE` naming a file the
  server never opened was the case the item did not name, and the worse one. Neutralised
  rather than dropped, because a review delegation reads source and `context.py` quotes
  those markers itself. The tool-level policy remains the real defence (`#96`)
- ✅ 2026-09-04 Refused a non-stdio transport outright, and deleted the port only it used
  — the field is **kept** rather than deleted, diverging from ADR-0034's remedy for a
  measured reason: `load` reads only variables matching a field, so deleting it would turn
  a configuration error into silence. `sandbox_enabled` differed in that the value being
  ignored was the one the operator wanted anyway. No token was the true half of the review
  finding; the loopback default made the rest of it moot (`#97`)
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
- ✅ 2026-09-04 Admission queues in order, and the wait timeout went 600s to 1800s with it
  (`#98`) — tickets in the same shared file the counters use, dropped from a `finally` so a
  timeout, a cancellation or any raise gives the place back. Two things the item did not
  foresee. A waiter counts as ahead of you only if it could be admitted **now**: strict
  ticket order reintroduces the head-of-line blocking the single predicate exists to
  prevent, and the existing large-prefill test failed against a first attempt that used it.
  And the schema is additive rather than versioned — resetting the file would zero the slots
  an older process on the machine still holds, which `/mcp` Reconnect makes normal. The
  docstring is corrected, here and in ARCHITECTURE.md. Ordering decides who goes next, not
  how fast anyone finds out: cross-process waiting is still polling
- ✅ 2026-09-03 A stall deadline, and a re-derived ceiling (#85, ADR-0047) — `stall_timeout`
  at 2100 does the killing; `dispatch_timeout` rose to 14400 and is now only a ceiling. The
  two had to land together: raising a bound that cannot see progress makes a stall more
  expensive for everyone queued behind it. The signal is turn *completion* — the per-turn
  notification fires at the top of a turn and the keepalive is a timer, so both would have
  reset the clock on the very turn that wedged. A one-shot completes no turns, so its
  deadline runs from entry and its bound becomes the tighter of the two. The lower bound was
  written strict and was wrong, since `turn_timeout == dispatch_timeout` is permitted here

- ✅ 2026-09-03 Said in `docs/DISPATCH.md` that the admission wait stacks on the dispatch
  deadline (#85) — folded into the deadline section while it was open for the stall work,
  which is the owning document being made correct about code that changed rather than a
  separate errand. Original entry follows.
- ~~Say in `docs/DISPATCH.md` that the admission wait stacks on the dispatch deadline~~ —
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

- ✅ 2026-09-04 A stalled delegation says what it had managed (`#102`) — turns, tool calls
  and the last tool, so a wedged task shows work behind it where a dead endpoint shows zero
  of both. **"Reachable without new plumbing" was wrong**: nothing on the error path could
  see the counters, since `_Watch` is a local of the loop and `AgenticDispatch` is built only
  on success. One handler wrapping the loop covers all five raise sites. Absent stays
  distinct from zero, so a one-shot's message is unchanged. The find was elsewhere: the
  template's literal "while" had been rendering "while with no turn completed" all along
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
- ✅ 2026-09-03 `search_files`, and a read-only delegation that can use it (#86, ADR-0048) —
  `delegate_readonly` now offers `read_file` and `search_files`, and so runs the loop. The
  read-only set is **derived** from a `writes` declaration on each tool rather than listed,
  which is the half worth keeping: injecting the mistake it exists to catch — a writing tool
  that forgets to declare it — fails four tests including the one asserting the executor
  refuses a write. `paths.py` gained a second *disposition* over one policy, not a second
  policy: `resolve_permitted` drops what fails, and is only for paths nobody named. Widest
  blast radius of the session, eight existing tests across four files
- ✅ 2026-09-04 `read_git`, a read-only git tool in the server process (`#99`) — the
  denylist entry stays and the sandbox is untouched. Two things the item's shape got wrong.
  Path *arguments* cannot go through the path policy: it validates a path that exists now,
  and history is about files that were deleted, so they are checked for not leaving the
  repository instead. And the repository needs validating **twice** — `-C` makes git
  discover a repo by walking up, so a validated directory can resolve to one above the
  root. `open_resolved`'s guarantee is unavailable here by construction and the CHANGELOG
  says so. Two usability gaps came from running it, not reading it: `git log -1` and
  `rev-list --count` were both refused, and the model reached for both first
- ✅ 2026-09-03 Linked an agent body to the limitations it encodes (#88) — the
  `agent-capability` gate check. The sandbox half is derived from the denylist rather than
  hardcoded, and the two agent formats are separated because only a server-format agent's
  shell is confined. **The narrow case only**, and the limit is stated in the check and in
  CONTRIBUTING.md: the motivating case, `python3 scripts/docs_gate.py`, needs git
  *indirectly* and is invisible from the text. Writing its meta-test found more than the
  check did — a mislabelled finding name, and three gate checks with no negative test at
  all. Original entry follows.
- ~~Link an agent body to the server limitations it encodes~~ — second sighting of one class.
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
- ✅ 2026-09-03 `edit_file`, addressing text rather than lines (#94, ADR-0050) — the
  ordering held: #93 landed first, so this tool holds one `"r+b"` descriptor for the whole
  read-modify-write and there was never a second `open` to make safe. The shape changed from
  what was filed. Line addressing had made a line range look obvious and it is the worse
  half of the choice, because a stale line number overwrites a different region silently
  while a stale quotation cannot — so `old_string` must match exactly once, and zero or two
  matches are refusals that leave the file byte-identical. The find was elsewhere: three
  documents each kept their own prose list of the tools the local model gets, none of them
  the document that owns `tools.py`, and one addition made all three wrong at once

- ✅ 2026-09-06 An audit pass is sized to one reply and split by check class (`#117`) —
  **both sessions were partly right and the disagreement was an ambiguity, not a conflict.**
  Session 1 saw decomposed passes survive and concluded the prefetch was at fault; session 2
  proved the budget was at fault and exonerated the prefetch. Neither noticed that the body's
  sentence fused two independent claims — prefetch everything, *and* answer in one turn — so
  correcting the second read as abandoning the first. A pass can be fully prefetched and
  small. **What the filing could not have known:** splitting by *document*, the obvious
  decomposition, silently disables four of the seven checks. WRONG DOCUMENT and CROSS-PLANE
  LEAK need every document that could hold the restatement, and MISSING cannot establish an
  absence from a subset — a pass that cannot see the other copy reports nothing and looks
  clean. So the split is by check class first. The concurrency argument holds and is now
  measured rather than asserted: 60% of backend time across 42 runs is decode, rising once
  eviction stops forcing re-prefill, and decode is the half fanning out parallelises —
  bounded by that 60%, not the 2.8x aggregate figure. ~~and less roughly 120s of stagger per
  extra call.~~ **The stagger was wrong and is corrected in `#118`**: calls issued in one
  message start seconds apart, so fanning out costs essentially nothing to start. Original entry follows.
- ~~`docs-audit-local` assumes one big prefetched pass, and that shape now stalls~~ —
  its body says a prefetched audit finishes "in one turn with zero tool calls", measured
  2026-09-03. The served model swapped to a vision model on 2026-09-04 and the measurement
  was not re-taken. On 2026-09-05 a twelve-document, seven-class call died at 2100s with
  **0 turns and 0 tool calls**, and two of four smaller passes went the same way — while
  the two that called tools completed 3 and 9 turns and finished.
  - **The predictor is whether tools sit on the critical path, not whether any are called.**
    A tool call ends a turn and resets the stall clock, so a pass with nothing to call must
    fit its whole answer inside one turn and ADR-0047's deadline becomes a wall clock. But
    *mandating* a call does not fix it: a forced `stat()` closed turn 1 in 12 seconds and
    the model then spent 1068 seconds in one silent turn, dying anyway. The passes that
    survive are the ones that cannot proceed without reading the next thing — 12 and 21
    turns, none longer than 282 seconds. ~~**What is not yet isolated is which change earns
    that**, and the comparison run varied three things at once: it dropped the prefetch,
    read in `start_line` pieces, *and* was told to emit each document's findings before
    reading the next, where the run it replaced was told to withhold everything until the
    end. Incremental output is at least as plausible a cause as incremental reading, and
    the two imply opposite advice about prefetching — one of them keeps a real optimisation,
    the other throws it away. Isolate it before rewriting the body.~~
  - **RESOLVED 2026-09-05 (session 2): neither, and no prompt change is the fix.** The
    struck paragraph hunts a prompt property; the cause is the reply budget — see
    "`max_tokens` is set to a budget no deadline can pay". A tool call helps only because it
    *ends a turn*, which is why "on the critical path" predicted survival while a forced
    `stat()` did not: it ended turn 1 and left turn 2 holding the same budget, and its 1068
    silent seconds are ~30,000 tokens of decode, not a wedge. **Prefetching is exonerated.**
    Fix the budget and re-take the measurement rather than rewriting the body
  - ~~**The 2026-09-03 optimisation is what now kills it.**~~ **It is the turn count, not
    the prefetch** (session 2). "Prefetched, one turn, zero tool calls, four findings" was a
    real measurement and a real improvement; what became fatal is the *one turn with zero
    tool calls*, which must fit a whole audit inside one deadline. The prefetch is what made
    that turn cheap and is worth keeping. Re-take the measurement against the model actually
    serving once the budget is fixed, and say in the body that the answer is model-dependent
    rather than settled.
  - Also correct two things the split exposed: `read_git` is available now and is how
    MISSING and ESCAPE ABUSE get done, and waiver counting must be line-anchored on
    `Docs-Gate-Skip:` — matching the substring counted a commit whose prose *described* a
    past waiver, over-reporting one document as being at the threshold.
