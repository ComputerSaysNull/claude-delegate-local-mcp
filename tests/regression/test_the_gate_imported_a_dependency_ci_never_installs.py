"""The gate imported PyYAML, which CI's gate job deliberately never installs.

CI runs `scripts/docs_gate.py` on a bare interpreter -- no `pip install` in that job, on
purpose, because the gate is meant to need nothing but the standard library. A check that
parsed skill frontmatter with `import yaml` passed every local run (the commit hook prefers
the project venv, which has PyYAML as a test dependency) and would have failed every pull
request with `ModuleNotFoundError` before a single check ran.

So this reads the gate's own imports rather than running it: whatever this interpreter
happens to have installed cannot hide a missing dependency from an AST.
"""

import ast
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[2] / "scripts" / "docs_gate.py"


def _imported_top_level_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_gate_imports_only_the_standard_library():
    imported = _imported_top_level_modules(GATE.read_text(encoding="utf-8"))
    outside = sorted(imported - set(sys.stdlib_module_names) - {"__future__"})
    assert not outside, f"docs_gate.py imports {outside}, which CI's gate job does not install"


def test_the_check_sees_a_third_party_import():
    """Negative control: the reader must notice a non-stdlib import, or the test is blind."""
    imported = _imported_top_level_modules("import os\nimport yaml\nfrom httpx import Client\n")
    assert imported - set(sys.stdlib_module_names) == {"yaml", "httpx"}


def test_every_generator_the_gate_runs_needs_only_the_standard_library():
    """The gate also runs each doc generator, and they import the package, so the AST of
    docs_gate.py alone cannot see a dependency that arrives through them. `-S` leaves out
    site-packages, as CI's bare interpreter has none; the generators put `src` on the path
    themselves, as they must there."""
    import subprocess

    sys.path.insert(0, str(GATE.parent))
    try:
        import docs_gate
    finally:
        sys.path.remove(str(GATE.parent))
    failed = {}
    for script, _ in docs_gate.generator_targets():
        proc = subprocess.run(
            [sys.executable, "-S", script, "--check"], cwd=GATE.parents[1],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            failed[script] = (proc.stderr or proc.stdout).strip().splitlines()[-1:]
    assert not failed, f"generators that need more than the standard library: {failed}"
