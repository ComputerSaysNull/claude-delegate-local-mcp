<!-- BUDGET: 215 -->
# Contributing

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # .venv/Scripts/pip on Windows
python scripts/install_hooks.py
```

The hook runs `scripts/docs_gate.py`, which is also what CI runs — one implementation, two
callers. A hook that reimplemented the checks would be a second copy destined to disagree
with the first.

Tests run from a bare clone with nothing installed but `pytest`; `tests/conftest.py` puts
`src/` on the path so a first `pytest` works before you have read anything.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q -m "not integration"   # skip anything needing the cluster
```

Work that needs `bubblewrap` or realistic filesystem speed runs in WSL2 on Windows:

```bash
wsl -d Ubuntu-24.04 -e bash -lc 'cd /mnt/c/path/to/repo && python3 -m pytest -q'
```

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

`scripts/docs_ownership.toml` is the only copy of that mapping.
[CLAUDE.md](CLAUDE.md) has the full set of rules; the short version:

- A fact belongs to one document, in one plane. Project plane is the repo root; product
  plane is `docs/`.
- Generated documents are never hand-edited. Change the source, run the generator.
- `docs/TROUBLESHOOTING.md` owns no facts. It links.
- Blocked but right anyway? `Docs-Gate-Skip: owning-doc -- <reason>` in the commit
  message. Visible in every run, and audited.

### Size budgets

Documents declare a budget. Exceeding it blocks, and the block **never means delete** —
three ways out: trim real redundancy, split for a valid reason, or raise the budget with a
one-line reason in the same commit.

A split is valid only for a *different audience*, *different owned code*, or *reference
separated from narrative*. "It got long" is a reason to raise a budget. The gate refuses a
new document whose audience and owned code are both subsets of its parent's.

Append-only documents cap each **entry** instead of the total, because their history never
stops earning its place. See ADR-0022.

## Tests

Regression tests are named after the bug and live in `tests/regression/`. If you fix
something subtle, the test goes in the same commit.

**Negative-test every check.** Assert that it fires on a real violation, not merely that
it passes on clean input. Three checks here have already been found unable to fail:
one searched a file for the reference it was validating, one read stale bytecode, one
flagged the pattern list that defined it. A check that cannot fail is worse than no check,
because it is believed.

Mark anything needing the live cluster or a real `bwrap` as
`@pytest.mark.integration`; it is skipped by default.

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

`.github/workflows/ci.yml` runs three jobs on every pull request — **gate**, **tests**
(3.11 and 3.12), and **gitleaks**. The workflow comments explain why each is shaped as it
is; they are not repeated here.

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

## Branch protection

`main` is protected by a ruleset, checked in as `.github/ruleset.json` so the configuration
is reproducible rather than a thing someone once clicked:

```bash
gh api -X POST repos/OWNER/REPO/rulesets --input .github/ruleset.json
```

- Pull request required. **Direct pushes to `main` are refused for everyone, including the
  owner** — `bypass_actors` is empty, which rulesets allow and classic branch protection
  did not.
- Four required checks: the gate, tests on 3.11 and 3.12, and the secret scan.
- Force-push and deletion blocked. Squash is the only merge method.
- `required_approving_review_count` is **0**, deliberately. GitHub will not let you
  approve your own pull request, so requiring one review would lock a solo maintainer out
  of their own repository entirely. The checks carry the weight instead. Raise it to 1 the
  day a second person joins.

Secret scanning and push protection are enabled at the repository level, which is free on
public repositories and catches what reaches GitHub even if the local hook was skipped.

The `upstream` remote is configured with a disabled push URL. It exists for reading, and a
working push URL to someone else's repository is an accident waiting to happen.

## Build-time agents

`.claude/agents/` holds four subagents used while working *on* this repository. They are
not shipped, and they are not the delegation agents described in
[docs/AGENTS.md](docs/AGENTS.md) — different thing, same file format.

| Agent | Model | Effort | For |
|---|---|---|---|
| `researcher` | haiku | low | Read-only exploration. Retrieval, no judgement |
| `docs-audit` | haiku | medium | Staleness and misplaced facts the gate cannot judge |
| `test-writer` | sonnet | medium | Tests, especially negative cases |
| `code-reviewer` | sonnet | high | Diffs, against this project's security invariants |

Model and effort are set per task cost: the cheapest tier that can do the job. Retrieval
and comparison get haiku; anything where being wrong is expensive gets sonnet and higher
effort. The frontmatter key is `effort`, not `reasoning_effort` — a misspelling is ignored
in silence and quietly bills the default tier.

Run at most five agents concurrently. Enforced in CI via `max-parallel`; elsewhere it is a
working rule.

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

Its budget has been raised twice. One more raise and it splits: the operational half —
CI, branch protection and the agent roster — moves to its own document owning `.github/**`
and `.claude/agents/**`, leaving the conventions here.

That is a valid split under this project's own rules (reference material separated from
narrative, and distinct owned code), and it is written down so the next person does not
have to re-derive it under pressure.

## Upstream

`git remote add upstream https://github.com/fegone/claude-code-delegate-local` — for
*reading*. This is a rewrite, so their fixes are read and reimplemented rather than
cherry-picked, and candidates are tracked in a pinned issue. See ADR-0001.
