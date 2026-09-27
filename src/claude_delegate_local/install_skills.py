r"""`--install-skills`: put the skills this package ships where a caller's tools find them.

The package carries the agent-file format as a skill rather than as documentation, because
documentation does not travel: the wheel holds this directory and nothing outside it. That
reaches the host but not the tools, which look in `.claude/skills/` under a project, so a
file in `site-packages` is invisible. This copies it across, and that is all it does.

**No interview, unlike `--init`.** The destination is the project you run it in and the
content is fixed, so it works with stdin closed -- which matters, because the caller most
likely to want it is an agent in a shell.

**An existing file is moved aside, never overwritten and never left in place**, by
`--init`'s `back_up`, for `--init`'s reason: refusing would leave a half-installed tree
fixable only by hand, and overwriting would destroy a deliberate edit. The backup name is
printed, because a backup nobody is told about is a file nobody deletes.

The name is validated on the way out, not trusted. `survey_agents` refuses a directory
name that could not be an agent name, so a skill shipped under one would install silently
and never load -- a failure with no symptom where it was caused.
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

    Sorted so output and the returned list are stable across filesystems: a directory
    walk is not ordered, and a test comparing lists would pass or fail by luck.
    """
    if not SKILLS_ROOT.is_dir():
        return []
    return sorted(d for d in SKILLS_ROOT.iterdir() if (d / "SKILL.md").is_file())


def install(project: Path | str, *, out=None, stamp: str | None = None) -> list[Path]:
    """Copy every shipped skill into `<project>/.claude/skills/`, returning what was written.

    Returns the written `SKILL.md` paths, not the directories: that file's presence is
    what discovery turns on.
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
