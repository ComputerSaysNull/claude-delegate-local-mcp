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

## Completed work moved from PLAN.md on 2026-09-07

Four ticked items, no struck originals, moved verbatim and never edited. Archived to
make room for the milestone structure PLAN.md took on in the same commit: the file was
at its 300-line budget, and its own rule is that a completed item leaves at the end of
the session that finished it. The reason for each is in its own annotation; the
provenance is in CHANGELOG.md. The `### Documentation accuracy` heading went with the
two items under it, which were its only content.

- ✅ 2026-09-07 TOO VERBOSE across `docs/ARCHITECTURE.md` and `docs/DISPATCH.md` — the one
  check class the 2026-09-06 audit did not run at all. Five duplications trimmed, both
  documents dense rather than padded; what the passes measured is in the #127 entry
- ✅ 2026-09-07 The gate now checks references, which both audit agent files had claimed
  for it since they were written. `doc-reference`, negative-tested in both directions
- ✅ 2026-09-06 A read-only form of `delegate_to_agent` — the agent tool with its set fixed
  to whatever declares no write, exactly as `delegate_readonly` is to `delegate`.
  Shipped as `delegate_to_agent_readonly` (ADR-0059), and it took the `workdir`/`project`
  split with it: the correspondence the annotation rests on — a workdir is a read-write
  bind, so a read-only tool cannot offer one — was prose, and `list_agents` had already
  falsified it. It is a test now. **The ramp is measured**: two arms issued in one message
  started 2.2s apart, against the 120s a chained tool predicts (JOURNAL 2026-09-06). The justification is
  ADR-0042's and unchanged: a client decides before the call runs and never sees arguments,
  so narrowing with `allowed_tools` cannot buy the declaration. `#91`'s note that "what
  `delegate_readonly` has no equivalent of is the agent" is this same gap seen from the
  other side, recorded there as context for a finished item rather than as work.
  **What makes it worth doing now is measured.** A `readOnlyHint` tool runs on independent
  clocks while `delegate_to_agent` is released one arm per 120s, so the 2026-09-06 audit
  paid 120s x (n-1) of pure client ramp on passes that were `read_file`-only anyway — every
  one after its second wave. It would also keep the agent file, whose accumulated
  false-positive guardrails are most of that agent's value and which `delegate_readonly`
  cannot carry. Two caveats to state rather than discover: the annotation's causation is
  correlated and not proven, testable with two calls timed against their own issue stamps;
  and it buys nothing against admission, which is server-side and starves a read-only
  fan-out identically on `max_inflight_large_prefills`
- ✅ 2026-09-07 **A transcript says a tool errored, never what it was asked or why it refused** — the
  ledger is `{name, outcome}` per call, so a pass reporting `tool_errors: 1` across twelve
  `read_git` calls on 2026-09-05 could not be diagnosed *even with transcripts enabled*.
  Searching the whole record for refusal text returns nothing. This is ADR-0039's own
  argument applied to half the record: it reasons that a transcript which "cannot say what
  was asked does not answer the question a transcript is opened to answer", and settles
  that for the task while leaving tool arguments out. Add the arguments and the refusal
  message to the ledger entry — mindful that arguments can carry paths, which is why the
  ADR excluded file *contents* and not identifiers
  - **CONFIRMED and widened 2026-09-05 (session 2).** The same gap hid a second thing, and
    that one already has its reader: `evicted_then_reread` caught the model re-reading a
    file eviction had just dropped, on three consecutive turns, and nothing surfaces it
    because `diagnostics` defaults off. The fix is not only richer ledger entries but
    deciding what an *operator* record carries without being asked — ADR-0024's own
    reasoning, which is why `transcript_dir` is already independent of the caller's flag
  - **And the live stream is missing a field the final record has.** `Stream.turn` writes
    the token counts but not `tool_results_evicted`, which `write` emits per turn. So a
    stream shows the cache collapsing and cannot show the eviction that caused it — the one
    field that would have made the bug above self-evident while watching. One line to add

## Completed work moved from PLAN.md on 2026-09-23

Moved verbatim and never edited, on the owner's word: M8, M9, M10 and M12 whole, M11's
closed items, and every ticked Unscheduled item, each with its sub-items, struck originals
and the note that introduced it. Ids are kept, so a citation such as `Unscheduled.24` still
resolves here. M11's open items and every open or cancelled Unscheduled item stayed in
PLAN.md. The provenance of each is in CHANGELOG.md.

### M8 — The server can say why it will not work

**Exit:** `--doctor` prints a pass or fail line per check and exits non-zero on any failure.

1. ✅ 2026-09-07 `uv` is absent on this host, so `probe_toolchain_binds` probed and bound
  nothing — and `config.py` already called that "the single most likely first-run sandbox
  failure". Nothing checks the environment at startup, so it went undiagnosed for a session
  and was written up as an architectural limit instead
2. ✅ 2026-09-07 Measure that the server really does start with a missing workspace root,
  no `bwrap` and a dead endpoint. Read from code rather than run, and it is the
  justification for the item below
3. ✅ 2026-09-07 `--doctor`, one line per check: each root exists, `bwrap` runs a trivial command, the
  provisioned toolchain is current, the head node resolves by name *inside WSL*, every
  registry entry answers with `id_confirmed`, `transcript_dir` is writable, `cross_process`
  slots are live. Refuses to run outside WSL, where `bwrap` and DNS would answer wrongly.
  `sandbox.available()`, `limiter_available()` and `probe_entry()` exist, uncalled at startup
4. ✅ 2026-09-08 `--init`, writing `.env` and `models.toml` from answers and *printing* the
  client registration it cannot write, and accepting a pasted Windows path wherever a path
  is asked
5. ✅ 2026-09-07 `transcript_dir` skips `to_posix`, unlike `workspace_roots`, `toolchain_binds` and
  `sandbox_home`, so a Windows path in it has to be hand-converted to `/mnt/c` form
6. ✅ 2026-09-07 One refused path in `files[]` kills the whole call. Prefetch what resolves, fill the
  `files_skipped` the reply already carries, and name the path and the root it missed

### M9 — A delegation can run the project's tests

**Exit:** a delegation runs this repository's suite in a workdir and the server captures a
real non-zero exit for a failing test and a real zero for a passing one.

Measured 2026-09-07 before any code: inside `bwrap --unshare-all`, a venv under
`sandbox_home` carrying this repository's dev dependencies and an editable install ran the
whole WSL suite — **1237 passed, 4 skipped, 1 deselected in 207s at exit 0**, and exit 1
with that test included. The criterion above is already satisfiable; only provisioning is left.

1. ✅ 2026-09-08 `provision <project>` — build the interpreter and dev dependencies under
  `sandbox_home`, bound read-write, persistent and *outside* the workspace. Outside is
  mandatory rather than tidy: ADR-0041 records that inside a virtualenv `*secret*` and
  `*credential*` match ordinary library filenames and the scan covers each with
  `/dev/null`, breaking the environment it just read. It runs server-side, where the
  network is, so no `network: true` grant is needed — and `uv` is no substitute, binding
  only its binary and leaving its cache outside by design, so with no network it resolves
  nothing
2. ✅ 2026-09-08 Hash the project's dependency declaration beside the provisioned venv and
  have `--doctor` report a mismatch, or a delegation tests stale dependencies and returns
  the clean exit code ADR-0007 says to trust
3. ✅ 2026-09-08 Invoke the venv's interpreter by absolute path rather than changing
  `SANDBOX_PATH`, which is hardcoded to `/usr/bin:/usr/sbin`; the measurement needed no
  PATH change
4. ✅ 2026-09-08 A per-project list of tests that cannot run nested. Measured:
  `test_network_is_reachable_by_address_when_shared` fails because `--unshare-all` denies
  the network it asserts, and without the list the exit condition above is unreachable
5. ✅ 2026-09-08 Network stays off, with an ADR. `--share-net` re-shares the host's whole
  namespace with no allowlist or destination list, and this host reaches the cluster and
  the LAN
