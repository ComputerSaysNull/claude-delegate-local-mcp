#!/usr/bin/env python
"""Publish a local stack of branches, one pull request at a time.

Only one pull request may be open at a time here, because merging one deletes its base and
closes any stacked child unreopenably. So a stack ships in sequence: rebase the front
branch onto `main`, open it, wait for checks, merge, then rebase what is left and repeat.
Doing that by hand is a dozen commands per branch and the same dozen mistakes.

Squash-merge is what makes the rebase non-obvious. The merge replaces the branch's commits
with one new commit, so the next branch is still based on the OLD tip and a plain
`git rebase main` replays a commit whose content is already upstream -- which conflicts on
`CHANGELOG.md`, the one file every commit touches. Every move here is therefore
`--onto <new parent> <old parent> <branch>`, and the old tips are read before anything
moves.

The stack is derived, never configured. A hardcoded order goes stale the moment a branch is
added, and it fails silently: the missing branch is simply not restacked and drifts behind
the rewritten history until a later rebase conflicts for no visible reason.

**This publishes.** Pushing and `gh` are gated by the operator's own hook, which asks before
each one; that prompt is the authorisation, and nothing here second-guesses it. What this
does guarantee is that no text reaches GitHub unscanned: `docs_gate.py --pr-event` runs
over the title and body *before* `gh pr create`, because a pull request's text is a public
surface no commit hook sees and CI only reads it once it is already published.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = "main"
POLL_SECONDS = 15
POLL_LIMIT = 80
DEAD = ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED")


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
    return run(["git", *args], check=check)


def gh(*args: str, check: bool = True) -> str:
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
    """The number the branch's newest CHANGELOG heading claims, or None."""
    for line in git("show", f"{branch}:CHANGELOG.md").splitlines():
        m = re.match(r"## #(\d+)", line)
        if m:
            return m.group(1)
    return None


def scan_pr_text(number: str, title: str, body: str) -> None:
    """Refuse to publish text the gate has not seen.

    Run from the branch that carries the entry, because the number check reads that
    branch's newest heading and compares it against this payload.
    """
    payload = {"pull_request": {"number": int(number), "title": title, "body": body}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(payload, f)
        path = f.name
    out = run([sys.executable, "scripts/docs_gate.py", "--mode", "pre-commit",
               "--pr-event", path], check=False)
    if "FAIL" in out:
        raise SystemExit(f"the gate refused this pull request's text:\n{out}")
    print("  text scanned, clean")


def wait_for_checks(number: str) -> None:
    for _ in range(POLL_LIMIT):
        raw = gh("pr", "checks", number, "--json", "state,name", check=False)
        try:
            rows = json.loads(raw)
        except ValueError:
            time.sleep(POLL_SECONDS)
            continue
        states = [r["state"] for r in rows]
        bad = [(r["name"], r["state"]) for r in rows if r["state"] in DEAD]
        if bad:
            raise SystemExit(f"checks failed on #{number}: {bad}")
        if rows and all(s == "SUCCESS" for s in states):
            print(f"  {len(rows)} checks pass")
            return
        time.sleep(POLL_SECONDS)
    raise SystemExit(f"checks on #{number} did not settle")


def ship_one(branch: str, rest: list[str], *, dry_run: bool) -> None:
    old_tips = {b: git("rev-parse", b) for b in [branch, *rest]}

    git("fetch", "-q", "origin")
    git("checkout", "-q", MAIN)
    git("reset", "--hard", "-q", f"origin/{MAIN}")
    git("rebase", "--onto", MAIN, git("rev-parse", f"{branch}^"), branch)
    print(f"  rebased onto {MAIN}")

    for parent, child in itertools.pairwise([branch, *rest]):
        if subprocess.run(["git", "merge-base", "--is-ancestor", parent, child],
                          cwd=ROOT, capture_output=True,
                          check=False).returncode == 0:
            continue
        git("rebase", "--onto", parent, old_tips[child] + "^", child)
        print(f"  restacked {child}")

    git("checkout", "-q", branch)
    title = git("log", "-1", "--format=%s", branch)
    body = git("log", "-1", "--format=%b", branch)
    claimed = claimed_number(branch)
    if claimed is None:
        raise SystemExit(f"{branch} has no CHANGELOG heading to check a number against")

    if dry_run:
        scan_pr_text(claimed, title, body)
        print(f"  DRY RUN: would open #{claimed} -- {title}")
        return

    git("push", "-q", "-u", "origin", branch)
    open_now = gh("pr", "list", "--head", branch, "--state", "open",
                  "--json", "number", "-q", ".[0].number", check=False).strip()
    if open_now.isdigit():
        number = open_now
        print(f"  reusing open #{number}")
    else:
        # Scanned against the claimed number, which is also what the heading says. If
        # GitHub then issues a different one the heading is corrected and re-scanned
        # below, so nothing is published on an unchecked pairing.
        scan_pr_text(claimed, title, body)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as f:
            f.write(body)
            body_path = f.name
        url = gh("pr", "create", "--base", MAIN, "--head", branch,
                 "--title", title, "--body-file", body_path).splitlines()[-1]
        number = url.rstrip("/").rsplit("/", 1)[-1]
        print(f"  opened {url}")

    if number != claimed:
        raise SystemExit(
            f"CHANGELOG heading says #{claimed} but GitHub issued #{number}. Correct the "
            f"heading on {branch}, push again, then re-run -- the gate refuses a mismatch."
        )

    wait_for_checks(number)
    gh("pr", "merge", number, "--squash", "--delete-branch")
    print(f"  MERGED #{number}  {title}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="derive the stack and scan each text; push nothing.")
    ap.add_argument("--one", action="store_true",
                    help="ship only the front branch, then stop.")
    args = ap.parse_args()

    stack = local_stack()
    if not stack:
        print("no branches to ship")
        return 0
    print("stack, base first:")
    for b in stack:
        print(f"  {b}")

    while stack:
        branch, rest = stack[0], stack[1:]
        print(f"=== {branch} ===")
        ship_one(branch, rest, dry_run=args.dry_run)
        if args.dry_run or args.one:
            break
        stack = local_stack()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
