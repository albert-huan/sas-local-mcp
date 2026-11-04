#!/usr/bin/env python3
"""Stdio entry point for the local SAS dataset MCP server."""

import os
import sys

from . import __version__
from .config import SAS_DATA_ROOT, SERVER_NAME
from .tools import register_tools

from fastmcp import FastMCP

mcp = FastMCP(SERVER_NAME)
register_tools(mcp)


def main() -> None:
    print(f"[sas-local-mcp] v{__version__} SAS_DATA_ROOT={SAS_DATA_ROOT}", file=sys.stderr, flush=True)
    if not os.getenv("SAS_DATA_ROOT"):
        print("[sas-local-mcp] 警告: 未设置 SAS_DATA_ROOT，已把当前工作目录整棵树作为可读范围；"
              "请在 MCP 客户端配置中显式设置数据根目录", file=sys.stderr, flush=True)
    # No banner: FastMCP prints it to stderr, and a driver that doesn't drain
    # stderr deadlocks on the filled pipe before the first MCP message.
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
