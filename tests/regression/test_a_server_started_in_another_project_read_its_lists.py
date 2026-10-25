"""A server started in another project read that project's security lists, or none.

`models_file` and the three glob-list settings default to paths relative to the working
directory. `wsl.exe --cd <repo>` used to pin that to this checkout, so the defaults always
landed on its own `models.toml` and `security/*.txt`. A stdio server on Linux starts in the
client's project instead (measured: its cwd is `CLAUDE_PROJECT_DIR`), so a session in
another repository read *that* repository's denylist -- or refused to start for want of one.

Relative file settings now resolve against the folder of the `.env` they were read from,
and against the checkout when none was read: one fixed place, whatever the cwd.
"""

import shutil
from pathlib import Path

import pytest

from claude_delegate_local import paths, registry, sandbox
from claude_delegate_local.config import REPO_ROOT, Config

REAL = "real-secret-*\n"
DECOY = "decoy-*\n"


@pytest.fixture
def settings_dir(tmp_path: Path) -> Path:
    """D: where the `.env` lives, holding the real lists and a registry."""
    d = tmp_path / "settings"
    (d / "security").mkdir(parents=True)
    (d / ".env").write_text("", encoding="utf-8")
    for name in ("secret_globs.txt", "protected_globs.txt", "opaque_globs.txt"):
        (d / "security" / name).write_text(REAL, encoding="utf-8")
    shutil.copy(REPO_ROOT / "models.toml.example", d / "models.toml")
    return d


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """E: the client's project, which is the cwd, holding decoys of every file."""
    e = tmp_path / "another-project"
    (e / "security").mkdir(parents=True)
    for name in ("secret_globs.txt", "protected_globs.txt", "opaque_globs.txt"):
        (e / "security" / name).write_text(DECOY, encoding="utf-8")
    (e / "models.toml").write_text("this is = not = toml\n", encoding="utf-8")
    monkeypatch.chdir(e)
    return e


def cfg(settings: Path | None) -> Config:
    """The defaults, relative, as a fresh `.env` leaves them."""
    return Config(workspace_roots=(str(Path.cwd()),),  # type: ignore[arg-type]
                  env_file=str(settings / ".env") if settings else None)


@pytest.mark.parametrize("load", [
    paths.load_secret_globs, paths.load_protected_globs, sandbox.load_opaque_globs,
])
def test_each_list_is_read_beside_the_env_file_not_from_the_cwd(settings_dir, elsewhere, load):
    globs = load(cfg(settings_dir))
    assert "decoy-*" not in globs, "read the client project's list"
    assert "real-secret-*" in globs


def test_the_registry_is_read_beside_the_env_file_not_from_the_cwd(settings_dir, elsewhere):
    registry.load(cfg(settings_dir))  # the decoy in the cwd is not TOML, so reading it raises


def test_with_no_env_file_the_lists_are_the_checkouts_own(elsewhere):
    """No `.env` read: the anchor is the checkout, never the cwd."""
    globs = paths.load_secret_globs(cfg(None))
    assert "decoy-*" not in globs
    assert globs == paths.load_secret_globs(
        Config(workspace_roots=(str(Path.cwd()),),  # type: ignore[arg-type]
               secret_globs_file=str(REPO_ROOT / "security" / "secret_globs.txt")))
