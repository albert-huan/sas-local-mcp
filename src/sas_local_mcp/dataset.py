"""本地 SAS 数据集读取核心。

改编自 sassoftware/sas-mcp-server（Apache-2.0）：原版通过 SAS Viya REST API
访问 CAS 数据，本模块把数据源换成本地文件——用 pyreadstat 直接读取
.sas7bdat / .xpt，无需 SAS 许可；pyreadstat 不可用时回退 pandas.read_sas。
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd

try:  # pragma: no cover - import guard
    import pyreadstat
except ImportError:  # pragma: no cover
    pyreadstat = None

from .config import MAX_ROWS, SAS_DATA_ROOT

READABLE_SUFFIXES: dict[str, str] = {".sas7bdat": "sas7bdat", ".xpt": "xport"}
# .sas7bcat 是格式目录文件：可被 list_datasets 发现，但没有读取器。
KNOWN_SUFFIXES: dict[str, str] = {**READABLE_SUFFIXES, ".sas7bcat": "sas7bcat"}

# SAS 日期/时间信息格式关键字（出现在 informat 中即视为日期时间列）。
_DATE_INFORMAT_MARKERS = (
    "DATE", "TIME", "YYMM", "MMDD", "DDMM", "MMYY", "JULIAN", "MONYY", "HHMM", "TODAY", "NOW",
)


class DatasetError(Exception):
    """面向用户的数据集定位/读取失败。"""


def resolve_dataset_path(path: str) -> Path:
    """把用户传入路径解析为 SAS_DATA_ROOT 内的绝对路径，拒绝 .. 越界访问。

    包含检查按规范化后的词法路径判断，不解析联接/junction——否则 renv 库
    这类指向盘上其他位置的目录联接会被误判为越界。
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = SAS_DATA_ROOT / candidate
    normalized = Path(os.path.normpath(candidate))
    if not normalized.is_relative_to(SAS_DATA_ROOT):
        raise DatasetError(f"路径越界: {path!r} 不在 SAS_DATA_ROOT({SAS_DATA_ROOT}) 内")
    if not normalized.is_file():
        raise DatasetError(f"文件不存在: {path!r} (相对于 {SAS_DATA_ROOT})")
    return normalized


def dataset_format(path: Path) -> str:
    return KNOWN_SUFFIXES.get(path.suffix.lower(), "unknown")


def _rel(path: Path) -> str:
    try:
        return Path(os.path.normpath(path)).relative_to(SAS_DATA_ROOT).as_posix()
    except ValueError:
        return str(path)


def _read(path: Path, metadata_only: bool) -> tuple[pd.DataFrame, Any]:
    """读取数据集，返回 (DataFrame, pyreadstat meta 或 None)。"""
    fmt = dataset_format(path)
    if fmt not in READABLE_SUFFIXES.values():
        raise DatasetError(
            f"不支持的格式 {path.suffix!r}（可读取: .sas7bdat / .xpt；.sas7bcat 仅可列出）"
        )
    if pyreadstat is not None:
        reader = pyreadstat.read_sas7bdat if fmt == "sas7bdat" else pyreadstat.read_xport
        try:
            try:
                return reader(str(path), metadataonly=metadata_only)
            except TypeError:  # 旧版 pyreadstat 不支持 metadataonly 参数
                return reader(str(path))
        except Exception as exc:
            raise DatasetError(
                f"读取 {path.name} 失败: {exc}（文件可能损坏、截断或版本不受支持）"
            ) from exc
    # pandas 回退：只支持全量读取，无 meta 信息。
    try:
        return pd.read_sas(str(path), format=fmt), None
    except Exception as exc:
        raise DatasetError(f"pandas 读取 {path.name} 失败: {exc}") from exc


def _meta_item(meta: Any, attr: str, idx: int) -> str:
    values = getattr(meta, attr, None) if meta is not None else None
    if values is not None and idx < len(values) and values[idx] is not None:
        return str(values[idx])
    return ""


def _meta_scalar(meta: Any, attr: str, default: Any) -> Any:
    value = getattr(meta, attr, None) if meta is not None else None
    return default if value is None else value


def _column_type(meta: Any, name: str, dtype: Any) -> str:
    """列类型判定：优先 SAS 原始类型（readstat_variable_types），日期看 informat。"""
    readstat_types = getattr(meta, "readstat_variable_types", None) if meta is not None else None
    informats = getattr(meta, "original_variable_informats", None) if meta is not None else None
    kind = (readstat_types or {}).get(name)
    if kind is None:  # pandas 回退（无 meta）：对象/字符串 dtype 视为字符
        return "str" if str(dtype) in {"object", "str", "string"} else "numeric"
    if kind != "string":
        informat = str((informats or {}).get(name) or "").upper()
        if any(marker in informat for marker in _DATE_INFORMAT_MARKERS):
            return "datetime"
        return "numeric"
    return "str"


