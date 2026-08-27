<!-- BUDGET: 190 -->
# Contributing

## Setup

One interpreter, and it is one CI runs: **Python 3.12, in WSL2**. A green run on a version
CI never tests proves less than it appears to, and WSL2 is where the server runs anyway
(ADR-0002) -- the only place `bubblewrap` and realistic filesystem speed exist. The venv sits
in the WSL home, not the repository: creating one on `/mnt/c` is far slower (ADR-0020).

```bash
wsl -d Ubuntu-24.04 -e bash -lc '
  python3 -m venv ~/.venvs/cdl
  ~/.venvs/cdl/bin/pip install -e "/mnt/c/path/to/repo[dev]"'

wsl -d Ubuntu-24.04 -e bash -lc 'cd /mnt/c/path/to/repo && ~/.venvs/cdl/bin/python -m pytest -q'
```

Add `-m "not integration"` to skip anything needing the cluster; that is what CI runs. Tests
also run from a bare clone with nothing but `pytest` -- `tests/conftest.py` puts `src/` on the
path, so a first `pytest` works before reading anything.

The commit hook is the exception: install it on Windows with `python
scripts/install_hooks.py`, because that is where git runs. It needs only the standard
library, and runs `scripts/docs_gate.py` -- one implementation, two callers, because a hook
reimplementing the checks would be a second copy destined to disagree. It installs a
**commit-msg** hook: git writes the message file only after a pre-commit hook returns, so a
gate there scans the *previous* commit's message and carries its waiver into this one.

## Commits

**One feature per commit: code, tests, documentation and CHANGELOG together.** Not
tidiness — it is what gives the owning-document check something to compare against. A
commit that changes `paths.py` without touching `docs/AGENTS.md` is blocked, and that only
means anything if features arrive whole.

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`,
`test:`, `refactor:`, `chore:`. Branches `feat/<slug>`, `fix/<slug>`, `docs/<slug>`.

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
file format.

<!-- GEN:AGENTS:START -->
<!-- Generated from .claude/agents/*.md by scripts/gen_agents_docs.py. Change the frontmatter, not this. -->

| Agent | Model | Effort | For |
|---|---|---|---|
| `code-reviewer` | sonnet | high | Reviews a diff for correctness and for regressions in this pr… |
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

- a document has not changed across **12+ commits that touched the code it owns**, and
- **60+ commits** have passed since the last file in `docs/audits/`.

Both are evidence rather than a calendar: a quiet month needs no audit, and a busy week
needs one whatever the date. Both warn rather than block, because blocking would force a
documentation edit to land unrelated work, which turns a signal into a rubber stamp.

Write findings to `docs/audits/YYYY-MM-DD-audit.md` and commit them — that file is what
resets the counter, so recording the audit and clearing the warning are the same act.

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

The budget has been raised three times — 130, 175, 190, 215 — and the sentence here
claiming twice was itself stale, which is the failure it was meant to warn about. It is
now back down to 190 by trimming rather than raising: the branch-protection reasoning went
to ADR-0026, and the documentation-strategy rules were dropped in favour of the link to
CLAUDE.md that was already there.

If it needs raising again, split instead: the operational half — CI and the agent
roster — moves to its own document owning `.github/**` and `.claude/agents/**`, leaving
the conventions here. That is a valid split under this project's own rules (distinct owned
code, reference separated from narrative), written down so nobody has to re-derive it
under pressure.

## Upstream

`fegone/claude-code-delegate-local` is the primary ancestor and what the `upstream` remote
points at; dormant since 2026-08-21. Its `mixicz/claude-code-delegate-local` fork is where
new work now appears. Fixes are read and reimplemented, never cherry-picked — ADR-0001 for
why and how often, ADR-0023 and ADR-0025 for the mechanism. Review through the API, not by
fetching: no upstream object lands here, and the push URL is disabled likewise. Findings go
in a dated `docs/reviews/` file, rejections included; `docs/audits/` is for audits only.
