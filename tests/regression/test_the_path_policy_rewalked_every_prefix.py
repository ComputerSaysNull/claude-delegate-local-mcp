"""The path policy re-walked the same directory prefix once per candidate.

`_resolve_one` called `os.path.realpath` on every candidate. Candidates in one batch share
deep directory prefixes, so the kernel re-walked the same prefix thousands of times: 13.5
`lstat` per candidate, 80.7s of a 140.9s policy pass over 2,000 candidates, profiled
2026-09-15. Matching was 1.1% of it, so the denylist was never the target.

Measured on /mnt/c 2026-09-16, resolving the same candidate set:

    2,000 files over 389 directories    30.40s -> 4.47s
    147 tracked files over 5            0.858s -> 0.019s

**The cache covers the prefix and never the final component**, which is the whole of its
safety argument. `realpath` resolves symlinks in the last segment too, so caching a whole
resolved path would let a symlink inside a workspace root keep its inside-the-root spelling
and pass layer 1 -- the one check it exists to fail. The last segment is resolved on every
candidate, every time; only the directory above it is remembered, and only for the life of
one `_resolve_many` call.

Named after the bug, per the project's convention.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_delegate_local import paths
from claude_delegate_local.config import Config

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="LAYER 1 UNPROVEN BY THIS RUN -- run it under WSL, where the server lives.",
)

FILES = 50


def cfg(root: Path, **over) -> Config:
    kw = {"workspace_roots": (str(root),), "respect_gitignore": False}
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


@pytest.fixture
def batch(tmp_path: Path) -> tuple[Path, list[str]]:
    """Many files under one deep prefix -- the shape a search produces."""
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    names = []
    for i in range(FILES):
        f = deep / f"mod{i}.py"
        f.write_text("x", encoding="utf-8")
        names.append(str(f))
    return tmp_path, names


@posix_only
def test_the_prefix_is_walked_once_not_once_per_candidate(batch, monkeypatch):
    """The bug, counted at the call that does the walking.

    Counting `os.path.realpath` is the direct expression of the behaviour: the fix is
    precisely that a shared prefix stops being handed to it once per candidate.
    """
    root, names = batch
    calls: list[str] = []
    real = os.path.realpath

    def counted(p, *a, **kw):
        calls.append(str(p))
        return real(p, *a, **kw)

    monkeypatch.setattr(paths.os.path, "realpath", counted)
    survivors = paths.resolve_permitted(cfg(root), names, must_exist=True)

    assert len(survivors) == FILES, "the batch stopped resolving; this is not a speed test"
    assert len(calls) < FILES, (
        f"realpath ran {len(calls)} times for {FILES} candidates sharing one directory, so "
        "the prefix is still being re-walked once per candidate")


@posix_only
def test_existence_is_asked_once_not_twice(batch, monkeypatch):
    """The other half of the same profile: `exists` then `isfile` is two stats for one fact.

    37.1s over 4,000 calls for 2,000 candidates, profiled 2026-09-15. One `stat` carries
    both, and its mode says which -- so the count must not exceed one per candidate.
    """
    root, names = batch
    calls: list[str] = []
    real = os.stat

    def counted(p, *a, **kw):
        calls.append(str(p))
        return real(p, *a, **kw)

    monkeypatch.setattr(paths.os, "stat", counted)
    survivors = paths.resolve_permitted(cfg(root), names, must_exist=True)

    assert len(survivors) == FILES
    assert len(calls) <= FILES, (
        f"stat ran {len(calls)} times for {FILES} candidates, so layer 1 is still asking "
        "the filesystem the same question twice and discarding the first answer")


@posix_only
def test_a_symlink_out_of_the_root_is_still_refused(batch):
    """The security control, and the reason the final component is never cached.

    The symlink sits in the same directory as the batch above, so its prefix is already in
    the cache by the time it is resolved. If the fix ever caches a whole resolved path, this
    file keeps its inside-the-root spelling and layer 1 waves it through.
    """
    root, names = batch
    outside = root.parent / "outside"
    outside.mkdir(exist_ok=True)
    secret = outside / "loot.py"
    secret.write_text("x", encoding="utf-8")

    link = Path(names[0]).parent / "innocent.py"
    link.symlink_to(secret)

    survivors = paths.resolve_permitted(cfg(root), [*names, str(link)], must_exist=True)

    resolved = {s.posix for s in survivors}
    assert str(secret) not in resolved, (
        "a symlink escaped the workspace root: the final component was resolved from cache "
        "rather than freshly, which is the one thing this cache may never do")
    assert not any(p.endswith("innocent.py") for p in resolved), (
        "the symlink was admitted under its inside-the-root spelling, so layer 1 compared "
        "the wrong path against the roots")


@posix_only
def test_every_candidate_still_resolves_to_the_same_answer(batch):
    """The control.

    A cache that returned a subtly different string would still be fast and would still
    pass the count above. This pins the answer itself against `os.path.realpath`, which is
    what the policy compared against before the cache existed.
    """
    root, names = batch

    survivors = paths.resolve_permitted(cfg(root), names, must_exist=True)

    by_given = {s.given: s.posix for s in survivors}
    for name in names:
        assert by_given[name] == os.path.realpath(name), (
            f"{name} resolved to {by_given[name]}, which is not what realpath says it is")
