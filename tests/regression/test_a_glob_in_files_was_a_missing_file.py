"""`files[]` took literal paths only, so a glob was resolved as a filename and refused.

Naming twenty files meant writing twenty absolute paths, and the shorthand everyone tried
first -- `/repo/src/*.py` -- reached `resolve_files` as a path, failed the existence check,
and came back as a missing file. ADR-0097 expands patterns server-side, before resolution,
so every match goes through the same four layers a hand-written path does.

The budget is the work, not the matching. All expansions go into **one** `prefetch` call,
because the token budget is per call: expanding batch by batch would hand each batch a full
budget and never enforce the total. A pattern that overflows the budget is reported in
`files_skipped` with `SKIP_OVER_TOTAL_BUDGET` rather than silently truncated.
"""

from __future__ import annotations

import os

from pathlib import Path

import pytest
from conftest import BASELINE_MISSING, baseline_blob

from claude_delegate_local import config as config_module
from claude_delegate_local.context import SKIP_OVER_TOTAL_BUDGET, prefetch
from claude_delegate_local.paths import expand_globs, has_magic, resolve_files

REPO = Path(__file__).resolve().parents[2]

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="EXPANSION IS UNPROVEN BY THIS RUN -- layer 1 needs a POSIX filesystem",
)


def _cfg(tmp_path: Path, **overrides: object) -> config_module.Config:
    return config_module.Config(  # type: ignore[arg-type]
        workspace_roots=(str(tmp_path),),
        respect_gitignore=False,
        **overrides,
    )


def _tree(tmp_path: Path, count: int = 3) -> Path:
    src = tmp_path / "src"
    (src / "deep").mkdir(parents=True)
    for i in range(count):
        (src / f"mod{i}.py").write_text(f"# module {i}\n", encoding="utf-8")
    (src / "deep" / "nested.py").write_text("# nested\n", encoding="utf-8")
    (src / "README.md").write_text("# readme\n", encoding="utf-8")
    return src


# ---- the predicate -----------------------------------------------------------------


def test_a_literal_path_is_not_a_pattern() -> None:
    assert not has_magic("/repo/src/main.py")
    assert has_magic("/repo/src/*.py")
    assert has_magic("/repo/src/**/*.py")
    assert has_magic("/repo/src/mod?.py")
    assert has_magic("/repo/src/[ab].py")


# ---- expansion ----------------------------------------------------------------------


@posix_only
def test_a_pattern_becomes_the_files_it_matches(tmp_path: Path) -> None:
    src = _tree(tmp_path)

    named, refusals = expand_globs(_cfg(tmp_path), [f"{src}/*.py"])

    assert not refusals
    assert [Path(p).name for p in named] == ["mod0.py", "mod1.py", "mod2.py"]


@posix_only
def test_literals_pass_through_untouched_and_keep_their_place(tmp_path: Path) -> None:
    """A mixed list is the ordinary case, and order is the caller's."""
    src = _tree(tmp_path)
    literal = str(src / "README.md")

    named, refusals = expand_globs(_cfg(tmp_path), [literal, f"{src}/*.py"])

    assert not refusals
    assert named[0] == literal
    assert len(named) == 4


@posix_only
def test_a_directory_match_is_not_prefetched(tmp_path: Path) -> None:
    """`src/*` matches `deep/`, which is a legitimate match and never a file to read."""
    src = _tree(tmp_path)

    named, _ = expand_globs(_cfg(tmp_path), [f"{src}/*"])

    assert all(Path(p).is_file() for p in named), named
    assert not any(Path(p).name == "deep" for p in named)


@posix_only
def test_recursive_patterns_reach_down(tmp_path: Path) -> None:
    src = _tree(tmp_path)

    named, refusals = expand_globs(_cfg(tmp_path), [f"{src}/**/*.py"])

    assert not refusals
    assert any(Path(p).name == "nested.py" for p in named)


# ---- what is refused, and told rather than dropped -----------------------------------


@posix_only
def test_a_pattern_matching_nothing_is_refused_not_ignored(tmp_path: Path) -> None:
    """The design call: silence here is indistinguishable from a typo that cost nothing."""
    _tree(tmp_path)

    named, refusals = expand_globs(_cfg(tmp_path), [f"{tmp_path}/src/*.rs"])

    assert not named
    assert len(refusals) == 1
    assert "matched no files" in refusals[0].reason


