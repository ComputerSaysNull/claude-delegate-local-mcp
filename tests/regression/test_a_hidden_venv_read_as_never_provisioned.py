"""A hidden virtualenv read as "never provisioned" to a delegation and its caller.

The secret scan covers a virtualenv wherever it sits -- by opaque-list name (`.venv`) or by
its `pyvenv.cfg` marker -- so a project that keeps its own `.venv` inside the workspace sees
an empty directory there. With nothing provisioned for the workdir either, `$DELEGATE_PYTHON`
is simply absent and the model falls back to the standard library instead of asking for a
build: the same silent shape as the stale case, for a different reason.

`missing_env_note` turns the covered directories into a sentence the model reads beside the
result body, and the caller gets `provisioning_missing` in the delegation result. `stale_for`
already covers the stale case, so this note deliberately stays silent when a stale
environment exists -- the stale note fires instead, never both.

Named after the bug, per the project's convention.
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from claude_delegate_local import provision, sandbox, tools
from claude_delegate_local.config import Config


def cfg(**over) -> Config:
    kw = {"workspace_roots": ("/mnt/c/Users/dev/proj",)}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def project_tree(tmp_path: Path) -> Path:
    """One minimal project, with a declaration so it is a real workdir."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    return root


def nothing_provisioned(monkeypatch) -> None:
    """No current and no stale environment, so only the missing case can fire."""
    monkeypatch.setattr(provision, "provisioned_for", lambda cfg, wd: None)
    monkeypatch.setattr(provision, "stale_for", lambda cfg, wd: None)


# --- covered_venvs ----------------------------------------------------------------------


def test_covered_venvs_names_venvs_and_not_plain_opaque_dirs(tmp_path):
    """A venv is one thing and a bulk directory another; only the former is reported.

    An opaque `.venv` that carries `pyvenv.cfg` is a venv, a `venv:` match is one by
    definition, and a `node_modules` with no marker is not -- naming it would tell the model
    its standard-library fallback is because of a JavaScript tree.
    """
    home = tmp_path / "home"
    home.mkdir()
    opaque_venv = home / ".venv"
    opaque_venv.mkdir()
    (opaque_venv / "pyvenv.cfg").write_text("", encoding="utf-8")
    marker_venv = home / "pyenv"
    marker_venv.mkdir()
    (marker_venv / "pyvenv.cfg").write_text("", encoding="utf-8")
    node = home / "node_modules"
    node.mkdir()
    (node / "index.js").write_text("", encoding="utf-8")

    found = sandbox._covered_venvs((
        sandbox.ShadowTarget(path=str(opaque_venv), kind="dir", matched="opaque:.venv/**"),
        sandbox.ShadowTarget(path=str(marker_venv), kind="dir", matched="venv:pyvenv.cfg"),
        sandbox.ShadowTarget(path=str(node), kind="dir", matched="opaque:node_modules/**"),
    ))

    assert found == (str(opaque_venv), str(marker_venv))


# --- missing_env_note -------------------------------------------------------------------


def test_the_note_fires_for_a_covered_venv_with_nothing_provisioned(tmp_path, monkeypatch):
    """The model must be told which directories are hidden and why `$DELEGATE_PYTHON` is
    unset, not just that something is wrong."""
    project = project_tree(tmp_path)
    nothing_provisioned(monkeypatch)

    note = tools.missing_env_note(
        cfg(sandbox_home=str(tmp_path / "home")), project.as_posix(),
        (str(project / ".venv"),),
    )

    assert note
    assert ".venv" in note
    assert "$DELEGATE_PYTHON" in note
    assert "provision" in note


def test_the_note_is_none_when_nothing_is_covered(tmp_path, monkeypatch):
    project = project_tree(tmp_path)
    nothing_provisioned(monkeypatch)

    assert tools.missing_env_note(
        cfg(sandbox_home=str(tmp_path / "home")), project.as_posix(), ()
    ) is None


def test_the_note_is_none_when_the_workdir_is_provisioned_and_current(tmp_path, monkeypatch):
    """A current environment is offered, so `$DELEGATE_PYTHON` is set and nothing is missing."""
    project = project_tree(tmp_path)
    monkeypatch.setattr(
        provision, "provisioned_for", lambda cfg, wd: {"interpreter": "/venv/bin/python"}
    )
    monkeypatch.setattr(provision, "stale_for", lambda cfg, wd: None)

    assert tools.missing_env_note(
        cfg(sandbox_home=str(tmp_path / "home")), project.as_posix(),
        (str(project / ".venv"),),
    ) is None


def test_the_note_is_none_when_the_environment_is_stale(tmp_path, monkeypatch):
    """Stale is the other case the note covers; the stale note fires instead, not both."""
    project = project_tree(tmp_path)
    monkeypatch.setattr(provision, "provisioned_for", lambda cfg, wd: None)
    monkeypatch.setattr(
        provision, "stale_for", lambda cfg, wd: {"interpreter": "/venv/bin/python"}
    )

    assert tools.missing_env_note(
        cfg(sandbox_home=str(tmp_path / "home")), project.as_posix(),
        (str(project / ".venv"),),
    ) is None


# --- the note reaches the run_bash result ----------------------------------------------


def test_run_bash_appends_the_missing_note_to_the_result_body(tmp_path, monkeypatch):
    """The note is said beside the output, where the model reads the result of its command.

    `_run_bash` needs no bubblewrap when `sandbox.run` is stubbed, so this drives the real
    text-assembly path rather than a helper that could drift from it.
    """
    project = project_tree(tmp_path)
    nothing_provisioned(monkeypatch)
    monkeypatch.setattr(
        sandbox, "run",
        lambda c, r: sandbox.SandboxResult(
            stdout="hello", stderr="", exit_code=0, timed_out=False,
            covered_venvs=(str(project / ".venv"),),
        ),
    )

    result = tools._run_bash(
        cfg(sandbox_home=str(tmp_path / "home"), workspace_roots=(str(project),)),
        {"command": "ls"},
        tools.BashPolicy(workdir=str(project)),
    )

    assert "$DELEGATE_PYTHON" in result.text
    assert ".venv" in result.text
    assert result.outcome.missing_env_note is True


def test_a_venv_covered_outside_the_workdir_is_not_named(tmp_path, monkeypatch):
    """The scan also walks the sandbox home; a venv covered there is not the project's, and
    naming it as `../home/...` would send the caller to provision the wrong folder."""
    project = project_tree(tmp_path)
    nothing_provisioned(monkeypatch)

    note = tools.missing_env_note(
        cfg(sandbox_home=str(tmp_path / "home")), project.as_posix(),
        (str(tmp_path / "home" / "tool" / ".venv"),),
    )

    assert note is None
