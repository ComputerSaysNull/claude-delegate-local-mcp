<!-- BUDGET: 489 -->
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

There are eight MCP tools: `delegate`, `delegate_readonly`, `delegate_to_agent`,
`delegate_to_agent_readonly`, `collect`, `cancel_delegation`, `list_agents` and `backend_status`. A new *kind* of delegated task — review, test-writing,
refactoring, migration — is a markdown file, not a new tool. A test asserts the exact set,
so another cannot arrive unargued.

`delegate_readonly` is one argument ADR-0005 asked for and not an exception to it. That rule
is about task *kinds*, and this is not: it is `delegate` with the tool set fixed, existing
because a client decides whether to prompt before a call runs and can only read the tool's
annotation then. A read-only call cannot be expressed where permission rules never inspect
arguments; only a read-only tool can. No agent file can carry that constraint, because the
tool an agent is reached through can write. (ADR-0042) `delegate_to_agent_readonly` is the
same argument again, and what it keeps that `delegate_readonly` cannot is the agent file
itself. (ADR-0059)

Two batch tools were argued for and cancelled, once the prefix sharing that justified them
was measured and found to need no tool of its own. (ADR-0051)

That keeps the tool list Claude sees from growing without bound, and makes adding a task
type a file rather than a code change and a release. The format is the one Claude Code
already uses for its own subagents. (ADR-0005)

ADR-0005 went further and called the files *portable*. They are not, and this repository's
own `.claude/agents/*.md` are the counter-example: Claude Code spells the tool list `tools`
where this format spells it `allowed_tools`, and accepts an `effort: medium` that
[config.py](../src/claude_delegate_local/config.py) deliberately refuses. The shape is
borrowed; a file is not moved between the two unedited. (ADR-0031)

<!-- GEN:AGENT-FORMAT-LOCATIONS:START -->
<!-- Generated from src/claude_delegate_local/skills/write-delegate-agent/SKILL.md by scripts/gen_agent_format_docs.py. That file ships inside the package; edit it, not this. -->

## Where the file goes

Three locations, **first match wins**, so a project's own agent shadows a personal one of
the same name:

1. `<project>/.claude/delegate-agents/<name>.md`
2. `<project>/.claude/skills/<name>/SKILL.md`
3. `<agents_dir>/<name>.md` — the personal tier, set by `DELEGATE_AGENTS_DIR`

**Not `.claude/agents/`**: Claude Code loads every file there as its own subagent, with
every tool. That directory is still read, last, for one release, and `list_agents` names
each agent it finds there under `old_location`, with where to move it.

The **filename supplies the name** — `<name>.md`, or the skill directory for a `SKILL.md`.
A name must match `^[A-Za-z0-9_-]+$`: it is a name, not a path, so it cannot traverse.

<!-- GEN:AGENT-FORMAT-LOCATIONS:END -->

`list_agents` gave one answer until 2026-09-02: a file that did not parse was simply left
out, and the standing advice for a missing agent — ask by name, read the error — needs the
very name an omission hides. Hence three, and `other_format` (ADR-0031) rather than a
second kind of broken: four of this repository's own five agent files are in it
deliberately, and folding them into `skipped` would leave that list permanently non-empty,
which is a list nobody reads. A shadowed name is listed once, as the lookup offers one — but a
Claude Code file shadows nothing: the lookup passes over it, so its name can be in both lists.

<!-- GEN:AGENT-FORMAT-FIELDS:START -->
<!-- Generated from src/claude_delegate_local/skills/write-delegate-agent/SKILL.md by scripts/gen_agent_format_docs.py. That file ships inside the package; edit it, not this. -->

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
---

You write tests for existing code. Read the module, then write tests that would have
caught the bug described in the task. Run them. Iterate until they pass.

