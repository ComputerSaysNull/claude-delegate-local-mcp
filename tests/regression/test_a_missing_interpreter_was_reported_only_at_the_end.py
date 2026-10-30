"""A missing interpreter was reported only once the run was over.

The stale and missing notes told the model to "say so": it did, in its final answer, after
spending the whole run working without the project's packages. Nothing told it that it could
stop and ask, and `ask_caller` itself said it was only for telling two readings of the task
apart. The notes now tell the model to ask its caller with `ask_caller` straight away, naming
the folder to build, so the run waits while the caller runs `provision` and then carries on
with `$DELEGATE_PYTHON` set. `ask_caller`, `run_bash`, the `provision` tool and the
orchestration resource all say the same from their side.
"""

import asyncio
import json
from pathlib import Path

from fastmcp import Client
from test_server import _built

from claude_delegate_local import provision, tools
from claude_delegate_local.config import Config


def _cfg(tmp_path: Path) -> Config:
    return Config(workspace_roots=(str(tmp_path),), sandbox_home=str(tmp_path / "home"))  # type: ignore[arg-type]


def test_the_missing_note_asks_now_and_names_the_folder(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "requirements.txt").write_text("six\n", encoding="utf-8")
    monkeypatch.setattr(provision, "provisioned_for", lambda cfg, wd: None)
    monkeypatch.setattr(provision, "stale_for", lambda cfg, wd: None)

    note = tools.missing_env_note(_cfg(tmp_path), project.as_posix(), ())

    assert note is not None
    assert "ask_caller" in note
    assert f"`provision` tool for {project.as_posix()}" in note
    assert "If your task needs the project's packages" in note


def test_the_stale_note_asks_now_and_names_the_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    venv = Path(provision.venv_dir(home.as_posix(), project.as_posix()))
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("", encoding="utf-8")
    (venv / provision.RECORD_NAME).write_text(json.dumps({
        "project": project.as_posix(),
        "interpreter": (venv / "bin" / "python").as_posix(),
        "dependency_hash": "a declaration that has since moved on",
    }), encoding="utf-8")

    note = tools.stale_env_note(_cfg(tmp_path), project.as_posix())

    assert note is not None
    assert "ask_caller" in note
    assert f"`provision` tool for {project.as_posix()}" in note


def test_ask_caller_may_be_used_for_what_only_the_caller_can_do():
    description = tools.ASK_CALLER_SPEC.description

    assert "only your caller can do" in description
    assert "interpreter" in description


def test_run_bash_no_longer_says_nothing_can_be_done():
    description = tools.REGISTRY["run_bash"].spec.description

    assert "no way to install one from here" not in description
    assert "your caller can" in description


def test_the_provision_tool_says_a_delegation_may_ask_for_it():
    async def go():
        async with Client(_built()) as client:
            tool = next(t for t in await client.list_tools() if t.name == "provision")
            guide = await client.read_resource("delegate://orchestration")
        return tool.description or "", guide[0].text

    description, guide = asyncio.run(go())

    assert "provisioning_missing" in description
    assert "asks for it" in description
    assert "calling `provision`" in guide
