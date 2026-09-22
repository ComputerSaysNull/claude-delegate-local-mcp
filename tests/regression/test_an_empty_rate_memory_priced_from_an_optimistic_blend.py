"""An empty rate memory priced the first turn from the endpoint's since-boot figure.

That figure is a blend over every concurrency regime the engine has ever served, and the
first turn is about to meet one specific regime. Measured 41.7% over (ADR-0094) and 1.745x
over on 2026-09-19, and its error runs *optimistic* -- which is the direction that kills a
turn rather than truncating it. A budget above what the clock can pay for authorises a
reply that never arrives; one below it truncates and something comes back.

So the empty case takes a configured floor instead, below every rate this deployment has
been seen to achieve. It is a floor to start from and not an estimate to keep: the sampler
files a real one within a scrape or two of any load, and a bucket with samples in it never
reaches this path at all.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import pytest

from claude_delegate_local.config import Config
from claude_delegate_local.loop import RateHistory, seed_decode_rate

SINCE_BOOT = 34.38  # the shape of the figure that used to answer here


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


class Cluster:
    """A backend whose only job is to report a since-boot rate, as the real one does."""

    def __init__(self, rate: float | None = SINCE_BOOT) -> None:
        self.rate = rate
        self.scrapes = 0

    async def probe_cluster(self) -> dict[str, object]:
        self.scrapes += 1
        return {
            "decode_tokens_per_second_since_boot": self.rate,
            "requests_running": 6.0,
            "kv_cache_size_tokens": 1_000_000,
        }


async def test_an_empty_memory_takes_the_floor_rather_than_the_since_boot_blend() -> None:
    """The red. Unfixed this returns 34.38 -- the blend -- and prices the first turn on it."""
    rate = await seed_decode_rate(
        Cluster(), RateHistory(), expected_concurrency=6,
        fallback=cfg().rate_fallback_tok_s,
    )

    assert rate.rate == pytest.approx(10.0), (
        f"an empty memory priced from {rate.rate}, which is the since-boot blend rather "
        f"than the configured floor; the blend was measured 41.7% over and errs in the "
        f"direction that kills a turn"
    )
    assert rate.source == "configured_fallback"


async def test_a_bucket_with_samples_never_reaches_the_floor() -> None:
    """The control that keeps the fix honest. A floor that answered a *measured* question
    would throw away the measurement this whole design exists to make."""
    history = RateHistory()
    for _ in range(4):
        history.observe(6_000, 200.0, concurrency=6)  # 30 tok/s, comfortably above 10

    rate = await seed_decode_rate(
        Cluster(), history, expected_concurrency=6, fallback=cfg().rate_fallback_tok_s,
    )

    assert rate.rate == pytest.approx(30.0), (
        f"a bucket with samples answered {rate.rate} rather than what it measured; the "
        f"floor is for the empty case only"
    )
    assert rate.source == "observed_at_concurrency"


async def test_a_failed_scrape_is_still_unpriced_rather_than_floored() -> None:
    """The boundary of the change, pinned so it cannot drift outwards.

    "Nothing was ever measured at this concurrency" and "the scrape that would have told
    us failed" are different questions. The floor answers the first. The second keeps its
    existing answer, `unknown`, which means the caller gets no ceiling -- and several
    deadline tests depend on exactly that, because their doubles have no cluster at all.
    """

    class Broken:
        async def probe_cluster(self) -> dict[str, object]:
            raise RuntimeError("metrics unreachable")

    rate = await seed_decode_rate(
        Broken(), RateHistory(), expected_concurrency=2,
        fallback=cfg().rate_fallback_tok_s,
    )

    assert rate.rate is None, (
        f"a failed scrape priced at {rate.rate}; the floor is for an empty memory, not "
        f"for a metrics endpoint that did not answer"
    )
    assert rate.source == "unknown"


async def test_turning_the_floor_off_restores_the_blend() -> None:
    """A pin on the escape hatch, so the old behaviour stays reachable and described."""
    rate = await seed_decode_rate(
        Cluster(), RateHistory(), expected_concurrency=6, fallback=0.0,
    )

    assert rate.rate == pytest.approx(SINCE_BOOT)
    assert rate.source == "cluster_since_boot"
