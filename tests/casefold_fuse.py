"""A FUSE filesystem that folds case, standing in for a case-insensitive backing store.

`mfusepy` is a dev dependency, but it must not be a hard one: the sandbox tests that only
need a plain directory still have to run on a machine with no `/dev/fuse`, so the bindings
are guarded here and `fuse_unavailable` decides whether the FUSE tests skip or run.
"""

import contextlib
import errno
import functools
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

try:
    from mfusepy import FUSE, Operations, FuseOSError
except ImportError:
    FUSE = None
    Operations = object
    FuseOSError = None


def _fuse(fn):
    """Turn an `OSError` from an operation into the `FuseOSError` libfuse expects."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OSError as e:
            raise FuseOSError(e.errno or errno.EIO) from e
    return wrapper


class CaseFoldingPassthrough(Operations):
    """A passthrough whose every path lookup folds case, as a case-insensitive store does.

    The kernel keeps the two spellings as separate names while this filesystem resolves
    both to the same backing entry -- exactly how CIFS and WSL's 9p behave, and why a
    cover over `.env` does not cover `.ENV` (PLAN M21.12).
    """

    use_ns = True

    def __init__(self, backing):
        self.backing = str(backing)

    def _resolve(self, path):
        """The backing path `path` names, folding each component's case.

        A component keeps its exact spelling when that name exists in the backing
        directory, else the one entry whose casefold matches, else the spelling as given
        -- so `create` and `mkdir` still make a new name rather than an old one.
        """
        parts = [p for p in path.split("/") if p]
        current = self.backing
        for part in parts:
            exact = os.path.join(current, part)
            if os.path.exists(exact):
                current = exact
                continue
            try:
                names = os.listdir(current)
            except OSError:
                names = []
            folded = part.casefold()
            matches = [n for n in names if n.casefold() == folded]
            current = os.path.join(current, matches[0] if len(matches) == 1 else part)
        return current

    @_fuse
    def getattr(self, path, fh=None):
        st = os.lstat(self._resolve(path))
        return {
            "st_mode": st.st_mode,
            "st_nlink": st.st_nlink,
            "st_size": st.st_size,
            "st_uid": st.st_uid,
            "st_gid": st.st_gid,
            "st_atime_ns": st.st_atime_ns,
            "st_mtime_ns": st.st_mtime_ns,
            "st_ctime_ns": st.st_ctime_ns,
        }

    @_fuse
    def readdir(self, path, fh):
        return [".", "..", *os.listdir(self._resolve(path))]

    @_fuse
    def open(self, path, flags):
        return os.open(self._resolve(path), flags)

    @_fuse
    def create(self, path, mode, fi=None):
        return os.open(self._resolve(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)

    @_fuse
    def read(self, path, size, offset, fh):
        return os.pread(fh, size, offset)

    @_fuse
    def write(self, path, data, offset, fh):
        return os.pwrite(fh, data, offset)

    @_fuse
    def truncate(self, path, length, fh=None):
        os.truncate(self._resolve(path), length)

    @_fuse
    def unlink(self, path):
        os.unlink(self._resolve(path))

    @_fuse
    def mkdir(self, path, mode):
        os.mkdir(self._resolve(path), mode)

    @_fuse
    def rmdir(self, path):
        os.rmdir(self._resolve(path))

    @_fuse
    def rename(self, old, new):
        os.rename(self._resolve(old), self._resolve(new))

    @_fuse
    def release(self, path, fh):
        os.close(fh)

    @_fuse
    def flush(self, path, fh):
        return 0

    @_fuse
    def fsync(self, path, datasync, fh):
        os.fsync(fh)

    @_fuse
    def utimens(self, path, times=None):
        if times is None:
            os.utime(self._resolve(path))
        else:
            os.utime(self._resolve(path), ns=times)

    @_fuse
    def chmod(self, path, mode):
        os.chmod(self._resolve(path), mode)

    @_fuse
    def access(self, path, amode):
        if not os.access(self._resolve(path), amode):
            raise FuseOSError(errno.EACCES)

    @_fuse
    def statfs(self, path):
        st = os.statvfs(self._resolve(path))
        return {
            "f_bsize": st.f_bsize,
            "f_frsize": st.f_frsize,
            "f_blocks": st.f_blocks,
            "f_bfree": st.f_bfree,
            "f_bavail": st.f_bavail,
            "f_files": st.f_files,
            "f_ffree": st.f_ffree,
            "f_favail": st.f_favail,
            "f_namemax": st.f_namemax,
        }


def _unmount(mountpoint):
    fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
    if fusermount:
        subprocess.run([fusermount, "-u", mountpoint], check=False, timeout=10)


@contextlib.contextmanager
def casefold_mount(backing, mountpoint):
    """Mount `backing` folded onto `mountpoint`; yield it; unmount on exit.

    FUSE runs in a daemon thread (`foreground=True, nothreads=True`), so this returns once
    the mount is up and unmounting it in `finally` is what makes the thread stop.
    """
    os.makedirs(str(mountpoint), exist_ok=True)
    ops = CaseFoldingPassthrough(backing)
    error = []

    def _run():
        try:
            FUSE(ops, str(mountpoint), foreground=True, nothreads=True)
        except Exception as e:
            error.append(e)

    thread = threading.Thread(target=_run, name="casefold-fuse", daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if os.path.ismount(str(mountpoint)):
            break
        if not thread.is_alive():
            raise RuntimeError(
                "the casefold FUSE thread died before mounting"
                + (f": {error[0]}" if error else "")
            )
        time.sleep(0.05)
    else:
        raise RuntimeError("timed out waiting for the casefold mount to appear")
    try:
        yield mountpoint
    finally:
        _unmount(str(mountpoint))
        thread.join(timeout=10.0)


@functools.lru_cache(maxsize=1)
def fuse_unavailable() -> str:
    """Empty when a casefold mount can be made here, else the reason why not.

    Probes in order: `mfusepy` importable, `/dev/fuse` present, a `fusermount` on PATH,
    then an actual mount of a temp backing dir that must fold `A` onto a file created as
    `a`.
    """
    if FUSE is None:
        return "mfusepy is not installed"
    if not Path("/dev/fuse").exists():
        return "/dev/fuse does not exist"
    if not (shutil.which("fusermount3") or shutil.which("fusermount")):
        return "neither fusermount3 nor fusermount is installed"
    with tempfile.TemporaryDirectory() as backing_s, tempfile.TemporaryDirectory() as mnt_s:
        backing = Path(backing_s)
        mnt = Path(mnt_s)
        (backing / "a").write_text("folded", encoding="utf-8")
        try:
            with casefold_mount(backing, mnt):
                if not (mnt / "A").exists() or (mnt / "A").read_text() != "folded":
                    return "the casefold mount did not fold case"
        except Exception as e:
            return f"casefold mount could not be made: {e}"
    return ""


def needs_fuse(fn):
    """Skip when FUSE is unavailable here; in CI, fail rather than skip.

    CI installs fuse3, so an unavailable FUSE there means the check silently never ran,
    which must be a failure and not a pass.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        reason = fuse_unavailable()
        if reason:
            if os.environ.get("CI"):
                pytest.fail(f"FUSE unavailable in CI: {reason}")
            pytest.skip(reason)
        return fn(*args, **kwargs)
    return wrapper
