<!-- BUDGET-PER-ENTRY: 60 -->
# Decisions

Numbered ADRs, newest first. **Append-only**: the body of a decision is never edited,
because the reasoning at the time is the point of the record. When a decision stops
being true, only its heading changes:

- **Superseded** — wholly replaced. Struck through, and linked forward.
  `## ~~ADR-0004 — ... ~~ — Superseded by ADR-0009`
- **Partially superseded** — part still holds, typically a measurement whose conclusion
  turned out wrong. Status only, no strikethrough: striking it would wrongly imply the
  whole entry is dead, and the surviving part is usually the expensive part.
  `## ADR-0015 — ... — Partially superseded by ADR-0019`

**The headings are the index.** `grep '^## ' DECISIONS.md` lists every decision with
its number, date, title and status. Read the headings; open a body only when that
decision is actually in play. There is deliberately no index table — a table would
be a second copy of the same facts, and second copies drift.

---

## ADR-0068 — 2026-09-11 — A waiter passed over long enough becomes a barrier — Accepted

**Context.** `_binding` refuses any waiter whose `ahead` is non-zero, and `ahead` counts
only earlier-ticketed waiters that could be admitted *now*. A waiter no rule would currently
admit is therefore invisible to everyone behind it. That is deliberate and ADR-0040's
ticket ordering depends on it: counting an infeasible waiter would reintroduce the
head-of-line blocking the single predicate exists to prevent.

**It had no floor, and one shape abused it.** A waiter needing more of a shared budget than
its successors is never feasible at the moment they ask — they hold the very budget it is
short of — so it is never counted as ahead, and every fresh arrival wins the same race
again. Measured 2026-09-11 by driving the gate directly: against `kv_token_budget`, five
successive smaller requests were admitted while the larger one parked ahead of all of them
stayed blocked, and nothing in the design would ever have ended that.

**`max_inflight_large_prefills` is not where this lives, and PLAN.md said it was.** The same
probe showed that cap behaving correctly: smalls do stream past a large parked on it, but
only while another *large* holds the slot, and larges take tickets and queue behind each
other — so the waiter was admitted the instant the blocker released, and a newcomer could
not overtake it once feasible. Bounded blocking, not starvation. A fix aimed there would
have serialised the gate for no gain, which is why the probe came before the change.

**Decision — past `admission_starvation_grace`, an infeasible waiter counts as ahead
anyway.** It becomes a barrier: arrivals queue behind it, the in-flight work drains, and the
budget falls to it rather than to the next caller. The grace is what keeps this from being
strict ticket order, which ADR-0040 rejected: inside it, overtaking is still the intent.

**Aging, not reservation.** Reserving capacity for a parked waiter would hold it idle while
the waiter is still blocked on something else, which is the "holds capacity it is not using"
failure the four-rules-as-one-predicate shape was written to avoid. Aging costs nothing
until a waiter is actually being passed over.

**One site, because `rival_fits` is already the shared primitive.** Both the in-process
queue and `SharedSlots` receive it as a callback, so the barrier reaches the cross-process
path without a second implementation — the asymmetry CLAUDE.md warns that extending one
enforcer alone produces. The waiter's age travels in its spec as wall-clock rather than
monotonic time, because across processes only wall clock is comparable, and it is stamped
by the caller once rather than per attempt: a timestamp refreshed on every retry measures
the gap between polls, so nothing would ever age.

**Consequences.** `admission_starvation_grace` is a new setting, defaulting to 30 seconds
and disabled at zero. Today it is close to inert, because `kv_token_budget` is set well
above the pool the endpoint reports and so rarely binds — correcting that is what will make
this reachable, which is the reverse of the order the roadmap ranked the two.

## ADR-0067 — 2026-09-10 — A broad pattern is exempted at one name, never narrowed — Accepted

**Context.** `security/secret_globs.txt` is one list with two enforcers: `paths.py`'s layer
3 and `scripts/docs_gate.py`. `.env.*` denies every environment file, and was also denying
`.env.example`, which is tracked and which the list's own comment calls the readable control
on that pair. Found 2026-09-09 by running this repository's suite nested for the first time:
inside the sandbox the file was a character device owned by `nobody` and read as `Permission
denied`, while `models.toml.example` read normally.

**The two enforcers already disagreed, and nothing reported it.** The gate carried an
exemption of its own — `r.endswith(".example")`, applied after a match — so it exempted
every `*.example` while the server exempted none. One list, two readings, in opposite
directions. That is the failure the shared file exists to prevent, and it had been live.

**Decision — a leading `!` exempts, checked before every deny, and neither enforcer carries
an exemption of its own.** The alternative was narrowing `.env.*` to the suffixes seen
today, which is an allowlist by omission: a `.env.production` added later would be readable
and nothing would say so. An exemption stays closed by default and names exactly what it
opens, so the blast radius is one filename rather than one pattern.

**The duplication is kept and made checkable.** The gate must run from a bare clone with
nothing installed, so it cannot import the package and cannot share `secret_match`. Each
holds its own reader, and a parametrised test asserts the two return the same verdict for
the same paths — denied and exempt alike, since a test over exempt paths alone would pass
against enforcers that agree on nothing else. Negative-tested by removing the gate's half
alone and confirming the test fails on `.env.example`; that is the asymmetry CLAUDE.md
warns extending one site silently produces.

**Consequences.** An exemption is a deliberate, reviewable line in the list rather than a
condition in code, and a future one costs nothing new. `!` is now reserved as the first
character of a pattern; a literal filename beginning with it cannot be expressed, which is
accepted because no such file is plausible here.

## ADR-0066 — 2026-09-09 — The contract has four homes, because a description is an index and not a manual — Accepted

**Context.** CLAUDE.md calls the tool descriptions the model-facing contract and treats
rewording one as a behaviour change. Nobody had measured what the client delivers.

Claude Code holds a 2048-character constant. A longer description is sliced to it and a
marker appended; the `/mcp` panel says so in as many words, and the same constant cuts the
server `instructions`. Nothing in FastMCP or the MCP schema caps either. No mechanism
returns the remainder, so a tool is chosen and called on the first 2048 characters.

All four delegating tools were over it, `delegate` at 6665 characters delivering 31% of
itself. `_COST_RULES` was *appended*, so #138's three facts sat exactly where the cut lands,
and what `delegate` never delivered included `effort` -- its one required argument -- every
return key, `empty_response`, and ADR-0061's skip-not-fatal rule.

**And the test could not see it.** It asserted against `client.list_tools()`, the server's
own copy rather than the wire, so it passed green throughout. The fifth
check-that-cannot-fail found in this repository.

**Decision -- each fact goes to the home that owns it, and a description keeps only the
smallest job.** A description is the retrieval index a tool is *found* by: tool search is on
by default, so it is matched on before it is read, and four of these six are near-twins.

- Arguments belong to `inputSchema`. Names, types and required-ness are already derived from
  the signature, so prose restating them was pure duplication; what an argument *means* now
  sits in its own `description`, on its own budget, beside the field it governs. `effort`
  gains an `enum` derived from `EFFORT_LEVELS`, so the wire and the runtime check cannot
  disagree -- two sites, neither trusting the other, as `allowed_tools` already does.
- Failures belong to the refusal. `Refusal.remedy` and `files_skipped` already carry what to
  do about a refused path, at the moment it happens, for callers who hit it -- rather than on
  every listing, for every caller who does not.
- The result belongs to `outputSchema`. `dict[str, Any]` infers an untyped object, so every
  key was undocumented; all 32 are described now, permissively -- nothing `required`,
  additions allowed -- because the one-shot path omits the loop ledger and a schema that
  could refuse a real result would trade a failure mode for documentation.
- The long form belongs to a **resource**, `delegate://orchestration`.
- What must be known before a tool is chosen stays in the server `instructions`, short.

**The resource is the load-bearing choice.** A prompt cannot do this job: prompts are
user-controlled by specification -- content arrives only on `prompts/get`, and a person must
select one -- so a rule the model must follow unprompted would never be delivered. A
resource is pulled by the *model*: Claude Code exposes listing and reading, so it reaches the
model unprompted, with no length limit. Relocating to a prompt would have deleted guidance
while looking like tidying; relocating to a resource is what lets a description shrink.

**Measured.** Descriptions went from 1484-6087 characters to 392-622, every argument of
every tool is described, and the long form is 3301 characters that cost nothing until read.
Common practice agrees: GitHub's official server has a median description of 69 characters
and keeps its workflow prose in server instructions.

**Enforced by checks that were each shown failing first**: a target per description well
under the client's cut, a per-property assertion that no argument is undescribed, the enum
compared against the config constants, and -- the one nothing else would catch -- that the
URI the instructions advertise is actually registered.

**A risk left open.** A reported client issue says the instructions budget is shared across
servers; the path observed truncates per server at connect. Verify before moving more there.

## ADR-0065 — 2026-09-09 — The lists the scan reads are the two files it may not cover — Accepted

**Context.** ADR-0035 covers a denylist match with a mount rather than leaving it out, and
ADR-0062 carved out the one tree a command must be able to *read*. Neither anticipated the
degenerate case: `security/secret_globs.txt` matches its own `*secret*` entry, so the scan
covered the list it had just read.

Covering a file means `--ro-bind /dev/null`, and inside the sandbox that is a character
device owned by `nobody`. It reads as `Permission denied`, not as empty -- so
`load_secret_globs` took its `except OSError` branch and raised `PathPolicyError` rather
than degrading. Layer 3 became unusable inside the sandbox, and with it every nested run
that exercises it: 42 tests of `test_tools.py`, and the reason this repository's own suite
could not run nested in full.

It failed **closed**, so this was usability rather than a hole, and that is why it stood as
a filed item rather than an incident.

**Decision -- both configured list files are exempt from being covered at all.**
`secret_globs_file` and `opaque_globs_file`, recognised by `os.path.realpath` on both sides.
Realpath rather than string equality because the two sides are constructed differently: the
setting is resolved against the server's working directory, while the walked path is joined
from a bound root, and one bind can reach the same file by two paths. Not `samefile` -- it
stats twice for no gain, since a walked path exists by construction.

**Exempting them discloses nothing.** They hold glob patterns, not secrets; both are tracked
in git; and a caller who can delegate at all can already read them. The exemption is exactly
two files wide, which is the property worth testing: a neighbour matching the same pattern
in the same directory is still covered, verified by a negative control rather than asserted.

**The resolution lives in one function**, `paths.resolve_configured_path`, called by both
loaders and by the scan. Three copies of the same cwd-relative dance was how the scan came
to compare a walked path against a setting resolved a different way.

**What this does not close.** The guard is on the file-match path only. No directory pattern
matches `security/` today, but one added later would cover the parent wholesale and reopen
this in a form the new tests do not catch -- they assert the containing directory is not
shadowed precisely so that failure is loud rather than silent. And an operator pointing
`secret_globs_file` at an absolute path outside the workspace, while delegating against this
repository, leaves the repository's own copy covered; the exemption names the file this
server was configured with, not every file of that name.

**Measured, both directions, through `tools.execute_tool` rather than a hand-written bwrap
line.** Before: `head -1 security/secret_globs.txt` inside the sandbox returns `Permission
denied`. After: it returns the first line. The control holds in the same run -- `.env` and
`models.toml` are still refused, and `security/opaque_globs.txt` is readable. Four unit tests
were then run against the un-fixed scan and all four failed, which is the only evidence that
they test anything.

**A note on the tests, because it cost a wrong pass.** The first version of the exemption
test was named after the pattern it exercised. `tmp_path` is derived from the test's name, so
the fixture directory itself matched `*secret*`, the whole tree was covered as a directory
and pruned, and the assertion "the list file is not among the shadows" passed against the
unfixed code. A check that cannot fail is worse than no check (CLAUDE.md); the tests now
assert the containing directory was not shadowed before asserting anything about the files.

## ADR-0064 — 2026-09-08 — A covered directory is read-only, because a writable one corrupts rather than discards — Accepted

**Context.** ADR-0035 covers a denylist match with a mount instead of leaving the path out,
and ADR-0041 extends the same mount to bulk directories. Both used a writable `tmpfs`, and
ADR-0041 recorded the consequence as benign: *"the tmpfs that covers a directory is
writable, so a command writing into one appears to succeed and leaves nothing behind."*
Filed as a wart about lost work.

It is worse than that, and M9 made it visible. The mount is 64 KiB. Measured 2026-09-08: a
200 KiB write into a covered directory is **truncated at exactly 65536 bytes with no error
reported to the writer**, and the truncated remainder stays there to be read. So a nested
`pytest` writes bytecode into a covered `src/.../__pycache__`, the write is cut short, and a
later import in the same run dies on `EOFError: marshal data too short` -- 18 collection
errors on a single test file. Not a discarded write: a corrupt artefact, produced by a
control that exists to make a path *empty*.

**Decision — `--remount-ro` immediately after each covering `tmpfs`.** The cover keeps doing
what it was for, and a write into it is refused rather than half-accepted. File shadows are
untouched: `--ro-bind /dev/null` is already read-only, and `--remount-ro` on a bind rather
than a mount of its own would remount whatever tree it landed in.

**The order is the whole thing.** `--remount-ro` applies to the mount that is current, so
emitted anywhere but directly after its own `tmpfs` it silently remounts something else, or
nothing. One remount per cover, asserted per cover rather than once.

**Both halves measured, and the control is what makes the measurement worth having.** A
write into a covered directory is refused at exit 1, a 200 KiB write leaves no partial file,
and the same writes into the workdir still succeed at exit 0 with all 200 KiB present. The
covering itself is unchanged -- a directory holding a private key still lists empty from
inside. The first probe in this area, on 2026-09-07, asserted read-only against a directory
it had never covered and passed; that is why the control is not optional.

**Why this supersedes rather than merely adds.** ADR-0041's closing sentence is now wrong in
the way that matters: the failure it describes as leaving nothing behind leaves something
behind. Its decision -- cover and prune bulk directories -- stands, so it is partially
superseded rather than replaced.

**What it costs.** Python tolerates an unwritable `__pycache__` and pytest degrades to a
cacheprovider warning, measured 2026-09-07 across the full suite. A command that genuinely
needs to write where a cover sits now fails loudly instead of appearing to work, which is
the point: ADR-0007 says to trust the captured exit code, and until now that code was 0 for
a write that never happened.

## ADR-0063 — 2026-09-08 — The sandbox that runs a test suite never gets the network — Accepted

**Context.** M9 gives a delegation an interpreter it can run a project's tests with, and the
obvious next question is whether that command needs the network. A test suite often does:
this repository's own has a test asserting the network *is* reachable. And provisioning
certainly does -- a virtualenv plus a dependency resolution is nothing but network.

**Decision — provisioning gets the network, and the sandbox does not.** `provision` runs
server-side, in the server's own process, where the network already is. Nothing about it
happens inside `bwrap`, so no grant is needed and none is added. `--unshare-all` stays, and
`agent_network_allowed` keeps being the only route to `--share-net`.

**Because `--share-net` is not a network grant, it is the host's whole namespace.** There is
no allowlist and no destination list in it: an agent given `network: true` can reach
whatever this machine can reach, which here means the cluster and the LAN. That is
acceptable for a fetch an operator deliberately enabled by name. It is not acceptable as the
standing condition for running tests, which is the most frequent thing a delegation will
do once M9 lands, and which would otherwise carry that reach every time.

**What it costs, stated rather than discovered later.** A test asserting outbound
connectivity fails inside the sandbox, and fails *correctly* -- there is no network. Such a
test is named in the project's nested-exclusion list rather than granted a namespace, which
is the trade this decision makes: a named exclusion is one line an operator can read, and a
shared namespace is a capability nobody re-reads after the day it was set.

**And a delegation cannot install anything**, deliberately. Provisioning is an operator
action with an operator's timing, so a dependency a model decides it wants mid-task is a
refusal rather than a download. `--doctor` reports a stale environment (ADR-0062) so the
answer to "why can it not import that" is one command away rather than a guess.

## ADR-0062 — 2026-09-08 — A tree the shell must read cannot be covered, so it is trusted at build time — Accepted

**Context.** ADR-0035 covers denylist matches with a mount rather than leaving them out, and
ADR-0041 extends the same mount to bulk directories and calls pruning *without* covering the
hole -- correctly, for a tree an operator may keep secrets in. M9 needs something neither
anticipated: a virtualenv the sandbox must be able to **read**, because a command that
cannot import pytest cannot verify anything.

