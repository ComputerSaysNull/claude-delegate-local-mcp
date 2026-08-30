<!-- BUDGET: 240 -->
# Agents and the path policy

Two things a user actually touches: the agent files that shape a delegation, and the rules
governing which files a delegated model may see.

Tool internals are in [TOOLS.md](TOOLS.md), generated from `tools.py`; sandbox
mechanics are in [ARCHITECTURE.md](ARCHITECTURE.md). Settings are in
[CONFIGURATION.md](CONFIGURATION.md).

**Status: the path policy is built and enforced; agents are not.** `paths.py` landed in M2;
`agents.py` is M6, so everything above [The path policy](#the-path-policy) describes a
design rather than behaviour. The roadmap is [../PLAN.md](../PLAN.md).

## Why agents are files

There are five MCP tools and there will stay five. A new *kind* of delegated task — review,
test-writing, refactoring, migration — is a markdown file, not a new tool.

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
| `effort` | `off`, `low`, `high`, `max`. Refused loudly if misspelt |
| `max_turns` | Round trips, capped by the server's hard ceiling |
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

`read_file` and `write_file` are governed by the policy and run in the server process. Only
`run_bash` enters the sandbox — and a shell can read anything visible to it, so for
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
