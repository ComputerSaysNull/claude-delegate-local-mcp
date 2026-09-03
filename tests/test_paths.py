r"""The four-layer path policy.

Every layer gets a test that asserts it **fires** on a real violation, not merely that a
good path passes. That is this repository's own rule, and it is not theoretical: four
checks have already been found here that could not fail, two of them with passing tests
written against the bug.

Layers 2 and 3 are pure functions of a string and run anywhere. Layers 1 and 4 need a
real POSIX filesystem -- symlinks that resolve, a git work tree -- and the server runs in
WSL, so on Windows they are skipped with a reason that says what is unproven rather than
adding to a green count.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_delegate_local import paths
from claude_delegate_local.config import Config
from claude_delegate_local.paths import (
    LAYER_EXT,
    LAYER_FORM,
    LAYER_GITIGNORE,
    LAYER_ROOTS,
    LAYER_SECRET,
    PathPolicyError,
    PathRefused,
    Refusal,
    load_secret_globs,
    resolve_all,
    resolve_workdir,
)

UNPROVEN = (
    "LAYERS 1 AND 4 UNPROVEN BY THIS RUN -- this is not a pass. They need a POSIX "
    "filesystem (resolving symlinks, a git work tree) and the server runs in WSL. Run "
    "them there -- see CONTRIBUTING.md: wsl -d Ubuntu-24.04 -e bash -lc "
    "'cd <repo> && ~/.venvs/delegate/bin/python -m pytest tests/test_paths.py'"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason=UNPROVEN)

GLOBS = ".env\n.env.*\n*.pem\n*secret*\n.git/**\n.ssh/**\n.docker/config.json\n"


def cfg(tmp_path, **over) -> Config:
    """A config rooted at `tmp_path`, with layer 4 off unless a test asks for it.

    Layer 4 shells out to git, so leaving it on by default would put a subprocess in
    every test of the other three layers. The tests that are about layer 4 turn it on.
    """
    globs = tmp_path / "globs.txt"
    if not globs.exists():
        globs.write_text(GLOBS, encoding="utf-8")
    kw = {
        "workspace_roots": (os.path.realpath(tmp_path),),
        "secret_globs_file": str(globs),
        "respect_gitignore": False,
    }
    kw.update(over)
    return Config(**kw)  # type: ignore[arg-type]


def touch(path, content: str = "x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


# ---- layer 2: the extension allowlist --------------------------------------------


@posix_only
def test_a_disallowed_extension_is_refused(tmp_path):
    target = touch(tmp_path / "bundle.wasm")
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [target])
    (refusal,) = e.value.refusals
    assert refusal.layer == LAYER_EXT
    assert ".wasm" in refusal.reason


@posix_only
@pytest.mark.parametrize("name", [".gitignore", "Makefile", "Dockerfile", ".env-example"])
def test_an_allowlisted_whole_filename_is_allowed(tmp_path, name):
    """The wrinkle worth a test of its own.

    These entries are written in the allowlist with a leading dot -- `.gitignore`,
    `.makefile` -- but `Path(".gitignore").suffix` is the empty string, so suffix
    matching alone refuses exactly the entries somebody added on purpose.
    """
    target = touch(tmp_path / name)
    resolved = resolve_all(cfg(tmp_path), [target])
    assert len(resolved) == 1


@posix_only
def test_an_extensionless_file_that_is_not_allowlisted_is_still_refused(tmp_path):
    """The other half of the same wrinkle: name matching must not admit everything."""
    target = touch(tmp_path / "id_rsa_backup_notes")
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [target])
    assert e.value.refusals[0].layer == LAYER_EXT


# ---- layer 3: the secret denylist -------------------------------------------------


@posix_only
def test_a_secret_glob_match_is_refused(tmp_path):
    """A file the extension allowlist is happy with, which layer 3 stops anyway.

    `.json` is allowlisted, so this reaches layer 3 -- which is the only way to observe
    layer 3 at all. Most credential files never get that far; see the ordering test below.
    """
    target = touch(tmp_path / "client_secret.json", "{}")
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [target])
    (refusal,) = e.value.refusals
    assert refusal.layer == LAYER_SECRET
    assert "*secret*" in refusal.reason


@posix_only
def test_the_allowlist_is_checked_before_the_denylist(tmp_path):
    """Order is documented, and it decides which message the caller gets.

    A `.pem` never reaches layer 3: its extension is not allowlisted, so layer 2 refuses
    it first. That is deliberate -- allowlist first, denylist as the second net -- and it
    is pinned because the reverse reads equally plausible and both refuse the file, so
    nothing else in the suite would notice the order flipping.
    """
    target = touch(tmp_path / "deploy.pem")
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [target])
    assert e.value.refusals[0].layer == LAYER_EXT


def test_a_directory_glob_fires_on_a_file_inside_that_directory():
    """`.git/**` matches neither the basename nor the absolute path.

    Matching only those two -- the obvious reading, and the one this was first written
    with -- leaves five of the shipped patterns unable to fire at all, which is half of
    layer 3 present and decorative. The pattern has to be checked against `.git/config`,
    a suffix of the path and neither of the two.
    """
    globs = (".git/**",)
    assert paths._check_secret("x", "/mnt/c/proj/.git/config", globs) is not None
    assert paths._check_secret("x", "/mnt/c/proj/.git/refs/heads/main", globs) is not None
    # The negative half: it must not swallow every path that merely contains the word.
    assert paths._check_secret("x", "/mnt/c/proj/src/git/config.py", globs) is None


@posix_only
def test_a_multi_component_glob_fires_end_to_end(tmp_path):
    """The same matching, through `resolve_all` rather than the helper.

    `.docker/config.json` is the one shipped pattern that spans directories *and* ends in
    an allowlisted extension, so it is the only end-to-end proof available that layer 3
    sees more than a basename.
    """
    target = touch(tmp_path / ".docker" / "config.json", "{}")
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [target])
    (refusal,) = e.value.refusals
    assert refusal.layer == LAYER_SECRET
    assert ".docker/config.json" in refusal.reason


@posix_only
def test_the_denylist_is_case_insensitive(tmp_path):
    """Paths cross from a case-insensitive filesystem, so the match must not depend on it."""
    target = touch(tmp_path / "MySecretStuff.py")
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [target])
    assert e.value.refusals[0].layer == LAYER_SECRET


def test_a_missing_denylist_file_stops_the_policy_rather_than_passing(tmp_path):
    """The silent-disable failure mode, asserted directly.

    An unreadable list that returned no patterns would be indistinguishable from one that
    matched nothing -- a layer that cannot fire, trusted because it is there.
    """
    conf = cfg(tmp_path, secret_globs_file=str(tmp_path / "absent.txt"))
    with pytest.raises(PathPolicyError) as e:
        load_secret_globs(conf)
    assert "not skipped when absent" in str(e.value)


def test_a_denylist_of_only_comments_stops_the_policy_too(tmp_path):
    empty = tmp_path / "commented.txt"
    empty.write_text("# everything is fine\n\n   \n", encoding="utf-8")
    with pytest.raises(PathPolicyError):
        load_secret_globs(cfg(tmp_path, secret_globs_file=str(empty)))


def test_the_shipped_denylist_denies_the_thing_it_exists_for(tmp_path):
    """Pins the claim that the server and the git gate share one list.

    The tests above use a small fixture list. This one reads the file that actually ships,
    so a pattern deleted from it fails here rather than only in production.
    """
    shipped = Path(__file__).resolve().parent.parent / "security" / "secret_globs.txt"
    conf = cfg(tmp_path, secret_globs_file=str(shipped))
    globs = load_secret_globs(conf)
    assert paths._check_secret("x", "/proj/.env", globs) is not None
    assert paths._check_secret("x", "/proj/id_rsa", globs) is not None
    assert paths._check_secret("x", "/proj/.ssh/known_hosts", globs) is not None
    assert paths._check_secret("x", "/proj/src/main.py", globs) is None


# ---- layer 1: the workspace roots -------------------------------------------------


@posix_only
def test_a_path_outside_every_root_is_refused(tmp_path):
    inside = tmp_path / "root"
    inside.mkdir()
    outside = touch(tmp_path / "elsewhere" / "x.py")
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(inside),))
    with pytest.raises(PathRefused) as e:
        resolve_all(conf, [outside])
    assert e.value.refusals[0].layer == LAYER_ROOTS


@posix_only
def test_a_symlink_inside_a_root_pointing_out_of_it_is_refused(tmp_path):
    """The test the whole `realpath`-first ordering exists for.

    The link is inside the root, so a containment check on the path *as written* passes.
    Only resolving first catches it -- which is why resolution is not an optimisation of
    layer 1 but the substance of it.
    """
    root = tmp_path / "root"
    root.mkdir()
    secret = touch(tmp_path / "outside" / "loot.py")
    link = root / "innocent.py"
    link.symlink_to(secret)

    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),))
    with pytest.raises(PathRefused) as e:
        resolve_all(conf, [str(link)])
    (refusal,) = e.value.refusals
    assert refusal.layer == LAYER_ROOTS
    assert "loot.py" in refusal.reason, "the message names the resolved target, not the link"


@posix_only
def test_a_symlink_staying_inside_the_root_is_allowed(tmp_path):
    """Otherwise the previous test would pass against a policy that refuses all symlinks."""
    root = tmp_path / "root"
    real = touch(root / "sub" / "real.py")
    link = root / "alias.py"
    link.symlink_to(real)
    resolved = resolve_all(cfg(tmp_path, workspace_roots=(os.path.realpath(root),)), [str(link)])
    assert resolved[0].posix == os.path.realpath(real)


@posix_only
def test_a_sibling_directory_sharing_a_name_prefix_is_not_inside_the_root(tmp_path):
    """`/a/proj` must not contain `/a/proj-secrets`, which a bare startswith would say."""
    root = tmp_path / "proj"
    root.mkdir()
    target = touch(tmp_path / "proj-secrets" / "x.py")
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),))
    with pytest.raises(PathRefused) as e:
        resolve_all(conf, [target])
    assert e.value.refusals[0].layer == LAYER_ROOTS


@posix_only
def test_a_missing_file_is_refused_not_silently_dropped(tmp_path):
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [str(tmp_path / "typo.py")])
    assert "no such file" in e.value.refusals[0].reason


@posix_only
def test_a_directory_is_refused(tmp_path):
    (tmp_path / "src").mkdir()
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [str(tmp_path / "src")])
    assert "directory" in e.value.refusals[0].reason


@posix_only
def test_a_relative_path_is_refused(tmp_path):
    """It would resolve against the server's working directory, which is not the caller's."""
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), ["src/foo.py"])
    (refusal,) = e.value.refusals
    assert refusal.layer == LAYER_ROOTS
    assert "relative" in refusal.reason


@posix_only
def test_an_untranslatable_path_names_the_boundary_not_a_layer(tmp_path):
    """A UNC share never reaches the policy, so it must not claim a layer that never ran."""
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [r"\\fileserver\share\x.py"])
    (refusal,) = e.value.refusals
    assert refusal.layer == LAYER_FORM
    assert "path form" in str(refusal)


# ---- layer 4: gitignore -----------------------------------------------------------


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "nogitconfig"), "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    (root / ".gitignore").write_text("build/\n*.generated.py\n", encoding="utf-8")
    return root


@posix_only
def test_a_gitignored_path_is_refused(tmp_path):
    root = _repo(tmp_path)
    target = touch(root / "build" / "out.py")
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),), respect_gitignore=True)
    with pytest.raises(PathRefused) as e:
        resolve_all(conf, [target])
    assert e.value.refusals[0].layer == LAYER_GITIGNORE


@posix_only
def test_the_same_path_is_allowed_with_the_layer_switched_off(tmp_path):
    """Pins that the previous test measured layer 4 and not something else about the file."""
    root = _repo(tmp_path)
    target = touch(root / "build" / "out.py")
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),), respect_gitignore=False)
    assert len(resolve_all(conf, [target])) == 1


@posix_only
def test_a_tracked_file_is_not_ignored_even_when_a_pattern_matches_it(tmp_path):
    """git's own semantics, relied on rather than reimplemented. Committed is not ignored."""
    root = _repo(tmp_path)
    target = touch(root / "schema.generated.py")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "nogitconfig"), "HOME": str(tmp_path)}
    subprocess.run(
        ["git", "-C", str(root), "add", "-f", "schema.generated.py"], check=True, env=env
    )
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),), respect_gitignore=True)
    assert len(resolve_all(conf, [target])) == 1


