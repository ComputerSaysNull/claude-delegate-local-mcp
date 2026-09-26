"""`ast_unchanged` strips every docstring before comparing ASTs, but the docstring
of a function decorated with `@mcp.tool`, `@mcp.tool(...)` or `@mcp.resource(...)`
is that MCP tool's description -- contract text a model reads. A comment pass that
rewords such a docstring is therefore wrongly reported unchanged, and this test
pins that docstring as behaviour rather than prose.
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


# --- an MCP tool's docstring is contract text, not prose -------------------------


def test_a_reworded_mcp_tool_async_docstring_is_visible():
    before = (
        'import mcp\n'
        '@mcp.tool\n'
        'async def get_weather(city: str) -> str:\n'
        '    """Return the current forecast for a city."""\n'
        '    return "sunny"\n'
    )
    after = (
        'import mcp\n'
        '@mcp.tool\n'
        'async def get_weather(city: str) -> str:\n'
        '    """Return the forecast, with the humidity too."""\n'
        '    return "sunny"\n'
    )
    assert _differ(after, before)


def test_a_reworded_mcp_tool_call_docstring_is_visible():
    before = (
        'import mcp\n'
        '@mcp.tool(title="weather")\n'
        'def get_weather(city: str) -> str:\n'
        '    """Return the current forecast for a city."""\n'
        '    return "sunny"\n'
    )
    after = (
        'import mcp\n'
        '@mcp.tool(title="weather")\n'
        'def get_weather(city: str) -> str:\n'
        '    """Return the forecast, with the humidity too."""\n'
        '    return "sunny"\n'
    )
    assert _differ(after, before)


def test_a_reworded_mcp_resource_docstring_is_visible():
    before = (
        'import mcp\n'
        '@mcp.resource("delegate://x")\n'
        'def read_x() -> str:\n'
        '    """Return the contents of x."""\n'
        '    return "x"\n'
    )
    after = (
        'import mcp\n'
        '@mcp.resource("delegate://x")\n'
        'def read_x() -> str:\n'
        '    """Return the contents of x, in full."""\n'
        '    return "x"\n'
    )
    assert _differ(after, before)


# --- other docstrings stay invisible ---------------------------------------------


def test_a_reworded_ordinary_function_docstring_is_invisible():
    before = 'def f():\n    """The old words."""\n    return 1\n'
    after = 'def f():\n    """The new words, which say it better."""\n    return 1\n'
    assert ast_unchanged.normalised(before) == ast_unchanged.normalised(after)


def test_a_reworded_functools_cache_docstring_is_invisible():
    before = (
        'import functools\n'
        '@functools.cache\n'
        'def f(x: int) -> int:\n'
        '    """The old words."""\n'
        '    return x\n'
    )
    after = (
        'import functools\n'
        '@functools.cache\n'
        'def f(x: int) -> int:\n'
        '    """The new words, which say it better."""\n'
        '    return x\n'
    )
    assert ast_unchanged.normalised(before) == ast_unchanged.normalised(after)


def test_a_reworded_module_docstring_is_invisible():
    before = '"""The old module words."""\n\nx = 1\n'
    after = '"""The new module words."""\n\nx = 1\n'
    assert ast_unchanged.normalised(before) == ast_unchanged.normalised(after)
