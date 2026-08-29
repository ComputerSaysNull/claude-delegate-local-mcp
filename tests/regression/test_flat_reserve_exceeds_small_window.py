"""A reserve held as a flat token count, against a model whose whole window is smaller.

Upstream reserved a fixed number of tokens for the reply before deciding whether a request
would fit. The number was chosen against a large window and was perfectly sensible there. On
a small local model the same constant was most of the window, so the projection was already
past the abort threshold with an empty history -- the detector reported every delegation as
out of room before it had done anything, and did so at rest, which is the hardest kind of
false positive to argue with because it never varies.

The fix is not a smaller constant. A smaller constant simply moves which model breaks. The
reserve is a *fraction* of `ModelEntry.context_window`, which makes the failure structurally
impossible rather than unlikely, and that is what these tests assert: the property, not one
instance of it.

So the central assertion here is deliberately not "an 8K model does not trip at rest". A
differently-broken flat reserve that happened to be small enough would pass that. It is that
the reserve equals a fraction of the window, for every window, which no flat constant can
satisfy for more than one.
"""

from __future__ import annotations

import pytest

from claude_delegate_local import loop
from claude_delegate_local.config import OVERFLOW_ABORT_AT, Config, ConfigError
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist

# Windows spanning the range this project actually sees: a small quantised local model at one
# end, the deployment's own million-token entry at the other.
WINDOWS = (4096, 8192, 32_768, 131_072, 1_048_576)

# What upstream's reserve was worth. Comfortable against a 1M window; larger than 95% of an
# 8K one, which is the whole bug.
FLAT_RESERVE = 7800


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",), "context_overflow_enabled": True}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def entry(window: int) -> ModelEntry:
    return ModelEntry(
        key="flash", base_url=HOST, served_model_id="served-id-1", context_window=window
    )


def test_the_reserve_is_a_fraction_of_the_window_for_every_window():
    """The property that closes the bug class. No flat constant satisfies it twice."""
    c = cfg(overflow_reserve_fraction=0.05)
    for window in WINDOWS:
        assert loop.overflow_reserve(c, entry(window)) == round(window * 0.05), window


def test_a_flat_reserve_would_report_a_small_model_as_full_before_it_started():
    """The bug itself, reconstructed, so the test above is anchored to a real failure.

    Nothing has happened yet -- no history, no prompt, an entirely empty delegation -- and
    the flat reserve alone already carries the projection past the abort threshold.
    """
    window = 8192
    flat_share = (0 + 0 + FLAT_RESERVE) / window
    assert flat_share >= OVERFLOW_ABORT_AT, "the reconstruction must reproduce the bug"

    actual = loop.projected_fraction(cfg(), entry(window), 0, 0)
    assert actual < OVERFLOW_ABORT_AT
    # Within one token: the reserve is rounded to whole tokens, so on a small window the
    # share it costs differs from the configured fraction by at most 1/window.
    assert actual == pytest.approx(cfg().overflow_reserve_fraction, abs=1 / window)


def test_no_model_in_range_starts_a_delegation_already_past_a_threshold():
    """The other direction, across the whole range rather than one convenient window.

    A fraction-based reserve costs the same share of every model, so an empty delegation
    begins at exactly that share and nowhere near any stage of the escalation.
    """
    c = cfg()
    for window in WINDOWS:
        at_rest = loop.projected_fraction(c, entry(window), 0, 0)
        assert at_rest == pytest.approx(c.overflow_reserve_fraction, abs=1 / window), window
        assert at_rest < OVERFLOW_ABORT_AT, window


def test_a_reserve_big_enough_to_reproduce_the_bug_is_refused_at_load():
    """The second line of defence, and the reason this cannot be reintroduced by config.

    A fraction at or above the abort threshold is the flat-reserve bug expressed as a
    setting: the reserve alone would account for the whole budget. `config.py` refuses it
    rather than letting it fail thirty minutes into a delegation.
    """
    with pytest.raises(ConfigError) as caught:
        cfg(overflow_reserve_fraction=OVERFLOW_ABORT_AT)
    assert "fraction of the context window" in str(caught.value)


def test_an_ordinary_reserve_is_accepted():
    """The other direction. Refusing everything would pass the test above."""
    assert cfg(overflow_reserve_fraction=0.2).overflow_reserve_fraction == 0.2
