<!-- BUDGET: 220 -->
# Upstream review — 2026-08-26

First review under ADR-0023. Decision recorded in ADR-0024; this file is the evidence.

Reviewed read-only through the GitHub API. Nothing was fetched, so no upstream ref or
object entered this repository.

| Repository | Role | Last push | Verdict |
|---|---|---|---|
| `fegone/claude-code-delegate-local` | Primary ancestor. What the `upstream` remote points at. | 2026-08-21 | **Nothing new.** |
| `mixicz/claude-code-delegate-local` | Fork of the above. The one still moving. | 2026-08-25 | **Reviewed below**, 2026-08-21 to 2026-08-25. Tagged v0.8.0. |

The ancestor's newest work is its turn-countdown notices and real-exit-code capture, both
already credited in NOTICE as ported. The fork's 2026-08-20 batch — frontmatter binding
the dispatch, per-agent reasoning effort, per-agent tool restriction at both sites, the
workdir-root allowlist, the bubblewrap sandbox — is also already in NOTICE. Everything
from 2026-08-21 onward had never been looked at.

## Adopted

### 1. Context-overflow handling → M3, promoted out of Deferred

Small-context backends were silently truncating conversations and returning a clean-looking
finish reason. Two mechanisms, both off by default:

- **Retroactive detection.** Prompt size plateaus or shrinks while history grows, and the
  server evicted nothing that would explain it. Abort and say so.
- **Preventive graduated response.** At 70% of projected usage, tighten tool-result
  retention. At 85%, inject a wrap-up nudge. At 95%, hard abort.

This project had already parked the same idea, and the deferred wording had converged on
the same detection signal independently — "warn when prompt tokens stop growing while
history does". That agreement is the reason to trust the design.

**The five bugs it cost them.** These are the transferable part, and they are carried as
constraints on the PLAN item. Three were caught in review before merge:

1. The retroactive check fired on the server's **own** evictions — the one case that has an
   innocent explanation, and the check had to exclude it explicitly.
2. A stale threshold band fired on ordinary growth, before anything was near a limit.
3. A flat reserve, on its own, exceeded 95% of a small context window — so the hard abort
   triggered immediately on exactly the backends the feature exists to protect.

Two more survived review and were found only in live testing:

4. The auto-probe read the model file's **architecture maximum** instead of the configured
   window. Those differ by a large factor, so every threshold was computed against a
   ceiling the backend would never reach.
5. A local-only gate also blocked the **explicit** override from applying to cloud
   backends — an auto-detect restriction leaking onto the manual setting beside it.

Bugs 1, 2 and 4 share a shape worth naming: each computed a threshold against the wrong
denominator. Whatever this project builds, the denominator is the thing to test.

### 2. Diagnostics on success, not only on failure → M3, M7

An opt-in per-call flag extends the response with the full action ledger on success as well
as failure, a per-turn token and eviction breakdown, and an **evicted-then-reread
correlation**. Session-level cumulative counters ship unconditionally.

The correlation is the valuable half. It separates a dispatch that was genuinely expensive
from one that kept re-reading a file it had already had evicted — which look identical in a
token total. It is also the measurement their own backlog says is needed before eviction
priority can be sized at all, so it must land before, not after, any eviction tuning.

This is ADR-0007 extended from exit codes to context economics: the same argument that
server-captured truth beats the model's account of it. No config field exists here yet.

### 3. A steer at the risky call → M5, M4

Models were using in-place stream editors and here-documents through the shell to patch
files instead of the write tool. Their evidence: three consecutive failed dispatches, one
of which corrupted a file when a truncated here-document put a raw newline inside a string
literal.

The fix that worked is narrow, and the shape is the lesson:

- The note is appended to **that tool call's own result**, not to the system prompt. An
  instruction in the prompt did not stop the pattern on retry — the reminder has to arrive
  at the moment of the risky call.
- It fires **only when the dispatch's allowed tools actually include the write tool**.
  Steering an agent toward a tool it was never given is worse than silence.
- It is advisory. The command still runs. A guardrail that blocks legitimate shell work
  would be disabled within a week.

The second point interacts with the two-site enforcement invariant in CLAUDE.md: the steer
must consult the same resolved tool set the executor enforces, not the declared list.

### 4. An operator-level dispatch transcript → M7

Each dispatch's raw transcript written to a per-session directory, keyed by sequence and
agent name, gated on a directory being configured, and **stripped from the response**.
Deliberately independent of the caller-facing diagnostics flag: what the operator can audit
should not depend on what the calling session chose to ask for.

**The two bugs a whole-branch review caught**, both of which are this project's shape:

- Failure paths did not carry the agent name, so failed dispatches logged as `unknown` —
  precisely the case the feature exists to make legible.
- The success-path merge leaked the entire abort-extras payload into ordinary successful
  responses whenever the log directory was set, contradicting the design's own "does not
  change the response shape" claim.

The second is a reminder that "off by default" is not the same as "inert when on".

### 5. Two constraints, not features → M3, M4

- **A negative cache with no expiry.** One transient backend outage permanently disabled
  their context-overflow handling until the server restarted. Negative caches expire.
- **A nudge reply overwrote the model's real answer** when the reply was a short
  confirmation. A follow-up concatenates; it never replaces.

## Rejected — already covered, and why

### Generation-ceiling environment variable

Upstream promoted a hardcoded output ceiling to an environment variable. `config.py`
already has that field, at the same value, so nothing is missing.

One thing does transfer, as an M4 constraint: their variable sits at the **lowest**
precedence rung, below the per-model bump that exists to stop heavy-reasoning models
returning empty output. An operator lowering a deployment-wide ceiling must not silently
suppress that bump.

### Explicit wire-format override

Added upstream after a model name matched none of their recognised prefixes and sent every
dispatch to the wrong endpoint shape — which presented as a model failure rather than a
routing bug. ADR-0009 removed prefix matching from this project entirely in favour of an
explicit registry, so there is no guess for an override to correct.

Recorded rather than dropped: a rejection that names its reason is what stops the next
review re-litigating it.

## Recorded — a negative result worth more than most adoptions

Their early-cancellation investigation closed with **no server-side action possible**:

- The client receives progress notifications but does not surface them to the orchestrating
  session, so an orchestrator cannot see a thrashing dispatch in time to react.
- There is no mechanism to cancel an in-flight **synchronous** tool call. Cancellation
  covers backgrounded shell processes and forked subagents only, and while the protocol
  defines a client cancellation notification, the client does not send one.

This independently confirms ADR-0018's narrow claim: a per-turn progress notification buys
an idle-timeout reset and nothing else. Do not reopen this without evidence that the client
has shipped progress display or cancellation.

## Noted, not adopted

- **An orchestration skill template.** Agree a retry cap and a fallback before dispatching;
  review the real diff rather than the delegate's self-report; the orchestrator does not
  author code. Aligns with ADR-0007 but is process, not product.
- **Two weak spots in their own backlog**, useful as foresight rather than as work:
  de-duplication may match only byte-identical tool-and-argument pairs and miss a re-read
  of the same file at a different offset; and eviction is oldest-first regardless of size,
  so a large read is discarded at the same priority as a one-line directory listing. Both
  are design choices this project can avoid making rather than fix later.

## Next review

Per ADR-0001, the usefulness of this decays with a half-life of roughly six to twelve
months. The fork is the one to check; the ancestor has been dormant since 2026-08-21.
