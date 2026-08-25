#!/usr/bin/env python
"""Generate STATUS.md from PLAN.md and git.

STATUS and PLAN are the pair most likely to contradict each other when both are written
by hand, so STATUS is derived and never edited. It is also **fixed size**: current values
only, overwritten every run. History belongs in PLAN (finished items keep their date and
commit), CHANGELOG (what shipped) and JOURNAL (what was hard). A status file that
accumulates is a second changelog with none of the discipline.

    python scripts/gen_status.py           # write
    python scripts/gen_status.py --check   # exit 1 if stale
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.pycache_prefix = tempfile.mkdtemp(prefix="cdl-status-pyc-")

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "PLAN.md"
TARGET = ROOT / "STATUS.md"

DONE, ACTIVE, TODO, CANCELLED = "✅", "🔄", "⬜", "❌"
MARKS = (DONE, ACTIVE, TODO, CANCELLED)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=False).stdout.strip()


def parse_plan() -> list[dict]:
    """Flatten PLAN.md into items tagged with their milestone heading."""
    items: list[dict] = []
    milestone = "(none)"
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        h = re.match(r"^## (.+)$", line)
        if h:
            milestone = h.group(1).strip()
            continue
        m = re.match(r"^- (%s) (.+)$" % "|".join(map(re.escape, MARKS)), line)
        if m:
            items.append({"mark": m.group(1), "text": m.group(2).strip(),
                          "milestone": milestone})
    return items


def render() -> str:
    items = parse_plan()
    if not items:
        raise SystemExit("PLAN.md yielded no items -- has its format changed?")

    by_mark = {k: [i for i in items if i["mark"] == k] for k in MARKS}

    # The current milestone is the first with anything unfinished. Deferred work is
    # excluded: it is a backlog, not a phase, and would otherwise always be "current".
    current = None
    for i in items:
        if i["mark"] in (ACTIVE, TODO) and not i["milestone"].startswith("Deferred"):
            current = i["milestone"]
            break

    in_milestone = [i for i in items if i["milestone"] == current] if current else []
    done_here = sum(1 for i in in_milestone if i["mark"] == DONE)

    active = by_mark[ACTIVE]
    nxt = [i for i in by_mark[TODO] if i["milestone"] == current][:3]

    tests = git("ls-files", "tests")
    ntests = len([x for x in tests.splitlines() if x.endswith(".py")])
    adrs = len(re.findall(r"^## ADR-", (ROOT / "DECISIONS.md").read_text(encoding="utf-8"), re.M))

    out = [
        "<!-- GENERATED FILE -- do not edit.",
        "     Source: PLAN.md plus git, via scripts/gen_status.py.",
        "     Fixed size by design: current values only, overwritten each run. History",
        "     lives in PLAN.md, CHANGELOG.md and JOURNAL.md. -->",
        "",
        "# Status",
        "",
        f"**Current phase:** {current or 'all planned work complete'}"
        + (f" — {done_here} of {len(in_milestone)} items done" if in_milestone else ""),
        "",
        "## In progress",
        "",
    ]
    out += [f"- {i['text']}" for i in active] or ["- *nothing marked in progress*"]
    out += ["", "## Next up", ""]
    out += [f"- {i['text']}" for i in nxt] or ["- *nothing queued in this phase*"]

    out += ["", "## Progress by phase", "",
            "| Phase | Done | Active | To do | Cancelled |",
            "| --- | --- | --- | --- | --- |"]
    seen: list[str] = []
    for i in items:
        if i["milestone"] not in seen:
            seen.append(i["milestone"])
    for ms in seen:
        g = [x for x in items if x["milestone"] == ms]
        row = [str(sum(1 for x in g if x["mark"] == k)) for k in MARKS]
        out.append(f"| {ms} | {row[0]} | {row[1]} | {row[2]} | {row[3]} |")

    total_done = len(by_mark[DONE])
    total_open = len(by_mark[ACTIVE]) + len(by_mark[TODO])
    out += ["", f"**Overall:** {total_done} done, {total_open} open, "
                f"{len(by_mark[CANCELLED])} cancelled.", ""]

    out += ["## Repository", "",
            f"- Branch `{git('rev-parse', '--abbrev-ref', 'HEAD') or 'unknown'}`, "
            f"{len(git('log', '--oneline').splitlines())} commit(s)",
            f"- {len(git('ls-files').splitlines())} tracked files, "
            f"{ntests} test file(s), {adrs} decision record(s)",
            f"- Working tree: {'clean' if not git('status', '--porcelain') else 'has uncommitted changes'}",
            ""]

    recent = git("log", "--format=%h %s", "-5").splitlines()
    if recent:
        out += ["## Recent commits", ""]
        out += [f"- `{c.split(' ', 1)[0]}` {c.split(' ', 1)[1]}" for c in recent if " " in c]
        out.append("")

    blocked = [i["text"] for i in items
               if i["mark"] == TODO and re.search(r"\bblocked\b", i["text"], re.I)]
    if blocked:
        out += ["## Blocked", ""] + [f"- {b}" for b in blocked] + [""]

    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    new = render()
    old = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""

    if args.check:
        # Commit counts and tree cleanliness move with every commit, so a byte comparison
        # would fail constantly and teach everyone to ignore this check. Compare only the
        # parts derived from PLAN.md, which is the drift that actually matters.
        def stable(text: str) -> str:
            cut = text.split("## Repository")[0]
            return "\n".join(l.rstrip() for l in cut.splitlines()).strip()

        if stable(old) == stable(new):
            print(f"ok: {TARGET.name} matches PLAN.md")
            return 0
        print(f"STALE: {TARGET.name} does not match PLAN.md. "
              f"Run: python scripts/gen_status.py")
        return 1

    TARGET.write_text(new, encoding="utf-8")
    print(f"wrote {TARGET.name} ({len(new.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
