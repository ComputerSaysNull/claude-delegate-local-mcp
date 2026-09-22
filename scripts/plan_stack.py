#!/usr/bin/env python
"""Derive a local stack of branches and print the commands that would publish its front.

This plans and does not publish. It reads git and GitHub and mutates neither, so it can be
run at any time, on any branch, without moving anything. Its predecessor did both, and the
half that published was invisible to the operator's hook: those pushes were subprocesses of
an already-approved script, where `PreToolUse` only ever sees a command run as a tool call.
Printing the commands instead puts each push, each `pr create` and each `pr merge` back in
front of that prompt.

**Only the front branch is planned**, because merging it invalidates everything behind it.
The merge is a squash, so it replaces the branch's commits with one new commit and the next
branch is still based on the OLD tip: a plain `git rebase main` then replays a commit whose
content is already upstream and conflicts on `CHANGELOG.md`, the one file every commit
touches. Every move is therefore `--onto <new parent> <old parent> <branch>` against tips
read before anything moved, which is exactly what goes stale the moment a merge lands. Run
this again after each merge.

The stack is derived, never configured. A hardcoded order goes stale the moment a branch is
added, and it fails silently: the missing branch is simply not restacked and drifts behind
the rewritten history until a later rebase conflicts for no visible reason.

Two temporary files are written, for the pull request body and the gate payload, because a
body carrying newlines cannot go on a command line. Neither is in the repository and
neither reaches GitHub except as the text of the pull request the operator then approves.
The body is the front branch's newest CHANGELOG section plus a Verification section read
from `--verification <file>` -- never the commit body, which is shorter on purpose.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = "main"

# Every git subcommand this script is allowed to use. Enforced by its own test rather than
# by convention, because "read-only" is the whole claim: a `fetch` or a `checkout` added
# here later would move the operator's tree from something that promises not to.
READ_ONLY_GIT = frozenset({"branch", "merge-base", "rev-parse", "log", "show"})


def run(cmd: list[str], *, check: bool = True) -> str:
    """Text output of `cmd`, decoded as UTF-8 whatever the console code page says.

    `text=True` alone decodes with the locale encoding, which is cp1252 on this host, and
    an em-dash in a CHANGELOG heading then raises UnicodeDecodeError from a git call that
    otherwise succeeded.
    """
    p = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if check and p.returncode:
        raise SystemExit(f"failed: {' '.join(cmd)}\n{out}")
    return out


def git(*args: str, check: bool = True) -> str:
    if args and args[0] not in READ_ONLY_GIT:
        raise SystemExit(
            f"plan_stack refuses to run `git {args[0]}`: it plans and does not publish, "
            f"and a subcommand that moves anything belongs in the skill that asks first."
        )
    return run(["git", *args], check=check)


def gh(*args: str, check: bool = True) -> str:
    # Both, because pull requests and issues draw from one counter and the number check
    # has to ask each. Listing is the only thing this script needs from GitHub.
    if args[:2] not in (("pr", "list"), ("issue", "list")):
        raise SystemExit(
            f"plan_stack refuses to run `gh {' '.join(args)}`: reading is `pr list` or "
            f"`issue list`, and anything that publishes belongs in the skill that asks "
            f"first."
        )
    return run(["gh", *args], check=check)


def order_stack(branches: list[str], is_ancestor) -> list[str]:
    """`branches` sorted base-first, or a refusal if they are not one chain.

    `is_ancestor(a, b)` says whether a is an ancestor of b, and is injected so this can be
    tested without a repository. A stack is a total order under that relation; anything
    else -- two branches off `main`, a fork half way up -- is not a stack, and guessing an
    order for it would rebase one sibling onto the other and silently invent history.
    """
    ordered = sorted(branches, key=lambda b: sum(is_ancestor(b, o) for o in branches),
                     reverse=True)
    for parent, child in itertools.pairwise(ordered):
        if not is_ancestor(parent, child):
            raise SystemExit(
                f"{parent!r} and {child!r} are not in one chain, so these branches are not "
                f"a stack. Ship them separately, or rebase one onto the other first."
            )
    return ordered


def local_stack() -> list[str]:
    names = [b for b in git("branch", "--format=%(refname:short)").splitlines()
             if b.strip() and b.strip() != MAIN]
    if not names:
        return []

    def is_ancestor(a: str, b: str) -> bool:
        if a == b:
            return False
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=ROOT, capture_output=True,
                              check=False).returncode == 0

    return order_stack([b.strip() for b in names], is_ancestor)


def claimed_number(branch: str) -> str | None:
    """The number the branch's newest CHANGELOG heading claims, or None.

    Read with `git show <branch>:CHANGELOG.md` rather than from the working tree, so every
    branch in the stack can be checked without any of them being checked out.
    """
    for line in git("show", f"{branch}:CHANGELOG.md").splitlines():
        m = re.match(r"## #(\d+)", line)
        if m:
            return m.group(1)
    return None


def newest_section(changelog: str) -> str | None:
    """Everything under the newest `## #N` heading, up to the next one, or None if empty.

    This is the pull request body's first half. The commit body is deliberately not: the
    commit stays short, and the pull request carries the CHANGELOG entry with its
    `Added` / `Changed` / `Fixed` subsections as they are.
    """
    lines = changelog.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"## #\d+", line):
            body: list[str] = []
            for rest in lines[i + 1:]:
                if rest.startswith("## "):
                    break
                body.append(rest)
            return "\n".join(body).strip() or None
    return None


def pr_body(section: str, verification: str) -> str:
    """The CHANGELOG section, then how the change was verified."""
    return f"{section}\n\n### Verification\n\n{verification.strip()}\n"


def next_number() -> int | None:
    """One past the highest number GitHub has issued, or None if it cannot be read.

    Pull requests and issues draw from one counter, so both are asked.
    """
    highest = 0
    for what in ("pr", "issue"):
        raw = gh(what, "list", "--state", "all", "--limit", "1",
                 "--json", "number", "-q", ".[0].number", check=False).strip()
        if raw.isdigit():
            highest = max(highest, int(raw))
    return highest + 1 if highest else None


def write_temp(text: str, suffix: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                     encoding="utf-8") as f:
        f.write(text)
        return f.name


def plan(stack: list[str], verification: str | None = None) -> int:
    branch, rest = stack[0], stack[1:]
    # Read before anything is proposed, because the restack below names these tips and the
    # first rebase is what makes them unfindable by branch name afterwards.
    old_tips = {b: git("rev-parse", b) for b in stack}
    title = git("log", "-1", "--format=%s", branch)
    claimed = claimed_number(branch)
    section = newest_section(git("show", f"{branch}:CHANGELOG.md"))

    print("stack, base first:")
    for b in stack:
        print(f"  {b:<44} {old_tips[b][:9]}  #{claimed_number(b) or '??'}")

    if claimed is None:
        print(f"\nREFUSED: {branch} has no CHANGELOG heading to check a number against.")
        return 1
    if section is None:
        print(f"\nREFUSED: {branch}'s newest CHANGELOG section is empty, and it is the pull "
              f"request body.")
        return 1
    if not (verification or "").strip():
        print(
            f"\nREFUSED: no verification for {branch}. The pull request body is the CHANGELOG "
            f"section plus how the change was verified -- the red-before-green result or the "
            f"check that fired before it passed, and the suites. Write it to a file and run "
            f"again with --verification <file>."
        )
        return 1
    body = pr_body(section, verification)

    issued = next_number()
    if issued is not None and int(claimed) != issued:
        print(
            f"\nREFUSED: {branch} claims #{claimed} but GitHub will issue #{issued}. "
            f"Correct the heading first -- the gate refuses a mismatch, so publishing it "
            f"this way only moves the failure later."
        )
        return 1

    payload = write_temp(
        json.dumps({"pull_request": {"number": int(claimed), "title": title, "body": body}}),
        ".json",
    )
    body_file = write_temp(body, ".md")

    print(f"\nfront branch: {branch}  ->  #{claimed}")
    print(f"title: {title}")
    print("\nrun these one at a time; each push and each gh call asks first:\n")
    print("  git fetch origin")
    print(f"  git checkout {MAIN}")
    print(f"  git reset --hard origin/{MAIN}")
    print(f"  git rebase --onto {MAIN} {old_tips[branch]}^ {branch}")
    for parent, child in itertools.pairwise(stack):
        print(f"  git rebase --onto {parent} {old_tips[child]}^ {child}")
    print(f"  git checkout {branch}")
    # `python` rather than this interpreter's own path: the emitted lines are meant to be
    # pasted, and CLAUDE.md's command list spells it that way. sys.executable's basename
    # would name an interpreter that need not be the one on PATH.
    print(f"  python scripts/docs_gate.py --mode pre-commit --pr-event {payload}")
    print(f"  git push -u origin {branch}")
    print(f"  gh pr create --base {MAIN} --head {branch} --title {title!r} "
          f"--body-file {body_file}")
    print(f"  gh pr checks {claimed} --watch")
    print(f"  gh pr merge {claimed} --squash --delete-branch")
    if rest:
        print(f"\nthen run this again: the merge rewrites {MAIN} and every tip above is "
              f"stale from that moment.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verification", metavar="FILE",
                    help="how the front branch was verified; becomes the pull request "
                         "body's Verification section")
    args = ap.parse_args()
    verification = (Path(args.verification).read_text(encoding="utf-8")
                    if args.verification else None)
    stack = local_stack()
    if not stack:
        print("no branches to ship")
        return 0
    return plan(stack, verification)


if __name__ == "__main__":
    raise SystemExit(main())
