"""Rewording a pytest marker left a provisioned environment reading stale.

`dependency_hash` hashed the whole bytes of `pyproject.toml`, so a change the build
backend never reads -- a marker description under `[tool.pytest.ini_options]` -- moved the
digest. `is_current` then withheld `$DELEGATE_PYTHON`, so `run_bash` could run no test at
all, with nothing about the project's dependencies changed. The digest must cover what the
build reads and nothing else.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

from pathlib import Path

from claude_delegate_local import provision


def project(tmp_path: Path, body: str) -> Path:
    """One project whose whole declaration is `body`, written to a fresh pyproject.toml."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(body, encoding="utf-8")
    return root


def test_rewording_a_pytest_marker_description_leaves_the_hash_unchanged(tmp_path):
    """The bug: a marker's prose is not a dependency, so it must not look like one.

    A digest that moved here costs a re-provision of the wrong kind: the environment is
    fine, but `interpreter_for` withholds it and the delegation runs no test at all.
    """
    body = (
        '[project]\nname = "demo"\nversion = "0.0.0"\n\n'
        "[tool.pytest.ini_options]\n"
        'markers = [\n    "slow: marks tests as slow",\n]\n'
    )
    root = project(tmp_path, body)
    before = provision.dependency_hash(str(root))

    (root / "pyproject.toml").write_text(
        body.replace("marks tests as slow", "marks tests as deliberately slow"),
        encoding="utf-8",
    )

    assert provision.dependency_hash(str(root)) == before


def test_correcting_the_nested_deselect_list_leaves_the_hash_unchanged(tmp_path):
    """`[tool.delegate-local] nested-deselect` is read at call time, so docs/ARCHITECTURE.md
    promises that correcting it needs no re-provision -- a promise the whole-file digest
    broke."""
    body = (
        '[project]\nname = "demo"\nversion = "0.0.0"\n\n'
        '[tool.delegate-local]\nnested-deselect = ["tests/test_a.py::test_one"]\n'
    )
    root = project(tmp_path, body)
    before = provision.dependency_hash(str(root))

    (root / "pyproject.toml").write_text(body.replace("test_one", "test_two"), encoding="utf-8")

    assert provision.dependency_hash(str(root)) == before


def test_changing_project_dependencies_still_moves_the_hash(tmp_path):
    """The direction with teeth: a real dependency edit must not read as current.

    Stale dependencies do not error -- they pass against the wrong versions and return the
    clean exit code ADR-0007 says to believe -- so this is the half that has to keep firing.
    """
    root = project(
        tmp_path,
        '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = ["httpx>=0.28"]\n',
    )
    before = provision.dependency_hash(str(root))

    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\ndependencies = ["httpx>=0.29"]\n',
        encoding="utf-8",
    )

    assert provision.dependency_hash(str(root)) != before


def test_changing_an_unlisted_tool_table_still_moves_the_hash(tmp_path):
    """The denylist is a denylist on purpose: an unknown table still counts.

    A mistake in the list costs one re-provision (a false "stale"); a table silently
    dropped the other way is a false "fresh", which is the one direction not allowed.
    """
    root = project(
        tmp_path,
        '[project]\nname = "demo"\nversion = "0.0.0"\n\n'
        '[tool.somebackend]\nsetting = "one"\n',
    )
    before = provision.dependency_hash(str(root))

    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\n\n'
        '[tool.somebackend]\nsetting = "two"\n',
        encoding="utf-8",
    )

    assert provision.dependency_hash(str(root)) != before
