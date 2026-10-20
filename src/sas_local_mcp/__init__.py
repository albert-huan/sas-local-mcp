"""sas-local-mcp: MCP server for reading local SAS datasets.

Reads local .sas7bdat / .xpt files via pyreadstat (pandas fallback) and exposes
them to MCP clients as read-only tools.
"""

__version__ = "0.1.0"
