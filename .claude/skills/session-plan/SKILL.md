---
name: session-plan
description: "Plan this session's work from PLAN.md — rank the open items, size them, fill the session from that ranking, argue against the resulting plan and settle what it rests on by measuring, then stop for approval. Use at the start of a working session, or when asked to plan, re-plan or pick up the next milestone item."
---

# Session plan

Five steps. Step 4 is not optional and step 5 is where you stop. Execution is a different
skill — [session-execute](../session-execute/SKILL.md) — and nothing here starts work. Read
it to understand what it needs from the plan you are about to write.

## 1. Read. Do not edit.

`PLAN.md` is the contract. **If it is not there, stop and say so** rather than planning from
memory or from another document: there is no fallback, and a session planned against the
wrong source is worse than one not planned at all. **`STATUS.md` was retired — it does not
exist, do not look for it.**

For why an item exists: `DECISIONS.md`. For what shipped and why: `CHANGELOG.md`. For what
was measured recently: the last few `JOURNAL.md` entries. For what an upcoming session needs
and no tracked document holds: the hand-off notebook in the plans directory.

Delegate the reading. `delegate_readonly`, one question per call, and name the documents in
`files[]`. Do not read them in the main conversation to answer what a delegation could
answer — that is what actually ends a session. If `delegate_readonly` is not available, use
the Explore agent.

## 2. Rank, then fill

`🔄` outranks everything: finish before starting. Then rank the `⬜` items under **Open** by
blocking impact ÷ effort. `✅`, `❌` and **Deferred** are out of scope for the ranking.

- **An item resting on a claim nobody has measured is a spike, not an item.** Rank the spike
  first, and say what would be run to settle it.
- **Settle that spike now if its answer would move anything else.** Ask: if the measurement
  came back the other way, would another item's viability or its order change? If yes,
  measure before the ranking is finished, or this step and the next are built on the very
  claim they exist to catch. If it only changes its own item's design, step 4 is soon enough.
- Rank every `⬜` item. Ranking stops when the items run out, not when the list starts to
  look long enough.

### Size each item, then fill to the budget

**Small 0** — a line or two in one file, no design decision. Rides along free.
**Medium 1** — two or more files, or one file plus its test and its owning document.
**Large 2** — several files, or a design decision, or a measurement pass.

**A session is 12 units.** It is a budget in units of work and **never a number of items**:
the count falls out of it and is never chosen. **There is still no right number of items**,
and a count you could have named before opening `PLAN.md` is a habit, not a plan. Show the
total beside the budget so both numbers are visible.

**12 is the default, and only the operator changes it.** "Plan a short session" or "plan a
big one" moves it; nothing you observe about the work does. The number stands in for context
headroom, because what ends a session is the 1M window rather than the clock — which is a
reason to **delegate the reading**, not a reason to re-price the budget yourself.

**If the ranking runs out before the budget does, say so and why** — too few open items, or
too many blocked behind one measurement. Report the shortfall; never pad the session to
reach 12.

- **Reasoning beats file count.** Medium by files but carrying a design decision is Large.
- **Size by what must be reasoned about, not by lines changed.** A 900-line mechanical
  migration a delegation performs is cheap; it is Large only if the rules governing it need
  designing.
- **A spike is sized by what the measurement costs**, never by what it unblocks.
- **Nothing is bigger than Large.** An item that feels heavier is not one item; split it
  before ranking it.

### Then look down the ranking for a passenger

An item sharing a file, a module or a document with one already taken costs a fraction of
its own estimate — the reading and the verification are already paid for — so it can fit
where its own rank never would. What it does not buy is a shared landing: one feature per
branch still holds, so it is its own commit, and its own branch if it is its own feature.

## 3. Propose ONE plan

One plan, not a menu. Per item, name:

- **which `PLAN.md` item it is**, by its id — `M11.10`, `Unscheduled.29` — never by quoting a
  title, which changes;
- **a clear title, and what it changes and why**, written for the person approving it. They
  cannot approve what they cannot follow, and that is the only bar this description has;
- **the branch and the commits it splits into** — `feat/`, `fix/` or `docs/`, one feature per
  branch, and say where a commit boundary falls if there is more than one;
- **the check, and how it fails without the fix.** A check that cannot fail is worse than no
  check, so say what the negative control is before writing either. For a change with no
  code in it the check is a command rather than a pytest — `docs_gate.py --mode pre-commit`,
  a budget header, a generator re-run whose diff must come back empty. If nothing mechanical
  can fail, say so and name the line a reviewer has to read instead;
- the owning document — `python scripts/docs_gate.py --owner <path>`, never from memory;
- **that it is a whole item.** If what you are proposing is a fragment of a PLAN entry — a
  precondition you measured out of it, a first half — say so, and plan the rest of the entry
  too.

The CHANGELOG entry is not drafted here. Its *why* is the description above; the entry
itself is written when the work lands, by [session-execute](../session-execute/SKILL.md).

Constraints that shape the split, not afterthoughts:

- One feature per **branch**. Squash-only, and one PR open at a time, because a merge
  deletes the base branch and closes a stacked child unreopenably. Stack locally. That caps
  what *lands* in a session, never what is *built* in one, so it is not a reason to plan
  fewer items.
- Every document carries a `BUDGET:` header. Trim the lines *you* are adding first; propose
  a raise only as a last resort, as CONTRIBUTING's "Prose" section says.
- A fact belongs to one document and one plane — the repository root or `docs/`, and
  [CLAUDE.md](../../../CLAUDE.md) says which holds what. If the plan says "and document X in
  two places", the plan is wrong.

## 4. Argue against the plan — before presenting, every time

You wrote the plan, so it already looks right to you. Switch sides here and try to break it:
this pass has worked when it finds something, not when it agrees with you. A pass that finds
nothing usually means the plan got re-read rather than attacked.

1. List what the plan rests on — every assumption that would change its *design*, not just
   its wording, if it turned out false. **There is no target number** — the list is as long
   as the plan makes it. Finding only one means the parts you feel sure about were skipped,
   and those are the ones worth attacking.
2. Against each, answer exactly one of: **verified by running `<command>`** or **assumed**.
   There is no third answer, and "it follows from" is `assumed`.
3. Go verify the assumed ones **live — measurement, not reasoning** — and revise the plan
   with the raw results inline. **Measuring is execution, so it obeys execution's rules:**
   [session-execute](../session-execute/SKILL.md) §1 says what may not run at once. A
   verification pass that ignores them measures the collision instead of the question.
4. Act on what the measurement says. It may **kill** an item — drop it, say what killed it,
   and pull the next one up from the ranking. It may instead **resize** it: a viable item
   that turned out bigger is re-estimated, re-ranked, and re-checked against the budget,
   which may push something else out. A resize is not a kill and must not be reported as one.
5. **Run 1–4 again over whatever entered or changed**, until a pass changes nothing. A
   replacement pulled up from the ranking was never on the assumption list — nobody knew it
   would be needed — so without this it reaches the plan unargued. If replacements keep
   dying, stop pulling: report the ranking exhausted and take the smaller session.

**Feedback on a presented plan re-enters here.** A change you were asked for is a change to
the plan's design, so it is argued like any other, and whatever it displaces or enables goes
round with it.

Claims about nested pytest, the sandbox, prefix caching, admission, git behaviour or the
network are measured. Plausible inference about those has been wrong here repeatedly.

## 5. Present, then STOP

Output the plan and stop. No implementation, no edit to `PLAN.md`, no commit, no push until
approval lands.

What approval buys is [session-execute](../session-execute/SKILL.md)'s to state, and that is
what this plan should be judged against.
