"""Prefetch: what gets read, what gets skipped, and what the prompt looks like.

The failures worth guarding here are the quiet ones. A truncated file still produces a
confident answer. A silently dropped file produces an answer about five files that reads
exactly like an answer about six. A prompt that varies with caller order still works, and
just costs a prefix-cache miss nobody will ever attribute to it.

So the assertions are mostly negative: that a skipped file's contents appear *nowhere*,
that the accounting names what was dropped, and that two orderings produce identical
bytes.
"""

from __future__ import annotations

import os
import random

import pytest

from claude_delegate_local import context
from claude_delegate_local.config import Config
from claude_delegate_local.context import (
    SKIP_BINARY,
    SKIP_OVER_FILE_BUDGET,
    SKIP_OVER_TOTAL_BUDGET,
    SKIP_TOO_MANY_BYTES,
    prefetch,
)
from claude_delegate_local.paths import ResolvedPath

UNPROVEN = (
    "PREFETCH UNPROVEN BY THIS RUN -- this is not a pass. It reads real files through "
    "resolved POSIX paths, and the server runs in WSL. Run it there -- see "
    "CONTRIBUTING.md: wsl -d Ubuntu-24.04 -e bash -lc "
    "'cd <repo> && ~/.venvs/cdl/bin/python -m pytest tests/test_context.py'"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)


def cfg(**over) -> Config:
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def write(tmp_path, name: str, data: bytes | str) -> ResolvedPath:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)
    return ResolvedPath(given=str(p), posix=os.path.realpath(p))


# ---- the happy path ---------------------------------------------------------------


@posix_only
def test_a_file_is_read_and_inlined(tmp_path):
    item = write(tmp_path, "a.py", "def f():\n    return 1\n")
    result = prefetch(cfg(), (item,))
    assert [e.path for e in result.files] == [item.posix]
    assert "def f():" in result.block()
    assert result.skips == ()
    assert result.total_tokens > 0


@posix_only
def test_the_block_uses_the_resolved_path_not_the_one_the_caller_wrote(tmp_path):
    """Two spellings of one file must not render as two different prompts.

    The alternative loses the prefix cache for a reason that has nothing to do with what
    was asked -- which is the failure mode ADR-0011 exists to prevent, and it is silent.
    """
    real = write(tmp_path, "a.py", "x = 1\n")
    aliased = ResolvedPath(given=r"C:\somewhere\else\a.py", posix=real.posix)
    assert prefetch(cfg(), (aliased,)).block() == prefetch(cfg(), (real,)).block()


@posix_only
def test_a_markdown_file_carrying_fences_is_not_cut_short_by_them(tmp_path):
    """Why the file markers are not a markdown fence.

    A fenced block would end at the file's own first fence, and the model would read the
    rest of the file as prose addressed to it.
    """
    body = "# Doc\n\n```python\nprint(1)\n```\n\nafter the fence\n"
    item = write(tmp_path, "readme.md", body)
    block = prefetch(cfg(), (item,)).block()
    assert "after the fence" in block
    assert block.count(f"--- END FILE {item.posix} ---") == 1


# ---- skips: size ------------------------------------------------------------------


@posix_only
def test_a_file_over_the_byte_ceiling_is_skipped_without_being_read(tmp_path):
    item = write(tmp_path, "big.py", "A" * 5000)
    result = prefetch(cfg(max_file_read_bytes=1000), (item,))
    assert result.files == ()
    assert result.skips[0].kind == SKIP_TOO_MANY_BYTES


@posix_only
def test_an_over_budget_file_is_skipped_whole_and_none_of_it_appears(tmp_path):
    """The assertion that matters is the absence, not the skip.

    A truncating implementation passes any test that only checks a skip was recorded --
    it records one, and inlines a prefix anyway. Source cut mid-function is worse than
    absent, because the model will confidently repair code it never saw.
    """
    marker = "SENTINEL_TOKEN_THAT_MUST_NOT_APPEAR"
    item = write(tmp_path, "big.py", f"{marker}\n" + ("x = 1\n" * 5000))
    result = prefetch(cfg(max_file_tokens=10, max_total_prefetch_tokens=10), (item,))

    assert result.files == ()
    block = result.block()
    assert marker not in block, "a prefix of the file was inlined; it must be skipped whole"
    assert "x = 1" not in block
    assert result.skips[0].kind == SKIP_OVER_FILE_BUDGET


@posix_only
def test_the_skip_reason_reaches_the_prompt_as_well_as_the_accounting(tmp_path):
    """The model has to be told, or it answers about a file it never saw."""
    item = write(tmp_path, "big.py", "x = 1\n" * 5000)
    result = prefetch(cfg(max_file_tokens=10, max_total_prefetch_tokens=10), (item,))
    assert item.posix in result.block()
    assert result.accounting()["files_skipped"][0]["path"] == item.posix


# ---- skips: binary ----------------------------------------------------------------


@posix_only
def test_a_nul_byte_makes_a_python_file_binary(tmp_path):
    """Extension is not evidence. The allowlist admits .py; a .py can still be a blob."""
    item = write(tmp_path, "weird.py", b"print(1)\n\x00\x01\x02binary tail")
    result = prefetch(cfg(), (item,))
    assert result.files == ()
    assert result.skips[0].kind == SKIP_BINARY
    assert "NUL" in result.skips[0].reason


@posix_only
def test_a_utf16_json_file_is_binary(tmp_path):
    """The case extension alone cannot catch, and the one a BOM check alone would miss."""
    item = write(tmp_path, "config.json", '{"a": 1}'.encode("utf-16"))
    result = prefetch(cfg(), (item,))
    assert result.files == ()
    assert result.skips[0].kind == SKIP_BINARY


