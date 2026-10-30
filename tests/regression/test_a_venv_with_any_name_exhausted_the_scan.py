"""A virtualenv called anything but `.venv` or `venv` exhausted the scan budget.

`discover_secret_shadows` covers a bulk directory by NAME, from
`security/opaque_globs.txt` -- `.venv/**`, `venv/**`, `node_modules/**` and so on. A venv
called `pyenv`, or any name not on the list, is walked entry by entry and exhausts
`secret_shadow_max_entries`, so every `run_bash` in that workdir is refused with
SecretShadowIncomplete. The fix recognises a venv by its contents instead: `pyvenv.cfg`,
the marker every venv carries whatever it is called, so no name can dodge the budget.

Measured at the time of the fix: a `pyenv` with 17,352 entries against a default cap of
10,000.
"""

from pathlib import Path

import pytest

from claude_delegate_local import sandbox
from claude_delegate_local.config import Config
from claude_delegate_local.sandbox import SandboxRequest

ROOT = Path(__file__).resolve().parents[2]
SHIPPED_OPAQUE = ROOT / "security" / "opaque_globs.txt"
SHIPPED_SECRETS = ROOT / "security" / "secret_globs.txt"


def cfg(**over) -> Config:
    kw = {
        "workspace_roots": ("/",),
        "secret_globs_file": str(SHIPPED_SECRETS),
        "opaque_globs_file": str(SHIPPED_OPAQUE),
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def req(home: Path, workdir: Path) -> SandboxRequest:
    return SandboxRequest(command="true", home=str(home), workdir=str(workdir))


def venv_tree(tmp_path: Path, *, marker: bool) -> Path:
    """A workdir whose venv is named `pyenv`, not anything the opaque list names."""
    work = tmp_path / "work"
    venv = work / "pyenv"
    site = venv / "lib" / "site-packages"
    site.mkdir(parents=True)
    (work / "README.md").write_text("x", encoding="utf-8")
    if marker:
        (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    for i in range(40):
        (site / f"mod{i}.py").write_text("x", encoding="utf-8")
    return work


def test_a_venv_named_pyenv_scans_within_budget(tmp_path):
    """The bug, as a user meets it: a shell command on an ordinary project.

    The venv is named `pyenv`, which `security/opaque_globs.txt` does not name, so it would
    be walked and refuse -- unless the scan recognises the venv by its `pyvenv.cfg` marker.
    The budget is set just above what the project is worth *without* the venv's bulk, so
    this passes only if the venv is covered rather than walked.
    """
    work = venv_tree(tmp_path, marker=True)

    found = sandbox.discover_secret_shadows(
        cfg(secret_shadow_max_entries=10), req(tmp_path / "nohome", work))

    covered = {t.path for t in found}
    assert str(work / "pyenv") in covered, (
        "the venv was not covered, so it was walked and exhausted the budget")
    venv_shadow = next(t for t in found if t.path == str(work / "pyenv"))
    assert venv_shadow.kind == "dir"
    assert venv_shadow.matched == "venv:pyvenv.cfg", (
        "a venv covered by content must be attributable as one; the opaque list is the "
        "name-based measure and must not claim credit for it")


def test_the_same_tree_without_pyvenv_cfg_is_still_refused(tmp_path):
    """The control. Without the marker the identical tree is refused, which proves the
    marker is the trigger rather than the budget being generous enough anyway."""
    work = venv_tree(tmp_path, marker=False)

    with pytest.raises(sandbox.SecretShadowIncomplete) as e:
        sandbox.discover_secret_shadows(
            cfg(secret_shadow_max_entries=10), req(tmp_path / "nohome", work))
    assert "run_bash is refused" in str(e.value)


def test_a_workdir_that_is_itself_a_venv_root_is_not_covered_as_a_whole(tmp_path):
    """The scan root is never covered by the marker rule.

    The workdir is the one read-write bind of a real project, and covering it would hide
    everything the command was given. A venv root at depth zero is walked like any other
    workdir: its files are matched, its directory is not covered whole.
    """
    work = tmp_path / "work"
    work.mkdir()
    (work / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (work / "app.py").write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(cfg(), req(tmp_path / "nohome", work))

    assert all(t.path != str(work) for t in found), (
        "the scan root was covered as a venv; that hides everything the command was given")


def test_a_workdir_nested_inside_home_is_not_covered_even_though_it_is_a_venv_root(tmp_path):
    """The root exclusion holds however the roots nest.

    The workdir sits inside the home, so the home walk reaches it at depth one -- the same
    depth as any venv the marker rule covers. It must still not be covered, because it is
    a scan root: covering it would hide everything the command was given.
    """
    home = tmp_path / "home"
    work = home / "proj"
    work.mkdir(parents=True)
    (work / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (work / "app.py").write_text("x", encoding="utf-8")

    found = sandbox.discover_secret_shadows(cfg(), req(home, work))

    assert all(t.path != str(work) for t in found), (
        "a scan root nested under another was covered as a venv")


def test_a_provisioned_venv_under_the_provisioned_root_is_not_covered(tmp_path):
    """The provisioned root is pruned before it is visited, and must stay bound.

    `provisioned_root(home)` is the one tree the scan skips without covering (ADR-0062);
    a venv inside it carries `pyvenv.cfg`, so the marker rule would otherwise cover it and
    hide the interpreter the command was given.
    """
    home = tmp_path / "home"
    provisioned = home / sandbox.PROVISIONED_DIRNAME / "proj-abc123"
    (provisioned / "lib" / "site-packages").mkdir(parents=True)
    (provisioned / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (provisioned / "lib" / "site-packages" / "credentials.py").write_text(
        "x", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    found = sandbox.discover_secret_shadows(cfg(), req(home, work))

    covered = {t.path for t in found}
    assert str(provisioned) not in covered
    assert str(provisioned / "pyvenv.cfg") not in covered


def test_the_refusal_says_a_covered_directory_is_hidden_from_the_command(tmp_path):
    """The refusal advised naming the bulk in the opaque list, which reads like a fix for a
    project's own virtualenv -- and covering one hides its interpreter from every command.
    The advice has to say what covering costs, and where an interpreter belongs instead."""
    work = tmp_path / "work"
    for i in range(12):
        (work / "bulk" / f"d{i}").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    cfg = Config(workspace_roots=(str(tmp_path),), sandbox_home=str(home),  # type: ignore[arg-type]
                 secret_shadow_max_entries=5)

    with pytest.raises(sandbox.SecretShadowIncomplete) as e:
        sandbox.discover_secret_shadows(cfg, SandboxRequest(command="true", home=str(home),
                                                    workdir=str(work)))

    message = str(e.value)
    assert "hidden from the command" in message
    assert "provision" in message
