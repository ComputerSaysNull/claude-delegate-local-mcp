r"""`--init` writes the two files a first run has no default for.

Every assertion here is paired with its opposite wherever the check could otherwise pass by
doing nothing. The writing rule in particular -- a line is emitted only when the answer
departs from the default -- is satisfied both by a renderer that writes everything and by
one that writes nothing, so neither direction is evidence on its own.

The answers are built by `script()` rather than written as a bare list. An index into a
flat list of prompts is a fixture that breaks silently when a question is added: it still
has the right length, so the run completes and asserts against the wrong field.

No test names a real host. `localhost` is one of the placeholders the gate's own allowlist
recognises, and a real endpoint in a tracked file is the leak that scanner exists to stop.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest

from claude_delegate_local import config, init, paths, registry

ACCEPT = ""                       # a blank line: keep the default, or end the root list
WINDOWS_ROOT = r"C:\Users\you\projects"
POSIX_ROOT = "/mnt/c/Users/you/projects"


# The three answers with no default, in the order they are asked: registry key, base URL,
# served model id.
REQUIRED = ("my-model", "http://localhost:8888", "some-model-id")


def script(*, roots: tuple[str, ...] = (WINDOWS_ROOT,),
           settings: tuple[str, ...] = (ACCEPT,) * 5,
           required: tuple[str, ...] = REQUIRED,
           model: tuple[str, ...] = (ACCEPT,) * 5,
           first: tuple[str, ...] = ()) -> list[str]:
    """One run's answers, in prompt order. `first` prepends an answer to be rejected."""
    return [*first, *roots, ACCEPT, *settings, *required, *model]


def rejected_then_accepted(field: str, bad: str) -> tuple[str, ...]:
    """Model answers where `field` is answered wrongly once, then left at its default.

    A rejected answer re-asks, so it costs two lines rather than one. Building that here
    keeps the arithmetic out of the tests, where getting it wrong reads as the run being
    abandoned rather than as the fixture being short.
    """
    out: list[str] = []
    for name in init.OFFERED_MODEL_FIELDS:
        out += [bad, ACCEPT] if name == field else [ACCEPT]
    return tuple(out)


def drive(answers: list[str], root: Path) -> tuple[int, str]:
    """Run the whole command against `root` with a scripted stdin. Returns (code, output)."""
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        code = init.run(root=root, inp=io.StringIO("\n".join(answers) + "\n"), out=out,
                        stamp="20260908-120000", tmp=Path(tmp))
    return code, out.getvalue()


def env_lines(root: Path) -> list[str]:
    """The settings actually written, comments and blanks dropped."""
    return [ln for ln in (root / ".env").read_text(encoding="utf-8").splitlines()
            if ln and not ln.startswith("#")]


# --------------------------------------------------------------------------------------
# What it writes loads


def test_what_it_writes_is_accepted_by_the_loaders_it_will_face(tmp_path: Path):
    """The strongest assertion available: round-trip through the server's own loaders."""
    code, _ = drive(script(), tmp_path)
    assert code == 0

    assert config.load(env_file=tmp_path / ".env").workspace_roots == (POSIX_ROOT,)

    reg = registry.load(config.load(environ={
        config.env_name("workspace_roots"): "/anywhere",
        config.env_name("models_file"): str(tmp_path / "models.toml"),
    }))
    assert len(reg) == 1
    entry = reg.resolve(None)
    assert entry.key == "my-model"
    assert entry.served_model_id == "some-model-id"


