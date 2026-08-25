"""Make the package importable without an editable install.

Deliberate: the test suite must run from a bare clone with nothing installed, so a
contributor's first `pytest` works before they have read anything.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
