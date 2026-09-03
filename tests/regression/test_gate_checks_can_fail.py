"""Six gate checks that had never been shown to fire.

The second audit of 2026-08-27 found that `check_budgets`, `check_orphan_docs`,
`check_manifest_docs_exist`, `check_split_dodge`, `check_secret_paths` and `check_pr_text`
had no negative test. `test_gate_self_defeating_checks.py` covers the four checks that were
already caught validating nothing, by name; its docstring says both were found "by
negative-testing the gate rather than by reading it", which is exactly what these six had
never had. Two of them -- `check_pr_text` and `check_secret_paths` -- guard the surface
CLAUDE.md says no hook can gate.

Every check here is asserted in BOTH directions: that it fires on a real violation, and
that it stays silent on the closest thing that is not one. One direction alone is not a
test. A check that always fires passes a fires-on-violation test, and a check that never
fires passes a silent-on-clean test; only the pair distinguishes a working check from
either broken one.

`check_secret_paths` had exactly that half-test before this file: one case asserting it
does *not* flag the policy list. Nothing asserted it flags anything.

Two later checks join them here for the same reason. `check_env_example` and
`check_scan_coverage` were both written *because* something had been passing
unseen -- a deleted setting still advertised, and files the content scanners
declined to open -- so shipping either without a negative test would have
repeated the mistake that created them.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"

# Fictional, and assembled at runtime, for the same reason test_forbidden_matching.py does
# it: a test proving a secret scanner works must not itself contain the string the scanner
# looks for, and exempting the test file would be a hole rather than a fix.
FORBIDDEN_LITERAL = "host-" + "zeta"

MANIFEST = """\
[docs."docs/PARENT.md"]
audience = ["contributor"]
plane = "product"
owns = ["src/real.py"]
covers_not = "Nothing."

