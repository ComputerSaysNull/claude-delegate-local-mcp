"""An API token in a file with an ordinary name passed every layer.

The content check found private-key armour and nothing else (ADR-0096), and the denylist
matches names, so a GitHub or Anthropic token in `config.local.json` was readable whenever
the gitignore layer did not stop it -- and that layer is a net for local files, not a
secret check. The shared marker table now also holds well-known token prefixes, each shaped
tightly enough to stay off ordinary source (ADR-0121).

**A token is never echoed.** The marker a refusal or a diagnostic names is a label such as
"a GitHub token", because quoting the match, as a key's header is quoted, would hand the
model the very credential it was refused.

Every token here is assembled at runtime from repeated fragments, so no literal one occurs
in this file and the sandbox walk does not cover this test during a run_bash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_delegate_local import config as config_module
from claude_delegate_local import sandbox
from claude_delegate_local.paths import (
    PathRefused,
    key_material_marker,
    load_secret_globs,
    open_resolved,
    resolve_files,
    secret_match,
)

REPO = Path(__file__).resolve().parents[2]

# Assembled, never written out. See the module docstring.
TOKENS = {
    "a GitHub token": "gh" + "p_" + "aB3" * 12,
    "a GitHub fine-grained token": "github" + "_pat_" + "x1Y_" * 20 + "ab",
    "an Anthropic API key": "sk-" + "ant-api03-" + "Zq9_" * 20,
    "an OpenAI API key": "sk-" + "proj-" + "Qw3-" * 12,
    "an AWS access key id": "AK" + "IA" + "Q7RT" * 4,
    "a Slack token": "xo" + "xb-123456789012-" + "aB3d" * 4,
    "a GitLab token": "gl" + "pat-" + "aB3d_" * 4,
    "a Hugging Face token": "h" + "f_" + "abcdEFGH" * 4 + "zz",
    "a Stripe secret key": "sk" + "_live_" + "aB3d" * 7,
    "an npm token": "np" + "m_" + "aB3" * 12,
}


def _cfg(tmp_path: Path) -> config_module.Config:
    return config_module.Config(  # type: ignore[arg-type]
        workspace_roots=(str(tmp_path),), respect_gitignore=False)


# ---- the primitive -----------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(TOKENS))
def test_each_kind_is_found_and_named_without_the_token(label: str) -> None:
    token = TOKENS[label]
    blob = ('{"name": "svc", "auth": "' + token + '"}\n').encode()

    marker = key_material_marker(blob)

    assert marker is not None and label in marker, marker
    assert token not in marker and token[-12:] not in marker, "the marker leaks the token"


def test_near_misses_do_not_match() -> None:
    """The false-positive control: what sits closest to each shape without being one."""
    near_misses = [
        # AWS's own documented example id, quoted in docs everywhere.
        b"aws_access_key_id = AKIAIOSFODNN7EXAMPLE",
        # Too short to be the real thing, and prose about the prefixes.
        b"GitHub classic tokens start with ghp_ and fine-grained ones with github_pat_.",
        b"token = 'ghp_abc123'",
        # A prefix buried inside a longer base64 run is not a token boundary.
        b"QUJDghp_" + b"aB3" * 12 + b"xyz",
        b"see https://huggingface.co/settings/tokens for an hf_ token",
        b"",
    ]

    for blob in near_misses:
        assert key_material_marker(blob) is None, blob


def test_the_repository_itself_does_not_trip_it() -> None:
    """Run over this project's own sources and tests, as the key check's own control does."""
    offenders = [
        path for path in sorted(REPO.glob("src/**/*.py")) + sorted(REPO.glob("docs/**/*.md"))
        + sorted(REPO.glob("tests/**/*.py"))
        if key_material_marker(path.read_bytes()[:4096]) is not None
    ]

    assert not offenders, offenders


# ---- the names the denylist missed -------------------------------------------------


@pytest.mark.parametrize("name", [
    "infra/prod.tfvars", "infra/terraform.tfstate", "infra/terraform.tfstate.backup",
    "home/kubeconfig", "repo/.claude/settings.local.json",
])
def test_local_credential_files_are_denied_by_name(name: str) -> None:
    """Files that hold credentials in plain text by design, under names nothing caught."""
    globs = load_secret_globs(config_module.Config(workspace_roots=("/work",)))  # type: ignore[arg-type]

    assert secret_match("/work/" + name, globs) is not None, name


def test_their_ordinary_neighbours_are_not() -> None:
    globs = load_secret_globs(config_module.Config(workspace_roots=("/work",)))  # type: ignore[arg-type]

    for name in ("infra/main.tf", "infra/variables.tf", ".claude/settings.json",
                 "docs/kubeconfig.md"):
        assert secret_match("/work/" + name, globs) is None, name


# ---- layer one: the file the model asked for ---------------------------------------


def test_a_token_in_an_ordinary_file_is_refused_without_echoing_it(tmp_path: Path) -> None:
    token = TOKENS["an Anthropic API key"]
    ordinary = tmp_path / "config.local.json"
    ordinary.write_text('{"api_key": "' + token + '"}\n', encoding="utf-8")

    resolved, refusals = resolve_files(_cfg(tmp_path), [str(ordinary)])
    assert not refusals, "the name passes every path layer -- that is the bug"

    with pytest.raises(PathRefused) as caught:
        open_resolved(resolved[0], "rb", scan_bytes=4096)

    assert "an Anthropic API key" in str(caught.value)
    assert token not in str(caught.value)


# ---- layer two: the file a shell could have opened ----------------------------------


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX only")
def test_the_sandbox_covers_a_token_and_names_it_by_label(tmp_path: Path) -> None:
    token = TOKENS["a GitHub token"]
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text('{"token": "' + token + '"}', encoding="utf-8")
    (home / "notes.md").write_text("Tokens start with ghp_, in prose.", encoding="utf-8")

    shadows = sandbox.discover_secret_shadows(
        _cfg(tmp_path), sandbox.SandboxRequest(command="true", home=str(home)))

    covered = {Path(s.path).name: s.matched for s in shadows}
    assert covered.get("settings.json") == "content:a GitHub token", covered
    assert "notes.md" not in covered
