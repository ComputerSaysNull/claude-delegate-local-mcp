"""A setting named only in prose is not a setting anything reads.

`_unread_fields` used to scan each module as raw text and collect every identifier-shaped
word, so a name written in a comment or a docstring counted as a use. The first thing that
cost was `dispatch_timeout`: unread by every module, and named in two comments that said
exactly that. The sentence documenting the setting as dead was the sole reason the
reference rendered it as a live knob.

That is under-marking, and `_unread_fields`'s own docstring calls it the dangerous
direction -- over-marking is visible and gets fixed, under-marking restores the bug the
scan exists to prevent.

Both directions are asserted, per the project's rule that one alone is not a test: a scan
that dropped every token would pass the prose cases here while marking all 42 settings, so
the real uses are pinned too. Nothing below asserts anything about `dispatch_timeout`
against the live source -- enforcing it makes the field genuinely read, and a test written
that way would stop testing one commit later while still passing.

Named after the bug, per the project's convention. Companion to
test_inert_settings_are_marked.py, which asserts the marker appears and clears.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import gen_config_docs as gen  # noqa: E402

FIELD = "sandbox_enabled"


def scan_with(tmp_path, monkeypatch, body: str) -> set[str]:
    """Run the real scan against a synthetic source tree containing only `body`."""
    fake = tmp_path / "src" / "claude_delegate_local"
    fake.mkdir(parents=True)
    (fake / "config.py").write_text("# the scan skips this file\n", encoding="utf-8")
    (fake / "mentions_it.py").write_text(body, encoding="utf-8")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    return gen._unread_fields()


# --- prose is not a use -------------------------------------------------------------


def test_a_field_named_only_in_a_comment_is_still_unread(tmp_path, monkeypatch):
    body = f"# {FIELD} is declared in config and is consumed nowhere.\nx = 1\n"
    assert FIELD in scan_with(tmp_path, monkeypatch, body)


def test_a_field_named_only_in_a_docstring_is_still_unread(tmp_path, monkeypatch):
    """The exact shape that caused it: loop.py's docstring, naming the gap it describes."""
    body = f'"""No outer deadline here. {FIELD} exists and is consumed nowhere."""\nx = 1\n'
    assert FIELD in scan_with(tmp_path, monkeypatch, body)


def test_a_field_named_only_in_a_string_literal_is_still_unread(tmp_path, monkeypatch):
    body = f'msg = "set {FIELD} to change this"\n'
    assert FIELD in scan_with(tmp_path, monkeypatch, body)


def test_a_field_named_only_in_fstring_text_is_still_unread(tmp_path, monkeypatch):
    """Literal text inside an f-string is prose; the expression beside it is not."""
    body = f'n = 1\nmsg = f"{{n}} -- {FIELD} is not read"\n'
    assert FIELD in scan_with(tmp_path, monkeypatch, body)


# --- the other direction: real uses must survive -------------------------------------


def test_a_field_read_in_code_is_not_unread(tmp_path, monkeypatch):
    """Without this, a scan that dropped every token would pass every case above."""
    assert FIELD not in scan_with(tmp_path, monkeypatch, f"x = cfg.{FIELD}\n")


def test_a_field_read_inside_an_fstring_expression_is_not_unread(tmp_path, monkeypatch):
    """Over-correction guard: an expression inside an f-string is code, not prose."""
    body = f'msg = f"{{cfg.{FIELD}}}"\n'
    assert FIELD not in scan_with(tmp_path, monkeypatch, body)


def test_the_scan_does_not_mark_everything(tmp_path, monkeypatch):
    """A scan returning no identifiers at all would mark every setting and look plausible."""
    unread = scan_with(tmp_path, monkeypatch, f"x = cfg.{FIELD}\n")
    assert FIELD not in unread
    assert unread, "every other setting is genuinely unread in this tree"


# --- the parser helper itself --------------------------------------------------------


def test_code_identifiers_separates_code_from_prose():
    """Also pins the f-string case across versions: before 3.12 an f-string is one
    STRING token, so a tokenising scan would miss `golf` and mark a real read unread."""
    source = (
        "# alpha\n"
        '"""bravo"""\n'
        'charlie = "delta"\n'
        'echo = f"foxtrot {golf}"\n'
    )
    found = gen._code_identifiers(source)
    assert {"charlie", "echo", "golf"} <= found
    assert not ({"alpha", "bravo", "delta", "foxtrot"} & found)
