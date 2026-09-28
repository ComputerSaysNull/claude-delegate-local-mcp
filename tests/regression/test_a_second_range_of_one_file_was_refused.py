"""A second `files[]` range of one file was refused as a duplicate.

ADR-0105 refused a second entry for a file already named, so a caller wanting two parts of
one file had to prefetch one and let the delegation read the other itself -- one
delegation spent 51 turns doing that. ADR-0106 takes several ranges: those that overlap or
touch merge into one span, so no line is sent twice, and a file also named whole counts as
the span from line 1. A merged span too big to send falls back to the ranges as named, so a
range that fits is still sent and what does not is skipped saying how to read it.
"""

from __future__ import annotations

import json
import os

import pytest
from test_a_large_file_could_not_be_prefetched_in_part import (
    UNPROVEN,
    big_file,
    dispatched,
    files_cfg,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)


def headers(sent: list) -> list[str]:
    prompt = json.dumps(sent[0])
    return [chunk.split(" ---")[0] for chunk in prompt.split("--- BEGIN FILE ")[1:]]


@posix_only
def test_two_separate_ranges_of_one_file_are_both_sent(tmp_path):
    target = big_file(tmp_path)
    sent: list = []
    result = dispatched(files_cfg(tmp_path), sent, files=[
        {"path": target, "start_line": 50, "end_line": 60},
        {"path": target, "start_line": 10, "end_line": 20},
    ])

    assert result["files_skipped"] == [], result["files_skipped"]
    found = headers(sent)
    assert len(found) == 2, found
    assert "lines 10-20 of 200" in found[0] and "lines 50-60 of 200" in found[1], found


@posix_only
@pytest.mark.parametrize(("first", "second", "merged"), [
    ((10, 30), (25, 40), "lines 10-40 of 200"),   # overlapping
    ((10, 20), (21, 30), "lines 10-30 of 200"),   # touching
    ((10, 30), (12, 15), "lines 10-30 of 200"),   # inside
])
def test_ranges_that_overlap_or_touch_become_one_span(tmp_path, first, second, merged):
    target = big_file(tmp_path)
    sent: list = []
    # Room for the merged span: it is judged by its own size, like any range.
    result = dispatched(files_cfg(tmp_path, max_file_tokens=200), sent, files=[
        {"path": target, "start_line": first[0], "end_line": first[1]},
        {"path": target, "start_line": second[0], "end_line": second[1]},
    ])

    assert result["files_skipped"] == [], result["files_skipped"]
    found = headers(sent)
    assert len(found) == 1 and merged in found[0], found
    assert json.dumps(sent[0]).count("\\tline 12\\n") == 1, "a line was sent twice"


def skipped(result) -> list[str]:
    return [s["reason"] for s in result["files_skipped"]]


# The cap in `files_cfg` is 50 tokens: about 21 of these lines fit, 31 do not.


@posix_only
def test_ranges_that_fit_are_sent_when_their_merge_does_not(tmp_path):
    """Each named range fits and their union does not: both are sent, no line twice."""
    target = big_file(tmp_path)
    sent: list = []
    result = dispatched(files_cfg(tmp_path), sent, files=[
        {"path": target, "start_line": 10, "end_line": 30},
        {"path": target, "start_line": 25, "end_line": 45},
    ])

    assert skipped(result) == [], skipped(result)
    found = headers(sent)
    assert ["lines 10-30" in found[0], "lines 31-45" in found[1]] == [True, True], found
    assert json.dumps(sent[0]).count("\\tline 27\\n") == 1, "a line was sent twice"


@posix_only
def test_a_range_that_fits_is_sent_when_the_range_holding_it_does_not(tmp_path):
    """The small range goes in; the rest of the large one is skipped loudly, with the way in."""
    target = big_file(tmp_path)
    sent: list = []
    result = dispatched(files_cfg(tmp_path), sent, files=[
        {"path": target, "start_line": 10, "end_line": 80},
        {"path": target, "start_line": 10, "end_line": 15},
    ])

    found = headers(sent)
    assert len(found) == 1 and "lines 10-15 of 200" in found[0], found
    reasons = skipped(result)
    assert len(reasons) == 1, reasons
    assert "lines 16-80" in reasons[0] and "read_file" in reasons[0], reasons
    assert "start_line=16, end_line=80" in reasons[0], reasons
    assert "lines 16-80" in json.dumps(sent[0]), "the model was not told what it is missing"


@posix_only
def test_a_range_of_a_file_too_big_to_send_whole_is_still_sent(tmp_path):
    """Named whole and by range: the range fits, the whole does not, so the range goes in."""
    target = big_file(tmp_path)
    sent: list = []
    result = dispatched(files_cfg(tmp_path), sent, files=[
        target, {"path": target, "start_line": 10, "end_line": 20},
    ])

    found = headers(sent)
    assert any("lines 10-20 of 200" in h for h in found), found
    reasons = skipped(result)
    assert reasons and all("read_file" in r for r in reasons), reasons
    prompt = json.dumps(sent[0])
    assert prompt.count("\\tline 15\\n") == 1, "a line was sent twice"


@posix_only
def test_a_file_named_whole_and_by_range_is_sent_once_whole(tmp_path):
    small = tmp_path / "small.py"
    small.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    sent: list = []
    result = dispatched(files_cfg(tmp_path), sent, files=[
        str(small), {"path": str(small), "start_line": 2, "end_line": 2},
    ])

    assert result["files_skipped"] == [], result["files_skipped"]
    found = headers(sent)
    assert len(found) == 1 and "lines" not in found[0], found
    assert json.dumps(sent[0]).count("b = 2") == 1, "the ranged line was sent twice"