[unowned]
paths = ["tests/**", "scripts/**", "security/**", "src/other.py"]
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repository the gate can be pointed at.

    The gate resolves ROOT from its own location, so copying the script into a temp tree
    makes that tree the repository under test. Nothing here touches the real one.
    """
    r = tmp_path / "r"
    (r / "scripts").mkdir(parents=True)
    (r / "security").mkdir()
    (r / "docs").mkdir()
    (r / "src").mkdir()
    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    (r / "scripts" / "docs_ownership.toml").write_text(MANIFEST, encoding="utf-8")
    for name, body in (
        ("allowed_emails.txt", "t@example.com\n"),
        ("secret_globs.txt", ".env\nid_rsa\n"),
        ("content_safe_emails.txt", "t@example.com\n"),
        ("forbidden_strings.txt", "# local\n" + FORBIDDEN_LITERAL + "\n"),
    ):
        (r / "security" / name).write_text(body, encoding="utf-8")
    (r / "docs" / "PARENT.md").write_text("<!-- BUDGET: 99 -->\n# Parent\n", encoding="utf-8")
    (r / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True)
    return r


def gate(repo: Path, *args: str) -> list[str]:
    """Run the gate in the temp repo and return its report lines."""
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    proc = subprocess.run(
        [sys.executable, "scripts/docs_gate.py", *(args or ("--mode", "pre-commit"))],
        cwd=repo, capture_output=True, text=True,
    )
    return proc.stdout.splitlines()


def fired(lines: list[str], check: str, *needles: str) -> bool:
    """True when `check` reported a BLOCK or WARN mentioning every needle."""
    return any(
        f"[{check}]" in ln
        and ("BLOCK" in ln or "WARN" in ln)
        and all(n in ln for n in needles)
        for ln in lines
    )


# --------------------------------------------------------------------------- budgets

def test_budget_fires_when_a_document_is_over_its_cap(repo: Path):
    (repo / "docs" / "big.md").write_text(
        "<!-- BUDGET: 5 -->\n" + "line\n" * 40, encoding="utf-8")
    assert fired(gate(repo), "budget", "docs/big.md", "against a budget of 5")


def test_budget_is_silent_when_the_document_fits(repo: Path):
    """The other direction. A check that blocks every document would pass the test above."""
    (repo / "docs" / "small.md").write_text(
        "<!-- BUDGET: 50 -->\n" + "line\n" * 4, encoding="utf-8")
    assert not fired(gate(repo), "budget", "docs/small.md")


def test_per_entry_budget_fires_on_one_long_section(repo: Path):
    (repo / "docs" / "log.md").write_text(
        "<!-- BUDGET-PER-ENTRY: 3 -->\n## short\nx\n## long\n" + "y\n" * 30,
        encoding="utf-8")
    assert fired(gate(repo), "budget", "docs/log.md", "'long'")


def test_per_entry_budget_is_silent_on_short_sections(repo: Path):
    (repo / "docs" / "log2.md").write_text(
        "<!-- BUDGET-PER-ENTRY: 30 -->\n## a\nx\n## b\ny\n", encoding="utf-8")
    assert not fired(gate(repo), "budget", "docs/log2.md")


def test_archive_threshold_is_gone_and_says_nothing(repo: Path):
    """ARCHIVE-AT was removed (ADR-0033): a stale marker must be inert, not honoured.

    It warned past a line count and pointed at a by-year split that a single-year document
    could not perform, so it fired on every commit with no way to answer it. A marker left
    behind in some old file must now do nothing at all -- silently still warning would be
    the instrument surviving its own removal.
    """
    (repo / "docs" / "hist.md").write_text(
        "<!-- ARCHIVE-AT: 5 -->\n" + "line\n" * 40, encoding="utf-8")
    lines = gate(repo)
    assert not fired(lines, "budget", "docs/hist.md")


def test_a_per_entry_budget_fires_on_the_changelog_shape(repo: Path):
    """The shape ADR-0033 adopted: one `## ` section per pull request.

    The 2026-08-27 audit withdrew per-entry budgeting for CHANGELOG.md because its entries
    were bullets under one section, so the cap read the whole file as a single entry.
    Sections per pull request are what make the existing check apply, and this asserts it
    really does -- and that a short section beside a long one is left alone.
    """
    long_entry = "\n".join(f"- line {i}" for i in range(40))
    (repo / "docs" / "cl.md").write_text(
        "<!-- BUDGET-PER-ENTRY: 30 -->\n"
        "## #2 -- second\n### Added\n- short\n"
        f"## #1 -- first\n### Added\n{long_entry}\n",
        encoding="utf-8")
    lines = gate(repo)
    assert fired(lines, "budget", "docs/cl.md", "#1 -- first")
    assert not any("#2 -- second" in ln for ln in lines)


# ---------------------------------------------------------------------- orphan docs

def test_orphan_doc_fires_when_owned_code_does_not_exist(repo: Path):
    (repo / "src" / "real.py").unlink()
    assert fired(gate(repo), "orphan-doc", "docs/PARENT.md")


def test_orphan_doc_is_silent_when_the_owned_code_is_tracked(repo: Path):
    assert not fired(gate(repo), "orphan-doc", "docs/PARENT.md")


# -------------------------------------------------------------- manifest documents

def test_manifest_fires_when_a_listed_document_is_absent(repo: Path):
    (repo / "docs" / "PARENT.md").unlink()
    assert fired(gate(repo), "manifest", "docs/PARENT.md", "does not exist")


def test_manifest_is_silent_when_the_document_is_present(repo: Path):
    assert not fired(gate(repo), "manifest", "does not exist")


# ------------------------------------------------------------------- the split dodge

def _add_child(repo: Path, *, audience: str, owns: str) -> None:
    """Register and stage a second document, as someone evading a budget would."""
    manifest = repo / "scripts" / "docs_ownership.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "[unowned]",
            textwrap.dedent(f"""\
                [docs."docs/CHILD.md"]
                audience = [{audience}]
                plane = "product"
                owns = [{owns}]
                covers_not = "Nothing."

                [unowned]"""),
        ),
        encoding="utf-8",
    )
    (repo / "docs" / "CHILD.md").write_text("<!-- BUDGET: 99 -->\n# Child\n", encoding="utf-8")


def test_split_dodge_fires_on_a_subset_of_an_existing_document(repo: Path):
    _add_child(repo, audience='"contributor"', owns='"src/real.py"')
    assert fired(gate(repo), "split-dodge", "docs/CHILD.md", "docs/PARENT.md")


def test_split_dodge_is_silent_when_the_audience_is_distinct(repo: Path):
    """A real split. Same owned code, different reader -- allowed by ADR-0003."""
    _add_child(repo, audience='"user"', owns='"src/real.py"')
    assert not fired(gate(repo), "split-dodge", "docs/CHILD.md")


# ------------------------------------------------------------------- secret paths

def test_secret_path_fires_on_a_tracked_file_matching_a_glob(repo: Path):
    """The direction that had never been asserted. `.env` is in the fixture's glob list."""
    (repo / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    assert fired(gate(repo), "secret-path", ".env")


def test_secret_path_is_silent_on_the_example_file(repo: Path):
    """`.env.example` matches nothing and must stay committable."""
    (repo / ".env.example").write_text("TOKEN=\n", encoding="utf-8")
    assert not fired(gate(repo), "secret-path", ".env.example")


def test_secret_path_fires_on_a_match_by_basename_in_a_subdirectory(repo: Path):
    """The glob is matched against the basename too, so nesting must not hide a key."""
    (repo / "src" / "id_rsa").write_text("key\n", encoding="utf-8")
    assert fired(gate(repo), "secret-path", "id_rsa")


# ----------------------------------------------------------------- pull request text

def _event(repo: Path, *, title: str, body: str) -> str:
    p = repo / "event.json"
    p.write_text(json.dumps({"pull_request": {"title": title, "body": body}}),
                 encoding="utf-8")
    return str(p)


def test_pr_title_is_scanned(repo: Path):
    ev = _event(repo, title=f"fix: crash on {FORBIDDEN_LITERAL}", body="clean")
    assert fired(gate(repo, "--mode", "ci", "--pr-event", ev), "public-text", "title")


def test_pr_body_is_scanned(repo: Path):
    ev = _event(repo, title="fix: a crash", body=f"reproduced on {FORBIDDEN_LITERAL}")
    assert fired(gate(repo, "--mode", "ci", "--pr-event", ev), "public-text", "body")


def test_pr_text_is_silent_when_both_are_clean(repo: Path):
    ev = _event(repo, title="fix: a crash", body="reproduced locally")
    assert not fired(gate(repo, "--mode", "ci", "--pr-event", ev), "public-text")


def test_pr_text_reports_a_skip_rather_than_a_pass_when_there_is_no_payload(repo: Path):
    """The distinction SKIP exists for: 'could not check' must not read as 'checked'."""
    lines = gate(repo, "--mode", "ci")
    assert any("[public-text]" in ln and "SKIP" in ln for ln in lines)


# ------------------------------------------------------ conventional commit subjects
#
# CLAUDE.md and CONTRIBUTING.md both required Conventional Commits and nothing read
# either. Five pull request titles and two subjects on main drifted to "M1: ...",
# "M2: ..." and "M3: ..." before anyone noticed, across eleven pull requests.
#
# Checked on both surfaces because each is decisive in a different case: a squash of a
# multi-commit branch takes its subject from the pull request title, and a squash of a
# single-commit branch takes it from the commit. Guarding one leaves the other open,
# which is exactly the split that let those two subjects through while every title
# around them was well formed.

def test_a_milestone_prefixed_pr_title_is_refused(repo: Path):
    ev = _event(repo, title="M1: one real backend call", body="clean")
    assert fired(gate(repo, "--mode", "ci", "--pr-event", ev), "conventional-subject")


def test_a_conventional_pr_title_is_accepted(repo: Path):
    """Without this, a check that refused every title would pass the test above."""
    ev = _event(repo, title="feat: one real backend call", body="clean")
    assert not fired(gate(repo, "--mode", "ci", "--pr-event", ev), "conventional-subject")


def test_a_scope_and_a_breaking_marker_are_still_conventional(repo: Path):
    ev = _event(repo, title="feat(loop)!: drop the old dispatch", body="clean")
    assert not fired(gate(repo, "--mode", "ci", "--pr-event", ev), "conventional-subject")


def test_a_type_outside_the_declared_set_is_refused(repo: Path):
    """`wip:` is Conventional-shaped but not one of the six CONTRIBUTING.md names."""
    ev = _event(repo, title="wip: half a thing", body="clean")
    assert fired(gate(repo, "--mode", "ci", "--pr-event", ev), "conventional-subject")


def test_a_milestone_prefixed_commit_subject_is_refused(repo: Path):
    msg = repo / "msg.txt"
    msg.write_text("M3: the response state machine\n\nbody\n", encoding="utf-8")
    lines = gate(repo, "--mode", "commit-msg", "--message-file", str(msg))
    assert fired(lines, "conventional-subject")


def test_a_conventional_commit_subject_is_accepted(repo: Path):
    msg = repo / "msg.txt"
    msg.write_text("feat: the response state machine\n\nbody\n", encoding="utf-8")
    lines = gate(repo, "--mode", "commit-msg", "--message-file", str(msg))
    assert not fired(lines, "conventional-subject")


def test_a_generated_merge_or_revert_subject_is_exempt(repo: Path):
    """Nobody wrote these, so holding them to a convention would block a real operation."""
    for subject in ("Merge branch 'main' into feat/x", 'Revert "feat: a thing"'):
        msg = repo / "msg.txt"
        msg.write_text(subject + "\n", encoding="utf-8")
        lines = gate(repo, "--mode", "commit-msg", "--message-file", str(msg))
        assert not fired(lines, "conventional-subject"), subject


def test_contributing_lists_exactly_the_gated_types():
    """The prose copy and the enforced copy must not drift.

    CONTRIBUTING.md names the types for a human; docs_gate.py decides. Two copies of one
    fact is the drift this repository's whole documentation scheme exists to prevent, so
    the second copy is asserted against the first rather than trusted.
    """
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "dg_types", ROOT / "scripts" / "docs_gate.py")
    mod = importlib.util.module_from_spec(spec)
    # Registered before execution: the module defines dataclasses, and dataclasses
    # resolves a class's module through sys.modules while the class body is executing.
    sys.modules["dg_types"] = mod
    spec.loader.exec_module(mod)
    prose = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    line = next(ln for ln in prose.splitlines() if "conventionalcommits.org" in ln)
    block = line + " " + prose.splitlines()[prose.splitlines().index(line) + 1]
    named = set(re.findall(r"`(\w+):`", block))
    assert named == set(mod.CONVENTIONAL_TYPES), (named, mod.CONVENTIONAL_TYPES)


