"""The default extension allowlist refused `.jsonl`, so no delegation could read a transcript.

`tests/transcript_samples/*.jsonl` are the transcript format's sample streams (ADR-0111).
`read_file`, `search_files` and `git show` all check layer 2, and `.jsonl` was not on the
default list, so every one of them refused the samples.
"""

from __future__ import annotations

import pytest

from claude_delegate_local import paths
from claude_delegate_local.config import Config


def _cfg() -> Config:
    return Config(workspace_roots=(".",))  # type: ignore[arg-type]


@pytest.mark.parametrize("name", [
    "tests/transcript_samples/agentic.jsonl",
    "tests/transcript_samples/one_shot_failed.jsonl",
    "tests/transcript_samples/asked_the_caller.jsonl",
])
def test_the_default_allowlist_accepts_a_jsonl_transcript_sample(name):
    assert paths.extension_refusal(_cfg(), name) is None


def test_an_extension_not_on_the_list_is_still_refused():
    """The control: the check that passes above must not be a switched-off check."""
    assert paths.extension_refusal(_cfg(), "release.exe") is not None
