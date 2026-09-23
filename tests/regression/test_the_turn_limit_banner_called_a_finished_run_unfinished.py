"""The turn-limit banner called a finished run unfinished.

`hit_turn_limit` is exactly "the loop reached its last turn", and that turn is always
declared with its tools forbidden -- so a two-turn run that called one tool and then
answered sets it, whether or not it had anything left to do. The banner said more than
the flag knows: that the delegation "ran out of turns while still working" and that the
answer was "a report of an unfinished investigation". Observed on a probe that had
finished. The banner now states what is known, and leaves the judgement to the reader.
"""

from __future__ import annotations

from claude_delegate_local.backends.base import TURN_LIMIT_BANNER


def test_the_banner_does_not_claim_the_run_was_unfinished():
    text = " ".join(TURN_LIMIT_BANNER.split()).lower()
    assert "still working" not in text
    assert "unfinished investigation" not in text


def test_the_banner_still_says_what_the_flag_knows():
    text = " ".join(TURN_LIMIT_BANNER.split()).lower()
    assert "last turn" in text and "max_turns" in text
