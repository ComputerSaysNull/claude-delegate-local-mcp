"""Overflow handling armed against a context window nobody set and nothing confirmed.

Every overflow threshold is a share of `context_window`. A `models.toml` entry that omits it
inherits a default, and the window check only disarmed when the endpoint *reported* a
different number: an endpoint that reports none left the guard armed on the guess, the one
case the switch shipped off to avoid. A defaulted window the endpoint cannot confirm now
disarms and says which line to add.
"""

from __future__ import annotations

import asyncio

from claude_delegate_local import server
from claude_delegate_local.config import Config
from claude_delegate_local.registry import ModelEntry

HOST = "http://example.com:8000"  # on the gate's placeholder allowlist


class Endpoint:
    """A backend whose only answer is what it says about its window."""

    def __init__(self, window: int | None) -> None:
        self.window = window

    async def probe_window(self):
        return self.window


def _entry(*, defaulted: bool, window: int = 100_000) -> ModelEntry:
    return ModelEntry(
        key="flash", base_url=HOST, served_model_id="served-id-1",
        context_window=window, context_window_defaulted=defaulted,
    )


def _armed(entry: ModelEntry, reported: int | None) -> tuple[bool, str]:
    check = server.WindowCheck(Config(
        workspace_roots=(".",), context_overflow_enabled=True,  # type: ignore[arg-type]
    ))
    return asyncio.run(check.armed(Endpoint(reported), entry))


def test_a_defaulted_window_the_endpoint_cannot_confirm_disarms():
    armed, reason = _armed(_entry(defaulted=True), None)

    assert armed is False, "armed on a window nobody set and nothing confirmed"
    assert "context_window" in reason and "flash" in reason, reason


def test_a_declared_window_the_endpoint_cannot_confirm_still_arms():
    """Control: the operator wrote this number, so an endpoint's silence is no objection."""
    assert _armed(_entry(defaulted=False), None) == (True, "")


def test_a_defaulted_window_the_endpoint_confirms_arms():
    """Control: the default happens to be right, and the endpoint says so."""
    assert _armed(_entry(defaulted=True), 100_000) == (True, "")
