"""本地 SAS 数据集 MCP 工具。

沿用 sassoftware/sas-mcp-server 的 register(mcp, ...) 注册模式，
工具全部只读；DatasetError 统一转为 ToolError 以便客户端干净地展示错误。
"""

import functools
from typing import Any

from fastmcp import FastMCP

from . import dataset
from .config import MAX_ROWS

try:
    from fastmcp.exceptions import ToolError
except ImportError:  # pragma: no cover - 兼容没有 ToolError 的 fastmcp 版本
    class ToolError(Exception):  # type: ignore[no-redef]
        """fastmcp 未提供 ToolError 时的退而求其次。"""

try:
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover
    ToolAnnotations = None

_READ_ONLY = (
    ToolAnnotations(readOnlyHint=True, openWorldHint=False, destructiveHint=False)
    if ToolAnnotations is not None
    else None
)


def _readonly_tool(mcp: FastMCP):
    return mcp.tool(annotations=_READ_ONLY) if _READ_ONLY is not None else mcp.tool()


def _friendly(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except dataset.DatasetError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def register_tools(mcp: FastMCP) -> None:
    @_readonly_tool(mcp)
    @_friendly
    def list_datasets(pattern: str | None = None, limit: int = 500) -> dict[str, Any]:
        """列出 SAS_DATA_ROOT 下所有 SAS 数据集文件（.sas7bdat / .xpt / .sas7bcat）。

        Args:
            pattern: 可选，文件名子串过滤（不区分大小写），如 "ae" 或 "14.1"。
            limit: 最多返回条数（默认 500）。
        """
        return dataset.list_datasets(pattern, limit)

    @_readonly_tool(mcp)
    @_friendly
    def get_dataset_metadata(path: str) -> dict[str, Any]:
        """获取数据集元数据：行数、每变量 label/type/format、文件标签、创建时间。

        Args:
            path: 数据集路径，相对 SAS_DATA_ROOT 或绝对路径，如 "02Sdtm/dm.sas7bdat"。
        """
        return dataset.read_metadata(dataset.resolve_dataset_path(path))

    @_readonly_tool(mcp)
    @_friendly
    def read_dataset(
        path: str,
        limit: int = 20,
        columns: list[str] | None = None,
        where: str | None = None,
        apply_labels: bool = False,
    ) -> dict[str, Any]:
        """读取数据集前 N 行（records 为 JSON，NaN 为 null，日期为 ISO 字符串）。

        Args:
            path: 数据集路径，相对 SAS_DATA_ROOT 或绝对路径。
            limit: 返回行数（默认 20，上限 SAS_LOCAL_MCP_MAX_ROWS）。
            columns: 可选，只返回这些变量。
            where: 可选，pandas 查询表达式，如 "AGE > 18" 或 "SEX == 'F'"。
            apply_labels: 为 True 时把 coded 值替换为 SAS 值标签文本。
        """
        return dataset.read_rows(
            dataset.resolve_dataset_path(path),
            limit=limit,
            columns=columns,
            where=where,
            apply_labels=apply_labels,
        )

    @_readonly_tool(mcp)
    @_friendly
    def get_value_labels(path: str) -> dict[str, Any]:
        """获取变量的 SAS 格式值标签映射（QC 时解码 coded 值用）。

        Args:
            path: 数据集路径，相对 SAS_DATA_ROOT 或绝对路径。
        """
        return dataset.get_value_labels(dataset.resolve_dataset_path(path))

    @_readonly_tool(mcp)
    @_friendly
    def query_dataset(path: str, sql: str, limit: int = 100) -> dict[str, Any]:
        """对数据集执行 duckdb SQL，数据以内存表 dataset 注册（仅 SELECT / WITH）。

        Args:
            path: 数据集路径，相对 SAS_DATA_ROOT 或绝对路径。
            sql: SQL 语句，表名为 dataset，如 "SELECT COUNT(*) AS n FROM dataset"。
            limit: 返回行数（默认 100，上限 SAS_LOCAL_MCP_MAX_ROWS）。
        """
        return dataset.query_dataset(dataset.resolve_dataset_path(path), sql, limit)


__all__ = ["MAX_ROWS", "register_tools"]
