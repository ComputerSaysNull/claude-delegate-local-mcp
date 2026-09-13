"""A length stop that produced only reasoning returned nothing, and the reasoning was binned.

`run_delegation` ends with `answer = response.text`, and `CanonicalResponse.text` joins
`TextBlock`s only. So a reply whose whole output was reasoning -- every token of it on the
wire, paid for, and already parsed into a `ThinkingBlock` -- became the empty string, and
`empty_response` reported the fact without anyone being able to act on it.

Measured over the 446 transcript summaries on this machine: **nine** dispatches reported
`empty_response` with `answer_chars` 0, every one of them at `finish_reason` `'length'` and
`ok` true, carrying between 14,475 and 44,854 output tokens -- 265,092 tokens generated and
discarded. A length stop is not a timeout and not a cancellation, so none of them would have
been rescued by returning a partial at a deadline; they completed exactly as asked and the
answer was thrown away at the last step.

`reasoning_exhausted` is true on all nine, which is the sharp end of it: the server already
diagnosed the condition. ADR-0014's recovery had also already run -- the verdict is earned
only after a larger budget and a lower effort have both been tried -- so each of those nine
burned a retry ladder before discarding what it had.

`test_the_decode_rate_charged_prefill_to_the_decoder` names this symptom in its own
docstring and fixes the *cause of the bad ceiling* rather than the discard. This is the
other half: whatever the ceiling, a reply that spent itself reasoning should hand back what
it produced rather than an empty string.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from claude_delegate_local.backends.base import (
    CanonicalResponse,
    TextBlock,
    ThinkingBlock,
    answer_of,
)

# The smallest of the nine, used as the fixture so the numbers stay tied to the
# observation rather than becoming round ones that mean nothing.
OUTPUT_TOKENS = 14_475

REASONING = "Weighing the two readings of the exit condition, the narrower one holds because"


def reply(*blocks) -> CanonicalResponse:
    return CanonicalResponse(
        content=tuple(blocks),
        finish_reason="length",
        input_tokens=41_016,
        output_tokens=OUTPUT_TOKENS,
        model="served-id-1",
    )


def test_a_length_stop_carrying_only_reasoning_hands_the_reasoning_back():
    """The bug. Today `response.text` is empty and the ThinkingBlock never reaches a caller."""
    answer, is_reasoning = answer_of(reply(ThinkingBlock(REASONING)))

    assert is_reasoning is True
    # The reasoning itself survives, whole.
    assert REASONING in answer
    # And it is labelled, so a caller cannot mistake it for an answer the model chose to
    # give. An unbannered dump would be worse than the empty string it replaces.
    assert answer != REASONING
    assert answer.startswith("[")


def test_an_answer_that_has_text_is_handed_back_unchanged():
    """Control. Must pass before the fix as well as after.

    The easy wrong fix appends reasoning whenever there is any, which would change every
    successful reply on the way to fixing the empty ones.
    """
    answer, is_reasoning = answer_of(reply(ThinkingBlock(REASONING), TextBlock("the answer")))

    assert answer == "the answer"
    assert is_reasoning is False


def test_a_reply_with_neither_text_nor_reasoning_stays_empty():
    """Control. The fallback must not invent content where the model produced none.

    This is what `empty_response` should go on meaning, and a banner returned over an
    empty reasoning string would make the flag unreachable.
    """
    answer, is_reasoning = answer_of(reply())

    assert answer == ""
    assert is_reasoning is False


def test_whitespace_only_reasoning_is_not_treated_as_content():
    """Control against the obvious truthiness bug.

    A `ThinkingBlock` holding only a newline is the shape an emptied reasoning stream
    leaves behind, and `if thinking:` would call that content and return a banner with
    nothing under it.
    """
    answer, is_reasoning = answer_of(reply(ThinkingBlock("\n\n")))

    assert answer == ""
    assert is_reasoning is False
