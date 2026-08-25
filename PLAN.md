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
- ⬜ `docs/TOOLS.md` generator — tool descriptions rendered from the registered tools
- ✅ 2026-08-25 `scripts/docs_ownership.toml` — activates the owning-doc, orphan and split-dodge checks. TOML, not YAML: tomllib is stdlib, so the gate runs from a bare clone
- ✅ 2026-08-25 `scripts/gen_status.py` — STATUS from this file plus git — `58f543c`
- ✅ 2026-08-25 Four build-time agents in `.claude/agents/`, models and effort set per task cost
- ✅ 2026-08-25 CI: gate, tests on 3.11 and 3.12, gitleaks with an allowlist-based email rule
- ✅ 2026-08-25 Audit trigger — the gate raises `audit-due` from git evidence: a document
  unchanged across 12+ commits touching its owned code, or 60+ commits since the last
  recorded audit. Warns, never blocks
- ✅ 2026-08-25 Public repo created, ruleset applied to `main`, secret scanning and push protection enabled. Direct push to `main` verified as refused

## M1 — One real backend call

- ⬜ `backends/base.py` — `Backend` protocol, canonical request and response
- ⬜ `backends/openai_compat.py` — the only adapter shipped
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

## M4 — Agentic loop and model tools

- ⬜ `tools.py` — read_file, write_file, run_bash; `allowed_tools` enforced at execution
- ⬜ `loop.py` — turns, eviction, dedup, countdown, final-turn short-circuit
- ⬜ Real exit codes captured by the server, reported apart from the model's claims
- ⬜ Progress notification per turn — required, not cosmetic (ADR-0018)

## M5 — Sandbox

- ⬜ `sandbox.py` — empty root, the corrected symlink set, bind-order rules
- ⬜ Toolchain binds so `uv` resolves inside the sandbox
- ⬜ Refuse to run when bwrap is absent
- ⬜ Secret denylist enforced at the mount level
- ⬜ Tests: bind order for all three HOME/workdir cases; denial verified **by address**
- ⬜ Test that the sandbox dies with its parent

## M6 — Agents, batching, discovery

- ⬜ `agents.py` — three-tier lookup, frontmatter validated and actually binding
- ⬜ `delegate_to_agent`, `delegate_batch`, `list_agents`
- ⬜ Workdir root allowlist, symlink escape closed

## M7 — Admission control and polish

- ⬜ Token-budget admission, three rules, high-water marks and wait totals
- ⬜ Cross-process slots
- ⬜ README launcher documentation, `scripts/release.py`

## Deferred and cancelled

- ⬜ Context-overflow handling — detection is nearly free (warn when prompt tokens stop
  growing while history does); graduated deterministic eviction, never summarisation; on
  abort, a state report reconciling the model's ledger against `git status` ground truth
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
