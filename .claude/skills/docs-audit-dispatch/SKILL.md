---
name: docs-audit-dispatch
description: "Runs the complete documentation audit for this repository by dispatching the docs-audit-local agent, one check class per pass. Holds the pass list, the check-class definitions, the per-pass effort levels and the concurrency rule. Use when the docs gate reports audit-due pressure, before a release, or when asked to run a docs audit."
---

# Documentation audit runbook

Fourteen passes complete one audit. Running the same fourteen every time is what makes two
audits comparable, so run them all and record the ones you skipped. The last is not delegable.

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
| 14 | FILED ALREADY | the hand-off notebook — **not delegable**, see below | — |

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

**Do not size a pass by line count.** Measured across one full audit, the largest pass by far
returned a report and the smallest exhausted its reasoning, so no threshold on document,
module or combined length separates the two. The shape of the question is the lever.

**Passes 11 and 12 search rather than compare, so raise `max_turns` for them.** Both run out
at the agent file's default, and an absence established by a pass that ran out of turns is
not an absence — it reports clean. Pass a higher `max_turns` on the call; the agent file's
value is a default a caller may raise, clamped silently at `max_turns_hard_cap`. Prefetching
what they seek is weaker, because neither class knows in advance which document holds it.

Effort is `high` everywhere but pass 13, and pass 14 is not dispatched at all. An audit is a
search across kinds of violation, and `low` narrows it to retrieval — more instances of one
violation, fewer kinds. Pass 13 *is* retrieval, so `low` is both correct and cheaper there.

**Asking for `high` does not guarantee getting it, and the column stands anyway.** A pass can
come back at `low`: the empty-answer recovery ladder steps the effort down after an empty
reply, and a reply is empty when the budget ceiling was priced above what the turn could
decode. That is a pricing symptom, not an argument for asking `low` outright. **Re-measure
before changing this column**, and change it only if the step-down survives a fan-out against
current pricing — a column rewritten on evidence taken under a defect since fixed would read
as measured while measuring something else.

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
reports it as unsourced. **Search all three before calling a number unsourced.** This is the
most retracted finding the class produces.

**FILED ALREADY** — a notebook line whose fact now lives in `CHANGELOG.md`, `DECISIONS.md`,
`JOURNAL.md` or `PLAN.md`. The notebook holds what an upcoming session needs and nothing a
tracked document already has, so anything filed is dead weight on every session that reads
it. Quote the line and name the document that now owns it. Two neighbours of this class are
worth reporting in the same pass: a line whose claim is no longer true, and a war story where
a rule would do — the notebook pays for its length once per session, like a skill body.

**ESCAPE ABUSE** — gather the waivers with `read_git`: `log` over the last ninety days for
`Docs-Gate-Skip:` trailers, grouped by the document each names. Any document waived more than
twice in ninety days is a signal that the document is wrong, not the rule. Count events, not
trailers — two waivers in one commit for one reason are one event.

## Concurrency

`max_inflight_seqs` is the only admission gate, and bounds how many passes run at once. A
pass over it waits, and one waiting longer than `admission_wait_timeout` is failed rather
than queued indefinitely. Read both values from
[docs/CONFIGURATION.md](../../../docs/CONFIGURATION.md) and do not assume them — they are
configuration, and this file is not their home.

**Never dispatch more passes than the gate admits.** Fourteen into six slots refused eight at
the full wait, having produced nothing, while the six holding slots ran on.

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

**Scope is the half that carries; effort is not.** A narrow arm returned thirteen of sixteen
clean, complete answers against a broad arm's four of twelve, and fifteen of sixteen against
six of twelve counting anything usable at all. Count both sides the same way when quoting it.
But that narrow arm also ran at `low`, so the two moved together and it is evidence for
neither on its own — **do not read it as a reason to ask for `low`**. The effort column below
says why, and what made `high` look expensive was pricing since fixed.

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

Committing the record resets the gate's audit-due pressure.
