<!-- BUDGET: 327
     Raised from 310 on 2026-09-02: list_agents now separates a broken agent file from one in Claude Code's format, and what a caller is told about each is a fact this document owns.
     Raised to the size it had already reached on 2026-09-01: the check that should have held this
     line was disabled from 2026-08-28, when reasons moved inside this comment and the pattern
     stopped matching, so the document grew unenforced. This records where it actually is rather than
     endorsing it; the 2026-09-01 audit tracks the trim. Raised from 272 on 2026-08-30: the three
     tools landed, so the worked examples became real calls and delegate_batch needed its shape
     stated where someone deciding whether to use it looks. Raised from 256 earlier that day: the
     workdir surface and the extra_binds constraint both became real and both needed stating where
     someone configuring them looks. Raised from 240 earlier that day: agents.py landed, so this
     document stopped describing a design and started describing behaviour. The added length is one
     new section -- why an over-cap max_turns is refused in a file and clamped for a caller. A
     deviation from a rule documented elsewhere has to be written down where someone hits it, or the
     two documents simply disagree and the reader picks whichever they found. -->
# Agents and the path policy

Two things a user actually touches: the agent files that shape a delegation, and the rules
governing which files a delegated model may see.

Tool internals are in [TOOLS.md](TOOLS.md), generated from `tools.py`; sandbox
mechanics are in [ARCHITECTURE.md](ARCHITECTURE.md). Settings are in
[CONFIGURATION.md](CONFIGURATION.md).

**Status: built and enforced.** `paths.py` landed in M2, `agents.py` and the three tools
that reach it in M6. Everything below describes behaviour. The roadmap is
[../PLAN.md](../PLAN.md).

## Why agents are files

There are six MCP tools: `delegate`, `delegate_readonly`, `delegate_to_agent`,
`delegate_batch`, `list_agents` and `backend_status`. A new *kind* of delegated task —
review, test-writing, refactoring, migration — is a markdown file, not a new tool. A test
asserts the exact set, so another cannot arrive without someone arguing for it.

It was five until `delegate_readonly`, which is the argument ADR-0005 asked for and not an
exception to it. That rule is about task *kinds*, and this is not one: it is `delegate`
with `allowed_tools` fixed at `[]`, existing because a client decides whether to prompt
before a call runs and can only read the tool's annotation at that point. A read-only call
cannot be expressed where permission rules never inspect arguments; only a read-only tool
can. No agent file can carry that constraint, because the tool an agent is reached through
can write. (ADR-0042)

That keeps the tool list Claude sees from growing without bound, and makes adding a task
type a file rather than a code change and a release. The format is the one Claude Code
already uses for its own subagents. (ADR-0005)

ADR-0005 went further and called the files *portable*. They are not, and this repository's
own `.claude/agents/*.md` are the counter-example: Claude Code spells the tool list `tools`
where this format spells it `allowed_tools`, and accepts an `effort: medium` that
[config.py](../src/claude_delegate_local/config.py) deliberately refuses. The shape is
borrowed; a file is not moved between the two unedited. (ADR-0031)

## Where they are found

Three locations, first match wins:

1. `<workdir>/.claude/agents/<name>.md`
2. `<workdir>/.claude/skills/<name>/SKILL.md`
3. `~/.claude/agents/<name>.md`

Project-local agents therefore override personal ones, which is usually what you want. The
name is validated against `^[A-Za-z0-9_-]+$` — an agent name is not a path, and allowing it
to look like one would make it a traversal.

`list_agents` gives **three** answers, because "not there", "there and broken" and "there
but not mine" need different ones and until 2026-09-02 gave the same one — a file that did
not parse was simply left out. The standing advice for a missing agent, ask by name and read
the error, needs the very name an omission hides.

- `skipped` is what needs fixing, with the name each file claimed and why it failed. A
  broken definition is still skipped rather than fatal.
- `other_format` is Claude Code's format sharing the directory (ADR-0031) — `tools` where
  this format says `allowed_tools`. Not broken, not for this server, and named rather than
  hidden. Four of this repository's own five agent files are in it, deliberately, which is
  why the split exists: folding them into `skipped` would leave that list permanently
  non-empty here, and a list that is never empty is one nobody reads.
- Absent from all three means it does not exist.

A name shadowed by a nearer tier is in none of them: the lookup really does offer only one.

## Frontmatter

```markdown
---
name: test-writer
description: Writes and extends pytest tests for a changed module.
model: deepseek-v4-flash
effort: low
max_turns: 20
max_tokens: 32768
keep_tool_results: 6
allowed_tools: [read_file, write_file, run_bash]
network: false
extra_binds: []
---

You write tests for existing code. Read the module, then write tests that would have
caught the bug described in the task. Run them. Iterate until they pass.

Report the real exit code of the final test run.
```

Everything below the frontmatter is the system prompt.

