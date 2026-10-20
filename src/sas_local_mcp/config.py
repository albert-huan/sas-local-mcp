"""Environment-driven configuration for the local SAS dataset MCP server.

Values are read once at import time; the stdio server is launched with the
environment already set by the MCP client.
"""

import os
from pathlib import Path

SERVER_NAME = os.getenv("SAS_LOCAL_MCP_NAME", "sas-local-mcp")


def _data_root() -> Path:
    """Directory every dataset path must live under (defaults to the CWD)."""
    raw = os.getenv("SAS_DATA_ROOT") or os.getcwd()
    return Path(raw).expanduser().resolve()


SAS_DATA_ROOT: Path = _data_root()


def _max_rows() -> int:
    try:
        return max(1, int(os.getenv("SAS_LOCAL_MCP_MAX_ROWS", "200")))
    except ValueError:
        return 200


MAX_ROWS: int = _max_rows()
