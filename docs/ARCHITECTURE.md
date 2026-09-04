<!-- BUDGET: 669
     Raised from 660 on 2026-09-04: the files block now escapes a body line shaped like its own boundary, and why it matches any path rather than the file's own is the part a reader needs. Two mid-sentence wraps in the paragraph above were reflowed to pay part of it.
     Raised from 655 on 2026-09-03: prefetch now holds one proven descriptor per file from the open to the read, so the size the budgets use and the bytes inlined are the same file (ADR-0049).
     Raised from 646 on 2026-09-03: delegate_readonly stopped being the one-shot path, which is a fact about how a delegation is assembled and this document owns server.py.
     Raised from 638 on 2026-09-03: the per-file prefetch cap stopped being a fairness control, so this document says where fairness actually lives and why a second copy of it here cost an under-used cluster.
     Raised from 633 on 2026-09-03: max_turns became a per-call argument, so this document can state where argument precedence is applied and why there is exactly one such place.
     Raised from 630 on 2026-09-02: list_agents reports what it skipped and what belongs to the other format, which is the discovery case of a rule this document already states.
     Raised from 625 on 2026-09-02: nothing set the transcript's file permissions, so this document could not state them.
     Raised to the size it had already reached on 2026-09-01: the check that should have held this
     line was disabled from 2026-08-28, when reasons moved inside this comment and the pattern
     stopped matching, so the document grew unenforced. This records where it actually is rather than
     endorsing it; the 2026-09-01 audit tracks the trim. Raised from 394 on 2026-08-30: agents.py
     joined the module table and the tool count in it became a number worth stating. Raised from 386
     that day: M6 made what the sandbox binds a property of the delegation rather than of the server,
     and the secret scan widened with it. Raised from 375 on 2026-08-29: the mount-level secret
     denylist is a mechanism this document owns and could not previously describe, because it did not
     exist. What it replaced was one wrong sentence in AGENTS.md. -->
<!-- Raised from 300 across M2. This document owns wsl.py, paths.py and context.py, and
     all three went from a table row reading "not built" to behaviour that has to be
     explained. Partly paid for by cutting the bytes-per-token measurements, which
     AGENTS.md already carried in full.
     Raised again from 340 in M3: loop.py stopped being a straight-through dispatch, and
     retry selectivity is a set of decisions whose reasons do not survive being compressed
     into a sentence. Some of those lines went on two bounds that did not exist yet, an
     unenforced dispatch_timeout being exactly the kind of gap a reader assumes is covered.
     Raised again from 375 to 425 for empty-answer recovery. The two terminal states are
     different diagnoses that send a caller to different fixes, and the reason each
     mitigation is ordered where it is -- prefix cache first, prefill last -- is not
     recoverable from the code by someone deciding whether to reorder them. The last ten
     lines are the measurement that says one of those stages does not fire in production:
     without it the skip reads as tidiness rather than the thing holding the cost down.
     Lowered from 425 to 330 on 2026-08-27, when loop.py and backends/ moved to DISPATCH.md
     with the four sections describing them. A budget left at its old ceiling after a split
     has been deferred rather than paid. ADR-0032.
     Raised from 360 to 375 in M5. sandbox.py's row stopped saying "not built", and the
     two bind-order rules are invisible in the code -- they are an ordering, not a
     statement -- so a reader deciding whether to move a bind cannot recover them by
     reading build_argv. The section saying what is built but still unreached is the other
     half: without it the config reference's newly un-marked sandbox rows read as wired. -->
# Architecture

How the pieces fit, and why they are arranged this way. For someone who has never seen the
repository.

This is not a configuration reference (see [CONFIGURATION.md](CONFIGURATION.md), which is
generated) and not a list of traps (see [../CLAUDE.md](../CLAUDE.md)). Decisions have their
own record in [../DECISIONS.md](../DECISIONS.md); this explains the shape they produced.

Sections marked **not built** describe intended design; in the module table the marker
says whether it scopes to the whole module or only the clause before it. The roadmap is
[../PLAN.md](../PLAN.md).

## The shape of a delegation

```
Windows                              WSL2 Ubuntu                    your hardware
┌────────────────────┐               ┌──────────────────────┐        ┌─────────────┐
│ Claude Code        │   stdio       │ MCP server           │  HTTP  │ vLLM        │
│                    ├──────────────►│                      ├───────►│ /v1/chat/   │
│  delegate_to_agent │               │  wsl.py  translate   │        │ completions │
│   (task, files[])  │               │  paths.py  policy    │        └─────────────┘
│                    │◄──────────────┤  context.py prefetch │
│  reviews the diff  │  result dict  │  loop.py   turns     │
└────────────────────┘               │  sandbox.py bwrap    │
         │                           └──────────┬───────────┘
         │  reads/edits                         │ read, write, run
         ▼                                      ▼
      C:\...\your-repo  ◄────── /mnt/c/... ──────┘
```