| Field | Effect |
|---|---|
| `model` | Registry key. **Actually binds the dispatch** — see below |
| `effort` | `off`, `low`, `high`, `max`. Refused loudly if misspelt, and `inherit` is not one of them — a file is a tier that word defers *to* (ADR-0045). Reached only when the caller passes `inherit` |
| `max_turns` | Round trips. Above the server's hard ceiling the **file is refused**, not clamped — see below |
| `max_tokens` | Per-reply budget, clamped by the model's cap |
| `keep_tool_results` | How many recent tool results survive history eviction — [DISPATCH.md](DISPATCH.md) |
| `allowed_tools` | Restricts what this agent may call. Enforced twice |
| `network` | `true` re-shares the network namespace for `run_bash`. Default off |
| `extra_binds` | Extra directories visible inside the sandbox |

### `model` genuinely binds

In the ancestor, frontmatter was loaded and then largely ignored — `model:` did nothing.
That is a real bug the fork fixed, and it is worth naming because it is easy to reintroduce:
resolution must be consistent between the code that picks a concurrency bucket and the code
that makes the call. If those disagree, the request is counted against one endpoint's limit
and sent to another.

Precedence: explicit call argument, then frontmatter, then the registry row, then the global
default.

### An over-cap `max_turns` is refused, where a caller's is clamped

A caller passing `max_turns` above [`max_turns_hard_cap`](CONFIGURATION.md) has it clamped
in silence, because the work is legitimate and only the number is not
([DISPATCH.md](DISPATCH.md)). An agent *file* asking for the same thing is refused when it
loads.

The difference is what happens next. A call argument is gone the moment the call returns; a
file is committed, read again, and believed. Clamping there would leave the wrong number in
the file forever, working correctly and reading as though it were in effect — and the person
who eventually needs those turns would have no way to tell it never had them.

### `allowed_tools` is enforced twice

Once when declaring tools to the model, and again at execution. Filtering only the declared
list is **advisory** — a model can call a tool it was never offered, and some do. If you
touch one enforcement site, check the other. Both live side by side in `tools.py`, and a
call to a tool outside the set comes back as an error result rather than ending the
delegation: mid-loop, throwing away every turn already paid for because the model made one
bad call costs more than telling it no.

### `network: false` is the default, and means absent

Not firewalled: the sandbox does not have a network namespace at all. Set `network: true`
only for an agent that genuinely needs to fetch something, and prefer prefetching instead.

Verifying this correctly matters more than it sounds. A hostname request fails whether or
not the namespace is isolated, so **test denial by address**, never by hostname — otherwise
a broken resolver reads as a tight sandbox. (ADR-0021)

### `extra_binds` for toolchains

The sandbox starts from an empty root, so only what is bound exists. `python3` comes from
the system bind and a project virtualenv works because it sits inside the bound workdir —
but a tool installed under your home directory, `uv` being the common case, is **absent**,
and `uv run pytest` fails with "not found". That is the most likely first-run surprise. The
server binds the resolved `uv` by default; anything else goes here.

A bind here is **read-only**, and the secret denylist still reaches inside it. The path
policy's layer 1 does not: requiring an `extra_binds` entry to sit inside a workspace root
would defeat the field, whose whole purpose is reaching a toolchain that lives outside one.
So the constraint that remains is the one that matters — no agent file can bind a directory
in order to read credentials out of it. (ADR-0036)

## The path policy

Four layers, checked in order, cheapest first. They apply to `files[]` *and* to the model's
own `read_file` and `write_file`. None of them involves the sandbox: only `run_bash` is ever
confined, so these four are the whole control for a read or a write (ADR-0010).

`write_file` creates, so for it — and only for it — layer 1's existence test is relaxed: a
missing file is allowed, while the directory to write into must still exist and an existing
*directory* is still refused. Every other layer runs unchanged, because writing to a secret
path is worse than reading one rather than better.

| | Layer | Refuses |
|---|---|---|
| 1 | Workspace roots | Anything whose **real** path falls outside a configured root. Resolution happens after symlinks, which is what closes escape via a link |
| 2 | Extension allowlist | Anything whose extension is not listed |
| 3 | Secret denylist | `.env*`, `*.pem`, `*.key`, `id_*`, `*credential*`, `*secret*`, `.git/**`, and more |
| 4 | Gitignore | Anything git ignores |

Allowlist first, deliberately. A *pure* allowlist cannot work for file contents — you
cannot enumerate every source file you might ever delegate — so extension is the axis that
*can* be allowlisted, and layers 3 and 4 are second and third nets for what passes it: an
extensionless key, a `.env.local`, a committed config full of tokens.

The reference implementation of server-side prefetch had **no validation whatsoever** and
would read a private SSH key on request. (ADR-0006)

Order decides which message you get, not just whether the file is refused. A `.pem` is
stopped by layer 2, never reaching the denylist, because its extension is not allowlisted;
layer 3 fires only for files the allowlist was happy with — `client_secret.json`,
`.docker/config.json`, anything matching `*secret*` or `*credential*` with a source
extension.

Two shapes of entry share the allowlist, and the second is easy to lose. `.py` is a
suffix; `.gitignore`, `.makefile` and `.dockerfile` are whole filenames written with a
leading dot. A file with no suffix is therefore matched by *name*, which is what makes
`Makefile` and `.gitignore` readable at all — matching suffixes alone would refuse exactly
the entries somebody added on purpose.

