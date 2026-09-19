"""Make the package importable without an editable install.

Deliberate: the test suite must run from a bare clone with nothing installed, so a
contributor's first `pytest` works before they have read anything.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

REPO = Path(__file__).resolve().parent.parent

# The commit a negative control compares against: `main` as it stood before the work that
# these controls prove the red of. **A fixed commit, never a branch name.**
#
# `main` was the obvious choice and is wrong in a way that only shows up later: the moment
# the branch merges, `main` contains the fix, the control's "this is absent from the
# baseline" assertion becomes false, and it fails for ever afterwards on `main` itself.
# That is not a flaky test -- it is a control that has quietly changed its question.
# Measured the hard way on 2026-09-19, when #250 merged and turned its own control red.
#
# A commit hash cannot drift like that. What it costs is that the claim is anchored: these
# controls say "absent as of this commit", which is exactly what red-before-green means.
BASELINE_COMMIT = "1ffce4ccbaaedb0a9fc76884b0371ebc5d6b7ae0"

BASELINE_MISSING = (
    "NEGATIVE CONTROL UNPROVEN BY THIS RUN -- the baseline commit is not in this "
    "checkout. A CI runner fetches a single commit of the merge ref, so history before it "
    "is absent; the control still runs from a full clone, where it was written."
)


def baseline_blob(path: str, ref: str = BASELINE_COMMIT) -> str | None:
    """`path` as it stood at `ref`, or None when `ref` is not in this checkout.

    Read from the object database rather than from disk, so the working tree is untouched
    and no stash is involved -- which is what lets a fix and the test proving its red land
    in the same commit.

    None means *no baseline was available*, never "no difference". A caller that treats
    the two the same turns a control into a check that cannot fail, which this repository
    has now found seven of. Skip on None, loudly, with `BASELINE_MISSING`.
    """
    found = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    return found.stdout if found.returncode == 0 else None