@posix_only
def test_a_path_outside_any_repository_is_not_treated_as_ignored(tmp_path):
    """git exits 128 there. Treating a fatal exit as "ignored" would refuse every file
    that happens to live outside a work tree."""
    target = touch(tmp_path / "loose.py")
    conf = cfg(tmp_path, respect_gitignore=True)
    assert len(resolve_all(conf, [target])) == 1


@posix_only
def test_an_absent_git_stops_the_policy_rather_than_passing(tmp_path, monkeypatch):
    """The other silent-disable mode: no git means no output, which reads as nothing ignored."""
    target = touch(tmp_path / "a.py")

    def no_git(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    with pytest.raises(PathPolicyError) as e:
        resolve_all(cfg(tmp_path, respect_gitignore=True), [target])
    assert "not skipped when absent" in str(e.value)


# ---- the whole call ---------------------------------------------------------------


@posix_only
def test_every_refusal_is_reported_not_just_the_first(tmp_path):
    """One round trip has to be enough to fix all of them.

    A first-failure-wins policy makes a five-file review cost five dispatches to
    discover, one refusal at a time.
    """
    bad_ext = touch(tmp_path / "a.wasm")
    secret = touch(tmp_path / "b_secret.json", "{}")
    missing = str(tmp_path / "c.py")
    good = touch(tmp_path / "d.py")

    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [bad_ext, secret, missing, good])

    assert {r.layer for r in e.value.refusals} == {LAYER_EXT, LAYER_SECRET, LAYER_ROOTS}
    assert len(e.value.refusals) == 3
    assert e.value.total == 4