Measured on this repository's own environment rather than reasoned about. Walked, it is
8,981 entries and the denylist fires 13 times -- `certifi/cacert.pem`,
`keyring/credentials.py`, `pydantic_settings/.../secrets.py` and the whole of
`secretstorage/` -- each covered with `/dev/null` or a tmpfs, which breaks the imports and
the TLS roots of the environment the scan just read. That is exactly what ADR-0041 predicted
for a workspace virtualenv, and moving the tree outside the workspace does not escape it:
`discover_secret_shadows` walks `home`, `workdir` and `extra_binds`, which is every bound
root there is. Naming it `.venv` is worse, not better -- the opaque list matches that and
covers it, so the interpreter becomes invisible instead of broken.

**Decision — one tree is pruned from the walk and not covered, and is bound read-only in
exchange.** `sandbox.PROVISIONED_DIRNAME` under the sandbox HOME. The read-only bind is not
a second opinion about the cover; it is the control that replaces it. Covering answers "what
if a secret is in there" by making the tree unreadable; read-only answers it by making the
tree unchangeable, and the contents are then whatever the server put there.

**So the guarantee moves from scan time to build time, and that is the whole of the
decision.** `provision` builds the tree from the project's own dependency declaration with
`PIP_CONFIG_FILE=/dev/null` and the index environment variables dropped, because a `pip.conf`
may carry an index URL with credentials in it and this is the last point at which one can be
kept out. An agent file cannot substitute the tree either -- it is reserved in `agents.py`
alongside the base mounts, since taking it would mean supplying an interpreter the sandbox
did not build, whose exit code ADR-0007 then tells every reader to believe.

**A guarantee nothing re-checks is one that stops being true quietly**, so `--doctor` scans
the finished tree with the same denylist and *reports* what matches instead of covering it.
Thirteen matches here, all ordinary library files. The number is the point: it moves if
someone provisions with `--system-site-packages`, or adds a dependency that ships something
that is not a library file.

**Measured, both directions.** With the tree bound read-only, `touch` inside it is refused
at exit 1 while the same write into the workdir succeeds, and the full WSL suite still
passes from that interpreter inside `bwrap --unshare-all` -- 1309 passed, 4 skipped, exit 0.
The control matters: the first read-only probe in this area, on 2026-09-07, asserted
read-only against a directory it had never covered and passed.

**What this does not close.** A dependency that ships a genuine credential is installed and
readable, as it would be on the host. The scan never protected against that; it covered
paths by name, and by name a library's `credentials.py` and a real credential are the same
thing -- which is why covering them broke the environment rather than protecting it.

## ADR-0061 — 2026-09-07 — A refused path costs the call its file, not the call — Accepted

**Context.** `files[]` was all-or-nothing: one path the policy would not allow discarded
the whole prefetch before anything was dispatched. The motivating case is mundane and was
expensive — a prefetch naming a dozen files in the workspace and one path outside every
root, a scratchpad or another checkout, threw all twelve away and cost a round trip to
learn it. The refusal message is good and names every failure at once, so the caller can
fix them in one go; what it cannot do is give back the work the other eleven files would
have done.

**This rule was never in an ADR.** It lived in `resolve_all`'s docstring and in two
sections of `docs/AGENTS.md`, one of which carried a worked example of it. That is worth
recording as much as the change: a behaviour with no decision behind it had been treated as
settled for long enough that it read like one.

**Decision — partial resolution for `files[]`, and only there.** `resolve_files` returns
what resolved beside what did not. Each refusal becomes a `Skip` with kind `refused`,
carrying the path, the layer that refused it and the remedy, so the reply's `files_skipped`
names the path *and* the root it missed. The prompt's skipped list carries it too, which is
not incidental: a model told the path was refused does not spend a turn calling `read_file`
on it.

**`resolve_all` keeps its contract exactly**, and `tools.py` keeps calling it. A mid-loop
`read_file` resolves one path for one answer and has nothing to partially succeed at.
Adding the disposition beside the existing one, rather than adding a flag to it, is the
same argument ADR-0006's two dispositions already made: the lenient one applied to a
caller-named path is a silent drop.

**Two things stay fatal.** A `workdir` refusal, because a workdir is singular and there is
no partial version of it. And every path in `files[]` being refused — nothing survived, so
dispatching would spend a delegation on a prompt carrying none of the context it asked for,
and a caller who got every path wrong has one mistake to fix rather than a dozen. This is
the half of the old rule that was right, and it preserves what the pre-dispatch check was
for: a wholly-refused call still costs nothing and still does not depend on the cluster
being reachable.

**Every refusal layer skips, the secret denylist included.** Considered keeping that one
fatal on the grounds that asking for credential material deserves a loud stop, and
rejected: the file is not read either way, so the posture is identical, and a reply naming
the layer and the pattern is a louder signal than an error that names one path while
discarding eleven good ones. `tools.py` has already treated that layer as non-fatal
mid-loop since 2026-09-06, so keeping it fatal here would have made the same refusal mean
two different things depending on which surface met it.

**What this costs.** A caller who mistypes one path now pays for a delegation that runs
without it. That is the trade, and it is the right way round: the reply says exactly which
file was missing and why, whereas the old behaviour spent nothing and returned nothing.

## ADR-0060 — 2026-09-07 — A per-call record carries what was asked and why it refused — Accepted

**Context.** A delegation on 2026-09-05 reported one tool error across twelve `read_git`
calls, and nothing in the record could say which call failed or why — even with transcripts
enabled. The per-call entry was a `(name, outcome)` pair. The refusal text was built in
`tools.py`, handed back to the model as the next turn's history, and then dropped, so
searching the whole record for it returns nothing.

**Decision — this extends ADR-0039 rather than reversing it.** That decision excluded file
*bodies*, for two stated reasons: they are the overwhelming majority of the bytes, and they
are recoverable from the repository by path. ADR-0043 already read that exclusion narrowly
and extended the record to the reply text, because a reply "is small, and it exists nowhere
else". An argument and a refusal are neither bulky nor recoverable by path, and a refusal
exists nowhere at all once the delegation ends. The same reasoning reaches the same answer,
so ADR-0039 stands.

**Decision — arguments are recorded, capped per field, bodies summarised rather than cut.**
`TOOL_ARG_VALUE_CAP` bounds each value and the elision marker says how much was dropped,
because a silently truncated argument reads as a complete one and a reader would draw
conclusions from a path the model never sent. `content`, `old_string` and `new_string` carry
a body rather than an identifier, and are reduced to a length and a digest instead: a
truncated body is useless to read and still a partial copy at rest, which would reopen
ADR-0039's exposure through the arguments door.

**Decision — a refusal is recorded, only on an error outcome and capped.**
`TOOL_MESSAGE_CAP` is the larger of the two caps, because a refusal is the whole reason this
record exists while an argument only has to be recognisable. The text is the one `tools.py`
put in the result block, so the record and the model agree about what was said — ADR-0007's
rule that the server reports what it watched, not what was claimed.

**Decision — on success, accounting instead of content.** Bytes, lines, and an exit code
when a process exited. A successful `read_file`'s result *is* the file body, so recording
"the message" uniformly would be ADR-0039's exclusion by another name. The three counters
are universal rather than per-tool: a table of per-tool counters would drift every time a
tool changed its output, which is what this repository already refuses to accept for second
copies of a fact.

**Consequences.** A dataclass rather than a wider tuple, because the pair it replaces was
unpacked at three call sites and widening a tuple that other code unpacks breaks one caller
quietly. Those three sites now share one serialiser, so a fourth field is one edit and not
three; the one that would have drifted is the live stream, which is watched rather than
asserted on. The caps are constants rather than settings: they size a record's shape, not a
deployment, and an operator who wants more of a refusal wants the refusal — which the
elision marker tells them they are missing. The caller's `diagnostics` flag is unchanged and
remains reply-shaping only. What is newly at rest on disk is a bash command line and a
search pattern, which ADR-0039's own terms already cover: whoever configures the directory
owns what lands in it.

## ADR-0059 — 2026-09-06 — The agent tool gets a read-only twin, and the lookup path stops being called a workdir — Accepted

ADR-0042 argued the sixth tool and, in passing, described the shape of this one: "no agent
file can carry that constraint, because the annotation belongs to the tool the agent is
reached through, and that tool can write." That sentence was the whole argument for a
read-only *agent* tool, and it sat unbuilt for six days.

**Decision — `delegate_to_agent_readonly`.** `delegate_to_agent` with `allowed_tools` fixed
to the derived read-only set, declared `readOnlyHint`, no `workdir`. It is the third
application of ADR-0042's reasoning and not a new task kind, so ADR-0005's rule survives
whole: a new *kind* of work is still a markdown file.

What it buys that `delegate_readonly` does not is the agent file — the instructions, the
model, the effort, and the accumulated false-positive guardrails that are most of a
well-worn agent's value. What it buys over `delegate_to_agent` is the promise, which a
caller must be able to make *before* the call runs and cannot make with an argument.

**The agent's own `allowed_tools` is replaced rather than intersected.** That is the
existing caller-wins rule, not a new one, and it cuts both ways: an agent declaring
`run_bash` loses it, and one declaring less than the full read-only set gains the rest.
Intersecting instead would have been defensible and was rejected — it would make the
offered set depend on a file the caller cannot see at the call site, which is the same
opacity the annotation exists to remove.

**Decision — the lookup path is `project`, on every tool that finds an agent file.** ADR-0042's
correspondence — a workdir is a read-write bind, so a read-only tool cannot offer one — was
already false when this was written: `list_agents` carried `readOnlyHint` and took a
`workdir` that bound nothing. One name for two meanings, in the one place a reader compares
tools side by side. `workdir` now means only the sandbox bind; `project` means only where to
look, defaults to `workdir` on `delegate_to_agent`, and is the sole directory argument a
read-only tool may have.

A hard rename rather than an alias. Two names for one argument is the confusion being
removed, and the model-facing descriptions are the contract — an alias would keep the old
meaning reachable and readable for exactly as long as anyone kept using it.

**The correspondence is now a test, not a sentence.** It walks the declared schemas and
fails on any tool that claims `readOnlyHint` and offers a `workdir`. It was negative-tested
by restoring `list_agents`'s old signature and watching it fire — which is the point: the
prose form had already drifted once, silently, and prose cannot fail.

**What this does not buy.** It does not shorten an audit the way withholding verification
did: #122 measured 26 turns against 1 and attributed the gap to verification *and history*
calls, and `read_git` is read-only and stays. What it buys is the annotation — so a
read-only agent pass runs in plan mode without prompting — and the client ramp, since a
`readOnlyHint` tool is not released one arm per 120s.

Review point: ADR-0042's stands and now covers two tools. If MCP grows per-call annotations,
or a client learns to gate on arguments, both read-only tools become redundant and both go.

## ADR-0058 — 2026-09-06 — A delegation reports what the whole run cost, beside what the answering turn cost — Accepted

The result dict's `input_tokens`, `output_tokens` and `cached_tokens` describe the attempt
that answered, and always did — the note beside `attempts` says so. The transcript's `end`
event uses the same three names for lifetime sums. Both are defensible in isolation and the
collision is not: the same words mean different quantities in the two records a person
compares.

Measured on one twelve-turn delegation: the caller was handed `output_tokens: 2,002` and
`cached_tokens: 0`, while its transcript recorded 4,404 and 559,872. Neither is wrong. The
caller's figures are turn twelve, which happened to be the tools-withdrawn final turn that
cached nothing (ADR-0057), so the one number a caller might use to judge what delegation
saved was structurally the worst turn of the run.

**Decision.** Add `total_input_tokens`, `total_output_tokens` and `total_cached_tokens` to
the loop ledger. Additive: the existing per-attempt fields keep their documented meaning,
because the two answer different questions and neither is derivable from the other. "Was
this answer truncated" is about the answering turn; "what did this delegation cost the
cluster" is about every turn there was.

**Accumulated in `_Watch`, outside the `diagnostics` branch.** What a delegation cost is not
a debugging extra, and the per-turn detail that would let a caller add it up themselves is
off by default. `total_cached_tokens` stays `None` until an endpoint reports caching at all,
keeping a measured zero apart from an unmeasured one — the same distinction the per-attempt
field already draws.

**The picker reports a share, not a total.** `watch_delegations.py` rendered the summed
cached tokens under "saved". The figure is real — the cluster did skip that prefill — but it
counts the same prefix once per turn it was re-served, so it grows fastest exactly when
reuse is worst. On the run above, 479,232 of the 559,872 was one 53,248-token opening prompt
counted nine times, against 902,996 sent. As a ratio that is 62%, visibly poor for a history
that is nominally append-only, and it falls when the prefix stops being reused. A cumulative
total is the one presentation that cannot show the bug it is measuring.

**Consequence.** Totalling what delegation saves is now possible from tool results alone,
which is what it was not before: a caller adding up `cached_tokens` across calls was adding
up final turns.

---

## ADR-0057 — 2026-09-06 — The final turn forbids tool calls instead of withdrawing the tools — Accepted

`final = turn == turns` sent `tools=()` on the last turn, so the only thing left to produce
was an answer. The intent is right and is kept: a model that ends on a tool call nobody will
run has spent the whole delegation and returned nothing readable.

The mechanism was wrong. Tool schemas are rendered into the *front* of the prompt by the
chat template, and the stack caches prefixes, so removing them moves the first difference to
the very beginning. Measured on the live cluster over one shared history, nothing evicted,
nothing else varying:

    A   tools present                        prompt 36,339   cached 36,096   99.3%
    B   tools present + tool_choice: none    prompt 36,339   cached 36,096   99.3%
    C   tools withdrawn (the old behaviour)  prompt 36,018   cached      0    0.0%
    A'  tools present, repeated              prompt 36,339   cached 36,096   99.3%

C re-prefilled 36,018 tokens to avoid sending 321 tokens of tool schema. A' afterwards shows
the cache was still warm rather than evicted by C, so the loss is C's own.

**Decision.** The tools stay in every request and the final turn sends `tool_choice: "none"`.
B is byte-identical to A, which is the property the cache needs and the one that had to be
confirmed before this was worth building — a template that dropped the tool block under
`tool_choice: "none"` would have reintroduced the same divergence, and that is a fact about
the server's template rather than something inferable from the spec.

**A canonical vocabulary of two words, translated per adapter**, exactly as `effort` is
(ADR-0013). `TOOL_CHOICES` is `("auto", "none")`; the wire formats spell this differently and
neither spelling belongs in `CanonicalRequest`. "required" and naming a specific tool are
real options in both formats and have no caller here, so they are left out rather than
guessed at. An unlisted value is refused before dispatch, not after a prefill has been paid
for. `"auto"` is the default and is omitted from the body, so an ordinary turn's bytes are
unchanged.

**Why this turn is the one that could least afford it.** The tools-withdrawn turn is also the
turn that must fit a whole answer inside one deadline, because the loop breaks there whether
or not the model asked for anything (ADR-0055). It was paying a full cold prefill first. The
two fixes compound: one gave the turn a budget the clock can pay, this one stops it starting
from zero.

**Rejected: moving the tools to the end of the prompt.** It would keep them out of the
divergence, but the tools are sent as a `tools` field the *server's* template positions, so
doing it means abandoning that field and injecting descriptions as prose — giving up native
tool-call parsing, which the endpoint does correctly today. It also destroys a larger win
than it buys: every delegation currently shares `system prompt + tools` as a common prefix,
so putting the tools after the conversation would leave each run sharing only the bare system
prompt. A once-per-run saving paid for with an always-on one.

**Accepted cost.** The final turn now sends 321 tokens of tool schema it previously omitted,
and a model could in principle still emit a call the loop will ignore. The loop already
breaks on `final` regardless of what came back, so that path is unchanged.

---

## ADR-0056 — 2026-09-06 — The eviction boundary is carried and stepped, not recomputed every turn — Accepted

`evict_stale_tool_results` collapsed everything older than the newest `keep` tool results,
every turn. That reads as a stable window and is not one: as the history grows the boundary
advances by one result per turn, so the *first difference* between consecutive prompts moves
toward the front. The serving stack caches **prefixes**. Moving the first difference forward
discards the cache for everything after it.

Measured on an idle cluster with `preemptions: 0` throughout: the hit rate climbed 75.0%,
80.0%, 83.3% on turns 5-7 exactly as an append-only history should, then hit **0.0%** on the
turn `tool_results_evicted` first went to 1, and stayed there. Turns 8-11 re-prefilled
315,625 tokens the run had already established — identical work at five times the wall clock.
This is ADR-0011's failure mode with no error and no symptom beyond slower prefill.
ADR-0011 guards the system prompt; nothing guarded the history.