@posix_only
def test_a_pattern_anchored_outside_every_root_never_walks(tmp_path: Path) -> None:
    """The bound on the walk, checked before the walk rather than on its results."""
    _tree(tmp_path)

    named, refusals = expand_globs(_cfg(tmp_path), ["/etc/**/*.conf"])

    assert not named
    assert len(refusals) == 1
    assert "outside every workspace root" in refusals[0].reason


@posix_only
def test_a_relative_pattern_is_refused(tmp_path: Path) -> None:
    named, refusals = expand_globs(_cfg(tmp_path), ["src/*.py"])

    assert not named
    assert "relative pattern" in refusals[0].reason


@posix_only
def test_too_many_matches_is_refused_rather_than_truncated(tmp_path: Path) -> None:
    """Truncating would send a subset and say it was the answer."""
    src = _tree(tmp_path, count=10)

    named, refusals = expand_globs(_cfg(tmp_path, max_glob_matches=4), [f"{src}/*.py"])

    assert not named
    assert len(refusals) == 1
    assert "more than 4" in refusals[0].reason


@posix_only
def test_the_policy_still_judges_every_match(tmp_path: Path) -> None:
    """Expansion widens what may be *named*, and nothing about what is allowed."""
    src = _tree(tmp_path)
    (src / "id_rsa.py").write_text("# not really a key\n", encoding="utf-8")

    named, _ = expand_globs(_cfg(tmp_path), [f"{src}/*.py"])
    assert any(Path(p).name == "id_rsa.py" for p in named), "expansion does not filter"

    resolved, refusals = resolve_files(_cfg(tmp_path), named)

    assert any("id_rsa" in r.given for r in refusals), "layer 3 still fires on a match"
    assert all(Path(r.posix).name != "id_rsa.py" for r in resolved)


# ---- the budget, which is where the work actually is ---------------------------------


@posix_only
def test_an_overflowing_expansion_is_skipped_not_truncated(tmp_path: Path) -> None:
    """One prefetch over the whole expansion, and the overflow is reported as such.

    A budget small enough to stop partway is the point: the files that do not fit must
    come back as `SKIP_OVER_TOTAL_BUDGET`, not vanish.
    """
    src = tmp_path / "src"
    src.mkdir()
    for i in range(8):
        (src / f"mod{i}.py").write_text("x" * 4000, encoding="utf-8")

    # Both caps move together: config refuses a total below the per-file ceiling, since
    # no file could then ever be prefetched.
    cfg = _cfg(tmp_path, max_total_prefetch_tokens=1500, max_file_tokens=1500)
    named, refusals = expand_globs(cfg, [f"{src}/*.py"])
    assert not refusals

    resolved, _ = resolve_files(cfg, named)
    result = prefetch(cfg, resolved)

    assert result.files, "some files fit"
    assert result.skips, "and some did not"
    assert any(s.kind == SKIP_OVER_TOTAL_BUDGET for s in result.skips)
    assert len(result.files) + len(result.skips) == 8, "every match is accounted for"


# ---- the negative control ------------------------------------------------------------


def test_negative_control_the_committed_code_had_no_expansion() -> None:
    def show(path: str) -> str:
        blob = baseline_blob(path)
        if blob is None:
            pytest.skip(BASELINE_MISSING)
        return blob

    paths_blob = show("src/claude_delegate_local/paths.py")
    assert "def expand_globs" not in paths_blob, (
        "the baseline already expands; this control can no longer fail"
    )
    assert "import glob" not in paths_blob
    assert "def has_magic" not in paths_blob

    server_blob = show("src/claude_delegate_local/server.py")
    assert "expand_globs" not in server_blob
    # The line the bug lived on: the caller's list went straight to resolution.
    assert "resolved, refusals = resolve_files(cfg, files or [])" in server_blob


@posix_only
def test_negative_control_a_glob_was_refused_as_a_missing_file(tmp_path: Path) -> None:
    """What `main` did with a pattern: resolved it as a filename and failed to find it."""
    src = _tree(tmp_path)

    resolved, refusals = resolve_files(_cfg(tmp_path), [f"{src}/*.py"])

    assert not resolved
    assert len(refusals) == 1
    assert "*.py" in refusals[0].given
