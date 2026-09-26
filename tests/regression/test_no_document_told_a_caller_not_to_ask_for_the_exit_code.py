"""No document told a caller that asking for the exit code destroys it.

A task that says "report the exit code" gets a model to run `cmd; echo $?`, and then
`last_bash_exit` holds the echo's 0 rather than the command's status. The `run_bash`
description tells the delegated model the code is recorded, but the advice for whoever
writes the task left ARCHITECTURE.md in #376 and had no other home. Its home is
`delegate://orchestration`, the guide a caller reads when shaping a delegation.
"""

from __future__ import annotations

from test_caller_rules_lived_only_in_one_operators_memory import _resource


def test_the_guide_tells_a_caller_to_ask_for_the_outcome() -> None:
    text = " ".join(_resource().split())
    assert "Ask for the outcome, never the exit code" in text
