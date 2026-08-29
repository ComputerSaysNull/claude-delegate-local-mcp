<!-- BUDGET: 290 -->
<!-- Raised from 240 on 2026-08-27: M1 shipped the first server that can fail at
     startup and the first tool that can report a backend, so two symptom classes
     exist that had nowhere to be indexed before.
     Raised again from 265 on 2026-08-27: M3 split the empty answer into two symptoms
     with opposite fixes, and one stale entry was deleted to help pay for it.
     Raised from 270 to 290 on 2026-08-27: the second audit found nine entries describing
     unbuilt subsystems with nothing saying so, and marking them costs lines. -->
# Troubleshooting

Symptom, cause, fix.

**This document owns no facts.** It links to whichever document owns the answer and never
restates a default, a schema or a value. That is not fussiness: restating defaults inside
symptom explanations is how the project this descends from ended up documenting one setting
as three different values in three places. Add the link instead.

**Entries marked *(not built)* describe a symptom you cannot hit yet**, written ahead of the
subsystem so the index is ready when it lands; until then nothing in the server can produce
them. [PLAN.md](../PLAN.md) has the roadmap, and a marker outlasting its milestone is a
defect in itself: the 2026-08-28 audit found four, all of them M4's.

---

## Startup

### The server refuses to start, naming a setting

Intended. Every setting is validated at load, never at first use — a bad timeout discovered
thirty minutes into a delegation is far worse than a refusal to boot. The message names the
variable and what was wrong with it. See [CONFIGURATION.md](CONFIGURATION.md).

### Claude Code shows the server failed, and says nothing else

There is nowhere for it to say more: stdout carries the MCP protocol, so a startup failure
goes to stderr, which a launcher discards. Run the registered command yourself to read it.
A configuration problem prints one line and exits non-zero; a healthy server prints nothing
and waits, which looks like a hang and is not one.

### `Model registry not found`, but the file is plainly there