**Decision.** `_OverflowGuard` owns a boundary, `evicted_upto`, which only ever advances and
advances in steps of `keep`. `stub_oldest_tool_results` is told how far to stub rather than
deriving it. One rewrite therefore buys `keep` turns of prefix stability instead of one.

**Both halves are necessary, and measurement is why the shape is this one.** PLAN listed
three candidates as alternatives — gate on projected share, evict in batches, evict from the
front once. Modelled over the alternation the loop appends, they are not alternatives:

    per-turn boundary (before)          2.9% reusable   409,588 chars re-prefilled
    pressure gate only                  5.1%            273,499
    pressure gate + stepped boundary   79.2%            109,310
    no eviction at all                 93.0%             68,049   -- unbounded

Gating alone is nearly worthless once pressure exists, because the boundary still moves every
turn. The stickiness is load-bearing; the gate is what makes the common case free, the old
policy having fired at 7% of a 1M-token window where there was nothing to relieve.

**The stepping is unconditional and only the holding is gated, which is not the obvious
arrangement.** `context_overflow_enabled` is off by default and deliberately so: every
threshold here is measured against `context_window`, which a registry entry may have
inherited rather than had set. Gating the whole policy on that flag would leave the default
configuration never bounding a history at all — trading a cache bug for an unbounded one,
which is worse than what it replaced. So an unarmed guard still steps. Arming it adds
`OVERFLOW_EVICT_AT`, a new first stage at 0.50 below the existing tighten, nudge and abort
ladder, which holds the boundary while there is genuinely room.

**Below the threshold the boundary is held, not reset.** Un-stubbing content rewrites the
history in the other direction and costs exactly the same cache.

**Consequence: the second-order cost is now visible.** The same run's `evicted_then_reread`
ledger caught the model re-reading the file eviction had just dropped, on three consecutive
turns — the dropped content returns as a fresh full-size result, which pushes the next one
out. That machinery already existed and `diagnostics` is off by default, so nobody saw it.
`Stream.turn` now carries `tool_results_evicted` beside `cached_tokens`, which the final
record already had and the live stream did not: a watcher could see the cache collapse and
not see the cause.

**Accepted cost.** Under pressure the reusable share is 79.2% rather than the 93.0% of an
unbounded history. That gap is the price of bounding the history at all, and it is paid in
one step per `keep` turns rather than continuously.

---

## ADR-0055 — 2026-09-06 — The reply budget is derived from the deadline and a measured decode rate — Accepted

`max_tokens` and the deadlines were in different units and nothing related them. At `high`
effort the budget is raised to `thinking_max_tokens_floor` = 131,072; the cluster decodes a
single stream at ~36 tok/s, so that is about 77 minutes of generation against a
`stall_timeout` of 2,100 s. The comparison had never been made because making it needs a
decode rate, and a decode rate has to be measured.

The symptom was not a wrong number, it was a wrong diagnosis. Three `docs-audit-local` runs
died at exactly 2100.0 s with `turns: null` and read as wedged. One had produced 34,276
output tokens in 1,132 s on a straight line and was still going. ADR-0047 chose turn
completion as the liveness signal because every other one was fake; against a budget 2.6x
what the clock can decode, a model spending what it was given is indistinguishable from a
model that has stopped, and the deadline kills both.

**Decision.** The budget is capped at `stall_timeout × rate × reply_budget_margin`, floored
at `reply_budget_floor`, and the rate is measured rather than configured.

The rate belongs to the deployment, not to this repository: it moved twice in the week this
was written, once when the served model was swapped and once when the cluster's
configuration was pulled. A constant would have been correct on the day it was committed
and wrong within a week, which is the failure this whole ADR is a correction for. So
`DecodeRate` seeds from the endpoint's own since-boot figure and then replaces that seed
with what the delegation itself achieves, turn by turn. The cluster's mean is a blend over
every tenant it has served; our own turns are the rate our own deadline is paid in.

**The ceiling binds an explicit `max_tokens`, where the ADR-0014 floor does not.** The
asymmetry is the point. A floor is a preference about how much room reasoning gets, and
silently overriding a caller's preference makes the argument advisory. The ceiling is a
statement about what the clock can deliver, and there is no version of the request that
beats it — a budget above it does not buy a longer answer, it buys the same answer
discarded.

**Consequences, including the one that is not comfortable.** A busy cluster decodes more
slowly, so the ceiling tightens and replies get shorter under load. That is deliberate: a
shorter answer that arrives beats a longer one killed at the deadline with everything
generated thrown away, and `reply_budget_floor` bounds how far it can degrade. Where the
first budget already sits at the ceiling, ADR-0014's enlarged retry is skipped by the
existing identical-request test and the cascade steps effort down instead — which is the
right remedy once more room is not available: think less, rather than ask for time that
does not exist.

**Rejected: raising `max_turns`.** `final = turn == turns` withdraws tools on the last turn,
so a delegation exhausting its budget ends on the one shape that must fit a whole answer
into one deadline. Raising the count postpones that turn and buys a longer run to lose.

**Rejected: a configured rate.** See above; it is the same mistake in a new unit.

**Accepted cost.** An endpoint that publishes no metrics leaves the rate unknown and caps
nothing, which is the behaviour that preceded this decision rather than a guess. Inventing
a rate to cap against would be the constant this design exists to avoid. The scrape is one
GET per delegation, wrapped so that a monitoring surface being briefly unavailable can
never fail a delegation — the inversion `slots.py` warns about, where a latency protection
becomes an outage.

---

## ADR-0054 — 2026-09-05 — Sandbox resource limits come from a launcher, and the process cap is the compromised one — Accepted

`build_argv` bounded nothing: a runaway allocation or a fork bomb was held only by
`run_bash_timeout`, and this machine's page file is capped by choice, so a demand-side OOM
was the live failure rather than a theoretical one.

The filed item assumed bwrap would do it. **bubblewrap 0.9.0 has no `--rlimit` flag** —
measured, first thing. It has `--size` for a tmpfs and nothing else. So the rlimits come
from a `prlimit` launcher in front of bwrap, which inherits them through. Not `preexec_fn`:
tool calls run through `asyncio.to_thread`, so this process is multi-threaded, and that
hook runs between fork and exec where only async-signal-safe calls are legal. The launcher
also lands in the argv, which keeps every cap assertable by the pure-argv tests that run
where no bwrap exists.

`RLIMIT_AS` is an approximation and the setting says so: address space is not resident
memory, so a Go runtime or a sanitiser reserves far more than it uses and will trip it. The
accurate control is a cgroup memory limit. No unprivileged process here can set one —
`systemd-run --user` finds no bus under WSL, though `/sys/fs/cgroup/cgroup.controllers`
does list `pids`, which would be the better process cap if it were reachable.

**The process cap is kept despite three measured defects, because the alternative is an
OOM.** `RLIMIT_NPROC` counts per real uid across the machine, so concurrent delegations
share one budget and a bomb in one starves the others. Below roughly 64 bwrap cannot create
its namespaces and every command fails at startup — 32 failed outright with only eight
processes owned. And when the cap binds, the shell cannot fork to report it: a bounded fork
storm against 256 died with no output and a bare exit code.

Getting that measurement right took three attempts, which is the part worth recording. The
first loop was sequential, so concurrency never passed two and every cap looked inert. The
second counted iterations, and `cmd &` returns success whether or not the fork happened, so
a capped run still reported 400. Only counting files written by children that actually ran
showed the cap binding. Two plausible experiments agreed with each other and were both
wrong, which is the failure mode reasoning cannot catch.

`RLIMIT_CORE` is pinned to zero rather than inherited, and is honestly a no-op here: this
host's soft limit is already zero, and the "(core dumped)" a shell prints alongside SIGXFSZ
comes from the kernel setting WCOREDUMP because `core_pattern` is a pipe, with no file
written either way. It is kept for a host whose default differs. An integration test
asserting "no core is left behind" was written, **passed with the flag removed**, and was
deleted — the fifth check found here that could not fail.

Rejected: sizing the shadow tmpfs by config. A shadow exists to be empty, so it gets a
small constant and no knob. Rejected: running unbounded when `prlimit` is absent — a
control that is silently off is what ADR-0034 deleted the last of, so a missing limiter
refuses the same way a missing bwrap does.

## ADR-0053 — 2026-09-05 — An agent file's network and binds are operator-gated, and gated differently from each other — Accepted

Two frontmatter fields grant what the sandbox otherwise withholds: `extra_binds` mounts a
host path read-only, `network` re-shares the namespace. Their whole validation was a
boolean parse and `os.path.isabs`, and two of the three lookup tiers sit inside the
caller's `workdir` — so a markdown file checked into a repository under review could take
both. ADR-0036 saw half of this and answered it at the mount, scanning binds for secrets
afterwards; covering matches afterwards is not the same as deciding what may be bound, and
only the second is an operator's to decide.

`DELEGATE_AGENT_BIND_ROOTS` is the containment, and deliberately does not fall back to
`workspace_roots` the way `workdir_roots` does. The first reason is ADR-0036's own, for
having no root check at all: the field exists to reach a toolchain outside a workspace. The
second is that a list shared with a reading tool lets a root widened for `files[]` widen a
mount.

**The two fields are gated differently, and that asymmetry is the decision.** A bind can be
pinned to a root; `--share-net` cannot — no destination list, no proxy, no filter, so the
grant is everything the workstation can reach and is unbounded once given. `network`
therefore needs the agent named in `DELEGATE_AGENT_NETWORK_ALLOWED` *and* the file found in
`agents_dir`. The name alone is not enough: the workspace tiers are searched first, so a
repository shipping `.claude/agents/<an-allowlisted-name>.md` shadows the operator's own
file and would inherit its grant by matching a string an attacker chose. Provenance is what
that string cannot forge. Do not later "tidy" one field's rule onto the other — a downgrade
in one direction and pointless in the other.

Checking a path and mounting a different one would have made this decorative, and it did:
`build_argv` emitted the frontmatter string verbatim, so a link inside a root was resolved
by the kernel at mount time, inside the sandbox, having never met the check. The resolved
path is now what is checked *and* what is carried forward, the same shape as
`resolve_workdir`, so there is no second resolution to disagree with the first. This closes
redirection, not substitution — a different directory later moved to an approved path is a
function of the path either way, exactly as ADR-0049 found for reads — and not a symlink
*underneath* an approved bind, which the secret scan skips by design and still does.

Enforcement is a single site, in `validate()`, which reads the `allowed_tools` two-site rule
rather than excepting it: that rule exists because a model can call a tool it was never
offered, so the declared list and the executor are two operations. These fields have one
producer and one consumer, and `tools.py` merges them with the operator's own
`toolchain_binds` into one untagged tuple, so a downstream check could not tell whose bind
it was anyway.

A bind at or above one of the sandbox's own mounts is refused for a second reason.
`extra_binds` are emitted after `/usr`, `/etc`, `/tmp` and HOME, so naming one replaces it —
read-only, so trust rather than write access: a `/usr/bin/python3` the sandbox did not
provide. **Reordering was the obvious fix and is wrong**, which is the part worth recording.
Emitting the base mounts last stops the shadowing and also wipes every bind *inside* `/tmp`,
which is a tmpfs and is where every temporary directory on Linux lives. Tried on 2026-09-05;
an existing integration test said so within one run. So the ordering stays and the narrow
case is refused: at or above, never merely overlapping, since inside is the ordinary use.
The reserved set is derived from the mount table rather than listed, so a mount added later
reaches it without a second edit, and HOME is unioned in because it is a config value the
static table cannot know. Four of the nine targets are symlinks into `usr/`, so
canonicalising a bind on one lands it inside `/usr` where it shadows nothing and is
correctly allowed — which is why their test asserts the outcome rather than a refusal.

Rejected: a per-agent boolean gate with no provenance test, which the name shadowing
defeats; and refusing a non-canonical bind rather than resolving it, stricter than
`resolve_workdir` for no gain. Deferred: `model`, a comparable grant, is not addressed.

## ADR-0052 — 2026-09-05 — Endpoint captures are dated, local and untracked, and that is not the generated-document pattern — Accepted

We need to know what the endpoint returns, and to notice when it changes. An earlier draft
of this made `docs/ENDPOINT.md` a generated document rendered from a committed fixture,
matching `CONFIGURATION.md` and `TOOLS.md`. **That design is dead deliberately.**

**Why this diverges from the generated-document pattern, which is the most important
paragraph here.** Those documents render from code in this repository, so CI can re-run the
generator and prove the document matches its source. The render input here is an *external
service*. It cannot be tracked without publishing what an endpoint returns, and it cannot be
re-derived in CI without the cluster being up — which would make a cluster outage
indistinguishable from a repository failure. A future reader who notices the inconsistency
and "fixes" it by moving these under `docs/` re-introduces exactly the risk this decision
was taken to avoid. The asymmetry settles it: publication is unrecoverable, while CI
verification of a reference document is a small benefit.

**Location and naming.** `local/endpoint-captures/endpoint-<YYYY-MM-DD>-<served_model_id>.json`.
The date alone cannot tell you two captures straddle a model change, so the model id is in
the name and the engine fingerprint is inside.

**Two enforcement layers, and the second costs something.** `.gitignore` is a convenience —
it stops `git add`, and nothing else; `git add -f`, a rename, or a stray `git add -A` on an
unignored variant all publish silently. `security/secret_globs.txt` is enforcement, and
`check_secret_paths` blocks any tracked file matching it. Both are used, because relying on
one layer has already been demonstrated to fail here: an empty `forbidden_strings.txt`
committed cleanly when only one layer covered it.

The cost is that this file feeds *two* enforcers, so listing the capture directory also
means **the local model cannot read the captures.** That is correct for this data class and
it is why `diff_endpoint_captures.py` exists as a script rather than as a delegation. It is
deliberate, not incidental.

**What may never be captured at all**, as a rule rather than a list of today's fields:
anything that echoes the prompt, which for this server is repository source; anything that
could name a host, including a KV transfer target once offloading is enabled; metric label
values, which carry configuration and handler paths and are stripped as a rule rather than
as a side effect of wanting names; and error response bodies, which can carry a traceback
with cluster paths. **No scanner protects these files.** They are never tracked, so the gate
never sees them, and the probe's allowlist is the only control there is.

**The engine fingerprint is recorded verbatim**, scoped to these local captures. It carries
build, upstream commit, date, tensor-parallel degree and a configuration hash, and recording
it whole is what makes it usable: a hash of it would say only *that* something moved, where
the string says what. The scope does not travel — anything derived from a capture that is
ever published states the mechanism and never the specimen, which is the standing rule for a
public surface no hook can gate and is not relaxed by this.

**Accepted cost: no CI check, so staleness is on the operator.** Nothing will tell you a
capture is a year old. The date in the filename and the fingerprint inside it are the only
mitigation, and they are enough precisely because the file is read by a person asking a
question rather than by a check asserting an answer.

Rejected: a committed values-free schema of field paths and types. It is defensible — the
paths are vLLM's public API surface and all the risk lives in values and labels — but CI
still could not verify the "the endpoint offers this field" half, so it would be a dated
observation like `docs/audits/`, not a generated document. Not now, and not without the
first question being asked again.

## ADR-0051 — 2026-09-05 — The batch delegation tools are cancelled: a batch buys no prefix that separate calls lack — Accepted

`delegate_batch` existed to run several tasks over the same files under one cached prompt
prefix, on the reasoning that one call sharing a prefix must beat several that each pay for
it. That reasoning was never tested, and it is wrong at the only point that matters: the
prefix is already shared.

**A batch and N separate calls are structurally identical.** `Delegation.render()` builds
`agent_body + files_block + task` with the task last, under a system prompt ADR-0011 keeps
byte-constant, and one file-read cache is passed into every `run_delegation`. Two separate
calls over the same `files[]` therefore present the same leading bytes to the endpoint as
two items of a batch do. Nothing about the batch shape creates a prefix; it only changes how
many MCP responses carry the results back.

**The justification was a quantity this server discarded.** The adapter reads
`usage.prompt_tokens` and stops, so `prompt_tokens_details.cached_tokens` — the only number
saying whether a prefix was reused — never reached us. A design the system holding it cannot
measure gets argued about instead.

**On the timing, which is bad and worth stating plainly.** `delegate_batch_readonly` landed
on 2026-09-03 in #91 and is cancelled two days later. What changed is the measurement, not
the opinion. Measuring it at all required a 45k-token prefix: an earlier attempt at ~6k found
no effect and concluded there was none, when in truth everything landed inside 20ms and the
method could not resolve it. Scale decided the method's validity, which is the transferable
lesson.

