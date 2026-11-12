"""One invalid byte in a command's output ended the whole delegation as an error.

`sandbox._execute` ran the built argv with `subprocess.run(..., text=True)` and no
`errors=`, so the decode was strict. A delegation ran
`awk '{print substr($0,1,50)}'` over a file of multi-byte characters; awk cut a
character in half at byte 50, and the `UnicodeDecodeError` -- "'utf-8' codec can't
decode bytes in position 58-59: invalid continuation byte" -- escaped `run_bash` and
killed the delegation, losing the command's real exit code and the rest of its output
with it. The timeout path already decoded with `_as_text` (errors="replace"), so only
the ordinary path was fatal, and a command whose output is merely ugly must not be
able to end a run.

These call `_execute` directly with a plain `sh -c` argv: no `bwrap`, no sandbox
namespace, no cluster. The bytes are written with octal escapes so the test file itself
holds no split character and the exact broken sequence is visible.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from claude_delegate_local import sandbox
from claude_delegate_local.config import Config

pytestmark = pytest.mark.skipif(
    not Path("/bin/sh").exists(), reason="POSIX shell required; the sandbox never runs on Windows"
)

# `\342\200` is the first two bytes of a three-byte sequence: a character cut in half,
# exactly what `substr($0,1,50)` produced at the end of a 50-byte line.
SPLIT = "\\342\\200"


def _execute(tmp_path: Path, command: str) -> sandbox.SandboxResult:
    """Run one `sh -c` argv through the real `_execute`, with no sandbox around it."""
    cfg = Config(workspace_roots=(".",))  # type: ignore[arg-type]
    return sandbox._execute(cfg, ["sh", "-c", command], tmp_path / "err-trap")


def test_a_split_character_on_stdout_does_not_end_the_delegation(tmp_path: Path) -> None:
    """The decode must not raise, and must keep both the exit code and the text around it.

    Today `_execute` decodes strictly, so the `UnicodeDecodeError` escapes before the
    `SandboxResult` is ever built.
    """
    result = _execute(tmp_path, f"printf 'ok {SPLIT} end'; exit 3")

    assert result.exit_code == 3, "the command's real exit code must survive a bad byte"
    assert "ok " in result.stdout, result.stdout
    assert "end" in result.stdout, result.stdout
    assert "\ufffd" in result.stdout, f"the invalid bytes were not replaced: {result.stdout!r}"
    assert not result.timed_out


def test_a_split_character_on_stderr_does_not_end_the_delegation(tmp_path: Path) -> None:
    """stderr is decoded by the same call, so a broken byte there is fatal in the same way."""
    result = _execute(tmp_path, f"printf 'ok {SPLIT} end' >&2; exit 3")

    assert result.exit_code == 3, "the command's real exit code must survive a bad byte"
    assert "ok " in result.stderr, result.stderr
    assert "end" in result.stderr, result.stderr
    assert "\ufffd" in result.stderr, f"the invalid bytes were not replaced: {result.stderr!r}"
    assert not result.timed_out


def test_valid_multibyte_output_comes_back_unchanged(tmp_path: Path) -> None:
    """The negative control: replacing invalid bytes must not touch a valid character.

    Passes today and after the fix. Without it, a "fix" that mangles all non-ASCII text
    -- or one that returns an empty string -- would still satisfy the two tests above.
    """
    result = _execute(tmp_path, "printf 'ok \\342\\200\\224 end'")

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "ok \u2014 end", repr(result.stdout)
    assert "\ufffd" not in result.stdout, "a valid character was replaced"
