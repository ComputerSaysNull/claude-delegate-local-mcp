<!-- BUDGET: 272      Raised from 262 on 2026-09-03: the commit hook's interpreter search, a fact about install_hooks.py
     and the reason committing from WSL never ran the gate at all.
     Raised from 249 on 2026-09-03: an agent body requiring what the
     sandbox cannot do is the inverse of the trap recorded directly above it, costs a turn
     on every invocation rather than merely going stale, and is the second sighting of the
     class. Recorded next to its twin so the pair is read together.
     Raised from 230 on 2026-09-01: a second agent format now lives
     in .claude/agents/ while the delegated audit route is proven, and a directory whose
     two readers each skip what they cannot parse needs that written down where someone
     editing an agent file will meet it. The note goes when the duplication does. Raised to the size it had already reached on 2026-09-01: the check that should have held this
     line was disabled from 2026-08-28, when reasons moved inside this comment and the pattern
     stopped matching, so the document grew unenforced. This records where it actually is rather than
     endorsing it; the 2026-09-01 audit tracks the trim. Raised from 210 to 220 on 2026-08-29: CI
     installs bubblewrap and cannot run it, which a contributor would otherwise learn by trusting a
     green tick over an unrun sandbox. -->
<!-- Raised from 190 on 2026-08-27: the audit-due section stopped restating two gate
     constants and had to say why, and the archive procedure the gate warns about was
     documented nowhere. -->
# Contributing

## Setup

One interpreter, and it is one CI runs: **Python 3.12, in WSL2**. A green run on a version
CI never tests proves less than it appears to, and WSL2 is where the server runs anyway
(ADR-0002) -- the only place `bubblewrap` and realistic filesystem speed exist. The venv sits
in the WSL home, not the repository: creating one on `/mnt/c` is far slower (ADR-0020).

```bash
wsl -d Ubuntu-24.04 -e bash -lc '
  python3 -m venv ~/.venvs/delegate
  ~/.venvs/delegate/bin/pip install -e "/mnt/c/path/to/repo[dev]"'

wsl -d Ubuntu-24.04 -e bash -lc 'cd /mnt/c/path/to/repo && ~/.venvs/delegate/bin/python -m pytest -q'
```

Add `-m "not integration"` to skip anything needing the cluster; that is what CI runs. Tests
also run from a bare clone with nothing but `pytest` -- `tests/conftest.py` puts `src/` on the
path, so a first `pytest` works before reading anything.

The commit hook is the exception: install it on Windows with `python
scripts/install_hooks.py`, because that is where git runs. It needs only the standard
library, and runs `scripts/docs_gate.py` -- one implementation, two callers, because a hook
reimplementing the checks would be a second copy destined to disagree. It installs a
**commit-msg** hook: git writes the message file only after a pre-commit hook returns, so a
gate there scans the *previous* commit's message and carries its waiver into this one. A
**prepare-commit-msg** hook records whether the message came from HEAD, which is how an
amend gets judged against its real parent -- see ADR-0028 for why that cannot simply be
detected, and why a pass relying on it announces itself.

The shim looks for an interpreter in four places — a POSIX venv, a Windows venv (only when
actually on Windows), `python3`, then `python` — and **refuses the commit saying the gate
did not run** if it finds none. It used to try the Windows venv and then bare `python`,
which Ubuntu does not have, so committing from inside WSL failed with
`exec: python: not found`: a hook that never ran, reported as though the hook were broken.
The Windows venv is gated on the platform because `/mnt/c` reports every file as
executable, so testing for one proves nothing there.

**Amend with the editor, not with `-m`.** Git tells that hook where a message came from,
and only an amend that *reuses* the existing one reports itself; `git commit --amend -m`
is byte-for-byte indistinguishable from an ordinary `git commit -m`, so the commit is
judged against HEAD and a commit that did update its owning document is blocked anyway.
`git commit --amend` on its own is detected *and* lets the message change, which is the
whole of what `--amend -m` was wanted for. The gate says this when it blocks, so nobody
has to remember it -- but a `Docs-Gate-Skip` spent on that block is spent on a limitation
rather than an exception, and one already was.

## Commits

