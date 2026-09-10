r"""`--install-skills`: put the skills this package ships where a caller's tools find them.

The package carries the agent-file format as a skill rather than as documentation, because
documentation does not travel: the wheel holds this directory and nothing outside it. That
solves *reaching* the host. It does not solve reaching the tools, which look in
`.claude/skills/` under a project, so a file sitting in `site-packages` is still invisible.
This copies it across, and is the whole of what it does.

**No interview, unlike `--init`.** There is nothing to ask: the destination is the project
you run it in, and the content is fixed. So this one works with stdin closed, which matters
because the caller most likely to want it is an agent in a shell rather than a person.

**An existing file is moved aside, never overwritten and never left in place** -- `--init`'s
rule, reusing `--init`'s `back_up`, for the same reason. Refusing outright would leave a
half-installed tree whose only route forward is hand-editing, and overwriting would destroy
an edit someone made on purpose. The backup name is printed, because a backup nobody is
told about is a file nobody deletes.

The name is validated on the way out rather than trusted. `survey_agents` refuses a
directory name that could not be an agent name, so a skill shipped under one would install
silently and never load -- a failure with no symptom at the point it was caused.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

from .agents import _NAME_RE
from .init import back_up

SKILLS_ROOT = Path(__file__).resolve().parent / "skills"


def shipped_skills() -> list[Path]:
    """Every directory under `skills/` holding a `SKILL.md`, sorted.

    Sorted so the printed output and the returned list are stable between runs on
    different filesystems -- a directory walk is not ordered, and a test comparing lists
    would otherwise pass or fail by luck.
    """
    if not SKILLS_ROOT.is_dir():
        return []
    return sorted(d for d in SKILLS_ROOT.iterdir() if (d / "SKILL.md").is_file())


def install(project: Path | str, *, out=None, stamp: str | None = None) -> list[Path]:
    """Copy every shipped skill into `<project>/.claude/skills/`, returning what was written.

    Returns the written `SKILL.md` paths rather than the directories, because that is the
    file whose presence the discovery code actually turns on.
    """
    root = Path(project)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    written: list[Path] = []

    for source in shipped_skills():
        if not _NAME_RE.fullmatch(source.name):
            raise ValueError(
                f"{source.name} cannot be an agent name, so installing it would produce a "
                "skill that is never found. Rename the shipped directory."
            )
        target = root / ".claude" / "skills" / source.name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if (moved := back_up(target, stamp=stamp)) is not None and out is not None:
            print(f"  moved aside: {moved.name}", file=out)
        shutil.copy2(source / "SKILL.md", target)
        written.append(target)
        if out is not None:
            print(f"  wrote: {target}", file=out)

    return written


def main(*, out=None) -> int:
    stream = out if out is not None else sys.stdout
    skills = shipped_skills()
    if not skills:
        print("this package ships no skills", file=sys.stderr)
        return 1

    root = Path.cwd()
    print(f"Installing {len(skills)} skill(s) into {root / '.claude' / 'skills'}", file=stream)
    written = install(root, out=stream)
    print(
        f"\nDone. Check they loaded with list_agents -- a name under `agents` is the only\n"
        f"success. Installed: {', '.join(p.parent.name for p in written)}",
        file=stream,
    )
    return 0