@posix_only
def test_one_bad_path_refuses_the_whole_call(tmp_path):
    """Skips proceed and are reported; refusals do not proceed at all."""
    good = touch(tmp_path / "good.py")
    bad = touch(tmp_path / "bad.pem")
    with pytest.raises(PathRefused) as e:
        resolve_all(cfg(tmp_path), [good, bad])
    assert "nothing was sent to the model" in str(e.value)


@posix_only
def test_the_same_file_named_twice_is_resolved_once(tmp_path):
    """Two spellings of one path would otherwise be inlined -- and paid for -- twice."""
    target = touch(tmp_path / "a.py")
    resolved = resolve_all(cfg(tmp_path), [target, target, str(tmp_path / "." / "a.py")])
    assert len(resolved) == 1


@posix_only
def test_results_keep_caller_order(tmp_path):
    """Prompt ordering belongs to context.py (ADR-0011).

    Sorting here would hide a caller-order dependency downstream rather than let the test
    for it fail.
    """
    b = touch(tmp_path / "b.py")
    a = touch(tmp_path / "a.py")
    assert [r.posix for r in resolve_all(cfg(tmp_path), [b, a])] == [
        os.path.realpath(b),
        os.path.realpath(a),
    ]


@posix_only
def test_an_empty_file_list_is_not_an_error(tmp_path):
    assert resolve_all(cfg(tmp_path), []) == ()