**A measurement that moved twice in two days is a reason not to encode scheduling in a
document.** On 2026-09-04, three concurrent calls over a shared 45k prefix hit cache once and
took 48.1s against 29.2s serialised. On 2026-09-05, after the operator pulled a configuration
change that made the engine serialise prefills exactly, the same arms hit cache twice and
took 29.5s — indistinguishable from serial, replicated three times with the order alternated.
Both results were true of their day. **Neither rescues the batch tool**, because both describe
concurrency, which callers control regardless of whether this tool exists. The first result
briefly suggested writing "serialise delegations that share a prefix" into the contributor
guide; the second is why it was not written. A scheduling rule outlives the cluster
configuration that makes it true.

**What is genuinely lost, and why it does not save the tool.** A batch returned one MCP
response for several tasks, and `asyncio.gather` held that response until every item settled.
The withholding was narrower than it looked — each item already streamed its turns live,
wrote its own transcript and fired its own progress notification as it finished, so only the
final aggregate dict waited. An as-completed drain was considered and rejected: it would have
passed its tests and improved nothing observable.

`on_turn` goes with them: it existed so a batch could reset the client's idle timer without
reporting interleaved turn counts that describe nothing, and had no other caller. What it
protected cannot be lost on a single path — every remaining tool passes its `ctx` and no
`on_turn`, so the notification falls through to `ctx.report_progress`.

Costs accepted. A caller with five tasks now makes five calls and five approval prompts
rather than one. Against a 27s cold prefill, the extra round trips do not register.

`max_batch_size` goes too; a setting bounding a tool that no longer exists is a setting an
operator has to reason about for nothing.

Rejected: keeping the tools and documenting when to use them, which preserves a model-facing
contract in order to describe a scheduling property of a cluster we do not control. Rejected:
keeping `delegate_batch_readonly` alone because it is newer — age is not an argument, and the
structural finding applies to both identically.

## ADR-0050 — 2026-09-03 — `edit_file` addresses text, and refuses anything but one match — Accepted

`write_file` replaces a file whole, so changing three lines of a long module meant reading
it back and rewriting every line. Every line the model reproduces is a line it can get
wrong, and a hallucinated character lands in a part of the file nobody was looking at — the
tool reports a successful write either way. That is why no code-writing task had been
delegated here yet.

**Exact text, not a line range.** Line addressing landed in #79 and made a range the
obvious shape, and it is the wrong one: line 151 is whatever line 151 now is, so a number
that has gone stale overwrites a different region and nothing can tell. A quotation carries
its own check. It either appears where the model thinks it does or it does not, and the
tool can say so before touching anything.

**Zero matches and two matches are both refusals, and that is the feature.** Refusing an
ambiguous edit costs a turn; guessing costs a corrupted file that the model is told was
written successfully and will not read again. Every refusal leaves the file byte-identical,
which is what lets a model retry with a better quotation rather than attempt a repair — and
every test asserts the bytes, not the message, because a tool that refused and wrote anyway
would pass on the message alone.

**One descriptor for the whole read-modify-write.** `open_resolved` with `"r+b"`, so there
is no second `open` to race and the tool is safe by construction rather than by review.
This is why ADR-0049 had to land first: a read-modify-write is exactly where a validated
string gets used twice, and it would have arrived with the gap that ADR closed. The
truncation happens after the write and is sized to it, never to zero beforehand — the same
open-prove-then-destroy ordering, for the same reason.

Nothing new is configured. Reading in is bounded by `max_file_read_bytes` and the result by
`max_write_bytes`, which is `write_file`'s existing cap; a third limit for the same idea is
a setting an operator has to reason about for no gain. `writes=True` is declared and
`cacheable` left false, so the read-only set derived in ADR-0048 excludes it without anyone
editing a list — verified by removing the declaration, which fails six tests.

Costs accepted. The model must read before it edits, and a quotation from memory will be
refused; that is the same discipline the numbered `read_file` output was added to support.
Long unique quotations cost tokens in the request, which is cheaper than a whole file in
both directions. And a file whose text genuinely repeats needs more context quoted, which
the refusal says.

Rejected: a line range, above. Rejected: replacing *all* occurrences on request, which
turns the ambiguity refusal into a flag the model can switch off, and the check exists
precisely for the case where it does not know how many there are. Rejected: a diff or patch
format, which fails on fuzz and offset and puts a second parser in the trust boundary for a
tool whose whole value is being unambiguous.

## ADR-0049 — 2026-09-03 — Validating a path and opening it are one operation — Accepted

`resolve_all` handed back a string and three handlers opened it later: `read_file` after a
`stat`, `write_file` after an `os.path.exists`, `prefetch` after a `_stat_size`. Checking a
name and using a file are the same thing only for as long as nothing changes in between,
and something can: the adversary is the delegated model itself, which holds a read-write
bind on its workdir under `run_bash` and can retry until a swap lands. Not the passive
window a review would describe.

`open_resolved` is the only sanctioned way to open what the policy approved. It opens, then
proves the descriptor, and returns a handle rather than a path — so there is no string for a
caller to reopen. `read_file` now sizes the file from `fstat` on the descriptor it holds,
which is what `max_file_read_bytes` always meant by checking before reading; opening loads
none of a file.

**What it catches is redirection, not substitution.** Every one of layers 1 to 4 is a
function of the path, so a different *regular* file appearing at an approved path is not a
bypass — those bytes could have arrived through `write_file`. The bypass is the path coming
to name something outside the approved set: a symlink to a key, a swapped parent. Measured
before the code was written: a pre-open substitution of one regular file for another fires
neither check, by design rather than by omission, and saying so here is the difference
between a documented limit and a hole someone rediscovers.

**`O_NOFOLLOW`, despite the standing objection to it.** PLAN.md recorded it as the wrong
fix because it refuses legitimate symlinked checkouts. That is true of the path a *caller*
wrote and false of this one: the flag goes on the resolved path, which `realpath` has
already collapsed, so the final component is known not to be a link at the moment the
policy approved it. A link there now is the attack, not a checkout. It refuses with ELOOP,
translated into a refusal rather than left to surface as "too many levels of symbolic
links".

**Open, prove, and only then destroy.** `"wb"` sets `O_CREAT` and never `O_TRUNC`, because
`O_TRUNC` empties the file at open time — before any proof can run. A check after that is a
report of what was already lost; a truncation after the proof is a guard. This is the half
that turns the write path from a detector into a control, and its test fails against
`O_TRUNC`. `O_EXCL` is tried first so "Created" and "Overwrote" come from the open itself
rather than from a second `stat` on the same string, which was another use of a path checked
once.

**The proof is weaker where the OS will not answer, and says so.** Linux reads
`/proc/self/fd`, measured to agree exactly with `realpath` on both DrvFs under `/mnt/c` and
ext4. Windows has no procfs and no `O_NOFOLLOW`, and falls back to inode identity, which
catches a swap after the open but not before it. Accepted rather than papered over: that
platform has no `run_bash` — bubblewrap is absent, so `available_tool_names` subtracts it —
and therefore no in-sandbox adversary, and it refuses to unlink a held file at all. The
negative test for that branch runs there anyway, because a branch whose only test is
skipped on the platform that uses it is a check that cannot fail.

Costs accepted. A file that vanishes between approval and open is now a refusal on
`read_file` rather than an `OSError` message, and a drop on `search_files` and `prefetch`,
matching `resolve_permitted`'s disposition for paths nobody named. `prefetch` opens files it
then skips on budget, which is an open and no read.

Rejected: comparing inodes alone, which cannot see a redirect; and re-running layers 1 to 4
on the descriptor's path, which is the same comparison spelled longer, since the approved
path is exactly what those layers already passed.

## ADR-0048 — 2026-09-03 — Read-only means nothing can write, not that nothing is offered — Accepted

`delegate_readonly` passed `allowed_tools=[]`, so a read-only delegation answered in one
turn from `files[]` and could look nothing up. That made the one research shape this
project most often needs — grep, read a hit, grep again — reachable only through
`delegate`, which can write and therefore has to stop and ask in plan mode. The cheap,
safe tool was the one that could not do the work.

The fix is not a loosening, because the promise was never what the empty list implied.
ADR-0042 declared the tool read-only so a client gating writes on that declaration could
run it unattended. That claim is about what the delegation *can do*, not about how many
tools it holds. `search_files` and `read_file` cannot write, so offering them keeps the
claim exactly as strong and makes the tool useful.