# ------------------------------------------------------------------- .env.example names

def _config_doc(repo: Path, *names: str) -> None:
    """A stand-in for the generated reference, carrying only the names that matter."""
    rows = "".join(f"| `{n}` | x | a setting. |\n" for n in names)
    (repo / "docs" / "CONFIGURATION.md").write_text(
        "<!-- BUDGET: 99 -->\n# Configuration\n\n"
        "| Variable | Default | Notes |\n|---|---|---|\n" + rows,
        encoding="utf-8")


def test_env_example_fires_on_a_setting_that_no_longer_exists(repo: Path):
    """The regression itself: ADR-0034 deleted a field and the example kept offering it."""
    _config_doc(repo, "DELEGATE_REAL_ONE")
    (repo / ".env.example").write_text(
        "DELEGATE_REAL_ONE=1\n# DELEGATE_DELETED_KNOB=1\n", encoding="utf-8")
    assert fired(gate(repo), "env-example", "DELEGATE_DELETED_KNOB")


def test_env_example_is_silent_when_every_name_is_real(repo: Path):
    """The other direction, including a commented-out line, which is still advice."""
    _config_doc(repo, "DELEGATE_REAL_ONE", "DELEGATE_REAL_TWO")
    (repo / ".env.example").write_text(
        "DELEGATE_REAL_ONE=1\n# DELEGATE_REAL_TWO=2\n", encoding="utf-8")
    assert not fired(gate(repo), "env-example")


