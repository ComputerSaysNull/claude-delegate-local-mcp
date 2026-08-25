<!-- BUDGET: 130 -->
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

## Secrets

The host is configuration, never a literal. The gate blocks addresses, tailnet names and
`host:port` shapes in tracked files, checks every commit's author against
`security/allowed_emails.txt`, and refuses email addresses not on that list. Put your
host's literal names in `security/forbidden_strings.txt`, which is untracked on purpose —
a committed file listing what must not be committed is itself the leak.

## Upstream

`git remote add upstream https://github.com/fegone/claude-code-delegate-local` — for
*reading*. This is a rewrite, so their fixes are read and reimplemented rather than
cherry-picked, and candidates are tracked in a pinned issue. See ADR-0001.
