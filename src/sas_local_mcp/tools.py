"""本地 SAS 数据集 MCP 工具。

通过 register_tools(mcp) 注册全部只读工具；
DatasetError 统一转为 ToolError 以便客户端干净地展示错误。
"""

import functools
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP

from . import dataset
from . import responses as r
from .config import MAX_AGG_ROWS, MAX_ROWS

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
            raise ToolError(f"[{exc.code}] {exc}") from exc
        except OSError as exc:
            raise ToolError(f"[IO_ERROR] 文件访问失败: {exc}") from exc

    return wrapper


def register_tools(mcp: FastMCP) -> None:
    def catalog(path: str | None) -> Path | None:
        return dataset.resolve_dataset_path(path) if path else None

    @_readonly_tool(mcp)
    @_friendly
    def list_datasets(pattern: str | None = None, limit: int = 500, offset: int = 0) -> r.Listing:
        """列出 SAS_DATA_ROOT 下所有 SAS 数据集文件（.sas7bdat / .xpt / .sas7bcat）。

        Args:
            pattern: 可选，文件名子串过滤（不区分大小写），如 "ae" 或 "14.1"。
            limit: 最多返回条数（默认 500）。
            offset: 起始条目；按 next_offset 继续获取后续数据集。
        """
        return r.respond(r.Listing, dataset.list_datasets(pattern, limit, offset))

    @_readonly_tool(mcp)
    @_friendly
    def get_dataset_metadata(path: str, catalog_path: str | None = None, include_row_count: bool = True,
                             column_offset: int = 0, column_limit: int = 500, encoding: str | None = None) -> r.Metadata:
        """获取数据集元数据：行数、每变量 label/type/format、文件标签、创建时间。

        Args:
            path: 数据集路径，相对 SAS_DATA_ROOT 或绝对路径，如 "02Sdtm/dm.sas7bdat"。
            catalog_path: 可选的 .sas7bcat 格式目录，路径也必须位于数据根内。
            include_row_count: False 时不为 XPORT 额外扫描计数，未知行数返回 null。
            column_offset: 变量元数据的分页起点。
            column_limit: 最多返回多少个变量的元数据。
            encoding: 可选，本次调用的字符编码，如 gb18030 / cp1252 / latin1；不传时先按默认解码，
                失败再自动回退，实际生效的回退编码见返回的 encoding_fallback。
        """
        return r.respond(r.Metadata, dataset.read_metadata(dataset.resolve_dataset_path(path), catalog_path=catalog(catalog_path),
                          include_row_count=include_row_count, column_offset=column_offset, column_limit=column_limit,
                          encoding=encoding))

    @_readonly_tool(mcp)
    @_friendly
    def read_dataset(
        path: str,
        limit: int = 20,
        offset: int = 0,
        columns: list[str] | None = None,
        where: str | None = None,
        apply_labels: bool = False,
        catalog_path: str | None = None,
        include_row_count: bool = True,
        missing_policy: Literal["sas", "pandas"] = "sas",
        encoding: str | None = None,
    ) -> r.Rows:
        """按 offset/limit 分页读取记录（records 为 JSON，NaN 为 null，日期为 ISO 字符串）。

        Args:
            path: 数据集路径，相对 SAS_DATA_ROOT 或绝对路径。
            limit: 返回行数（默认 20，上限 SAS_LOCAL_MCP_MAX_ROWS）。
            offset: 起始行号（默认 0）。用于翻到数据中部/尾部取样，
                避免只看前几行（临床数据常是同一受试者）造成误判。
            columns: 可选，只返回这些变量。
            where: 受限比较表达式，如 "AGE > 18 and SEX == 'F'"。先过滤后投影；
                支持 in/not in、算术和布尔组合，不允许函数调用或属性访问。
            apply_labels: 为 True 时把 coded 值替换为 SAS 值标签文本。
            catalog_path: 可选的 .sas7bcat 格式目录。
            include_row_count: False 时允许提前停止过滤扫描，未知计数返回 null。
            missing_policy: sas 将字符空白视为缺失；pandas 仅把 null/NaN 视为字符缺失。
            encoding: 可选，本次调用的字符编码，如 gb18030 / cp1252 / latin1；不传时先按默认解码，
                失败再自动回退，实际生效的回退编码见返回的 encoding_fallback。
        """
        return r.respond(r.Rows, dataset.read_rows(
            dataset.resolve_dataset_path(path),
            limit=limit,
            offset=offset,
            columns=columns,
            where=where,
            apply_labels=apply_labels,
            catalog_path=catalog(catalog_path), include_row_count=include_row_count, missing_policy=missing_policy,
            encoding=encoding,
        ))

    @_readonly_tool(mcp)
    @_friendly
    def summarize_dataset(
        path: str,
        columns: list[str] | None = None,
        apply_labels: bool = False,
        catalog_path: str | None = None,
        missing_policy: Literal["sas", "pandas"] = "sas",
        stats_mode: Literal["full", "counts"] = "full",
        offset: int = 0,
        limit: int = 500,
        encoding: str | None = None,
    ) -> r.Summary:
        """逐变量概要统计——了解一张表的首选入口，省去手写 SQL。

        每个变量给出：类型、非空数 n、缺失数 n_missing、缺失率 pct_missing、
        唯一值数 n_distinct，以及 has_value_labels（有值标签时改用 frequency 看解码分布）。
        再按类型给分布指标：
        数值 -> mean/std/min/p25/median/p75/max；
        日期 -> min/max；
        字符 -> max_length 与 Top5 取值（value/label/n/pct）。

        Args:
            path: 数据集路径，相对 SAS_DATA_ROOT 或绝对路径。
            columns: 可选，只统计这些变量；不传则统计全部。
            apply_labels: 字符型 Top5 是否套用 SAS 值标签。
            catalog_path: 可选的 .sas7bcat 格式目录。
            missing_policy: sas 将字符空白纳入缺失；特殊数值缺失始终纳入缺失。
            stats_mode: counts 仅返回计数、缺失和唯一值数，跳过分布计算。
            offset: 起始变量序号。
            limit: 最多统计并返回多少个变量；按 next_offset 继续。
            encoding: 可选，本次调用的字符编码，如 gb18030 / cp1252 / latin1；不传时先按默认解码，
                失败再自动回退，实际生效的回退编码见返回的 encoding_fallback。
        """
        return r.respond(r.Summary, dataset.summarize_dataset(
            dataset.resolve_dataset_path(path),
            columns=columns,
            apply_labels=apply_labels,
            catalog_path=catalog(catalog_path), missing_policy=missing_policy, stats_mode=stats_mode,
            offset=offset, limit=limit, encoding=encoding,
        ))

    @_readonly_tool(mcp)
    @_friendly
    def frequency(
        path: str,
        column: str,
        limit: int = 50,
        offset: int = 0,
        apply_labels: bool = True,
        include_missing: bool = False,
        catalog_path: str | None = None,
        missing_policy: Literal["sas", "pandas"] = "sas",
        encoding: str | None = None,
    ) -> r.Frequencies:
        """单变量频数表：各取值的 n 与占比，默认自动套用 SAS 值标签。

        结果按 n 降序；原始 value 与解码后的 label 一并返回。
        denominator 回传百分比的分母（include_missing 为 True 时是总行数，否则是非缺失行数）。

        Args:
            path: 数据集路径，相对 SAS_DATA_ROOT 或绝对路径。
            column: 要统计的变量名。
            limit: 返回取值个数（默认 50，上限 SAS_LOCAL_MCP_MAX_AGG_ROWS）。
            offset: 起始序号，用于翻页看长尾取值。
            apply_labels: 默认 True，把 coded 值译为值标签文本（coded 值本身不可读）。
            include_missing: 默认 False；为 True 时把缺失单列为一档并计入分母。
            catalog_path: 可选的 .sas7bcat 格式目录。
            missing_policy: sas 将字符空白视为缺失；missing_details 保留各类缺失计数。
            encoding: 可选，本次调用的字符编码，如 gb18030 / cp1252 / latin1；不传时先按默认解码，
                失败再自动回退，实际生效的回退编码见返回的 encoding_fallback。
        """
        return r.respond(r.Frequencies, dataset.frequency(
            dataset.resolve_dataset_path(path),
            column,
            limit=limit,
            offset=offset,
            apply_labels=apply_labels,
            include_missing=include_missing,
            catalog_path=catalog(catalog_path), missing_policy=missing_policy, encoding=encoding,
        ))

    @_readonly_tool(mcp)
    @_friendly
    def get_value_labels(path: str, catalog_path: str | None = None,
                         columns: list[str] | None = None, encoding: str | None = None) -> r.ValueLabels:
        """获取变量的 SAS 格式值标签映射（QC 时解码 coded 值用）。

        Args:
            path: 数据集路径，相对 SAS_DATA_ROOT 或绝对路径。
            catalog_path: 可选的 .sas7bcat 格式目录。
            columns: 仅获取这些变量的映射，避免大格式目录超出响应上限。
            encoding: 可选，本次调用的字符编码，如 gb18030 / cp1252 / latin1；不传时先按默认解码，
                失败再自动回退，实际生效的回退编码见返回的 encoding_fallback。
        """
        return r.respond(r.ValueLabels, dataset.get_value_labels(dataset.resolve_dataset_path(path),
                          catalog_path=catalog(catalog_path), columns=columns, encoding=encoding))

    @_readonly_tool(mcp)
    @_friendly
    def query_dataset(
        path: str,
        sql: str,
        limit: int = 100,
        offset: int = 0,
        extra_paths: list[str] | None = None,
        apply_labels: bool = False,
        include_total: bool = True,
        label_sources: dict[str, r.LabelSource] | None = None,
        catalog_paths: dict[str, str] | None = None,
        missing_policy: Literal["sas", "pandas"] = "sas",
        encoding: str | None = None,
    ) -> r.Query:
        """对数据集执行 duckdb SQL（仅 SELECT / WITH）。

        主数据集表名固定为 dataset。传入 extra_paths 可再注册附加表，表名取文件名主干
        （如 lb.sas7bdat -> lb），冲突时回退 dataset2/dataset3；返回的 tables 给出
        表名->路径 映射。用于跨数据集对比，例如：
        sql="SELECT COUNT(*) FROM dataset a FULL JOIN lb b USING(USUBJID)"

        Args:
            path: 主数据集路径，相对 SAS_DATA_ROOT 或绝对路径。
            sql: SQL 语句，如 "SELECT COUNT(*) AS n FROM dataset"。
            limit: 返回行数（默认 100，上限 SAS_LOCAL_MCP_MAX_AGG_ROWS，默认 1000）。
            offset: 起始行号，用于翻页。
            extra_paths: 可选，附加数据集路径列表，注册为额外表参与 JOIN。
            apply_labels: 仅自动解码来源明确的原始列；表达式与复杂查询保持原值。
            include_total: False 时跳过精确结果计数，total_rows 返回 null。
            label_sources: 显式指定结果列的标签来源，如 {"treatment": {"table": "dataset", "column": "ARM"}}。
            catalog_paths: 注册表名到格式目录路径的映射，如 {"dataset": "formats.sas7bcat"}。
            missing_policy: sas 将字符空白及 SAS 特殊缺失转换为 SQL NULL；pandas 保留字符空白。
            encoding: 可选，本次调用的字符编码，如 gb18030 / cp1252 / latin1；不传时先按默认解码，
                失败再自动回退，实际生效的回退编码见返回的 encoding_fallback。
        """
        return r.respond(r.Query, dataset.query_dataset(
            dataset.resolve_dataset_path(path),
            sql,
            limit,
            offset,
            [dataset.resolve_dataset_path(p) for p in (extra_paths or [])],
            apply_labels,
            include_total=include_total,
            label_sources={name: source.model_dump() for name, source in (label_sources or {}).items()},
            catalog_paths={name: catalog(value) for name, value in (catalog_paths or {}).items()},
            missing_policy=missing_policy,
            encoding=encoding,
        ))

    @_readonly_tool(mcp)
    @_friendly
    def search_variables(
        variable: str,
        pattern: str | None = None,
        contains: bool = False,
        limit: int = 50,
        max_datasets: int = 200,
        offset: int = 0,
        scan_offset: int = 0,
        encoding: str | None = None,
    ) -> r.Search:
        """在 SAS_DATA_ROOT 下搜索含指定变量的数据集。

        回答"哪个数据集里有 USUBJID / 哪些表有 AEDECOD"，避免逐个 get_dataset_metadata
        试错。只读取元数据，不加载数据行；损坏文件记入 errors 而不中断搜索。

        Args:
            variable: 要查找的变量名（默认精确匹配，不区分大小写）。
            pattern: 可选，先按文件名子串过滤数据集，缩小扫描范围。
            contains: 为 True 时改为子串匹配（如 "AGE" 命中 AGE / AGEU）。
            limit: 返回匹配条数（默认 50）。
            max_datasets: 最多扫描多少个数据集（默认 200）。
            offset: 当前扫描窗口中匹配结果的分页起点，按 next_offset 继续。
            scan_offset: 扫描窗口起点；取完当前窗口后，用 next_scan_offset 开始下一窗口，并重置 offset。
            encoding: 可选，本次调用的字符编码，如 gb18030 / cp1252 / latin1；不传时先按默认解码，
                失败再自动回退，实际生效的回退编码见返回的 encoding_fallback。
        """
        return r.respond(r.Search, dataset.search_variables(
            variable,
            pattern=pattern,
            contains=contains,
            limit=limit,
            max_datasets=max_datasets,
            offset=offset, scan_offset=scan_offset, encoding=encoding,
        ))


__all__ = ["MAX_AGG_ROWS", "MAX_ROWS", "register_tools"]
