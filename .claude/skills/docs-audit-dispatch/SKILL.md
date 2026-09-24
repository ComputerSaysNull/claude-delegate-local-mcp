---
name: docs-audit-dispatch
description: "Runs the complete documentation audit for this repository by dispatching the docs-audit-local agent, one check class per pass. Holds the pass list, the check-class definitions, the per-pass effort levels and the concurrency rule. Use when the docs gate reports audit-due pressure, before a release, or when asked to run a docs audit."
---

# Documentation audit runbook

Sixteen passes complete one audit. Running the same sixteen every time is what makes two
audits comparable, so run them all and record the ones you skipped. Pass 14 is the one you
run yourself.

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
- [ ] 14 FILED ALREADY    the hand-off notebook (run by the caller, not delegated)
- [ ] 15 NARRATIVE        .claude/ bodies — skills and agents
- [ ] 16 STALE            docs/ARCHITECTURE.md vs the viewer
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
| 11 | MISSING | every document, plus `PLAN.md`, `archive/PLAN-milestones.md` and `scripts/docs_ownership.toml` | high |
| 12 | CLAIMS | every document, plus `DECISIONS.md` and `JOURNAL.md` | high |
| 13 | ESCAPE ABUSE | nothing | low |
| 14 | FILED ALREADY | the hand-off notebook — **not delegable**, see below | — |
| 15 | NARRATIVE | every `.claude/agents/*.md`, `.claude/delegate-agents/*.md`, `.claude/skills/*/SKILL.md` and `.claude/skills/*/references/*.md` | high |
| 16 | STALE | `docs/ARCHITECTURE.md` + `scripts/watch_delegations.py` | high |

**Pass 14 is run by whoever is driving the audit, not by a delegation.** The notebook lives
in the plans directory, outside every workspace root, so `files[]` refuses it and the agent
cannot be handed it. That is a path-policy fact rather than a permission to skip the pass:
the notebook is the one document nothing else checks, it is untracked so no gate sees it,
and it is read at the start of every session. Read it yourself and apply the class.

**One class, one document, named modules.** A pass carries exactly one check class, one
document — or named sections of one — and modules listed individually rather than by glob.
Check the call against those three before sending it; a whole document against a whole
subsystem asks for every claim at once, which is the enumerable shape Sizing warns about.
Passes 3 and 4 split one document by section, and pass 8 wants the same treatment.

**Every pass gets `max_turns: 25`; give MISSING and CLAIMS 40 or split them.** Effort is the
table's column. Why those numbers, and what must be re-measured before changing any of them,
is in [references/sizing.md](references/sizing.md): read it before changing a cap or the
column, not to run an audit.

**Split by check class, never by document.** Four of the nine classes cannot see a split by
document: WRONG DOCUMENT and CROSS-PLANE LEAK need every document that could hold the
restatement, MISSING needs every document or absence cannot be established, and CLAIMS needs
`DECISIONS.md` and `JOURNAL.md` alongside. A pass that cannot see the other copy reports
nothing and looks clean.

## Task template

Fill the two slots. The definition is what makes the task self-contained, so paste it in full.

```
Run the <CLASS> check class only, over <SCOPE>. Everything you need is in your files[]
block. The repository root is <ABSOLUTE PATH>.

<CLASS> means: <its definition from references/check-classes.md, verbatim>

A claim you cannot check against a file you were given is not a finding and is not a
question. List it once under "could not verify" and move on. Do not weigh it twice.

Gate output for this run, which you cannot obtain yourself:
<paste it>

Use the output format your file specifies.
```

**The "could not verify" line is load-bearing.** A pass meeting a claim about code outside
its `files[]` has no rule for it otherwise, so it re-litigates the same sentence until the
budget ends the turn. The symptom is an exhausted ceiling whose output is mostly duplicate
lines, and it reproduces at *every* ceiling — so a budget that ran out is evidence about the
task's wording, never about the budget. Check duplicate-line share before raising anything.

## Check-class definitions

In [references/check-classes.md](references/check-classes.md), one per class. Read the one
for the pass you are writing and paste it into the task verbatim.

## Concurrency

`max_inflight_seqs` is the only admission gate, and bounds how many passes run at once. A
pass over it waits its turn, by default for as long as the work ahead takes; only a
non-zero `admission_wait_timeout` fails it instead. Read both values from
[docs/CONFIGURATION.md](../../../docs/CONFIGURATION.md) and do not assume them — they are
configuration, and this file is not their home.

**Dispatching more passes than the gate admits costs wall clock, not results**, unless
that timeout is set: the overflow queues and runs as the admitted ones finish.

The wait is **not silent**: a queued delegation reports progress on every tick. A fan-out
that looks stalled is a fan-out that is queueing, and the way to tell is that stream plus a
missing `priced` event, rather than assuming the endpoint is sick.

Do **not** serialise to avoid the queue. Passes dispatched together share a cached prefix and
the later ones read it; running them one at a time throws that away and buys nothing, because
the queue was never what killed a pass. That was the reply budget (ADR-0070).

**Fill the gate: `max_inflight_seqs` at a time, narrowly scoped.** Six is what the gate admits
here, and dispatching fewer leaves capacity idle for no gain — a burst that fits is priced for
the contention it will actually meet, so a full gate is the intended shape rather than a risk.
Dispatching *more* is the refusal above.

**Expect more calls than the table has rows.** Split any pass that cannot meet that shape,
dispatch the halves like any others, and prefer more small calls over fewer that return
nothing.

## Sizing

Judge a pass by expected findings, not by input size — prefetch is cheap and replies are not.
The reply budget is derived from the observed decode rate, so it shrinks as the cluster
fills. An oversized pass does not reliably come back empty. It may exhaust its reasoning or truncate
mid-sentence, and **`empty_response` reads `false` for both** — read alone it files a failure
as a clean pass with a long answer. Check `answer_is_reasoning` and `reasoning_exhausted` on
every returned pass. Raising `max_tokens` cannot help either way, because the
deadline-derived ceiling applies over an explicit argument too.

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

**A pass that came back `hit_turn_limit: true` gave a partial answer, and the record says
so.** Its last turn forbids tools, so it wrote whatever it had rather than what it had
finished — a clean verdict from one is weaker than a clean verdict from a pass that stopped
because it judged itself done. Read the field on every returned pass, not just the ones that
look thin.

Committing the record resets the gate's audit-due pressure.
