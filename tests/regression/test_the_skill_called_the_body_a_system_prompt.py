"""The shipped skill told callers the agent body becomes the system prompt. It does not.

`write-delegate-agent` said it twice -- "a body that becomes the system prompt" and
"Everything below the closing `---` is the **system prompt**". `Delegation.render` puts the
body at the head of the *user* message, and its docstring is explicit that this is load
bearing rather than incidental: the system prompt is a byte-for-byte constant the cluster
caches, so nothing varying per delegation may enter it, "the agent body included, however
much it reads like one" (ADR-0011).

It matters to whoever writes an agent file. A body believed to be a system prompt is
believed to be cached and to sit above the task in precedence; it is neither. It is resent
every call, ahead of the prefetched files, and counts against the same prompt they do.

This is the second defect found in the shipped skill by using it rather than reading it,
and M10's exit condition is exactly that a caller can work from this file alone.

The rule this file is written to (CLAUDE.md): a check that cannot fail is worse than no
check. `test_the_claim_would_be_caught_again` feeds the checker the sentence that shipped
and asserts it is rejected.
"""

from __future__ import annotations

import inspect
import pathlib

from claude_delegate_local.loop import Delegation

SKILL = pathlib.Path(__file__).resolve().parents[2] / (
    "src/claude_delegate_local/skills/write-delegate-agent/SKILL.md"
)

# The claim, in the two shapes it took. Matched case-insensitively on normalised whitespace,
# because a passage rewrapped across a line break is the same claim.
FORBIDDEN = (
    "becomes the system prompt",
    "is the **system prompt**",
)


def flat(text: str) -> str:
    return " ".join(text.split()).lower()


def says_the_body_is_the_system_prompt(text: str) -> bool:
    haystack = flat(text)
    return any(flat(claim) in haystack for claim in FORBIDDEN)


def test_the_code_still_puts_the_body_in_the_user_message() -> None:
    """Pins the claim to the behaviour. If `render` ever does put the body in the system
    prompt, this fails and the skill's old wording becomes correct again."""
    source = inspect.getsource(Delegation.render)
    assert "self.agent_body" in source
    assert "system" not in source.split('"""')[2], (
        "render() now touches the system prompt; re-check what the skill should say"
    )


def test_the_skill_does_not_call_the_body_a_system_prompt() -> None:
    assert not says_the_body_is_the_system_prompt(SKILL.read_text(encoding="utf-8"))


def test_the_claim_would_be_caught_again() -> None:
    """The negative control, using the sentence that actually shipped."""
    assert says_the_body_is_the_system_prompt(
        "An agent file is a role this server can run: frontmatter that sets the dispatch, "
        "and a body\nthat becomes the system prompt."
    )
