"""`check_changelog_number`: the guessed heading, against the number GitHub actually issued.

A heading's number cannot be known when the entry is written, so it is guessed -- the next
number GitHub will issue -- and corrected in the branch if it moved. That leaves one way to
fail: guess, forget to check, merge. Which is what happened twice already, as `#TBD`
placeholders rather than wrong numbers, and both times a later pull request had to repay it
(#193, then #205).

A rule in a skill cannot catch that, because a skill only runs when it is invoked. This runs
on the pull request event, where the real number first exists and before the merge that would
make the mistake permanent.

Both directions, per the rule this repository learned the hard way: a check asserted only to
fire could fire on everything, and one asserted only to stay silent could be silent always.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "wt"
    for d in ("scripts", "docs", "security", "src"):
        (r / d).mkdir(parents=True, exist_ok=True)
    (r / "scripts" / "docs_gate.py").write_bytes((ROOT / "scripts" / "docs_gate.py").read_bytes())
    (r / "scripts" / "docs_ownership.toml").write_bytes(
        (ROOT / "scripts" / "docs_ownership.toml").read_bytes())
    for name, body in (
        ("allowed_emails.txt", "t@example.com\n"),
        ("secret_globs.txt", ".env\n"),
        ("content_safe_emails.txt", "t@example.com\n"),
        ("forbidden_strings.txt", "# local\n"),
    ):
        (r / "security" / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, capture_output=True)
    return r


def changelog(repo: Path, *headings: str) -> None:
    body = "<!-- BUDGET-PER-ENTRY: 50 -->\n# Changelog\n\nPreamble.\n\n"
    body += "\n\n".join(f"## {h}\n\n### Fixed\n- something" for h in headings)
    (repo / "CHANGELOG.md").write_text(body + "\n", encoding="utf-8")


def event(repo: Path, number: int) -> str:
    p = repo / "event.json"
    p.write_text(
        json.dumps({"pull_request": {"number": number, "title": "fix: a thing", "body": "why"}}),
        encoding="utf-8")
    return str(p)


def gate(repo: Path, *args: str) -> list[str]:
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    proc = subprocess.run(
        [sys.executable, "scripts/docs_gate.py", *args],
        cwd=repo, capture_output=True, text=True,
    )
    return proc.stdout.splitlines()


def fired(lines: list[str], *needles: str) -> bool:
    return any(
        "[changelog-number]" in ln
        and ("BLOCK" in ln or "WARN" in ln)
        and all(n in ln for n in needles)
        for ln in lines
    )


def test_fires_when_the_guess_was_wrong(repo: Path):
    changelog(repo, "#206 — 2026-09-14 — fix: a thing", "#205 — 2026-09-13 — fix: older")
    assert fired(gate(repo, "--mode", "ci", "--pr-event", event(repo, 209)), "206", "209")


def test_silent_when_the_guess_was_right(repo: Path):
    """The other direction. A check that blocked every pull request would pass the test above."""
    changelog(repo, "#209 — 2026-09-14 — fix: a thing", "#205 — 2026-09-13 — fix: older")
    assert not fired(gate(repo, "--mode", "ci", "--pr-event", event(repo, 209)))


def test_fires_when_the_placeholder_was_never_filled(repo: Path):
    """The shape that actually reached main, twice."""
    changelog(repo, "#TBD — 2026-09-14 — fix: a thing")
    assert fired(gate(repo, "--mode", "ci", "--pr-event", event(repo, 209)), "TBD")


def test_only_the_newest_heading_is_judged(repo: Path):
    """Older entries carry other numbers by definition; they are not this PR's business."""
    changelog(repo, "#209 — 2026-09-14 — fix: a thing", "#42 — 2026-01-01 — fix: ancient")
    assert not fired(gate(repo, "--mode", "ci", "--pr-event", event(repo, 209)))


def test_reports_a_skip_rather_than_a_pass_without_a_payload(repo: Path):
    """'Could not check' must not read as 'checked', which is the whole of SKIP's job."""
    changelog(repo, "#209 — 2026-09-14 — fix: a thing")
    lines = gate(repo, "--mode", "ci")
    assert any("[changelog-number]" in ln and "SKIP" in ln for ln in lines)
