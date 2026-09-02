<!-- BUDGET: 439
     Raised from 413 on 2026-09-02: the one Deferred and cancelled section became Open,
     Deferred and Cancelled, because twelve live items were being filed as deferred. Three
     headings and their leads; no new items.
     Raised from 354 on 2026-09-02: eleven deferred items from the 2026-09-02 review, each carrying the reason it was not built and, where the review's own recommendation was wrong, why.
     Raised to the size it had already reached on 2026-09-01: the check that should have held this
     line was disabled from 2026-08-28, when reasons moved inside this comment and the pattern
     stopped matching, so the document grew unenforced. This records where it actually is rather than
     endorsing it; the 2026-09-01 audit tracks the trim. Raised from 245 on 2026-08-29: M5 gained a
     ninth item. Un-withholding `run_bash` opens the route the whole milestone exists to open, and it
     was tracked only in a handoff note, which is not a place a plan can be read from. -->
<!-- Raised from 220 on 2026-08-28: the file sat at exactly 220 of 220 with ten M4
     items still to annotate on completion. PLAN.md grows monotonically by design --
     ADR-0003 keeps completed items with their annotations and cancelled ones with
     their reasons -- so its length tracks the milestone count rather than settling.
     If it recurs the answer is archive/, which check_budgets skips, not a raise. -->
# Plan

The roadmap. One line per item, status first so the file scans.

`✅` done, dated · `🔄` in progress · `⬜` not started — queued under **Open**, on hold
under **Deferred** · `❌` cancelled, with date and **reason** — cancelled items stay,
because the fact that something was considered and dropped is worth more than a tidy list.

A date, not a hash. The fifteen 2026-08-25 items still cite one; nothing since does, for
the reason CHANGELOG.md's header gives, and per-PR provenance lives there instead. Six
M4 items carry no date either, and it was not recoverable after the fact.

`STATUS.md` is generated from this file. Edit here; never edit that.

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

## Open — hardening, testing and troubleshooting

The milestone plan closed with M7; this is what is queued now. Ordered within each
group by what the item's own annotation says it costs.

### Security review, 2026-09-02

- ⬜ Operator allowlist for an agent's `network` and `extra_binds` — the only validation is
  `os.path.isabs` and a boolean parse, so a markdown file in a repository you delegate over
  can bind any absolute host path read-only and turn on egress for that call. The
  mount-level secret scan covers matches afterwards (ADR-0036) rather than refusing the
  bind. Of everything the 2026-09-02 review raised this is the one with a plausible
  end-to-end attack, and `workdir_roots` is the pattern to copy
- ⬜ Validate the opened inode, not the path — `resolve_all` then `open` is check-then-use
  in both file tools and in prefetch, with no re-validation. The adversary is the delegated
  model itself, which holds a read-write workdir bind under `run_bash` and can retry, so
  this is not the passive window the review described. `O_NOFOLLOW` is the wrong fix: it
  would refuse legitimate symlinked checkouts, since `realpath` has already collapsed them
  by design. Compare `realpath("/proc/self/fd/N")` against the roots after opening
- ⬜ Content-level detection for a renamed secret — every path-policy layer inspects the
  path and none the bytes, so `config.json` holding a private key passes all of them and is
  inlined, and `run_bash` can read one the mount-level scan did not match by name. One
  finding, not two: fixing the detection fixes both ends. **Not** by pointing `scan_text` at
  it, which the 2026-09-02 review recommended — that scanner looks for RFC1918 addresses,
  private-DNS suffixes and non-allowlisted emails, and would false-positive on the source a
  review delegation exists to read. A narrow, high-precision check for key material instead
  (PEM armour, `BEGIN OPENSSH PRIVATE KEY`, cloud key prefixes)
- ⬜ Resource limits inside the sandbox — `build_argv` emits no `--rlimit`, no process cap
  and no `--size` on either tmpfs, and the module never imports `resource`. A fork bomb or
  a runaway allocation is bounded only by `run_bash_timeout` and `--die-with-parent`. This
  machine's page file is capped by choice, so a demand-side OOM is the live failure mode
  rather than a theoretical one
