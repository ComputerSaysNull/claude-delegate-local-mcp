"""A `setup.cfg` change left a provisioned environment reading current (PLAN M13.9.b, .c).

`provision` records a digest of the project's dependency declaration, and `is_current`
withholds an environment whose declaration has moved since it was built. The digest was
taken over `pyproject.toml` alone. A project that declares its dependencies in `setup.cfg`
or `setup.py` could change them and still read current, which is the dangerous direction:
a delegation then tests against the wrong dependencies and returns the clean exit code
ADR-0007 says to trust. The converse was the other half: a project with no
`pyproject.toml` built fine and could never read current at all.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from claude_delegate_local import provision


@pytest.fixture
def project(tmp_path: Path) -> Path:
    # Bytes, not text: `write_text` on Windows writes CRLF itself, which would make the
    # line-ending case below compare CRLF against CRLF-doubled rather than LF against CRLF.
    (tmp_path / "pyproject.toml").write_bytes(b"[project]\nname = 'p'\n")
    (tmp_path / "setup.cfg").write_bytes(b"[options]\ninstall_requires = a\n")
    return tmp_path


@pytest.mark.parametrize("name", ["setup.cfg", "setup.py"])
def test_every_declaration_moves_the_digest(project, name):
    before = provision.dependency_hash(str(project))
    (project / name).write_text("# a changed declaration\n", encoding="utf-8")
    assert provision.dependency_hash(str(project)) != before


def test_a_project_without_pyproject_has_a_digest(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n",
                                       encoding="utf-8")
    assert provision.dependency_hash(str(tmp_path)) is not None


def test_a_project_with_no_declaration_still_has_none(tmp_path):
    """The control on the half above: nothing to hash is still "cannot tell"."""
    assert provision.dependency_hash(str(tmp_path)) is None


def test_a_pyproject_only_project_keeps_the_digest_it_always_had(tmp_path):
    """So every environment recorded before this change still reads current. Pinned as the
    formula rather than a stored value, because the formula is what the old records hold."""
    body = b"[project]\nname = 'p'\n"
    (tmp_path / "pyproject.toml").write_bytes(body)
    assert provision.dependency_hash(str(tmp_path)) == hashlib.sha256(body).hexdigest()


def test_line_endings_still_do_not_move_it(project):
    """The CRLF fix `dependency_hash` documents must survive hashing more than one file."""
    before = provision.dependency_hash(str(project))
    cfg = project / "setup.cfg"
    cfg.write_bytes(cfg.read_bytes().replace(b"\n", b"\r\n"))
    assert provision.dependency_hash(str(project)) == before
