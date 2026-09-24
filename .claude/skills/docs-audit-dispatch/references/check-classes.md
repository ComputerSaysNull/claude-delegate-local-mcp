# Check-class definitions

One per check class. Paste the one for the pass you are writing into its task, verbatim;
the agent is never handed this whole file, which is what keeps a pass to its one class.

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

**A rule stated with its owner named is not a leak either**, and forgetting that is how this
class misfires. `CLAUDE.md` states a trap and then says whose the explanation is — "what
that buys ... is `docs/ARCHITECTURE.md`'s" — and `README.md` states what a reader needs at
that point and links to the owner with a bracketed `why`. Both are the prescribed pattern. **Quote the whole
sentence including the clause that follows it**, and check whether it names its owner before
reporting it — otherwise every invariant in the project plane reads as a duplicate.

**MISSING** — a module or behaviour with no documentation coverage at all. Check `PLAN.md`
and `archive/PLAN-milestones.md` first: not-yet-built is not undocumented. And check
`scripts/docs_ownership.toml`: a file **declared unowned** was decided to need no document,
so it is an answered question rather than a gap. Without that file the pass reasons from the
absence of prose and reports the declaration as an oversight.

**CLAIMS** — documentation asserting a measurement that no ADR, JOURNAL **or CHANGELOG**
entry substantiates. Quote the substantiating sentence where there is one. Numbers decay,
and an unsourced one cannot be rechecked.

CHANGELOG.md counts, and forgetting it is how this check misfires. A measurement recorded in
the section for the pull request that made it *is* sourced — that is where every change's
why lives, by CONTRIBUTING's rule — and a pass told to look in two of the three places
reports it as unsourced. **Search all three before calling a number unsourced.** This is the
most retracted finding the class produces.

**NARRATIVE** — a skill or agent body telling the story of an incident where a rule would
serve. These files are re-read on every invocation, so a sighting costs on every run and
buys nothing a reader can act on. Quote the passage and propose the rule that replaces it.
Report only what the gate cannot: it already blocks a date and a pull request number, so the
findings here are the undated kind — "it reported six where three had been written", "this
was tried and reverted", a paragraph of provenance for a one-line instruction. An `ADR-NNNN`
pointer is not a finding. Neither is a *measured number* that calibrates a decision, such as
a threshold or a benchmark; a number doing work stays, a number telling a story goes.

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