- ⬜ Escape a file's own END marker in the files block — `context.py` wraps each file in
  `--- BEGIN FILE <path> ---` / `--- END FILE <path> ---`, deliberately not a markdown
  fence, but the body is not escaped against those markers. A hostile file being reviewed
  can forge an end-of-file boundary and speak as the prompt. Concrete and testable, unlike
  prompt injection in general; the tool-level policy remains the real defence
- ⬜ Refuse a non-stdio transport outright — `config.py` already says adding the HTTP
  transport "is a real integration task, not a flag flip", yet setting it runs a server. A
  knob advertised as unfinished that still starts is the shape ADR-0034 deleted
  `sandbox_enabled` for. The review called it unauthenticated *and* unbound; measured,
  FastMCP defaults its host to loopback and `main.py` passes only a port, so the reachable
  surface is other local processes rather than the network, and no token is the true half
- ⬜ `security/secret_globs.txt` claims a reach it does not have — its header calls the list
  the single source of truth for "never let a model see this, never let git take it", and
  CLAUDE.md repeats "one list, two enforcers". The git half is a hardcoded `NEVER_TRACK`
  set in the gate, not derived from the globs, so the second copy the rule warns about
  already exists. Demonstrated: `.env.example` is tracked and matches `.env.*`, and the list
  matches its own `*secret*` — the path policy refuses both and the gate objects to neither

### Documentation accuracy

- ⬜ The documentation trim the 2026-09-01 audit listed — `docs/ARCHITECTURE.md`,
  `docs/DISPATCH.md`, `docs/AGENTS.md` and `PLAN.md` all sit at their ceilings, and the
  review fixes of 2026-09-02 needed three budget raises across two documents to state facts
  the code had just acquired. Each feature now pays interest on it. Worth doing before the
  next one rather than after
- ⬜ Say in `docs/DISPATCH.md` that the admission wait stacks on the dispatch deadline —
  the deadline is taken after a slot is granted (ADR-0038), so the caller-visible worst
  case is both settings added. `admission_wait_timeout` does not appear in that document at
  all, and its deadline section enumerates three enforcement points without mentioning
  admission. Recorded in M7 and in the ADR, which is not where a reader looks
- ⬜ Re-measure `BYTES_PER_TOKEN` per tokenizer and say so in `docs/MODELS.md` — measured
  against one model. Lower priority than the 2026-09-02 review implied: the table is
  per-extension and every entry is rounded **down**, so estimates over-count and the
  conservative direction survives a different tokenizer unless it is denser than the
  densest entry. A sentence, not a project

### Improvements

- ⬜ A heartbeat for the agentic loop — it reports at the top of each turn, so one turn is
  silent for its whole duration, bounded only by `turn_timeout`, which defaults to exactly
  the client's 1800s idle timeout. `#58` measured what that silence costs: the caller
  abandons the call, nothing reaches the server, and the slot is held to the end. Lowering
  the default would kill legitimate work — one call generated for 1645s — so the fix is to
  give the loop the one-shot's heartbeat, not a smaller budget. `#59`'s guard cannot reach
  this path: it bounds the interval, and this path has no heartbeat to bound
- ⬜ Server-format twins for the four Claude Code agents — `#72` made it visible that
  `code-reviewer`, `docs-audit`, `researcher` and `test-writer` load only in Claude Code,
  so `delegate_to_agent` can reach one of five agents in this repository. `docs-audit-local`
  is the shape to copy (`#67`). CONTRIBUTING.md already records the two-format arrangement
  as temporary; this is what it costs

## Deferred

On hold for weeks or months. Not cancelled, and not queued.

- ⬜ Anthropic-compatible adapter — the seam and canonical shape are kept so this is
  additive, roughly 150 to 220 lines in one new file (ADR-0008)

## Cancelled

- ❌ 2026-08-25 Streaming in v1 — cancelled. MCP tool calls are request/response, so
  Claude sees nothing incrementally either way. Progress notifications, which are
  required for the idle timeout, cover the part that actually matters. ADR-0018
  — **worth revisiting: the premise moved.** That reasoning weighed one consumer, the
  caller, and it still holds for the caller. ADR-0043 added a second one six days later:
  the transcript stream, which a person reads *while* the delegation runs. Streaming
  would let it carry tokens as they are generated rather than a heartbeat saying only
  how long it has been. Not reopened here, because nobody has yet wanted it enough —
  recorded so the next reader knows the cancellation was decided without this consumer
  in view rather than despite it.
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
