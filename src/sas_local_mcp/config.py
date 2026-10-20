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


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _max_rows() -> int:
    """行预览上限：read_dataset 一次最多返回多少条原始记录。"""
    return _int_env("SAS_LOCAL_MCP_MAX_ROWS", 200)


def _max_agg_rows() -> int:
    """聚合结果上限：频数表 / SQL 聚合的行数上限。

    与 MAX_ROWS 分开，因为 GROUP BY 结果的 cardinality 可能远超预览行数，
    被 200 行截断会让模型读到不完整的分布。
    """
    return _int_env("SAS_LOCAL_MCP_MAX_AGG_ROWS", 1000)


MAX_ROWS: int = _max_rows()
MAX_AGG_ROWS: int = _max_agg_rows()


def _encoding() -> str | None:
    """数据集字符编码；中文数据常需 utf-8 / gbk。None 表示交给 pyreadstat 默认。"""
    raw = (os.getenv("SAS_LOCAL_MCP_ENCODING") or "").strip()
    return raw or None


ENCODING: str | None = _encoding()


def _cache_size() -> int:
    """缓存的数据集个数（DataFrame + meta）。0 表示关闭缓存。

    大文件解析一次要数秒，多轮问答下缓存能显著提速；按 (路径, mtime, size) 失效。
    """
    try:
        return max(0, int(os.getenv("SAS_LOCAL_MCP_CACHE_SIZE", "8")))
    except ValueError:
        return 8


CACHE_SIZE: int = _cache_size()
