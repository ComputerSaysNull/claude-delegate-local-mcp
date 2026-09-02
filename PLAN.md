<!-- BUDGET: 200
     Reset on 2026-09-02 from 477, when M0a-M7 and Extra moved to
     archive/PLAN-milestones.md. The raise history it replaces described a document more
     than twice this size, and the reason for each raise is in the CHANGELOG.md section for
     the pull request that made it. Completed items stay here: they are a useful reminder of
     what the last session did. Passing this line is a prompt to check whether any of them
     are ready to archive, not a requirement to move them, and not a reason to raise. -->
# Plan

Open work, one line per item, status first so the file scans.

`✅` done, dated · `🔄` in progress · `⬜` not started — queued under **Open**, on hold
under **Deferred** · `❌` cancelled, with date and **reason** — cancelled items stay,
because the fact that something was considered and dropped is worth more than a tidy list.

Completed items are annotated and stay here (ADR-0003), as a record of recent work.
[archive/PLAN-milestones.md](archive/PLAN-milestones.md) holds the closed milestone roadmap
M0a through M7 and the work found alongside it; anything else moves there only when someone
decides it should.

---

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
- ⬜ Measure whether the tool schemas sit inside the cached prefix, and record it in
  ADR-0011's terms — the ADR fixes the prompt order and requires the leading tokens to be
  bit-identical, naming timestamps and counters as what breaks it. It never says whether a
  tool description is inside that region. If it is, rewording one costs prefill on every
  later call; if it is not, it costs nothing, and the difference decides how freely the
  model-facing contract can be edited. Reasoned about so far, never measured

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
- ⬜ Globs and a search term in `files[]`, expanded server-side — `delegate_readonly` fixes
  `allowed_tools` at `[]`, so it can look for nothing and every file must be named in
  advance. Expanding before the call keeps that promise literally true: no tools, no loop,
  and the static prefix ADR-0011 protects untouched. The work is in the budget rather than
  the matching — a glob hitting two hundred files has to skip and account for them the way
  `context.prefetch` already does, not spend `prefetch_budget` silently
- ⬜ A read-only search tool, for the research a glob cannot do — grep, read a hit, grep
  again. This is the half that needs iteration, so it means giving `delegate_readonly` tools
  and with them the agentic loop, which ADR-0016 already prefers and which no ADR argues
  against; the one-shot is a consequence of the empty toolset being falsy, not a decision.
  ADR-0042's promise survives, because it is that nothing the tool can do will write rather
  than that it has no tools — MCP matches permissions on the name and never on the
  arguments. Its annotation and heading both need rewording, which is the behaviour change
- ⬜ Line addressing in `read_file` — `offset` counts characters, so the model can neither be
  pointed at lines 400 to 460 nor cite what it read. Found in review and never filed here,
  because it was absorbed as a workaround instead: `.claude/agents/docs-audit-local.md`
  tells that one agent to cite by quotation and warns it is never shown a line number. Every
  other caller still asks for line numbers and gets hand-counted ones
- ⬜ `edit_file`, on a helper that validates and opens together — `write_file` replaces a
  file whole, so changing three lines in a long module means reading it back in
  `max_read_chars` windows and rewriting every line, with any hallucinated character landing
  silently. Together with no search and no line addressing, this is the likeliest reason no
  code-writing task has been delegated yet. It must not land before the inode item above:
  `_one_path` returns a string and each handler opens for itself, so a read-modify-write
  tool validates once and uses the path twice across two `open` calls, against an adversary
  holding a read-write bind under `run_bash` who can retry until the swap lands. One
  `open_resolved` helper closes that item and makes the third tool safe by construction
  rather than by review

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