def test_env_example_refuses_to_pass_with_nothing_to_check_against(repo: Path):
    """A reference document holding no names would make this check vacuous.

    It reads its allowed set out of a generated document. If that document ever stops
    carrying `DELEGATE_*` names -- renamed, restructured or replaced -- the comparison
    silently becomes "nothing is known, so nothing is wrong", which is exactly the shape
    CLAUDE.md says is worse than no check at all. It must block instead.
    """
    (repo / "docs" / "CONFIGURATION.md").write_text(
        "<!-- BUDGET: 99 -->\n# Configuration\n\nNo names here at all.\n",
        encoding="utf-8")
    (repo / ".env.example").write_text("DELEGATE_ANYTHING=1\n", encoding="utf-8")
    assert fired(gate(repo), "env-example", "would pass whatever")


# ----------------------------------------------------------------------- scan coverage

def test_scan_coverage_fires_on_a_file_over_the_byte_cap(repo: Path):
    """The gap: `scannable_files` skipped an oversized file with a bare `continue`.

    So `email-content` and `host-identifier` reported a clean pass over a file they had
    never opened. One byte past the cap is enough -- the point is that it is announced.
    """
    (repo / "docs" / "bulk.md").write_bytes(b"a" * 2_000_001)
    assert fired(gate(repo), "scan-coverage", "docs/bulk.md", "over the")


