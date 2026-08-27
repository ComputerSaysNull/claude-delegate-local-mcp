"""`.env` is read as a fallback under the real environment.

The README has told you to `cp .env.example .env` since the first commit, and until now
nothing read it: `config.load()` looked at `os.environ` only. Creating the file did
nothing, reported nothing, and left every setting at its default (ADR-0027).

The tests that matter here are the ones asserting a *silent* failure is gone: a requested
file that does not exist raises, and a line that could never have been read raises. A
loader that shrugs at both is indistinguishable from the bug it replaces.
"""

from __future__ import annotations

import pytest

from claude_delegate_local import config

ROOTS = "DELEGATE_WORKSPACE_ROOTS"


def write(tmp_path, body: str):
    f = tmp_path / ".env"
    f.write_text(body, encoding="utf-8")
    return f


# ---------------------------------------------------------------- parsing

def test_comments_and_blank_lines_are_skipped():
    got = config.parse_env_file("# note\n\n  \nA=1\n")
    assert got == {"A": "1"}


def test_a_value_is_taken_literally_so_windows_paths_survive():
    """Unescaping here would corrupt DELEGATE_WORKSPACE_ROOTS without saying so."""
    got = config.parse_env_file(r"P=C:\Users\you\projects")
    assert got["P"] == r"C:\Users\you\projects"


def test_one_pair_of_surrounding_quotes_is_stripped():
    assert config.parse_env_file('A="two words"')["A"] == "two words"
    assert config.parse_env_file("A='two words'")["A"] == "two words"


def test_an_inner_quote_is_left_alone():
    assert config.parse_env_file('A=say "hi"')["A"] == 'say "hi"'


def test_an_export_prefix_is_stripped_not_silently_mangled():
    """Without this the key is 'export A', which no setting matches -- value dropped."""
    assert config.parse_env_file("export A=1") == {"A": "1"}


def test_a_key_that_cannot_be_an_env_var_raises():
    """It could never have been read. Failing quietly is the bug being fixed."""
    with pytest.raises(config.ConfigError, match="cannot be"):
        config.parse_env_file("not a key = 1")


# ---------------------------------------------------------------- precedence

def test_the_env_file_supplies_a_value(tmp_path):
    f = write(tmp_path, f"{ROOTS}=/from/file\nDELEGATE_MAX_TURNS_DEFAULT=7\n")
    cfg = config.load(environ={}, env_file=f)
    assert cfg.max_turns_default == 7
    assert cfg.workspace_roots == ("/from/file",)


def test_the_real_environment_wins_over_the_file(tmp_path):
    """An explicit override has to keep working, or the file becomes a trap."""
    f = write(tmp_path, f"{ROOTS}=/from/file\nDELEGATE_MAX_TURNS_DEFAULT=7\n")
    cfg = config.load(environ={"DELEGATE_MAX_TURNS_DEFAULT": "9"}, env_file=f)
    assert cfg.max_turns_default == 9
    assert cfg.workspace_roots == ("/from/file",), "the file still fills what env omits"


def test_an_empty_environment_value_does_not_mask_the_file(tmp_path):
    """An unset-looking variable is not an instruction to ignore the file."""
    f = write(tmp_path, f"{ROOTS}=/from/file\nDELEGATE_MAX_TURNS_DEFAULT=7\n")
    cfg = config.load(environ={"DELEGATE_MAX_TURNS_DEFAULT": ""}, env_file=f)
    assert cfg.max_turns_default == 7


def test_values_from_the_file_are_validated_like_any_other(tmp_path):
    f = write(tmp_path, f"{ROOTS}=/x\nDELEGATE_CONNECT_TIMEOUT=0\n")
    with pytest.raises(config.ConfigError, match="must be positive"):
        config.load(environ={}, env_file=f)


# ---------------------------------------------------------------- discovery

def test_a_requested_file_that_is_missing_raises(tmp_path):
    """The whole point. Asking for a file and silently getting defaults is the bug."""
    with pytest.raises(config.ConfigError, match="does not exist"):
        config.load(environ={}, env_file=tmp_path / "absent.env")


def test_passing_environ_suppresses_discovery(tmp_path, monkeypatch):
    """A test must never pick up whatever .env happens to sit in the working tree."""
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    write(tmp_path, "DELEGATE_MAX_TURNS_DEFAULT=7\n")
    cfg = config.load(environ={ROOTS: "/x"})
    assert cfg.max_turns_default != 7


def test_discovery_reads_the_repo_root_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    write(tmp_path, f"{ROOTS}=/from/discovered\n")
    monkeypatch.delenv(config.ENV_FILE_VAR, raising=False)
    monkeypatch.setattr(config.os, "environ", {})
    assert config.load().workspace_roots == ("/from/discovered",)


def test_a_missing_repo_root_env_is_not_an_error(tmp_path, monkeypatch):
    """Not having one is normal; only an explicit request is a promise."""
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config.os, "environ", {ROOTS: "/x"})
    assert config.load().workspace_roots == ("/x",)