Two things had to be true first, and both now are. ADR-0016 already prefers the agentic
loop, so the one-shot was a consequence of the empty toolset being falsy rather than a
decision anyone defended. And the loop needed its heartbeat (#77): before that, moving a
delegation onto the loop moved it onto a path that could go silent for a whole turn and
be abandoned by the client while the server worked on.

**The read-only set is derived, never listed.** Each tool declares `writes`, and
`READ_ONLY_TOOL_NAMES` is computed from that declaration. A hand-kept list is the shape
this project keeps finding and naming a check that cannot fail: add a writing tool, forget
the list, and the annotation stays advertised while being false. Verified by injecting
exactly that mistake — `write_file` with its declaration removed — which fails four tests
including the one asserting the executor refuses a write, so the omission cannot ship
quietly.

The toolset stays fixed and stays out of the tool's signature. `resolve_allowed`
intersects rather than unions, so there is no argument a caller could pass to widen it
back; an annotation a caller could falsify would be decoration.

Costs accepted. A read-only delegation now spends turns, so it can hit `max_turns` and
return partial work — which is why `max_turns` became a per-call argument in the same
session, reaching this tool too. It is also slower and dearer than a one-shot for a task
that genuinely needs nothing looked up; `delegate` with an explicitly empty toolset
remains the honest shape for that, and the one-shot path is kept rather than deleted.

Rejected: a fifth tool, `delegate_readonly_search`, leaving the existing one one-shot.
That is two tools differing only in a toolset the caller cannot see, on a contract where
every tool description costs a full prefill to reword.

## ADR-0047 — 2026-09-03 — A no-progress deadline, distinct from the whole-delegation ceiling — Accepted

`dispatch_timeout` bounds total time, and total time cannot distinguish a delegation that
is **merely long** from one that is **wedged**. One must not be killed; the other must be.
Having only the ceiling meant treating them alike, and the price was measured rather than
imagined: on 2026-09-03 a research delegation sat for the full 3600s and died "waiting on
the backend" having completed no turns, while five siblings were refused by admission after
waiting out `admission_wait_timeout` — because the wedged one held a slot it was not using.
At `turn_timeout` 1800 and `retry_max_attempts` 3, that hour was two full-length wedged
attempts back to back.

That also blocked the thing this started as. `dispatch_timeout`'s own description named
Claude Code's 30-minute stdio idle timeout as the constraint it was sized against, and
ADR-0018's per-turn notification had stopped that binding long before; nothing re-derived
the number. But raising a ceiling that cannot see progress makes a stall *strictly* more
expensive for everyone queued behind it. The re-derivation needed the second deadline
first, which is why they land together: the ceiling goes to 14400, and `stall_timeout` at
2100 does the killing.

**The signal is turn completion, and choosing it is the whole correctness of this.** Two
other signals were available and both are wrong. The per-turn progress notification fires
at the *top* of a turn, so it would reset the clock on entry to the very turn that then
wedges. The keepalive fires on a timer regardless of progress — it proves liveness, which
is exactly what a no-progress deadline must not count as progress.

Both deadlines bound every attempt and the tighter wins. Without that the raised ceiling
would let one wedged call sit for four hours, which is the failure being fixed rather than
a smaller version of it. Each enforcement point asks which deadline expired before
reporting one, because the remedy differs: "raise it or shorten the task" is right for a
ceiling and actively wrong for a stall, where raising the deadline only lengthens the wait
for a run that has already stopped progressing.

A one-shot completes no turns, so its deadline runs from entry. That makes its effective
bound the tighter of the two settings, which matters because otherwise the raised ceiling
would become its only bound — and it falls out of the same mechanism rather than a special
case, so the failure still names whichever setting expired.

The lower bound on `stall_timeout` is `turn_timeout` and is deliberately **not strict**.
A strict one was written first and was wrong: `turn_timeout == dispatch_timeout` is a
configuration this project permits, and a strict bound leaves it no legal value at all.
Six existing tests found that within a minute of the check being added.

Rejected: replacing the ceiling with the stall deadline alone. A delegation that keeps
producing turns would then be bounded only by `max_turns`, which the same session raised
to a hard cap of 100 — so the two changes together would have removed any absolute bound.

## ADR-0046 — 2026-09-03 — One prefetch budget, because fairness is admission's job — Accepted

`max_file_tokens` was 40000 against a `max_total_prefetch_tokens` of 140000, and the gap
was doing two different jobs badly. The per-file cap is a **drop** threshold: a file over
it is skipped whole rather than truncated, because source cut mid-function is worse than
absent — the model confidently repairs code it never saw. That part is right and is not
changing.

What was wrong is the second job the gap was quietly doing. Sized separately, the per-file
cap read as a fairness control: no single file may take more than a share of the call, so
one request cannot monopolise a shared KV pool. That reasoning is sound and it is also
already implemented, once, properly, somewhere else. `admission.py` tests four rules —
in-flight requests, summed token estimate, concurrent large prefills, and the endpoint's
own declared concurrency — over counters that `slots.py` shares across every server process
on the machine. Two knobs guarding one property, tuned independently, with only one of them
able to see the machine-wide picture.

The cost was paid in the direction that hurts most. The largest documents are the ones most
likely to have drifted from the code they describe, so a cap that drops them is a cap that
removes exactly what a documentation audit came for — while most of the total budget sits
unspent. The 2026-09-03 audit worked around it by being handed sixteen files by its caller.

So the per-file cap now defaults equal to the total, and keeps one job: an operator's way
of refusing one huge file while still allowing a large total. The relation the code enforces
is unchanged and is the only one that has to hold — `Config` refuses to load when the total
is below the per-file cap, because then no file could ever fit.

**The consequence is real and is accepted, not hidden.** With the caps equal, one large
file can spend the whole budget, and `prefetch` stops at the first file that does not fit
rather than continuing — so every file sorted after it is skipped too. That stopping rule
predates this decision and is itself deliberate: carrying on to fit whatever happens to be
small enough makes the result depend on an unpredictable size mix, and a coherent prefix of
what was asked for beats an arbitrary subset. What makes it survivable is that nothing is
silent. `Prefetch.accounting()` names every skipped file and why, in the result and in the
prompt, and the model is told to treat a skipped file as unavailable rather than infer it.

Rejected: keeping a separate per-file number and merely raising it. That leaves two
independently tuned controls over one property, which is the thing being fixed, and the
next person to size either one has to rediscover which of them is the fairness control.

## ADR-0045 — 2026-09-02 — Effort is always stated, and `inherit` is how deference is stated — Accepted

`effort` was optional on all four delegation tools, and the value a caller got by saying
nothing was four links away: the call argument, then the agent file, then the registry
row's `default_effort`, then `Config.thinking_default`, which is `low`. Every link is
sound; the problem is the entry to the chain. A caller that never considered the question
and a caller that deliberately wanted the default were indistinguishable, and in practice
almost every call was the first kind. Effort changes what a call costs *and* how good the
answer is, so a value reached by omission is the one argument least able to afford being
chosen by accident. Observed rather than theorised: four consecutive research delegations
in one session ran at `low` because none of them named a level, and the one rerun at
`high` found a blocking-subprocess defect the others had missed.

**Decision — the argument is required.** Dropping the default makes fastmcp mark it
required in the schema, so the model is asked the question rather than allowed past it.
This is the same move ADR-0042 made for `allowed_tools` from the other direction: there,
fixing a value made an annotation honest; here, refusing to supply one makes a choice
visible. Both rest on the same rule — the schema is where a promise about a call can
actually be kept.

**Decision — `inherit` is a fifth accepted value, and not a fifth level.** Making the
argument required deletes the absent value the precedence chain runs on: the merge is
`effort or agent.effort`, so there is no longer a falsy case to fall through, and an agent
file binding `effort: high` would become unreachable through the tool that exists to use
agent files. `inherit` restores that path as a statement rather than a silence. A reusable
agent still carries the effort its kind of work needs, and the call site has to say it is
deferring.

It is normalised to `None` at the tool boundary, before the agent merge, and for a reason
worth writing down: a non-empty string is truthy, so leaving it in place would skip the
very tier it names. Nothing inside the server ever sees the word — internally "no explicit
effort" is still `None`, and `resolve_effort` still refuses the string, correctly, because
reaching it means the boundary was bypassed.

It is deliberately invalid for `default_effort` and `thinking_default`. Those are the two
ends of the chain `inherit` defers along, and a value there would defer to itself. Three
refusals, one per layer, each with a test that feeds it a real violation.

**Not a contradiction of ADR-0013**, which refused a fourth level because it would make
this project's enum disagree with the backend's documented values. `inherit` is not a
level and never reaches the adapter: it is spent at the boundary, and the backend still
sees exactly one of the four words it knows. What ADR-0013 protects is the translation
table; this adds nothing to it.

**Cost, stated plainly.** Sixty tests failed on the first run, every one of them a call
site that had been passing nothing. That number *is* the finding — it measures how much of
this repository was choosing effort by silence. They were fixed by passing `inherit`,
which preserves the old behaviour exactly, so the diff is honest about which calls were
never really decisions.

## ADR-0044 — 2026-09-02 — A generated status file stops earning its place once the plan is short — Accepted

**Context.** `STATUS.md` was rendered from `PLAN.md` plus git from the first week, and it
answered a real question: `PLAN.md` reached 477 lines, and a fixed-size snapshot of where
the work stood was cheaper to read than the document it summarised. Closing M7 removed the
premise from both ends. `PLAN.md` had stopped describing intent and started holding
history, so its length tracked what was finished rather than what anyone was going to do;
and the snapshot itself went wrong in the direction that matters, reporting `Current phase:
all planned work complete` two lines above `Overall: 73 done, 13 open, 7 cancelled`.

**Decision — archive the closed roadmap.** M0a through M7 and `Extra` move verbatim to
`archive/PLAN-milestones.md`. `check_budgets` skips any path with an `archive` component,
which is what the header of `PLAN.md` had prescribed for a recurrence since 2026-08-28 and
what two budget raises in a single day on 2026-09-02 finally made unavoidable. `Extra` goes
too: it was held apart so a milestone's counts meant what they said, and with the
milestones archived there is nothing left for it to be apart from.

**Decision — retire `STATUS.md` and its generator.** `PLAN.md` is now roughly 170 lines of
open work, which is its own snapshot; a summary of it would be a second copy of a short
document, which this project's own ownership rule exists to prevent. Deleted with it:
`scripts/gen_status.py`, the generator pair in `docs_gate.py`, the `--check` step in CI, and
three regression tests.

**What this removes besides a file.** One of those tests,
`test_status_no_vcs_position.py`, existed because a *generated* file had recorded a branch
name and a commit hash — things a squash merge deletes — and because `gen_status --check`
compared only the region above `## Repository`, so nothing looked at the rest. That hazard
belonged to the artefact, not to the plan, and it goes with it. This is the argument for
retiring rather than fixing: the fix would have been a fourth test guarding a file nobody
reads.

**Consequences.** The three agent definitions that read `PLAN.md` to tell built from
not-yet-built now name the archive as well, because "not in `PLAN.md`" stopped meaning "not
built" the moment completed items left it. `README.md` no longer sends a new reader to a
summary before the thing summarised. The counts are gone: 73 done and 7 cancelled are no
longer stated anywhere derived, and live in `CHANGELOG.md`, the archive and git instead.
That is accepted — the number was the least-read part of the least-read file.

**Review.** Completed items stay in `PLAN.md` deliberately, as a record of what a recent
session did; nothing here prescribes moving them. Passing the budget is a prompt to ask
whether any of them are ready to archive, not a requirement that they be moved and not on
its own a reason to raise. If `PLAN.md` ever grows long enough that it stops being its own
snapshot, that is the condition under which retiring a summary was the wrong call.

## ADR-0043 — 2026-08-31 — A delegation is also written as it happens, and that stream carries what the model said — Accepted

**Context.** ADR-0024 adopted an operator transcript and ADR-0039 settled what a record
holds. Both describe a record written once the dispatch is over. That answers what
happened and cannot answer what is happening: a file that appears at the end is silent
during the only period when the question "is this stuck, or is it working" can be asked.
The gap showed up in practice — a delegation that had in fact been running for half an
hour was indistinguishable, from outside, from one that had died.

**Decision — a second file, appended per turn.** Alongside the record, a dispatch appends
one JSON object per line as it runs: a `start` when it begins, a `turn` as each completes,
an `end` whatever ends it. Append-only and flushed per line, so a reader tailing it sees a
turn the moment it lands and can never read half of one. It is not derived from the record
and the record is not derived from it: the stream must survive a dispatch that never
reaches an end, which is exactly the case the record cannot describe.

**Decision — the stream carries the reply text.** This is the part ADR-0039 appears to
forbid and does not. That decision excluded file *bodies*, for two stated reasons: they
are the overwhelming majority of the bytes, and they are recoverable from the repository
by path. A reply is neither. It is small, and it exists nowhere else — which is the exact
argument ADR-0039 used to write the task verbatim. The same reasoning reaches the same
answer, so this extends that decision rather than reversing it, and ADR-0039 stands.

**What this does newly expose.** A model may quote a file in its reply, so bodies can
reach the stream indirectly, in fragments, where they could not reach the record at all.
This is accepted rather than mitigated: the alternative is withholding the thing the
stream exists to show. Two properties bound it. The model has already passed the
secret-glob and gitignore layers before it sees anything, so a quote cannot contain what
those layers refused. And the operator chooses the directory — as ADR-0039 put it,
whoever configures it owns what lands in it. That choice now carries more weight than it
did, and one case deserves naming: a transcript directory inside a synchronised or
backed-up location is a directory whose contents leave the machine, on someone else's
schedule and to someone else's storage. Where the record held paths and accounting that
was a modest consequence. With replies in the stream it is no longer modest, and it is
the operator's decision to make knowingly rather than by default.

**Decision — throughput is measured over the backend call.** A turn's wall clock includes
tool execution and any wait for an admission slot. Dividing output tokens by it would
report the cluster as slower than it is, and a throughput figure that is quietly measuring
the wrong interval is worse than none because it gets believed. Both intervals are
written, so the difference between them is visible rather than hidden inside one number.

Review point: the stream has no retention cap, on the same reasoning ADR-0039 gave for
the record. Replies are larger than accounting. Revisit once there is evidence about size
rather than before.

## ADR-0042 — 2026-08-31 — A sixth tool, because a read-only delegation cannot be expressed as an argument — Accepted

ADR-0005 fixed the surface at five and asked that a sixth be argued for. This is the
argument.

It is the second partial supersession of ADR-0005; ADR-0031 was the first, correcting the
claim that agent files are portable. ADR-0005's heading names both.

A client decides whether to stop and ask *before* a call runs, and the only thing it can
read at that point is the tool's own annotation. MCP permission rules match on tool name
and never inspect arguments, so `delegate(allowed_tools=[])` -- which is genuinely
read-only -- is indistinguishable from `delegate()`, which hands the local model
`write_file` and `run_bash`. A read-only *call* therefore cannot be expressed. Only a
read-only *tool* can.

That gap is not theoretical. Approving the prompt in plan mode approves the call, not its
contents: the server is never told which mode the client is in, and a plain `delegate`
approved during planning will write to the repository. Demonstrated before this was
written, by delegating a write and finding the file.

ADR-0005's reasoning survives whole. A new *kind* of delegated task -- review, migration,
test-writing -- is still a markdown file, and `delegate_readonly` is not a new kind of
task. It is the same task under a constraint the caller must be able to state in advance.
No agent file can carry that constraint, because the annotation belongs to the tool the
agent is reached through, and that tool can write.

`allowed_tools` is fixed at `[]` rather than defaulted to it, and is not a parameter at
all. An annotation a caller could falsify by passing an argument would be exactly the
check that cannot fail, which this repository has already found four of.

What this does not buy: it does not make plan mode read-only. It makes one tool read-only,
so that anything which ran without approval wrote nothing. Approving a plain `delegate`
still permits writes, and should.

Review point: if MCP ever grows per-call annotations, or a client learns to gate on
arguments, this tool becomes redundant and should go.

## ADR-0041 — 2026-08-30 — Bulk directories are covered and skipped, not the denylist — Partially superseded by ADR-0062, ADR-0064

**Context.** `run_bash` was refused on every real project. The mount-level secret scan
walks the workdir before each call and refuses past `secret_shadow_max_entries`, which is
right — a partial denylist reads exactly like a complete one — but the default was
measured on a checkout with no virtualenv in it. `config.py` said as much in its own help
text: "This repository scans in 230." With `.venv` present it walks 10,586. Found by
running the tool rather than the suite: every scan test builds one to twelve files by
hand, and the two that exercise the budget do it by lowering the cap to five, so nothing
ever asked whether a realistic tree passes the shipped default.

**Decision — a second list, covered with the same mount and pruned from the walk.**
`opaque_globs_file` names machine-generated directories. A match is covered with the tmpfs
a matched secret directory already gets, and pruned for the same reason. Measured on this
repository, on /mnt/c: 10,586 entries and 66s walked, against 248 and 0.7s covered. The
scan runs per call, so the 66s was not a slow start but the feature's whole cost, paid
before every command.

**Covering is what makes skipping safe.** A secret inside an opaque directory is hidden by
the mount over its parent whether or not the walk looked inside. Pruning *without*
covering is a hole, and it is the easy mistake: the obvious rule — skip whatever gitignore
ignores — drops `.env`, which is gitignored, from the scan that exists to find it. Both
wrong builds were constructed and shown failing; prune-without-cover is caught from inside
a real sandbox, by a shell reading the file, because no argv assertion can catch it —
there is no op for a path the walk never visited.

**Decision — separate file, and a missing one is not fatal.** The denylist is a security
control and its absence stops the server, because a list matching nothing is
indistinguishable from one that passed. This list only decides what the walk skips, so an
absent file warns. Sharing one file would make a slow scan fixable by editing the security
list, which is the one edit nobody should make for a performance reason.

**Alternative rejected — raise the entry budget.** Worse than slow. Every call would walk
the tree, and once inside a virtualenv `*secret*` and `*credential*` match ordinary
library filenames — `keyring/credentials.py`, `pydantic_settings/.../secrets.py`,
`certifi/cacert.pem`. The scan mounts `/dev/null` over each, so raising the cap spends a
minute and then breaks the imports and the TLS roots of the environment it just read.
Twenty of the twenty-two matches on this repository were of that kind.

**Alternative deferred — `os.scandir`.** About 76% of the walk is one `stat` per entry
across the Windows boundary, and a dirent-cached symlink test removes it: 68.0s → 16.3s
without the stat, 9.6s with `scandir`. A real 7x, and still not a feature at 9.6s per
command, so it does not substitute for pruning. Recorded with its numbers so the next
person need not re-measure; worth doing only if the walk ever matters again after pruning,
where it is 0.7s.

**Consequences.** A regression test builds a project-shaped tree and scans it with the
shipped lists and a budget just above what the project is worth without its virtualenv, so
it passes only while the shipped file still names one — deleting that line fails three
tests. `node_modules` is on the list from measurement rather than principle: a
32,184-entry project is refused on every call without it and scans in 3s with it, and
raising the cap instead is a ~3 minute walk per command — so `npm test` inside the sandbox
is not something the entry costs, it is unreachable at any setting. The tmpfs that covers
a directory is writable, so a command writing into one appears to succeed and leaves
nothing behind. The refusal message names the lever.

## ADR-0040 — 2026-08-30 — Admission counts the machine, through a locked file every process shares — Accepted

**Context.** ADR-0012 set the policy and ADR-0038 made it one atomic predicate over four
rules. Both reasoned as though the server were the only thing spending the budget, and
`admission.py` said so: one instance per process, "the global budget by construction".
Transport is stdio, so the client spawns one server per registration: two editor windows
on two projects are two servers with two sets of zeroed counters against one KV pool, and
the cluster sees the ceiling times the number of windows open. The rules were never wrong
— the scope was, and the docstring asserting otherwise kept it invisible.

**Decision — a file of per-process records, on tmpfs, shared by every server.** Each
process keeps one record of what it holds; the four rules are tested against the sum.
`slots.py` owns the storage and the reclaim, `admission.py` keeps the policy — the
predicate is the same function, reading summed totals instead of local attributes.

**Decision — the predicate is evaluated inside the lock.** Reading the totals, testing the
rules and publishing the result are one critical section under one exclusive `flock`. The
obvious alternative — return totals, decide, write afterwards — is a time-of-check race in
which two processes both see room and both take it, widening exactly when the cluster is
saturated. `admit()` therefore takes the predicate as a callable rather than returning
numbers. ADR-0038's property survives across processes: nothing is partially acquired, and
a waiter that does not fit holds nothing anywhere.

**Decision — a record is keyed by `(pid, start_time)`, reclaimed on liveness.** The start
time is field 22 of `/proc/<pid>/stat`. A record whose PID is gone, or live under a
different start time, is dropped by the next process to take the lock. A `kill -9`d window
costs nothing — no heartbeat to miss, no timeout to wait out — and PID reuse cannot
inherit a dead process's slots. The staleness timeout beside it is a backstop for
platforms without `/proc`, where reclaiming on a timer would either leak for its length or
evict a live process that was merely slow.

**Decision — one shared file per machine; the operator separates them.** Keying it by a
digest of the endpoint was designed and rejected: it shares correctly until one project's
registry drifts, at which point the digest changes and the two installations *silently
stop sharing*, reintroducing this bug with no symptom. `slots_dir` makes separation
explicit where it is genuinely wanted. Differing budgets are likewise not negotiated: each
process enforces its own limits against the global totals, so a stricter one simply waits
more.

**Decision — never block the loop, never wedge on a corrupt file.** The lock is `LOCK_EX |
LOCK_NB` retried around `await asyncio.sleep`; a blocking `flock` would freeze delegations
already running and waiting for nothing. An unparseable file is reset rather than refused,
because refusing every delegation on the machine until someone deletes a file by hand
turns a latency protection into an outage.

**Alternative rejected — one shared server over `streamable-http`.** It would make the
gate global by construction and delete this mechanism, and stays the better answer if the
configuration model changes. It is blocked by that model, not the transport: config,
registry, path roots and the agent directory are all loaded once per process, so one
shared server means one project's path policy and agent roster applied to every client — a
redesign, not a flag.

**Consequences.** `backend_status` gains a `cross_process` block: whether the shared
budget is active, the machine-wide totals, and how many processes hold slots — observed
rather than configured, because those differ exactly when something is wrong. Without a
lock (Windows, where the suite runs but the server never does) it degrades to per-process
and says so. The tests use two real processes, negative-tested against a build with
sharing removed: "it eventually ran" passes identically against a gate counting the
machine and one counting nothing.

## ADR-0039 — 2026-08-30 — A transcript record holds paths, accounting and the task, but never file contents — Accepted

**Context.** ADR-0024 adopted an operator dispatch transcript and named the two bugs
upstream shipped in one. It did not say what a record contains, and that turns out to be
the decision with consequences: a record is written to local disk and kept indefinitely.

**Decision — paths and accounting, not file bodies.** A record names every file the
delegation was given, with byte counts, token estimates and skip reasons, but not their
text. The text is recoverable from the repository by path, it is the overwhelming majority
of the bytes, and writing it would put every prefetched file at rest on disk for as long as
the directory exists. The alternative considered, full fidelity with compaction after a
retention window, was rejected as machinery bought for little: the window is most of the
exposure, and there is no evidence yet about how large records actually get.

**Decision — the task is written verbatim.** It exists nowhere else, and a record that
cannot say what was asked does not answer the question a transcript is opened to answer.
Files already pass the secret-glob and gitignore layers before a model sees them, so they
are no new exposure; a task string passes nothing, and today it only crosses the network.
Writing it accepts that whoever configures the directory owns what lands in it. Stated
rather than left implicit, because the difference between in-flight and at-rest is the
whole of what changes here.

**Decision — no retention cap.** Records without file bodies are small enough that a cap
would be a setting nobody could tune from evidence. Revisit if the directory grows in a way
anybody notices; a cap added now would need a negative test proving it prunes, for a
pressure that has not been observed.

**Decision — one file per dispatch.** `delegate_batch` runs items concurrently, so a batch
has as many writers as items. Per-file sidesteps append atomicity rather than reasoning
about it, and the filesystem here may be `/mnt/c`, where reasoning about it would be
reasoning about the wrong one. The agent name is in the filename as well as the record,
because a directory of files nobody can search restores the bug in its only surviving form.

**Decision — a configured transcript turns per-turn recording on for itself.** The turn
loop keeps `TurnDiagnostic` records only when told to, so a transcript reading whatever the
caller happened to request would be empty for nearly every delegation. It asks for them
independently, and the caller's own flag still decides, separately, what the reply carries.
Verified to change nothing backend-visible: the flag is loop-local bookkeeping and does not
reach the request.

**Consequences.** Enabling the directory changes no response, which is asserted by
comparing whole responses rather than by looking for known field names — a leak of a field
nobody thought to list is the leak that would actually happen. Records carry real token
usage rather than the admission estimate, so they can be summed later to say what the
cluster has spent; an estimate standing in would be the right shape and the wrong number.

## ADR-0038 — 2026-08-30 — Admission is one predicate over four rules, sized by two numbers — Accepted

**Context.** ADR-0012 fixed the policy — three rules, queue rather than fail, report
high-water marks — and the four settings it implies have sat in `config.py` since M0b
being read by nothing. Building it raised three questions ADR-0012 does not answer.

**Decision — one predicate, not gates in series.** Four semaphores acquired in turn would
let a request take the sequence slot and then block on the large-prefill cap, holding
capacity it is not using for the whole wait and starving smaller requests that fit every
rule. That is not a corner case: ADR-0012 says the large-prefill cap is what actually
binds for big tasks, so "admitted under one rule, blocked by another" is the normal case.
One condition variable over plain counters, checked as a single atomic boolean. Nothing is
ever partially acquired, so a waiter that does not fit holds nothing.

**Decision — the endpoint's own `concurrency` becomes the fourth rule.** ADR-0037 enforced
it inside `delegate_batch` with a local semaphore, which bounded a batch against itself and
nothing else: two batches, or a batch beside a plain `delegate`, could still exceed it, and
a single `delegate` was never checked at all. `max_inflight_seqs`' own description already
claimed both were checked. Folding it in makes that true on every path and leaves one gate
rather than two that can disagree.

**Decision — two numbers size a request.** Its KV footprint is the prompt plus the reply it
is permitted to generate, and that is what the token budget counts. Its prefill is the
prompt alone, and that is what classifies it as a large cold prefill. Conflating them is
not cosmetic: `max_tokens` defaults above `large_prefill_tokens`, so classifying on the
total makes *every* delegation large and silently bounds the whole server at
`max_inflight_large_prefills` while every other rule reads as though it were the one
binding. Found by a test that passed for the wrong reason — the batch bound held with the
endpoint rule deleted — which is the failure mode CLAUDE.md's negative-testing rule exists
to catch, caught this time by negative-testing rather than in production.

**Decision — the estimate is fixed when a slot is granted.** A long agentic delegation's
true footprint can exceed it late in the loop, so the token rule is a floor-time
approximation. Growing it per turn would couple the gate to the turn loop's internals and
add a reconciliation path on every abort. ADR-0012's own closing line is the answer:
`peak_inflight_tokens` is the cheaper way to find out the trade was wrong.

**Decision — the wait has its own timeout.** `dispatch_timeout`'s deadline is computed
inside `run_one_shot` and `run_agentic_loop`, which do not run until a slot has been
granted, so it cannot bound a wait that ends before it starts. Reusing it would mean
threading a caller-supplied deadline through both and every test asserting on their
deadline maths. `admission_wait_timeout` therefore stacks rather than dividing one budget,
and defaults well below it: a wait that long means the budget is misconfigured or the
cluster is wedged, and an error naming the rule that bound is worth more than an hour spent
queued. A timeout also names its rule and is counted, because a limit nobody can see having
been hit is a limit nobody revisits.

**Consequences.** Four settings that were inert now change behaviour: work that ran
immediately may queue. Zero in any of them is refused at load — it does not mean unlimited,
it means nothing is ever admitted, which presents as congestion rather than as the
misconfiguration it is.

## ADR-0037 — 2026-08-30 — `delegate_batch` shares one prefix, bounds itself by the endpoint, and reports per item — Accepted

**Context.** `max_batch_size` existed from M0b and nothing else did: no shape, no failure
contract, no concurrency rule. Three questions had to be answered together, because the
answer to the first constrains the others.

**Decision — one agent, one `files[]`, many tasks.** The prompt order ADR-0011 fixed is
system, agent body, files, task last, chosen so the task is the part that varies. That is
already a description of a batch. Items that share everything but the task share a prompt
prefix, and the cluster serves a shared prefix from cache, so eight questions about one
module cost roughly one read of it. The alternative considered, fully independent items,
was rejected because every item is then a fresh prefix: it saves round trips and nothing
else, which is close to the caller making N calls. It also discovers a path refusal in item
nine after eight items have been paid for, where a shared `files[]` resolves once, before
anything is sent.

**Decision — bounded by `entry.concurrency`.** The registry has declared a per-endpoint
limit since M1 and nothing enforced it, because until now nothing here ran two requests at
once. A batch is the first thing that can, so it is the first thing that could break that
promise. This is not the global token budget: that is M7's, it is a different quantity, and
`max_inflight_seqs` stays inert.

**Decision — per-item results, and the batch always returns.** Each item carries its own
`ok`, and a failed one carries `error` where its answer would be. Failing the whole batch
would discard compute already spent on the items that worked, and on a shared cluster a
single transient refusal is not rare. This is ADR-0007's stance applied to a new unit: report
what the server observed, per unit of work, rather than a summary that flattens it.

**Consequences.** A caller must read `failed` before trusting any summary of a batch — a
partially failed batch still returns successfully, which is the trade for not discarding
work. Progress is reported per finished item rather than per turn, because turn counts
interleaved from items running concurrently describe nothing anyone can act on.

## ADR-0036 — 2026-08-30 — `extra_binds` is scanned for secrets, because an agent file now chooses it — Accepted

**Context.** `discover_secret_shadows` scanned the sandbox HOME and the workdir and
deliberately skipped `extra_binds`. Two reasons were recorded. They were paths "an operator
chose, typically somewhere under `/usr`", so scanning them spent latency where credentials
do not live; and covering a file inside a read-only bind "protects nothing that bind did not
already protect".

M6 lets an agent file supply `extra_binds`. A markdown file that anyone can add to a
repository is not an operator decision, and the whole force of the first reason was that a
person with server access had typed the value. The second reason was never right for
secrets specifically: a read-only bind stops a file being *edited*, and nobody was worried
about a private key being edited. Read-only means readable, and being read is the threat.

**Decision.** The scan covers `home`, `workdir` and `extra_binds`. The alternative
considered was constraining `extra_binds` by the layer-1 root check instead, and it was
rejected because it defeats the field: its documented purpose is binding a toolchain such as
`uv` from under the home directory, which is deliberately outside the workspace roots.

**Consequences.** The walk is wider, bounded by the `secret_shadow_max_entries` and
`_max_depth` limits that already existed. An operator binding a large tree pays for it once
per call. Verified by reading a planted key from inside a real sandbox and getting nothing,
then removing the change and reading it successfully — the control was demonstrated failing
before it was claimed to work.

What this does not change: the scan is still point-in-time, still skips symlinks, and is
still defence-in-depth for one tool rather than the authority `paths.py` is for `read_file`.
ADR-0035 governs all of that and stands.

## ADR-0035 — 2026-08-29 — The secret denylist is enforced by covering paths up, not by leaving them out — Partially superseded by ADR-0062

`paths.py` enforces the denylist by refusing a path. The sandbox cannot: bubblewrap starts
from an **empty root**, so there is nothing to subtract from. A secret is only ever visible
inside the sandbox because it sits within a tree that had to be bound whole — the sandbox
HOME, or a workspace. So the denylist is enforced afterwards, by mounting something empty
over each match: `--tmpfs` over a directory, `--ro-bind /dev/null` over a file. Both
primitives were checked against a running bwrap 0.9.0 rather than read off documentation,
which is now the third time that has been the difference between a working argv and a
plausible one (ADR-0021).

**Covering up needs a concrete path, and the denylist holds patterns.** Hence a walk, which
is filesystem I/O in a module whose whole design rests on `build_argv` being pure. The walk
therefore lives in `run`, and `build_argv` takes the results as a parameter. Folding the I/O
into `build_argv` would make the bind-ordering rules — the security property — assertable
only on a machine with a real bubblewrap and a real tree, which on Windows, where a
contributor's first `pytest` happens, means not at all.

**One matcher, not one list.** `secret_match` is lifted out of `paths.py` and shared. Sharing
only the file would leave two readings of it: a pattern could deny `read_file` while leaving
the same file readable from a shell, and nothing would report the disagreement. This also
means the two layers share their limits, deliberately — both match *resolved* paths, so
neither covers a link whose name matches the list but whose target does not. That gap is
stated rather than closed, because closing it in one layer only would be worse than having
it in both.

**Only HOME and the workdir are scanned.** Toolchain binds are read-only paths an operator
chose, usually under `/usr`. Scanning them is latency spent where credentials do not live,
and a shadow inside a read-only bind protects nothing that bind did not already protect.

**The scan is bounded, and running out of budget refuses the command.** Fail-closed, for the
same reason `load_secret_globs` refuses a missing denylist: once the command is running,
partial coverage is indistinguishable from full coverage, and what was left uncovered is by
definition what the list exists to cover. The bound is also a latency ceiling — the scan runs
per call, on `/mnt/c`, which is roughly 12x slower (ADR-0020).

**Symlinks are skipped, and the reason is measured.** Emitting a shadow op on a symlink node
does not follow it and does not create it; it aborts the entire invocation with `Can't mount
tmpfs on ...: No such file or directory`. That is fail-closed and leaks nothing, but a
`~/.ssh` symlinked into a dotfiles repository is common enough that every `run_bash` call
would die naming neither the denylist nor the link. Skipping loses little: a link's target is
either inside a bound root, where the walk reaches it by its real path, or outside every
bound root, where the sandbox never bound it and the link dangles.

**What this is not.** It is defence in depth for one tool, not an authority. The scan is
point-in-time, and `run_bash` holds a read-write bind for the whole call, so a file the
command itself writes afterwards is not covered and cannot be. `paths.py` remains the sole
authority for `read_file` and `write_file`; these stay independent layers, not redundant ones
(ADR-0010). A reading of this decision that treats the sandbox as making the path policy
unnecessary has the direction backwards.

---

## ADR-0034 — 2026-08-29 — No setting runs a shell unconfined, and bind order is part of the policy — Accepted

Three decisions taken while writing `sandbox.py`, recorded because each is easy to
reintroduce and two of them fail quietly.

**`sandbox_enabled` is deleted rather than implemented.** Its description promised that
`DELEGATE_SANDBOX_ENABLED=0` was "an explicit, logged choice to run shell commands with no
confinement". ADR-0010 refuses when bubblewrap is *absent*, and the escape hatch was written
as the compatible other half: an operator's deliberate opt-out rather than a silent degrade.
It is deleted because that distinction does not survive contact with the rest of the system.
Nothing downstream can see the difference — not the caller, not the model, not the tool
result — so a server with the sandbox switched off is indistinguishable from one with it on
until something has already run unconfined. A control that can be turned off from outside
the code is a control whose state has to be checked to be believed, and nothing here checks
it. The field is removed, not left as a no-op, because a `bool` that can only be `True`
renders in the generated reference as a knob that does something.

**Bind order is a security property, not a formatting choice.** bubblewrap applies binds in
argv order and a later bind shadows an earlier one at or below its path. So HOME binds
before the workdir, and read-only toolchain binds come before the read-write workdir; the
first keeps a workdir nested under the sandbox HOME writable, the second stops an
overlapping toolchain bind pinning the workdir read-only. Both are invisible until two paths
overlap, and the failure of the second names the wrong cause entirely — a read-only
filesystem error inside the directory the operator explicitly chose. `build_argv` is
therefore a pure function, so the ordering is asserted directly rather than inferred from a
command that happened to work.

**`--dir` does not create a bind's source, and the error says otherwise.** The first version
used `--dir <home> --bind <home> <home>`, reasoning that the mount point had to exist.
bubblewrap creates the mount point itself; what it cannot do is invent the *source*, and
`--dir` makes a directory inside the sandbox rather than on the host. On a fresh install this
fails with `Can't find source path`, which reads as a mistyped setting rather than as a
directory nobody has created yet. The host directory is now created before every call. Found
by running the integration tests, not by reading the argv — the same way ADR-0021's two
corrections were found, and the reason those tests exist at all.

---

## ADR-0033 — 2026-08-28 — The changelog is one section per pull request, and append-only documents have no size threshold — Accepted

Two changes with one cause: the instruments did not fit the document.

`CHANGELOG.md` was a flat list of dated bullets under a single `## [Unreleased]`. ADR-0022
says append-only documents cap each entry rather than the total, and this one could not:
`check_budgets` splits entries on `^## `, so the marker would have read 600 lines as one
entry and blocked at once. The 2026-08-27 audit found exactly this and withdrew the finding,
concluding `ARCHIVE-AT` was the right instrument instead. That accepted a limitation of the
tool as a fact about the document.

Restructuring the file removes the limitation rather than working around it. One `##`
section per pull request, newest first, with `Added` / `Changed` / `Fixed` beneath it, means
the sections `check_budgets` already looks for are the entries, and `BUDGET-PER-ENTRY: 30`
works with no change to the gate at all. The heading carries the number, so an entry and the
pull request it describes are named the same way, and a merged section is never edited
afterwards — a correction is a new section, exactly as an ADR supersedes rather than
overwrites.

`ARCHIVE-AT` is removed outright, from the gate and from all three documents that carried
it. It warned when a file passed a line count and pointed at a procedure that split by year;
`CHANGELOG.md` reached the threshold with every entry in the same year, so there was no older
year to move and no action the warning could be answered with. It then fired on every commit,
which is how a warning stops being read. A threshold that cannot be cleared is worse than no
threshold, for the same reason a check that cannot fail is worse than no check: both are
believed. Archiving is now requested by a person, and `check_budgets` still skips any path
with an `archive` component so an archived file stays unbudgeted.

The migration is a cut, not a rewrite. Of 59 entries only 11 carried a pull request number —
the convention began at #16 — so the rest could not become numbered sections without
inventing numbers that never existed. They move verbatim to `archive/CHANGELOG-2026-08.md`
and the new format starts from #20. Rewriting them would have meant fabricating the one
field the new heading exists to carry.

Supersedes ADR-0022 only in part. Its split by document class stands, and per-entry budgets
now apply to `CHANGELOG.md` for the first time; what it loses is the optional total-size
warning for append-only files.

## ADR-0032 — 2026-08-27 — ARCHITECTURE.md splits along module ownership, not along the prose seam — Accepted

At 423 of 425 lines, with M4's turn loop about to land in exactly its territory. Both
audits of 2026-08-27 recommended splitting first, the second calling it overdue: splitting
under pressure mid-milestone is worse than splitting deliberately before.

The obvious cut was the prose seam -- narrative about the shape of a delegation, reference
about the response state machine. `check_split_dodge` blocks it. A document holding only
the empty-answer section has the same audience as its parent and a subset of the parent's
owned code, which is the definition of a size budget being evaded rather than a split. The
check was right, and it reframes the question: not "where does the prose divide" but
"which module does the new document own".

That has one answer. `loop.py` and `backends/` leave ARCHITECTURE.md for DISPATCH.md, and
the four sections describing them go with the code: the wire format seam, retry above the
adapter, per-request reasoning control, and empty-answer recovery. ARCHITECTURE.md keeps
`server.py`, `context.py`, `wsl.py`, `sandbox.py` and `main.py` -- the orchestration around
a dispatch. Moving the prose without moving the ownership would have left the parent
describing code it no longer explained, and the child explaining code it did not own.

The turn loop is `loop.py`, so it now lands in DISPATCH.md rather than back in the file
this relieved. That is the test of whether the seam was real, and it is why the split had
to happen before M4 rather than during it.

ARCHITECTURE.md's budget drops 425 → 330 in the same commit. Leaving it at 425 over 309
lines of content would re-create the headroom that produced the problem; a budget that is
not lowered after a split has not been paid, only deferred.

## ADR-0031 — 2026-08-27 — The agent file format is borrowed from Claude Code, not compatible with it — Accepted

ADR-0005 said the files "use the same format Claude Code already uses for its own
subagents, so they are portable". The first half is the useful part and still holds: the
shape is frontmatter plus a system prompt, which is why the format needed no design and
reads as familiar. The second half is false, and this repository's own
`.claude/agents/*.md` are the counter-example sitting in the same tree.

Claude Code spells the tool list `tools` and takes a comma-separated string; this format
spells it `allowed_tools` and takes a list. Claude Code accepts `effort: medium`, which
`config.py` refuses on purpose, because this project's effort vocabulary is its own
(ADR-0013). And this format adds five keys Claude Code has never heard of — `max_turns`,
`max_tokens`, `keep_tool_results`, `network`, `extra_binds` — because a delegated agent
runs in a sandbox with a budget and a Claude Code subagent does not.

So: borrowed, not compatible. A file does not move between the two unedited in either
direction. Nothing changes in the code — the format was never actually built to be
portable, only described that way — but the claim is load-bearing in
`docs/AGENTS.md`, where a reader could reasonably have copied a file across and expected
it to work.

Found by the second documentation audit of 2026-08-27, which is the argument for auditing
a claim against the repository's own files rather than against its intent.

## ADR-0030 — 2026-08-27 — A file is text if it has no NUL in 8 KiB and decodes as strict UTF-8 — Accepted

Prefetch has to decide whether a file is text before inlining it, and the obvious answer —
trust the extension — cannot work. The extension allowlist is layer 2 of the path policy
and admits `.json` and `.md`; nothing about either forbids UTF-16, a BOM, or a minified
blob. A `.py` full of NUL bytes passes every check upstream of this one.

Two tests, because neither alone covers the ground. A NUL byte in the first 8 KiB catches
UTF-16 and most compiled output for the price of a substring search. A strict UTF-8 decode
catches what is left: latin-1 text, a truncated multi-byte sequence, anything mislabelled.
The decode must be strict. `errors="replace"` would hand the model a page of U+FFFD and
present it as source, which is the same failure as truncation — a plausible-looking input
the model will reason confidently about — with none of the visible symptoms.

8 KiB rather than the whole file because a binary that hides its first NUL past that point
is rarer than the cost of scanning every file to the end, and the UTF-8 decode covers the
tail anyway. A UTF-8 BOM decodes fine and is stripped rather than treated as content: it
is invisible, and left in place it sits at the top of the first line of a source file for
no reason anyone can see.

The alternative considered was `git diff --numstat`-style heuristics, or libmagic. Both
add a dependency or a subprocess to answer a question two lines of Python answer, and
neither is more correct for the file types this actually sees.

A binary file is a **skip**, not a refusal: the call proceeds without it and the accounting
says why. It is a fact about the file, not a permission decision, and the permission
decisions all belong to `paths.py`.

## ADR-0029 — 2026-08-27 — Tool results never carry the endpoint address — Accepted

The adapter already keeps the host out of its exception strings, on the reasoning that an
exception reaches a log and from there a pasted issue comment. `backend_status()` is the
first tool whose **return value** could carry it, and a health report is more likely to be
pasted somewhere public than a stack trace is — that is what a health report is for.

So the rule extends from exceptions to results: nothing a tool returns names the endpoint.
`backend_status()` reports the registry key, the failure layer and the HTTP status, which
is what the reader acts on anyway; the address is in `models.toml`, which is gitignored
precisely because it names a host. The probe's model list is withheld for the same reason,
a count standing in for it — a list of served ids is somewhere an internal name leaks
without anyone choosing to disclose it.

This costs something real. Diagnosing which of several endpoints failed now means reading
the registry key and looking it up, rather than seeing the URL. Accepted, because the
alternative leaks by default and is discovered only after it has been published, at which
point it cannot be withdrawn.

The rule binds future tool output too — the operator dispatch transcript in M7 most
obviously. A tool that genuinely cannot be diagnosed without the address should say so and
be argued on its own terms, rather than each author re-deriving this from first principles.

## ADR-0028 — 2026-08-26 — An amend is judged against HEAD~1, and a pass that relied on it says so — Accepted

The owning-doc check compared the staged index against HEAD. Correct for a normal commit;
wrong for `git commit --amend`, whose parent is HEAD~1. The files already inside the commit
being amended are part of what lands but are absent from the index, so a complete commit was
reported as incomplete. The documented workaround was `git reset --soft HEAD~1` and recommit
— considerable ceremony to answer a question the tool had got wrong. It fired twice while
this session's own commits were being prepared.

**It cannot be detected outright, and that is the whole difficulty.** `prepare-commit-msg`
receives the message source and the commit it came from, and `git commit --amend` arrives as
`source=commit, sha=HEAD`. So does `git commit -C HEAD`, which is *not* an amend and whose
parent is HEAD. `GIT_REFLOG_ACTION` is unset for both. All of this was measured in a
throwaway repository rather than assumed, because the first design depended on the two being
distinguishable and they are not.

Rejected: treating `source=commit` as an amend outright. It would silently misjudge
`-C HEAD`, and the failure would be a *pass* — the previous commit's document counted toward
this commit's code. A check that wrongly blocks is annoying; one that wrongly passes is the
thing this repository keeps finding.

Rejected: unioning HEAD's files into the changed set unconditionally. Same false pass, on
every commit rather than a rare one.

So both readings are evaluated. Strict first — index against HEAD. If that passes, nothing
else happens. If it blocks and the message came from HEAD, the amend reading is tried, and a
pass there is reported with a WARN naming the files it counted from the previous commit and
saying plainly what it assumed. The escape is real but never quiet, which is the same
bargain as the `Docs-Gate-Skip` trailer: an escape hatch nobody can see becomes the default
route; one that leaves a line in every run does not.

The marker is consumed when read, and the hook rewrites it on every commit, so a commit
abandoned between the two hooks cannot leave state behind that changes how the next one is
judged.

## ADR-0027 — 2026-08-26 — config.load() reads .env; an MCP client's env key cannot reach the server — Accepted

The README has said `cp .env.example .env` since the first commit. Nothing read it.
`config.load()` consulted `os.environ` and nothing else, there is no dotenv dependency,
and the `mcpServers` block in the README carries no `env` key. Creating the file did
nothing, reported nothing, and left every setting at its default — a documented setup step
that was a no-op, which is worse than an undocumented one because it is believed.

**The alternative does not work here.** Putting an `env` key in the MCP client's
configuration looks like the idiomatic fix, and on a native-Linux install it is. This
project's documented topology is not that: the client runs on Windows and launches
`wsl.exe -d Ubuntu-24.04 -e claude-delegate-local-mcp` (ADR-0002, ADR-0020). An `env` key
sets variables for **wsl.exe, on the Windows side** — one hop short of the Linux process
that reads them. Carrying a variable across that boundary additionally requires `WSLENV` to
name it, set in the Windows environment before launch. Forgetting that produces no error
and no symptom beyond the setting being ignored, which is the same failure this decision
exists to remove.

So `config.load()` reads the file itself, in about fifteen lines, with no new dependency:
`pyproject.toml` pins fastmcp and httpx deliberately, and a dotenv package to read
`KEY=VALUE` would cost more than it explains.

Three choices inside it are deliberate:

- **The real environment wins over the file.** Standard convention, and it keeps an
  explicit one-off override working. The file fills what the environment omits.
- **A file named explicitly and missing is an error**, while a discovered `<repo>/.env`
  that is absent is not. Not having one is normal; asking for a specific one is a promise,
  and silently substituting defaults for a named file is precisely the bug above.
- **Passing `environ` suppresses discovery.** Otherwise the test suite reads whatever
  `.env` happens to sit in the working tree, and passes or fails according to a file
  nobody in the test wrote.

There is no escape processing. `DELEGATE_WORKSPACE_ROOTS` on Windows is a path full of
backslashes, and unescaping it would corrupt it without saying so. A leading `export ` is
stripped and a name that cannot be an environment variable raises, because both otherwise
parse to a key no setting matches — read, accepted, ignored.

## ADR-0026 — 2026-08-26 — main is protected by a checked-in ruleset, with no bypass and zero required reviews — Accepted

`main` is protected by a ruleset checked in as `.github/ruleset.json`, applied with
`gh api -X POST repos/OWNER/REPO/rulesets --input .github/ruleset.json`. Checking it in
makes the configuration reproducible rather than a thing someone once clicked, and a
review of it a diff rather than a tour of a settings page.

Two of its values are not self-explanatory, and JSON carries no comments, so they are
recorded here rather than in the file or in CONTRIBUTING.md — which describes what
contributors must do, not why the repository is configured as it is.

**`bypass_actors` is empty.** Direct pushes to `main` are refused for everyone including
the owner. Rulesets permit this; classic branch protection did not, which is part of why
a ruleset was chosen at all. A protection the owner can step over protects against
accidents only, and the accidents are the owner's.

**`required_approving_review_count` is 0, deliberately.** GitHub will not let anyone
approve their own pull request, so on a single-maintainer repository requiring one review
does not raise the bar — it locks the only maintainer out of their own repository
permanently, with no self-service way back. The four required checks carry the weight
instead: the gate, tests on 3.11 and 3.12, and the secret scan. None of those can be
satisfied by asserting that the change is fine.

This is the value most likely to be "corrected" by someone reading it as an oversight.
It is not. Raise it to 1 on the day a second person can approve, and not before.

## ADR-0025 — 2026-08-26 — Upstream reviews live in docs/reviews/; only a documentation audit resets the audit clock — Accepted

ADR-0023 put upstream reviews in `docs/audits/` without checking what already read that
directory. The audit-due check did: it took the alphabetically last `*.md` there as the last
recorded documentation audit and counted commits since. Audits are named
`YYYY-MM-DD-audit.md`, so a filename beginning with a letter sorts after every one of them
and wins permanently.

Effect, measured: the counter went from 14 -- the whole history, correct, because no
documentation audit has ever run here -- to 0. A warning due at commit 60 would not have
appeared until 74, and each further review would have pushed it out again. The check was
disarmed, silently, by a record that is not an audit at all, in the same change that removed
two other checks which could not fail.

Two faults, and it needed both. The directory mixed two kinds of record, and the check dated
a filename instead of asking git. Fixing either alone leaves the other armed, so both change:

- Upstream reviews move to `docs/reviews/`. `docs/audits/` holds documentation audits and
  nothing else, which is what the counter has always claimed to measure.
- The check asks git for the most recent commit touching `docs/audits/`. Filename order is
  only ever correct while a single naming scheme is in use, and nothing enforced that.

Only the location in ADR-0023 changes. Its substance -- that the review mechanism must be an
artefact inside the repository rather than an issue in a tracker that can be deleted -- is
untouched, and is why this is a partial supersede rather than a replacement.

The general lesson is worth more than the fix: adding a file to a directory is a write to
every check that reads that directory. Nothing here treats a directory as an interface, and
this is the second time a policy file has collided with a check that scanned it -- after the
secret-glob list matching its own `*secret*` pattern.

Not chosen: making the counter ignore filenames it does not recognise. That is a denylist,
and it fails open -- the next unrecognised name resets the clock again. Asking git for
recency cannot be fooled by naming at all.

---

## ADR-0024 — 2026-08-26 — The upstream review of 2026-08-26: adopt six, reject two as already covered — Accepted

First review under ADR-0023. The primary ancestor had nothing new — its turn countdown and
real-exit-code capture are already credited in NOTICE. Everything below comes from the
fork, covering 2026-08-21 to 2026-08-25. Full evidence, per item, in
`docs/audits/upstream-review-2026-08-26.md`.

Adopted as planned work, not as code:

- **Context-overflow handling.** Promoted out of Deferred into M3. They shipped what this
  project parked, and our deferred wording had already converged on the same detection
  signal independently. The design transfers; so do five bugs it cost them, now carried as
  constraints on the item.
- **Diagnostics on success, not only on failure**, with an evicted-then-reread correlation.
  This is ADR-0007 extended from exit codes to context economics: the same argument that
  server-captured truth beats the model's account of it.
- **A steer at the risky call.** Shell text-patching gets an advisory note appended to that
  tool call's own result, gated on the write tool actually being available to the agent.
  Their evidence is that a prompt instruction alone did not stop the pattern on retry.
- **An operator-level dispatch transcript**, independent of any caller-facing flag.
- Two smaller constraints: negative caches expire, and a nudge reply concatenates rather
  than overwrites what the model already said.

Rejected, both because this project already went further:

- An env var for the generation ceiling — `config.py` has that field, at the same value.
  Only the precedence ordering transfers, as an M4 constraint: an operator lowering the
  ceiling must not suppress the per-model bump that exists to stop heavy-reasoning models
  returning empty output.
- An explicit override for the wire-format guess, added upstream after a model name matched
  no recognised prefix and sent every dispatch to the wrong endpoint shape. ADR-0009 removed
  prefix matching entirely, so the override has nothing to override. Recorded because a
  rejection that names its reason is what stops the next review re-litigating it.

Also recorded, and worth more than most adoptions: their early-cancellation investigation
closed with no action possible. The client does not surface progress notifications to the
session, and an in-flight synchronous tool call cannot be cancelled. That independently
confirms ADR-0018's narrow claim — those notifications buy an idle-timeout reset and
nothing else — and closes a question this project would otherwise have paid to reopen.

---

## ADR-0023 — 2026-08-26 — The upstream review is a dated file in this repository, not an issue in someone's tracker — Partially superseded by ADR-0025

ADR-0001 requires upstream fixes to be read and reimplemented, and sets a six-to-twelve
month half-life on the exercise. It named no artefact. CONTRIBUTING filled that gap with
"candidates are tracked in a pinned issue", which was true until the repository was
deleted and recreated on 2026-08-25 to unpublish a leaked identifier. The issue went with
it. A policy whose only named artefact lives outside the repository can be destroyed
without a diff, and nobody notices, because there is nothing to notice.

Two further holes were open the whole time. The `upstream` remote had never been fetched
once, so no local ref existed to review against. And it points at the primary ancestor,
which has been dormant since 2026-08-21, rather than at the fork that is actually moving.
The review this ADR mandates had, in practice, no implementation at all.

So: findings land in a dated file under `docs/audits/`, one per review, recording every
upstream change considered and its verdict. The value is not the adopted items — those
become PLAN entries and leave anyway. It is the rejected ones. Without them, the next
review cannot distinguish what was examined and dismissed from what was never looked at,
and re-examines everything or nothing.

Reviewing read-only through the API rather than by fetching is deliberate, and stronger
than the disabled push URL: with no upstream object in the repository there is no
cherry-pick to reach for under time pressure. `docs/audits/*` is already exempt from the
email-content scan, so a review may quote upstream verbatim where that is the evidence.

Not chosen: watching the fork with a scheduled job. It would produce a feed nobody reads
between reviews, and the half-life in ADR-0001 says the correct cadence is occasional and
deliberate. Also not chosen: repointing `upstream` at the fork. The remote records
ancestry, which NOTICE ties licence obligations to; liveness is a property of the review,
not of the remote.

---

## ADR-0022 — 2026-08-25 — Size budgets differ by document class: total lines for mutable, per-entry for append-only — Partially superseded by ADR-0033

Found by hitting the limit. `DECISIONS.md` reached 372 lines against a 400 budget after a
day's work, and the only available response would have been to raise the number — again
next week, and the week after.

That reveals the rule was wrong for the file, not the file wrong for the rule. ADR-0003
justified budgets as a prompt to ask whether content still earns its place. For an
append-only record, it always does: history does not become less true. A total cap on such
a file generates friction and never once produces a useful decision.

So budgets now come in two kinds:

- `BUDGET: n` — total lines, for **mutable** documents. Unchanged, and still blocking with
  the same three resolutions.
- `BUDGET-PER-ENTRY: n` — longest `## ` section, for **append-only** documents. Keeps each
  entry terse, which is the quality actually at risk, while letting the file grow.
- `ARCHIVE-AT: n` — optional, warns rather than blocks, suggesting older entries be split
  into a dated file. Splitting history is fine; trimming it is not.

`CHANGELOG.md` gets an archive threshold and no per-entry cap, because its `## ` sections
are releases that legitimately accumulate entries across a cycle. Its discipline — every
entry carries the why — is a review matter that no line count can enforce.

The mechanism worked as designed, which is the point worth recording: the budget forced a
review, and the review concluded the budget itself was misapplied. A rule that can be
found wrong by its own enforcement is a good rule.

## ADR-0021 — 2026-08-25 — The sandbox argv, corrected against a running kernel — Accepted

Two corrections to the planned bubblewrap invocation, both found by running it rather
than reading it. Recorded as a decision because both are easy to reintroduce and one
produces a false sense of security.

`--symlink usr/lib64 /lib64` is **mandatory** on x86-64, not "if present on the distro"
as planned. Without it the ELF interpreter is absent and nothing dynamically linked runs.
The failure is actively misleading: the kernel returns ENOENT for a missing interpreter,
so bwrap reports "No such file or directory" against the *executable*, which is present
and readable. `--symlink usr/sbin /sbin` for the same reason.

DNS inside the sandbox requires binding the real path of `/etc/resolv.conf`. Under WSL
that file is a symlink to `/mnt/wsl/resolv.conf`, so binding `/etc` binds a dangling
symlink: connections by address succeed while connections by name fail. The sandbox
builder resolves the link and binds its target at its own path whenever network access is
granted.

The consequence for testing is the part worth keeping: **network denial must be verified
by address, never by hostname.** With resolv.conf unbound, a hostname request fails
whether or not the network namespace is genuinely isolated, so a hostname-only test would
report a tight sandbox that might merely have broken name resolution. Only an
address-based attempt is evidence. This is the same shape as the self-defeating checks
already recorded: a test that cannot fail for the reason it claims to test.

Verified on Ubuntu 24.04 under WSL2, bubblewrap 0.9.0: commands run, python3 resolves,
network denied by default with 000 by address, the real HOME absent rather than
read-only, `~/.ssh` absent, a bound workdir writable, and network reachable when
explicitly re-shared.

## ADR-0020 — 2026-08-25 — Topology A confirmed: server in WSL2, workspace on the Windows drive — Accepted

Spike C measured the 9p bridge instead of speculating about it. Same tree, warm caches:
git status ~100x slower, small writes ~200x, venv creation 27x (69s against 2.5s), and
the number that actually matters, a full pytest run, 12x — 7.11s against 0.59s.

The ratios read as alarming and the absolute numbers mostly do not. Five iterations of
the self-verification loop is about 35 seconds of wall clock and **zero** Claude tokens,
against roughly 10-25k tokens for the same work under the no-shell alternative. Cost was
the stated priority for this project, so the trade is clearly worth 7-second test runs.
Venv creation at 69s is the one genuinely poor number, and it is a one-off.

Two alternatives were measured and rejected rather than assumed:

Moving only the *workspace* onto ext4 while keeping Claude Code on Windows does not work.
Windows `CreateProcess` refuses a `\wsl$\` UNC path as a working directory, so any shell
command Claude runs in the repository fails outright without first mapping a drive letter.

Moving development entirely into WSL removes every penalty at once — native test speed
*and* free self-verification, with no path translation — and the measurements make it
materially stronger than it looked during planning. It remains rejected only because it
is a workflow change the user does not want, not because the engineering disfavours it.
That keeps ADR-0002's trigger live: if development moves onto Linux for independent
reasons, this becomes strictly best, and the server code is identical either way.

## ADR-0019 — 2026-08-25 — Denominate the prefetch budget in tokens, not bytes, with measured per-extension ratios — Accepted

Challenged during implementation on two grounds: that a 30% over-estimate was wasteful,
and that 128 KiB is not much of a file when programming. Measuring instead of arguing
showed the second point was right and the first was wrong in an interesting way.

Bytes per token, measured against the model's own tokenizer:

    JSON, punctuation-heavy   1.78      minified python   3.55
    TOML lockfile             2.08      python source     3.89
    markdown prose            3.42      dense-docstring   4.16

So the previous `bytes / 3` estimator did not over-estimate at all for structured data —
it **under**-estimated JSON by 41% and a lockfile by 31%. The claim that it erred in the
safe direction held only for Python, which is the only thing ADR-0015 measured.

The deeper error was the unit. A cap in bytes buys 33K tokens of Python or 72K tokens of
JSON — the same limit means wildly different context and latency depending on file type,
so bytes were only ever a proxy for the thing being rationed.

Therefore: cap on estimated **tokens** (40K per file, 140K per call), estimate via a
per-extension ratio table rounded down from the measurements, and default an unknown
extension to the worst case observed. A byte ceiling survives only as a pre-read guard
so a huge file is never loaded to discover it is huge.

Net effect for code, which is the common case: the per-file allowance rose from 128 KiB
to about 145 KiB of Python, and a full prefetch holds about 506 KiB of it. JSON
correctly tightens to 66 KiB. The limit now adapts to type instead of penalising source
to stay safe for data.

Prefill latency was measured at the same time: 1900-2600 tok/s, so 33K tokens is about
17s and 136K about 56s. The 140K budget is therefore roughly a minute of prefill, paid
once per distinct prefix — which is why ADR-0011's stable prompt ordering matters.

Partially supersedes ADR-0015: the measurement stands, the conclusion drawn from it did
not.

## ADR-0018 — 2026-08-25 — Emit a progress notification every turn, to defeat the stdio idle timeout — Accepted

Claude Code's MCP wall-clock timeout (`MCP_TOOL_TIMEOUT`) defaults to about 28 hours,
so our 3600s `DISPATCH_TIMEOUT` is never the binding limit. The real hazard is the
**idle** timeout: 30 minutes on stdio (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`), which
aborts a call that has sent nothing at all in that window. A 25-turn loop can be
silent for longer — a single max-effort turn measured 82s, and long cold prefills are
slower still.

