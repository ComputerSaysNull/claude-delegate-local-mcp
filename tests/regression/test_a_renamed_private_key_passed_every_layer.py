"""Every path-policy layer inspected the name, so a key called `config.json` got through.

Layers 1-4 are functions of the path: roots, extension, denylist glob, gitignore. Rename a
private key to something on the extension allowlist and outside the denylist and all four
approve it. `run_bash` could read one the mount-level scan had not matched by name either,
and on this machine `DELEGATE_TRANSCRIPT_DIR` points into a synced folder, so key material
reaching an answer leaves the box.

ADR-0096 adds a content check to **both** layers, sharing one pattern table. The layers stay
independent -- `paths.py` refuses a file the model asked for, `sandbox.py` covers one a shell
could have opened -- and the shared table is what stops a pattern added to one leaving the
same bytes readable through the other.

**The second test is the point.** A check that fires on ordinary source would be disabled
within a week, and a disabled check is worse than none because it is still believed.

No real key material appears here. The header is assembled at runtime from fragments so the
literal never occurs in this file -- otherwise the sandbox walk would shadow this very test
file during any run_bash that binds the repo, which is the feature working correctly and a
suite failing for it.
"""

from __future__ import annotations

import os

from pathlib import Path

import pytest
from conftest import BASELINE_MISSING, baseline_blob

from claude_delegate_local import config as config_module
from claude_delegate_local import sandbox
from claude_delegate_local.paths import (
    PathRefused,
    key_material_marker,
    open_resolved,
    resolve_files,
)

REPO = Path(__file__).resolve().parents[2]

# Layer 1 resolves through `to_posix` and then `realpath`, which agree only on POSIX: on
# Windows a workspace root translates to `/mnt/c/...` and `realpath` hands back
# `C:\mnt\c\...`, so every path is refused as outside every root before the content check
# can run. Same reason `tests/test_paths.py` guards layers 1 and 4, and the same warning --
# a Windows run does NOT prove anything carrying this mark.
posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="THE OPEN-TIME CHECK IS UNPROVEN BY THIS RUN -- layer 1 needs a POSIX filesystem",
)

# Assembled, never written out. See the module docstring.
DASHES = "-" * 5
PEM_HEADER = f"{DASHES}BEGIN RSA PRIVATE" + f" KEY{DASHES}"
PEM_FOOTER = f"{DASHES}END RSA PRIVATE" + f" KEY{DASHES}"
FAKE_KEY = f"{PEM_HEADER}\nMIIEowIBAAKCAQEAfictional+not+a+real+key+0000\n{PEM_FOOTER}\n"


def _cfg(tmp_path: Path, **overrides: object) -> config_module.Config:
    return config_module.Config(  # type: ignore[arg-type]
        workspace_roots=(str(tmp_path),),
        respect_gitignore=False,
        **overrides,
    )


# ---- the primitive -----------------------------------------------------------------


def test_the_marker_is_found_whatever_surrounds_it() -> None:
    """A key inside JSON is the shape the item names -- not only a bare .pem file."""
    blob = ('{"id": "svc", "private_key": "' + FAKE_KEY.replace("\n", "\\n") + '"}')

    assert key_material_marker(blob.encode()) == PEM_HEADER


def test_ordinary_source_and_prose_do_not_match() -> None:
    """The false-positive control, run over material that sits close to the pattern."""
    near_misses = [
        b"# Load the private key from disk before signing the request.",
        b"def read_private_key(path): return path.read_bytes()",
        b"-----BEGIN CERTIFICATE-----\nMIIC...\n-----END CERTIFICATE-----",
        b"-----BEGIN PUBLIC KEY-----\nMFkw...\n-----END PUBLIC KEY-----",
        b"PRIVATE KEY",
        b"The file holds a private key, which is why it is on the denylist.",
        b"",
    ]

    for blob in near_misses:
        assert key_material_marker(blob) is None, blob


def test_the_repository_itself_does_not_trip_it() -> None:
    """The strongest control available: run the detector over this project's own sources.

    If the check fired here, every delegation reading this repository would start losing
    files, which is precisely how a check gets switched off.
    """
    offenders = []
    for path in sorted(REPO.glob("src/**/*.py")) + sorted(REPO.glob("docs/**/*.md")):
        marker = key_material_marker(path.read_bytes()[:4096])
        if marker is not None:
            offenders.append(path)

    assert not offenders, offenders


# ---- layer one: the file the model asked for ---------------------------------------


