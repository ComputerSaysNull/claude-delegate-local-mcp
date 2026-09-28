"""`test_provision.py`'s `drive` loaded the developer's real `.env`.

`provision.main` builds its config with `config.load()`, which discovers `<repo>/.env`. The
tests driving it named no env file, so two of them passed on a clean checkout and failed on
a machine whose `.env` carried a refused line -- a retired setting, say -- while CI, having
no `.env`, never saw it. A green local run was partly a statement about that machine.

`drive` now names an empty env file for the call. The planted `.env` here stands in for the
developer's: the old `drive`, loaded from the baseline commit by path, fails on it, and the
new one does not.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from conftest import BASELINE_MISSING, REPO, baseline_blob

from claude_delegate_local import config

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="UNPROVEN BY THIS RUN -- `provision.main` refuses to run on Windows",
)

REFUSED_LINE = "DELEGATE_TOOL_CALL_TEMPERATURE=0.2\n"


def _load(source: str, where: Path, name: str):
    path = where / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A repository root whose `.env` carries a line `config.load` refuses."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text(REFUSED_LINE, encoding="utf-8")
    monkeypatch.setattr(config, "REPO_ROOT", root)
    monkeypatch.delenv(config.ENV_FILE_VAR, raising=False)
    (tmp_path / "permitted").mkdir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setenv("DELEGATE_WORKSPACE_ROOTS", str(tmp_path / "permitted"))
    return tmp_path


def test_the_planted_line_is_refused(planted):
    """Negative control: without this, both tests below would pass on a harmless `.env`."""
    with pytest.raises(config.ConfigError, match="retired"):
        config.load()


@posix_only
def test_the_old_drive_read_the_planted_env(planted, capsys):
    """`main` reports a refused config on stderr and exits 1, the refusal's own code, so
    the tell is which message came back rather than the exit status."""
    old = baseline_blob("tests/test_provision.py")
    if old is None:
        pytest.skip(BASELINE_MISSING)
    module = _load(old, planted, "old_test_provision")

    code, text = module.drive([str(planted / "elsewhere")])

    assert code == 1
    assert "outside every workdir root" not in text
    assert "retired" in capsys.readouterr().err


@posix_only
def test_the_new_drive_does_not(planted):
    new = (REPO / "tests" / "test_provision.py").read_text(encoding="utf-8")
    module = _load(new, planted, "new_test_provision")

    code, text = module.drive([str(planted / "elsewhere")])

    assert code == 1
    assert "outside every workdir root" in text