Progress notifications do **not** extend the wall clock, but they **do** reset the
idle timer. So progress reporting is a correctness requirement, not the optional UX
nicety it was filed as during planning. One notification per turn, carrying turn
number and turn budget.

Consequences: M4 owns this, not a later polish pass. `docs/TROUBLESHOOTING.md` gets
the per-server `timeout` field (milliseconds, minimum 1000) as the documented knob.
Also noted: calls background automatically after 2 minutes, so nearly every
delegation will background — expected, not a fault.

## ADR-0017 — 2026-08-25 — Do not depend on `thinking_token_budget`; the docs named the wrong gate — Accepted

The serving repo's docs say the field is gated by a `DSPARK_ENABLE_ISSUE31_GPU_HOTFIX`
boot flag. The live server disagrees. Its actual 400 response says the field needs
`VLLM_USE_V2_MODEL_RUNNER=0` — a different switch entirely.

Decision unchanged in substance but now correctly grounded: feature-detect the 400,
log once, degrade, never rely on the field. Reasoning is bounded by `max_tokens` plus
the retry-and-step-down guard instead.

Wider lesson, recorded because it will recur: **this stack's documentation and its
running behaviour disagree in places.** Prefer a probe over a doc. The same repo also
ships a maximum-reasoning default in its example environment file while two of its own
docs still recommend the lower setting.

