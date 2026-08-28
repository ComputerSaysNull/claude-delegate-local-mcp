<!-- BUDGET: 330 -->
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
     has been deferred rather than paid. ADR-0032. -->
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

**Agentic.** The local model is given `read_file`, `write_file` and `run_bash` and loops:
one model reply plus any tool it ran is a *turn*, up to a budget. It can write code, run
your tests, read the real failure and try again — at no cloud token cost — before handing
anything back.

`files[]` is not an alternative to the loop. It is a **prefetch** that seeds it. Measured
during spikes: given no prefetch, the model's first turn was a wasted directory listing.
Prefetching removes several such turns from the front of every delegation.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Every setting, one frozen dataclass. The only place a default exists |
| `registry.py` | One explicit row per model: URL, format, window, defaults, concurrency |
| `wsl.py` | The single path-translation boundary |
| `paths.py` | The four-layer path policy |
| `context.py` | Prefetch, token budgeting, prompt ordering; history eviction *(that clause not built)* |
| `backends/base.py` | `Backend` protocol and the canonical message shape — [DISPATCH.md](DISPATCH.md) |
| `backends/openai_compat.py` | The only adapter shipped — [DISPATCH.md](DISPATCH.md) |
| `loop.py` | The one-shot path and the response state machine — [DISPATCH.md](DISPATCH.md); the turn loop *(that clause not built)* |
| `tools.py` | Model-facing tools and their enforcement *(whole module not built)* |
| `sandbox.py` | bubblewrap invocation *(whole module not built)* |
| `server.py` | MCP wiring, the tool declarations, the backend cache |
| `main.py` | The console-script entrypoint: load, build, run one transport |

The table covers every module; the three marked above are documented in
[DISPATCH.md](DISPATCH.md), which owns them. The ancestor put all of this in one
2588-line file. We add two concerns it never had —
path translation and sandboxing — so the split follows concerns, not line count.
`server.py` stays thin wiring; the logic lives in `loop.py`, `backends/` and `context.py`.

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

### The one-shot path prefetches, resolves, sends, and retries

`delegate()` builds one request from one task and the files named with it, and returns
what came back. Paths are checked before the backend is even looked up, so a refusal costs
nothing and does not depend on the cluster being reachable that day. Effort resolves
explicit argument → registry row → global default and is always sent, never inherited from
whatever the cluster was booted with (ADR-0013). The reply budget takes the reasoning floor
at high or max effort, then the per-model cap last, because the cap is what the wire will
actually accept. An unlisted effort is refused before dispatch: it has no translation into
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

Reachability is the easy half. The half worth having is `id_confirmed`: whether the
endpoint actually serves the `served_model_id` the registry names, compared exactly, the
way `Registry.resolve()` compares it. An endpoint that is up and serving *something else*
is invisible to every other check — the delegation either gets refused, or gets answered by
a model nobody chose. That case reports `status: "ok"` with `id_confirmed: false`, because
the endpoint really is healthy; it is the configuration that is wrong.

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
empty answer, and the whole-delegation deadline. Split out at 423 lines, before M4's turn
loop -- also `loop.py` -- landed on top of it. (ADR-0032)

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

The division of labour is easy to get wrong: `read_file` and `write_file` are governed by
the **path policy** and never enter the sandbox — they run in the server process. Only
`run_bash` is confined. That is intended. The policy is sufficient for calls the server
makes itself, and insufficient only once an arbitrary shell exists — which is why the
secret denylist is additionally enforced at the *mount* level for `run_bash`.

### Ground truth over self-report

Models misreport command outcomes. The server therefore computes `bash_calls`,
`bash_failures` and `last_bash_exit` from real process exits and reports them as fields
distinct from the model's prose, and the `run_bash` description tells the model not to
contradict them.

Without this, "the tests pass" is an assertion rather than a measurement, and the entire
self-verification design rests on it. (ADR-0007)

### Prompt order is load-bearing

The cluster caches prompt prefixes, so identical leading tokens are served from cache —
saving prefill time *and* leaving more KV pool free. Order is therefore fixed: system
prompt, agent body, files block, task last, with the file list sorted deterministically by
resolved path.

Sorted *before* the total budget is accumulated, not after, or the same six files listed
in a different order would return a different five. The budget also stops at the first
file that does not fit rather than continuing to pack in whatever is small enough: a
coherent prefix of what was asked for beats an arbitrary subset of it, and it is the only
version a caller can predict. A file over a cap is left out **whole** — source cut
mid-function is worse than absent, because the model will repair code it never saw — and
what was left out is named in the prompt as well as the result, since the model cannot
otherwise tell an omitted file from one that does not exist.

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
correctness — and it can degrade badly, because large cold prefills serialise. Three rules
apply: total in-flight requests, summed token estimate against budget, and a separate cap
on concurrent large prefills. That last is what actually binds for big tasks, and it is
deliberate: the engine admits one long prefill at a time, so sending five makes all five
slow rather than any of them fast.

Oversubscription announces itself as latency; idle capacity is silent. So the status tool
reports high-water marks and admission-wait totals, and the constants are tunable from
evidence instead of guesswork. (ADR-0012)

### Token estimates are per file type

Bytes are the wrong unit for rationing context: a byte cap allows twice the *context* for
a data file as for a source file, which is backwards. Budgets are denominated in estimated
tokens instead, from ratios measured per extension. The measurements and what they buy are
in [AGENTS.md](AGENTS.md). (ADR-0019)

## Non-goals

- **No standalone CLI.** The server speaks MCP only. Recorded here so nobody adds an
  undocumented one later.
- **No streaming in v1.** MCP tool calls are request/response, so Claude sees nothing
  incrementally either way. Progress notifications — which are *required*, to avoid the
  stdio idle timeout — cover the part that matters. (ADR-0018)
- **No cloud providers.** Everything cloud-specific in the ancestor was deleted.
