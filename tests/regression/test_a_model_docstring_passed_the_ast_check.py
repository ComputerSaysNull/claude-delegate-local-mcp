"""`ast_unchanged` strips every docstring before comparing ASTs, but the docstring of
a pydantic model's class is that model's description in an MCP tool's input-schema --
contract text a model reads. `FileRange(BaseModel)` in
`src/claude_delegate_local/server.py` sits in a tool signature, so a comment pass that
re-wrapped its docstring was once wrongly reported unchanged. This test pins a
`BaseModel` subclass's docstring as behaviour rather than prose.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ast_unchanged.py"

_spec = importlib.util.spec_from_file_location("ast_unchanged", SCRIPT)
assert _spec and _spec.loader
ast_unchanged = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ast_unchanged)


def _differ(after: str, before: str) -> bool:
    return ast_unchanged.normalised(after) != ast_unchanged.normalised(before)


# --- a pydantic model's class docstring is contract text, not prose ---------------


def test_a_reworded_basemodel_class_docstring_is_visible():
    before = (
        'from pydantic import BaseModel\n'
        'class FileRange(BaseModel):\n'
        '    """A range of lines in a file."""\n'
        '    start: int\n'
        '    end: int\n'
    )
    after = (
        'from pydantic import BaseModel\n'
        'class FileRange(BaseModel):\n'
        '    """A range of lines in a file, inclusive of both ends."""\n'
        '    start: int\n'
        '    end: int\n'
    )
    assert _differ(after, before)


def test_a_reworded_pydantic_basemodel_class_docstring_is_visible():
    before = (
        'import pydantic\n'
        'class FileRange(pydantic.BaseModel):\n'
        '    """A range of lines in a file."""\n'
        '    start: int\n'
        '    end: int\n'
    )
    after = (
        'import pydantic\n'
        'class FileRange(pydantic.BaseModel):\n'
        '    """A range of lines in a file, inclusive of both ends."""\n'
        '    start: int\n'
        '    end: int\n'
    )
    assert _differ(after, before)


# --- other class docstrings stay invisible ---------------------------------------


def test_a_reworded_plain_class_docstring_is_invisible():
    before = 'class Plain:\n    """The old words."""\n    x: int = 1\n'
    after = 'class Plain:\n    """The new words, which say it better."""\n    x: int = 1\n'
    assert ast_unchanged.normalised(before) == ast_unchanged.normalised(after)


def test_a_reworded_class_with_other_base_docstring_is_invisible():
    before = 'class Plain(Exception):\n    """The old words."""\n    pass\n'
    after = 'class Plain(Exception):\n    """The new words."""\n    pass\n'
    assert ast_unchanged.normalised(before) == ast_unchanged.normalised(after)
