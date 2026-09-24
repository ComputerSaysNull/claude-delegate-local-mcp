---
name: session-execute
description: "Execute an approved session plan — what may run at once, what closes out every commit, where a bug found mid-flight goes, and how a pull request is published. Use once a plan has been approved, and whenever work is already under way and a commit or a pull request is about to be made."
---

# Session execute

The plan was approved. [session-plan](../session-plan/SKILL.md) stopped here on purpose;
this is what it was stopping for.

**If you cannot find the approved plan, stop and say so.** Reconstructing it from the
conversation produces something nobody approved, and the difference is invisible afterwards.

Read the hand-off notebook in the plans directory before the first edit. It says what this
machine is set to that the repository does not, and it is the only place that knows.

## 1. What may run at once

Overlap what is genuinely independent — wall-clock is worth having — but the limits here are
not about compute:

- **Delegations and delegated agents overlap freely**, whether they read or write.
- **Never the Windows and WSL suites against one checkout.** They share `__pycache__` under
  `/mnt/c` and it surfaces as an ImportError in an unrelated module.
- **Never a suite while a fan-out is live.** The server shares this machine.
- **Every commit edits the top of `CHANGELOG.md`**, so two items built in parallel conflict
  there even when nothing else they touch does.

**Separate worktrees do not buy parallel suites.** A worktree run
collects its tests from the worktree but imports `claude_delegate_local` from the *main*
checkout, because the editable install points there — so it exercises code the branch does
not have. It is wrong rather than merely slow, and the fix is a provisioned venv per
worktree, which costs more than one run's parallelism returns.

**What spends the window is what enters this conversation**, and that is as true of writing a
module as of reading one. Quote a suite's tail rather than pasting its whole run.

## 2. Closing out a commit

In order, every time:

1. **Full suite on both platforms, plus ruff** — Windows and the WSL venv, never at once
   against this checkout. Targeted runs are the right tool *while you iterate*; the full
   suite is what closes a commit, because a targeted run cannot see the coupling a change
   creates. What has to be green is what **lands**: a branch is squash-merged, so that is
   its tip rather than every intermediate commit. `ruff format` is not a gate here;
   `ruff check` is.
2. **The CHANGELOG entry, written to `CHANGELOG.md`'s own header** — it states the heading
   shape, the subsections and what an entry has to carry, and restating that here is how the
   two would come to disagree. **The heading's number is provisional at this point**; §4 is
   where it gets checked.
3. **The owning document**, from `python scripts/docs_gate.py --owner <path>` and never from
   memory. Changing the code means updating its owner in the same commit.
4. **`python scripts/docs_gate.py --mode pre-commit`, with the change staged.** Confirm it
   is staged first — `git diff --cached --name-only` must print at least one path. With
   nothing staged the gate prints `0 file(s) changed` and then PASS, having run no ownership
   check at all, and that pass is indistinguishable from a real one.
5. **`PLAN.md`: flip the marker and change nothing else in that document.** A `✅` body is
   frozen — no rewording, no line added or removed. A parent goes `✅` only once all its
   children are.
6. **The hand-off notebook: delete before you add.** Remove every line this commit just
   filed into `CHANGELOG.md`, `DECISIONS.md`, `JOURNAL.md` or `PLAN.md`, then add only what
   is still unfiled. The notebook holds what an upcoming session needs and nothing a tracked
   document already has, so it shrinks as work lands.

One feature per commit: code, tests, documentation and CHANGELOG together. That is what
gives the owning-document check something to compare against.

## 3. What surfaces while you work

**A Small you fix as it surfaces** — if it is inside what this commit already changes, it
rides along in this commit. If it is not, it gets its own commit, and its own branch if it
is its own feature. When in doubt, separate it: a Small buried in an unrelated commit is
invisible to review and to the owning-document check.

**A bug, quick to fix** — the repair is a line or two and the cause is understood: fix it in
this session, with its own commit, its own check and its own CHANGELOG entry. *Quick*
describes the fix, not the rigour.

