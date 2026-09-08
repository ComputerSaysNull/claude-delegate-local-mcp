"""The `sys.argv` dispatch in `main.run`, which nothing tested until `provision` arrived.

`--doctor` and `--init` are matched by *membership* -- deliberately, because the server
itself takes no arguments and an argument parser here would be a second place to set the
transport and the config file (ADR-0027). `provision` cannot be matched that way: it reads
a value, and membership would make any argument that merely contained the word start
building a virtualenv instead of a server. So it is matched on position, and that
difference is what these tests pin down.

Each dispatch is asserted both ways round -- it fires on its own form, and it does *not*
fire on the form that looks like it. A dispatch test that only ever checked the positive
case would pass against a function that dispatched on everything.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_delegate_local import doctor, init, main, provision


@pytest.fixture
def calls(monkeypatch):
    """Record which subcommand ran, with what, instead of running any of them."""
    seen: dict[str, object] = {}

    def fake_doctor():
        seen["doctor"] = True
        return 0

    def fake_init():
        seen["init"] = True
        return 0

    def fake_provision(*, argv=None, out=None):
        seen["provision"] = argv
        return 0

    def fake_load():
        seen["fellthrough"] = True
        return SimpleNamespace(transport="stdio")

    monkeypatch.setattr(doctor, "main", fake_doctor)
    monkeypatch.setattr(init, "main", fake_init)
    monkeypatch.setattr(provision, "main", fake_provision)
    # The fall-through path loads configuration and builds a server. Neither belongs in a
    # dispatch test, and `config.load` raises on a host with no `.env`, so it is cut off
    # here with a marker that is distinguishable from a real failure.
    monkeypatch.setattr(main.config, "load", fake_load)
    monkeypatch.setattr(main.registry, "load", lambda cfg: None)
    monkeypatch.setattr(main.server, "build", lambda cfg, reg: _NoServer())
    return seen


class _NoServer:
    def run(self, **_kw):
        return None


def dispatch(argv: list[str], monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["claude-delegate-local-mcp", *argv])
    try:
        main.run()
    except SystemExit:
        pass


def test_doctor_dispatches(calls, monkeypatch):
    dispatch(["--doctor"], monkeypatch)
    assert "doctor" in calls


def test_init_dispatches(calls, monkeypatch):
    dispatch(["--init"], monkeypatch)
    assert "init" in calls


def test_provision_dispatches_with_the_arguments_after_it(calls, monkeypatch):
    dispatch(["provision", "/mnt/c/proj"], monkeypatch)
    assert calls["provision"] == ["/mnt/c/proj"]


def test_an_argument_merely_containing_the_word_does_not_dispatch(calls, monkeypatch):
    """The reason `provision` is positional rather than a membership test.

    Matched the way `--doctor` is, this path would build a virtualenv instead of starting a
    server -- and the symptom would be a server that never came up, which is the failure
    mode this module's docstring says is the hardest one to report.
    """
    dispatch(["/mnt/c/notes/provision-plan"], monkeypatch)
    assert "provision" not in calls
    assert "fellthrough" in calls


def test_provision_after_another_argument_does_not_dispatch(calls, monkeypatch):
    """Position means first. Anywhere else it is somebody's file name."""
    dispatch(["--init", "provision"], monkeypatch)
    assert "provision" not in calls
    assert "init" in calls


def test_no_arguments_starts_the_server(calls, monkeypatch):
    """The control: with nothing to match, all three must be skipped."""
    dispatch([], monkeypatch)
    assert "fellthrough" in calls
    assert not {"doctor", "init", "provision"} & set(calls)