def test_a_registry_of_one_resolves_without_being_asked_for_a_name(tmp_path: Path):
    """`default = true` is written for the second entry someone adds by hand later."""
    drive(script(), tmp_path)
    assert "default = true" in (tmp_path / "models.toml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# The writing rule, both directions


def test_an_accepted_default_is_not_written_down(tmp_path: Path):
    """A default in .env freezes a copy of config.py. Accepting one must emit nothing."""
    drive(script(), tmp_path)
    written = env_lines(tmp_path)
    assert any(ln.startswith(config.env_name("workspace_roots")) for ln in written)
    for name in init.OFFERED_SETTINGS:
        assert not any(ln.startswith(config.env_name(name)) for ln in written), name


def test_a_changed_default_is_written(tmp_path: Path):
    """The other direction: a renderer that wrote nothing would pass the test above."""
    changed = tuple("high" if n == "thinking_default" else ACCEPT
                    for n in init.OFFERED_SETTINGS)
    code, _ = drive(script(settings=changed), tmp_path)
    assert code == 0
    assert f"{config.env_name('thinking_default')}=high" in env_lines(tmp_path)
    assert config.load(env_file=tmp_path / ".env").thinking_default == "high"


def test_an_accepted_model_default_is_not_written_and_a_changed_one_is(tmp_path: Path):
    changed = tuple("65536" if n == "context_window" else ACCEPT
                    for n in init.OFFERED_MODEL_FIELDS)
    drive(script(model=changed), tmp_path)
    text = (tmp_path / "models.toml").read_text(encoding="utf-8")
    assert "context_window = 65536" in text
    for name in ("concurrency", "max_tokens_cap", "api_key_env", "default_effort"):
        assert f"{name} =" not in text, name


def test_the_prompt_carries_the_setting_s_own_help_text(tmp_path: Path):
    """Proves the prompt is read from config.py rather than restated in init.py."""
    _, shown = drive(script(), tmp_path)
    rows = {row["field"]: row for row in config.describe()}
    for name in (*init.OFFERED_SETTINGS, "workspace_roots"):
        assert rows[name]["description"].split(".")[0] in shown, name
        assert rows[name]["env"] in shown, name


# --------------------------------------------------------------------------------------
# Backups


def test_an_existing_pair_is_moved_aside_rather_than_overwritten(tmp_path: Path):
    (tmp_path / ".env").write_text("OLD=env\n", encoding="utf-8")
    (tmp_path / "models.toml").write_text("old = 1\n", encoding="utf-8")

    code, shown = drive(script(), tmp_path)
    assert code == 0
    assert (tmp_path / ".env.bak-20260908-120000").read_text(encoding="utf-8") == "OLD=env\n"
    assert (tmp_path / "models.toml.bak-20260908-120000").read_text(
        encoding="utf-8") == "old = 1\n"
    assert ".env.bak-20260908-120000" in shown
    assert "OLD=env" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_a_backup_is_never_written_over_a_backup(tmp_path: Path):
    """The other direction on the same code path: nothing is lost to a second run."""
    (tmp_path / ".env").write_text("first\n", encoding="utf-8")
    assert init.back_up(tmp_path / ".env", stamp="20260908-120000") is not None

    (tmp_path / ".env").write_text("second\n", encoding="utf-8")
    with pytest.raises(OSError, match="never overwritten"):
        init.back_up(tmp_path / ".env", stamp="20260908-120000")
    assert (tmp_path / ".env.bak-20260908-120000").read_text(encoding="utf-8") == "first\n"


def test_nothing_to_back_up_is_not_an_error(tmp_path: Path):
    assert init.back_up(tmp_path / "absent", stamp="20260908-120000") is None


def test_both_backup_names_are_covered_by_the_secret_denylist():
    """The coverage this feature adds, asserted through the list the server actually reads.

    `security/secret_globs.txt` is consumed by the path policy and by the docs gate, so one
    entry covers both. A backup of either file names a host, and the model must not be able
    to read one -- while the example file beside it must stay readable, or this would be
    passing by refusing everything.
    """
    cfg = config.load(environ={config.env_name("workspace_roots"): "/anywhere"})
    globs = paths.load_secret_globs(cfg)

    assert paths.secret_match("/repo/.env.bak-20260908-120000", globs)
    assert paths.secret_match("/repo/models.toml.bak-20260908-120000", globs)
    assert paths.secret_match("/repo/models.toml.bak-20991231-235959", globs)
    assert paths.secret_match("/repo/.env", globs)

    # The negative control for the glob this feature adds: it must not reach the example
    # file beside it. (`.env.example` is a different matter -- `.env.*` has denied that
    # since before any of this, deliberately, and is not what is under test here.)
    assert not paths.secret_match("/repo/models.toml.example", globs)
    assert not paths.secret_match("/repo/models.toml", globs)


# --------------------------------------------------------------------------------------
# Pasted Windows paths


def test_a_pasted_windows_root_is_translated_before_it_is_written(tmp_path: Path):
    r"""Storing it as typed was the obvious choice, and is wrong on the host that reads it.

    `config.load` splits a tuple setting on `os.pathsep`, which is `:` where the server
    runs, so `C:\Users\you\projects` written verbatim loads as the two roots `C` and
    `\Users\you\projects`. The loaded value is asserted as well as the written line,
    because the line alone cannot show the split.
    """
    drive(script(), tmp_path)
    written = env_lines(tmp_path)
    assert f"{config.env_name('workspace_roots')}={POSIX_ROOT}" in written
    assert config.load(env_file=tmp_path / ".env").workspace_roots == (POSIX_ROOT,)

    # The invariant, covering the path this command generates as well as the ones it was
    # handed: nothing written into .env is in Windows form.
    assert not any("\\" in ln.partition("=")[2] for ln in written)


def test_the_example_file_no_longer_ships_a_root_that_cannot_load():
    """The same defect in the fallback path --init replaces. Found by the test above."""
    text = Path(".env.example").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines()
                if ln.startswith(config.env_name("workspace_roots") + "="))
    value = line.partition("=")[2]
    assert config.load(
        environ={config.env_name("workspace_roots"): value}).workspace_roots == (value,)


