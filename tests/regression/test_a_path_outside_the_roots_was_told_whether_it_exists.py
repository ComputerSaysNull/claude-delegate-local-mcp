"""A path outside every root was told whether it exists.

`resolve_search_root` and `resolve_workdir` tested existence before the roots, so a
missing path anywhere on the host got "does not exist" and an existing one got "outside
every root" -- a small oracle about the host's filesystem, handed to a caller with no
business asking. `_resolve_one` has always done it the other way round, for exactly that
reason, and now all three agree: the roots first, and existence only inside them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import paths
from claude_delegate_local.config import Config

pytestmark = pytest.mark.skipif(os.name != "posix", reason="layer 1 needs a POSIX filesystem")

MISSING_OUTSIDE = "/nonexistent-delegate-probe/deeper"


def _layer(excinfo) -> str:
    return excinfo.value.refusals[0].layer


def test_search_root_checks_the_roots_before_existence(tmp_path: Path):
    cfg = Config(workspace_roots=(str(tmp_path),))  # type: ignore[arg-type]
    with pytest.raises(paths.PathRefused) as excinfo:
        paths.resolve_search_root(cfg, MISSING_OUTSIDE)
    assert _layer(excinfo) == paths.LAYER_ROOTS
    assert "does not exist" not in str(excinfo.value)


def test_workdir_checks_the_roots_before_existence(tmp_path: Path):
    cfg = Config(workspace_roots=(str(tmp_path),))  # type: ignore[arg-type]
    with pytest.raises(paths.PathRefused) as excinfo:
        paths.resolve_workdir(cfg, MISSING_OUTSIDE)
    assert _layer(excinfo) == paths.LAYER_ROOTS
    assert "does not exist" not in str(excinfo.value)


def test_a_missing_path_inside_a_root_still_says_so(tmp_path: Path):
    cfg = Config(workspace_roots=(str(tmp_path),))  # type: ignore[arg-type]
    with pytest.raises(paths.PathRefused) as excinfo:
        paths.resolve_search_root(cfg, str(tmp_path / "missing"))
    assert "does not exist" in str(excinfo.value)
    with pytest.raises(paths.PathRefused) as excinfo:
        paths.resolve_workdir(cfg, str(tmp_path / "missing"))
    assert "does not exist" in str(excinfo.value)