# ---- the refusal message itself ---------------------------------------------------


def test_a_refusal_names_the_layer_and_a_remedy():
    """These strings reach a model, which acts on them. They are part of the contract."""
    rendered = str(
        Refusal(given="C:/p/.env", layer=LAYER_SECRET, reason="it matches.", remedy="Drop it.")
    )
    assert "C:/p/.env" in rendered
    assert "layer 3, secret denylist" in rendered
    assert "Drop it." in rendered


# --- the workdir surface (layer 1 only) ---------------------------------------------------


@posix_only
def test_a_workdir_inside_a_root_resolves_to_its_real_location(tmp_path):
    root = tmp_path / "root"
    (root / "proj").mkdir(parents=True)
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),))
    assert resolve_workdir(conf, str(root / "proj")) == os.path.realpath(root / "proj")


@posix_only
def test_a_workdir_symlinked_out_of_every_root_is_refused(tmp_path):
    """The escape this item exists to close, and the one a written-path check cannot see.

    The link sits inside a root, so containment passes on the path as written. Binding it
    read-write into the sandbox would hand a delegated model a directory nobody allowed --
    and unlike a file read, a workdir bind is writable for the whole call.
    """
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "loot"
    outside.mkdir(parents=True)
    link = root / "innocent"
    link.symlink_to(outside, target_is_directory=True)

    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),))
    with pytest.raises(PathRefused) as e:
        resolve_workdir(conf, str(link))
    (refusal,) = e.value.refusals
    assert refusal.layer == LAYER_ROOTS
    assert "loot" in refusal.reason, "the message names where it lands, not where it sits"