def test_scan_coverage_is_silent_on_a_file_under_the_cap(repo: Path):
    """A check that announced every file would pass the test above."""
    (repo / "docs" / "small.md").write_bytes(b"a" * 64)
    assert not fired(gate(repo), "scan-coverage", "docs/small.md")


def test_scan_coverage_says_nothing_about_a_binary_file(repo: Path):
    """Binary is a deliberate exclusion, not a coverage gap, and must stay quiet.

    Reporting it would make the warning routine, and a routine warning is read past --
    costing exactly the visibility this check was added for.
    """
    (repo / "docs" / "blob.md").write_bytes(bytes([0]) * 32 + b"text")
    assert not fired(gate(repo), "scan-coverage", "docs/blob.md")


# ------------------------------------------------------- agent bodies vs their capabilities


def agent(repo: Path, name: str, frontmatter: str, body: str) -> None:
    d = repo / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\n{frontmatter}---\n{body}\n", encoding="utf-8"
    )


def test_agent_capability_fires_on_a_body_that_needs_a_shell_it_lacks(repo: Path):
    """The first half of the class. A body instructing a command an agent has no tool for
    costs a turn and a failed call on every single invocation."""
    agent(repo, "reader", "tools: Read, Grep\n",
          "Start by running `pytest -q` to see what is broken.")
    assert fired(gate(repo), "agent-capability", "reader.md", "no shell")


def test_agent_capability_is_silent_when_the_agent_has_a_shell(repo: Path):
    """The closest thing that is not a violation. Without this the check could refuse
    every agent that mentions a command and still pass the test above."""
    agent(repo, "runner", "tools: Read, Bash\n",
          "Start by running `pytest -q` to see what is broken.")
    assert not fired(gate(repo), "agent-capability", "runner.md")


def test_agent_capability_fires_on_git_inside_a_sandboxed_agent(repo: Path):
    """The second half, and the one PLAN.md filed. `.git` is on the denylist, so the
    sandbox covers it with a tmpfs and every git command inside a delegation exits 128.

    Derived rather than asserted: this repo's `secret_globs.txt` fixture is extended with
    the `.git` entry, and the check reads that file. Hardcoding "git is unrunnable" would
    be the second copy that drifts the moment the denylist changes.
    """
    (repo / "security" / "secret_globs.txt").write_text(
        ".env\nid_rsa\n.git/**\n", encoding="utf-8")
    agent(repo, "auditor", "allowed_tools: [read_file, run_bash]\n",
          "Check the history first:\n\n```bash\ngit log --oneline -20\n```\n")
    assert fired(gate(repo), "agent-capability", "auditor.md", "git")


