<!-- BUDGET: 160 -->
# claude-delegate-local-mcp

An MCP server that lets Claude Code hand work to a local model you host yourself.

Bulk, mechanical, read-heavy work — reading a subsystem, writing tests, mechanical
refactors, first-pass review — costs cloud tokens even when the reasoning required is
modest. This moves that class of work onto your own hardware, where it is effectively
free, and keeps Claude for the parts that need it.

**Status: early.** `delegate()` and `backend_status()` work end to end against a real
endpoint. `files[]`, the agentic loop, the sandbox and the agent roster do not. See
[STATUS.md](STATUS.md) for where things actually stand and [PLAN.md](PLAN.md) for the
route.

## How it works

Two shapes of delegation, and the second is the interesting one:

- **One-shot** — file contents are read *server-side* and inlined into a single prompt.
  The model answers; the bytes never enter Claude's context. Good for review, summary,
  explanation.
- **Agentic** — the local model gets its own `read_file`, `write_file` and `run_bash`, and
  iterates. It writes code, runs your test suite, reads the real failure, fixes it, and
  runs again — for as many turns as it needs, at **zero cloud token cost** — then hands
  back a result for Claude to review.

That second loop is why the shell exists, and why `run_bash` is confined by
[bubblewrap](https://github.com/containers/bubblewrap) in an empty-root sandbox with the
network off by default. If bubblewrap is unavailable the server **refuses** to run shell
commands rather than running them unconfined.

The server also captures real process exit codes itself and reports them separately from
whatever the model claims in its answer. Models misreport command outcomes; the whole
self-verification idea collapses without a ground truth.

## What it is not

- Not a cloud router. One backend format ships — OpenAI-compatible. Anthropic-compatible
  endpoints are a planned addition behind an existing seam, not a current feature.
- Not a way to run Claude Code against a different model. It delegates *tasks*; Claude
  Code stays Claude Code.
- Not a sandbox for untrusted code. It confines a model you chose to run against a
  workspace you chose to expose.

## Requirements

- A local OpenAI-compatible endpoint (this was built against vLLM serving DeepSeek V4
  Flash on two DGX-Spark-class machines, but nothing depends on that specific stack).
- Python 3.11+.
- **Linux, or WSL2 on Windows** — `bubblewrap` is Linux-only and there is no cheap Windows
  equivalent, so the server runs there even when Claude Code does not.

## Install

```bash
git clone https://github.com/ComputerSaysNull/claude-delegate-local-mcp
cd claude-delegate-local-mcp

cp .env.example .env                  # then set DELEGATE_WORKSPACE_ROOTS
cp models.toml.example models.toml    # then set your endpoint

python -m venv .venv && .venv/bin/pip install -e ".[dev]"
python scripts/install_hooks.py       # optional, gives the gate at commit time
```

On Windows plus WSL2 the two interpreters cannot share `.venv`: a Linux `python -m venv
.venv` overwrites a Windows one in place, and it reads as a corrupted install rather than a
collision. Put the WSL one elsewhere, on the native filesystem rather than `/mnt/c` where
creating it is ~27x slower (ADR-0020) — `python3 -m venv ~/.venvs/delegate`, then
`~/.venvs/delegate/bin/pip install -e /mnt/c/path/to/the/repo`.

`.env` is read by `config.load()` as a fallback: anything already set in the environment
wins, so an explicit override still works. Point `DELEGATE_ENV_FILE` at another file to use
one elsewhere — if it names a file that does not exist, that is an error rather than a
silent fall back to defaults. ADR-0027 for why the server reads the file itself instead of
taking an `env` key from your MCP client's configuration.

`.env` and `models.toml` are gitignored, deliberately: they name a host, and a hostname
identifies your machine as surely as an address does.

### On Windows, with the server in WSL2

```powershell
wsl --install -d Ubuntu-24.04
```

Then inside Ubuntu — and verify rather than assume, because two of these fail silently:

```bash
sudo apt install -y bubblewrap python3 python3-venv git
bwrap --unshare-all --ro-bind /usr /usr --ro-bind /etc /etc --proc /proc \
      --dev /dev --tmpfs /tmp --symlink usr/bin /bin --symlink usr/lib /lib \
      --symlink usr/lib64 /lib64 -- /bin/echo ok      # must print: ok
getent hosts YOUR-HEAD-NODE                           # must resolve in WSL, not just Windows
```

The `usr/lib64` symlink is not optional on x86-64: without it nothing dynamically linked
runs, and the error blames the executable rather than the missing loader.

## Register with Claude Code

Native Linux:

```json
{ "mcpServers": { "delegate-local": {
    "command": "claude-delegate-local-mcp",
    "timeout": 900000
} } }
```

Windows, server in WSL2:

```json
{ "mcpServers": { "delegate-local": {
    "command": "wsl.exe",
    "args": ["-d", "Ubuntu-24.04",
             "--cd", "C:\path\to\claude-delegate-local-mcp",
             "-e", "/home/YOU/.venvs/delegate/bin/claude-delegate-local-mcp"],
    "timeout": 900000
} } }
```

`--cd` and the script's absolute path are both load-bearing: without them the server starts
in the wrong directory or is not found at all, and neither failure names its own cause —
[TROUBLESHOOTING](docs/TROUBLESHOOTING.md#startup) has both symptoms. Give `--cd` the
Windows form of the path; `/mnt/c/...` is rejected.

`timeout` is milliseconds, and the wall-clock default is generous. A long delegation can
still trip the separate 30-minute stdio *idle* timeout; the per-turn progress notification
that answers that is M4, so until then keep one-shot tasks well inside it.

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit, and why |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting *(generated)* |
| [docs/MODELS.md](docs/MODELS.md) | The registry, and adding a model |
| [docs/AGENTS.md](docs/AGENTS.md) | Agent files, and the path policy |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Symptom to cause to fix |
| [DECISIONS.md](DECISIONS.md) | Numbered decisions, newest first |
| [JOURNAL.md](JOURNAL.md) | What took real work to figure out |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup and conventions |

No configuration default is stated anywhere but `src/claude_delegate_local/config.py`,
which is where they live; `docs/CONFIGURATION.md` is generated from it and is a rendering,
never a source to edit. If you find one repeated elsewhere, that is a
bug — [CLAUDE.md](CLAUDE.md) explains the scheme.

## Provenance and licence

MIT. A derivative work, not an independent implementation: substantial code was ported
from [fegone/claude-code-delegate-local](https://github.com/fegone/claude-code-delegate-local)
and its [mixicz](https://github.com/mixicz/claude-code-delegate-local) fork, both MIT. The
server-side context-prefetch idea comes from
[fjgbue/claude-delegator-deepseek-mcp](https://github.com/fjgbue/claude-delegator-deepseek-mcp).
[NOTICE](NOTICE) records what came from where, feature by feature.