Report the outcome of the final test run.
```

A frontmatter block is **required** — a `---` line, a closing `---`, and something between
them: a file with no block, or whose `---` lines are adjacent, is refused. **No individual
field is required**, so a block of one blank line yields an all-default agent.

Every key must be one of these ten. An unknown key is **refused, not ignored**, because a
typo that costs you a setting in silence is the failure this format was rewritten to
prevent:

| Key | Value | Notes |
|---|---|---|
| `name` | the agent's name | Optional, but if present it **must equal the filename**, or the file is refused — and a bare `name:` counts as present, because a key whose value went missing is a setting that silently does nothing |
| `description` | one line | What `list_agents` reports, so a caller can choose without opening the file |
| `model` | a model-registry key | Binds the dispatch |
| `effort` | `off`, `low`, `high`, `max` | Refused loudly if misspelt. **Not** `medium` — the backend has no such level. **Not** `inherit` — that is what a *caller* passes to defer to this file |
| `max_turns` | integer ≥ 1 | Above the server's hard cap the file is **refused, not clamped**: a caller's number is transient, a file is committed and trusted |
| `max_tokens` | integer ≥ 1 | Reply budget; the model's own cap still applies afterwards |
| `keep_tool_results` | integer ≥ 0 | How many recent tool results survive history eviction. `0` evicts each one as the next turn starts |
| `allowed_tools` | `[a, b]` | From `read_file`, `search_files`, `read_git`, `run_bash`, `write_file`, `edit_file`. Naming an unimplemented tool is refused. Brackets are the format: bare `a, b` is refused, and so is a `-` block list |
| `network` | `true` / `false` | Default false. An **operator grant**: the agent must also be listed in `DELEGATE_AGENT_NETWORK_ALLOWED` and found in the personal tier |
| `extra_binds` | `[a, b]` | Extra directories visible in the sandbox, within operator-set roots. Read-only, and the denylist still reaches inside. Bracketed like `allowed_tools`, and refused the same way |

Precedence for a value: the call argument, then this file, then the registry row (for
`model` and `effort` only), then the server default.

## The body

Everything below the closing `---` is the role's instructions, stripped of surrounding blank
lines. A body is optional. A `---` inside it is just text: only the opening block is parsed.

It is delivered at the **head of the user message** — body, files, then task — and not as
the system prompt, which is a cached constant nothing per-delegation may enter (ADR-0011).
A body is therefore resent every turn: keep it to standing instructions for the role.

## Validate it

Do not assume the file was read. Call `list_agents` with the same `project` you intend to
delegate with, and find your name: **`agents`** is usable and the only success;
**`skipped`** means it was found and did not parse, with the reason on the entry;
**`other_format`** means it read as a Claude Code agent file, recognised by keys such as
`tools` that this format does not have. Absent from all three means no file was found at
any of the three locations — check the filename against the name, and the `project`.

<!-- GEN:AGENT-FORMAT-FIELDS:END -->

## Why the format is what it is

**Every field is overridable per call except `network` and `extra_binds`**, which no tool
takes an argument for: a caller cannot ask for a bind or for egress, only a file can, and
that is the surface the two checks below exist for. `max_turns` was the exception until
2026-09-03: it read the file and ignored the argument, so the single field that truncates a
run was the single field that needed the file edited to change.

`effort: inherit` is refused in a file rather than accepted, because a file is a tier that
word defers *to* (ADR-0045).

### `model` genuinely binds

In the ancestor, frontmatter was loaded and then largely ignored — `model:` did nothing.
That is a real bug the fork fixed, and it is worth naming because it is easy to reintroduce:
resolution must be consistent between the code that picks a concurrency bucket and the code
that makes the call. If those disagree, the request is counted against one endpoint's limit
and sent to another.

### An over-cap `max_turns` is refused, where a caller's is clamped

A caller passing `max_turns` above [`max_turns_hard_cap`](CONFIGURATION.md) has it clamped
in silence, because the work is legitimate and only the number is not
([DISPATCH.md](DISPATCH.md)). An agent *file* asking for the same thing is refused when it
loads.

The difference is what happens next. A call argument is gone the moment the call returns; a
file is committed, read again, and believed. Clamping there would leave the wrong number in
the file forever, working correctly and reading as though it were in effect — and the person
who eventually needs those turns would have no way to tell it never had them.

### One path policy, three dispositions

`resolve_files` is what `files[]` uses: it returns what resolved beside what did not, so a
refused path costs the call its file rather than the call, and `files_skipped` carries the
path, the layer and the remedy. Refusing *every* path still dispatches nothing, since
nothing would be left to send (ADR-0061). `resolve_all` raises on any refusal, right
wherever one path means one answer — a mid-loop `read_file` cannot partially succeed.
`resolve_permitted` drops what fails, only for paths nobody named — the candidates a
`search_files` walk enumerates, where a file the policy declines is not an error but simply
not a result.

A refusal names the surface it came from, and says what it cost. `workdir` is resolved
before anything is dispatched, so its refusal ends the call and says so. A `path` given to
`read_file` or `search_files` is refused *inside* a tool call and comes back as a tool error
the delegation continues from — so it names the `path` argument and makes no claim about a
dispatch. Until 2026-09-06 every one of those reported "path(s) in `files[]` were refused,
so nothing was sent to the model", which named an argument the model had not written and a
consequence that had not happened. A remedy names the configured roots rather than offering
to drop the argument — a refusal fires when the caller has just shown it does not know the
layout, and for a search that fallback was the slowest call available (ADR-0074).

All three run the same four layers through the same function, so a pattern cannot deny
`read_file` while leaving the same file findable by search. Separate functions rather than a
flag, because using a lenient one on a caller-supplied path is a silent drop — the failure
the strict ones exist to prevent.

A directory is a third case: `resolve_search_root` checks layer 1 alone against
`workspace_roots`, where `resolve_workdir` checks a directory against `workdir_roots`. The
roots differ because the surfaces do — one governs what may be read, the other a
read-write bind into a sandbox.

A glob in `files[]` is a fourth, and it sits *before* all of them: `expand_globs` turns one
pattern into the files it matches and then hands those to the same `resolve_files` a
literal list goes through. So a match is judged exactly as if the caller had typed it, and
expansion never widens the policy — only what a caller has to type. Two things bound it.
The part before the first wildcard must resolve inside a workspace root, checked before the
walk rather than on its results, or `/**` would read the machine before anything refused it;
and a pattern matching more than `max_glob_matches` is refused rather than truncated,
because a truncated expansion sends a subset and calls it the answer. A pattern matching
*nothing* is refused too — silence there is indistinguishable from a typo that cost nothing,
and the caller is the only one who can tell them apart. (ADR-0097)

### `allowed_tools` is enforced twice

Once when declaring tools to the model, and again at execution. Filtering only the declared
list is **advisory** — a model can call a tool it was never offered, and some do. If you
touch one enforcement site, check the other. Both live side by side in `tools.py`, and a
call to a tool outside the set comes back as an error result rather than ending the
delegation: mid-loop, throwing away every turn already paid for because the model made one
bad call costs more than telling it no.

### `network: false` is the default, and means absent

Not firewalled: the sandbox does not have a network namespace at all. `network: true` is a
grant an operator makes rather than one a file takes — it needs the agent named in
`DELEGATE_AGENT_NETWORK_ALLOWED` *and* the file found in `agents_dir`, and is refused, not
quietly dropped. The name alone is not enough: the workspace tiers are searched first, so a
repository can ship a file under any name it likes. Gated harder than `extra_binds` because
`--share-net` has no destination list to pin egress to, and a bind has a root. (ADR-0053)

Verifying this correctly matters more than it sounds. A hostname request fails whether or
not the namespace is isolated, so **test denial by address**, never by hostname — otherwise
a broken resolver reads as a tight sandbox. (ADR-0021)

### `extra_binds` for toolchains

The sandbox starts from an empty root, so only what is bound exists. `python3` comes from
the system bind and a project virtualenv works because it sits inside the bound workdir —
but a tool installed under your home directory, `uv` being the common case, is **absent**,
and `uv run pytest` fails with "not found". That is the most likely first-run surprise. The
server binds the resolved `uv` by default; anything else goes here.

A bind here is **read-only**, the secret denylist still reaches inside it, and it must
resolve inside `DELEGATE_AGENT_BIND_ROOTS`. The *resolved* path is both what is checked and
what is carried forward, so a link inside a root pointing out of it is refused on where it
lands rather than approved on where it sits. A bind at or *above* something the sandbox
mounts itself is refused too — binds are emitted after those mounts, so naming one replaces
it. Inside one is fine, and is how a toolchain under `/tmp` works at all.

The provisioned virtualenv root is reserved too, and needs its own entry because HOME's
does not reach it: it sits *inside* HOME, where a bind is allowed. Naming it would supply an
interpreter the sandbox did not build, whose exit code ADR-0007 tells every reader to
believe — and it is the one tree the scan skips rather than covers, so nothing else would
notice. (ADR-0062)

Not the workspace roots, on two counts. The field exists to reach a toolchain living
outside one, and a list shared with a reading tool would let a root widened for `files[]`
widen a mount. The reasoning this replaces — that the denylist made a root list unnecessary,
so *"no agent file can bind a directory in order to read credentials out of it"* — was
wrong when it was written: that scan matches names, so a credential in a file it does not
recognise was never covered. (ADR-0036, ADR-0053)

## The path policy

Four layers, checked in order, cheapest first, and then a fifth check when the file is
actually opened. They apply to `files[]` *and* to the model's own `read_file`,
`write_file` and `edit_file`.

`write_file` creates, so for it — and only for it — layer 1's existence test is relaxed: a
missing file is allowed, while the directory to write into must still exist and an existing
*directory* is still refused. Every other layer runs unchanged, because writing to a secret
path is worse than reading one rather than better.

Inside layer 1 the roots come before existence, for a file, a search path and a workdir
alike, so a path outside every root is never told whether it exists.

| | Layer | Refuses |
|---|---|---|
| 1 | Workspace roots | Anything whose **real** path falls outside a configured root. Resolution happens after symlinks, which closes escape through a link that was already there; one planted afterwards is caught at the open, below |
| 2 | Extension allowlist | Anything whose extension is not listed |
| 3 | Secret denylist | `.env*`, `*.pem`, `*.key`, `id_*`, `*credential*`, `*secret*`, `.git/**`, and more — **and, at the open, private-key armour in the first `secret_content_scan_bytes` whatever the file is named** |
| 4 | Gitignore | Anything git ignores |
| 6 | Protected path | **Writes only.** A file a host program acts on, from `security/protected_globs.txt`, whether or not it exists yet |

Layer 6 asks who may *change* a file rather than who may see it, so it runs for
`write_file` and `edit_file` and nothing else. Why those files, and why by name, is the
[design note](specs/2026-09-23-host-acted-paths.md)'s.

Layer 3 is the one layer that asks a question about *bytes* as well as about names. Every
other check is a function of the path, so a key renamed `config.json` passed all of them —
and `run_bash` could read one the mount-level scan had missed by name too, which is why the
same detector runs in `sandbox.py` as well. One pattern table serves both, or a pattern
added to one would leave the same bytes reachable through the other. It is deliberately
tiny — PEM headers and the PuTTY one — and is **not** `scan_text` pointed at contents: that
scanner hunts addresses, private-DNS suffixes and unfamiliar emails, and would fire on the
sources a review delegation exists to read. A check that cries wolf gets switched off, and
a switched-off check is worse than none because it is still believed. (ADR-0096)

A pattern may be prefixed `!` to **exempt** rather than deny, and every exemption is
checked before any deny. It is for the case where a broad pattern is right about a family
and wrong about one tracked member of it: `.env.*` denies every environment file and was
also denying the committed `.env.example`. Narrowing the pattern would be an allowlist by
omission — a `.env.production` added later would be readable, with nothing to say so —
where an exemption stays closed by default and opens exactly one name. (ADR-0067)

Allowlist first, deliberately. A *pure* allowlist cannot work for file contents — you
cannot enumerate every source file you might ever delegate — so extension is the axis that
*can* be allowlisted, and layers 3 and 4 are second and third nets for what passes it: an
extensionless key, a `.env.local`, a committed config full of tokens.

The reference implementation of server-side prefetch had **no validation whatsoever** and
would read a private SSH key on request. (ADR-0006)

The list is read from `secret_globs_file`, a relative setting resolving against the server's
working directory via `resolve_configured_path` — shared with the opaque list and with the
scan that must recognise both to avoid covering them (ADR-0065).

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

History gets the same two layers. `read_git`'s `show`, `diff` and `blame` judge each file
they would print by `extension_refusal` and `secret_match`, take commits only, and refuse
output carrying key material; layer 4 is not asked, since git reports no tracked file ignored.

### Approval is not the open

Layer 1 resolves symlinks at the moment it checks. It cannot close one planted
afterwards: between a path being approved and the file being opened there is a window,
and the adversary is the delegated model, which holds a read-write bind on its workdir
under `run_bash` and can retry until a swap lands. Within one batch the *directory* above a
candidate is remembered so a shared prefix is walked once; the final component never is,
because resolving that is what catches a link pointing out of the root.

So the policy hands back an open descriptor and never a path. The open refuses a link at
the final component outright, then proves the descriptor still refers to the approved
path, which catches a swapped parent directory too. What it deliberately does not treat
as a breach is a different *regular* file at an approved path — layers 1, 2 and 4 are
functions of the path, so those bytes could have arrived through `write_file` anyway.
Refused as `layer 5, the opened file`, with nothing read and nothing written. (ADR-0049)

The content check is the one thing that *does* judge the substituted bytes, and it runs
here rather than at resolve time for exactly that reason: it reads through the proven
descriptor, so what it scans is what the caller would have received. Refused as layer 3,
because it is the denylist's question asked a second way. (ADR-0096)

### Windows paths are accepted, and translated for you

`files[]` comes from Claude Code on Windows; the server runs in WSL. Drive-letter paths
(`C:\proj\src\foo.py`, in either separator style) and `\\wsl$\...` paths are translated
before any layer runs. A UNC network share is refused outright rather than translated into
something that resolves to nothing, and a relative path is refused because it would
resolve against the server's working directory rather than yours.

### Refusals are actionable, and they cost the call their file

A **refusal** means the file was not allowed. The call goes ahead with whatever did resolve,
and each refused path is reported in `files_skipped`, so one correction fixes all of them
rather than a round trip each. Refuse them all and nothing is dispatched — the message below:

```
3 of 3 path(s) in files[] were refused, so nothing was sent to the model. Every
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

The three file tools are governed by the policy and run in the server process. The
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

**Finding the agent is a separate argument**, `project`, which binds nothing and defaults
to `workdir`. Both are checked against the workdir roots **before** either is used to look
the agent up, since the lookup reads `<project>/.claude/delegate-agents/` and a check that runs
afterwards is not a check.

Several tasks over the same material are several calls, and they still share the prefix:

```
delegate_to_agent_readonly(
  agent_name = "reviewer",
  files      = ["C:/proj/src/payments/refund.py"],
  task       = "Check the currency rounding.",
)
```

Ask the next question the same way, with the same `agent_name` and the same `files[]`. The
agent body and the files block are identical and come first, which is exactly the order
[ADR-0011](../DECISIONS.md) fixed the prompt in, so the cluster serves the shared part from
its prefix cache rather than prefilling it again. **Keeping `files[]` stable across a series
of questions is what makes this work**, and it is worth more than any other tuning available
here. A batch tool once existed for this and bought nothing the separate calls do not
already get. (ADR-0051)

The result reports `bash_failures` and `last_bash_exit` **captured by the server**, not
claimed by the model. Trust those over the prose: models misreport command outcomes, and
the ancestor ships a dedicated test because of it. (ADR-0007)

## The server runs git, and that is not the sandbox

`paths.py` shells out to git in three places, all inside the server process: layer 4 runs
`check-ignore` to decide what a delegated model may see, `_repo_top` runs `rev-parse` to
find the work tree a path belongs to, and `repo_status` runs `status --porcelain` for the
ground truth in a context-overflow abort report. None is a route into `run_bash`, which is
bwrap-confined and refuses rather than run unconfined where bubblewrap is absent (ADR-0010,
ADR-0034). `repo_status` sees only work trees the delegation wrote to, never every root.
Each is fed or has stdin closed, never inherited, for the reason ARCHITECTURE's stdio rule gives.
A git failure in layer 4 refuses: only git's own "not a git repository", read in the C
locale, means outside a work tree, and anything else is a layer that could not answer.

Before `check-ignore` or `status`, a fourth call reads the repository's own config, and one
carrying a key off `TRUSTED_GIT_CONFIG_KEYS` is refused: that config can name a program for
git to run, and a repository `run_bash` created holds config the model wrote. `read_git`
refuses the same way, and every host-side git takes `GIT_HARDENING` and `git_env()`. Why
flags alone cannot do it is the [design note](specs/2026-09-23-host-acted-paths.md)'s.

`rev-parse` is the one that costs: it is charged per distinct *directory*, so a wide batch
spends a subprocess on each before a single `check-ignore` runs. The search walk calls layer
4 while enumerating, to prune an ignored directory before the scan cap is charged for it.