### Windows paths are accepted, and translated for you

`files[]` comes from Claude Code on Windows; the server runs in WSL. Drive-letter paths
(`C:\proj\src\foo.py`, in either separator style) and `\\wsl$\...` paths are translated
before any layer runs. A UNC network share is refused outright rather than translated into
something that resolves to nothing, and a relative path is refused because it would
resolve against the server's working directory rather than yours.

### Refusals are actionable, and they fail the whole call

A **refusal** means the file was not allowed. Nothing is dispatched — not the other files,
not the task — and *every* refusal is reported, so one correction fixes all of them rather
than costing a round trip each:

```
2 of 3 path(s) in files[] were refused, so nothing was sent to the model. Every
refusal is listed, not just the first, so one correction fixes all of them:

  C:\proj\.env
      layer 2, extension allowlist: the filename '.env' is not on the extension
      allowlist.
      Extension is the one axis that can be allowlisted for file contents [...]

  C:\proj\client_secret.json
      layer 3, secret denylist: it matches the secret denylist pattern '*secret*'.
      Delegated models never receive credential material. [...]
```

A **skip** is the other category and behaves oppositely: the call proceeds, the file is
left out, and the accounting says so. Size and binary content are skips, not refusals —
they are facts about a file that was allowed.

### Files are skipped whole, never truncated

A source file cut mid-function is worse than an absent one, because the model will
confidently repair code it never saw. Over-budget files are dropped entirely, with the
message pointing at paginated `read_file` — available in the agentic loop, though not in
one-shot mode, where the file is simply unavailable and the message says so.

### Budgets are in tokens, not bytes

Bytes mislead by more than a factor of two: the measured ratios are in
the 2026-08-25 entry in
[JOURNAL.md](../JOURNAL.md), which owns them. A byte cap would allow twice the *context* for a data file as for a
source file, which is backwards — data files are the ones worth trimming. (ADR-0019)

### What the policy does not cover

`read_file` and `write_file` are governed by the policy and run in the server process. The
`workdir` argument is checked as its own surface, against
[`workdir_roots`](CONFIGURATION.md), which falls back to the workspace roots when unset —
reading a project and being able to work in it are separable grants, and a workdir is bound
**writable** for the whole call. It is resolved before it is compared, so a symlink sitting
inside a root but pointing out of it is refused on where it lands.

Only `run_bash` enters the sandbox — and a shell can read anything visible to it, so for
`run_bash` the policy alone is decorative. The secret denylist is therefore *also* enforced
at the mount level, by mounting something empty over each match. One matcher. (ADR-0035)

## A worked example

Reviewing a module without its contents ever entering Claude's context:

```
delegate(
  task = "Review this for correctness bugs. Ignore style.",
  files = ["C:/proj/src/payments/refund.py",
           "C:/proj/tests/test_refund.py"],
)
```

Fixing a failing test, with the model verifying its own work:

```
delegate_to_agent(
  agent_name = "test-writer",
  task = "test_refund_partial fails after the currency change. Fix the code, not the test.",
  files = ["C:/proj/src/payments/refund.py",
           "C:/proj/tests/test_refund.py"],
  workdir = "C:/proj",
)
```

`workdir` is what separates an agent that can only read from one that can work: it binds
that directory into the sandbox, writable, so `run_bash` can run the project's tests there.
It is checked against the workdir roots **before** it is used to look the agent up, since
the lookup reads `<workdir>/.claude/agents/` and a check that runs afterwards is not a check.

Several tasks over the same material go in one call:

```
delegate_batch(
  agent_name = "reviewer",
  files      = ["C:/proj/src/payments/refund.py"],
  tasks      = ["Check the currency rounding.",
                "Check the refund window boundary.",
                "Check for unhandled partial refunds."],
)
```

Every item shares the agent body and the files block and differs only in the task, which is
exactly the order [ADR-0011](../DECISIONS.md) fixed the prompt in — so the shared part is
served from the cluster's prefix cache instead of being paid for three times. Items run
concurrently up to the endpoint's declared `concurrency`, and **one failing does not fail
the batch**: each result carries its own `ok`, so read `failed` before trusting a summary.
(ADR-0037)

The result reports `bash_failures` and `last_bash_exit` **captured by the server**, not
claimed by the model. Trust those over the prose: models misreport command outcomes, and
the ancestor ships a dedicated test because of it. (ADR-0007)

## The server runs git, and that is not the sandbox

`paths.py` shells out to git twice, both inside the server process: layer 4 runs
`check-ignore` to decide what a delegated model may see, and `repo_status` runs
`status --porcelain` for the ground truth in a context-overflow abort report. Neither is a
route into `run_bash`, which is bwrap-confined and refuses rather than run unconfined where
bubblewrap is absent (ADR-0010, ADR-0034) — these are the server's own calls, with arguments
it chose. `repo_status` sees only work trees the delegation wrote to, never every root.
