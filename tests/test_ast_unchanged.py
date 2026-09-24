"""`ast_unchanged`: what makes two modules provably the same, and what does not.

The check exists to turn "only comments changed" from a claim into a measurement. The
`normalised` tests exercise it directly -- a comment and a reworded docstring are
invisible, a changed literal and a renamed variable are not. The CLI tests run the real
script against a throwaway repository, because the interesting failures live in the git
seam: a path absent at the base counts as changed, and a git failure is an error rather
than a clean pass.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ast_unchanged.py"

_spec = importlib.util.spec_from_file_location("ast_unchanged", SCRIPT)
assert _spec and _spec.loader
ast_unchanged = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ast_unchanged)


# --- normalised: what is invisible and what is not -------------------------------


def test_a_comment_between_functions_is_invisible():
    before = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    after = ("def a():\n    return 1\n\n# a comment between them\n\n"
             "def b():\n    return 2\n")
    assert ast_unchanged.normalised(before) == ast_unchanged.normalised(after)


def test_a_reworded_function_docstring_is_invisible():
    before = 'def f():\n    """The old words."""\n    return 1\n'
    after = 'def f():\n    """The new words, which say it better."""\n    return 1\n'
    assert ast_unchanged.normalised(before) == ast_unchanged.normalised(after)


def test_a_reworded_module_docstring_is_invisible():
    before = '"""The old module words."""\n\nx = 1\n'
    after = '"""The new module words."""\n\nx = 1\n'
    assert ast_unchanged.normalised(before) == ast_unchanged.normalised(after)


def test_changing_a_literal_is_visible():
    """The negative control: the check can fail, which is what proves it is a check."""
    assert ast_unchanged.normalised("x = 1\n") != ast_unchanged.normalised("x = 2\n")


def test_renaming_a_local_is_visible():
    assert ast_unchanged.normalised("x = 1\n") != ast_unchanged.normalised("y = 1\n")


# --- the CLI against a throwaway repository --------------------------------------

MOD_ORIGINAL = (
    '"""A module with a value and a function."""\n'
    "\n"
    "VALUE = 1\n"
    "\n"
    "\n"
    'def answer():\n'
    '    """Return the value."""\n'
    "    return VALUE\n"
)

MOD_COMMENTED = (
    '"""A module with a value and a function."""\n'
    "\n"
    "VALUE = 1\n"
    "\n"
    "\n"
    'def answer():\n'
    '    """Return the value."""\n'
    "    # the whole point of the function\n"
    "    return VALUE\n"
)

MOD_LITERAL = (
    '"""A module with a value and a function."""\n'
    "\n"
    "VALUE = 2\n"
    "\n"
    "\n"
    'def answer():\n'
    '    """Return the value."""\n'
    "    return VALUE\n"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", check=True,
    )


def _repo(tmp_path: Path) -> Path:
    """A throwaway git repository with one committed module."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ast@example.com")
    _git(tmp_path, "config", "user.name", "AST Check")
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text(MOD_ORIGINAL, encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def _run_script(repo: Path, *paths: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *paths], cwd=repo, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )


def test_a_comment_only_edit_exits_zero(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "pkg" / "mod.py").write_text(MOD_COMMENTED, encoding="utf-8")

    result = _run_script(repo, "src/pkg/mod.py")

    assert result.returncode == 0
    assert "1 file(s) unchanged" in result.stdout


def test_a_literal_edit_exits_one_and_names_the_path(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "pkg" / "mod.py").write_text(MOD_LITERAL, encoding="utf-8")

    result = _run_script(repo, "src/pkg/mod.py")

    assert result.returncode == 1
    assert "src/pkg/mod.py" in result.stdout


def test_a_newly_added_module_exits_one(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "pkg" / "new.py").write_text("x = 1\n", encoding="utf-8")

    result = _run_script(repo, "src/pkg/new.py")

    assert result.returncode == 1
    assert "src/pkg/new.py" in result.stdout


def test_an_untracked_module_is_found_without_naming_it(tmp_path):
    """With no paths, discovery must see a new file git does not track yet."""
    repo = _repo(tmp_path)
    (repo / "src" / "pkg" / "new.py").write_text("x = 1\n", encoding="utf-8")

    result = _run_script(repo)

    assert result.returncode == 1
    assert "src/pkg/new.py" in result.stdout