@posix_only
def test_latin1_bytes_are_binary_rather_than_silently_mangled(tmp_path):
    """The strict decode, asserted directly.

    Decoding with errors="replace" would hand the model a file full of U+FFFD and call it
    source, which is the truncation failure wearing a different hat.
    """
    item = write(tmp_path, "notes.txt", b"caf\xe9 au lait\n")
    result = prefetch(cfg(), (item,))
    assert result.files == ()
    assert result.skips[0].kind == SKIP_BINARY


@posix_only
def test_a_utf8_bom_is_stripped_rather_than_treated_as_content(tmp_path):
    item = write(tmp_path, "a.py", b"\xef\xbb\xbfx = 1\n")
    result = prefetch(cfg(), (item,))
    assert result.files[0].text.startswith("x = 1")


def test_valid_utf8_with_multibyte_characters_is_text():
    text, why = context.decode_text("# naïve — ok\n".encode())
    assert why == ""
    assert text is not None and "naïve" in text


# ---- the total budget -------------------------------------------------------------


# Each of these is ~149 estimated tokens of Python: comfortably under a 200-token
# per-file cap, so the *total* budget is what binds. The two caps have to be separated
# deliberately -- a file large enough to blow the total on its own is necessarily over
# the per-file cap too, and would be skipped by the wrong layer.
MEDIUM = "x = 1\n" * 92


@posix_only
def test_the_total_budget_stops_the_list_and_the_accounting_says_so(tmp_path):
    """Silent truncation of the *list* reads as full coverage, exactly like a truncated file."""
    items = [write(tmp_path, f"{n}.py", MEDIUM) for n in "abcd"]
    conf = cfg(max_file_tokens=200, max_total_prefetch_tokens=400)

    result = prefetch(conf, tuple(items))

    assert [e.path for e in result.files] == [items[0].posix, items[1].posix]
    kinds = {s.path: s.kind for s in result.skips}
    assert kinds[items[2].posix] == SKIP_OVER_TOTAL_BUDGET
    assert kinds[items[3].posix] == SKIP_OVER_TOTAL_BUDGET, "everything after is skipped too"
    # The two are skipped for the same reason but not by the same sentence: one did not
    # fit, the other never got the chance. A caller reading the accounting can tell.
    reasons = {s.path: s.reason for s in result.skips}
    assert "needs about" in reasons[items[2].posix]
    assert "already spent" in reasons[items[3].posix]


@posix_only
def test_the_cutoff_does_not_depend_on_the_order_the_caller_named_them(tmp_path):
    """Sorting before accumulating, not after.

    Accumulating in caller order would make the same six files return a different five
    depending on how they were listed, which is both unpredictable and a cache miss.
    """
    items = [write(tmp_path, f"{n}.py", MEDIUM) for n in "abcdef"]
    conf = cfg(max_file_tokens=200, max_total_prefetch_tokens=400)

    baseline = prefetch(conf, tuple(items))
    # Without this the test passes vacuously against a build that includes nothing, or
    # everything -- neither of which exercises a cutoff at all.
    assert 0 < len(baseline.files) < len(items), "the budget must actually bind here"

    for seed in (7, 11, 13):
        shuffled = list(items)
        random.Random(seed).shuffle(shuffled)
        assert [e.path for e in prefetch(conf, tuple(shuffled)).files] == [
            e.path for e in baseline.files
        ]


@posix_only
def test_a_shuffled_input_produces_a_byte_identical_block(tmp_path):
    """The whole point of the ordering rule, asserted on the bytes rather than the list."""
    items = [write(tmp_path, f"{n}.py", f"# {n}\nvalue = {i}\n") for i, n in enumerate("abcde")]
    baseline = prefetch(cfg(), tuple(items)).block()
    for seed in (1, 2, 3):
        shuffled = list(items)
        random.Random(seed).shuffle(shuffled)
        assert prefetch(cfg(), tuple(shuffled)).block().encode() == baseline.encode()


# ---- token accounting -------------------------------------------------------------


@posix_only
def test_json_and_python_of_equal_size_are_costed_differently(tmp_path):
    """Pins the ADR-0019 claim that the prefetch budget rests on.

    If the per-extension table stopped being consulted, every test above would still
    pass -- the budget would simply be wrong by up to a factor of two, and nothing would
    say so.
    """
    payload = "a" * 4000
    py = write(tmp_path, "same.py", payload)
    js = write(tmp_path, "same.json", payload)

    py_tokens = prefetch(cfg(), (py,)).files[0].est_tokens
    json_tokens = prefetch(cfg(), (js,)).files[0].est_tokens
    assert json_tokens > py_tokens, "punctuation-dense JSON must cost more tokens per byte"


@posix_only
def test_the_accounting_totals_match_what_was_included(tmp_path):
    a = write(tmp_path, "a.py", "x = 1\n" * 50)
    b = write(tmp_path, "b.py", "y = 2\n" * 50)
    result = prefetch(cfg(), (a, b))
    acc = result.accounting()
    assert acc["prefetch_tokens"] == sum(e.est_tokens for e in result.files)
    assert len(acc["files_read"]) == 2


def test_no_files_produces_no_block_and_no_noise():
    """The no-files shape has to stay clean: an empty header would still be a prefix."""
    result = prefetch(cfg(), ())
    assert result.block() == ""
    assert result.accounting()["prefetch_tokens"] == 0
