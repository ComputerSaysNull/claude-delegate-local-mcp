"""Make the package importable without an editable install.

Deliberate: the test suite must run from a bare clone with nothing installed, so a
contributor's first `pytest` works before they have read anything.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

REPO = Path(__file__).resolve().parent.parent

# Where a negative control looks for the code it is proving the absence of. `main` is the
# local branch, `origin/main` the tracking ref a fetched clone has instead. Ordered, and
# both are tried, because which exists depends on how the checkout was made rather than on
# anything about the work.
_BASELINE_REFS = ("main", "origin/main")

BASELINE_MISSING = (
    "NEGATIVE CONTROL UNPROVEN BY THIS RUN -- this checkout has no main ref to compare "
    "against. A CI runner checks out a single commit of the merge ref, so neither `main` "
    "nor `origin/main` resolves; the control still runs locally, where it was written."
)


def baseline_blob(path: str) -> str | None:
    """`path` as it stands on the branch this work departed from, or None.

    Read from the object database rather than from disk, so the working tree is untouched
    and no stash is involved -- which is what lets a fix and the test proving its red land
    in the same commit.

    None means *no baseline was available*, never "no difference". A caller that treats
    the two the same turns a control into a check that cannot fail, which this repository
    has now found seven of. Skip on None, loudly, with `BASELINE_MISSING`.
    """
    for ref in _BASELINE_REFS:
        found = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=REPO, capture_output=True, text=True, check=False,
        )
        if found.returncode == 0:
            return found.stdout
    return None
