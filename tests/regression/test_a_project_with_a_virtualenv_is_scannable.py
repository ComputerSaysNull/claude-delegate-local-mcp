"""`run_bash` was refused outright on any project carrying a virtualenv.

The mount-level secret scan walks the workdir before every `run_bash` call and refuses
when it passes `secret_shadow_max_entries`. That is correct — a partial denylist reads
exactly like a complete one — but the default was measured on a checkout of this
repository that had no `.venv` in it. `config.py` said so in its own help text: "This
repository scans in 230." With a virtualenv present it scans 10,586, and every shell
command was refused.

Nothing caught it because every scan test built a tree of one to twelve files by hand,
and the two that exercise the budget do it by *lowering* the cap to 5. None ever asked
whether a realistic tree passes the shipped default with the shipped lists — which is the
only question a user's first `run_bash` actually asks.

So this test uses `security/opaque_globs.txt` as shipped rather than a fixture. Deleting
the virtualenv pattern from that file has to fail something, or the file is a comment.

Measured at the time of the fix, on /mnt/c: 10,586 entries and 66s walked, against 248
entries and 0.7s with the directory covered. (ADR-0041)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import sandbox
from claude_delegate_local.config import Config
from claude_delegate_local.sandbox import SandboxRequest

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_OPAQUE = ROOT / "security" / "opaque_globs.txt"
SHIPPED_SECRETS = ROOT / "security" / "secret_globs.txt"

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="the scan walks a POSIX tree and is proven under WSL"
)


def project(tmp_path: Path, bulk_files: int) -> Path:
    """A workdir shaped like a real one: a little source, a lot of installed packages."""
    work = tmp_path / "proj"
    (work / "src").mkdir(parents=True)
    (work / "src" / "app.py").write_text("x", encoding="utf-8")
    (work / "README.md").write_text("x", encoding="utf-8")
    (work / ".env").write_text("TOKEN=shhh", encoding="utf-8")

    site = work / ".venv" / "lib" / "site-packages"
    site.mkdir(parents=True)
    for i in range(bulk_files):
        (site / f"mod{i}.py").write_text("x", encoding="utf-8")
    # The names that made raising the cap actively harmful: ordinary library source that
    # matches `*secret*` and `*credential*`, which the scan would cover with /dev/null.
    (site / "secrets.py").write_text("x", encoding="utf-8")
    (site / "client_credentials.py").write_text("x", encoding="utf-8")
    return work


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": ("/",),
        "secret_globs_file": str(SHIPPED_SECRETS),
        "opaque_globs_file": str(SHIPPED_OPAQUE),
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def req(work: Path) -> SandboxRequest:
    return SandboxRequest(command="true", home=str(work / "nohome"), workdir=str(work))


@posix_only
def test_a_workdir_with_a_virtualenv_scans_within_budget(tmp_path):
    """The bug, as a user meets it: a shell command on an ordinary project.

    The budget is set just above what the project is worth *without* its virtualenv, so
    this passes only if the virtualenv is skipped rather than walked. With the shipped
    list, and only with it.
    """
    work = project(tmp_path, bulk_files=60)

    found = sandbox.discover_secret_shadows(cfg(secret_shadow_max_entries=12), req(work))

    covered = {t.path for t in found}
    assert str(work / ".venv") in covered, (
        "the virtualenv was not covered, so it was walked; remove the venv pattern from "
        f"{SHIPPED_OPAQUE.name} and this is what happens on every run_bash call")
    assert str(work / ".env") in covered, (
        "the real secret stopped being covered -- skipping bulk must never cost coverage")


@posix_only
def test_the_library_source_inside_it_is_never_shadowed(tmp_path):
    """Covering the directory beats walking it, and not only on speed.

    Walked, `*secret*` and `*credential*` match ordinary library filenames, and the scan
    mounts /dev/null over each -- breaking the imports inside the very environment it just
    spent a minute reading. One mount over the parent is both cheaper and more correct.
    """
    work = project(tmp_path, bulk_files=5)

    found = sandbox.discover_secret_shadows(cfg(), req(work))
    paths = {t.path for t in found}

    site = work / ".venv" / "lib" / "site-packages"
    assert str(site / "secrets.py") not in paths
    assert str(site / "client_credentials.py") not in paths
    assert str(work / ".venv") in paths


@posix_only
def test_without_the_opaque_list_the_same_project_is_refused(tmp_path):
    """The control. Without it the two tests above pass against a scan that never had a
    budget problem, and would keep passing if the walk stopped being bounded at all."""
    work = project(tmp_path, bulk_files=60)

    with pytest.raises(sandbox.SecretShadowIncomplete) as e:
        sandbox.discover_secret_shadows(
            cfg(secret_shadow_max_entries=12, opaque_globs_file=""), req(work))
    assert "run_bash is refused" in str(e.value)


def test_the_shipped_list_names_a_virtualenv():
    """Guards the fixture above from passing for the wrong reason on a renamed pattern."""
    patterns = [
        line.strip()
        for line in SHIPPED_OPAQUE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert patterns, "the opaque list is empty; every project tree is walked in full"
    assert any(p.startswith((".venv", "venv")) for p in patterns), (
        f"{SHIPPED_OPAQUE.name} no longer names a virtualenv, which is the directory that "
        "made run_bash unusable in the first place")


def test_the_two_lists_are_separate_files():
    """The secret denylist is a security control; the opaque list is a speed measure.

    Merged, a slow scan becomes fixable by editing the security list -- the one edit that
    should never be made for a performance reason.
    """
    assert SHIPPED_OPAQUE != SHIPPED_SECRETS
    secrets = SHIPPED_SECRETS.read_text(encoding="utf-8")
    assert ".venv" not in secrets, (
        "a bulk directory has been added to the secret denylist; it belongs in "
        f"{SHIPPED_OPAQUE.name}, which is not a security control and says so")
