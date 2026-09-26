<!-- BUDGET: 158 -->
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
    python scripts/gen_agents_docs.py               # after touching either agents directory
    python scripts/gen_tools_docs.py                # after touching tools.py
    python scripts/plan_stack.py                    # the stack, and the commands to ship it
    python -m pytest -q                             # parallel by default, `-n 0` for serial

`bwrap` needs Linux or WSL. Your own machine's paths go in the gitignored `CLAUDE.local.md`.

## Documentation ownership

Every file under `src/`, `scripts/`, `.github/` and `.claude/` has exactly one owning
document, or is explicitly declared unowned. **Changing the code means updating its owning document in the
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
- **Generated documents are never hand-edited.** `docs/CONFIGURATION.md` and
  `docs/TOOLS.md` are rendered from code. Edit the source, run the generator.
- Blocked by the gate and genuinely right anyway? Add a trailer:
  `Docs-Gate-Skip: owning-doc -- pure rename, no behaviour change`. It is echoed in every
  run and audited, so it is visible rather than quiet. Two skips on one document in ninety
  days is a signal that something is wrong with the document, not with the rule.

## Invariants

- **Config defaults live only in `config.py`.** Never in a docstring, a README, a comment
  in another module, or a test. The reference table is generated from the dataclass, and
  the gate fails when they disagree.
- **The model-facing contract has several homes, and a description is the smallest.** An
  argument's meaning belongs in `inputSchema`, a remedy in the refusal that raises it, the
  long form in a resource. Why a description cannot hold a contract, and what each home is
  delivered by, is `docs/ARCHITECTURE.md`'s. The trap here is a fact in the wrong home:
  changing any of them is a behaviour change, not a wording fix. And a prompt is pulled by a
  *person* where a resource is pulled by the *model* — never interchangeable. (ADR-0066)
- **The system prompt must be static, byte for byte.** No timestamp, session id, turn
  number or counter; dynamic content goes in the tail, inside tool results. What that buys
  and how breaking it stays silent is `docs/ARCHITECTURE.md`'s. (ADR-0011)
- **`paths.py` and `sandbox.py` are independent layers, not redundant ones.** Which tool
  each one governs is `docs/ARCHITECTURE.md`'s. A bug in one is not covered by the other, and
  the sandbox reading the same denylist does not change that: it covers up matches inside what
  it binds, point-in-time, while a command holds a read-write bind for its whole run. Treating
  either as a backstop for the other is the trap. (ADR-0010, ADR-0035)
- **Validating a path and opening it are one operation.** `open_resolved` is the only
  sanctioned way to open what `paths.py` approved: it returns a handle, not a path, so
  there is no string left for a handler to reopen. A second `open` on a `.posix` puts the
  check-then-use gap back. What that proves, and what it deliberately does *not* treat as a
  breach, is `docs/AGENTS.md`'s. (ADR-0049)
- **`allowed_tools` is enforced at two sites**, `declared_tools` and `execute_tool` in
  `tools.py`, and neither trusts the other — why that is necessary is `docs/AGENTS.md`'s. The
  trap here is a maintenance one: change either site alone and enforcement goes back to being
  asymmetric, silently. `WITHHELD_TOOL_NAMES` is empty and kept anyway, so remember
  what it never was: it narrows only what is *declared*, never a substitute for the check.
- **Trust server-captured exit codes, never the model's account of them.** `bash_failures`
  and `last_bash_exit` come from real process exits and may contradict the model's final
  text. Why the design rests on this is `docs/ARCHITECTURE.md`'s. (ADR-0007)
- **Never trust a cached compile in a tool that compares an artefact against source.**
  Python validates a `.pyc` on `(mtime, size)`, so a same-length edit inside one timestamp
  tick is invisible. Set `sys.pycache_prefix` to a fresh temp directory. `-B` does *not*
  help — it stops writing bytecode, not reading a stale cache. (JOURNAL 2026-08-25)
- **A check that cannot fail is worse than no check**, because it is trusted. The shapes
  found here (JOURNAL has each one): a check that finds its own needle — the reference it
  validates, the pattern list that defines it, a fixture named after the pattern it tests; a
  check that reads stale state — cached bytecode, the previous commit's message; and a check
  that reads the wrong copy — `list_tools()` rather than the wire the client truncates.
  Negative-test every check — assert it fires on a real violation, not merely that it passes.
  The rule applies to the tests as much as to the checks.
- **Red before green, in that order.** The test is written first and run against the unfixed
  code, and its failure output is kept: a test that passes before the fix is blind and gets
  rewritten rather than trusted. Ordering is what makes the rule above checkable instead of
  merely believed, so the PR body states that result.
- **Verify network isolation by address, never by hostname.** A hostname-only test passes on
  broken DNS and reports a tight sandbox either way. (ADR-0021; mechanism in `docs/AGENTS.md`.)

## Environment

- The server is POSIX-only. From a Windows client, paths cross into WSL in one place,
  `wsl.py` — what it converts and refuses is `docs/ARCHITECTURE.md`'s.
- A checkout on a Windows drive seen from WSL is an order of magnitude slower. Accepted.
  (ADR-0020)
- **Never run two interpreters' suites against one checkout at once.** They share
  `__pycache__`, and it surfaces as an ImportError in an unrelated module.
- `bwrap` needs `--symlink usr/lib64 /lib64`; without it nothing dynamically linked runs
  and the error blames the executable rather than the missing loader. (ADR-0021)
- The head node is configuration. Never a literal in code, docs, tests, a commit message
  or a pull request: addresses, private-network hostnames and `host:port` shapes are
  blocked on all five. **By two scanners over shared primitives, not by one scanner.**
  `check_host_identifiers` reads files; `scan_text` reads the commit message and a pull
  request title and body. Extending either alone covers half of what this rule promises.
  The primitives both call — the pattern table, the `host:port` predicate, the literal
  splitter — are the place a new rule belongs, and the only place one edit reaches both.
- **Cite a source line as the file name, the word `line`, then the number.** Joining the
  two with a colon reads as a host and a port once the number reaches four digits, and is
  refused — this rule cannot show you the shape it forbids, which is the point. Do not fix
  it by exempting names ending in a source suffix: `.py` is a live TLD, so the exemption
  would admit the exact shape the predicate exists to catch.
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
  leaves half of what reaches `main` unchecked.
- **Agent and skill files have two readers**, `agents.py` and real YAML, and ours is the more
  permissive: `list_agents` accepting a file proves nothing. Quote any value containing `: `.

## Conventions

- Plan a session with the `session-plan` skill; execute it with `session-execute`, which is
  invoked when a plan is **approved**, not when it is written.
- One feature per commit: code, tests, docs and CHANGELOG together. This is what gives the
  owning-document check something to compare against.
- Conventional Commits. Branches `feat/`, `fix/`, `docs/`.
- Regression tests are named after the bug and live in `tests/regression/`.
- CHANGELOG entries carry the **why** — the symptom, the cause, the fix.
- A blocking `BUDGET:` means trim your own additions first; cutting others' prose is audit
  work. What a comment, an ADR or a budget raise may hold is CONTRIBUTING's "Prose" section.
- ADR bodies are never edited. A superseded decision changes only its heading.
- Upstream fixes are read and reimplemented, not cherry-picked. This is a rewrite.
  (ADR-0001)
