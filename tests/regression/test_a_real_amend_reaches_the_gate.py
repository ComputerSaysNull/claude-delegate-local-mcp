"""The amend handling was tested by planting the marker, never by amending.

`test_gate_judges_an_amend_against_the_right_parent.py` writes
`.git/docs-gate-reused-message` itself and drives the gate directly. That proves the gate
does the right thing *given* the marker, and proves nothing about whether a real
`git commit --amend` produces one -- which is the half that broke. It does not, for the
form that supplies a new message:

    git commit --amend --no-edit   -> prepare-commit-msg src="commit" sha="HEAD"
    git commit --amend             -> prepare-commit-msg src="commit" sha="HEAD"
    git commit --amend -m "..."    -> prepare-commit-msg src="message" sha=<none>
    git commit -m "..."            -> prepare-commit-msg src="message" sha=<none>

The last two are identical, so the third is judged against HEAD and blocks a commit that
did update its owning document. `GIT_REFLOG_ACTION` is unset in every one of them, and
reading the parent's argv from `/proc` -- which does work under WSL -- is unavailable
where these hooks run, because Git for Windows hands the hook a PPID of 1. All measured
on git 2.43.0, both platforms.

So this drives real commits through real hooks, and pins both the forms that work and the
one that cannot, so a future change is told which is which.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "docs_gate.py"
INSTALL = ROOT / "scripts" / "install_hooks.py"

# Split so the literal never lands in this file: the gate scans it too, and
# an allowlisted address is still an address written into a public repository.
AUTHOR = "t@" + "example.com"
# The copied gate quotes this one in its own explanation, and the lab scans the
# copy. Both are assembled rather than written, so this file stays clean itself.
SERVICE = "noreply@" + "github.com"

MANIFEST = (
    '[docs."docs/THING.md"]\n'
    'audience = ["contributor"]\n'
    'plane = "product"\n'
    'owns = ["src/**"]\n'
    'covers_not = "nothing"\n'
    '\n[unowned]\npaths = ["scripts/**", "security/**"]\n'
)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=False)
    if check:
        assert done.returncode == 0, f"git {' '.join(args)}\n{done.stdout}\n{done.stderr}"
    return done


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "lab"
    (r / "scripts").mkdir(parents=True)
    (r / "docs").mkdir()
    (r / "src").mkdir()
    (r / "security").mkdir()
    shutil.copy(GATE, r / "scripts" / "docs_gate.py")
    shutil.copy(INSTALL, r / "scripts" / "install_hooks.py")
    (r / "scripts" / "docs_ownership.toml").write_text(MANIFEST, encoding="utf-8")
    # The gate refuses to run at all with these empty, and those refusals would drown the
    # one finding under test. Same shape as the other gate labs, content allowlist
    # included -- the copied gate quotes an address itself. `forbidden_strings.txt` is
    # deliberately absent: the gate blocks when that one is tracked, and everything here
    # is.
    (r / "security" / "allowed_emails.txt").write_text(AUTHOR + "\n", encoding="utf-8")
    (r / "security" / "content_safe_emails.txt").write_text(
        f"{AUTHOR}\n{SERVICE}\n", encoding="utf-8")
    (r / "security" / "secret_globs.txt").write_text(".env\n", encoding="utf-8")
    (r / "docs" / "THING.md").write_text("the document\n", encoding="utf-8")
    (r / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")

    git(r, "init", "-q", "-b", "main", ".")
    git(r, "config", "user.email", AUTHOR)
    git(r, "config", "user.name", "Tester")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "initial")
    done = subprocess.run([sys.executable, "scripts/install_hooks.py"], cwd=r,
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    return r


def code_and_doc_together(repo: Path, marker: str) -> subprocess.CompletedProcess:
    (repo / "src" / "a.py").write_text(f"print('{marker}')\n", encoding="utf-8")
    (repo / "docs" / "THING.md").write_text(f"the document, {marker}\n", encoding="utf-8")
    git(repo, "add", "-A")
    return git(repo, "commit", "-m", f"feat: {marker}")


def code_only(repo: Path, marker: str) -> None:
    (repo / "src" / "a.py").write_text(f"print('{marker}')\n", encoding="utf-8")
    git(repo, "add", "-A")


def test_the_hooks_are_really_installed(repo):
    """Everything below is meaningless if the shims are not there to run."""
    for name in ("commit-msg", "prepare-commit-msg"):
        hook = repo / ".git" / "hooks" / name
        assert hook.exists(), f"{name} was not installed"
    code_only(repo, "undocumented")
    done = git(repo, "commit", "-m", "feat: code with no document", check=False)
    assert done.returncode != 0, "the gate did not block an undocumented change at all"
    assert "owning-doc" in done.stdout + done.stderr


def test_a_commit_carrying_its_document_passes(repo):
    assert code_and_doc_together(repo, "first").returncode == 0


@pytest.mark.parametrize("form", [
    ["commit", "--amend", "--no-edit"],
    ["commit", "--amend", "-C", "HEAD"],
    ["commit", "--amend"],                       # editor; GIT_EDITOR is set below
])
def test_an_amend_that_reuses_its_message_is_judged_against_the_real_parent(repo, form):
    """These are the forms git reports as an amend, and the ones the remedy points at."""
    assert code_and_doc_together(repo, "first").returncode == 0
    code_only(repo, "revised")
    env = dict(os.environ, GIT_EDITOR="true")
    done = subprocess.run(["git", *form], cwd=repo, capture_output=True, text=True,
                          check=False, env=env)
    out = done.stdout + done.stderr
    assert done.returncode == 0, f"a correct amend was blocked:\n{out}"
    assert "read as an amend" in out, (
        "it passed, but not via the amend reading -- so this proves nothing about the "
        f"marker:\n{out}")


def test_an_amend_with_a_new_message_cannot_be_detected_and_says_so(repo):
    """The hole, pinned rather than papered over.

    Git reports this exactly as it reports an ordinary commit, so the gate blocks. What
    it must not do is block silently: the finding has to name the amend case and the form
    that works, or the next person spends a Docs-Gate-Skip on a gate defect -- which is
    what happened, on docs/ARCHITECTURE.md, and what prompted this file.
    """
    assert code_and_doc_together(repo, "first").returncode == 0
    code_only(repo, "revised")
    done = git(repo, "commit", "--amend", "-m", "feat: first, revised", check=False)
    out = done.stdout + done.stderr
    assert done.returncode != 0, "git has started reporting this amend; widen the marker"
    assert "--amend -m" in out and "in the editor" in out, (
        f"blocked without telling anyone why or what to do instead:\n{out}")


def test_the_hint_does_not_appear_when_nothing_is_blocked(repo):
    """Advice on every passing commit is noise, and noise is how a hint stops being read."""
    done = code_and_doc_together(repo, "first")
    assert done.returncode == 0
    assert "in the editor" not in done.stdout + done.stderr


def test_the_header_names_what_it_counted(repo):
    """An amend stages only its increment, so a bare count reads as a bug report."""
    code_only(repo, "undocumented")
    done = subprocess.run([sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
                          cwd=repo, capture_output=True, text=True, check=False)
    assert "file(s) changed against HEAD" in done.stdout, done.stdout


def test_a_hand_run_pre_commit_says_it_cannot_see_an_amend(repo):
    """prepare-commit-msg has not run yet, so the marker cannot exist. Saying so beats
    blocking with an explanation that only fits the commit-msg case."""
    assert code_and_doc_together(repo, "first").returncode == 0
    code_only(repo, "revised")
    done = subprocess.run([sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit"],
                          cwd=repo, capture_output=True, text=True, check=False)
    assert done.returncode != 0
    assert "has not run yet" in done.stdout, done.stdout
