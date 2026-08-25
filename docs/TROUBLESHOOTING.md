<!-- BUDGET: 240 -->
# Troubleshooting

Symptom, cause, fix.

**This document owns no facts.** It links to whichever document owns the answer and never
restates a default, a schema or a value. That is not fussiness: restating defaults inside
symptom explanations is exactly how the project this descends from ended up documenting one
setting as three different values in three places. If you want to add a value here, add the
link instead.

---

## Startup

### The server refuses to start, naming a setting

Intended. Every setting is validated at load, never at first use — a bad timeout discovered
thirty minutes into a delegation is far worse than a refusal to boot. The message names the
variable and what was wrong with it. See [CONFIGURATION.md](CONFIGURATION.md).

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
name resolution working on Windows says nothing about whether it works inside the guest —
this bites particularly with Tailscale or mDNS-style short names.

Check from inside the guest, not the host:

```bash
wsl -d Ubuntu-24.04 -e bash -lc 'getent hosts YOUR-HEAD-NODE; \
  curl -s -m 8 http://YOUR-HEAD-NODE:8888/v1/models | head -c 80'
```

If `getent` fails but the address works, add an entry to `/etc/hosts` inside the guest, or
use the address in `models.toml`.

### Health check fails against a healthy server

The server probes `GET {base_url}/v1/models`. If you have configured something else, note
that bare vLLM does **not** serve `/health/liveliness` — that belongs to a proxy, and
probing it makes a healthy cluster look down.

### `served_model_id` mismatch

It must match the `id` field exactly:

```bash
curl -s http://YOUR-HEAD-NODE:8888/v1/models | python3 -m json.tool
```

---

## Delegations that end badly

### The answer is empty, with a length stop

Reasoning consumed the entire reply budget. The server retries at a larger budget, then
steps effort down, then fails explicitly. Persistent cases mean the effort level is too
high for the task — set `effort: low`. See
[MODELS.md](MODELS.md#choosing-default_effort).

### `HTTP 400 ... thinking_token_budget`

Feature-detected and dropped automatically; you should only see it in logs. Note the
serving stack's own documentation names the wrong gate for this field — the server's error
message names the real one. ADR-0017.

### `reasoning_exhausted_budget`

Every mitigation was tried and the model still returned nothing. Reframe the task, or lower
effort further.

### The model insists tests pass when they do not

Trust `bash_failures` and `last_bash_exit` in the result: those are captured by the server
from real process exits, and may contradict the prose. Models misreport command outcomes.
ADR-0007.

### Tool arguments arrive malformed

The model gets an actionable error and another turn. Recurring cases usually mean the
temperature is too high — tool-call syntax tokens are sampled at the request temperature,
so malformed calls grow likelier as it rises. See
[CONFIGURATION.md](CONFIGURATION.md#generation-budgets).

### `turn_limit` with work unfinished

The task needed more round trips than the budget allowed. Raise `max_turns` in the agent's
frontmatter, or split the task. Prefetching more via `files[]` also helps: without it the
first turn or two get spent exploring.

---

## Timeouts

### The call disappears after about 30 minutes

Not the wall-clock limit. This is Claude Code's separate **stdio idle timeout**, which
fires when the server sends nothing at all for its window. Progress notifications exist to
prevent it, so seeing this means they are not arriving. ADR-0018.

### The call disappears sooner than the configured timeout

Set `timeout` on the server's entry in your MCP config — milliseconds, minimum 1000.
Progress notifications reset the *idle* timer but do **not** extend the wall clock, so the
wall clock must cover the whole delegation up front.

### The tool call moves to the background after two minutes

Expected. Nearly every delegation will. It does not affect either timeout.

---

## Sandbox

### `bwrap not found; run_bash is disabled`

Deliberate. A security control that silently degrades to nothing is worse than an absent
one, because it is believed. Install it (`apt install bubblewrap`) or accept that shell
commands are unavailable. ADR-0010.

### `No such file or directory` for a command that plainly exists

Almost always the **ELF loader**, not the command — the kernel reports a missing
interpreter as if the executable were absent. Verify with the empty-root smoke test in the
[README](../README.md#on-windows-with-the-server-in-wsl2). ADR-0021.

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

### `STALE: docs/CONFIGURATION.md`

It is generated. Run `python scripts/gen_config_docs.py` and commit the result; never edit
the table by hand.

### The gate blocks a new document as a "split dodge"

Its audience and owned code are both subsets of an existing document's, which makes it a
size budget being evaded rather than a split. Raise the other document's budget with a
reason instead. ADR-0003.

### A commit is refused over my email address

The author must be in `security/allowed_emails.txt`. On a machine whose global git identity
belongs to an employer, check `git config user.email` **inside** the repository — a
directory-conditional include should be overriding it.
