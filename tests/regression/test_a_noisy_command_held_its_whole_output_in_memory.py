"""A noisy command's whole output sat in the server's memory until it exited or timed out.

`sandbox._execute` ran the built argv with `subprocess.run(capture_output=True, timeout=...)`,
which holds a command's entire stdout and stderr in this process until it exits or is killed;
`tools._capped` cut the text to `max_bash_output_chars` only afterwards, keeping the tail.
A `yes` or a `find /` that reached the timeout therefore piled up hundreds of MB in the
server before anything was cut -- measured, a 50 MB command peaked at 100 MB under
tracemalloc. The sandbox's memory cap bounds the command, never the server, so an OOM-killed
server takes every delegation running beside it with it.

These call `_execute` directly with a plain `sh -c` argv, as the split-UTF-8 regression does:
no bwrap, no sandbox namespace, no cluster.
"""

import sys
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from claude_delegate_local import sandbox, tools
from claude_delegate_local.config import Config

pytestmark = pytest.mark.skipif(
    not Path("/bin/sh").exists(), reason="POSIX shell required; the sandbox never runs on Windows"
)

# What the server may hold for one command, in bytes. Far below what the command writes, so
# only a bounded tail fits and anything holding the whole stream is caught here.
BOUND = 5 * 1024 * 1024
BIG = 50_000_000
END = "THE-END"


def _cfg() -> Config:
    """A cap small enough that the tail is tiny, and a timeout a deadlock would reach."""
    return Config(
        workspace_roots=(".",),  # type: ignore[arg-type]
        max_bash_output_chars=200,
        run_bash_timeout=60,
    )


def _peak(call: Callable[[], sandbox.SandboxResult]) -> tuple[sandbox.SandboxResult, int]:
    """The result, and the peak the server allocated while producing it."""
    tracemalloc.start()
    try:
        result = call()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_a_50mb_command_does_not_hold_its_output_in_the_server(tmp_path: Path) -> None:
    cfg = _cfg()
    result, peak = _peak(lambda: sandbox._execute(
        cfg,
        ["sh", "-c", f"head -c {BIG} /dev/zero; printf '{END}'"],
        tmp_path / "err-trap",
    ))

    assert peak < BOUND, f"peak {peak} bytes for {BIG} bytes of output"
    assert result.exit_code == 0, result.stderr[-200:]
    assert result.stdout.endswith(END), "the tail, not the head, must be kept"
    assert len(result.stdout) < 4096, f"{len(result.stdout)} characters held, not a tail"
    assert result.output_bytes == BIG + len(END), result.output_bytes

    note = tools._capped(cfg, result)
    assert "truncated" in note, note[:200]
    assert str(BIG + len(END)) in note, "the true size must be stated"
    assert "bytes" in note
    assert note.rstrip().endswith(END)


def test_a_command_flooding_both_streams_does_not_deadlock(tmp_path: Path) -> None:
    """Read one pipe to EOF before the other and the second fills and blocks the command.

    Both writers flood, so a reader that serialises the streams deadlocks until the timeout.
    """
    cfg = _cfg()
    half = 2_000_000
    command = (
        f"{{ head -c {half} /dev/zero; printf 'OUT-END'; }} & "
        f"{{ head -c {half} /dev/zero >&2; printf 'ERR-END' >&2; }} & "
        "wait"
    )
    result, peak = _peak(lambda: sandbox._execute(
        cfg, ["sh", "-c", command], tmp_path / "err-trap"
    ))

    assert not result.timed_out, "one stream's pipe filled while the other was read"
    assert result.exit_code == 0, result.stderr[-200:]
    assert peak < BOUND, f"peak {peak} bytes"
    assert result.stdout.endswith("OUT-END"), "the stdout tail was lost"
    assert result.stderr.endswith("ERR-END"), "the stderr tail was lost"
    assert result.output_bytes == 2 * half + len("OUT-END") + len("ERR-END")
