"""`.env.example` is tracked, is meant to be readable, and `.env.*` was denying it.

Found 2026-09-09 by running this repository's suite nested for the first time, which the
ADR-0065 fix made possible: inside the sandbox `.env.example` was a character device owned
by `nobody` and read as `Permission denied`, while `models.toml.example` was readable --
because the denylist names `models.toml` exactly and `.env.*` matches every suffix.

`security/secret_globs.txt` says of that pair, in its own comment, that "the example file
stays readable, which is the control on this pair". One of the two was not.

It fails closed, so it is a usability bug rather than a hole. It still changes what a
delegated model may read, which is why the fix is an entry in the list rather than a detail
of some other commit.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from claude_delegate_local.config import Config
from claude_delegate_local.paths import load_secret_globs, secret_match

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def repo_globs() -> tuple[str, ...]:
    """The real committed list, not a fixture.

    A fixture list would test the matcher and say nothing about what this repository
    actually denies, which is the half that was wrong.
    """
    cfg = Config(  # type: ignore[arg-type]
        workspace_roots=(str(REPO_ROOT),),
        secret_globs_file=str(REPO_ROOT / "security" / "secret_globs.txt"),
    )
    return load_secret_globs(cfg)


def test_a_tracked_example_file_is_readable() -> None:
    globs = repo_globs()
    assert secret_match("/repo/.env.example", globs) is None
    # The sibling that was already right, kept here so the pair is asserted together --
    # the asymmetry between them is what the comment in the list promises does not exist.
    assert secret_match("/repo/models.toml.example", globs) is None


def test_the_real_files_are_still_denied() -> None:
    """The negative control, and the reason the fix is a negation rather than a narrowing.

    Narrowing `.env.*` to the suffixes seen today would be an allowlist by omission: a
    `.env.production` added next month would be readable and nothing would say so. These
    assertions fail if the exemption is written too wide.
    """
    globs = repo_globs()
    assert secret_match("/repo/.env", globs) == ".env"
    assert secret_match("/repo/.env.local", globs) == ".env.*"
    assert secret_match("/repo/.env.production", globs) == ".env.*"
    assert secret_match("/repo/.env.bak-20260910-120000", globs) == ".env.*"
    assert secret_match("/repo/models.toml", globs) == "models.toml"


def test_an_exemption_does_not_leak_across_patterns() -> None:
    """Exempting one name must not turn off the pattern for a lookalike.

    `.env.example.local` is not the tracked example file; it still matches `.env.*` and
    must stay denied. Without this, an exemption implemented as a substring or prefix test
    would pass the two tests above while opening a hole.
    """
    globs = repo_globs()
    assert secret_match("/repo/.env.example.local", globs) == ".env.*"
    assert secret_match("/repo/.env.examples", globs) == ".env.*"


# --- the two enforcers, asserted against each other ----------------------------------------


def gate_module():
    """Import `scripts/docs_gate.py` as a module.

    It is a script rather than a package, and deliberately importable from a bare clone
    with nothing installed, so it cannot be reached by a normal import.
    """
    spec = importlib.util.spec_from_file_location("_docs_gate", REPO_ROOT / "scripts/docs_gate.py")
    if spec is None or spec.loader is None:  # pragma: no cover - the file exists
        pytest.skip("scripts/docs_gate.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_docs_gate"] = module
    spec.loader.exec_module(module)
    return module


# Each is a relative path as `git ls-files` prints it, which is what the gate reads, and
# also a suffix `secret_match` sees. Deliberately includes both verdicts: a consistency
# test over exempt paths alone would pass against two enforcers that agree on nothing else.
SHARED_CASES = [
    ".env.example",
    "models.toml.example",
    ".env",
    ".env.local",
    ".env.production",
    ".env.example.local",
    "models.toml",
    "id_rsa",
    "README.md",
    "src/claude_delegate_local/paths.py",
]


@pytest.mark.parametrize("rel", SHARED_CASES)
def test_the_gate_and_the_server_read_one_list_the_same_way(rel: str) -> None:
    """`paths.py` and `docs_gate.py` cannot share code, so they must share a verdict.

    The gate runs from a bare clone and imports nothing from the package, which is why
    each holds its own reader. That duplication is the standing risk CLAUDE.md names --
    extend one and enforcement goes back to being asymmetric, silently. It already had:
    the gate exempted every `*.example` and the server exempted none, and nothing
    reported the difference until a nested test run did.

    This asserts only agreement on *whether* a path is denied. The pattern reported may
    differ, since the two walk the list against different candidate spellings.
    """
    gate = gate_module()
    globs = list(repo_globs())

    server_denied = secret_match(f"/repo/{rel}", globs) is not None
    gate_denied = gate.denying_glob(rel, globs) is not None

    assert server_denied == gate_denied, (
        f"{rel}: paths.secret_match says denied={server_denied}, "
        f"docs_gate.denying_glob says denied={gate_denied}"
    )