@posix_only
def test_a_workdir_symlink_staying_inside_the_root_is_allowed(tmp_path):
    """Otherwise the test above would pass against a policy that refused every symlink."""
    root = tmp_path / "root"
    real = root / "sub" / "proj"
    real.mkdir(parents=True)
    link = root / "alias"
    link.symlink_to(real, target_is_directory=True)
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),))
    assert resolve_workdir(conf, str(link)) == os.path.realpath(real)


@posix_only
def test_a_workdir_outside_every_root_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),))
    with pytest.raises(PathRefused) as e:
        resolve_workdir(conf, str(other))
    assert e.value.refusals[0].layer == LAYER_ROOTS


@posix_only
def test_workdir_roots_are_a_separate_surface_from_workspace_roots(tmp_path):
    """Reading a project and working in it are different grants, so they are configured
    separately. A model may be allowed to read three checkouts and write in one."""
    readable = tmp_path / "readable"
    workable = tmp_path / "workable"
    readable.mkdir()
    workable.mkdir()
    conf = cfg(
        tmp_path,
        workspace_roots=(os.path.realpath(readable), os.path.realpath(workable)),
        workdir_roots=(os.path.realpath(workable),),
    )
    assert resolve_workdir(conf, str(workable)) == os.path.realpath(workable)
    with pytest.raises(PathRefused, match="outside every workdir root"):
        resolve_workdir(conf, str(readable))


@posix_only
def test_empty_workdir_roots_falls_back_to_the_workspace_roots(tmp_path):
    """The default, and the reason the second setting is not required to use the first."""
    root = tmp_path / "root"
    root.mkdir()
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),), workdir_roots=())
    assert resolve_workdir(conf, str(root)) == os.path.realpath(root)


def test_a_relative_workdir_is_refused(tmp_path):
    """The server has its own working directory and will not share the caller's."""
    with pytest.raises(PathRefused, match="not an absolute path"):
        resolve_workdir(cfg(tmp_path), "relative/proj")


