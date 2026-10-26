"""A `.env` in the user's project configured the server's FastMCP.

FastMCP reads its `FASTMCP_*` settings from `.env` in the working directory, unless
`FASTMCP_ENV_FILE` names another file, and so does `fastmcp-tasks` for its backend. Claude
Code starts a stdio server in the project it is working on, so the working directory is the
user's project, and a file in it could set the server's log level or point its task backend
at a Redis of the project's choosing. The project is what a delegation works *on*, never
something that configures the server, so the package points both readers at no file before
FastMCP is imported. An operator who sets `FASTMCP_ENV_FILE` themselves still gets theirs.
"""

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

PROBE = """
import claude_delegate_local.server  # what the server process imports first
import fastmcp
print(fastmcp.settings.log_level)
"""


def _settings_seen_from(project: Path, **env: str) -> str:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("FASTMCP_")}
    environ.update(env, PYTHONPATH=str(SRC))
    done = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=project, env=environ, capture_output=True, text=True, timeout=60, check=True,
    )
    return done.stdout.strip()


def test_a_projects_env_does_not_reach_fastmcp(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("FASTMCP_LOG_LEVEL=CRITICAL\n", encoding="utf-8")
    assert _settings_seen_from(tmp_path) != "CRITICAL"


def test_an_operators_own_env_file_is_still_read(tmp_path: Path) -> None:
    mine = tmp_path / "operator.env"
    mine.write_text("FASTMCP_LOG_LEVEL=CRITICAL\n", encoding="utf-8")
    assert _settings_seen_from(tmp_path, FASTMCP_ENV_FILE=str(mine)) == "CRITICAL"
