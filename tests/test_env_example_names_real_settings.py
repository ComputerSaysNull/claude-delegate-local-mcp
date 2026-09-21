"""Every `DELEGATE_*` name in `.env.example` is a setting that still exists.

`.env.example` is owned by `docs/CONFIGURATION.md`, which is generated -- so the
owning-doc check exempts it and a rename in `config.py` would leave the example naming a
variable nothing reads. That is the same silent-ignore failure `load()` has for any
unknown name, arriving by a different door: the operator copies the file, sets the
setting, and is told nothing.

Commented lines count. A `# DELEGATE_FOO=` suggestion is exactly as misleading as a live
one, and more likely to be stale because nothing ever exercises it.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from claude_delegate_local import config

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"(DELEGATE_[A-Z0-9_]+)")
# An *assignment*, live or commented out: `FOO=` or `# FOO=`. Distinct from a mention,
# because the migration note has to name the retired settings in order to say "delete
# these" -- and a note telling you to remove a line is the opposite of offering it. The
# first version of this check conflated the two and failed on that note, which is how the
# distinction got found.
ASSIGNMENT = re.compile(r"^\s*#?\s*(DELEGATE_[A-Z0-9_]+)\s*=", re.MULTILINE)


def names_in(text: str) -> set[str]:
    """Every `DELEGATE_*` token, live, commented or merely mentioned.

    Deliberately not a parse: a parse would skip the commented suggestions, which are the
    half most likely to have rotted.
    """
    return set(NAME.findall(text))


def assignments_in(text: str) -> set[str]:
    """Only the names the file offers a value for."""
    return set(ASSIGNMENT.findall(text))


def real_names() -> set[str]:
    return {config.env_name(f.name) for f in dataclasses.fields(config.Config)}


def test_every_name_in_the_example_is_a_real_setting():
    found = names_in((ROOT / ".env.example").read_text(encoding="utf-8"))
    assert found, "no DELEGATE_* names found at all; the regex or the file is wrong"
    unknown = found - real_names()
    assert not unknown, (
        f"{sorted(unknown)} named in .env.example but absent from Config. `load()` reads "
        "only names matching a field, so each of these would be ignored in silence."
    )


def test_the_example_does_not_offer_a_retired_setting():
    """Offering one is worse than offering an unknown: the server refuses to start.

    Assignments only. The file must still be free to *name* a retired setting in prose,
    because telling an operator which line to delete is the whole of the migration note.
    """
    offered = assignments_in((ROOT / ".env.example").read_text(encoding="utf-8"))
    retired = {config.env_name(f) for f in config.RETIRED_FIELDS}
    assert not (offered & retired), (
        f"{sorted(offered & retired)} is retired; setting it is refused at load."
    )


def test_the_retirement_check_fires_on_an_offered_line():
    """Demonstrated, not asserted. Both live and commented-out forms must trip it, since
    a commented suggestion is the one an operator uncomments."""
    retired_name = config.env_name(config.RETIRED_FIELDS[0])
    for offered in (f"{retired_name}=0.7", f"# {retired_name}=0.7"):
        assert assignments_in(offered) & {retired_name}, offered
    # ...and a prose mention of the same name does not.
    assert not assignments_in(f"{retired_name} is retired; delete the line.")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("DELEGATE_NOT_A_SETTING=1", {"DELEGATE_NOT_A_SETTING"}),
        ("# DELEGATE_ALSO_NOT_ONE=2", {"DELEGATE_ALSO_NOT_ONE"}),
        ("nothing here", set()),
    ],
)
def test_the_scanner_finds_what_it_claims_to(text, expected):
    """The negative control, and the reason the two tests above mean anything.

    Both assert that a set is empty, which a scanner returning nothing would satisfy
    forever. This pins that it reads live lines and commented ones alike.
    """
    assert names_in(text) == expected
