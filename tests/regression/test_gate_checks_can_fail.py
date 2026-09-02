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

`check_env_example` joins them for the same reason: it exists *because* a
deleted setting went on being advertised, so shipping it without a negative
test would repeat the mistake that created it.

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