One boundary crossing, in one place. `wsl.py` converts paths at the MCP argument edge;
everything inside is POSIX-only and never sees a Windows path. There is no reverse
translator, and no need of one — results carry the caller's own spelling of a path beside
the resolved one, rather than converting back and hoping the two agree.

## Two paradigms, composed

The two projects this descends from took opposite approaches, and both are here.

**One-shot.** The server reads the files named in `files[]`, inlines them into a single
prompt, and returns text. No tools, one request. The point is that the bytes are read
*server-side* — they never enter Claude's context, so a 40K-token file review costs Claude
almost nothing.

**Agentic.** The local model is given the file tools and `run_bash`, and loops:
one model reply plus any tool it ran is a *turn*, up to a budget. It can write code, run
your tests, read the real failure and try again — at no cloud token cost — before handing
anything back.

`files[]` is not an alternative to the loop. It is a **prefetch** that seeds it. Measured
during spikes: given no prefetch, the model's first turn was a wasted directory listing.
Prefetching removes several such turns from the front of every delegation.

A prefetch cap is a **drop** threshold and never a truncation: a file over it is left out
whole, because source cut mid-function is worse than absent. It is also not a fairness
control, and used to be sized as though it were. Fairness between concurrent requests is
admission's, below, which counts it across every server process on the machine rather than
per call — so a second control here only meant one large file being dropped while the
budget it would have fitted in sat unused (ADR-0046).

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Every setting, one frozen dataclass. The only place a default exists |
| `registry.py` | One explicit row per model: URL, format, window, defaults, concurrency |
| `wsl.py` | The single path-translation boundary |
| `paths.py` | The four-layer path policy |
| `context.py` | Prefetch, token budgeting, prompt ordering. History eviction is the loop's, in `loop.py` |
| `backends/base.py` | `Backend` protocol and the canonical message shape — [DISPATCH.md](DISPATCH.md) |
| `backends/openai_compat.py` | The only adapter shipped — [DISPATCH.md](DISPATCH.md) |
| `loop.py` | The one-shot path, the response state machine and the turn loop — [DISPATCH.md](DISPATCH.md) |
| `agents.py` | The three-tier agent lookup and the frontmatter — [AGENTS.md](AGENTS.md) |
| `tools.py` | Model-facing tools, and both `allowed_tools` sites — [TOOLS.md](TOOLS.md) |
| `sandbox.py` | bubblewrap invocation: the argv, the binds, and the refusal |
| `admission.py` | The four-rule gate every delegation passes before it reaches a backend |
| `slots.py` | The counters those rules read, shared by every server process on the machine |
| `transcript.py` | One operator record per dispatch, written outside the response |
| `server.py` | MCP wiring, the six tool declarations, the backend cache |
| `main.py` | The console-script entrypoint: load, build, run over stdio |

The table covers every module; the three marked above live in [DISPATCH.md](DISPATCH.md),
which owns them, and `agents.py` in [AGENTS.md](AGENTS.md). The ancestor put all of this in one large file; we add two concerns it
never had — path translation and sandboxing — so the split follows concerns, not line count.
`server.py` stays thin wiring; the logic lives in `loop.py`, `backends/` and `context.py`.

`delegate_readonly` is no longer the one-shot path. It was given the read-only tools in
2026-09-03, which puts it on the turn loop like every other delegation — the one-shot
remains reachable, by `delegate` with an explicitly empty toolset, and is still the honest
shape when there is genuinely nothing to look up. What made the change safe rather than a
weakening is that ADR-0042's promise was never "this delegation has no tools" but "nothing
it can do will write" (ADR-0048), and the prerequisite was the loop's heartbeat: before
that, moving a delegation onto the loop moved it onto the silent path.

One exception to thin, and it is deliberate: every delegation tool resolves its arguments
through the single `run_delegation`, which applies the call-argument-then-agent-file
precedence [AGENTS.md](AGENTS.md) states. Four tools with four resolution paths is how the
two halves of one precedence rule drift apart, and precedence is exactly what an agent file
is. `max_turns` was the standing proof: it alone read the file without consulting the
argument, inside the function that got every other setting right.

### stdout belongs to the protocol

The server speaks MCP over stdio, so **stdout is the wire**. A `print`, a traceback, or a
logging handler left on its default stream corrupts every message after it, and the only
symptom is the client reporting a server that failed without saying why. Diagnostics go to
stderr; `main.py` writes startup failures there and exits non-zero, because a non-zero exit
is the one thing a launcher can report. Reading that message means running the command by
hand — [TROUBLESHOOTING](TROUBLESHOOTING.md) says so, since there is nowhere else it shows.

