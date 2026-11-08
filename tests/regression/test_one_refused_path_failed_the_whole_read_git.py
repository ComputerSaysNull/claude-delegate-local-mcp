"""One refused path failed a whole `read_git` call that named several.

A delegation asked `git show <commit> -- <four paths>`; one, `contract/VERSION`, has no
extension on the allowlist, and the call was refused outright, so the three readable paths
cost a turn. A refused path is now dropped and named, and the rest are read. All of them
refused is still a refusal: running git with none left would read every path.
"""

import subprocess

from test_tools import git_call, git_only, repo  # noqa: F401 -- `repo` is a fixture


def _commit_with_version(r) -> str:
    (r / "contract").mkdir()
    (r / "contract" / "VERSION").write_text("secret-ish 1.15\n", encoding="utf-8")
    (r / "kept.py").write_text("x = 99\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
           "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "add", "-A"], cwd=r, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "both"], cwd=r, env=env, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, capture_output=True,
                          text=True, check=True).stdout.strip()


@git_only
def test_the_refused_path_is_dropped_and_named_and_the_rest_is_read(repo):  # noqa: F811
    head = _commit_with_version(repo)
    result = git_call(repo.parent, repo=str(repo), command="show",
                      args=[head], paths=["kept.py", "contract/VERSION"])
    assert not result.is_error, result.content
    assert "x = 99" in result.content
    assert "left out of paths" in result.content
    assert "'contract/VERSION'" in result.content
    assert "secret-ish" not in result.content


@git_only
def test_every_path_refused_is_still_a_refusal(repo):  # noqa: F811
    head = _commit_with_version(repo)
    result = git_call(repo.parent, repo=str(repo), command="show",
                      args=[head], paths=["contract/VERSION"])
    assert result.is_error
    assert "x = 99" not in result.content
    assert "secret-ish" not in result.content
