<!-- BUDGET: 320 -->
<!-- Raised from 300 in M2: this document owns wsl.py and paths.py, and both went from
     a table row saying "not built" to real behaviour needing a section. Paid for in the
     same commit by cutting the bytes-per-token measurements, which AGENTS.md already
     carried in full. -->
# Architecture

How the pieces fit, and why they are arranged this way. For someone who has never seen the
repository.

This is not a configuration reference (see [CONFIGURATION.md](CONFIGURATION.md), which is
generated) and not a list of traps (see [../CLAUDE.md](../CLAUDE.md)). Decisions have their
own record in [../DECISIONS.md](../DECISIONS.md); this explains the shape they produced.

Sections marked **not built** describe intended design. The roadmap is
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

One boundary crossing, in one place. `wsl.py` converts paths at the MCP argument edge and
converts them back on the way out; everything inside is POSIX-only and never sees a
Windows path.

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
| `context.py` | Prefetch, token budgeting, prompt ordering, history eviction *(not built)* |
| `backends/base.py` | `Backend` protocol and the canonical message shape |
| `backends/openai_compat.py` | The only adapter shipped |
| `loop.py` | The one-shot path; the turn loop and response state machine *(not built)* |
| `tools.py` | Model-facing tools and their enforcement *(not built)* |
| `sandbox.py` | bubblewrap invocation *(not built)* |
| `server.py` | MCP wiring, the tool declarations, the backend cache |
| `main.py` | The console-script entrypoint: load, build, run one transport |

The ancestor put all of this in one 2588-line file. We add two concerns it never had —
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

### The one-shot path resolves, sends, and interprets nothing

`delegate()` builds one request from one task and returns what came back. Effort resolves
explicit argument → registry row → global default and is always sent, never inherited from
whatever the cluster was booted with (ADR-0013). The reply budget takes the reasoning floor
at high or max effort, then the per-model cap last, because the cap is what the wire will
actually accept. An unlisted effort is refused before dispatch: it has no translation into
the server's vocabulary, and discovering that mid-call wastes the call.

What it does not do is decide anything: `finish_reason` and the token counts come back raw,
because M3's state machine needs the unread values and would be built on sand otherwise.

That leaves one hazard M1 must face without M3's machinery. A reply can be valid, empty and
stopped on length — the budget spent on reasoning, nothing left to answer with (ADR-0014).
Returned bare, `{"answer": ""}` reads as a model with nothing to say, and the caller reports
a false result. So the result carries `empty_response`, a mechanical fact rather than a
diagnosis, and the tool description says what an empty answer at a length stop means. It is
not called `reasoning_exhausted_budget`: that word means every mitigation was tried.

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

### One wire format, behind a seam

Only the OpenAI-compatible adapter ships. But the *canonical* message shape kept
internally is the Anthropic one — content blocks, tool-use and tool-result — because
nothing outside the backend layer then knows which wire protocol is in play. Adding an
Anthropic adapter later is one new file rather than a refactor.

Three conditions keep that true, and breaking any turns it back into a refactor: the
canonical shape stays block-structured and is never flattened to strings; SSE accumulation
lives per adapter behind one contract; model selection is a registry lookup and never a
reintroduced prefix function. (ADR-0008)

Built. The seam holds three things the layer above depends on. Flattening lives in the
adapter and nowhere else, so the canonical side stays block-structured. Failures arrive as
four distinguishable kinds — unreachable, refused with its status *and body* intact so a
specific 400 stays feature-detectable (ADR-0017), a 2xx that is not the promised shape, and
a malformed canonical request, which is our own bug and must never be retried — because
retry is only buildable on that distinction. And `finish_reason` and the token counts come
back **uninterpreted**: null content with `finish_reason: "length"` is a valid response
carrying no text, not an error. Deciding what that means belongs to the response state
machine in M3, which needs the raw value to decide it.

The adapter's single HTTP client bounds **connect and request separately**. One timeout for
both looks harmless, because the failure everyone pictures is a refused connection — and a
refused connection sends RST and fails in milliseconds regardless. A *dropped* route sends
nothing, so a shared bound leaves the connect phase held open for the entire request
timeout. The operating system's own SYN-retransmission limit is the only thing underneath,
and it varies by platform, so the stall is long and its length is not ours to predict.
Connect is therefore bound by its own, much shorter setting.

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

### Reasoning is controlled per request, never inherited

The cluster's reasoning default is set at boot and is not ours to assume — the stack we
run against ships one value in its example environment while two of its own documents
recommend another. So every request states its reasoning level explicitly. Resolution:
agent frontmatter, then the registry row, then the global default.

An unrecognised value is refused rather than passed through — but not because the backend
would ignore it. It does not: it validates `reasoning_effort` and answers a bad one with a
400, measured rather than assumed (JOURNAL 2026-08-26). The refusal here is simply earlier
and cheaper. These are *this project's* levels, not the server's, so the adapter translates
them — `off` is our word for the server's `none` — and a level with no translation would be
refused only after its prefill had been paid for. (ADR-0013)

There is a real failure mode here, reproduced rather than assumed: at high effort with a
small reply budget, reasoning consumes the whole allowance and the response comes back
with null content and a length stop. The server detects exactly that, retries once at a
larger budget without charging the turn, then steps effort down, then fails with a precise
reason rather than an empty answer. (ADR-0014)

### Prompt order is load-bearing

The cluster caches prompt prefixes, so identical leading tokens are served from cache —
saving prefill time *and* leaving more KV pool free. Order is therefore fixed: system
prompt, agent body, files block, task last, with the file list sorted deterministically.

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
