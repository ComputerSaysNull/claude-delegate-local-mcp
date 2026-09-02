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

## ADR-0041 — 2026-08-30 — Bulk directories are covered and skipped, not the denylist — Accepted

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

## ADR-0035 — 2026-08-29 — The secret denylist is enforced by covering paths up, not by leaving them out — Accepted

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

## ADR-0005 — 2026-08-24 — Task shaping lives in agent definition files, not in more MCP tools — Partially superseded by ADR-0031 and ADR-0042

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
