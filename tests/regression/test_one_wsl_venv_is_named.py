"""Every document and skip message must name the same WSL virtualenv.

`CONTRIBUTING.md` said `~/.venvs/cdl` and `README.md` said `~/.venvs/delegate`, for what
is one environment. The machine had only the second, so the first was a path that did not
exist -- and three test files told a reader to prove a skipped test by running
`~/.venvs/cdl/bin/python`, a command that fails before it reaches pytest.

That is worse than an ordinary stale instruction. Those messages exist so a skip cannot be
read as a pass (`test_live_skip_is_not_readable_as_a_pass.py`); one naming an unrunnable
command sends the reader away and the skip stands unproven anyway. Found when the tools
tests needed WSL to prove layer 1 and the documented interpreter was absent.

Five copies of one fact, and nothing compared them. This does.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENV = re.compile(r"~?/?\.venvs/([A-Za-z0-9._-]+)")

# Where the path is written for a human to copy. Not a glob over the tree: a match inside
# an archived document or an audit is a record of what was once true, not an instruction.
SOURCES = (
    "README.md",
    "CONTRIBUTING.md",
    "tests/test_paths.py",
    "tests/test_context.py",
    "tests/test_tools.py",
)


def _named() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for rel in SOURCES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        names = set(VENV.findall(text))
        if names:
            found[rel] = names
    return found


def test_every_document_names_the_same_venv():
    found = _named()
    assert found, "no document names a virtualenv; this test has stopped testing"
    everywhere = set().union(*found.values())
    assert len(everywhere) == 1, (
        f"the WSL virtualenv is named {sorted(everywhere)} in different places: {found}. "
        f"One of them is a path nobody creates.")


def test_the_skip_messages_name_a_venv_at_all():
    """A message that dropped the path would pass the test above by naming nothing."""
    for rel in ("tests/test_paths.py", "tests/test_context.py", "tests/test_tools.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert VENV.search(text), f"{rel}'s skip reason no longer names an interpreter"


def test_contributing_installs_the_dev_extra():
    """The venv the skip messages point at has to be one that actually has pytest.

    README installs the runtime only, which is right for someone running the server and
    was how this environment came to have no pytest at all. CONTRIBUTING is what a
    contributor follows, so `[dev]` belongs there -- and `pyproject.toml` is where pytest
    is listed, so the two are checked against each other rather than both trusted.
    """
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "[dev]" in contributing, "CONTRIBUTING.md no longer installs the dev extra"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dev = [" in pyproject and "pytest" in pyproject, (
        "the dev extra CONTRIBUTING.md installs does not define pytest")
