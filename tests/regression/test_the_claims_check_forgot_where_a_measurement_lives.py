"""The CLAIMS check called a measurement unsourced because it looked in two of three places.

`docs-audit-dispatch` defines CLAIMS as "a measurement that no ADR or JOURNAL entry
substantiates". But this project records a measurement in the CHANGELOG section for the pull
request that made it -- that is where the *why* of every change lives, by CONTRIBUTING's own
rule -- so a figure sourced there reads as unsourced to an auditor following the instruction.

It has now happened twice. The 2026-09-06 audit retracted such a finding and diagnosed it
exactly: "a caller error: a measurement recorded in a changelog entry for the change that
introduced it is evidence. The instruction, not the document, was wrong." The instruction was
not changed. The 2026-09-11 audit then reported four CLAIMS findings that were
changelog-sourced, one of them the *same* measurement retracted the first time.

A diagnosis that does not reach the instruction is not a fix, which is the whole reason this
test exists rather than a third retraction.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DISPATCH = ROOT / ".claude/skills/docs-audit-dispatch/SKILL.md"


def claims_definition() -> str:
    """Just the CLAIMS paragraph, never the whole file.

    Scoped deliberately. `CHANGELOG.md` is named elsewhere in this skill -- the recording
    step points at it -- so a test searching the whole document would pass on a mention
    that no auditor reading the check class would ever see. That is the shape of an
    unfailable check, and this file exists because an instruction went unfixed once already.
    """
    text = DISPATCH.read_text(encoding="utf-8")
    start = text.index("**CLAIMS**")
    rest = text[start + len("**CLAIMS**"):]
    end = rest.find("\n\n**")
    return rest[:end if end != -1 else len(rest)]


def test_the_claims_definition_names_all_three_places_evidence_lives():
    """The fix. ADR and JOURNAL are not the only places a measurement is recorded."""
    body = claims_definition()
    for source in ("ADR", "JOURNAL", "CHANGELOG"):
        assert source in body, (
            f"the CLAIMS definition does not name {source}, so an auditor following it "
            f"will report a measurement recorded there as unsourced"
        )


def test_the_definition_is_what_is_checked_not_the_whole_skill():
    """Guards the guard: the scoping above must actually exclude the rest of the file.

    If `claims_definition` ever returned the whole document, the test above would pass on
    the recording step's mention of CHANGELOG.md and stop being able to fail.
    """
    body = claims_definition()
    whole = DISPATCH.read_text(encoding="utf-8")
    assert len(body) < len(whole) / 2, "the CLAIMS scope is reading too much of the skill"
    assert "## The passes" not in body


@pytest.mark.parametrize("other", ["ESCAPE ABUSE", "MISSING"])
def test_other_check_classes_are_not_swept_into_the_claims_scope(other: str):
    """A scope that ran to the end of the file would pass every assertion above by luck."""
    assert other not in claims_definition()
