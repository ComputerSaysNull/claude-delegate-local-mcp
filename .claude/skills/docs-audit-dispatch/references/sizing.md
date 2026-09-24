# Sizing evidence

Why the turn caps and the effort column in `SKILL.md` are what they are. Read it before
changing either, not to run an audit.

**Do not size a pass by line count.** Measured across one full audit, the largest pass by far
returned a report and the smallest exhausted its reasoning, so no threshold on document,
module or combined length separates the two. The shape of the question is the lever.

**Every pass gets `max_turns: 25`, and the searching ones need more than that.** The agent
file's 5 is sized for one verification round and forces an answer before a search has
finished — and an absence established by a pass that ran out of turns is not an absence, it
reports clean. Give MISSING and CLAIMS 40 or split them: CLAIMS returned
`hit_turn_limit: true` at 25, and at 40 split in two it used 21 turns and 54 tool calls. A caller's number is clamped silently at `max_turns_hard_cap`, so asking high
costs nothing. Turns are cheap: measured over a 25-turn pass, tools were 3.4% of the wall
clock and 94% of input tokens were cache hits, so what a turn costs is what the model writes
in it. Prefetching what they seek is weaker, because neither knows which document holds it.

**Scope is the half that carries; effort is not.** A narrow arm returned thirteen of sixteen
clean, complete answers against a broad arm's four of twelve, and fifteen of sixteen against
six of twelve counting anything usable at all. Count both sides the same way when quoting it.
But that narrow arm also ran at `low`, so the two moved together and it is evidence for
neither on its own — **do not read it as a reason to ask for `low`**. The effort column below
says why, and what made `high` look expensive was pricing since fixed.

Effort is `high` everywhere but pass 13; pass 14 is not dispatched at all. An audit is a
search across kinds of violation, and `low` narrows it to retrieval — more instances of one
violation, fewer kinds. Pass 13 *is* retrieval, so `low` is both correct and cheaper there.

**Asking for `high` does not guarantee getting it, and the column stands anyway.** A pass can
come back at `low`: the empty-answer recovery ladder steps the effort down after an empty
reply, and a reply is empty when the budget ceiling was priced above what the turn could
decode. That is a pricing symptom, not an argument for asking `low` outright. **Re-measure
before changing this column**, and change it only if the step-down survives a fan-out against
current pricing — a column rewritten on evidence taken under a defect since fixed would read
as measured while measuring something else.