`wsl.exe -e` inherits the Windows working directory, so the server starts inside whichever
project Claude Code was launched from and looks for `models.toml` there. Pin `--cd` in the
registration ([README](../README.md#register-with-claude-code)), in its Windows form — a
`/mnt/c/...` argument is rejected with `ERROR_PATH_NOT_FOUND`.

### `No such file or directory` naming the console script

A venv's `bin/` is not on the PATH `wsl.exe -e` uses; register the script's absolute path.

### `DELEGATE_WORKSPACE_ROOTS is required`

There is no safe default for "which files may a delegated model read". Set it to the
directory holding your projects. It is layer 1 of the policy in
[AGENTS.md](AGENTS.md#the-path-policy).

### `Model registry not found`

Copy `models.toml.example` to `models.toml` and fill in your endpoint. It is gitignored on
purpose — it names a host. See [MODELS.md](MODELS.md).

### `has unknown field 'xyz'`

Refused rather than ignored: a typo in a registry key would otherwise cost you the setting
silently. Check the field list in [MODELS.md](MODELS.md#fields).

### `base_url should not include the /v1 suffix`

The server appends the API path itself. The message gives the corrected value.

### Several models and none marked default

Silently choosing a model changes cost and behaviour, so the server will not choose. Mark
one `default = true` or set the global default. See [MODELS.md](MODELS.md).

---

## Connectivity

### `backend_unreachable`, but the endpoint works from a terminal

**If the server runs in WSL2, this is the usual cause.** WSL2 has its own network stack, so
name resolution working on Windows says nothing about what happens inside the guest — this
bites particularly with an overlay VPN, or with mDNS-style short names.

**Check that both sides resolve the name to the *same* address, not merely that both
resolve it.** A short name can resolve inside the guest through a search domain and land on
an entirely different host, which then refuses or drops the connection. That looks like a
network fault and is not one, and a check asking only "did it resolve?" reports success:

```bash
powershell -c "(Resolve-DnsName YOUR-HEAD-NODE -Type A).IPAddress"
wsl -d Ubuntu-24.04 -e bash -lc 'getent hosts YOUR-HEAD-NODE'
```

Different answers mean the guest is talking to something else entirely; the same answer
narrows it to routing. Either way the fix is the same: put the **address** in `models.toml`
rather than a short name — which is what ADR-0021 asks for, verify by address, never by
hostname.

### `served_model_id` mismatch

It must match the `id` field exactly:

```bash
curl -s http://YOUR-HEAD-NODE:8888/v1/models | python3 -m json.tool
```

`backend_status()` reports this as `id_confirmed: false` with `status: "ok"` — the endpoint
is healthy; the registry names something it does not serve. Check it first when a
delegation is refused by a cluster that is plainly up.

### The health check fails against a server that is plainly healthy

Bare vLLM does **not** serve `/health/liveliness` — that belongs to a proxy, and probing it
makes a healthy cluster look down. What is probed, and the six words `backend_status()` can
report with what each implies, are in
[ARCHITECTURE.md](ARCHITECTURE.md#backend_status-answers-a-question-a-stack-trace-cannot):
a wrong key is a `.env` edit, an unreachable endpoint is somebody else's hardware.

The result never contains the endpoint address (ADR-0029), which is what makes it safe to
paste into a bug report.

---

## Delegations that end badly

### The answer is empty, with a length stop

Reasoning consumed the entire reply budget. Seeing this means the server already tried to
recover and failed too ([the stages](DISPATCH.md#an-empty-answer-is-recovered-from-before-it-is-reported)),
so do not retry the call yourself. Measured: at the top effort level this deployment never
answered at any budget, so set `effort: low` ([MODELS.md](MODELS.md#choosing-default_effort)).

### `reasoning_exhausted: true`

Both mitigations were spent and it still returned nothing: the task needs more reasoning than
this model finishes inside its budget. Split it, or send it elsewhere — asking again will not
help.

### `empty_response: true` with `reasoning_exhausted: false`

A different fault, and a different fix: effort was already lowest, so nothing could step down
and the budget ran out rather than the reasoning. Shorten the task, or raise the model's cap.

### The model insists tests pass when they do not

Trust `bash_failures` and `last_bash_exit` in the result: those are captured by the server
from real process exits, and may contradict the prose. Models misreport command outcomes.
ADR-0007.

### Tool arguments arrive malformed

The model gets an actionable error and another turn. Recurring cases usually mean the
temperature is too high — tool-call syntax tokens are sampled at the request temperature,
so malformed calls grow likelier as it rises. See
[CONFIGURATION.md](CONFIGURATION.md#generation-budgets).

### `hit_turn_limit: true` with work unfinished

The task needed more round trips than the budget allowed. Raise `max_turns` on the call, or
split the task. Prefetching more via `files[]` also helps: without it the first turn or two
get spent exploring.

---

## Timeouts

### The call disappears after about 30 minutes

Not the wall-clock limit. This is Claude Code's separate **stdio idle timeout**, which
fires when the server sends nothing at all for its window. Progress notifications exist to
prevent it, so seeing this means they are not arriving. ADR-0018.

### The call disappears sooner than the configured timeout

Set `timeout` on the server's entry in your MCP config; the units and the accepted range
are Claude Code's, not this project's, so they are not restated here. Progress
notifications reset the *idle* timer but do **not** extend the wall clock, so the wall
clock must cover the whole delegation up front. ADR-0018.

### The tool call moves to the background after two minutes

Expected. Nearly every delegation will. It does not affect either timeout.

---

## Sandbox

*`sandbox.py` is built; nothing calls it yet.* `run_bash` still refuses every call until the
secret denylist is enforced at the mount level, so these symptoms reach you today only from
the sandbox's own tests or from a hand-run `bwrap`. The causes below are real either way.

### `bwrap not found; run_bash is disabled`

Deliberate. A security control that silently degrades to nothing is worse than an absent
one, because it is believed. Install it (`apt install bubblewrap`) or accept that shell
commands are unavailable. ADR-0010.

### `No such file or directory` for a command that plainly exists

Almost always the **ELF loader**, not the command — the kernel reports a missing
interpreter as if the executable were absent. Verify with the empty-root smoke test in the
[README](../README.md#on-windows-with-the-server-in-wsl2). ADR-0021.

### `bwrap: Can't find source path ...`, naming the sandbox HOME

The persistent HOME is bound from the host, and a bind needs its source to exist. The
message reads like a mistyped `DELEGATE_SANDBOX_HOME`, and on a fresh install it usually is
not — it is a directory nobody has created yet. The server creates it before every call, so
seeing this from the server itself means the path is genuinely unwritable. ADR-0034.

### `uv: not found` inside the sandbox

The empty root contains only what is bound, and `uv` lives outside it. Add toolchains via
`extra_binds` — see [AGENTS.md](AGENTS.md#extra_binds-for-toolchains).

### An agent with `network: true` cannot resolve hostnames

Under WSL, `/etc/resolv.conf` is a symlink to `/mnt/wsl/resolv.conf`, so binding `/etc`
binds a dangling link — connections by address succeed while names fail. The server binds
the resolved target; a custom invocation must too. ADR-0021.

### Confirming the sandbox is actually sealed

Test denial **by address**, never by hostname — see
[AGENTS.md](AGENTS.md#network-false-is-the-default-and-means-absent) for why.

---

## Paths, on Windows

### `Drive X: is not mounted in WSL`

WSL does not auto-mount every drive. Use a path on a mounted drive, or configure automount.

### UNC paths are refused

`\\server\share` and `\\wsl$\...` are out of scope. Related and worth knowing: Windows
cannot use a `\\wsl$\` path as a process **working directory** at all, so a repository
inside the WSL filesystem is not reachable by Windows-side tooling without mapping a drive
letter first. ADR-0020.

### `No such file` for a path that exists

Windows is case-insensitive; the Linux filesystem is not. Paths are resolved as given, so a
case mismatch fails as a missing file. Copy the path from a file listing rather than typing
it.

### Everything is slow

Expected on `/mnt/c`, and accepted knowingly. ADR-0020 has the measurements and the
alternatives that were rejected.

---

## Contributing

### The gate blocks on a document I did not touch

Changed code must arrive with its owning document. Ask which one:

```bash
python scripts/docs_gate.py --owner src/claude_delegate_local/paths.py
```

If it is genuinely unnecessary — a pure rename, say — add
`Docs-Gate-Skip: owning-doc -- <reason>` to the commit message. It stays visible in every
run. See [../CONTRIBUTING.md](../CONTRIBUTING.md).

### `STALE:` on a generated document

It is rendered from code or from PLAN.md, and is never edited by hand. Run the matching
generator and commit the result — [../CLAUDE.md](../CLAUDE.md) lists which one renders
which, and is the only place that mapping lives.

### The gate blocks a new document as a "split dodge"

Its audience and owned code are both subsets of an existing document's, which makes it a
size budget being evaded rather than a split. Raise the other document's budget with a
reason instead. ADR-0003.

### A commit is refused over my email address

The author must be in `security/allowed_emails.txt`. On a machine whose global git identity
belongs to an employer, check `git config user.email` **inside** the repository — a
directory-conditional include should be overriding it.