The FastMCP banner is suppressed for the same reason it would otherwise be harmless: it is
drawn to stderr, but drawing it first calls PyPI for a version check. An outbound request
on every launch is the wrong default for a tool whose point is that inference stays on
hardware you control.

### One backend per registry entry, for the life of the server

`server.py` caches a backend — and so an `httpx` connection pool — per registry key, and
closes them in the lifespan teardown. Building per call would discard connection warmup on
every delegation, and would give `backend_status()` a second pool alongside `delegate()`'s
for the same endpoint. The cache is injectable, for the reason the adapter takes a
`client`: otherwise a test of the tool surface opens a real socket and waits a real timeout.

### `delegate()` prefetches, resolves, and then runs turns

`delegate()` builds the opening request from one task and the files named with it, hands it
to the turn loop, and returns what the loop finished with. Paths are checked before the
backend is looked up, so a refusal costs nothing and needs no reachable cluster.

Which path runs is decided by the resolved toolset and nothing else. `allowed_tools`
narrows what the model may call, and resolving it to an empty set takes the one-shot path,
whose prompt says plainly that the model cannot open anything and has no second turn --
true there, and a lie in the loop, which is why they are separate prompts rather than one
with an empty tool list. `run_bash` is subtracted from every resolved set while the sandbox
is unbuilt, so it is never declared and cannot be asked back in. That does not replace the
refusal at execution, which stays: a model can call a tool it was never offered.

