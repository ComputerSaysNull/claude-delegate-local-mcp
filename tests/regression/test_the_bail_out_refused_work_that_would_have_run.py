"""The admission bail-out refused waiters that a little more patience would have served.

Measured 2026-09-17: a fourteen-wide fan-out into six slots. Every one of the eight waiters
was refused at the full `admission_wait_timeout` of 1800s having produced nothing, while the
six holding slots ran on and finished — three of them at 37,605 output tokens each, so the
gate was genuinely full for the whole window rather than shrunk by a leak.

Refusing bought nothing. The wait runs *before* `dispatch_timeout` starts its own clock —
the two stack rather than divide one budget — so a waiter that reaches the head still has
its whole allowance. A bail-out can therefore only turn slow into failed.

What bounds the wait instead is the queue itself: admission is first-come-first-served,
`admission_starvation_grace` ages a passed-over waiter into the barrier, and
`dispatch_timeout` bounds how long each slot ahead of it can be held. The setting survives
as an operator's cap on latency, which is a different question from safety.

The setting had already been carrying this lesson at a smaller scale: its own history
records being raised from 600 because "600s refused requests that would have run". 1800 did
the same thing to a bigger fan-out.

Before this, no configuration could express "wait as long as it takes" — the value was
required to be positive, so every setup carried a bail-out, and 0 was a `ConfigError`
rather than the "off" it is everywhere else in this config.
"""

from __future__ import annotations

import asyncio

import pytest

from claude_delegate_local.admission import Admission, AdmissionTimedOut
from claude_delegate_local.config import Config, ConfigError
from claude_delegate_local.server import admission_deadline


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": (".",),
        "max_inflight_seqs": 1,
        "admission_idle_hold": 0.0,
        "kv_token_budget": 100_000,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def test_zero_is_how_an_operator_says_the_wait_has_no_bound() -> None:
    assert admission_deadline(cfg(admission_wait_timeout=0)) is None


def test_zero_is_accepted_where_it_used_to_be_a_config_error() -> None:
    # The red: this raised "DELEGATE_ADMISSION_WAIT_TIMEOUT must be positive", so the
    # configuration space had no value meaning "as long as the work ahead takes".
    assert cfg(admission_wait_timeout=0).admission_wait_timeout == 0


def test_it_is_the_default() -> None:
    assert cfg().admission_wait_timeout == 0


def test_a_positive_value_still_caps_the_wait() -> None:
    """Negative control: the cap must remain available, or this removes a lever."""
    deadline = admission_deadline(cfg(admission_wait_timeout=60))
    assert deadline is not None


@pytest.mark.asyncio
async def test_an_unbounded_waiter_is_served_once_the_slot_frees() -> None:
    """The whole point: the waiter that used to be refused now gets the slot."""
    g = Admission(cfg())
    held = await g.acquire(100, entry_key="flash", entry_limit=5)

    async def free_it() -> None:
        await asyncio.sleep(0.2)
        await g.release(held)

    freeing = asyncio.create_task(free_it())
    lease = await asyncio.wait_for(
        g.acquire(100, entry_key="flash", entry_limit=5, deadline=None), timeout=5.0
    )

    await freeing
    await g.release(lease)


@pytest.mark.asyncio
async def test_a_capped_waiter_still_gives_up_and_says_which_rule() -> None:
    """Negative control: with a cap configured, the refusal must still work and explain."""
    g = Admission(cfg())
    held = await g.acquire(100, entry_key="flash", entry_limit=5)

    with pytest.raises(AdmissionTimedOut) as caught:
        await g.acquire(100, entry_key="flash", entry_limit=5, deadline=0.0)

    assert caught.value.rule == "max_inflight_seqs"
    await g.release(held)


def test_a_negative_value_is_still_refused() -> None:
    """0 means off; a negative is a mistake rather than a stronger off."""
    with pytest.raises(ConfigError):
        cfg(admission_wait_timeout=-1)
