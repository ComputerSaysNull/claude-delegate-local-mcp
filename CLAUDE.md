<!-- BUDGET: 135 -->
# CLAUDE.md

Traps and invariants for anyone — human or agent — editing this repository. Terse on
purpose: it assumes you can read code, and it exists to stop mistakes that are cheap to
make and expensive to find.

Not a summary of `docs/ARCHITECTURE.md`. That explains how the system works and why.
This lists what will bite you.

## Commands

    python scripts/docs_gate.py --mode pre-commit   # structural checks, by hand
    python scripts/install_hooks.py                 # installs the commit-msg hook
    python scripts/docs_gate.py --owner <path>      # which document owns this file
    python scripts/gen_config_docs.py               # after touching config.py
    python scripts/gen_agents_docs.py               # after touching .claude/agents/
    python scripts/gen_status.py                    # after touching PLAN.md
    .venv/Scripts/python.exe -m pytest -q           # Windows
    wsl -d Ubuntu-24.04 -e bash -lc '...'           # anything needing bwrap or ext4

## Documentation ownership

Every file under `src/`, `scripts/` and `.github/` has exactly one owning document, or is
explicitly declared unowned. **Changing the code means updating its owning document in the
same commit.** The gate blocks otherwise.

Do not memorise the mapping and do not copy it here — `scripts/docs_ownership.toml` is the
only copy, and a second one is the drift this whole scheme exists to prevent. Ask instead:

    python scripts/docs_gate.py --owner src/claude_delegate_local/paths.py
    → docs/AGENTS.md

Rules a machine cannot check, so they land here:

- **A fact belongs to exactly one document, and to exactly one plane.** Project plane is
  the repo root (where are we, why did we choose this). Product plane is `docs/` (how does
  it work). If you find yourself explaining a config default in prose, stop — link.
- **`docs/TROUBLESHOOTING.md` owns zero facts.** It is a symptom index. It links to
  whichever document owns the answer and never restates it. Restating a default inside a
  symptom explanation is exactly how the project this descends from ended up documenting
  one setting three different ways.
- **Generated documents are never hand-edited.** `docs/CONFIGURATION.md`,
  `docs/TOOLS.md` and `STATUS.md` are rendered from code or from `PLAN.md`. Edit the
  source, run the generator.
- Blocked by the gate and genuinely right anyway? Add a trailer:
  `Docs-Gate-Skip: owning-doc -- pure rename, no behaviour change`. It is echoed in every
  run and audited, so it is visible rather than quiet. Two skips on one document in ninety
  days is a signal that something is wrong with the document, not with the rule.

## Invariants

- **Config defaults live only in `config.py`.** Never in a docstring, a README, a comment
  in another module, or a test. The reference table is generated from the dataclass, and
  the gate fails when they disagree.
- **The MCP tool descriptions are the model-facing contract.** Rewording one changes
  runtime behaviour. Treat it as a behaviour change with a CHANGELOG entry, not as a
  wording fix.
- **The system prompt must be static, byte for byte.** No timestamp, session id, turn
  number or counter. The cluster caches prefixes, so one dynamic byte silently disables
  that with no error and no symptom beyond slower prefill. Dynamic content goes in the
  tail, inside tool results. (ADR-0011)
- **`paths.py` and `sandbox.py` will be independent layers, not redundant ones.** The path
  policy governs `read_file` and `write_file`, which run in the server process. Only
  `run_bash` enters the sandbox. A bug in one is not covered by the other. `sandbox.py` is
  M5 and unwritten: this is a trap pre-registered for whoever writes it. Until it exists
  `run_bash` refuses every call *and* is withheld from declaration, so writing `sandbox.py`
  makes live a route nothing exercises end to end today. (ADR-0010)
- **`allowed_tools` is enforced at two sites**, `declared_tools` and `execute_tool` in
  `tools.py`, and neither trusts the other. Filtering only the declared list is advisory,
  because a model can call a tool it was never offered. Both exist now, which makes the
  trap a maintenance one rather than a build one: change either site alone and enforcement
  goes back to being asymmetric, silently. Withholding a tool -- as `run_bash` is while the
  sandbox is unbuilt -- narrows only what is *declared*, and is never a substitute for the
  execution check.
