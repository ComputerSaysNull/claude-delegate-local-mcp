"""A `Config` description says what the setting does and links its reason, and no more.

CONTRIBUTING's "Prose" rules send a setting's history to JOURNAL and its design reason to
an ADR. The descriptions are read twice by people -- in the generated CONFIGURATION.md and
in `--init`'s prompts -- and some had grown to 200 words of measurement narrative. A word
ceiling keeps them from regrowing. It is a count, not a judgement, so it cannot say a
description is good, only that it has stopped being a history.
"""

from __future__ import annotations

from claude_delegate_local import config

MAX_WORDS = 80


def _too_long(rows: list[dict]) -> list[str]:
    return [
        f"{row['field']}: {len(row['description'].split())} words"
        for row in rows
        if len(row["description"].split()) > MAX_WORDS
    ]


def test_every_config_description_is_at_most_the_ceiling() -> None:
    too_long = _too_long(config.describe())
    assert not too_long, (
        f"over {MAX_WORDS} words -- say what the setting does and link the ADR or JOURNAL "
        "entry for why:\n" + "\n".join(too_long)
    )


def test_the_ceiling_fires_on_a_long_description() -> None:
    """Negative control: a planted over-long row is reported."""
    rows = [{"field": "planted", "description": "word " * (MAX_WORDS + 1)}]
    assert _too_long(rows) == [f"planted: {MAX_WORDS + 1} words"]
