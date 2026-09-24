#!/usr/bin/env python3
"""Prove a comment pass changed no behaviour, by comparing stripped ASTs.

A comment pass rewrites comments and docstrings in `src/` modules. "Only comments
changed" is a claim made by the party doing the changing, which is exactly the claim
worth checking. This turns it into a measurement: for each module it reads the old text
from a git revision and the new text from the working tree, removes every docstring, and
compares the two `ast.dump` outputs. Comments never reach the AST, so a pass that touched
only comments and docstrings produces identical dumps; anything else -- a literal
changed, a variable renamed -- makes them differ.

It parses source *text* only. It never imports a module or compiles it to bytecode: a
cached `.pyc` is validated on (mtime, size) and can be stale, so trusting one would
report a change that is not there, or miss one that is.

Usage:  python scripts/ast_unchanged.py [--base REV] [PATH ...]

With no PATHs, every `*.py` under `src/` that differs from base or is untracked is
checked (working tree against base). Exit codes: 0 all unchanged, 1 at least
one changed, 2 a git command failed. A failed git command is never read as "nothing
changed" -- it is an error, because a check that treats an unreachable repository as
clean is a check that cannot fail.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path


def normalised(source: str) -> str:
    """`ast.dump` of `source` with every docstring removed.

    Removes the first statement of the module and of every class, function and async
    function when it is an `ast.Expr` whose value is an `ast.Constant` holding a `str`;
    if removing it empties that body, an `ast.Pass()` is put in its place so the node is
    still well formed. Returns `ast.dump` with default arguments, so no line numbers or
    positions -- those are exactly what a comment pass shifts, and they are not
    behaviour.
    """
    tree = ast.parse(source)
    _strip_docstrings(tree)
    return ast.dump(tree)


def _strip_docstrings(node: ast.AST) -> None:
    """Remove the docstring from `node`, then recurse into its children."""
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(
            body[0].value, ast.Constant
        ) and isinstance(body[0].value.value, str):
            del body[0]
            if not body:
                node.body = [ast.Pass()]
    for child in ast.iter_child_nodes(node):
        _strip_docstrings(child)


def _git(*args: str, required: bool = True) -> str | None:
    """Run `git` and return stdout; a required failure is an error.

    The repository is the current working directory, so this works both when run from
    the checkout and when a test hands it a throwaway repository. `check=False` is
    deliberate -- a failure is inspected here rather than left for a traceback to name.
    """
    proc = subprocess.run(
        ["git", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        if required:
            sys.stderr.write(f"git {' '.join(args)} failed:\n{(proc.stderr or '').strip()}\n")
            raise SystemExit(2)
        return None
    return proc.stdout


def _old_text(base: str, path: str) -> str | None:
    """The module's text at `base`, or None if it did not exist there."""
    return _git("show", f"{base}:{path}", required=False)


def _new_text(path: str) -> str | None:
    """The module's text in the working tree, or None if it was deleted."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _unchanged(base: str, path: str) -> bool:
    """True if the module's stripped AST is the same at `base` and in the tree."""
    old = _old_text(base, path)
    new = _new_text(path)
    if old is None or new is None:
        return False
    return normalised(old) == normalised(new)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prove a comment pass changed no behaviour by comparing stripped ASTs."
    )
    parser.add_argument("--base", default="HEAD",
                        help="revision to compare the working tree against")
    parser.add_argument("paths", nargs="*",
                        help="python files to check (default: changed *.py under src/)")
    args = parser.parse_args(argv)

    _git("rev-parse", "--verify", f"{args.base}^{{commit}}", required=True)

    if args.paths:
        paths = args.paths
    else:
        # `diff` never lists an untracked file, so a pass that added a module would
        # compare nothing and exit 0. `ls-files --others` is what sees it.
        listing = _git("diff", "--name-only", args.base, "--", "src", required=True)
        listing += _git("ls-files", "--others", "--exclude-standard", "--", "src",
                        required=True)
        paths = sorted({line for line in listing.splitlines() if line.endswith(".py")})

    changed = [path for path in paths if not _unchanged(args.base, path)]
    if changed:
        for path in changed:
            print(path)
        return 1
    print(f"{len(paths)} file(s) unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
