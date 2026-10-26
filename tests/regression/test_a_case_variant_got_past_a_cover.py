"""PLAN M21.12: a case variant got past a cover.

On a case-insensitive filesystem a cover over `.env` did not cover `.ENV`, and a read-only
`.claude` did not cover `.CLAUDE` (JOURNAL 2026-10-26, measured on CIFS). FUSE here stands
in for CIFS and WSL's 9p, because like them the kernel keeps the two spellings as separate
names while the backing store folds them; the fix refuses to run a command when a folder on
the way to a cover folds case.
"""

import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

from claude_delegate_local import config as config_module
from claude_delegate_local import sandbox
from casefold_fuse import casefold_mount, needs_fuse


def _cfg(**over):
    kw = {"workspace_roots": (".",)}
    kw.update(over)
    return config_module.Config(**kw)  # type: ignore[arg-type]


def _req(command, home, workdir=None):
    return sandbox.SandboxRequest(command=command, home=home, workdir=workdir)


def globs_file(tmp_path: Path, *patterns: str) -> str:
    f = tmp_path / "globs.txt"
    f.write_text("# test denylist\n" + "\n".join(patterns) + "\n", encoding="utf-8")
    return str(f)


@lru_cache(maxsize=1)
def _sandbox_unusable() -> str:
    """Empty when a real sandbox runs here, else why not. As the masked-failure test says:
    an installed bwrap that cannot unshare is the host, not the feature, being broken.

    Probed with no process cap: under the default cap, the very bug these tests catch makes
    the probe fail, and they would skip on the unfixed code instead of failing there."""
    if not Path("/bin/sh").exists():
        return "POSIX only: the sandbox never runs on Windows"
    cfg = _cfg(sandbox_max_processes=0)
    if not sandbox.available(cfg):
        return "bwrap is not installed"
    with tempfile.TemporaryDirectory() as home:
        try:
            probe = sandbox.run(cfg, sandbox.SandboxRequest(command="true", home=home))
        except Exception as e:  # any failure at all here means "cannot run one"
            return f"bwrap could not run: {e}"
    if probe.exit_code != 0:
        return (
            "THE END-TO-END SANDBOX TESTS DID NOT RUN -- bwrap cannot run a command here "
            f"(`true` exited {probe.exit_code}). The binds are unproven by this run."
        )
    return ""


needs_bwrap = pytest.mark.skipif(bool(_sandbox_unusable()), reason=_sandbox_unusable())


# ---- the shared tree --------------------------------------------------------------


def _fill(work: Path) -> None:
    """The covered secret and the protected file the tests all start from."""
    (work / ".claude").mkdir(parents=True, exist_ok=True)
    (work / ".env").write_text("CANARY-7f3a", encoding="utf-8")
    (work / ".claude" / "settings.json").write_text('{"orig": true}', encoding="utf-8")


def _lists(tmp_path: Path) -> tuple[str, str]:
    """The two list files, in their own folders so they do not collide or get walked."""
    (tmp_path / "secret-globs").mkdir(exist_ok=True)
    (tmp_path / "protected-globs").mkdir(exist_ok=True)
    return (
        globs_file(tmp_path / "secret-globs", ".env"),
        globs_file(tmp_path / "protected-globs", ".claude/**"),
    )


def _tree(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """The backing tree and its list files: backing dir, home, secret globs, protected."""
    backing = tmp_path / "backing"
    _fill(backing)
    secret, protected = _lists(tmp_path)
    return backing, tmp_path / "home", secret, protected


# ---- the two halves ----------------------------------------------------------------


@needs_fuse
@needs_bwrap
@pytest.mark.integration
def test_a_case_variant_read_a_covered_secret(tmp_path):
    """A cover over `.env` did not cover `.ENV`: today the canary comes back in stdout."""
    backing, home, secret, protected = _tree(tmp_path)
    mnt = tmp_path / "mnt"
    cfg = _cfg(
        secret_globs_file=secret,
        protected_globs_file=protected,
        sandbox_max_processes=0,
    )
    req = _req("cat .ENV 2>&1; true", home=str(home), workdir=str(mnt))
    with casefold_mount(backing, mnt):
        try:
            result = sandbox.run(cfg, req)
        except sandbox.SecretShadowIncomplete:
            return  # the fix refused: that is the pass
    assert "CANARY-7f3a" not in result.stdout


@needs_fuse
@needs_bwrap
@pytest.mark.integration
def test_a_case_variant_wrote_a_protected_file(tmp_path):
    """A read-only `.claude` did not cover `.CLAUDE`: today the write lands in the backing."""
    backing, home, secret, protected = _tree(tmp_path)
    mnt = tmp_path / "mnt"
    cfg = _cfg(
        secret_globs_file=secret,
        protected_globs_file=protected,
        sandbox_max_processes=0,
    )
    req = _req("printf x > .CLAUDE/settings.json 2>&1; true", home=str(home), workdir=str(mnt))
    with casefold_mount(backing, mnt):
        try:
            sandbox.run(cfg, req)
        except sandbox.SecretShadowIncomplete:
            return  # the fix refused: that is the pass
    assert (backing / ".claude" / "settings.json").read_text(encoding="utf-8") == '{"orig": true}'


@needs_fuse
def test_a_folding_folder_is_named(tmp_path):
    """The refusal names the mount: a folder on the way to a cover folds case.

    Not integration: discovery is plain Python and needs no bwrap, so this is the one that
    runs in CI.
    """
    backing, home, secret, protected = _tree(tmp_path)
    mnt = tmp_path / "mnt"
    cfg = _cfg(secret_globs_file=secret, protected_globs_file=protected)
    req = _req("true", home=str(home), workdir=str(mnt))
    with casefold_mount(backing, mnt):
        with pytest.raises(sandbox.SecretShadowIncomplete) as exc:
            sandbox.discover_secret_shadows(cfg, req)
    assert str(mnt) in str(exc.value)


def test_a_plain_folder_is_not_named(tmp_path):
    """The negative control: a plain dir does not fold, so the walk names `.env` and nothing
    is refused."""
    work = tmp_path / "work"
    _fill(work)
    secret, protected = _lists(tmp_path)
    cfg = _cfg(secret_globs_file=secret, protected_globs_file=protected)
    req = _req("true", home=str(tmp_path / "home"), workdir=str(work))
    found = sandbox.discover_secret_shadows(cfg, req)
    assert str(work / ".env") in [t.path for t in found]


def test_a_real_second_spelling_is_not_folding(tmp_path):
    """The negative control for the probe: `.env` and `.ENV` as two real files are not one
    folder, so both are named and nothing is refused."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text("first", encoding="utf-8")
    (work / ".ENV").write_text("second", encoding="utf-8")
    if (work / ".env").read_text(encoding="utf-8") != "first":
        pytest.skip("this tmp dir is case-insensitive: .ENV overwrote .env")
    secret, protected = _lists(tmp_path)
    cfg = _cfg(secret_globs_file=secret, protected_globs_file=protected)
    req = _req("true", home=str(tmp_path / "home"), workdir=str(work))
    found = sandbox.discover_secret_shadows(cfg, req)
    paths = [t.path for t in found]
    assert str(work / ".env") in paths
    assert str(work / ".ENV") in paths
