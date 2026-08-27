<!-- BUDGET: 220 -->
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
- ✅ 2026-08-25 Spike B — tokenizer ratio measured, 1.78 to 4.16 bytes/token across file
  types. Changed the prefetch budget from bytes to tokens. ADR-0019 — `7650b5d`
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
  unchanged across 12+ commits touching its owned code, or 60+ commits since the last
  recorded audit. Warns, never blocks
- ✅ 2026-08-25 Public repo created, ruleset applied to `main`, secret scanning and push protection enabled. Direct push to `main` verified as refused

## M1 — One real backend call

- ✅ 2026-08-26 `backends/base.py` — `Backend` protocol, canonical request and response — `9e03113`
- ✅ 2026-08-26 `backends/openai_compat.py` — the only adapter shipped — `9e03113`
- ⬜ `delegate()` one-shot, no `files[]` yet
- ⬜ `backend_status()` probing `/v1/models` per registry entry
- ⬜ Launched through `wsl.exe` from a real Claude Code config, end to end

## M2 — files[] prefetch

- ⬜ `paths.py` — the four policy layers, each refusal actionable
- ⬜ `context.py` — prefetch, token budgeting, stable prompt ordering for prefix caching
- ⬜ Binary detection, whole-file skip, per-type token accounting

## M3 — Response state machine

- ⬜ Retry and backoff, honouring `Retry-After`
- ⬜ Empty-answer detection, retry at the floor, effort step-down
- ⬜ Feature-detect the `thinking_token_budget` rejection and degrade
- ⬜ Integration test reproducing the empty answer against the live cluster
- ⬜ Context-overflow handling, off by default. Promoted from Deferred 2026-08-26: the
  upstream fork shipped it and our parked wording had already converged on the same
  detection signal (ADR-0024). Retroactive abort when prompt size plateaus while history
  grows *and* this server evicted nothing to explain it; preventive graduated response at
  70/85/95% of projected usage — tighten retention, wrap-up nudge, hard abort. On abort, a
  state report reconciling the model's ledger against `git status` ground truth
- ⬜ Negative tests for the five bugs that cost upstream, three of one shape — a threshold
  computed against the wrong denominator: firing on this server's own evictions; a band
  firing on ordinary growth; a flat reserve alone exceeding 95% of a small window; a probe
  reading the architecture maximum instead of the configured window; a local-only gate also
  blocking the explicit override on cloud backends. The denominator is the thing to test
- ⬜ Diagnostics opt-in per call — action ledger on success as well as failure, per-turn
  token and eviction breakdown, and an evicted-then-reread correlation, which is what
  separates a genuinely expensive dispatch from one re-reading what it lost. ADR-0007
  extended from exit codes to context economics, and a prerequisite for sizing eviction
- ⬜ Two constraints from upstream's bugs: a negative cache expires, or one transient
  backend outage disables overflow handling until restart; a nudge reply concatenates and
  never overwrites what the model already said

## M4 — Agentic loop and model tools

- ⬜ `tools.py` — read_file, write_file, run_bash; `allowed_tools` enforced at execution
- ⬜ `docs/TOOLS.md` generator — descriptions rendered from the registered tools. Moved
  here from M0b 2026-08-26: it cannot render a registry that does not exist yet, and the
  descriptions are the model-facing contract, so they arrive with `tools.py` or not at all
- ⬜ `loop.py` — turns, eviction, dedup, countdown, final-turn short-circuit
- ⬜ Real exit codes captured by the server, reported apart from the model's claims
- ⬜ Progress notification per turn — required, not cosmetic (ADR-0018)
- ⬜ `max_tokens` precedence: call argument, then frontmatter, then the per-model bump,
  then the configured default *last*. An operator lowering the ceiling must not suppress
  the bump that stops heavy-reasoning models returning empty output (ADR-0024)

## M5 — Sandbox

- ⬜ `sandbox.py` — empty root, the corrected symlink set, bind-order rules
- ⬜ Toolchain binds so `uv` resolves inside the sandbox
- ⬜ Refuse to run when bwrap is absent
- ⬜ Secret denylist enforced at the mount level
- ⬜ Tests: bind order for all three HOME/workdir cases; denial verified **by address**
- ⬜ Test that the sandbox dies with its parent
- ⬜ Steer shell text-patching toward `write_file` — a note appended to that `run_bash`
  call's own result, not the system prompt, because upstream found a prompt instruction did
  not stop the pattern on retry. Advisory, never blocking, and only when the resolved tool
  set actually includes `write_file` — the resolved set the executor enforces, not the
  declared list (ADR-0024)

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
- ⬜ README launcher documentation, `scripts/release.py`

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
