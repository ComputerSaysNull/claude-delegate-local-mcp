"""The `run` subcommand: one delegation from a shell, with no MCP server in the path.

The red for this module is `test_the_run_subcommand_returns_before_a_server_is_built`,
which fails on its assertion rather than on an import: before the subcommand existed,
`run` fell through to `config.load` and `server.build` like any unrecognised argument,
so the gate it is asserting was genuinely open.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from claude_delegate_local import config, main, registry, run_task, server


class _FakeMCP:
    """Stands in for the built server. Running it is the thing that must not happen."""

    def run(self, **_kw: object) -> None:
        raise SystemExit(99)


def test_the_run_subcommand_returns_before_a_server_is_built(monkeypatch) -> None:
    """`run` must return where `--doctor` and `provision` do, not reach the transport.

    stdout carries a JSON result here, so a server started underneath it would write
    MCP frames onto the same stream -- the corruption main.py's docstring exists about.
    """
    built: list[bool] = []

    def _build(_cfg: object, _reg: object) -> _FakeMCP:
        built.append(True)
        return _FakeMCP()

    monkeypatch.setattr(config, "load", lambda: SimpleNamespace(transport="stdio"))
    monkeypatch.setattr(registry, "load", lambda _cfg: object())
    monkeypatch.setattr(server, "build", _build)
    # No `--task`, so the runner refuses on usage and never dispatches anything. That
    # keeps this test about the dispatch gate rather than about a delegation.
    monkeypatch.setattr(sys, "argv", ["prog", "run"])

    with pytest.raises(SystemExit):
        main.run()

    assert built == [], "the run subcommand must not reach server.build"


def test_a_missing_task_is_a_usage_error_rather_than_a_traceback() -> None:
    assert run_task.main(argv=[]) == 2


def test_the_result_is_one_json_document_on_stdout(monkeypatch, capsys) -> None:
    """A shell fan-out collects these, so the whole of stdout has to parse."""
    monkeypatch.setattr(run_task, "_build_pieces", lambda: ("cfg", "reg", "cache", None))

    async def _fake(**_kw: object) -> dict[str, object]:
        return {"answer": "hello", "output_tokens": 2}

    monkeypatch.setattr(run_task, "_dispatch", _fake)

    assert run_task.main(argv=["--task", "say hello"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["answer"] == "hello"
    assert printed["output_tokens"] == 2


def test_a_failure_exits_non_zero_and_still_prints_json(monkeypatch, capsys) -> None:
    """The exit code is what a shell branches on; the JSON is what a human reads."""
    monkeypatch.setattr(run_task, "_build_pieces", lambda: ("cfg", "reg", "cache", None))

    async def _boom(**_kw: object) -> dict[str, object]:
        raise RuntimeError("backend refused")

    monkeypatch.setattr(run_task, "_dispatch", _boom)

    assert run_task.main(argv=["--task", "say hello"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["error_type"] == "RuntimeError"
    assert "backend refused" in printed["error"]
