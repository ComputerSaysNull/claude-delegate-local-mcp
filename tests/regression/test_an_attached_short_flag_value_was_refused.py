"""`read_git` refused a short flag whose value was attached to it.

`-U1`, `-M50%`, `-n5` and `-L1,5` are how git spells a short option with its value, and
for `-U` and `-M` the only way: measured, `diff -U 1` reads the `1` as a revision. The
allowlist looked each token up whole, so `-U1` was "not an accepted flag" while `-U` was
listed -- the flag was allowlisted and unusable. Found while writing the R21 regression
test, whose context-width case had to fall back to `--unified=1`.
"""

from __future__ import annotations

import pytest

from claude_delegate_local import tools


@pytest.mark.parametrize("command, token", [
    ("diff", "-U1"), ("diff", "-M50%"), ("log", "-n5"), ("blame", "-L1,5"),
])
def test_an_attached_value_is_accepted_for_an_allowlisted_short_flag(command, token):
    assert tools._checked_args(command, [token]) == [token]


@pytest.mark.parametrize("command, token", [
    ("diff", "-O/tmp/order"),   # -O names a file, and is not on the allowlist
    ("log", "-U1"),             # -U is diff's, not log's
    ("diff", "-sw"),            # bundled booleans stay refused
])
def test_a_short_flag_that_is_not_allowlisted_stays_refused(command, token):
    with pytest.raises(tools.ToolRefused):
        tools._checked_args(command, [token])
