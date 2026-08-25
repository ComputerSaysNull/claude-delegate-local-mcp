<!-- BUDGET: 400 -->
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
