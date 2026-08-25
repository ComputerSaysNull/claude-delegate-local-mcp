"""Two bugs where a safety mechanism silently validated nothing.

Both were found by negative-testing the gate rather than by reading it, and both share a
shape worth naming: the check ran, reported success, and could not have failed. That is
worse than an absent check, because an absent check is not trusted.

Named after the bugs, per the project's testing convention.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"
GEN = ROOT / "scripts" / "gen_config_docs.py"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT, capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Bug 1: a supersede reference validated against the whole file, so the
# reference satisfied itself and the check could never fire.
# ---------------------------------------------------------------------------

def _adr_check(text: str) -> list[str]:
    """Reimplements the gate's declared-number logic against arbitrary text.

    Testing the rule directly rather than mutating the real DECISIONS.md keeps this
    hermetic -- a test that edits a tracked file in place will eventually lose a race
    with something else and leave the repo dirty.
    """
    heads = re.findall(r"^## (.+)$", text, re.M)
    declared = {
        int(m.group(1))
        for h in heads
        if (m := re.match(r"^(?:~~)?ADR-(\d{4}) ", h.strip()))
    }
    problems = []
    for h in heads:
        m = re.match(r"^(?:~~)?ADR-(\d{4}) ", h.strip())
        ref = re.search(r"[Ss]uperseded by ADR-(\d{4})", h)
        if m and ref and int(ref.group(1)) not in declared:
            problems.append(f"ADR-{m.group(1)} -> ADR-{ref.group(1)}")
    return problems


def test_supersede_pointing_at_a_nonexistent_adr_is_caught():
    """The bug: searching the file text for "ADR-0099" always succeeded, because the
    heading being validated contains that string itself."""
    text = textwrap.dedent("""
        ## ADR-0002 — 2026-08-24 — Later thing — Accepted

        ## ADR-0001 — 2026-08-24 — Earlier thing — Superseded by ADR-0099
    """)
    assert _adr_check(text) == ["ADR-0001 -> ADR-0099"]


def test_supersede_pointing_at_a_real_adr_passes():
    text = textwrap.dedent("""
        ## ADR-0002 — 2026-08-24 — Later thing — Accepted

        ## ADR-0001 — 2026-08-24 — Earlier thing — Superseded by ADR-0002
    """)
    assert _adr_check(text) == []


def test_a_struck_through_heading_still_counts_as_declared():
    """A superseded ADR remains a valid target for an even later one, so the
    declared-set must include struck-through headings."""
    text = textwrap.dedent("""
        ## ADR-0003 — 2026-08-24 — Newest — Accepted

        ## ~~ADR-0002 — 2026-08-24 — Middle~~ — Superseded by ADR-0003

        ## ADR-0001 — 2026-08-24 — Oldest — Superseded by ADR-0002
    """)
    assert _adr_check(text) == []


def test_the_real_decisions_file_has_no_dangling_supersede_references():
    text = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    assert _adr_check(text) == []


# ---------------------------------------------------------------------------
# Bug 2: the generator compared a committed doc against a CACHED compile of
# the source, so a same-length edit inside one timestamp tick was invisible.
# ---------------------------------------------------------------------------

def test_generator_does_not_read_a_cached_compile_of_its_source():
    """Python validates a .pyc on (mtime, size). Changing a default from 6 to 9 keeps the
    file length identical, so a stale cache can survive and the generator would then
    report the doc current against code that no longer exists.

    Asserting on the mechanism rather than trying to reproduce a filesystem timing race,
    which would be flaky by construction."""
    src = GEN.read_text(encoding="utf-8")
    assert "pycache_prefix" in src, (
        "the generator must redirect the bytecode cache so it always compiles the real "
        "source; see JOURNAL 2026-08-25"
    )
    # -B is NOT sufficient and must not be relied on: it stops WRITING bytecode, not
    # reading an existing stale cache. That mistake is why the first fix did not work.
    #
    # Match an ASSIGNMENT, not a mention: the generator's own comment explains why the
    # flag is inadequate, and a naive substring search flagged that explanation. A test
    # that cannot tell code from prose about code will fire on the documentation.
    assert not re.search(r"^\s*sys\.dont_write_bytecode\s*=", src, re.M), (
        "dont_write_bytecode does not prevent READING a stale cache -- use pycache_prefix"
    )


def test_gate_also_avoids_the_cached_compile_trap():
    assert "pycache_prefix" in GATE.read_text(encoding="utf-8")


def test_generated_config_doc_is_in_sync():
    """The check the gate runs, asserted directly so a stale doc fails the suite too."""
    proc = _run(GEN, "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# The gate's own self-reference false positive.
# ---------------------------------------------------------------------------

def test_the_secret_glob_list_does_not_flag_itself():
    """security/secret_globs.txt matches its own '*secret*' pattern. A pattern list that
    describes itself will trip on itself unless the policy files are exempt."""
    proc = _run(GATE, "--mode", "pre-commit")
    offending = [
        ln for ln in proc.stdout.splitlines()
        if "BLOCK" in ln and "secret-path" in ln and "security/" in ln
    ]
    assert not offending, offending


# ---------------------------------------------------------------------------
# Bug 3: the generated document embedded a platform-specific separator, so the
# freshness check could never pass on two platforms at once. Found by CI, which
# is the only place it could be found -- local runs are all one platform.
# ---------------------------------------------------------------------------

def test_generated_defaults_do_not_embed_os_pathsep():
    """`os.pathsep` is ';' on Windows and ':' elsewhere.

    Rendering it into the generated table meant a file generated locally could never
    match one generated in CI, making the "generated document is current" check
    unsatisfiable rather than merely failing. A reproducibility bug in the mechanism that
    exists to guarantee reproducibility.
    """
    import os
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from claude_delegate_local import config

    other = ";" if os.pathsep == ":" else ":"
    for row in config.describe():
        rendered = str(row["default"])
        assert os.pathsep not in rendered, (
            f"{row['env']} renders the local os.pathsep {os.pathsep!r}; it would differ "
            f"on the other platform"
        )
        assert other not in rendered, (
            f"{row['env']} renders the other platform's separator {other!r}"
        )


def test_committed_config_doc_contains_no_platform_separator():
    text = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    body = text.split("GEN:CONFIG:START")[1] if "GEN:CONFIG:START" in text else text
    for line in body.splitlines():
        if line.startswith("| `DELEGATE_") and (";" in line or ":" in line):
            # A colon is fine in prose; what must not appear is a separator-joined list.
            assert ".py;" not in line and ".py:" not in line, line[:120]


# ---------------------------------------------------------------------------
# Bug 4: the commit-identity check flagged GitHub's own synthetic merge commit,
# which would have blocked every pull request permanently.
# ---------------------------------------------------------------------------

def test_identity_check_ignores_merge_commits():
    """For a pull_request event, Actions checks out a merge commit that GitHub authors
    itself as noreply@github.com. It is not a contribution, and checking it would refuse
    every pull request forever -- a gate that blocks everything is as useless as one that
    blocks nothing."""
    src = GATE.read_text(encoding="utf-8")
    ident = src[src.index("def check_commit_identity"):src.index("def check_emails_in_files")]
    assert '"--no-merges"' in ident, (
        "the identity check must exclude merge commits; see the CI failure on PR #1"
    )


def test_author_allowlist_and_content_allowlist_are_not_the_same_policy():
    """Conflating them broke both directions.

    A service address must never be able to author a commit, but must be mentionable in a
    comment. Merging the lists forced a choice between a false positive on prose and a
    hole in the identity check.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("dg", GATE)
    dg = importlib.util.module_from_spec(spec)
    # Register before executing: the @dataclass decorator resolves its own module via
    # sys.modules, and fails opaquely on a module that is not there yet.
    sys.modules[spec.name] = dg
    try:
        spec.loader.exec_module(dg)
    finally:
        sys.modules.pop(spec.name, None)

    authors = set(dg.load_lines(ROOT / "security" / "allowed_emails.txt"))
    assert "noreply@github.com" not in authors, (
        "a service address must not be allowed to author commits"
    )
    assert "noreply@github.com" in dg.content_safe_emails(), (
        "a service address must be mentionable in file content"
    )
    assert authors, "the author allowlist must not be empty"


def test_gitleaks_allowlist_is_generated_not_hand_maintained():
    """CONTRIBUTING once said the gate's list and gitleaks' list "must stay in step".

    They drifted within an hour: the gate was fixed for a false positive and gitleaks was
    not, and CI caught it. A hand-maintained invariant between two files is the thing this
    project replaces with a generator, so it now is one.
    """
    proc = _run(ROOT / "scripts" / "gen_gitleaks_config.py", "--check")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    text = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "GEN:EMAILS:START" in text and "GEN:EMAILS:END" in text
    for addr in ("noreply@github", "ComputerSaysNull"):
        assert addr in text, f"{addr} missing from the generated allowlist"
