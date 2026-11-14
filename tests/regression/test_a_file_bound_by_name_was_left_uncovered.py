"""A secret file bound by name in `extra_binds` was left uncovered.

`discover_secret_shadows` treated every `extra_binds` entry as a scan root and walked it with
`os.walk`. `os.walk` on a regular file yields nothing at all -- no error, no entries -- so a
denylisted file named directly in an agent's `extra_binds` (`/some/project/.env`) was bound
read-only with no cover over it, and every `run_bash` command could read it. The same file
one level inside a bound folder was covered, because the per-file loop saw it.

Read-only is not the protection a credential needs: read-only still means readable, and
being read is the whole threat. So a file root goes through the same per-file checks a file
inside a walked root does, and emits the same kind of cover.

These call `discover_secret_shadows` directly, with no bwrap, so they prove the denylist
rather than the sandbox binary. Every Config names its own denylist under `tmp_path`; none of
them resolves the repo's real `security/secret_globs.txt`.

The key armour below is split into two literals on purpose. `test_the_repository_itself_does
_not_trip_it` reads every `tests/**/*.py` and refuses one carrying key material, and a
sandboxed run covers such a file with /dev/null before pytest can open it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from claude_delegate_local import sandbox
from claude_delegate_local.config import Config
from claude_delegate_local.sandbox import SandboxRequest

WORKDIR = "/mnt/c/Users/dev/proj"
HOME = "/home/dev/.cache/sandbox-home"


def cfg(**over) -> Config:
    kw = {"workspace_roots": (WORKDIR,)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def req(**over) -> SandboxRequest:
    kw = {"command": "echo hi", "home": HOME}
    kw.update(over)
    return SandboxRequest(**kw)  # type: ignore[arg-type]


def globs_file(tmp_path: Path, *patterns: str) -> str:
    f = tmp_path / "globs.txt"
    f.write_text("# test denylist\n" + "\n".join(patterns) + "\n", encoding="utf-8")
    return str(f)


def test_a_secret_file_bound_by_name_is_covered(tmp_path):
    """The bug: `extra_binds` naming a file directly left it readable.

    A `.env` bound by name is bound read-only, which is not a cover -- the command can open
    it. It has to be shadowed exactly as a `.env` inside a bound folder is.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    env = project / ".env"
    env.write_text("TOKEN=1\n", encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, ".env")),
        req(home=str(home), extra_binds=(str(env),)))

    assert [(t.path, t.kind, t.matched) for t in found] == [(str(env), "file", ".env")]


def test_a_harmless_file_bound_by_name_is_left_readable(tmp_path):
    """The other half: a file the denylist does not name must not be covered up.

    A cover over a file the command needs is an outage, so the check cannot be "any file
    root is shadowed" -- it is the same name-and-content verdict every other file gets.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    notes = project / "notes.md"
    notes.write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, ".env")),
        req(home=str(home), extra_binds=(str(notes),)))

    assert found == ()


def test_a_file_bound_by_name_is_content_scanned_too(tmp_path):
    """A key renamed `config.json` is invisible to every pattern, so the content scan is the
    second detector -- and a file root must reach it, not only a walked one."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    config = project / "config.json"
    config.write_text(
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nnot really, but the header is what counts\n",
        encoding="utf-8")

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, "nothing-matches-this")),
        req(home=str(home), extra_binds=(str(config),)))

    assert [(t.path, t.kind) for t in found] == [(str(config), "file")]
    assert found[0].matched.startswith("content:")


def test_a_symlink_bound_by_name_is_never_covered(tmp_path):
    """A shadow op on a symlink node aborts the whole bwrap invocation rather than following
    it, so a link root must be left alone -- the same rule the walk follows."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    real = project / "real"
    real.write_text("x", encoding="utf-8")
    link = project / "id_rsa"
    link.symlink_to(real)

    found = sandbox.discover_secret_shadows(
        cfg(secret_globs_file=globs_file(tmp_path, "id_rsa*")),
        req(home=str(home), extra_binds=(str(link),)))

    assert found == ()