def test_several_roots_are_asked_one_at_a_time_and_joined(tmp_path: Path):
    """Which is the whole reason for the loop: a separated list cannot be parsed safely."""
    if os.pathsep != ":":
        pytest.skip("the joined form is read by a POSIX process; see ask_roots")
    drive(script(roots=(WINDOWS_ROOT, "/srv/work")), tmp_path)
    assert config.load(env_file=tmp_path / ".env").workspace_roots == (
        POSIX_ROOT, "/srv/work")


def test_a_root_that_does_not_exist_is_a_note_rather_than_a_refusal(tmp_path: Path):
    """--doctor is what fails on a missing root; this command cannot know it is a mistake."""
    _, shown = drive(script(), tmp_path)
    assert f"{POSIX_ROOT} does not exist yet" in shown


def test_a_unc_network_path_is_refused_at_the_prompt_and_re_asked(tmp_path: Path):
    r"""`\\server\share` has no mount point inside the distribution.

    Re-asked rather than fatal: the answers already given are worth more than the one
    mistyped, so the run continues and the good root is what lands.
    """
    code, shown = drive(script(first=(r"\\server\share",)), tmp_path)
    assert code == 0
    assert "UNC network path" in shown
    assert f"{config.env_name('workspace_roots')}={POSIX_ROOT}" in env_lines(tmp_path)


def test_a_blank_first_root_is_refused_and_a_blank_second_one_ends_the_list(tmp_path: Path):
    """Both directions of one prompt: required the first time, optional afterwards."""
    code, shown = drive(script(first=(ACCEPT,)), tmp_path)
    assert code == 0
    assert "cannot be blank" in shown
    assert config.load(env_file=tmp_path / ".env").workspace_roots == (POSIX_ROOT,)


# --------------------------------------------------------------------------------------
# Validation comes from the registry, not from a copy here


def test_a_base_url_carrying_the_v1_suffix_is_refused_by_the_registry_s_own_rule(
        tmp_path: Path):
    key, base_url, served = REQUIRED
    code, shown = drive(script(required=(key, base_url + "/v1", base_url, served)),
                        tmp_path)
    assert code == 0
    assert "/v1 suffix" in shown


def test_an_unknown_effort_level_is_refused_with_the_registry_s_wording(tmp_path: Path):
    code, shown = drive(
        script(model=rejected_then_accepted("default_effort", "medium")), tmp_path)
    assert code == 0
    assert "default_effort" in shown
    assert "is not one of" in shown
    assert "default_effort =" not in (tmp_path / "models.toml").read_text(encoding="utf-8")


def test_a_non_numeric_answer_to_a_numeric_field_is_refused(tmp_path: Path):
    code, shown = drive(
        script(model=rejected_then_accepted("context_window", "lots")), tmp_path)
    assert code == 0
    assert "not a whole number" in shown
    assert "context_window =" not in (tmp_path / "models.toml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# Nobody there


def test_input_ending_early_writes_nothing(tmp_path: Path):
    """EOF part way through is not an empty answer, and must not leave half a file."""
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, pytest.raises(init.Abandoned):
        init.run(root=tmp_path, inp=io.StringIO(WINDOWS_ROOT + "\n"), out=out,
                 stamp="20260908-120000", tmp=Path(tmp))
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "models.toml").exists()


def test_main_refuses_when_stdin_is_not_a_terminal(monkeypatch, capsys):
    """Otherwise it blocks on a pipe, or reads a default from silence."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert init.main(out=io.StringIO()) == 2
    assert "not a terminal" in capsys.readouterr().err


def test_main_reports_an_abandoned_run_without_a_traceback():
    out = io.StringIO()
    assert init.main(inp=io.StringIO(""), out=out) == 2
    assert "Nothing was written" in out.getvalue()


# --------------------------------------------------------------------------------------
# The registration block


def test_the_registration_block_names_the_resolved_command(tmp_path: Path):
    _, shown = drive(script(), tmp_path)
    assert "mcpServers" in shown
    assert "delegate-local" in shown
    assert "claude-delegate-local-mcp" in shown


def test_the_wsl_form_carries_the_distribution_and_a_windows_cd(monkeypatch):
    """Under WSL the block has to go through `wsl.exe`, and `--cd` takes a Windows path.

    The repository path is passed as a string: `Path("/mnt/c/repo")` on the Windows suite is
    a `WindowsPath` and stringifies with backslashes, so the test would silently exercise
    the untranslatable branch instead of the one it names. `registration` only prints its
    argument, which is why a string is the honest input.
    """
    monkeypatch.setattr(init, "is_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    block = init.registration("/mnt/c/repo")
    assert '"wsl.exe"' in block
    assert "Ubuntu-24.04" in block
    assert r"C:\\repo" in block          # JSON-escaped, since the block is JSON
    assert "/mnt/c spelling is rejected" in block


def test_a_repository_inside_the_distribution_says_so_rather_than_guessing(monkeypatch):
    """There is no Windows form of `/home/you/repo`, and an invented one would look right."""
    monkeypatch.setattr(init, "is_wsl", lambda: True)
    block = init.registration("/home/you/repo")
    assert "Windows path of this repository" in block