## ADR-0016 — 2026-08-25 — The agentic loop is viable; build M4 to M6 as planned — Accepted

The single largest unverified assumption in the plan was whether this model does
OpenAI-format tool calling well enough to justify the loop at all. Spikes A and B
answered it directly against the live cluster.

Single-call: `finish_reason: "tool_calls"`, one well-formed call, arguments valid
JSON, schema respected. Multi-turn: four turns, each emitting a valid call, correctly
consuming the `tool_result` fed back to it, ending with an accurate synthesis.
Temperature is honoured — two `temperature=0` calls returned byte-identical output —
so the tool-call temperature setting is a live knob and stays.

One behaviour worth recording as encouraging: asked for a *real* exit code, the model
spontaneously reached for shell constructs that capture and echo the exit status
rather than trusting its own reading of the output. It reaches for ground truth on its
own. The server still captures exit codes itself — trust is not the mechanism — but
the model is not fighting us.

One flaw, also recorded: the first turn was a wasted directory listing. That is the
exact cost the `files[]` prefetch and the scope-hint prompt exist to remove, and it is
now measured rather than assumed.

Consequence: M4's ~450 lines are no longer contingent. The fallback of shipping only
the one-shot path is not needed.

## ADR-0015 — 2026-08-25 — Prefetch budget 512 KiB total, 128 KiB per file, from a measured 3.9 bytes per token — Partially superseded by ADR-0019

