"""A bare `name:` loaded cleanly where `docs/AGENTS.md` says the file is refused.

`agents.py` reads `declared = _scalar(raw.get("name", ""))` and then
`if declared and declared != name`. An absent key gives `""`; a key present with no value
gives `""` too, because YAML parses `name:` as null and `_scalar` renders it empty. So
present-but-empty is indistinguishable from absent, and the file loads.

`docs/AGENTS.md` promises otherwise: `name` is "Optional, but if present it **must equal the
filename**, or the file is refused".

`PLAN.md` filed this with a leaning towards changing the document, as the cheaper of the two.
This goes the other way, and the argument is eleven lines above the bug. `validate` refuses
an unknown key rather than ignoring it because "a typo would otherwise cost you the setting
in silence, and a setting that silently does nothing is the bug this format was rewritten to
prevent". A bare `name:` is that exact shape: someone typed the key meaning to set it, and
the value went missing. Accepting it is the bug the file's own error message names, so the
document was right and the code was wrong.

Refusing it costs nothing real either. A `name` that equals the filename is redundant, and
one that disagrees is already refused -- so no working file carries a bare `name:` on
purpose.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_delegate_local.agents import AgentError, validate
from claude_delegate_local.config import Config


def cfg(root: Path) -> Config:
    return Config(workspace_roots=(str(root),))  # type: ignore[arg-type]


def _validate(tmp_path: Path, raw: dict[str, str]):
    return validate(
        cfg(tmp_path), raw, "Body text.", name="reviewer",
        where=str(tmp_path / "reviewer.md"),
    )


def test_a_bare_name_is_refused(tmp_path: Path):
    """The bug. YAML gives null, `_scalar` gives "", and the check reads it as absent."""
    with pytest.raises(AgentError) as caught:
        _validate(tmp_path, {"name": None, "description": "d"})  # type: ignore[dict-item]
    assert "name" in str(caught.value)


def test_an_empty_string_name_is_refused_too(tmp_path: Path):
    """The other spelling of the same mistake -- `name: ""` rather than `name:`."""
    with pytest.raises(AgentError):
        _validate(tmp_path, {"name": "", "description": "d"})


def test_a_whitespace_name_is_refused(tmp_path: Path):
    """`name: "   "` is a value that names nothing, and stripping is what makes it empty."""
    with pytest.raises(AgentError):
        _validate(tmp_path, {"name": "   ", "description": "d"})


def test_an_absent_name_still_loads(tmp_path: Path):
    """The control, and the one this must not break: `name` is genuinely optional.

    A guard that refused an absent key would break every agent file in the repository,
    and would pass the three assertions above while doing it.
    """
    spec = _validate(tmp_path, {"description": "d"})
    assert spec.name == "reviewer"


def test_a_matching_name_still_loads(tmp_path: Path):
    """The other control: a correctly declared name is not collateral damage."""
    spec = _validate(tmp_path, {"name": "reviewer", "description": "d"})
    assert spec.name == "reviewer"
