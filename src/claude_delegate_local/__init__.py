import os

# FastMCP and fastmcp-tasks read `FASTMCP_*` from `.env` in the working directory, which for
# a stdio server is the user's project. Point them at no file before anything imports them;
# an operator's own FASTMCP_ENV_FILE still wins.
os.environ.setdefault("FASTMCP_ENV_FILE", os.devnull)