def test_agent_capability_is_silent_on_git_where_the_denylist_does_not_cover_it(repo: Path):
    """The direction that proves the derivation is real.

    Same agent, same body -- only `.git/**` removed from the denylist. If the check still
    fired, it would be reading a hardcoded opinion about git rather than the policy file,
    and would keep firing after someone changed that file.
    """
    (repo / "security" / "secret_globs.txt").write_text(".env\nid_rsa\n", encoding="utf-8")
    agent(repo, "auditor", "allowed_tools: [read_file, run_bash]\n",
          "Check the history first:\n\n```bash\ngit log --oneline -20\n```\n")
    assert not fired(gate(repo), "agent-capability", "auditor.md")


def test_agent_capability_leaves_a_host_agent_alone_about_git(repo: Path):
    """The distinction between the two formats, which is the easiest thing to get wrong.

    Only a server-format agent's shell is confined. A Claude Code agent lists `tools:` and
    runs on the host, where `.git` is simply there -- flagging it would be a false positive
    on every reviewer and auditor agent in the repository.
    """
    (repo / "security" / "secret_globs.txt").write_text(
        ".env\nid_rsa\n.git/**\n", encoding="utf-8")
    agent(repo, "reviewer", "tools: Read, Grep, Bash\n",
          "Check the history:\n\n```bash\ngit log --oneline -20\n```\n")
    assert not fired(gate(repo), "agent-capability", "reviewer.md")


def test_agent_capability_ignores_prose_that_merely_mentions_a_command(repo: Path):
    """A code span is an instruction; a sentence is not. The check reads shell fences and
    inline code, so an agent explaining that something cannot be run must not be flagged --
    which is exactly what a body documenting this very limitation would look like."""
    agent(repo, "explainer", "tools: Read\n",
          "You cannot run git here, and nothing in this repository will let you.")
    assert not fired(gate(repo), "agent-capability", "explainer.md")


def test_every_gate_check_is_exercised_by_some_test():
    """The meta-check, and the reason this file exists at all.

    Four checks in this project have been found unable to fail, two of them by an audit
    rather than by a test. A check with no negative test anywhere is the next one.

    Scoped to the whole test tree rather than to this file, which is the correction its
    first draft needed: eight checks are covered in files named after their own bugs --
    the amend handling, the commit message, host identifiers, the audit clock -- and
    demanding they also appear here would either fail for no reason or push a second copy
    of each into one place. Naming a check is weaker than proving it fires, and it is
    strong enough to stop a new one arriving with nothing at all.
    """
    block = GATE.read_text(encoding="utf-8").split("CHECKS = {", 1)[1].split("}", 1)[0]
    # Key AND function, because a test may name either: some drive a check directly by
    # calling it, which never mentions the registry key. Missing that cost the first draft
    # of this test three false positives.
    pairs = re.findall(r'"([a-z-]+)":\s*(\w+)', block)
    assert len(pairs) > 10, f"CHECKS did not parse ({pairs}); this would pass vacuously"

    tests_root = Path(__file__).resolve().parents[1]
    corpus = "\n".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in sorted(tests_root.rglob("test_*.py"))
    )
    missing = sorted(key for key, fn in pairs if f'"{key}"' not in corpus and fn not in corpus)
    assert not missing, (
        f"gate checks no test names: {missing}. Add one that fires on a real violation "
        f"and one that stays silent on the nearest thing that is not."
    )


# ------------------- the three checks the meta-test found with no negative test ---------
#
# `generated-doc`, `generated-coverage` and `never-track` were named by nothing. The first
# demonstrably works -- it fired repeatedly while this session's config changes were being
# made -- but "I have seen it fire" is not a test, and the other two were unproven.