@posix_only
def test_a_workdir_that_is_a_file_or_missing_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    conf = cfg(tmp_path, workspace_roots=(os.path.realpath(root),))
    afile = touch(root / "notadir.py")
    with pytest.raises(PathRefused, match="not a directory"):
        resolve_workdir(conf, str(afile))
    with pytest.raises(PathRefused, match="does not exist"):
        resolve_workdir(conf, str(root / "nope"))


# ---- opening what was resolved ---------------------------------------------------
#
# The gap these close was open until ADR-0049: `resolve_all` returns a string, a handler
# opened that string later, and in between an adversary holding a read-write workdir bind
# under `run_bash` could redirect the path. Every test below stages a real violation and
# asserts the refusal, rather than asserting that a good path opens.
#
# `resolve_all` cannot run on Windows at all -- `to_posix` is for the WSL crossing and
# mangles a native path -- so anything reached through it is POSIX-only, like layers 1 and
# 4 above. The two tests that drive `_prove_descriptor` with a hand-built entry are not,
# deliberately: the inode branch is the one Windows itself uses, and a branch whose only
# negative test is skipped on the platform that runs it is a check that cannot fail.


def entry_for(path) -> paths.ResolvedPath:
    """A `ResolvedPath` built directly, for tests that must not go through layer 1."""
    return paths.ResolvedPath(given=str(path), posix=os.path.realpath(path))


def test_the_proof_refuses_a_descriptor_that_points_somewhere_else(tmp_path, monkeypatch):
    """The procfs branch, driven where there is no procfs, so the comparison is covered.

    What procfs answers is measured elsewhere; what this pins is that an answer naming
    anything other than the approved path is refused rather than shrugged at.
    """
    target = Path(touch(tmp_path / "a.txt", "x"))
    monkeypatch.setattr(paths, "_opened_path", lambda fd: "/etc/shadow")
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        with pytest.raises(PathRefused) as e:
            paths._prove_descriptor(entry_for(target), fd)
        assert [r.layer for r in e.value.refusals] == [paths.LAYER_OPENED]
        assert "/etc/shadow" in str(e.value)
        # And it passes when procfs names the approved path, so the refusal above is not
        # this branch refusing everything it is handed.
        monkeypatch.setattr(paths, "_opened_path", lambda fd: entry_for(target).posix)
        paths._prove_descriptor(entry_for(target), fd)
    finally:
        os.close(fd)


