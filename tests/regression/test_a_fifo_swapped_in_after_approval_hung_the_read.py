"""An approved regular file replaced by a FIFO hung the read, and skipped the key scan.

`open_resolved` opened `entry.posix` with the flags in `_MODE_FLAGS` and no `O_NONBLOCK`,
so `os.open` on a FIFO waited for something to open the other end. A `run_bash` command can
do exactly that in the workdir -- `rm a.txt && mkfifo a.txt` -- between the moment the path
was approved and the moment it was opened. Nothing in `open_resolved` noticed: layer 1's
`S_ISREG` check is a function of the path and had already run, and `_prove_descriptor` proves
the descriptor is at the approved path, not what kind of thing that path now names.

The hang is the visible half. The quiet half is the content check: `pread` fails on a pipe,
and `_refuse_key_material` reads an `OSError` there as "no key material", so a FIFO could
not be scanned and did not refuse either.

Measured on Linux. A FIFO is POSIX only, so the Windows leg skips with a reason rather than
adding to a green count.

Nothing here polls or sleeps: every open of the FIFO that could block is either
`O_NONBLOCK` or runs on a daemon thread the suite joins with a deadline, so a regression
costs five seconds and a report rather than the whole run.
"""

import os
import stat as stat_module
import threading
from pathlib import Path

import pytest

from claude_delegate_local import paths
from claude_delegate_local.config import Config
from claude_delegate_local.paths import PathRefused, open_resolved, resolve_all

try:  # POSIX only, like mkfifo below.
    import fcntl
except ImportError:  # pragma: no cover -- exercised by the Windows leg of CI
    fcntl = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="a FIFO is POSIX only: Windows has no os.mkfifo"
)

GLOBS = ".env\n.env.*\n*.pem\n*secret*\n.git/**\n.ssh/**\n.docker/config.json\n"

TIMEOUT = 5.0


def cfg(tmp_path, **over) -> Config:
    """A config rooted at `tmp_path` with a denylist beside it.

    Built the way `tests/test_paths.py` builds one: `secret_globs_file` points at a file in
    the tmp tree, because the shipped denylist is resolved relative to the real `.env` and
    layer 3 refuses to run when it cannot be read.
    """
    globs = tmp_path / "globs.txt"
    if not globs.exists():
        globs.write_text(GLOBS, encoding="utf-8")
    kw = {
        "workspace_roots": (os.path.realpath(tmp_path),),
        "secret_globs_file": str(globs),
        "respect_gitignore": False,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def approved_then_swapped_for_a_fifo(tmp_path, name: str = "a.txt"):
    """A path approved as a regular file, then replaced by a FIFO at the same path."""
    target = tmp_path / name
    target.write_text("hello\n", encoding="utf-8")
    entry = resolve_all(cfg(tmp_path), [str(target)])[0]
    target.unlink()
    os.mkfifo(target)
    return entry, target


def release(path: Path) -> None:
    """Unblock whichever end an unfixed `os.open` is waiting on.

    `O_NONBLOCK` here is what makes this a release rather than a second hang: a
    non-blocking write-open succeeds once a reader waits, and a non-blocking read-open
    succeeds with no writer at all, so the same call frees either side.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return
    os.close(fd)


def open_off_the_main_thread(entry, mode: str):
    """`open_resolved` on a daemon thread, joined with a deadline.

    A blocking open on the main thread would stop the suite, which is the bug's shape
    leaking into the harness. Here the deadline is the assertion.
    """
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["opened"] = open_resolved(entry, mode)
        except BaseException as e:  # reported to the test, never swallowed
            outcome["error"] = e

    thread = threading.Thread(target=run, name=f"open_resolved-{mode}", daemon=True)
    thread.start()
    thread.join(TIMEOUT)
    return thread, outcome


def open_descriptors() -> int | None:
    """How many descriptors this process holds, where procfs will say."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


@pytest.mark.parametrize("mode", ["rb", "wb"])
def test_a_fifo_swapped_in_after_approval_is_refused_not_awaited(tmp_path, mode):
    """Both directions, because they fail differently.

    A read-open on a FIFO waits for a writer, and a write-open waits for a reader, so
    neither mode is covered by the other.
    """
    entry, fifo = approved_then_swapped_for_a_fifo(tmp_path)
    assert stat_module.S_ISFIFO(os.stat(fifo).st_mode), (
        "the swap did not land, so the rest of this test proves nothing"
    )

    before = open_descriptors()
    thread, outcome = open_off_the_main_thread(entry, mode)
    hung = thread.is_alive()
    if hung:
        # Release it first, so the failure below is a report and the suite moves on.
        release(fifo)
        thread.join(TIMEOUT)
    after = open_descriptors()

    opened = outcome.get("opened")
    if opened is not None:  # tidy up a handle the bug left behind
        opened.handle.close()

    assert not hung, (
        f"open_resolved({mode!r}) was still blocked on the FIFO after {TIMEOUT}s: the open "
        "ran without O_NONBLOCK, so a FIFO swapped in after approval waits for a peer."
    )

    error = outcome.get("error")
    assert isinstance(error, PathRefused), (
        f"expected a refusal, got {error!r} -- a raw OSError from a write-only FIFO open "
        "is the ENXIO half of the same bug."
    )
    assert [r.layer for r in error.refusals] == [paths.LAYER_OPENED]
    assert "regular file" in str(error)
    assert "FIFO" in str(error) or "fifo" in str(error)

    if before is not None and after is not None:
        assert after == before, (
            f"the refusal leaked a descriptor: {before} open before, {after} after"
        )


def test_a_regular_file_still_opens_without_the_nonblock_flag(tmp_path):
    """The guard on the fix's other half.

    `O_NONBLOCK` is added so the open cannot wait, and cleared again once the descriptor is
    proven, so the handle a caller receives blocks exactly as it did before. Without this
    test, clearing the flag is an unobserved claim.
    """
    target = tmp_path / "a.txt"
    target.write_text("hello\n", encoding="utf-8")
    entry = resolve_all(cfg(tmp_path), [str(target)])[0]

    with open_resolved(entry, "rb").handle as fh:
        if fcntl is not None:
            flags = fcntl.fcntl(fh.fileno(), fcntl.F_GETFL)
            assert flags & os.O_NONBLOCK == 0
        assert fh.read() == b"hello\n"