def test_generated_doc_fires_when_a_rendered_block_drifts(repo: Path):
    """The freshness check, proven on a real generator rather than a stub.

    `gen_config_docs.py --check` is what the gate shells out to, so the violation has to be
    a document that genuinely disagrees with the dataclass -- which is arranged by editing
    the rendered block, exactly as a hand-edit of a generated file would.
    """
    shutil.copy(ROOT / "scripts" / "gen_config_docs.py", repo / "scripts")
    shutil.copytree(ROOT / "src" / "claude_delegate_local",
                    repo / "src" / "claude_delegate_local")
    shutil.copy(ROOT / "docs" / "CONFIGURATION.md", repo / "docs")
    text = (repo / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    (repo / "docs" / "CONFIGURATION.md").write_text(
        text.replace("| `DELEGATE_MAX_TOKENS`", "| `DELEGATE_MAX_TOKENS_DRIFTED`", 1),
        encoding="utf-8")
    assert fired(gate(repo), "generated-doc", "CONFIGURATION.md")


def test_generated_doc_is_silent_when_the_block_matches(repo: Path):
    """Without this, a check that always fired would pass the test above."""
    shutil.copy(ROOT / "scripts" / "gen_config_docs.py", repo / "scripts")
    shutil.copytree(ROOT / "src" / "claude_delegate_local",
                    repo / "src" / "claude_delegate_local")
    shutil.copy(ROOT / "docs" / "CONFIGURATION.md", repo / "docs")
    assert not fired(gate(repo), "generated-doc", "CONFIGURATION.md")


def test_generated_coverage_fires_on_a_generated_document_nothing_renders(repo: Path):
    """The check that stops a generator arriving without a freshness entry -- leaving the
    new document passing for the reason that nothing looked at it.

    It also had the wrong label until this test was written: registered as
    `generated-coverage` and reporting as `generated-doc`, so a waiver aimed at either one
    hit the wrong check.
    """
    (repo / "docs" / "RENDERED.md").write_text("<!-- BUDGET: 9 -->\n# R\n", encoding="utf-8")
    manifest = (repo / "scripts" / "docs_ownership.toml").read_text(encoding="utf-8")
    (repo / "scripts" / "docs_ownership.toml").write_text(
        manifest + '\n[docs."docs/RENDERED.md"]\naudience = ["contributor"]\n'
        'plane = "product"\ngenerated = true\nowns = []\ncovers_not = "Nothing."\n',
        encoding="utf-8")
    assert fired(gate(repo), "generated-coverage", "RENDERED.md")


def test_generated_coverage_is_silent_when_the_document_is_not_generated(repo: Path):
    """The nearest thing that is not a violation: the same new document, unflagged."""
    (repo / "docs" / "RENDERED.md").write_text("<!-- BUDGET: 9 -->\n# R\n", encoding="utf-8")
    manifest = (repo / "scripts" / "docs_ownership.toml").read_text(encoding="utf-8")
    (repo / "scripts" / "docs_ownership.toml").write_text(
        manifest + '\n[docs."docs/RENDERED.md"]\naudience = ["contributor"]\n'
        'plane = "product"\nowns = []\ncovers_not = "Nothing."\n',
        encoding="utf-8")
    assert not fired(gate(repo), "generated-coverage", "RENDERED.md")


def test_never_track_fires_on_a_staged_file_that_must_never_be_committed(repo: Path):
    """The second layer under .gitignore, and the one that has to work when .gitignore is
    edited, when someone uses `git add -f`, and when the file is empty -- an empty one
    committed today is populated tomorrow in a commit nobody reads twice."""
    (repo / "security" / "forbidden_strings.txt").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "security/forbidden_strings.txt"],
                   cwd=repo, capture_output=True)
    assert fired(gate(repo), "never-track", "forbidden_strings.txt")


def test_never_track_is_silent_when_the_file_is_only_on_disk(repo: Path):
    """Untracked and unstaged is the normal, correct state for every one of these files.
    A check that flagged it would fire on every clean checkout.

    The `.gitignore` is what makes that state reachable here: `gate()` runs `git add -A`
    first, so without it the file this check exists to keep out would be re-staged by the
    harness on the way in -- which is also the first layer this check sits underneath.
    """
    (repo / ".gitignore").write_text(
        "security/forbidden_strings.txt" + "\n", encoding="utf-8")
    subprocess.run(["git", "rm", "--cached", "-q", "security/forbidden_strings.txt"],
                   cwd=repo, capture_output=True)
    assert not fired(gate(repo), "never-track", "forbidden_strings.txt")