def _jsonable(value: Any) -> Any:
    """任意标量 -> JSON 安全值（NaN/NaT -> null，日期 -> ISO 字符串）。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (str, bool, int, float)):
        return value
    item = getattr(value, "item", None)  # numpy 标量 -> python 标量
    if callable(item):
        try:
            return _jsonable(item())
        except (ValueError, AttributeError):
            pass
    return str(value)


def _to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    return [
        {str(name): _jsonable(value) for name, value in row.items()}
        for row in df.to_dict(orient="records")
    ]


def list_datasets(pattern: str | None = None, limit: int = 500) -> dict[str, Any]:
    """递归列出 SAS_DATA_ROOT 下所有 SAS 数据集文件。"""
    skip_dirs = {
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        ".roo", ".codebuddy", ".rproj.user", "renv",
    }
    found: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(SAS_DATA_ROOT):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip_dirs and not d.startswith(".")]
        for name in filenames:
            suffix = Path(name).suffix.lower()
            if suffix not in KNOWN_SUFFIXES:
                continue
            if pattern and pattern.lower() not in name.lower():
                continue
            file_path = Path(dirpath) / name
            stat = file_path.stat()
            found.append(
                {
                    "path": _rel(file_path),
                    "name": name,
                    "format": KNOWN_SUFFIXES[suffix],
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                }
            )
    found.sort(key=lambda item: item["path"])
    effective = max(1, min(int(limit), 5000))
    return {
        "root": str(SAS_DATA_ROOT),
        "count": len(found),
        "datasets": found[:effective],
    }


def read_metadata(path: Path) -> dict[str, Any]:
    """行/列数、每变量 label/type/informat、文件标签与创建时间（不加载数据）。"""
    df, meta = _read(path, metadata_only=True)
    informats = (getattr(meta, "original_variable_informats", None) or {}) if meta is not None else {}
    user_missing = (getattr(meta, "missing_user_values", None) or {}) if meta is not None else {}
    columns = [
        {
            "name": str(name),
            "label": _meta_item(meta, "column_labels", idx),
            "type": _column_type(meta, str(name), df.dtypes.iloc[idx]),
            "informat": "" if informats.get(str(name)) is None else str(informats.get(str(name))),
        }
        for idx, name in enumerate(df.columns)
    ]
    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "table_name": _meta_scalar(meta, "table_name", None),
        "number_rows": _meta_scalar(meta, "number_rows", len(df)),
        "number_columns": len(df.columns),
        "file_label": _meta_scalar(meta, "file_label", ""),
        "file_encoding": _meta_scalar(meta, "file_encoding", None),
        "created": _meta_scalar(meta, "creation_time", None),
        "modified": _meta_scalar(meta, "modification_time", None),
        "user_missing": {str(k): v for k, v in user_missing.items()},
        "columns": columns,
    }


def read_rows(
    path: Path,
    *,
    limit: int = 20,
    columns: list[str] | None = None,
    where: str | None = None,
    apply_labels: bool = False,
) -> dict[str, Any]:
    """读取前 limit 行，支持列子集、pandas where 过滤、套用值标签。"""
    df, meta = _read(path, metadata_only=False)
    total_rows = int(len(df))
    if columns:
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise DatasetError(f"未知变量: {missing}；可用变量见 get_dataset_metadata")
        df = df[list(columns)]
    rows_after_filter = total_rows
    if where:
        try:
            df = df.query(where)
        except Exception as exc:
            raise DatasetError(f"where 条件无法应用: {exc}") from exc
        rows_after_filter = int(len(df))
    value_labels = getattr(meta, "variable_value_labels", None) or {}
    if apply_labels:
        for var, mapping in value_labels.items():
            if var in df.columns:
                df[var] = df[var].map(lambda value, m=mapping: m.get(value, value))
    effective = max(1, min(int(limit), MAX_ROWS))
    head = df.head(effective)
    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "total_rows": total_rows,
        "rows_after_filter": rows_after_filter,
        "columns": [str(c) for c in head.columns],
        "records": _to_records(head),
        "truncated": rows_after_filter > len(head),
        "value_labels_applied": bool(apply_labels and value_labels),
    }


def _label_key(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def get_value_labels(path: Path) -> dict[str, Any]:
    """变量的 SAS 格式值标签映射（QC 解码 coded 值用）。"""
    _df, meta = _read(path, metadata_only=True)
    raw = getattr(meta, "variable_value_labels", None) or {}
    variables = {
        str(var): {_label_key(k): str(v) for k, v in mapping.items()}
        for var, mapping in raw.items()
    }
    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "variables": variables,
    }


def query_dataset(path: Path, sql: str, limit: int = 100) -> dict[str, Any]:
    """对数据集执行 duckdb SQL（数据以内存表 dataset 注册，只允许 SELECT/WITH）。"""
    statement = sql.strip()
    if not statement[:6].lower().startswith(("select", "with")):
        raise DatasetError("只允许 SELECT / WITH 查询语句（只读服务器）")
    try:
        import duckdb
    except ImportError as exc:
        raise DatasetError("duckdb 未安装，请执行: pip install duckdb") from exc
    df, _ = _read(path, metadata_only=False)
    con = duckdb.connect()
    try:
        con.register("dataset", df)
        result = con.execute(sql).fetch_df()
    except Exception as exc:
        raise DatasetError(f"SQL 执行失败: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            con.unregister("dataset")
        con.close()
    effective = max(1, min(int(limit), MAX_ROWS))
    head = result.head(effective)
    return {
        "path": _rel(path),
        "sql": sql,
        "total_rows": int(len(result)),
        "columns": [str(c) for c in head.columns],
        "records": _to_records(head),
        "truncated": len(result) > len(head),
    }