The per-turn progress notification is wired here, the only layer holding an MCP session:
`loop.py` takes it as an injected callable and stays free of MCP imports (ADR-0018). What the
notification *carries* is separable from the fact that it is sent, and `delegate_batch` needs
both halves separately: interleaved turn counts from items running at once would describe
nothing, but a batch that reports only when an item lands sends nothing across the turns that
take the time. So `run_delegation` takes an `on_turn` hook that displaces the turn numbers
without displacing the notification, and the batch reports its own completed-item count on
every turn of every item. Withholding the notification instead is what let a client abandon a
batch at the idle timeout while the server carried on to `dispatch_timeout`, holding the
machine-wide budget for the remainder. Effort resolves
explicit argument → agent file → registry row → global default and is always sent, never
inherited from whatever the cluster was booted with (ADR-0013). The argument is required,
and `inherit` is how a caller enters that chain rather than skipping it — spent at the
boundary before the agent merge, refused as a registry or configured default (ADR-0045).
The reply budget resolves the same way,
caller first and the configured default last, and is
[described with the rest of resolution](DISPATCH.md#the-reply-budget-is-resolved-once-most-specific-first). An unlisted effort is refused before dispatch: it has no translation into
the server's vocabulary, and discovering that mid-call wastes the call.

A dispatch that fails on the way out is retried here rather than surfaced, and a reply that
arrives empty is recovered from — both below, under
[retry](DISPATCH.md#retry-sits-above-the-adapter-and-honours-what-the-endpoint-asks-for)
and [empty-answer recovery](DISPATCH.md#an-empty-answer-is-recovered-from-before-it-is-reported).
What
stays true is where the reading happens: `finish_reason` and the token counts cross the
adapter raw, and `loop.py` is the only layer that interprets them.

A reply can be valid, empty and stopped on length — the budget spent on reasoning, nothing
left to answer with (ADR-0014). That is now recovered from rather than merely reported;
see [empty-answer recovery](DISPATCH.md#an-empty-answer-is-recovered-from-before-it-is-reported). The result still carries `empty_response` as a mechanical fact, and now
`reasoning_exhausted` beside it as the diagnosis — which is a separate claim, and earned
only once the mitigations have actually been spent.

### `backend_status()` answers a question a stack trace cannot

It probes `GET {base_url}/v1/models` for every registry entry at once, bounded by
[`status_probe_timeout`](CONFIGURATION.md) rather than the generation-sized `turn_timeout`,
so one blackholed endpoint cannot stall the report on the others. Each probe returns its
failure as data rather than raising: a dead model is a finding, and must not take down the
report for every healthy one.

Reachability is the easy half. The half worth having is `id_confirmed`: whether the endpoint
serves the `served_model_id` the registry names, compared exactly as `Registry.resolve()` does.
An endpoint up and serving *something else* is invisible to every other check — the delegation
is refused, or answered by a model nobody chose — so it reports `status: "ok"` with
`id_confirmed: false`: healthy endpoint, wrong configuration. `context_window_defaulted` is
the same class of fact, marking a window this server assumed rather than one an operator set.

Six status words, chosen so that each sends the reader somewhere different:

| | |
|---|---|
| `ok` | Reachable. Read `id_confirmed` as well |
| `backend_unreachable` | Nothing answered: route, DNS, or the endpoint is down |
| `auth_failed` | 401 or 403. The key is wrong, not the cluster |
| `backend_refused` | Answered with some other error. Often the wrong path behind a proxy |
| `backend_protocol_error` | Answered 200 with something that is not a model list — a proxy, or a different API |
| `misconfigured` | Never reached the network. The named API-key variable is unset |

What it never reports is the address. ADR-0029.

## Design decisions worth understanding

### An explicit registry, not name-prefix routing

The ancestor chose a backend by string-prefix-matching the model name across five separate
tables that had to be kept mutually consistent by hand. Its own comments record the
resulting near-misses.

That cannot work here regardless of tidiness: the serving stack runs **one model per
inference instance**, so a second model means a second base URL — a fact no amount of
name-matching can express. One row per model replaces all five tables with strictly less
state. (ADR-0009, [MODELS.md](MODELS.md))

### Dispatch, retry and the response state machine

Moved to [DISPATCH.md](DISPATCH.md), which now owns `loop.py` and `backends/`: the wire
format seam, retry above the adapter, per-request reasoning control, recovery from an
empty answer, the whole-delegation deadline, and the turn loop that M4 added on top of
them. Split out at 423 lines, before that loop -- also `loop.py` -- landed. (ADR-0032)

What stays here is how those failures reach a caller. `server.py` maps each one to a
`ToolError` that names the fix rather than the layer: a path refusal, a backend that is
unreachable or refusing, and a delegation that outlived `dispatch_timeout`. That last one
is routed separately from the backend failures on purpose -- they name an endpoint, and it
names a deadline the operator set, which is a different thing to go and change.

### Four path layers, allowlist first

Workspace roots, then an extension allowlist, then a secret denylist, then gitignore.

A *pure* allowlist cannot work for file contents — you cannot enumerate every source file
you might ever delegate — so extension is the axis that can be allowlisted, and the
denylist and gitignore are second and third nets for what slips through. Every refusal is
actionable and the whole call fails on any one of them; the layers as a user meets them
are in [AGENTS.md](AGENTS.md). The reference implementation had no validation at all and
would read a private SSH key on request. (ADR-0006)

Layers 3 and 4 read something outside the process — a globs file, `git check-ignore` — so
both can be absent, and both raise rather than defaulting to "nothing matched". A layer
that cannot fire is trusted exactly as much as one that works, and a missing denylist and
a clean pass are the same empty result in every log.

### One place crosses the boundary

Claude Code runs on Windows, the server runs in WSL, and `files[]` arrives in a form
nothing downstream understands. `wsl.py` is the only module that knows this; everything
below it is POSIX-only.

Translating at the edge, rather than requiring POSIX paths from the caller, keeps the
translation away from the least reliable participant in it. Both failure modes here are
silent ones: a model's guessed `/c/Users/...` is a path that does not exist rather than an
error saying so, and rewriting separators on a path that was already POSIX corrupts it,
since a backslash is a legal filename character. So a path in no recognised Windows form
is passed through untouched, and an untranslatable one is refused by name.

### Why bubblewrap, and what it does not cover

`run_bash` is the reason a sandbox exists. Bubblewrap starts from an **empty root**:
nothing exists unless explicitly bound, so `~/.ssh` is not merely unwritable, it is
*absent*. Network is denied by default and re-shared only when an agent asks for it.

Chosen over the alternatives on availability rather than preference. Windows Sandbox is a
disposable desktop VM, far too heavy per command. AppContainer is the real Windows
primitive but has no CLI, so using it means hand-written Win32 security code — precisely
where a subtle hole hides. Docker Desktop uses the WSL2 backend anyway. Hence: the server
runs in WSL2 even though Claude Code does not. (ADR-0002)

**If bubblewrap is missing, `run_bash` refuses.** The ancestor logs a warning and runs the
command unconfined. A security control that silently degrades to nothing is worse than an
absent one, because it is believed. (ADR-0010)

There is also no setting that runs a shell unconfined. `DELEGATE_SANDBOX_ENABLED` once
promised exactly that as "an explicit, logged choice", and was deleted rather than
implemented: a control an operator can switch off is still a control that can silently be
off, and nothing downstream — not the caller, not the model — can tell from the outside.
(ADR-0034)

What is bound is now the delegation's to choose — a workdir, a network namespace, extra
toolchain directories — so the secret scan covers all three roots rather than the two it
began with. Skipping `extra_binds` had been justified by the value being an operator's, and
an agent file supplying it ended that. The reasoning is worth keeping even where it stopped
applying: this is the second time here that a comment has outlived the code it justified,
the first being the one explaining `WITHHELD_TOOL_NAMES`. (ADR-0036)

### The route, now open

`run_bash` is declared and it runs commands. It was withheld from M4 until two things
existed: a sandbox that could confine it, and a denylist enforced at the mount level for
secrets inside what that sandbox binds. Both do, so `WITHHELD_TOOL_NAMES` is empty.

The set is kept rather than deleted. Withholding is how this server says "this tool exists
and cannot work today" — a fact about the server, distinct from a caller narrowing one
delegation through `allowed_tools`. It also never was a control on its own: it narrows only
what is *declared*, and `execute_tool` checks its own allowed set without consulting it,
because a model can call a tool it was never offered.

**Bind order is load-bearing.** bubblewrap applies binds in argv order and a later one
shadows an earlier one at or below the same path, so two rules hold: HOME binds before the
workdir, and read-only toolchain binds come before the read-write workdir. Both matter only
when paths overlap, which is exactly why they are asserted rather than remembered. (ADR-0034)

The division of labour is easy to get wrong: the file tools are governed by
the **path policy** and never enter the sandbox — they run in the server process. Only
`run_bash` is confined. That is intended. The policy is sufficient for calls the server
makes itself, and insufficient only once an arbitrary shell exists — which is why the
secret denylist is additionally enforced at the *mount* level for `run_bash`.

**Covering up, not leaving out.** An empty root means the denylist cannot subtract: a secret
is visible only because it sits inside a tree that had to be bound whole. So `run` walks the
bound HOME and workdir and `build_argv` mounts something empty over each match — `--tmpfs` on
a directory, `--ro-bind /dev/null` on a file — after every bind, since a shadow needs its tree
to exist first. The walk is bounded, and exhausting the budget refuses the command rather than
covering part of a tree. `secret_match` is shared with the path policy, so one list cannot be
read two ways — which means the layers share limits too: both match resolved paths, so neither
covers a link whose *name* matches while its target does not. The scan is point-in-time, and
`run_bash` holds a read-write bind for the whole call, so a file the command writes afterwards
is not covered and cannot be. Defence in depth for one tool, not a replacement. (ADR-0035)

**Bulk directories are covered and not walked.** The walk is per `run_bash` call, on a
workspace that lives on `/mnt/c`, and the budget above is what a project's own installed
dependencies exhaust: this repository walked 10,586 entries in 66 seconds once it carried a
virtualenv, against 248 in 0.7 with one covered. So a second list, `opaque_globs_file`,
names machine-generated directories, and a match is covered with the same tmpfs a matched
secret directory gets and pruned from the walk for the same reason.

Covering is what makes skipping safe: a secret inside such a directory is hidden by the
mount over its parent whether or not the walk ever looked inside it. Pruning *without*
covering would be a hole, and the direction is easy to get backwards — the obvious rule,
"skip whatever gitignore ignores", would drop `.env` from a scan that exists to find it.

Kept as a separate file from the denylist deliberately. That one is a security control
whose absence is fatal; this one only decides what the walk skips, so a missing file is a
warning. Sharing them would make a slow scan fixable by editing the security list. Raising
the entry budget is the other tempting fix and is worse than it looks: it makes every call
walk the tree, and once inside a virtualenv `*secret*` and `*credential*` match ordinary
library filenames, so the scan mounts `/dev/null` over the imports of the environment it
just read. (ADR-0041)

### Ground truth over self-report

Models misreport command outcomes, and also which tools they used. The server therefore
computes `bash_calls`, `bash_failures`, `last_bash_exit` and a count of every tool call by
name from what it watched, and reports them as fields distinct from the model's prose. What
each counts, and why `last_bash_exit` can be `None` when `0` would be a lie, is in
[DISPATCH.md](DISPATCH.md), which owns the loop that counts them.

Without this, "the tests pass" is an assertion rather than a measurement, and the entire
self-verification design rests on it. (ADR-0007)

### Prompt order is load-bearing

The cluster caches prompt prefixes, so identical leading tokens are served from cache —
saving prefill time *and* leaving more KV pool free. Order is therefore fixed: system
prompt, agent body, files block, task last, with the file list sorted deterministically by
resolved path. That order lives on `Delegation.render`, in one place, because the one-shot
builder and the turn loop both need it and two copies of an ordering rule is one too many.

Each file is opened once and held from the open to the read, so the size the budgets are
computed from and the bytes that get inlined come from one descriptor, proven to be the
path the policy approved (ADR-0049). Sorted *before* the total budget is accumulated, not
after, or the same six files listed in a different order would return a different five.
The budget also stops at the first file that does not fit rather than continuing to pack
in whatever is small enough: a coherent prefix of what was asked for beats an arbitrary
subset of it, and it is the only version a caller can predict. A file over a cap is left
out **whole** — source cut mid-function is worse than absent, because the model will
repair code it never saw — and what was left out is named in the prompt as well as the
result, since the model cannot otherwise tell an omitted file from one that does not
exist.

Each file is wrapped in `--- BEGIN FILE <path> ---` / `--- END FILE <path> ---` rather
than a markdown fence, because an inlined `.md` file's own fences would close it early.
The body is then escaped against those markers, which the fence choice alone did not
cover: a line in the file matching the marker's *shape* — for any path, not just its own —
is prefixed so it cannot read as a boundary. Any path, because a forged marker naming a
file the server never read attributes what follows to something nobody vouched for, which
is worse than a file merely ending itself early. Neutralised rather than dropped: a review
delegation exists to read source, and source legitimately quotes things.

One system prompt covers the files and no-files shapes rather than one for each. Two would
be two prefixes, so a caller alternating between the shapes would miss the cache on every
other call, for wording unrelated to the difference.

This only pays if the leading tokens are **byte-identical**, which makes the system prompt
static by construction: no timestamp, no session id, no turn counter. One dynamic byte
disables the cache with no error and no symptom beyond slower prefill. Dynamic content
goes in the tail, inside tool results. (ADR-0011)

The reasoning level is part of that prefix, which is not obvious: setting it rewrites the
rendered prompt, so `prompt_tokens` moves with the level. Byte-identity therefore holds
*within* one effort level and not across them, and varying effort per turn discards the
cache. (JOURNAL 2026-08-26)

### Admission by token budget, not a request count

A flat concurrency cap wastes capacity on small tasks and still overcommits on large ones.
The real constraint is that summed live tokens stay under the KV pool; per-request and
per-sequence limits are ceilings, not reservations.

Oversubscription **queues** rather than failing, so this protects latency, not
correctness — and it can degrade badly, because large cold prefills serialise. Four rules
apply: total in-flight requests, summed token estimate against budget, a separate cap on
concurrent large prefills, and the endpoint's own declared `concurrency`. The third is
what actually binds for big tasks, and it is deliberate: the engine admits one long
prefill at a time, so sending five makes all five slow rather than any of them fast. The
fourth is per endpoint rather than global, and is checked on every path — a limit enforced
only where requests happen to run in parallel bounds a caller against itself and nothing
else.

The four are **one predicate, not four gates in series**. A request that took a sequence
slot and then blocked on the large-prefill cap would hold capacity it is not using for the
whole wait, starving smaller requests that fit every rule. Nothing is ever partially
acquired: a waiter that does not fit holds nothing.

Two numbers size a request, and conflating them is a trap. Its KV footprint is the prompt
plus the reply it is permitted to generate, and that is what the token budget counts. Its
prefill is the prompt alone, and that is what decides whether it is a large cold prefill —
decode is not prefill, and a reply allowance above the threshold would otherwise make
every request "large" and quietly bound the whole server at one setting while every other
rule reads as though it were the one binding.

The estimate is fixed when a slot is granted and never grows, so for a long agentic
delegation the token rule is a floor-time approximation rather than a running total.
Growing it per turn would couple the gate to the turn loop's internals and add a
reconciliation path on every abort; the high-water marks are the cheaper way to find out
whether that trade was wrong.

Oversubscription announces itself as latency; idle capacity is silent. So the status tool
reports high-water marks, admission-wait totals and the count of waits that hit their
limit, and the constants are tunable from evidence instead of guesswork. (ADR-0012)

### The budget belongs to the machine, not to the process

Every rule above is only as global as the thing counting it, and for a while that was one
process. The transport is stdio, so the MCP client starts a server per registration: two
editor windows open on two projects are two servers, each with counters starting at zero,
against one KV pool. Each rule bounded a session, and the cluster saw the configured
ceiling multiplied by the number of windows open.

So the counters live in a file under `flock` that every server on the machine shares, and
`admission.py` tests the four rules against the sum. `slots.py` owns that file; the policy
did not change, only the scope it counts over. The test and the write are one critical
section — reading totals, deciding, then writing would let two processes see the same room
and both take it, precisely when the cluster is busy.

A record is keyed by PID and process start time and is dropped as soon as that pair stops
matching a live process, so an editor window that is killed outright leaks nothing and no
recycled PID can inherit its slots. The lock is non-blocking and retried between `await`s,
because holding the event loop inside it would stall delegations that are already running.

`backend_status` reports whether the shared budget is actually active, the machine-wide
totals, and how many processes hold slots — observed rather than assumed, since a gate that
has quietly narrowed to one process looks exactly like a working one until the cluster is
oversubscribed. Where no POSIX lock exists it degrades to per-process counting and says so.
Defaults and the two settings are in [CONFIGURATION.md](CONFIGURATION.md). (ADR-0040)

### The operator transcript is not the caller's diagnostics

Two different audiences, and conflating them is what broke this upstream twice. The
caller's `diagnostics` argument shapes the caller's reply. The transcript is written for
whoever runs the server, and is **independent of any caller-facing flag** — what an
operator can audit should not depend on what the calling session thought to ask for. A
delegation worth investigating is rarely one anybody suspected in advance, so the record
has to exist already.

That independence has a cost the design has to pay rather than assume: the turn loop only
*keeps* per-turn records when it is told to, so a configured transcript asks for them
itself and the caller's own flag still decides, separately, what the reply contains.

**Setting the directory must not change a single byte of any response.** The writer is
called for its effect from a `finally` and returns nothing, so there is no value for the
response dict — assembled from conditional spreads — to pick up. The record is built from
identity captured *before* the attempt, because the agent's name is in scope only at the
top of a delegation; assembling it any deeper leaves a failure with nothing to name, which
is how upstream came to log exactly the dispatches it existed to explain as unknown.

One file per dispatch rather than an appended log, because a batch has as many concurrent
writers as items. Records carry the task, the files with their accounting, real token usage
as the backend reported it, and the server-captured ledger — but **not file contents**,
which are recoverable from the repository by path and are the only bulky part. The task is
written verbatim: it exists nowhere else, and whoever configures the directory owns what
lands in it. A write that fails is swallowed to stderr, never to stdout and never into the
dispatch: a full disk must not fail work that already succeeded. (ADR-0024, ADR-0039)

### Token estimates are per file type

Bytes are the wrong unit for rationing context: a byte cap allows twice the *context* for
a data file as for a source file, which is backwards. Budgets are denominated in estimated
tokens instead, from ratios measured per extension. The measurements and what they buy are
in [AGENTS.md](AGENTS.md). (ADR-0019)

## Read-only tools say so, and the ones that can write must not

An MCP tool annotation is a claim made to the client before the call runs, so a client can
act on it without asking. `backend_status` and `list_agents` carry `readOnlyHint` because
neither can change anything; a caller gating writes on that declaration -- plan mode in
Claude Code does -- runs them without stopping. Measured rather than assumed: the same call
prompts for approval without the annotation and does not with it.

`delegate_readonly` and `delegate_batch_readonly` are what that asymmetry leaves room for:
their writing twins with the tool set fixed to whatever declares no write, rather than
accepted as an argument. Each does what its twin does when called the same way; what differs
is that a caller can promise it in advance. The fixed set intersects where `None` resolves to
everything available, so no parameter widens it back -- an annotation a caller could falsify
by passing an argument would be the check that cannot fail. Nor an agent file, which the
batch form accepts: `run_delegation` reads `agent.allowed_tools` only when nobody passed any.

The three that can write carry no such hint and must not. With `allowed_tools` unset a delegation
hands the local model the writing tools and `run_bash`, so a read-only claim would be false
in the way that is hardest to notice -- the client stops asking, the write still happens, and
nothing anywhere reports a contradiction. The permission layer matches on tool name and never
inspects arguments, so the claim is a property of the tool or it is worth nothing. Holding
that asymmetry is what the guard in `tests/test_server.py` is for, and it is worth more than
the annotations themselves.

## A dispatch is written twice, because "what happened" and "what is happening" are different questions

`transcript.write` produces the record an operator reads afterwards: paths, accounting, the
task, and the per-turn ledger, written once the dispatch is over. `transcript.Stream`
produces the file a person watches during it — `start`, one `turn` per completed turn, then
`end` — appended and flushed a line at a time. Neither is derived from the other. A record
that exists only once the work is finished cannot say whether the work is stuck, and a
stream has to survive a dispatch that never reaches an end. (ADR-0043)

The stream carries the model's reply text, which the record does not. That is an extension
of ADR-0039 rather than a reversal of it: that decision excluded file *bodies* as bulky and
recoverable from the repository by path, and a reply is neither — it is small and exists
nowhere else, which is the same argument ADR-0039 used to write the task verbatim.

Both files are created at `0o600` and the directory at `0o700`, by `os.open` with an
explicit mode rather than a `chmod` afterwards — that would leave a window in which the
file existed at whatever the umask allowed. umask only clears bits, so this is a ceiling.

Both halves say **which call they came from and what it was handed**: `tool` is the tool the
caller invoked, `tools` is the set that call resolved to, and the files are the prefetch
accounting — paths and cost, never text. Two fields rather than one, because neither answers
the other's question. A read-only tool is its writing twin with the tool set fixed, so the
pair runs one path and `tools` alone cannot say which was called; and `delegate` alone
does not say whether a loop ran, because a caller may pass `allowed_tools=[]` and get a
one-shot. Until both were recorded, a read-only call, a `delegate_batch` item and a plain
`delegate` wrote byte-identical transcripts — so a directory of them could not be counted by
kind, which is what the records exist for. **Absent and empty are not interchangeable
anywhere downstream:** a missing `tools` is a transcript written before the field existed and
is reported as unknown, while an empty one is a one-shot. The files are in the stream as well
as the record because the record is written when the work is over, and a reader asking what a
delegation is chewing on is asking while it runs.

Two intervals are recorded per turn and the difference between them is the point. `ms` is
the turn's wall clock, including tool execution and any wait for a slot; `backend_ms` is the
backend call alone. Tokens per second is taken from the second, because a rate divided by
the first would blame the cluster for time it did not spend generating.

`scripts/watch_delegations.py` reads that stream: it lists what is in the transcript
directory, follows the one you pick, and renders turns as a conversation rather than as
JSON. It is owned by this document rather than its own, because a renderer and the format
it renders are one decision — split across two documents, a renderer ends up describing a
shape the writer no longer produces.

The list and the follow view are two states of one process, not two runs of it. `q` leaves
a transcript and comes back to the list; only the list exits. Reaching the `end` event
deliberately does not return on its own — the last thing a dispatch writes is usually the
thing that was being waited for, and taking the screen away at that moment is the one
behaviour a watcher must not have. The list redraws itself every couple of seconds so a
dispatch started elsewhere appears without a keypress, and `r` forces it.

Ordering comes from the stream format rather than the filesystem: from **when the dispatch
started** — the `start` event's `at`, falling back to the timestamp `transcript.py` puts in the
filename — never from mtime, which moves every turn and would reshuffle the list under a reader
watching a long dispatch. **The list is then the newest twenty**, a count rather than the
seven-day window it replaces: an age left a busy day unreadable and a quiet week nearly empty.
Nothing is deleted, and an older stream still opens by path. Unchanged files come from a cache
keyed on `(mtime, size)`: an unattended redraw over `/mnt/c` that re-read every one would make
the viewer a load generator (ADR-0020). Start and last-write clocks sit side by side in local
time, because `at` is UTC and an mtime is not.

The listing also names the **kind** of each call in one word — `delegate`, `readonly`,
`agent`, `batch`, or `one-shot` for a `delegate` that was handed no tools — because that is
the difference between two rows that otherwise look alike, and it decides which one is worth
opening. Two facts share the column, since only one of them is ever a surprise: a read-only
call is a one-shot by construction, so the shape is worth naming only where a `delegate`
quietly ran as one. A transcript from before the field existed reads `?`. That is the point
rather than a gap — every call once wrote `delegate` whether or not it was one, so defaulting
an old row to `delegate` would reproduce exactly the confusion the column ends. Opening a
transcript then shows the resolved tools and every file it was given, skipped ones named with
their reason: a file the caller believes it passed and the model never saw is the one thing
in that block worth interrupting a reader for.

Both paths write a fourth kind of event, `alive`, and it is the only one that reports no
work done. The other three mark something that happened; this one exists because either
shape can be silent for a long time — a one-shot has no turns at all, and one turn can
outlast the client's idle timer unaided. A synthetic `turn` is written when a one-shot's
answer arrives, so the record is never the empty shape a failed delegation has.
[DISPATCH.md](DISPATCH.md) owns what the heartbeat carries and why (ADR-0018).

A stream ends by writing an `end` event, so the listing has three states rather than two:
`ok`/`fail` for one that ended, `live` for one written to recently, and `quiet <age>` for
one that has neither ended nor been written to for `STALL_SECONDS`. The third exists
because a dispatch whose server was killed — closing the editor takes the whole process
tree with it — leaves a file that is byte-for-byte indistinguishable from one still being
written, and calling that `live` is a claim the file cannot support. It cannot be resolved
by looking harder: a writer pid in the stream would mean nothing on another machine, and
this directory is routinely synchronised. So the state says only what is known, and names
the age — a one-shot delegation is legitimately silent between its start and its end, and
must not be called dead for it.

## Non-goals

- **No standalone CLI.** The server speaks MCP only. Recorded here so nobody adds an
  undocumented one later.
- **No streaming in v1.** MCP tool calls are request/response, so Claude sees nothing
  incrementally either way. Progress notifications — which are *required*, to avoid the
  stdio idle timeout — cover the part that matters. (ADR-0018)
- **No cloud providers.** Everything cloud-specific in the ancestor was deleted.

## The server decides whether overflow handling may act, and the loop only reads a switch

`WindowCheck` lives beside `BackendCache` and for the same reason: its verdict is per model
and per process, and re-probing on every delegation would spend a round trip to re-learn
something that changes only when the operator edits a file.

The verdict is applied by handing `run_agentic_loop` a config whose switch already reflects
it, rather than by passing the loop a switch and a verdict to combine. `arm_overflow`
returns a *new* config; the server's own is never mutated, so one model whose endpoint was
briefly unreachable cannot disarm the feature for every other model in the registry. When
the server declines to use a feature the operator armed, the reply says so — silence there
is indistinguishable from the feature working. `list_agents` applies the same rule to
discovery: a file it could not read is named in `skipped` rather than omitted, and one in
Claude Code's format in `other_format` rather than called faulty.

`dispatch_delegation` holds the two dispatch paths and the translation of every failure they
can raise into a `ToolError`. It is out of `build()` because it is the only part of that
function that is not wiring: which path ran decides which failures are possible.
