"""Naming a top-level Python symbol instead of guessing at a line range.

A line range into a large module lands mid-function as often as not, and goes stale the
moment anything above it changes. A symbol names the unit the caller actually wants -- the
lines of that one top-level `def`, `async def` or `class`, decorators included -- and the
range is derived from the file's own syntax rather than from a guess.

Pure functions over text already read: this module never opens a file, so its caller keeps
the invariant that proving a path and opening it are one operation (ADR-0049). It is
independent of `context.py` because `read_file` will want the same answers.

Every refusal is a sentence the caller can act on: the syntax error that made the file
unparseable, the parent that holds a nested name, the names that do exist.
"""

import ast
from dataclasses import dataclass

# How many names a refusal lists. Enough to recognise the file, few enough to read in a
# skip reason that also has to say why the file was left out.
NAMES_LISTED = 10

_Definitions = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


class SymbolRefused(Exception):
    """A symbol that cannot be delivered, with the reason and the remedy in one string."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Symbol:
    """One top-level definition, and the lines it occupies."""

    name: str
    kind: str  # "def", "async def" or "class"
    start_line: int  # 1-based, and the first decorator when it has one
    end_line: int  # 1-based and inclusive


def _parse(text: str) -> ast.Module:
    """The module tree, or a `SymbolRefused` saying the file has no symbols to name."""
    try:
        return ast.parse(text)
    except SyntaxError as e:
        where = f"line {e.lineno}" if e.lineno is not None else "an unnamed line"
        raise SymbolRefused(
            f"the file is not valid Python, so its symbols cannot be read: {e.msg} "
            f"({where}). Name a line range instead."
        ) from e
    except (ValueError, RecursionError) as e:
        # A NUL byte past the sniff, or an expression nested past the parser's limit.
        raise SymbolRefused(
            f"the file is not valid Python, so its symbols cannot be read: {e}. "
            "Name a line range instead."
        ) from e


def _start(node: _Definitions) -> int:
    """The first line of the definition: its first decorator, when it has one.

    A decorated function delivered without its decorators reads as a different function.
    """
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def _end(node: _Definitions) -> int:
    return node.end_lineno or node.lineno


def _outline(tree: ast.Module) -> list[Symbol]:
    found: list[Symbol] = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            found.append(Symbol(node.name, "async def", _start(node), _end(node)))
        elif isinstance(node, ast.FunctionDef):
            found.append(Symbol(node.name, "def", _start(node), _end(node)))
        elif isinstance(node, ast.ClassDef):
            found.append(Symbol(node.name, "class", _start(node), _end(node)))
    # A name defined more than once is one name: Python binds the last definition, so the
    # earlier ones are dead code and offering them as choices offers ones that do not exist.
    effective: dict[str, int] = {}
    for index, symbol in enumerate(found):
        effective[symbol.name] = index
    return [s for i, s in enumerate(found) if effective[s.name] == i]


def outline(text: str) -> list[Symbol]:
    """Every top-level def, async def and class in `text`, in the file's own order.

    Raises `SymbolRefused` when the text does not parse: a file with no parseable
    structure has no outline, and an empty list would read as one with no symbols.
    """
    return _outline(_parse(text))


def _holder(tree: ast.Module, name: str) -> str | None:
    """The top-level symbol a nested `name` sits inside, when there is one."""
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(node):
            if child is node:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and child.name == name:
                return node.name
    return None


def find(text: str, name: str) -> Symbol:
    """The top-level definition called `name`, or a `SymbolRefused` saying why not.

    Only the module body counts. A method or a nested function is refused with the
    top-level symbol that holds it named, so the caller can name that instead -- or take a
    line range, which reaches any lines at all.
    """
    if not name:
        raise SymbolRefused(
            "the symbol name is empty, so it names nothing. Give one top-level function "
            "or class name, or name the file plainly."
        )
    tree = _parse(text)
    found = _outline(tree)
    for symbol in found:
        if symbol.name == name:
            return symbol

    holder = _holder(tree, name)
    if holder is not None:
        raise SymbolRefused(
            f"{name!r} is not top-level: it is defined inside {holder!r}, which is the "
            "symbol to name instead. A line range reaches the nested one."
        )

    if not found:
        raise SymbolRefused(
            f"there is no top-level def or class named {name!r}, and this file defines "
            "none. It may be nested inside another symbol, or defined under an `if` at "
            "module level; a line range reaches it."
        )
    listed = ", ".join(s.name for s in found[:NAMES_LISTED])
    more = len(found) - NAMES_LISTED
    tail = f", and {more} more" if more > 0 else ""
    raise SymbolRefused(
        f"there is no top-level def or class named {name!r}. The top-level symbols this "
        f"file defines are: {listed}{tail}. It may be nested inside one of them, which a "
        "line range reaches."
    )
