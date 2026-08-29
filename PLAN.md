<!-- BUDGET: 252      Raised from 245 on 2026-08-29: M5 gained a ninth item. Un-withholding
     `run_bash` opens the route the whole milestone exists to open, and it was tracked
     only in a handoff note, which is not a place a plan can be read from. -->
<!-- Raised from 220 on 2026-08-28: the file sat at exactly 220 of 220 with ten M4
     items still to annotate on completion. PLAN.md grows monotonically by design --
     ADR-0003 keeps completed items with their annotations and cancelled ones with
     their reasons -- so its length tracks the milestone count rather than settling.
     If it recurs the answer is archive/, which check_budgets skips, not a raise. -->
# Plan

The roadmap. One line per item, status first so the file scans.

`✅` done, with date and commit · `🔄` in progress · `⬜` not started ·
`❌` cancelled, with date and **reason** — cancelled items stay, because the fact that
something was considered and dropped is worth more than a tidy list.

`STATUS.md` is generated from this file. Edit here; never edit that.

---

## M0a — Spikes

Run before any scaffolding, so a bad assumption could not become load-bearing.
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
action ledger, none of which exist until the turn loop and `tools.py` do, so building them
here would have meant four commits whose only caller was a test and an `off by default`
flag controlling nothing. The design work is not lost — it is recorded under M4.

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
- ⬜ Un-withhold `run_bash` — empty `WITHHELD_TOOL_NAMES` and reword `CURRENTLY REFUSES
  EVERY CALL` in the same commit, or the tool is offered while telling the model not to
  use it. That string is the model-facing contract, so it is a behaviour change. Last:
  the only commit in M5 where a mistake reaches a real shell

## M6 — Agents, batching, discovery

- ⬜ `agents.py` — three-tier lookup, frontmatter validated and actually binding
- ⬜ `delegate_to_agent`, `delegate_batch`, `list_agents`
- ⬜ Workdir root allowlist, symlink escape closed

## M7 — Admission control and polish

- ⬜ Token-budget admission, three rules, high-water marks and wait totals
- ⬜ Cross-process slots
- ⬜ Operator-level dispatch transcript to disk, independent of any caller-facing flag and
  stripped from the response. Upstream shipped two bugs here: failure paths losing the
  agent name, so the very dispatches it exists to explain logged as unknown; and the
  success path leaking its whole diagnostic payload into ordinary responses once the
  directory was set. Off by default is not the same as inert when on (ADR-0024)
- ⬜ README launcher documentation
- ❌ 2026-08-27 `scripts/release.py` — cancelled. Its job was to backfill each CHANGELOG
  entry's commit hash at tag time, and the hash is the wrong thing to cite: `main` is
  squash-merged, so a branch hash names an object that never leaves the clone it was made
  in. The PR number survives and is written by hand in the same edit as the entry, so
  there is nothing left to automate. CHANGELOG.md's format line described this script in
  the present tense while it did not exist -- found by the 2026-08-27 audit

## Deferred and cancelled

- ⬜ Anthropic-compatible adapter — the seam and canonical shape are kept so this is
  additive, roughly 150 to 220 lines in one new file (ADR-0008)
- ❌ 2026-08-25 Streaming in v1 — cancelled. MCP tool calls are request/response, so
  Claude sees nothing incrementally either way. Progress notifications, which are
  required for the idle timeout, cover the part that actually matters. ADR-0018
- ❌ 2026-08-25 Run Claude Code inside WSL — cancelled on workflow grounds, not
  engineering ones. It would delete the path-translation module outright and remove the
  12x test penalty. ADR-0002 keeps the trigger: if development moves onto Linux for
  independent reasons, revisit immediately. ADR-0020
- ❌ 2026-08-25 Dedicated Linux box beside the cluster — cancelled. Solves sandboxing but
  the workspace would reach it only over a share, a sync tool, or a clone, each worse
  than the local bridge and each adding a failure the bridge does not have. ADR-0020
- ❌ 2026-08-25 Scheduled docs-audit workflow — cancelled. It needs an API key, which is
  standing billing exposure for a job that fires whether or not anything changed, and a
  calendar measures the wrong thing. Replaced by the gate's `audit-due` signal
- ❌ 2026-08-25 Collapse reasoning effort to three levels — cancelled. Saves one enum
  value, does not shrink the state machine, and would make our API disagree with the
  backend's documented values. ADR-0013

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
