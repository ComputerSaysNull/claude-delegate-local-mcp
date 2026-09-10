---
name: docs-audit-dispatch
description: Caller-side guidance for dispatching docs-audit-local -- how to size a pass, why splitting by document disables half the checks, and the two concurrency limits. Read this before invoking that agent. Not a role to dispatch: it has no body a pass would act on.
---

This is the half of the documentation audit that belongs to whoever *dispatches* it, split
out of `docs-audit-local.md` so the agent body carries only what one pass reads. The agent
owns what a check means; this owns how a pass is shaped. Neither restates the other.

Yours to read, not to act on: the agent audits what it is given, and cannot size its own
pass. This was the agent body's fourth section until it moved here, on the reasoning that a
pass reading fifty lines it cannot act on is paying for them every turn. Getting it wrong is
what produced the 2026-09-05 failures.

**Size a pass so its findings fit one reply — and the reply is not a fixed size.** The
budget is `reply_budget_margin x stall_timeout x` the *observed* decode rate (ADR-0055), so
it shrinks as the cluster fills: measured 2026-09-06, from ~44,000 tokens to ~10,800 as
per-sequence decode fell from ~35 tok/s to 8.6. Judge a pass by expected findings, never by
input size — prefetch is cheap and answers are not. An oversized pass does not come back
truncated. It comes back **empty**, with `finish_reason: "length"`, `reasoning_exhausted:
true` and `ok: true`; two did exactly that at 27,603 and 41,364 output tokens. Raising
`max_tokens` cannot help, because the deadline-derived ceiling applies over an explicit
argument too.

**An enumerable question is what blows it, not a big one.** A five-part ask burned 41,831
tokens returning nothing, and narrowing it to one of its five parts failed identically —
while its sibling over a *larger* file answered in one turn. "List every recognised key and
what each does" spends the whole budget enumerating; "does this paragraph still match this
function" does not.

**Run at most two passes at once.** Admission admits only `max_inflight_large_prefills` (2)
cold prefills at a time and a fully prefetched pass is always one, so twelve concurrent
passes on 2026-09-06 lost five to `admission_timed_out … waited 1800.0s` — 12,137 seconds of
queue for nothing. The cap is server-side and tool-agnostic, so a read-only fan-out starves
identically. Concurrency also depresses the decode rate, which shrinks the ceiling above, so
the two failures compound.

**The 120 s ramp belongs to the tool, not to how calls are batched.** Arms sent through
`delegate_to_agent` are released one per 120 s however they are issued; the same arms through
a `readOnlyHint` tool run on independent clocks. Measured both ways from transcripts on
2026-09-06. Batching a fan-out into one message buys nothing, and is never a reason to keep a
pass large.

**Split by check class first, because four of the seven cannot see a split by document.**
This is the trap. Splitting a twelve-document audit into four passes of three looks obvious
and silently disables half the checks:

| check | needs |
|---|---|
| STALE, TOO VERBOSE | one document, plus the code it describes. Splits cleanly. |
| CLAIMS WITHOUT EVIDENCE | one document, plus `DECISIONS.md` and `JOURNAL.md` in every pass. |
| WRONG DOCUMENT, CROSS-PLANE LEAK | every document that could hold the restatement. A pass that cannot see the other copy reports nothing and looks clean. |
| MISSING | every document, or absence cannot be established at all. |
| ESCAPE ABUSE | no documents — only `read_git`. Runs alone and concurrently with everything. |

So: the per-document classes split by document group; the breadth classes take every document
but look for one thing, which keeps their answers small; and ESCAPE ABUSE is its own tiny
pass. A pass told to split by document **must not** be asked for the breadth classes, and if
you are given one that is, say so rather than reporting a clean result you could not have
seen.