Measured on real Python source via the server's own tokenize endpoint: 3.64
bytes/token at 10 KB, 3.74 at 50 KB, 3.89 at 123 KB — converging near 3.9 as sample
size grows.

So 512 KiB is roughly 134K tokens, comfortably inside a 1M window alongside the
system prompt, agent body, tool schemas, history and output budget, and small enough
that one request cannot monopolise the shared KV pool.

The admission estimator keeps `bytes / 3`, which over-estimates by roughly 23%
against the measured 3.9. That is deliberate and the safe direction: over-estimating
costs a little idle capacity, under-estimating costs a queued request that times out.

Supersedes the planning-stage figure of 2 MiB, which was wrong — it was paired with a
claim of 160 to 220K tokens that implied about 10 bytes per token, nonsense for source
code. The real figure for 2 MiB would have been around 540K tokens, over half the
context window.

## ADR-0014 — 2026-08-24 — Reproduce and guard the reasoning-exhaustion failure rather than avoiding it — Accepted

At maximum effort with a 512-token budget, a hard prompt returns `content: null` and
`finish_reason: "length"` — reproduced on the live cluster. At 4096 it answers but
still truncates, having spent about 15,600 characters reasoning. Low effort truncated
at 512 too, so the hazard is not exclusive to the top setting.

Therefore: retry once at the larger of double the budget or the configured floor,
without charging the turn budget, then step effort down one level for the remainder,
then fail with an explicit `reasoning_exhausted_budget` rather than an empty answer.
Admission accounting uses the retry's larger size, not the original request's.

## ADR-0013 — 2026-08-24 — All four reasoning levels are supported; the top level is not remapped — Accepted

Collapsing to three levels and remapping the top one down was considered. Rejected:
it saves one enum value, the retry guard is needed for the second-highest level
anyway so the state machine does not shrink, and remapping makes our API disagree with
the backend's own documented values — a caller asking for maximum effort would
silently get something else.

Instead the default is low, and `docs/AGENTS.md` says plainly that the high settings
are rarely right for the bulk mechanical work this tool exists for.

## ADR-0012 — 2026-08-24 — Admission control by token budget, not a flat concurrency cap — Accepted

The per-request context ceiling and the concurrent-sequence ceiling are ceilings, not
reservations; the real constraint is that summed live tokens stay under the KV pool,
measured at about 2.49M tokens. Six simultaneous full-context requests are impossible;
six fifth-size requests fit comfortably.

Oversubscription queues rather than failing, so this protects latency, not
correctness — and it can be severe: large cold prefills serialise behind a
1024-token threshold, giving roughly an 8 tok/s decode floor.

Three rules: total in-flight sequences, summed token estimate against budget, and a
separate cap on concurrent large prefills. That last one is what actually binds for
big tasks, and it is deliberate — the engine permits one in-flight long prefill, so
sending five makes all five slow.

Undersubscription is invisible where oversubscription announces itself, so the status
tool reports high-water marks and admission-wait totals. The constants are then
tunable from evidence instead of guessed.

## ADR-0011 — 2026-08-24 — Prompt order is fixed and the system prompt is static, to preserve prefix caching — Accepted

The cluster enables prefix caching, so identical leading tokens are served from cache
— saving prefill time and leaving more KV pool free. Order is therefore system
prompt, agent body, files block, task last, with the file list sorted
deterministically.

This only pays if the leading tokens are bit-identical, so the system prompt is
**static by construction**: no timestamp, session id, turn number or counter. A single
dynamic byte disables the cache silently, with no error and no symptom beyond slower
prefill. All dynamic content goes in the tail, inside tool results.

## ADR-0010 — 2026-08-24 — Refuse to run shell commands when the sandbox is unavailable — Accepted

Upstream logs a warning and runs the command unconfined. We refuse instead, naming the
fix. A security control that silently degrades to nothing is worse than one that is
absent, because it is believed.

Corollary made explicit because it will otherwise be filed as a bug: `read_file` and
`write_file` are governed by the path policy and never enter the sandbox. Only
`run_bash` is confined. The policy is a sufficient control for calls the server itself
makes, and insufficient only once an arbitrary shell exists.

## ADR-0009 — 2026-08-24 — An explicit model registry replaces prefix matching — Accepted

The serving stack runs one model per inference instance; a second model means a second
container on a second port, and its own base URL. Prefix-matching a model *name*
cannot express that at all.

Upstream keeps five prefix-keyed tables that must stay mutually consistent by hand,
resolved longest-prefix-wins, and its own comments record the resulting near-misses.
One row per model replaces all five with strictly less state, and makes the API format
a field lookup instead of a string heuristic.

Supersedes upstream's routing approach entirely.

## ADR-0008 — 2026-08-24 — Ship the OpenAI adapter only, but keep the Anthropic-shaped canonical format — Accepted

Upstream's internal representation is already Anthropic-shaped (content blocks,
tool-use and tool-result blocks), with OpenAI converted at the edge, and nothing
outside the backend layer knows which wire format is in play.

So: delete the roughly 154 lines of Anthropic wire code, which is unused and untested
here and would rot, but keep the canonical shape and a `Backend` protocol seam. Adding
an Anthropic adapter later is then about 150 to 220 lines in one new file.

Three conditions keep that cheap, and violating any turns it into a refactor: the
canonical shape stays content-block structured and is never flattened to strings; SSE
accumulation lives per adapter behind one contract; model-to-backend selection is a
registry lookup and never a reintroduced prefix function.

## ADR-0007 — 2026-08-24 — The server captures real exit codes, separately from what the model claims — Accepted

Models misreport command outcomes, and upstream ships a dedicated test because of it.
Bash call counts, failure counts and the last exit code are computed by the server from
actual process exits and reported as distinct result fields, with the tool description
telling the model not to contradict them.

The whole self-verification design rests on this. Without it, "the tests pass" is an
assertion rather than a measurement.

## ADR-0006 — 2026-08-24 — Four-layer path policy, allowlist first — Accepted

Workspace roots, then an extension allowlist, then a secret denylist, then gitignore.
A pure allowlist cannot work for file *contents* — you cannot enumerate every source
file you will ever delegate — so the extension allowlist is the practical allowlist,
and the denylist and gitignore are second and third nets for what passes it: an
extensionless key, a local environment file, a committed config full of tokens.

The reference implementation of this feature has no validation whatsoever and will
read a private SSH key on request. Every refusal here returns an actionable message so
the caller can retry with a valid path.

## ADR-0005 — 2026-08-24 — Task shaping lives in agent definition files, not in more MCP tools — Partially superseded by ADR-0031, ADR-0042 and ADR-0059

Five tools total. A new kind of delegated task is a markdown file, not a code change
and a release, and Claude is not shown a tool list that grows without bound. The files
use the same format Claude Code already uses for its own subagents, so they are
portable.

## ADR-0004 — 2026-08-24 — Two documentation planes, split by location, with generated files where facts live in code — Accepted

Project plane at the repo root (where are we, why did we choose this); product plane
under `docs/` (how does it work). A fact may appear in exactly one plane.

Config reference and tool reference are **generated** from the code that defines them,
so they cannot disagree with it. Status is generated from the plan and git. Decisions,
journal and changelog are append-only, so they cannot rot. That leaves only five
documents both mutable and hand-written.

The motivating evidence is upstream, where one setting is documented as one value in
the README, a different value in the configuration reference, and is a third value in
code — and the serving stack, where two docs recommend a default that was changed a
release ago.

## ADR-0003 — 2026-08-24 — Size budgets block, but never delete — Accepted

A hard cap that forces deletion is worse than no cap; a review prompt with no teeth is
ignored. So exceeding a budget blocks, with exactly three resolutions: trim real
redundancy, split for a valid reason, or raise the budget with a one-line
justification in the same commit.

A split needs a genuine reason — different audience, different owned code, or
reference separated from narrative — or it is a budget dodge that produces sprawl.
That is machine-checkable: a new doc whose audience and owned globs are both subsets
of its parent's is refused.

## ADR-0002 — 2026-08-24 — Server in WSL2, Claude Code stays on Windows; topology reviewed after the filesystem benchmark — Accepted

Bubblewrap is Linux-only and Windows has no cheap equivalent — Windows Sandbox is a
disposable desktop VM, AppContainer has no CLI and would mean hand-written Win32
security code, and Docker Desktop uses the WSL2 backend anyway.

Running Claude Code itself inside WSL2 would delete the path-translation module
entirely and was seriously considered; the user chose to keep Claude Code on Windows
in VS Code. A dedicated Linux box beside the cluster was also considered and rejected:
it solves sandboxing but makes the workspace reachable only over a network share, a
sync tool, or a clone — each worse than the local bridge, and each adding an
availability or divergence failure the bridge does not have.

The condition that would flip this: if development ever moves onto Linux for
independent reasons, the native-Linux topology becomes strictly best.

## ADR-0001 — 2026-08-24 — This is a rewrite, not a port, and that changes obligations not at all — Accepted

Two new subsystems, a registry replacing five prefix tables, and every model-facing
string re-authored in English. Calling it a rewrite is honest about the engineering:
upstream fixes get read and reimplemented, not cherry-picked, and the test suite is
ours to own.

It changes the licence position not at all. MIT's condition triggers on copying
substantial portions, not on what the result is called, and we are plainly a
derivative work — a clean-room rewrite stopped being possible the moment upstream's
source was read. Both ancestors are MIT under the same copyright line, verified.
See NOTICE for what came from where.

Review point: the usefulness of watching upstream has a half-life of roughly six to
twelve months. Revisit rather than watching a remote indefinitely.