- **Trust server-captured exit codes, never the model's account of them.** `bash_failures`
  and `last_bash_exit` come from real process exits and may contradict the model's final
  text. The whole self-verification design rests on this. (ADR-0007)
- **Never trust a cached compile in a tool that compares an artefact against source.**
  Python validates a `.pyc` on `(mtime, size)`, so a same-length edit inside one timestamp
  tick is invisible. Set `sys.pycache_prefix` to a fresh temp directory. `-B` does *not*
  help — it stops writing bytecode, not reading a stale cache. (JOURNAL 2026-08-25)
- **A check that cannot fail is worse than no check**, because it is trusted. Four have
  been found here already: one searching a file for the reference it was validating, one
  reading stale bytecode, one flagging the pattern list that defined it, and one scanning
  the previous commit's message because git had not written the new one yet. Negative-test
  every check — assert that it fires on a real violation, not merely that it passes. Two
  tests written *for* that fourth one passed against the bug before they were rewritten,
  so the rule applies to the tests as much as to the checks.
- **Verify network isolation by address, never by hostname.** A hostname request fails
  whether or not the network namespace is isolated, so a hostname-only test reports a
  tight sandbox that may just have broken DNS. (ADR-0021)

## Environment

- Claude Code runs on Windows; the server runs in **WSL2 Ubuntu 24.04**. Paths cross that
  boundary in exactly one place, `wsl.py`, and everything inside it is POSIX-only.
- The workspace lives on `/mnt/c`, which is roughly 12x slower for a test run and ~27x for
  creating a virtualenv. Expected, measured, and accepted. (ADR-0020)
- `bwrap` needs `--symlink usr/lib64 /lib64`; without it nothing dynamically linked runs
  and the error blames the executable rather than the missing loader. (ADR-0021)
- The head node is configuration. Never a literal in code, docs, tests or a commit
  message — the gate blocks addresses, private-network hostnames and `host:port` shapes
  in every tracked non-binary file **and in the commit message itself**.
- Local literals in `security/forbidden_strings.txt` match case-insensitively and as
  substrings. A multi-word entry matches only the contiguous phrase, so **list the
  distinctive parts separately too** — a full name alone will not catch a surname
  written on its own. For a part that is also ordinary vocabulary, prefix it
  `word:` for case-sensitive whole-word matching. Never write a real personal
  identifier into an example, a commit message, or a pull request body — use a
  fictional one, and describe the mechanism rather than the specimen.

- **A pull request title and body are a public surface that no hook can gate.** They are
  written outside git, so no commit hook can see them and CI only sees them after they are
  already published. For a *leak* that makes CI a backstop rather than a gate — the damage
  is done on publish. Write the **mechanism, never the specimen** — "a fragment that
  collides with ordinary vocabulary", not the fragment itself. The same applies to issue
  comments.
- **The title's shape, unlike its contents, is blocked outright**, because a malformed
  title is repaired by editing it. Conventional Commits is checked on the pull request
  title *and* on every commit subject: a squash takes its subject from the title for a
  multi-commit branch and from the commit for a single one, so guarding either alone
  leaves half of what reaches `main` unchecked. Both were unchecked until two `M1:`-style
  subjects and five such titles had already landed.

## Conventions

- One feature per commit: code, tests, docs and CHANGELOG together. This is what gives the
  owning-document check something to compare against.
- Conventional Commits. Branches `feat/`, `fix/`, `docs/`.
- Regression tests are named after the bug and live in `tests/regression/`.
- CHANGELOG entries carry the **why** — the symptom, the cause, the fix.
- ADR bodies are never edited. A superseded decision changes only its heading.
- Upstream fixes are read and reimplemented, not cherry-picked. This is a rewrite.
  (ADR-0001)
