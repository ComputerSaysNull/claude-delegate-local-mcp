"""Naming a top-level symbol instead of guessing at a line number.

A line range into a 3,500-line module lands mid-function as often as not, and goes stale
the moment anything above it changes. A symbol names the unit the caller actually wants:
the lines of that one top-level def, async def or class, decorators included.

Every refusal here is a sentence the caller can act on -- the syntax error that made the
file unparseable, the parent that holds a nested name, the names that do exist. A refusal
that only says "not found" costs another round trip.
"""

import pytest

from claude_delegate_local import symbols


def test_outline_lists_a_def_an_async_def_and_a_class_with_their_lines():
    text = (
        "import os\n"
        "\n"
        "\n"
        "def first():\n"
        "    return 1\n"
        "\n"
        "\n"
        "async def second():\n"
        "    return 2\n"
        "\n"
        "\n"
        "class Third:\n"
        "    pass\n"
    )
    found = symbols.outline(text)

    assert [(s.name, s.kind, s.start_line, s.end_line) for s in found] == [
        ("first", "def", 4, 5),
        ("second", "async def", 8, 9),
        ("Third", "class", 12, 13),
    ]


def test_a_decorated_function_starts_at_its_first_decorator():
    """The decorator is part of the unit: delivered without it, the function reads as
    undecorated, which is a different function."""
    text = "@retry\n@wraps(inner)\ndef guarded():\n    return 1\n"

    (found,) = symbols.outline(text)

    assert (found.start_line, found.end_line) == (1, 4)


def test_a_class_stops_at_its_last_line_not_at_its_body():
    text = "class Outer:\n    def method(self):\n        return 1\n\n\nx = 2\n"

    (found,) = symbols.outline(text)

    assert (found.start_line, found.end_line) == (1, 3)


def test_find_returns_the_lines_of_a_decorated_function_including_the_decorators():
    text = "x = 1\n\n\n@decorate\ndef target():\n    return 2\n"

    found = symbols.find(text, "target")

    assert (found.name, found.start_line, found.end_line) == ("target", 4, 6)


def test_a_nested_name_is_refused_and_the_symbol_that_holds_it_is_named():
    text = "class Outer:\n    def inner(self):\n        return 1\n"

    with pytest.raises(symbols.SymbolRefused) as e:
        symbols.find(text, "inner")

    message = str(e.value)
    assert "not top-level" in message
    assert "Outer" in message


def test_a_name_nested_in_a_function_names_the_function_that_holds_it():
    text = "def outer():\n    def inner():\n        return 1\n    return inner\n"

    with pytest.raises(symbols.SymbolRefused) as e:
        symbols.find(text, "inner")

    assert "outer" in str(e.value)


def test_a_missing_name_is_refused_and_the_names_that_do_exist_are_listed():
    text = "def alpha():\n    pass\n\ndef beta():\n    pass\n"

    with pytest.raises(symbols.SymbolRefused) as e:
        symbols.find(text, "gamma")

    message = str(e.value)
    assert "gamma" in message
    assert "alpha" in message and "beta" in message


def test_the_list_of_names_stops_at_ten_and_says_how_many_more():
    text = "".join(f"def f{i}():\n    pass\n\n" for i in range(14))

    with pytest.raises(symbols.SymbolRefused) as e:
        symbols.find(text, "nope")

    message = str(e.value)
    assert "f0" in message and "f9" in message
    assert "f10" not in message, "the list ran past its cap"
    assert "and 4 more" in message


def test_a_module_level_name_that_is_not_a_def_or_class_is_refused():
    """`x = 1` is top-level but has no lines of its own to deliver."""
    text = "x = 1\n\ndef f():\n    pass\n"

    with pytest.raises(symbols.SymbolRefused) as e:
        symbols.find(text, "x")

    assert "f" in str(e.value), "the reason must name what can be named instead"


def test_a_syntax_error_is_refused_and_says_so():
    with pytest.raises(symbols.SymbolRefused) as e:
        symbols.find("def broken(:\n    pass\n", "anything")

    assert "not valid Python" in str(e.value)


def test_a_file_with_no_top_level_symbols_has_an_empty_outline():
    assert symbols.outline("x = 1\n# just a value\n") == []


def test_an_empty_name_is_refused_rather_than_matching_nothing():
    with pytest.raises(symbols.SymbolRefused):
        symbols.find("def f():\n    pass\n", "")


def test_a_name_defined_twice_resolves_to_the_definition_python_binds():
    """The first definition of a name is dead code the moment the second appears: Python
    binds the last one, so delivering the first hands the model a function nothing calls.
    The outline lists the name once, at the definition that is live."""
    text = (
        "def handler():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def handler():\n"
        "    return 2\n"
    )

    found = symbols.find(text, "handler")

    assert (found.start_line, found.end_line) == (5, 6)

    listed = symbols.outline(text)

    assert [s.name for s in listed] == ["handler"], "a shadowed def is not a choice"
    assert (listed[0].start_line, listed[0].end_line) == (5, 6)