6. ✅ 2026-09-08 **Spike answered** — weigh covering read-only, which would make a discarded
  write fail loudly instead of exiting 0. Measure first: `__pycache__` and `.pytest_cache`
  are on the same list and a test run writes to both, so read-only may break what this
  milestone exists to enable. Any change supersedes a line of ADR-0041
    - a. ✅ **Measured 2026-09-07: it is viable, and the timid version is unnecessary.** With eight
    directories covered by `--tmpfs` plus `--remount-ro`, a write into a covered path is
    refused with exit 1 while the same write into the workdir succeeds, and the full suite
    still passes — 1237 passed, 4 skipped, exit 0. Python tolerates an unwritable
    `__pycache__` and pytest degrades to a cacheprovider warning, so **both** the secret
    shadows and the bulk list can be read-only rather than only the former
    - b. ✅ The first probe asserted read-only against a directory it had not covered, and passed.
    It needed a control write into the workdir to show the assertion could fail at all
7. ❌ 2026-09-08 ~~**`workdir` cannot verify Python work, which is the one thing it exists
  for.**~~ **Cancelled by the three items above:** a venv provisioned under `sandbox_home`
  ran this repository's whole suite offline at a real exit code, so the workdir does verify.
    - a. ✅ **Measured 2026-09-07, before provisioning:** no `python` on `PATH`, `import
    pytest` raises, `/tmp/vv` absent (the WSL venv is on the host's `/tmp`), no network to
    install one — so ADR-0007 had no captured exit to trust.
    - b. ✅ **The cause ruled out the obvious fix.** `.venv` is covered, not empty — a 64k
    `tmpfs` over it where the repository is `v9fs`, deliberately and by name (ADR-0035).
    Committing a Linux venv would be covered too, so a fix had to use an unnamed path.
    - c. ✅ **A covered write exits 0 and is discarded** — `touch .venv/probe` is gone by the
    next `run_bash` where a workdir write persists, so the exit ADR-0007 trusts was wrong.

### M10 — Knowledge that travels with the package

**Exit:** on a host holding only the package, a caller can write a valid agent file for
their own project without ever reading this repository.

1. ✅ 2026-09-07 Three facts are missing from the tool descriptions, which is the only channel the
  protocol delivers by itself: one question per delegating call, an unprefetched call is the
  dearest rather than the cheapest, and prefetch what is already known to be needed. A
  behaviour change with a CHANGELOG entry, not a wording fix
2. ✅ 2026-09-09 An `@mcp.prompt` entry point carrying the orchestration discipline — sizing
  a pass by expected findings, two large calls at a time, splitting a multi-part ask. The
  server registers six tools and no prompts or resources at all
    - a. ✅ **Done as a resource, not a prompt, and the difference is the whole finding.** A prompt
    is user-controlled by specification, so a person must invoke one; a resource is pulled
    by the *model*. `delegate://orchestration` carries the discipline and costs nothing
    until read (ADR-0066). A prompt remains defensible later as a *user-facing* briefing,
    which is a different job and no longer this item's
3. ✅ 2026-09-10 `install-skills`, shipping `write-delegate-agent`: it writes an agent file in this
  server's format and validates it by calling `list_agents` and checking the name lands
  under `agents` rather than `skipped` or `other_format`
4. ✅ 2026-09-10 Move the caller-side half of `docs-audit-local.md` into a repository-local skill, so
  the agent body keeps only what one pass reads — the file says exactly that of the section
  itself. It stays local rather than shipping, because the check list, the ownership map and
  the gate integration are specific to this repository
5. ✅ 2026-09-10 **Spike answered** — does Claude Code consume skills served over MCP through FastMCP's
  `SkillsDirectoryProvider`? If it does, that replaces `install-skills` outright
    - a. ✅ **Server half measured 2026-09-07: it serves them.** A `SkillsDirectoryProvider` over a
    directory holding one skill advertises the `resources` capability and lists two entries
    per skill under a `skill://` scheme — the `SKILL.md` and a `_manifest` — both readable
    through `resources/read`, plus one resource template
    - b. ✅ **Answered 2026-09-07: it does not, so `install-skills` stands.** Claude Code 2.1.263
    finds MCP skills through a paginated `skills/list`, gated on the server declaring
    `io.modelcontextprotocol/skills` under capability `extensions` and on a client flag off
    by default; FastMCP 3.4.7 declares `io.modelcontextprotocol/ui`, no skills key and no
    handler, so two gates fail before the flag is reached. Measured twice: the client binary
    against a negative control (`mcp__` 278 matches, `SKILL.md` 94, `skill://` 0), then live
    — provider wired in, client reconnected, `resources/list` returning both `skill://`
    entries while no skill was offered. Resources present and skill absent is the
    discriminator, so the null result is not a mis-wired provider

### M12 — Admission that queues instead of starving

**Exit:** a waiter parked on `kv_token_budget` is no longer overtaken indefinitely by later
smaller ones, shown by a test that reproduces the starvation first. Met 2026-09-11.

Needed before a second person shares the cluster, and worth having alone.

Raised by a documentation audit that was truncated by its own turn budget, then rerun. The
three settings below were each sized against a constraint that has since moved, and none of
them was re-derived when it did.

1. ✅ 2026-09-13 **Admission has no anti-starvation, and 2026-09-05's measurements make it
  sharper.** `_binding` refuses any waiter with `ahead > 0`, where `ahead` counts only
  earlier-ticketed waiters *that currently fit*. Deliberate — strict ticket order would
  reintroduce head-of-line blocking — but there was **no aging, no reservation, no barrier.**
    - a. ✅ **Barrier landed 2026-09-11, and the rule named here was the wrong one, measured.**
    `max_inflight_large_prefills` is held by other *large* requests, which queue by ticket,
    so a waiter on it is admitted the moment the blocker releases — bounded, not starvation.
    `kv_token_budget` is held by the newcomers themselves, so a waiter short of it is never
    feasible when they ask. Aging in `rival_fits`, ADR-0068. **`is_large` keeps this open.**
    - b. ✅ **What changed:** the eviction half was reasoned, not measured, and now can be. The
    KV pool is `kv_cache_size_tokens` from the endpoint's own metric, and
    `vllm:kv_cache_usage_perc` says how full it is, so "the prefix a starved request was
    queued to reuse gets evicted meanwhile" is now a question with an instrument. Decide
    after step 5 lands the reader, not before.
    - c. ✅ **Measured 2026-09-12, and the eviction worry is answered for this workload.** Read
      during a six-way fan-out with two passes actually queued: `kv_cache_used_fraction`
      **0.047**, `preemptions` **0**, `kv_cache_size_tokens` 1,467,988. Nothing was evicted
      because the pool was never near full — six passes at ~45k are about 19% of it, which
      is the figure the deferral below already predicted. So the eviction half does not
      block `is_large`, and `is_large` is what remains
    - d. ✅ **`is_large` closed 2026-09-13** (#184). Re-deriving it per turn is moot: the only rule
      that reads it for a *decision* is the gate the measurement condemned, so the question
      leaves with the setting rather than being answered on its own
    - e. ✅ **What is settled:** the related worry that `max_inflight_large_prefills = 2` trades
    cache hits for pipelining is **answered and dead.** Three concurrent large prefills
    over a shared prefix cost the same as three serial ones and hit cache identically, and
    the engine runs one at a time, touching two only at the handoff — which is exactly
    what "one running plus one staged" describes. Keep the setting and the value.
    - f. ✅ **What session 2 adds: `is_large` is decided once and never revisited.** `admit()` reads
    `prefill_tokens` from the *opening* estimate and holds the lease for the whole
    delegation, so an audit opening under `large_prefill_tokens` is filed as small for life
    — while later turns re-prefill 40-70k each and the slots file still reads `large: 0`.
    The rule that exists to serialise large prefills has never been tested by the workload
    it was written for, and the same staleness understates `kv_token_budget`. **The eviction
    item above is what makes a delegation's prefill grow**, so fix that first and re-measure;
    re-deriving `is_large` per turn is cheap but may then be unnecessary

2. ✅ 2026-09-13 **`kv_token_budget` is 1.64x the real KV pool, and the number to fix it is
  now readable.** The setting defaults to 2,400,000 and its help text says it "sits just under
  the measured KV pool". The endpoint reports `kv_cache_size_tokens = 1,467,988`, re-read
  2026-09-13, so it sits well over. Nothing has failed, because the setting protects latency rather than
  correctness — over-admitting queues and preempts rather than erroring — which is exactly
  why it drifted unnoticed. Likely cause is the 2026-09-04 model swap: a vision model
  carries more weights, so less memory is left for KV and the pool shrank underneath a
  constant measured against the old model.
    - a. ✅ **Do not fix it with a new constant.** An `.env` override is right today and drifts on
    the next swap; changing the default bakes one deployment's hardware into the
    repository. Derive it from what `backend_status` now reads, the way `WindowCheck`
    already derives per process — a server guessing at a figure the cluster publishes is
    the whole argument of the 2026-09-05 session.
    - b. ✅ Deferred deliberately on 2026-09-05: our delegations run ~45k tokens, so six of them
    is 19% of the pool and the gap is not currently reachable.
- **Correction to the ticked item above**, filed beside it rather than edited into it:
  `WindowCheck` **validates and never derives**, and says why, so it is not the precedent
  that entry claims. What allows this is narrower — the two numbers are ceilings on the same
  physical thing, and the lower of two ceilings overrules neither (#192)

### M11 — A call you can watch, and a cluster you can see (closed items)

**Exit:** the viewer shows a running delegation and live cluster figures on a host where
nothing was configured, and no tool result changes shape.

1. ✅ 2026-09-15 **`transcript_dir`'s fallback needs a state directory that does not exist yet, and the
  argument the item cites does not reach it.** **Decide the durability and the privacy
  question before writing the fallback.** Original:
    - a. ✅ `slots.default_dir()`, the only server-owned directory helper, is tmpfs in both
    branches — right for ephemeral slots, wrong for an audit record that must survive a
    reboot.
    - b. ✅ ADR-0024 argues an operator's audit should not depend on the *caller's* flag, where
    `transcript_dir` is the operator's, so it does not support defaulting this on. Doing so
    also writes task text to disk unchosen — "a trade rather than a rule", per its own help.
2. ❌ 2026-09-15 **Refused: it carries task text, which is the caller's material rather than the
  server's own behaviour, so it stays the operator's choice (ADR-0086).** Original:
  `transcript_dir` falls back to the server's own state directory when unset, so the
  viewer and the cost record work without setup. ADR-0024 already argues that what an
  operator can audit should not depend on the caller's flag
5. ✅ 2026-09-19 **`RateHistory` answered most of this; the post-reboot cold start is what is left.**
  `seed_decode_rate` consults the memory first and reaches the since-boot mean only when
  nothing has been seen that busy — measured 2026-09-15, 0 of 94 `priced` events did
    - a. ✅ **Raised 2026-09-11 from reporting to correctness, which re-ranks it.** `DecodeRate`
    seeds from the since-boot mean and only a **completed** turn replaces it, so a delegation
    sized for an idle cluster never corrects itself and dies at `stall_timeout` at zero turns.
    - b. ✅ **Obsolete as filed.** The seven deaths it cites were priced from the since-boot
    mean, which a persisted per-concurrency memory replaced (ADR-0075). The reply budget is
    already priced from observations rather than from a lifetime blend.
    - c. ✅ 2026-09-16 `vllm:generation_tokens_total` is published and **not** in the allowlist; differenced over
    a window, over `num_requests_running`, it is a live per-request rate. The histogram in use
    records only on *completion*, so it is blind during the stall it must detect.
    - d. ✅ 2026-09-16 **Backwards as filed, and unmeasured.** `expect` takes a minimum, which is pessimistic.
    The blend is "conservative" only if history is at least as contended as the moment being
    priced, and nothing has measured that. A spike rather than an item.
6. ✅ 2026-09-07 **Spike answered** — a `status` subcommand printing one plain-text block, since a TUI
  cannot run inside an agent's shell. Measure whether a detached terminal window can be
  launched from one; if not, print the command to paste
    - a. ✅ **Measured 2026-09-07: a detached launch works, so the fallback is not needed.**
    `wt.exe -- wsl.exe -d <distro> -e <cmd> <args>` opens a window, runs the command, and
    outlives the tool call that started it; the window closes when the command exits, so a
    long-running viewer persists
    - b. ✅ **The trap, which cost four wrong readings:** `wt.exe` consumes `;` as its own pane
    separator, so a compound command loses everything after the first statement and the
    fragments are launched as executables. Avoid `;` or escape it `\;`. Separately,
    `Start-Process -ArgumentList @(...)` against `wsl.exe` exits 0 and runs nothing at all,
    while the same arguments as one string work
    - c. ✅ And a lesson about the probe rather than the feature: a marker file written *first* in
    the command reported success while the tail was being mangled. Write the marker last
7. ✅ 2026-09-14 **Streaming, reopened 2026-09-05 with a scope.** **Slice 1 landed 2026-09-12 (#172,
  ADR-0070)**: the transport streams, `complete()` is unchanged, and the decode interval no
  longer contains prefill. **Slices 2-3 landed 2026-09-13 (ADR-0072)**: `stall_left` resets
  on token arrival against a live ceiling, and the heartbeat carries what has arrived.
  **Slice 4's partial landed 2026-09-14 (ADR-0078)**: a failing turn carries what it decoded
  and a deadline returns it as `partial: true`. **What remains of slice 4**: the retry split,
  still deferred for its own evidence, and showing the stream itself. Narrower than it looked. Two
  populations were conflated: a *length stop* returns normally with its tokens already parsed
  into the response, fixed above without streaming; a *cancelled* turn leaves them in the
  adapter's accumulator, which is what this still owns. Since #172 they are no longer "only on
  the backend" as the scope note below says — they are in `_StreamAccumulator`, discarded on
  the exception path — plus the retry split
  streaming makes available (a read timeout *before* first token is prefill or queueing, *after* is
  slow decode; untouched in #172), and showing the stream itself. **Weigh a non-terminal
  viewer first**: `follow` never repaints and making it is the expensive half, where a
  browser over the same `.jsonl` gets repaint, scrollback and selection for nothing.
  Filed and cancelled the same day,
  2026-08-25, on the grounds that MCP tool calls are request/response so the caller sees
  nothing incrementally either way. That is still true of the caller and was never the
  whole picture: it never had its own ADR (ADR-0018 is about the progress notifications
  offered as the substitute), and it was annotated once during the ADR-0043 work as
  *"worth revisiting: the premise moved"* — because the cancellation weighed one consumer
  and there are two. The second is the transcript stream a person reads *while* a
  delegation runs.
    - a. ✅ **Lost work is the argument that was missing.** A task too big to finish a turn is
    abandoned at the tighter of `stall_timeout` and `turn_timeout`, and everything
    generated is discarded: `_decode()` needs the whole body as one JSON object, and
    `complete()` states it never returns a partial. The tokens exist only on the backend.
    Nor is the work re-done — `except TimeoutError` always raises, and retry covers only
    an unavailable or refusing backend.
    - b. ✅ **It makes the stall deadline honest, which beats returning partials.** ADR-0047 chose
    turn completion because every other signal was fake: the per-turn notification fires at
    the *top* of a turn and the keepalive is a timer, so both reset the clock on the very
    turn that wedged. Token arrival is real liveness. `stall_left` resetting on token
    arrival means a call producing tokens is never killed and a call producing nothing
    still dies — **this supplies the signal ADR-0047 lacked rather than contradicting it**,
    which is the argument that would supersede its heading.
    - c. ✅ **Scope, in order of value:** a `stream` key in `wire_body()`; an SSE accumulator
    behind the existing `complete()` contract, so *"never returns a partial"* stays true
    and the stall path reads the accumulator rather than `complete()`; `stall_left` reset
    on token arrival; deltas feeding `stream.turn`; and last, a partial returned at
    `dispatch_timeout`, marked partial so the calling conversation can decide whether to
    ask for the rest.
    - d. ✅ **Cheaper than it looks**, because the seam was preserved for it — `base.py` says SSE
    accumulation lives per adapter behind one contract, a method on the protocol rather
    than a shape baked into the caller. **Costly** because `_decode()` requires one whole
    JSON object, so there is no line-by-line path to extend, and the adapter's *"a single
    non-streaming call **is** the turn"* is a claim streaming invalidates.
    - e. ✅ **2026-09-05 supplied the evidence this was missing, and session 2 confirmed it.** Five
    stalls in one session, every
    one with `backend_status` reporting the endpoint healthy and idle — zero preemptions,
    KV under 4%, no admission wait. Turn completion is the only liveness signal there is, so
    a model reasoning productively inside one long turn is indistinguishable from a wedged
    one, and the deadline kills both. Prompt shape shifts the odds and does not remove the
    failure: one pass died having completed **29 turns**, in its thirtieth. ~~Nothing bounds
    the length of a single turn, and no prompt can. `stall_left` resetting on token arrival
    is the only fix here that is not a guess about how a model will decompose its work.
    Tentative because the session's own shape experiments then contradicted each other —
    a prefetched pass with one turn boundary survived while the same shape with many died
    at zero turns — so the stalls are real and the mechanism proposed for them is not
    established. A second session was investigating the same failures independently; read
    its findings before treating any of this as the argument.~~
    - f. ✅ **Corrected:** something does bound a single turn — `max_tokens` — and it is set
      2.6x above what the deadline can decode, so the bound never binds before the kill
      does. That makes streaming a **liveness** fix rather than *the* fix: capping the
      budget stops the deaths, and `stall_left` resetting on token arrival is what stops
      a legitimately long turn being killed once the budget is honest. Keep both, in that
      order, and drop "only". The shape experiments that read as contradictory were
      measuring turn boundaries against a budget nobody had priced
    - g. ✅ **The trap, recorded with it:** token flow is not turn completion. The `alive`
    heartbeat must not report streamed tokens as liveness without the deadline change
    above, or it would call the 2026-09-04 stalls healthy. ~~Open question to measure: a
    model looping while emitting tokens is bounded by `max_tokens`, but whether a reasoning
    model's thinking tokens count against it is unknown here.~~ **Answered 2026-09-05
    (session 2): they do.** A turn whose entire visible answer was the word `DONE` reported
    `output_tokens: 697` at `effort: low`. So `max_tokens` does bound a looping model — it
    is simply set far above the deadline today.
8. ✅ 2026-09-15 **The retry split streaming made available, and half of it is already in.** The
  `httpx` read-timeout path still lumps prefill/queueing together with slow decode, and that
  is the half that remains.
    - a. ✅ A timeout *before* the first token is prefill or queueing; *after* it is slow decode.
    A slow decoder will likely be slow again, so retrying spends the budget discovering
    that, where a queueing timeout may sail through immediately.
    - b. ✅ **Half landed 2026-09-14 (ADR-0078):** the adapter's whole-turn bound reports
    `while_generating=first is not None` rather than an unconditional `True`, so that path
    distinguishes them.
    - c. ✅ Split out of streaming, where `openai_compat.py` deferred it as "would change what
    #167 retries, which wants its own evidence rather than arriving as a side effect" — so
    measure what each population costs before changing the retry rule.
### Unscheduled — the ticked items

2. ✅ 2026-09-13 **Reasoning was generated, paid for, and then discarded** — nine dispatches of 446
  reported empty at a *length stop* holding 265,092 output tokens, because `answer` joined text
  blocks only. Bannered now, with `answer_is_reasoning` (#188)
3. ✅ 2026-09-14 **Eviction and dedup undo each other** — a stubbed 34KB result is handed straight back on the
  next identical call, plus the turn spent asking. Neither half can see the other
6. ✅ 2026-09-14 **The M10 spike is misfiled and stale** — it does not move M10's exit, and the workaround it
  meant to avoid writing down is now in CLAUDE.md. Its accuracy half is still unmeasured
    - a. ✅ Re-filed rather than run. M10's exit is about a caller on a host holding only the
    package writing a valid agent file, and withholding `run_bash` from a verifying pass
    moves none of it. It stays open as an **Unscheduled** item, where its accuracy half —
    a task that *necessarily* quotes, against a known discrepancy — can be measured on its
    own merits instead of being paid for out of a milestone it does not serve
7. ✅ 2026-09-14 **The docs gate measures the last append-only entry a line short** — length runs to the next
  heading, so the newest entry omits its trailing separator and an over-budget one lands, then
  blocks whoever appends next. Found by being that next person
8. ✅ 2026-09-14 **Nothing stops a conflict marker reaching `main`.** Two did, in Markdown, where no test or
  lint looks and the gate checks ownership and budgets rather than content. One line in the
  gate; the nested shape is the one to write the negative test against
9. ✅ 2026-09-13 **Eviction fired at 4% of the window against a 50% gate** — the pressure check
  was gated on a setting that ships off, so it stubbed on count alone. It now applies wherever
  the window was declared, and `keep_tool_results` is 16 (JOURNAL 2026-09-13, #191)
10. ✅ 2026-09-14 **A retained *count* is the wrong unit for a history.** One run's 36 results ranged from
  200 bytes to 50,068, so `keep_tool_results` prices a one-line refusal and a 50KB file
  identically. A share of the window is the unit; the count becomes its floor
    - a. ✅ **Corrected 2026-09-14, twice, before any code was written.** *A share of the window
    cannot be the unit*: the denominator is `entry.context_window`, which is a silent
    131,072 default whenever `models.toml` omits the key — `context_window_defaulted` exists
    precisely to say so, and it is why `evict_upto` consults `share()` only as a gate today.
    The numerator is part estimate as well. The unit that is always known is the **retained
    bytes themselves**, with the count as the floor
    - b. ✅ *And the selection must not change*: eviction is prefix-ordered on purpose (ADR-0056),
    because a boundary that moves costs the cache everything after it. So this is a
    size-based **boundary** — advance the oldest-first cut until retained bytes fit a budget
    — never size-based *selection*. "Evict the large ones and keep the small ones" would
    trade the whole prefix cache for a few thousand tokens
12. ✅ 2026-09-13 **The priced rate climbed past what the cluster can physically decode** — 88.53
  tok/s and a 95,612-token ceiling against a benchmark of 44.1. ADR-0070's move to
  `decode_seconds` inverted the short-turn defect ADR-0071 had fixed in only one of the two
  estimators; one shared floor now, and the cost is tested rather than hidden (ADR-0073, #181)
13. ✅ 2026-09-15 **`rate_source` names where the *seed* came from, not the number beside it.** It is set once
  in `__init__`, so `observed_at_concurrency` can label an EMA no observation at it produced
15. ✅ 2026-09-13 **An unscoped `search_files` cost 490-572s, and the contract recommended it** —
  `glob` claimed to be the speed lever and the bad-`path` refusal said "omit it to search
  everywhere", which one delegation did, at 239s. Scope is worth ~100x; `read_file` was never
  the problem. Schema, result note and refusal all fixed (ADR-0074, #182)
16. ✅ 2026-09-15 **A turn's independent tool calls run serially.** `_run_calls` holds one thread to preserve
  result order, but returned order need not be executed order and reads do not affect each
  other. `run_bash` stays ordered; the work is locking `cached` and `watch`
17. ✅ 2026-09-13 **Scoping was asked for and never shown** — `path` is required with an
  `_unscoped_` escape, and the description carries the workspace layout, folders and files
  (ADR-0076, #189). Unproven until a delegation's *first* search names a subdirectory
18. ✅ 2026-09-16 **The path policy is 65-75% of a search and it is all `lstat`.** Profiled: 80.7s in
  26,942 `lstat` (13.5 a candidate -- `realpath` re-walks shared prefixes), 37.1s in 4,000
  `stat` (`isfile` then `exists`), 20.5s in 403 `check-ignore`, matching 1.1%
20. ✅ 2026-09-13 **The viewer states true things in ways that read false.** Four findings, one
  branch:
  `requests_running` in a `priced` row is the lease's grant-time concurrency echoed, not live
  cluster state, and the viewer renders it as "N running"; a turn boundary collapses into one
  displayed second, so a budget line attaches to the wrong turn (turn 15's end and turn 16's
  pricing were 5ms apart); the state column is 9 wide against a 2-wide gutter, so "queued 38s"
  has no room and "queued 120s" overflows — count minutes past 59s; and a queued delegation
  paints a row per second, which should be once a minute plus one line when it ends
21. ✅ 2026-09-13 **Two more the branch above found, filed here rather than rewriting it** — a
  selected row carrying an emoji over-padded and wrapped, because `len()` counts characters
  and a cell is not one; and a cut-off reply read as a finished one, `ok` being true of both.
  Its "queued 120s" is impossible and was corrected in the CHANGELOG, not in its body
22. ✅ 2026-09-16 **The registry's `concurrency` default is 5** while this deployment's `models.toml` sets 6. Carried out of the session hand-off notes, which are not a document anyone else reads
23. ✅ 2026-09-13 **An empty `name:` is accepted where the document says it is refused.**
  `agents.py` line
  330 reads `if declared and declared != name`, so a bare `name:` with no value is falsy and
  treated as absent, while `docs/AGENTS.md` says a `name` that is present "must equal the
  filename, or the file is refused". Either the code distinguishes present-but-empty from
  absent, or the document stops promising it does — the second is cheaper and probably right.
  Found by a STALE pass during the 2026-09-12 verification fan-out
    - a. ✅ **Closed the other way, 2026-09-13** (#186). The code moved, not the document: the
    unknown-key refusal eleven lines above calls a silently-ignored setting "the bug this
    format was rewritten to prevent", and a bare `name:` is that shape. Refusing costs
    nothing — a matching `name` is redundant and a disagreeing one was already refused
24. ✅ 2026-09-19 Content-level detection for a renamed secret — every path-policy layer inspects the
  path and none the bytes, so `config.json` holding a private key passes all of them, and
  `run_bash` can read one the mount-level scan did not match by name.
    - a. ✅ One finding, not two: fixing the detection fixes both ends. A narrow, high-precision
    check for key material.
    - b. ✅ **Not** by pointing `scan_text` at it, as the 2026-09-02 review recommended — that
    scanner looks for RFC1918 addresses, private-DNS suffixes and non-allowlisted emails,
    and would false-positive on the source a review delegation exists to read.

26. ✅ 2026-09-09 **The denylist file matches itself, so layer 3 is unusable inside the sandbox.**
  `security/secret_globs.txt` matches its own `*secret*` entry, so the scan covers it with
  `--ro-bind /dev/null` — and inside the sandbox it is then a character device owned by
  `nobody`, which reads as `Permission denied` rather than as empty. Any nested run that
  exercises layer 3 fails: 42 of `test_tools.py` on 2026-09-08, and it is why this repository's
  own suite cannot run nested in full. It fails *closed*, so it is a usability bug and not a
  hole. Covering it protects nothing either way — the file holds patterns, not secrets, and is
  tracked in git — so the fix is to exempt the configured `secret_globs_file` and
  `opaque_globs_file` from being shadowed at all

27. ✅ 2026-09-08 `models.toml` is not on the layer-3 denylist, though it names the host that
  `.env` is denied for. Found while covering `--init`'s backups on 2026-09-08:
  `.gitignore` and `NEVER_TRACK` both hold it, so it cannot be committed, but
  `security/secret_globs.txt` has no entry for it and so `read_file` will hand it to a
  delegated model. Adding one line fixes it and changes what a delegation may read, which
  is why it is an item rather than a detail of that commit

28. ✅ 2026-09-10 **`.env.example` is covered by `.env.*`, so a tracked example file is unreadable.**
  Found 2026-09-09 by running this repository's suite nested for the first time, which the
  ADR-0065 fix made possible. Inside the sandbox `.env.example` is a character device owned
  by `nobody` and reads as `Permission denied`, while `models.toml.example` is readable —
  because the denylist names `models.toml` exactly and `.env.*` matches every suffix. That
  asymmetry contradicts `security/secret_globs.txt`'s own comment, which says the example
  file stays readable and is "the control on this pair". It fails closed, so it is the same
  usability class as ADR-0065 rather than a hole, and it is the one test still failing a
  nested run. Changing it changes what a delegated model may read, which is why it is an
  item and not a detail of another commit — the same reasoning the `models.toml` entry used

29. ✅ 2026-09-19 Globs in `files[]`, expanded server-side — a shorthand for naming many files, not a
  way to look for anything. A convenience ranking below the search tool, now the fork is
  settled the other way and the toolless-`delegate_readonly` justification no longer holds.
    - a. ✅ The work is in the budget rather than the matching: a glob hitting two hundred files
    has to skip and account for them the way `context.prefetch` already does, not spend
    `prefetch_budget` silently.

These came out of nine delegations dying at the stall deadline with zero turns. The
measurements, what each fix does and does not cover, and what the next session should start
with are in the hand-off notes — `~/.claude/plans/handoff-notebook.md`, untracked and
local, because they are working notes rather than a product fact.

30. ✅ 2026-09-12 **The reply budget is priced while the cluster is idle and spent while it is busy.**
  Admission serialises a fan-out, so every sibling prices against a cluster that has not
  filled yet. Priced from admission's own counters since #169; a burst's *first* member
  still cannot know the burst is coming
31. ✅ 2026-09-15 **Hold a delegation briefly when the gate is idle**, so a burst's first member prices
  against the burst. ~~Buys the one case above; costs latency on every solo large call.
  Default off.~~ **Re-ranked 2026-09-12; the "large" in the title was backwards.**
    - a. ✅ **Re-evidenced 2026-09-15; the 65.6 figure it cited was withdrawn 2026-09-12.** Over 94
    `priced` events `expect` returned **10.95** at concurrency 1, 2 and 3 alike, 16.09 at 4,
    20.98 at 5 -- a rate rising with contention, and **4.0x** pessimistic against 44.1 solo.
    - b. ✅ **Bucketing without the hold was tried 2026-09-15 and reverted.** A burst's first member
    is labelled 1 and decodes at six-way, so trusting the label prices it at 44 to decode at
    19 -- the fatal direction. The hold comes first; a regression test already says so.
    - c. ✅ Hold only while the gate is idle, so it costs nothing when concurrency is already known
    and 10s when it is not. The arrival distribution measured that day is bimodal, which is
    what makes a fixed window work: six probes inside 8.5s, or one alone.
    - d. ✅ **The coalescing evidence this rested on died with #194; the item did not.** That gate
    is gone (ADR-0077), so large calls now arrive uncoalesced exactly as small ones do,
    which *widens* what a hold covers. The mechanism never depended on the gate.
32. ✅ 2026-09-13 **`RateHistory.expect` falls through to the since-boot blend when it has
  nothing at the asked concurrency**, which is the optimistic answer to the busier question. Measured
  2026-09-12: six probes priced from that fall-through at 34.85 tok/s and then decoded at
  27.1 six-way — a 1.29x overestimate, not the 1.8x recorded before the instrument was
  fixed. A 37,638-token ceiling still decodes inside an 1,800s turn at that rate, so the
  fall-through alone did not kill the fan-out. **Both of those figures came from a task that
  reproduced its own prompt and are withdrawn** — see the tick below. Against the rates that
  stand, the cold start is not marginal but decisive: the blend reads 34.96 where six
  concurrent delivers just under **20 tok/s**, so it is **1.75x** optimistic, and the 37,756
  tokens it authorises need **1,888s of an 1,800s turn**. Two of four died there at zero
  turns. An honest rate prices 20,952 — which is itself below what a STALE pass needs, so
  the rate and the margin have to be fixed together or neither helps. That is what remains
    - a. ✅ **Persisting the memory across restarts would have saved all four** at no cluster cost,
    unlike a synthetic warm-up, because the samples are real work at real concurrency and
    effort. `slots.default_dir()` is tmpfs — wrong for an audit record, right for a rate —
    and a `served_model_id` stamp discards it on a swap rather than a constant
33. ✅ 2026-09-12 **Every rate measured from a synthetic probe is an artefact, and the cause is
  confirmed.** Seven probes reproducing README.md verbatim read 65.6 tok/s solo and 27.1 at
  six concurrent — faster at six than the 23 measured at four, which cannot be true of one
  decoder, because contention only slows. The served model has a speculative-decoding module
  attached, and copying in-prompt text is close to a best case for acceptance. **A benchmark
  task must not be answerable from its own prompt**; the figures that stand are the owner's,
  44.1 solo, ~23 at four and just under 20 at six
    - a. ✅ **The other half was a different defect and shipped in #177.** "A low expectation
    searches widely and keeps the worst" is the design working, not the asymmetry: a
    six-way sample bounds a solo question from below, which is what already prices a
    burst's first member at the six-way floor. What made it look broken was that the
    memory had no floor of its own, so a 237-token turn reading 16.64 tok/s entered it and
    became permanent. Guarded at the memory; `expect` deliberately untouched
34. ✅ 2026-09-11 **`turn_timeout` is absent from the ceiling's `min`**, so the budget
  authorises a reply one attempt cannot deliver (#166)
35. ✅ 2026-09-12 **A generation overrun is retried as though it were a network blip**, with
  the same budget against a fraction of the clock (#167)
36. ✅ 2026-09-12 **The heartbeat names a deadline that will not kill the turn**, reporting
  `dispatch_timeout` while a tighter one fires (#168)
37. ✅ 2026-09-12 **The docs gate dies while printing a finding that is not pure ASCII** (#173).
  The hooks run under Git Bash, where stdout is cp1252, and the printing loop covers warnings
  and runs before the verdict — so a non-ASCII warning killed a commit that was about to pass.
  Invisible because the agent's own shell exports UTF-8
38. ✅ 2026-09-13 **The 2026-09-11 audit's remaining findings**: the `read_metrics` note that
  calls the
  blend conservative when it is flattering under load, five cross-plane duplications, and two
  unsourced claims in `docs/ARCHITECTURE.md`. The three runbook findings are closed — CLAIMS
  in #171, the concurrency remedy re-derived in #175, and the effort column deliberately left
  alone there because its premise reproduced only under the pricing defect #172 fixed
    - a. ✅ **Closed 2026-09-13** (#185). The note is corrected with the number that refutes it;
    three duplications resolved by making `CLAUDE.md` delegate rather than restate, and one
    by `README.md` linking. **Two findings were themselves wrong** — both "unsourced" claims
    are sourced, in `PLAN.md` and `CHANGELOG.md`, and the audit missed them by searching for
    its own phrasing. The fifth duplication is a deliberate non-fix: ARCHITECTURE's copy is
    in a `BUDGET` comment justifying a past raise, which is a record, not competing prose
39. ✅ 2026-09-14 **The rate memory surviving a reconnect switched off the KV-pool reading, so the
  token budget is 1.63x the pool again.** `seed_decode_rate` returns early when the history
  remembers a rate for this concurrency, and `on_pool` is called *after* that return — so
  the scrape that reports `kv_cache_size_tokens` to admission only ever ran when the memory
  was cold. ADR-0075 made it warm on every reconnect and thereby retired #192 without
  touching it. Measured 2026-09-14 after nine delegations on a freshly reconnected server:
  every dispatch priced `observed_at_concurrency`, `kv_cache_size_tokens_seen` **null**, and
  `kv_token_budget_effective` **2,400,000** against a reported pool of **1,467,988**. The
  fix is to report the pool before the early return, or to scrape regardless; the trap is
  that the better the rate memory gets, the less often the pool is seen. Also worth a
  negative test that the *pairing* holds, since each half passes its own tests today
40. ✅ 2026-09-19 **`admission_wait_timeout` bails out after 30 minutes having produced nothing**, and
  its help text says it was sized for an era when the queue was unordered — a premise
  tickets and the starvation barrier removed, with nobody re-deriving the number.
    - a. ✅ **Re-measured 2026-09-14: no reachable path on this workload.** A five-wide fan-out of
    ~40k-token prefills, `peak_inflight_seqs` 5, `peak_inflight_tokens` 498,392, and
    `admission_wait_count`, `admission_wait_seconds_total` and `queued_waiters` all **0**.
    - b. ✅ 2026-09-19 So the question is no longer what the number should be but whether the bail-out has any
    purpose left. `peak_inflight_seqs` is the honest gauge: `admission_timeouts` counts
    *completed* waits and reads 0 both when nothing waits and when everything still is.
    - c. ✅ **It had fired twice on 2026-09-12**, the first time on this deployment — two passes of
    a six-way fan-out waited the full 1800s on `max_inflight_large_prefills` and were
    refused having produced nothing. Not latent, that morning.
    - d. ✅ **Then twice more that evening with two passes answering**, killing the theory that a
    working budget dissolves it: a 2-wide gate holding each slot for a whole delegation.
    3,600s of waiting at `kv_cache_used_fraction` **0.031**, 0 preemptions, idle cluster.
    - e. ✅ 2026-09-19 Every wait it ever fired on was on that gate, and the gate is gone (ADR-0077, #184),
    which is what makes the 2026-09-14 reading the current word. **Re-rank on that**, not on
    the 09-12 firings, and measure the item below before moving 1800.
    - f. ✅ 2026-09-19 **Refuted 2026-09-17: it fired eight times on `max_inflight_seqs`**, the gate that
    survived. A fourteen-wide fan-out into six slots; every waiter refused at 1800s having
    produced nothing, while the six holding slots ran on. Re-rank on this, not on 09-14.
    - g. ✅ 2026-09-19 **Deferring the deadline on queue position would have saved none of them**, read
    2026-09-19: `_binding` tests the three capacity rules before `QUEUED_RULE`, so those eight
    named a full gate rather than a queue. What 40.b needs deciding is the wait's own bound
    - h. ✅ 2026-09-19 **58 is not the cause here, checked rather than assumed.** A leaked slot does shrink
    the gate permanently, but all six were producing tokens on 2026-09-17 — 37,605 output on
    three of them — so the gate was genuinely full and 40 stands on its own facts
41. ✅ 2026-09-13 **Release the *large* half of an admission lease at first token, not at the
  end of the run.** `admit()` holds it for the whole delegation, and the prefill it serialises
  is over once decoding starts. Measured 2026-09-12 at `effort: high`: time to first token
  **64.1s**, delegations running 271-847s, and the two that died holding a slot for 2,100s —
  4x to 33x longer than the work it protects, which is what made two passes wait out
  `admission_wait_timeout` behind a limit of 2 on a cluster at 3% KV
    - a. ✅ **Only reachable since ADR-0070**, which made first-token arrival observable
    - b. ✅ **It probably retires `max_inflight_large_prefills` rather than competing with it.** Once
    a slot is held only while a request is prefilling, the number held at any moment is the
    number the *engine* is prefilling — one running plus one staged — so a limit of 6 can
    never bind and even 2 would rarely. The setting goes inert, which is a cleaner answer
    than tuning it and is why both belong in one ADR
    - c. ✅ **A slot is taken on the estimate, not on the work, measured 2026-09-12.** At limit 2,
    four of six calls waited 28.9s and 57.1s for a large-prefill slot *while doing no cold
    prefill at all* — the prefix cache served 98% of each. `is_large` reads `prefill_tokens`
    from the opening estimate, which cannot know that. Hit rate here is 59%, and a fan-out
    over shared documents is the shape that triggers it most reliably
    - d. ✅ **Measured properly 2026-09-12, over twelve disjoint cold-prefill sets: the gate is
    pure overhead here.** Limit 2 against 6 — 4 of 6 queued against none, 286.3s of
    aggregate waiting against zero, batch 12.1s slower, each call 10.2s slower, nothing
    bought. Limit 6's span is the floor the engine sets by serialising prefills itself, and
    limit 2 cannot beat it. **So the setting is a candidate for removal, not retuning**, and
    that decision belongs in the same ADR as the first-token release above — a slot released
    at first token may make the question moot either way
42. ✅ 2026-09-13 **Remove `max_inflight_large_prefills`, or find the fan-out where it earns
  its keep.**
  Carried out of the ticked item above, whose body is frozen. 2026-09-13, three delegations
  on the first-token release: the gate never bound, and the one still holding a slot after
  two minutes was held by the *engine* queueing its prefill, not by the rule. Evidence of
  redundancy, not a decision — three calls on one machine is not the case that matters, so
  re-run it wider at the shipped limit of **2**. **`admission_wait_timeout` and M12's
  `is_large` both wait on this**: 1,800s is re-derivable only once the gate that made it
  reachable is settled, and `is_large` is read by this rule alone
    - a. ✅ **Answered 2026-09-13, and the answer is remove** (#184). Two arms of six over disjoint
    cold sets: limit 2 queued 4 of 6 for 217.9s; limit 6 queued none and finished 10.6s
    sooner, at 0.034 KV and zero preemptions either way. "Inert at 6" is confirmed; "even 2
    would rarely" is not. Inert where set, harmful at the shipped default — so no value earns
    its keep
43. ✅ 2026-09-13 **Remove the setting the measurement condemned.** Five consumers, one a
  decision: the
  `_binding` rule; ADR-0072's early release, which goes vestigial; `rival_fits`'s ordering;
  the server instructions and `delegate://orchestration`, making it a contract change with
  an ADR; the cross-process counter and two gauges. `peak_inflight_large_prefills` counts
  *leases*, never prefills the cluster ran — the engine queues those itself
- **Correction to the ticked item above**, filed beside it rather than edited in:
  `large_prefill_tokens` does not survive as that entry assumed. It classified for the rule
  and for nothing else, so it went with it (ADR-0077, #194)
44. ✅ 2026-09-18 **Work that does not fit one turn** — the splitting premise was refuted by this
  entry's own evidence (#204). What remains is the `reply_budget_margin` half, which is
  arithmetic rather than an instrument reading and still binds.
    - a. ✅ **7.6 tok/s was an arithmetic artefact, not a rate:** one attempt's tokens over two
    attempts' wall time. Its concurrent sibling produced more tokens in 663s. Transcript
    rows, both turns and the independent corroboration are in #204.
    - b. ✅ **`reply_budget_margin` is the binding constraint, measured 2026-09-12.** A STALE pass
    wants ~23,700 output tokens; an 1,800s turn at the six-way 19.4 tok/s authorises
    `1800 x 19.4 x 0.6` = **20,952**, so the margin alone puts the task out of reach.
    - c. ✅ An intermediate reading of 27.1 tok/s concluded it fitted at 28,698. That came from
    probes reproducing their own prompt against a speculative-decoding module, so it was an
    artefact; the owner's benchmark puts six concurrent just under 20, restoring 19.4.
    - d. ✅ **What the re-derivation settled:** the denominator is `turn_timeout`, not the
    `stall_timeout` the help text named, and "a turn also prefills" is wrong twenty-fold —
    prefill measured ~2% of a turn.
    - e. ✅ The 0.6 is untouched because the *rate* was wrong. It would need to be about 0.57 for
    the cold-start ceiling to fit, and fitting a constant to a wrong rate is the mistake
    this roadmap already records against `kv_token_budget`.
    - f. ✅ **Hardened 2026-09-16:** the seed priced above what the turn achieved in 31 of 113
    informative turns, worst 2.98x, so the margin absorbs more error than it was credited with.
    Blocked on the rate; moving `turn_timeout` to fit is the same mistake on another constant
    - g. ✅ **Demonstrated by accident 2026-09-17:** one audit pass succeeded first attempt at
    `high` on a 58,959 ceiling, while another exhausted its reasoning at 20,378 holding the
    *smallest* prefetch of the wave. Input size is not the driver, the ceiling is. Supports b.
45. ✅ 2026-09-15 **A `path` naming a whole workspace root is refused.** Required and
  supplied since ADR-0076, then satisfied with the root: 8 of 9 first searches named a root
  or the sentinel and 1 named a subdirectory. The refusal carries its children (ADR-0082)

46. ✅ 2026-09-16 **A gitignored directory eats the scan cap, so the search answers from nothing.**
  `_search_candidates` prunes symlinks and secrets, never gitignore, so a root walk spends
  all 2000 on `.venv` and returns 2 lines where 351 exist. Prune in the walk, never admit

47. ✅ 2026-09-16 **A refused root costs a turn, and a turn is ~20% of a short delegation.**
  ADR-0082 trades wall-clock for a round trip, which is cheap against 391.8s and dear against
  a five-turn budget. Revisit once 18 and 46 land and an unscoped walk is no longer ruinous

48. ✅ 2026-09-16 **`prefill_tokens` is threaded through `acquire` and `admit` and read by nothing.**
  It existed only to enforce `max_inflight_large_prefills`, removed with that gate (ADR-0077).
  Every caller still computes and passes an estimate the predicate never sees

49. ✅ 2026-09-18 **`RateHistory` is one 64-sample deque shared by every concurrency, evicted by recency.**
  `expect` takes a *minimum*, so the busiest samples carry all the value — and 64 newer quiet
  ones discard them. Per-concurrency buckets, or eviction by value rather than by age.
    - a. ✅ Thirteen five-wide dispatches are 65 samples and evict a six-way reading. Measured
    2026-09-17 after a six-wide fan-out the file held one pair, `[3, 18.869…]` — a six-way
    rate under a label of 3, because `expected_concurrency` is a guess (50).
    - b. ✅ ~~**`trusted` disables the widening entirely**: `label_trusted` is
    `admission_idle_hold > 0`, so `expect` never widens. `expect(5)` = 54.59 beside 18.74.~~
    **Corrected 2026-09-18: trusted *prefers* the bucket and still widens when it is empty,
    deliberately (ADR-0085).** The harm is a *populated* bucket of one, which is 14's, not this.
    - c. ✅ ~~54.59 is above the 44.1 solo benchmark, so it is 14's quoting-turn artefact
    returned whole from a bucket of one, pricing a five-way pass at **58,959** tokens.~~
    **Re-measured 2026-09-18: that bucket gained a sample, so `expect(5)` is 17.050 and the
    artefact moved rather than healed** — it is now `expect(1)` = **60.590** against the same
    44.1, alone in its bucket, on every solo call. Filed to 14; the eviction half is this one.

50. ✅ 2026-09-18 **`expected_concurrency` under-counts a burst the client staggers**, so a
  fan-out labels its samples below the contention they met. `admission_idle_hold` is 10s;
  the six calls of one message opened 5.5s apart across **28.4s**, and were labelled 2 and 3.
    - a. ✅ The two errors cancel while both sides use the same guess — `expect(2)` still finds
    the six-way sample — so this is not urgent alone. It becomes wrong the moment 49 keys the
    memory on the label, which makes it a prerequisite rather than a nicety.
    - b. ✅ **Proposed: release when the gate fills, or 10s pass with no new arrival.** Measured
    gaps inside a six-wide burst: max 6.4s over a 28.4s span, then 8.0s over 32.4s. A debounce
    beats a fixed hold, and 10s leaves the solo call exactly where today's fixed 10s puts it.

51. ✅ 2026-09-18 **The since-boot seed is structurally optimistic above mean concurrency, and the comment
  justifying it claims the opposite.** `openai_compat.py` line 827: "a blend over every
  concurrency regime since boot makes it conservative rather than flattering". It cannot be.
    - a. ✅ A lifetime mean over regimes averaging below six must overprice a six-way stream.
    Measured 2026-09-17: seed 34.82 against a real 19.3 per stream, between the module's own
    44.1-solo and 19.4-at-six benchmarks by construction. Feeds 44's rate half.

52. ✅ 2026-09-18 **No audit pass covers the project plane or `.claude/` for staleness**, so the audit's own
  runbook drifted unchecked. Passes 1-6 read `docs/` against `src/`; the root documents get
  only TOO VERBOSE, WRONG DOCUMENT, CROSS-PLANE LEAK and CLAIMS.
    - a. ✅ Found by hand in minutes: `docs-audit-dispatch/SKILL.md` names two removed
    settings as live (ADR-0077), calls the admission wait silent against 6,936 `waiting`
    events, and advises a fan-out that guarantees `admission_timed_out`. ~~CLAUDE.md's
    ownership roots omit `.claude/`.~~ **Stale on filing: `b1175eb` (#227) added it in the
    same commit.** The SKILL.md half stood and is what this item fixed.
    - b. ✅ **The agent file caps `max_turns` at 5, and the searching classes need more.** MISSING
    and CLAIMS both hit it at 8 and 9 tool calls, so both "clean" verdicts are partial — the same
    weak evidence the 2026-09-11 record flagged. Raise it per class, or prefetch what they seek.
    - c. ✅ **The runbook says an oversized pass "does not come back truncated; it comes back
    empty"** (line 169). Measured 2026-09-17: four exhausted their reasoning and one truncated
    mid-sentence, every one with `empty_response: false`. Name the fields that do say so.

53. ✅ 2026-09-18 Server-format twins for the four Claude Code agents — `code-reviewer`, `docs-audit`,
  `researcher` and `test-writer` load only in Claude Code, so `delegate_to_agent` reaches
  one of five agents here. Moved out of Deferred on 2026-09-18 on the evidence in b.
    - a. ✅ Deferred from M10 on 2026-09-10: the entry named `docs-audit-local` as the shape to
    copy and `#159` split it into an agent plus a runbook, so that shape no longer exists.
    Re-derive what a twin is before committing four of them.
    - b. ✅ **Measured 2026-09-18: the write tools are used, almost never to write.** 83 of 541
    recorded calls reached `delegate`/`delegate_to_agent`; three tasks ever wrote anything, all
    on one morning and all because the owner asked. Unprompted it has never happened once.
    - c. ✅ `test-writer` is the twin to do first: writing tests is the kind of work the routing
    rule sends to a project agent, and the one kind with no agent to send it to. The others are
    read-only and already have working read-only routes.

54. ✅ 2026-09-18 **Nothing tells a session to delegate writing, and two things discourage it.** CLAUDE.md
  forbids reading in the main conversation and says nothing about writing there; session-execute
  names the write tools only as a cost — "chain at 120s" — then says "Delegate the reading".
    - a. ✅ The friction is real: a read-only tool is declared read-only so a gating
    client runs it unasked, where a write-capable one needs a keypress. Repo half landed
    2026-09-18 (#234); the operator applied the global half the same day.
    - b. ✅ Not fixed by 53, which gives writing work an agent to route to but does not change what
    the instructions say. The 2026-09-07 calls show the capability already works: a regression
    module and two owning documents, three calls, nothing recorded as a failure.
55. ✅ 2026-09-21 **Concurrent server processes all number their transcripts `0001`**, `_COUNTER` being
  process-local, so a same-millisecond same-slug pair is one filename: the `.json` truncates
  and the `.jsonl` appends, interleaving two streams (JOURNAL 2026-09-19)
56. ✅ 2026-09-19 **A delegation runs from the command line**, `run --task`, because the 120s
  stagger is the client's own queue and no server-side change reaches it — six arms span 88ms
  against 600s, and a result lands in a file rather than the caller's window (ADR-0092)
57. ✅ 2026-09-21 **The idle hold fixes one member of a burst, not the burst.** It fires only where
  `seqs` and `waiting` are both zero, so a simultaneous six priced 2,3,4,5,6,6 — no 1 and two
  6s is the hold moving exactly one arm, measured 2026-09-19 over the out-of-process fan-out
    - a. ✅ 2026-09-19 50 closed this for a burst the *client* staggered. Removing that stagger
    makes the burst simultaneous and the under-count returns in a shape the hold does not
    reach, so the question is whether a member prices on the gate it joins or the one it meets
    - b. ✅ 2026-09-21 **The cross-process half is what remains.** A member takes an open wait's answer
    within one process; the slots file does not carry that a wait is open, so three arms from
    three processes still price 2,3,4 — measured, and the counting itself now reads shared
58. ✅ 2026-09-19 **A cancellation during the idle hold took a slot nothing released.** The
  slot is taken before the wait and `admit` releases only a lease it has been handed, so a
  server lost one per cancellation until `max_inflight_seqs` of them closed the gate for good
60. ✅ 2026-09-22 **An empty rate memory prices from a blend that may not exist either.** `expect`
  returning None falls through to the since-boot figure at face value — 41.7% over (ADR-0094),
  1.745x on 2026-09-19 — and a newly served model has no such figure at all (JOURNAL)
    - a. ❌ 2026-09-22 **Spike: what does the since-boot rate read on an engine minutes old**, and does
    the stamp's own discard land a delegation in exactly this state? Neither is measured
61. ✅ 2026-09-22 **Price from periodic cluster samples, and from the bucket mean not its minimum.**
  A per-turn sample is filed under `expected_concurrency`, frozen at lease grant, and carries
  whatever contention that turn met; the minimum then makes one bad minute the price for 64
  samples. Measured 2026-09-20: bucket means match the operator benchmark to 1-3% at every
  well-populated concurrency, the minima sit 24-71% below it (JOURNAL)
    - a. ✅ 2026-09-22 Scrape on a ticker while `inflight_seqs > 0`: `_DecodeWindow` already differences
    `generation_tokens_total`, so rate and concurrency come from one reading
    - b. ✅ 2026-09-22 **Not what 14.b refuted** — that rejected the window as a *ceiling*; this makes it a
    *sample* the bucket and the margin still act on. The mean is safe only once samples are regular
63. ✅ 2026-09-21 **A pass that loops inside one turn is invisible to every control there is.** `max_turns`
  cannot act on a turn that never ends, and `finish_reason: length` with `reasoning_exhausted`
  false is what a healthy long answer looks like. Temperature 0.2 was the cause (JOURNAL)
    - a. ✅ 2026-09-21 A duplicate-line share over the reply separates a loop from work in one number, and
    nothing computes one — 66-94% on every looping pass against under 1% on every reporting one
    - b. ✅ 2026-09-21 **The sampling parameters are not reported anywhere**, so the setting a turn was
    drawn at cannot be recovered from a transcript. It belongs in `priced`, beside the budget
64. ✅ 2026-09-21 **`read_file` says where to start but not where to stop.** A pass wanting one section
  takes ~650 lines to get 60, and a tool result is resent every turn, so the waste multiplies
  by the turns left. `end_line`, inclusive — not `line_count`, whose off-by-one is silent
    - a. ✅ 2026-09-21 **Number the prefetch block too.** Whole-file delivery already exists and costs no
    turn; what it lacks is the addressability that made `read_file` delivery terminate
65. ✅ 2026-09-21 **Only `temperature` is ever sent, and it is 0.2 on every loop turn.** No `top_p`, no
  penalty, against an evaluated 1.0 / 0.95 for this model — and the penalties are accepted by
  the endpoint but never used. 0.2 is what made five audit passes loop (JOURNAL)
    - a. ✅ 2026-09-21 0.7 is a working point rather than a validated one, and `top_p` exists nowhere in
    `src/`, so the evaluated pair cannot be reached without adding it
66. ✅ 2026-09-21 **A pass that hits `max_turns` answered under duress, and nothing treats it as partial.**
  `hit_turn_limit` already marks it; every audit record since 2026-09-18 has noted the weakness
  and none has acted on it. A forced answer is evidence of a different kind, and should say so
67. ✅ 2026-09-21 **`turn_timeout` is the reply ceiling's denominator and may not be earning it.** Doubling
  it 1800→3600 took the ceiling 18,905→31,793 and bought 347 seconds and 111 tokens (JOURNAL
  2026-09-20). Either it leaves the `min()` and stays a plain deadline, or it goes entirely
68. ✅ 2026-09-22 **Spike, and it outranks 60 and 61: is the reply budget needed at all?** It exists to keep
  a reply inside the deadlines. Stall now counts only silence and cannot kill a producing turn
  (JOURNAL 2026-09-21), so if 67 removes `turn_timeout` the thing it avoids may not remain
    - a. ✅ 2026-09-22 Settle before building 60 or 61 — a positive answer deletes both, and ADR-0055 with
    them. Proposed twice on 2026-09-20 and recorded nowhere until now, which is why it is here
