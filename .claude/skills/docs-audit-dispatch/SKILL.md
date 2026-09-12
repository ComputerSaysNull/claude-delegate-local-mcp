---
name: docs-audit-dispatch
description: "Runs the complete documentation audit for this repository by dispatching the docs-audit-local agent, one check class per pass. Holds the pass list, the check-class definitions, the per-pass effort levels and the concurrency rule. Use when the docs gate reports audit-due pressure, before a release, or when asked to run a docs audit."
---

# Documentation audit runbook

Thirteen passes complete one audit. Running the same thirteen every time is what makes two
audits comparable, so run them all and record the ones you skipped.

```
Audit progress:
- [ ] 1  STALE            docs/ARCHITECTURE.md  vs context, admission, slots
- [ ] 2  STALE            docs/ARCHITECTURE.md  vs transcript, server
- [ ] 3  STALE            docs/DISPATCH.md      vs loop, turn and history sections
- [ ] 4  STALE            docs/DISPATCH.md      vs backends, wire and retry sections
- [ ] 5  STALE            docs/AGENTS.md
- [ ] 6  STALE            docs/MODELS.md + docs/TROUBLESHOOTING.md
- [ ] 7  TOO VERBOSE      project plane
- [ ] 8  TOO VERBOSE      product plane
- [ ] 9  WRONG DOCUMENT   every document
- [ ] 10 CROSS-PLANE LEAK every document
- [ ] 11 MISSING          every document
- [ ] 12 CLAIMS           every document
- [ ] 13 ESCAPE ABUSE     history only
```

## Before you start

1. Run `python scripts/docs_gate.py --mode pre-commit` and keep the output. A pass cannot
   obtain it — the sandbox denies both the gate and shelled git — so paste it into every task.
2. Note the commit count the gate reports. It goes in the record's opening line.

## The passes

Dispatch with `delegate_to_agent_readonly`, agent `docs-audit-local`, and pass `project` as
the repository root or the agent is not found. Generated documents (`docs/CONFIGURATION.md`,
`docs/TOOLS.md`) are never audited — a generator already guarantees them.

| # | Class | Prefetch in `files[]` | Effort |
|---|---|---|---|
| 1 | STALE | `docs/ARCHITECTURE.md` + `context.py`, `admission.py`, `slots.py` | high |
| 2 | STALE | `docs/ARCHITECTURE.md` + `transcript.py`, `server.py` | high |
| 3 | STALE | `docs/DISPATCH.md`, *the turn and history sections* + `loop.py` | high |
| 4 | STALE | `docs/DISPATCH.md`, *the wire and retry sections* + `backends/*.py` | high |
| 5 | STALE | `docs/AGENTS.md` + `agents.py`, `paths.py`, `sandbox.py` | high |
| 6 | STALE | `docs/MODELS.md`, `docs/TROUBLESHOOTING.md` + `registry.py`, `doctor.py` | high |
| 7 | TOO VERBOSE | `CLAUDE.md`, `CONTRIBUTING.md`, `README.md` | high |
| 8 | TOO VERBOSE | every hand-written `docs/*.md` | high |
| 9 | WRONG DOCUMENT | every document, plus `config.py` | high |
| 10 | CROSS-PLANE LEAK | every document | high |
| 11 | MISSING | every document, plus `PLAN.md` and `archive/PLAN-milestones.md` | high |
| 12 | CLAIMS | every document, plus `DECISIONS.md` and `JOURNAL.md` | high |
| 13 | ESCAPE ABUSE | nothing | low |

**Name the document sections for a STALE pass over a module above ~1,000 lines.** A whole
large document against a whole large module is an enumerable question wearing a narrow one's
clothes: it asks for every claim to be checked, so the reply budget goes on the checking and
nothing is reported. Passes 3 and 4 are one such pair, split by section.

Effort is `high` everywhere but pass 13. An audit is a search across kinds of violation, and
`low` narrows it to retrieval — more instances of one violation, fewer kinds. Pass 13 *is*
retrieval, so `low` is both correct and cheaper there.

**Split by check class, never by document.** Four of the seven classes cannot see a split by
document: WRONG DOCUMENT and CROSS-PLANE LEAK need every document that could hold the
restatement, MISSING needs every document or absence cannot be established, and CLAIMS needs
`DECISIONS.md` and `JOURNAL.md` alongside. A pass that cannot see the other copy reports
nothing and looks clean.

## Task template