@posix_only
def test_a_renamed_key_is_refused_by_its_bytes(tmp_path: Path) -> None:
    disguised = tmp_path / "config.json"
    disguised.write_text(FAKE_KEY, encoding="utf-8")

    resolved, refusals = resolve_files(_cfg(tmp_path), [str(disguised)])
    assert not refusals, "the name passes every path layer -- that is the bug"
    assert len(resolved) == 1

    with pytest.raises(PathRefused) as caught:
        open_resolved(resolved[0], "rb", scan_bytes=4096)

    assert "key material" in str(caught.value)


@posix_only
def test_an_ordinary_file_at_the_same_path_opens_fine(tmp_path: Path) -> None:
    """Without this, the test above passes against an open that always refuses."""
    ordinary = tmp_path / "config.json"
    ordinary.write_text('{"private_key_path": "~/.ssh/id_rsa"}', encoding="utf-8")

    resolved, _ = resolve_files(_cfg(tmp_path), [str(ordinary)])
    opened = open_resolved(resolved[0], "rb", scan_bytes=4096)
    with opened.handle as fh:
        # And the handle is still at byte zero: the scan used pread.
        assert fh.read().startswith(b'{"private_key_path"')


@posix_only
def test_the_check_is_off_unless_the_caller_asks(tmp_path: Path) -> None:
    """`open_resolved` has callers that are the server reading its own material."""
    disguised = tmp_path / "config.json"
    disguised.write_text(FAKE_KEY, encoding="utf-8")

    resolved, _ = resolve_files(_cfg(tmp_path), [str(disguised)])

    with open_resolved(resolved[0], "rb").handle as fh:
        assert fh.read()


# ---- layer two: the file a shell could have opened ----------------------------------


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX only")
def test_the_sandbox_covers_a_renamed_key_the_denylist_missed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(FAKE_KEY, encoding="utf-8")
    (home / "notes.md").write_text("A note about a private key, in prose.", encoding="utf-8")

    cfg = _cfg(tmp_path)
    req = sandbox.SandboxRequest(command="true", home=str(home))

    shadows = sandbox.discover_secret_shadows(cfg, req)

    covered = {Path(s.path).name: s.matched for s in shadows}
    assert "config.json" in covered
    assert covered["config.json"].startswith("content:")
    assert "notes.md" not in covered, "prose about a key is not a key"


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX only")
def test_the_sandbox_content_scan_is_off_at_zero(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(FAKE_KEY, encoding="utf-8")

    cfg = _cfg(tmp_path, secret_content_scan_bytes=0)
    req = sandbox.SandboxRequest(command="true", home=str(home))

    shadows = sandbox.discover_secret_shadows(cfg, req)

    assert not [s for s in shadows if Path(s.path).name == "config.json"]


# ---- the negative control ------------------------------------------------------------


def test_negative_control_the_committed_code_read_no_bytes() -> None:
    """Against the baseline: no shared table, no scan, and nothing to turn it on with."""
    def show(path: str) -> str:
        blob = baseline_blob(path)
        if blob is None:
            pytest.skip(BASELINE_MISSING)
        return blob

    paths_blob = show("src/claude_delegate_local/paths.py")
    assert "key_material_marker" not in paths_blob, (
        "the baseline already has the detector; this control can no longer fail"
    )
    assert "scan_bytes" not in paths_blob
    assert "os.pread" not in paths_blob
    # The signature the bug lived behind: two arguments, no content question available.
    assert "def open_resolved(entry: ResolvedPath, mode: str) -> OpenedFile:" in paths_blob

    assert "key_material_marker" not in show("src/claude_delegate_local/sandbox.py")
    assert "secret_content_scan_bytes" not in show("src/claude_delegate_local/config.py")


@posix_only
def test_negative_control_main_would_have_handed_the_key_over(tmp_path: Path) -> None:
    """Not that the baseline lacks a name -- that it opens the disguised file and reads it.

    `scan_bytes` defaults to off, so calling today's `open_resolved` without it reproduces
    exactly what the committed code did. That is the measurement, and it is stronger than
    asserting a string is absent from a blob.
    """
    disguised = tmp_path / "config.json"
    disguised.write_text(FAKE_KEY, encoding="utf-8")

    resolved, refusals = resolve_files(_cfg(tmp_path), [str(disguised)])

    assert not refusals
    with open_resolved(resolved[0], "rb").handle as fh:
        assert PEM_HEADER.encode() in fh.read(), (
            "this is what every layer approved before ADR-0096"
        )