def test_the_inode_branch_refuses_when_the_path_stops_naming_the_open_file(
    tmp_path, monkeypatch
):
    """The branch Windows uses, where no procfs can say where a descriptor points.

    Two real files: the descriptor is on one and the entry names the other, which is what
    a swap looks like to this comparison. Staged this way rather than by unlinking an open
    file because Windows refuses that outright -- measured -- so the swap cannot be acted
    out there at all.
    """
    held = Path(touch(tmp_path / "held.txt", "held"))
    named = Path(touch(tmp_path / "named.txt", "named"))
    monkeypatch.setattr(paths, "_opened_path", lambda fd: None)

    fd = os.open(held, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        with pytest.raises(PathRefused) as e:
            paths._prove_descriptor(entry_for(named), fd)
        assert [r.layer for r in e.value.refusals] == [paths.LAYER_OPENED]
        # And it passes for the file it really is holding, so the test above is not
        # passing because the comparison refuses everything.
        paths._prove_descriptor(entry_for(held), fd)
    finally:
        os.close(fd)


@posix_only
def test_open_resolved_reads_the_file_it_approved(tmp_path):
    target = tmp_path / "a.txt"
    touch(target, "hello\n")
    opened = paths.open_resolved(resolve_all(cfg(tmp_path), [str(target)])[0], "rb")
    with opened.handle as fh:
        assert fh.read() == b"hello\n"
    assert opened.created is False


@posix_only
def test_an_unsupported_mode_is_a_programming_error_not_a_refusal(tmp_path):
    """A bad mode is our bug, so it must not arrive dressed as the model's fault."""
    target = touch(tmp_path / "a.txt", "x")
    entry = resolve_all(cfg(tmp_path), [target])[0]
    with pytest.raises(ValueError, match="unsupported mode"):
        paths.open_resolved(entry, "a+")


@posix_only
def test_a_link_planted_after_approval_is_refused_by_the_open_itself(tmp_path):
    """O_NOFOLLOW, which is safe here because `realpath` already collapsed the path."""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    target = tmp_path / "a.txt"
    touch(target, "legitimate\n")
    entry = resolve_all(cfg(tmp_path), [str(target)])[0]

    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(PathRefused) as e:
        paths.open_resolved(entry, "rb")
    assert [r.layer for r in e.value.refusals] == [paths.LAYER_OPENED]
    assert "symbolic link" in str(e.value)


@posix_only
def test_the_descriptor_proof_catches_the_same_link_without_o_nofollow(
    tmp_path, monkeypatch
):
    """The two halves are independent, so neither is load-bearing alone.

    With O_NOFOLLOW folded out the open succeeds and hands us the *target*; procfs says so
    and the proof refuses. Measured before it was written: without the flag the descriptor
    reports the link's target rather than the path that was asked for.
    """
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    target = tmp_path / "a.txt"
    touch(target, "legitimate\n")
    entry = resolve_all(cfg(tmp_path), [str(target)])[0]
    target.unlink()
    target.symlink_to(outside)

    monkeypatch.setattr(paths, "_NOFOLLOW", 0)
    with pytest.raises(PathRefused) as e:
        paths.open_resolved(entry, "rb")
    assert [r.layer for r in e.value.refusals] == [paths.LAYER_OPENED]
    assert os.path.realpath(outside) in str(e.value)


@posix_only
def test_a_refused_write_has_not_truncated_anything(tmp_path, monkeypatch):
    """The reason "wb" sets O_CREAT and never O_TRUNC.

    O_TRUNC empties the file at open time, before any proof can run, which would leave the
    write path with a report of what was already destroyed rather than a guard. This test
    fails against O_TRUNC, which is the only thing that makes it worth having.
    """
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("must survive\n", encoding="utf-8")
    target = tmp_path / "a.txt"
    touch(target, "legitimate\n")
    entry = resolve_all(cfg(tmp_path), [str(target)])[0]
    target.unlink()
    target.symlink_to(outside)

    # O_NOFOLLOW off, so the open reaches the swapped target and the *ordering* is what is
    # under test rather than the flag that would have stopped it one step earlier.
    monkeypatch.setattr(paths, "_NOFOLLOW", 0)
    with pytest.raises(PathRefused):
        paths.open_resolved(entry, "wb")
    assert outside.read_text(encoding="utf-8") == "must survive\n"


@posix_only
def test_wb_reports_whether_it_created_the_file(tmp_path):
    target = tmp_path / "new.txt"
    entry = resolve_all(cfg(tmp_path), [str(target)], must_exist=False)[0]
    opened = paths.open_resolved(entry, "wb")
    with opened.handle as fh:
        fh.write(b"first")
    assert opened.created is True

    again = paths.open_resolved(resolve_all(cfg(tmp_path), [str(target)])[0], "wb")
    with again.handle as fh:
        fh.write(b"xy")
    assert again.created is False
    # Shorter than what was there, so a missing truncation would leave "xyrst".
    assert target.read_bytes() == b"xy"


@posix_only
def test_r_plus_b_holds_one_descriptor_for_the_whole_read_modify_write(tmp_path):
    """What makes `edit_file` safe by construction: there is no second open to race."""
    target = tmp_path / "a.txt"
    touch(target, "one two three\n")
    opened = paths.open_resolved(resolve_all(cfg(tmp_path), [str(target)])[0], "r+b")
    with opened.handle as fh:
        before = fh.read()
        fh.seek(0)
        fh.write(before.replace(b"two", b"2"))
        fh.truncate()
    assert target.read_text(encoding="utf-8") == "one 2 three\n"