**A bug, anything larger** — a `⬜` item in `PLAN.md`, under the milestone it belongs to or
under `### Unscheduled` if it belongs to none. Record the symptom and what you were doing
when it appeared. Do not fix it now: it has not been ranked against anything, and doing it
anyway is how a session quietly becomes a different session.

**A partial observation** that is neither a bug nor a plan item — something noticed, not yet
understood — goes in the hand-off notebook. That is what the notebook is for, and it keeps
`JOURNAL.md` at its own bar.

Anything that needs its own branch is **stacked locally and opened later**, never opened
now. Only one pull request may be open at a time, because merging one deletes its base
branch and closes any stacked child unreopenably.

## 4. The pull request

`python scripts/plan_stack.py --verification <file>` derives the stack and prints the
commands that publish its front branch.

**The commit and the pull request say different things.** The commit body is short: what
changed, why it is allowed, any correction a reader would otherwise trust. The pull request
body is the branch's CHANGELOG section as it stands — its `Added`, `Changed` and `Fixed`
subsections — plus a `### Verification` section: the red-before-green result or the check
that fired before it passed, and the suites. The file you pass is that section's text
*without* its heading; the planner writes the heading, and refuses a file that is missing or brings its own. It reads and publishes nothing, so it is safe to run at any time, on any
branch. **You then run those commands yourself, one at a time**, and each push, `pr create`
and `pr merge` asks before it happens.

That division is the point, not a formality. A hook can only gate a command run as a tool
call; it cannot see a subprocess of a script it has already approved. A publisher that
pushes on your behalf is therefore a publisher no hook can stop, however carefully it
scans first.

**Only the front branch is planned, and the plan expires when it merges.** The squash
rewrites `main`, so every tip above it is stale from that moment and the `--onto` arguments
name commits that have moved. Re-run the planner after each merge rather than working down
a printed list.

What follows is why the commands are those commands, which is what you need when the
planner refuses.

**The number in the CHANGELOG heading is a guess you then check.** Take the highest number
GitHub has issued — pull requests and issues draw from one counter, so read both — add one,
and write that heading when you write the entry. The planner checks it for you and refuses
before anything is pushed. If the issued number differs anyway, correct the heading and push
again **before merging**; `docs_gate.py --pr-event` refuses a mismatch, so a forgotten
correction cannot reach `main`.

**Nothing is published unscanned.** A pull request's text is a public surface no commit hook
sees, and CI only reads it once it is already published — for a leak that makes CI a
backstop rather than a gate, so the scan happens before `gh pr create` rather than after it.
It runs from the branch carrying the entry, because the number check reads that branch's
newest heading and from anywhere else compares against the wrong one. Write the mechanism,
never the specimen.

The title's *shape* is checked too: Conventional Commits, on the pull request title and on
every commit subject, because a squash takes its subject from whichever of the two applies.

**One at a time, and squash-merged**, which is what makes the sequencing necessary: merging
deletes the base branch and closes a stacked child unreopenably, and the squash leaves the
next branch based on a commit that is no longer in `main`'s history.

## 5. Working without interruption

**Keep going.** Do not stop to check in, to confirm something already approved, or to report
progress. The plan is the mandate.

- **Blocked on one item and needing a decision?** Say so plainly in the conversation and
  move to the next item. The conversation is read between other things, and guidance arrives
  when it arrives — a question that halts the session costs more than the item is worth.
- **Blocked on everything that is left?** That is the moment to ask, with
  `AskUserQuestion`, because that notifies rather than waits to be found.
- **Approvals are batched.** When the planned work is done, print every commit message and
  every pull request body as literal text, ask once, and push after the answer.

## 6. When the work is done

The session ends with the plan's items ticked, the notebook reconciled, and anything the
session learned filed where it belongs: a decision in `DECISIONS.md`, a measurement that
cost real time in `JOURNAL.md`, everything else either in the notebook or deliberately cut.

Nothing is archived mid-roadmap. An over-budget Done item is **reported**, not rewritten.