**One feature per commit: code, tests, documentation and CHANGELOG together.** Not
tidiness — it is what gives the owning-document check something to compare against. A
commit that changes `paths.py` without touching `docs/AGENTS.md` is blocked, and that only
means anything if features arrive whole.

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`,
`test:`, `refactor:`, `chore:`. Branches `feat/<slug>`, `fix/<slug>`, `docs/<slug>`. The
gate holds this list and checks it on both the commit subject and the pull request title.

Write the **why** in the body, not just the what. The symptom that prompted the change,
the cause, and the fix. In six months the why is the only part still worth having.

`main` requires a pull request and green checks. Squash-merge, delete the branch.

## Which document do I update?

Ask; do not guess, and do not keep a mental copy:

```bash
python scripts/docs_gate.py --owner src/claude_delegate_local/paths.py
→ docs/AGENTS.md
```

`scripts/docs_ownership.toml` is the only copy of that mapping, and
[CLAUDE.md](CLAUDE.md) states the rules it enforces. Do not keep a second copy of either
here or in your head.

Documents also declare a size budget. Exceeding it blocks, and the block **never means
delete**: trim redundancy, split for a valid reason, or raise the budget with a one-line
reason in the same commit. ADR-0022 for what makes a split valid.

## Tests

Regression tests are named after the bug and live in `tests/regression/`. If you fix
something subtle, the test goes in the same commit.

**Negative-test every check**: assert it fires on a real violation, not merely that it
passes on clean input. Four checks here have already been found unable to fail — CLAUDE.md
lists them.

Mark anything needing the live cluster or a real `bwrap` as `@pytest.mark.integration`.

**Neither kind runs in CI, and the `bwrap` kind cannot**: on a runner it builds its user
namespace, then fails to configure loopback for want of `CAP_NET_ADMIN` (measured
2026-08-29, see JOURNAL). A green CI run is therefore no evidence about the sandbox. Change
`sandbox.py` and you must run the suite in WSL yourself before pushing.

## Decisions

Non-obvious choices get an ADR in `DECISIONS.md`, newest first, headings as the index.
Bodies are **never edited** — the reasoning at the time is the point. When a decision
stops being true only its heading changes: struck through and linked forward if wholly
replaced, or marked partially superseded if part still holds.

`JOURNAL.md` is for what cost real time to work out — the trap, the misleading error, the
thing you would have got wrong again in six months. Not a diary.

Security-sensitive work — `sandbox.py`, `paths.py`, `wsl.py` — gets a short design note in
`docs/specs/YYYY-MM-DD-<slug>.md` before the code. Nothing else needs one.

## CI

`.github/workflows/ci.yml` runs four jobs on every pull request — **gate**, **lint**,
**tests** (3.11 and 3.12), and **gitleaks**. `pyproject.toml` pins the ruff version *and*
the rule set: unpinned, findings went 3 to 45 across releases with no code change, and a
lint job on a moving rule set fails builds nobody broke. Workflow comments explain the rest.
The generated-document step now checks `docs/CONFIGURATION.md` alone (ADR-0044).

Two things that live outside it and are easy to miss:

- The email rule in `.gitleaks.toml` is an **allowlist** — a denylist would mean writing
  the addresses you want caught into a public file. Its entries are **generated** from
  `security/allowed_emails.txt` and `security/content_safe_emails.txt` by
  `scripts/gen_gitleaks_config.py`, so the gate and gitleaks cannot disagree. This used to
  read "keep them in step", which drifted within the hour.
- Those two lists are separate policies. Authors must be strict; file **content** also
  tolerates documentation placeholders and service accounts. A service address able to
  author commits would defeat the identity check.
- Host literals reach CI through a `FORBIDDEN_STRINGS` repository secret, never the repo.
- The gate job also scans the **pull request title and body**, via the Actions event
  payload. Those are written outside git, so no hook and no file check can see them — a
  specimen once reached a public pull request body while every other check passed. The
  scan runs after publication, so it is a backstop, not a gate: write the mechanism,
  never the specimen. GitHub retains edit history for pull request bodies, so an edited
  mistake is not an erased one.

## Branch protection

`main` is protected by a ruleset checked in as `.github/ruleset.json` — read it for the
rules, ADR-0026 for why the bypass list and the review count are set the way they are. In
practice: open a pull request, because direct pushes are refused for everyone, and expect
four required checks. Repository-level secret scanning and push protection are on as well,
catching what reaches GitHub even if a local hook was skipped.

## Build-time agents

`.claude/agents/` holds the subagents used while working *on* this repository. Not shipped,
and not the delegation agents in [docs/AGENTS.md](docs/AGENTS.md) — different thing, same
file format. A recipe inside one is run and believed, so it earns a check's scrutiny.

**Two formats live here at once, and that is temporary.** Most of these files are Claude
Code's format; `docs-audit-local` is this server's, for running the audit on the local model
through `delegate_to_agent`. **The two readers fail differently, and both failures are
quiet.** This server skips a file it cannot parse silently, so a malformed agent is simply
not there and `list_agents` returning nothing is what a typo looks like. Claude Code does
not skip this server's format at all: it loads the file and ignores frontmatter it does not
recognise, so `allowed_tools` goes unapplied and the named model is not the one that runs —
the worse of the two, because the agent appears to work. Confirm an edit against the
consumer you meant; neither is evidence the other is happy.

Say which one you want when asking for an audit. The duplication ends when the local route
is dependable enough to be the only one; the copy is then deleted, not left to rot.

**A body can work around a server limitation, and nothing links them.** `docs-audit-local`
cited by quotation because `read_file` had no line numbers; adding them made that stale.

**It can also require what the sandbox cannot do — the same fault inverted, and costlier.**
That body opened with "run the gate first", which cannot succeed: `.git` is under a tmpfs
and `security/secret_globs.txt` under `/dev/null`, both because they match the secret
denylist, so git exits 128 and the gate dies on a `PermissionError`. A stale workaround
wastes its own instruction; an impossible requirement burns a turn on every invocation and
has the model report the sandbox working correctly as a fault. Check a command in a body
against that agent's `allowed_tools` *and* against what the sandbox binds, and say in the
body where the caller must supply what the agent cannot fetch.

<!-- GEN:AGENTS:START -->
<!-- Generated from .claude/agents/*.md by scripts/gen_agents_docs.py. Change the frontmatter, not this. -->

| Agent | Model | Effort | For |
|---|---|---|---|
| `code-reviewer` | sonnet | high | Reviews a diff for correctness and for regressions in this pr… |
| `docs-audit-local` | deepseek-v4-flash | high | The delegated documentation audit, in this server's own agent… |
| `docs-audit` | haiku | medium | Audits documentation for staleness, verbosity, misplaced fact… |
| `researcher` | haiku | low | Read-only exploration of this repository |
| `test-writer` | sonnet | medium | Writes and extends pytest tests for this repository |

<!-- GEN:AGENTS:END -->

Model and effort follow task cost: the cheapest tier that can do the job. The frontmatter
key is `effort`, not `reasoning_effort` — a misspelling is ignored in silence and bills the
default tier, so the table renders a missing key rather than guessing one. Run at most five
agents concurrently; CI enforces it via `max-parallel`.

### When to run docs-audit

Not on a schedule, and not "when it feels off". The gate raises `audit-due` on two
signals it computes from git:

- a document has not changed across enough commits that touched the code it owns, and
- enough commits have passed since the last file in `docs/audits/`.

Both thresholds are constants at the top of `scripts/docs_gate.py`. They are deliberately
not repeated here: one of them was lowered on 2026-08-27 and the prose copies in this file
and in `PLAN.md` went on claiming the old value, which is the drift this whole scheme
exists to stop.

Both are evidence rather than a calendar: a quiet month needs no audit, and a busy week
needs one whatever the date. Both warn rather than block, because blocking would force a
documentation edit to land unrelated work, which turns a signal into a rubber stamp.

Write findings to `docs/audits/YYYY-MM-DD-audit.md` and commit them — that file is what
resets the counter, so recording the audit and clearing the warning are the same act. More
than one audit on a date takes a `-2` suffix; a later audit supplements its predecessors
rather than replacing them, since each is a dated record of what was true when it ran.

### Archiving an append-only document

Archiving is a judgement, not a threshold: no check asks for it or warns that it is due —
one did, and named a remedy nobody could apply (ADR-0033). Move the older entries — never
trim them — into `archive/<document>-<period>.md` beside the original, where the period is
whatever actually divides them: a year, a month, or the format they were written in.
`check_budgets` skips any path containing an `archive` component, so the archived file is
not budgeted and the original keeps its header.

There is deliberately **no scheduled workflow**. It would need an API key, and a key sitting
in CI is standing billing exposure for a job that fires whether or not anything changed.
Running the agent locally uses the Claude Code subscription instead.

## Secrets

The host is configuration, never a literal. The gate blocks addresses, private-network hostnames and
`host:port` shapes in tracked files, checks every commit's author against
`security/allowed_emails.txt`, and refuses email addresses not on that list. Put your
host's literal names in `security/forbidden_strings.txt`, which is untracked on purpose —
a committed file listing what must not be committed is itself the leak.

## When this document splits

Raised, trimmed, raised again. The count that stood here went stale three times — the
failure this section warns about — so it is gone rather than corrected. The header records
every raise with its cause, and ADR-0026 holds the 215→190 trim's branch-protection move.

If it needs raising again, split instead: the operational half — CI and the agent roster —
moves to its own document owning `.github/**` and `.claude/agents/**`, leaving the
conventions here. Valid under this project's rules, and written down so nobody re-derives it.

## Upstream

`fegone/claude-code-delegate-local` is the primary ancestor and what the `upstream` remote
points at; dormant since 2026-08-21. Its `mixicz/claude-code-delegate-local` fork is where
new work now appears. Fixes are read and reimplemented, never cherry-picked — ADR-0001 for
why and how often, ADR-0023 and ADR-0025 for the mechanism. Review through the API, not by
fetching: no upstream object lands here, and the push URL is disabled likewise. Findings go
in a dated `docs/reviews/` file, rejections included; `docs/audits/` is for audits only.
