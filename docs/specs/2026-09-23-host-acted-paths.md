<!-- BUDGET: 100 -->
# Host-acted paths, host-side git and provision

Design note for PLAN M13.7, M13.8 and M13.9 (review R3, R4, R5), written before the code as
CONTRIBUTING asks of `sandbox.py` and `paths.py`. It records what was measured, and the shape
each fix takes.

## The threat

Nothing here is about secrets; the denylist already covers them. It is about files a *host*
program reads as configuration or instructions, which a delegation can change before a person
reviews a diff. Untrusted content steering a trusted model is enough, and the model holds write
tools and, with a `workdir`, a read-write bind of the project.

## M13.7 — protected paths

**The list** is `security/protected_globs.txt`, in `secret_globs.txt`'s format, read by one
loader in `paths.py` that the sandbox also calls. The rule for membership is: a host program
acts on the file without a person running anything, before a diff is read.

    .claude/**   .vscode/**   .idea/**   .mcp.json   CLAUDE.md   CLAUDE.local.md

Considered and left out: `.envrc`, because direnv already refuses a changed file until it is
allowed; `.pre-commit-config.yaml` and `.github/**`, which act at a commit or a push, the
moment a diff is being read anyway.

**Two layers, independent**, as `paths.py` and `sandbox.py` always are:

- *The write tools* refuse a protected path by name, in a layer after the roots check that
  runs only on a write. Matching the name rather than the inode means a path that does not
  exist yet is refused too.
- *The sandbox* binds each existing protected path read-only onto itself, after the workdir
  bind and before the secret shadows, so a secret inside `.claude/` is still hidden.

**A missing path is the hard case.** Measured in bwrap 0.9.0 with a read-write bind of a scratch
directory:

| | Result |
|---|---|
| write inside a read-only bind in the read-write area | refused, EROFS |
| `mv` or `rmdir` of that bind | refused, EBUSY |
| create a protected file that did not exist | **allowed** |
| bind over a missing path | bwrap creates the mount point **on the host**: an empty directory, or an empty 0444 file |

So a missing path cannot simply be bound: a file placeholder is litter, untracked, and a
`CLAUDE.md` that Claude Code will read. Directories and files differ:

- **A missing protected directory** (the literal prefixes: `.claude`, `.vscode`, `.idea`) is
  created by the server before the command and bound read-only. Afterwards it is removed if it
  is still empty and the server created it. Git never shows an empty directory, so the
  placeholder is invisible while it exists.
- **A missing protected file** cannot be covered. The literal names at the workdir root
  (`CLAUDE.md`, `CLAUDE.local.md`, `.mcp.json`) are checked before and after the command;
  one that appeared is renamed, never deleted, to `<name>.delegate-refused` and listed in the
  result. A second walk to catch a nested one was rejected on cost: the walk measured 1.5s
  here and 5.8s over a project with `node_modules`, and would run twice per command. So a new
  *nested* `CLAUDE.md` is not caught; the write tools refuse it, and the next command binds it.

## M13.8 — host-side git

Every host-side git runs through `paths._git` or `tools._run_git`. `run_bash` can create a
nested repository the point-in-time `.git/**` shadow never saw, and the gitignore layer runs
`status` in it during an ordinary read. Measured in throwaway repositories, git 2.43:

| Planted in the repository's own config | Runs on | Stopped by |
|---|---|---|
| `core.fsmonitor` | `status`, `blame` | `-c core.fsmonitor=false` |
| textconv driver | `log -p`, `show`, `blame` | `--no-textconv` |
| clean filter driver | `diff`, `blame` | only `-c filter.<name>.clean=` |
| any of these, in a submodule | the top's `status` | `-c diff.ignoreSubmodules=all` |

**Flags cannot close this.** A driver's name is whatever the planted config chose, and
`--attr-source` does not help: `.git/info/attributes` is read regardless. So the control is
the configuration itself. Before either helper runs git in a repository, it reads that
repository's `local` and `worktree` scopes (`git config --list --show-scope`, which never
executes anything) and refuses if any key is off an allowlist of exact names:

- the ones git writes itself (`core.bare`, `core.filemode`, `core.repositoryformatversion`,
  and the like), plus `branch.*.merge`/`remote`, `remote.*.url`/`fetch`/`pushurl`, `user.*`;
- keys that only act on network operations, which host-side git never runs: `credential.*`,
  which this repository's own config carries.
- **Not** whole sections. `extensions.partialclone` makes a plain `show` fetch, and a fetch
  from a local path runs `remote.*.uploadpack`.

The operator's global config is not checked; it is theirs. It costs one more git process,
measured at 28 ms against 23 ms for the `rev-parse` it sits beside, and one per repository top
per call. The flags stay too: `-c core.fsmonitor=false -c diff.ignoreSubmodules=all` on both
helpers, and `--no-textconv`/`--no-ext-diff` where `read_git`'s subcommand takes them. Both
helpers take their environment from `paths.git_env()`, formerly `tools._git_env()`, which was
defined and never called, so
the review's "`GIT_ENV_DENY` already covers the environment half" described code that did not
run.

## M13.9 — provision

`provision` runs `pip install --editable`, and so the project's build backend, on the host.
The record gains a copy of each dependency declaration. When the hash differs, the change is
shown as a diff and the build needs `--yes`, which a caller without a terminal must pass
deliberately. Refusing an uncommitted declaration was rejected: it would add a third host-git
call site to harden, and a committed change is no safer to build blind.
