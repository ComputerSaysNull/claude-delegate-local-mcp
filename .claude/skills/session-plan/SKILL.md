---
name: session-plan
description: Plan this session's work from PLAN.md — rank the open items, propose one plan, challenge its own assumptions by measuring them, then stop for approval. Use at the start of a working session, or when asked to plan, re-plan or pick up the next milestone item.
---

# Session plan

Six steps. Step 4 is not optional and step 6 is where you stop.

## 1. Read. Do not edit.

`PLAN.md` is the contract. **`STATUS.md` was retired — it does not exist, do not look for
it.** For why an item exists: `DECISIONS.md`. For what shipped and why: `CHANGELOG.md`. For
what was measured recently: the last few `JOURNAL.md` entries.

Delegate the reading. `delegate_readonly` with those documents in `files[]`, one question
per call — a call with no `files[]` is the dearest shape, not the cheapest. Do not read
them in the main conversation to answer what a delegation could answer. If 
`delegate_readonly` is not available, use the Explore agent.

## 2. Rank

`🔄` outranks everything: finish before starting. Then rank the `⬜` items under **Open** by
blocking impact ÷ effort. `✅`, `❌` and **Deferred** are out of scope for the ranking.

- **An item resting on a claim nobody has measured is a spike, not an item.** Rank the
  spike first, and say what would be run to settle it.
- Check the milestone exit condition the items sit under. An item that does not move that
  condition is not this session's work, however cheap it looks.

## 3. Propose ONE plan

One plan, not a menu. Per item, name:

- the branch (`feat/`, `fix/`, `docs/`) and the commits it splits into;
- **the test, and how that test fails without the fix.** A check that cannot fail is worse
  than no check. Say what the negative control is before writing either;
- the owning document — `python scripts/docs_gate.py --owner <path>`, never from memory;
- the CHANGELOG entry's *why*: symptom, cause, fix.

Constraints that shape the split, not afterthoughts:

- One feature per **branch**. Squash-only, and one PR open at a time, because a merge
  deletes the base branch and closes a stacked child unreopenably. Stack locally.
- Every document carries a `BUDGET:` header. Trim the lines *you* are adding first; propose
  a raise only as a last resort, with a one-line reason.
- A fact belongs to one document and one plane. If the plan says "and document X in two
  places", the plan is wrong.

## 4. Assumption pass — before presenting, every time

1. Name the three assumptions in the plan that would most change its design if wrong.
2. For each, answer exactly one of: **verified by running `<command>`** or **assumed**.
   There is no third answer, and "it follows from" is `assumed`.
3. Go verify the assumed ones **live — measurement, not reasoning** — and revise the plan
   with the raw results inline.

Claims about nested pytest, the sandbox, prefix caching, admission, git behaviour or the
network are measured. Plausible inference about those has been wrong here repeatedly.

## 5. Touching a ticked item

Flip the marker and nothing else. A `✅` body is frozen: no rewording, no
line added or removed. An over-budget Done item is **reported**, not rewritten. Nothing is
archived mid-roadmap — raise the budget instead.

## 6. Present, then STOP

Output the plan and stop. No implementation, no edit to `PLAN.md`, no commit, no push until
approval lands.

What approval buys, so the plan can be judged against it: full suite on **both** platforms
before every commit — `.venv/Scripts/python.exe -m pytest -q` on Windows and the WSL venv
at `/tmp/vv` — plus ruff; every commit message and PR body printed as literal text, batched
at the end of the work, then one approval request; the PR title and body run through
`python scripts/docs_gate.py --pr-event <payload>` before publishing, because they are a
public surface no commit hook can gate and cannot be un-published.