Fill the two slots. The definition is what makes the task self-contained, so paste it in full.

```
Run the <CLASS> check class only, over <SCOPE>. Everything you need is in your files[]
block. The repository root is <ABSOLUTE PATH>.

<CLASS> means: <the definition from the table below, verbatim>

Gate output for this run, which you cannot obtain yourself:
<paste it>

Use the output format your file specifies.
```

## Check-class definitions

**STALE** — the document describes behaviour the code no longer has. Quote the document and
quote the code that disagrees. The highest-value class, and the reason this agent exists.
Only report a disagreement you can point at in a file you were given.

**TOO VERBOSE** — a section that could say the same in fewer lines without losing meaning.
Propose the trimmed version; do not merely complain about length. Never cut a fact, a caveat,
a measured number or a stated reason — this project keeps the *why* deliberately. If a
document is dense rather than padded, say so and name the seam you would split on.

**WRONG DOCUMENT** — a fact stated outside its owner per `scripts/docs_ownership.toml`. The
one that matters most is a configuration default — a field of the `Config` dataclass in
`config.py`, published through the generated `docs/CONFIGURATION.md` — restated as a number
in prose elsewhere. Naming a setting and linking to it is the prescribed pattern and is not a
finding. A constant defined in another module is not a configuration default.

**CROSS-PLANE LEAK** — a fact stated substantively in both the project plane (repository
root) and the product plane (`docs/`). A link is not a leak. A shared term of art is not a
leak. Only a restatement of the same substance counts.

**MISSING** — a module or behaviour with no documentation coverage at all. Check `PLAN.md`
and `archive/PLAN-milestones.md` first: not-yet-built is not undocumented.

**CLAIMS** — documentation asserting a measurement that no ADR, JOURNAL **or CHANGELOG**
entry substantiates. Quote the substantiating sentence where there is one. Numbers decay,
and an unsourced one cannot be rechecked.

CHANGELOG.md counts, and forgetting it is how this check misfires. A measurement recorded in
the section for the pull request that made it *is* sourced — that is where every change's
why lives, by CONTRIBUTING's rule — and a pass told to look in two of the three places
reports it as unsourced. The 2026-09-06 audit retracted exactly such a finding and diagnosed
it as a caller error; the instruction was not changed, so 2026-09-11 reported four more, one
of them the same measurement. Search all three before calling a number unsourced.

**ESCAPE ABUSE** — gather the waivers with `read_git`: `log` over the last ninety days for
`Docs-Gate-Skip:` trailers, grouped by the document each names. Any document waived more than
twice in ninety days is a signal that the document is wrong, not the rule. Count events, not
trailers — two waivers in one commit for one reason are one event.

## Concurrency

`max_inflight_large_prefills` bounds how many **large** passes run at once. A pass is large
only when its estimated prefetch exceeds `large_prefill_tokens`; below that it does not
contend at all, so any number of small passes may overlap. A pass over the bound waits, and
one waiting longer than `admission_wait_timeout` is failed rather than queued indefinitely.

Read all three values from [docs/CONFIGURATION.md](../../../docs/CONFIGURATION.md) and do
not assume them — they are configuration, and this file is not their home.

In practice passes 1-6 and 9-12 prefetch whole modules and are large, so check the bound
before overlapping them. Pass 13 prefetches nothing and never contends.

## Sizing

Judge a pass by expected findings, not by input size — prefetch is cheap and replies are not.
The reply budget is derived from the observed decode rate, so it shrinks as the cluster
fills. An oversized pass does not come back truncated; it comes back **empty**, reporting
success, and raising `max_tokens` cannot help because the deadline-derived ceiling applies
over an explicit argument too.

An *enumerable* question is what blows the budget, not a broad one. "List every recognised
key and what each does" spends the whole reply enumerating; "does this paragraph still match
this function" does not. Phrase every pass as the second kind.

A pass that keeps verifying never reports. The agent is told to verify once and then write,
so do not add "check every quotation" to a task — it reinstates the loop that instruction
exists to prevent.

## Recording the result

Write `docs/audits/<date>-audit.md`: a `BUDGET:` header, the commit count that raised the
audit, the scope, what was **not** covered, then the findings. `docs/audits` is declared
unowned, so the record needs no owning-document update — but any fix landed from a finding
does.

Re-verify every finding against source yourself before writing it down. A pass reports what
it believes; the record states what is true. An audit whose verification never overturns
anything is not verifying.

Committing the record resets the gate's audit-due pressure.
