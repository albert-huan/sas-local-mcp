"""本地 SAS 数据集读取核心。

用 pyreadstat 直接读取本地 .sas7bdat / .xpt 文件，无需 SAS 许可；
pyreadstat 不可用时回退 pandas.read_sas。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections import OrderedDict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd

try:  # pragma: no cover - import guard
    import pyreadstat
except ImportError:  # pragma: no cover
    pyreadstat = None

from .config import CACHE_SIZE, ENCODING, MAX_AGG_ROWS, MAX_ROWS, SAS_DATA_ROOT

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


def _call_reader(reader: Any, path: Path, metadata_only: bool) -> tuple[pd.DataFrame, Any]:
    """调用 pyreadstat 读取器；逐级回退不支持的 metadataonly / encoding 参数。"""
    kwargs: dict[str, Any] = {}
    if ENCODING:
        kwargs["encoding"] = ENCODING
    try:
        return reader(str(path), metadataonly=metadata_only, **kwargs)
    except TypeError:  # 旧版 pyreadstat 不接受 metadataonly 或 encoding
        try:
            return reader(str(path), **kwargs)
        except TypeError:
            return reader(str(path))


def _read_uncached(path: Path, metadata_only: bool) -> tuple[pd.DataFrame, Any]:
    """读取数据集，返回 (DataFrame, pyreadstat meta 或 None)。"""
    fmt = dataset_format(path)
    if fmt not in READABLE_SUFFIXES.values():
        raise DatasetError(
            f"不支持的格式 {path.suffix!r}（可读取: .sas7bdat / .xpt；.sas7bcat 仅可列出）"
        )
    if pyreadstat is not None:
        reader = pyreadstat.read_sas7bdat if fmt == "sas7bdat" else pyreadstat.read_xport
        try:
            return _call_reader(reader, path, metadata_only)
        except Exception as exc:
            raise DatasetError(
                f"读取 {path.name} 失败: {exc}（文件可能损坏、截断或版本不受支持）"
            ) from exc
    # pandas 回退：只支持全量读取，无 meta 信息。
    try:
        kwargs = {"encoding": ENCODING} if ENCODING else {}
        return pd.read_sas(str(path), format=fmt, **kwargs), None
    except Exception as exc:
        raise DatasetError(f"pandas 读取 {path.name} 失败: {exc}") from exc


# 缓存键含 metadata_only：metadataonly 读出的 df 没有数据行，不能与全量结果混用。
_CACHE: OrderedDict[tuple, tuple[pd.DataFrame, Any]] = OrderedDict()


def _cache_key(path: Path, metadata_only: bool) -> tuple:
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size, metadata_only)


def _read(path: Path, metadata_only: bool) -> tuple[pd.DataFrame, Any]:
    """带 LRU 缓存的读取入口；按 (路径, mtime, size, metadata_only) 失效。"""
    if not CACHE_SIZE:
        return _read_uncached(path, metadata_only)
    key = _cache_key(path, metadata_only)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    result = _read_uncached(path, metadata_only)
    _CACHE[key] = result
    while len(_CACHE) > CACHE_SIZE:
        _CACHE.popitem(last=False)
    return result


def _meta_item(meta: Any, attr: str, idx: int) -> str:
    values = getattr(meta, attr, None) if meta is not None else None
    if values is not None and idx < len(values) and values[idx] is not None:
        return str(values[idx])
    return ""


def _meta_scalar(meta: Any, attr: str, default: Any) -> Any:
    value = getattr(meta, attr, None) if meta is not None else None
    return default if value is None else value


def _row_count(df: pd.DataFrame, meta: Any) -> int | None:
    """数据集行数；XPORT 的 metadataonly 读取拿不到行数时返回 None（未知，不谎报 0）。"""
    rows = _meta_scalar(meta, "number_rows", None)
    if rows is not None:
        return int(rows)
    # meta 为 None 说明走的是 pandas 回退，df 本身已是全量
    return int(len(df)) if meta is None else None


def _column_type(meta: Any, name: str, dtype: Any) -> str:
    """列类型判定：优先 SAS 原始类型（readstat_variable_types），日期看 informat/format。

    日期同时看 format 与 informat——SAS 日期列常只带 format（如 DATE9.）而无 informat。
    """
    if meta is None:
        return "str" if str(dtype) in {"object", "str", "string"} else "numeric"
    readstat_types = getattr(meta, "readstat_variable_types", None) or {}
    informats = getattr(meta, "original_variable_informats", None) or {}
    formats = getattr(meta, "original_variable_formats", None) or {}
    kind = readstat_types.get(name)
    if kind is None:  # pandas 回退（无 meta）：对象/字符串 dtype 视为字符
        return "str" if str(dtype) in {"object", "str", "string"} else "numeric"
    if kind != "string":
        text = f"{informats.get(name) or ''} {formats.get(name) or ''}".upper()
        if any(marker in text for marker in _DATE_INFORMAT_MARKERS):
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


_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".roo", ".codebuddy", ".rproj.user", "renv",
}


def _iter_dataset_files(pattern: str | None = None, readable_only: bool = False):
    """递归产出 SAS_DATA_ROOT 下的数据集文件；readable_only 会排除 .sas7bcat。"""
    for dirpath, dirnames, filenames in os.walk(SAS_DATA_ROOT):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            suffix = Path(name).suffix.lower()
            if suffix not in KNOWN_SUFFIXES:
                continue
            if readable_only and suffix not in READABLE_SUFFIXES:
                continue
            if pattern and pattern.lower() not in name.lower():
                continue
            yield Path(dirpath) / name


def list_datasets(pattern: str | None = None, limit: int = 500) -> dict[str, Any]:
    """递归列出 SAS_DATA_ROOT 下所有 SAS 数据集文件。"""
    found: list[dict[str, Any]] = []
    for file_path in _iter_dataset_files(pattern):
        suffix = file_path.suffix.lower()
        stat = file_path.stat()
        found.append(
            {
                "path": _rel(file_path),
                "name": file_path.name,
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
    """行/列数、每变量 label/type/informat、文件标签与创建时间（不加载数据）。

    XPORT 例外：pyreadstat 对 .xpt 的 metadataonly 读取只返回列信息，
    meta.number_rows 为 None 且 df 为空——直接采信会把非空数据集报成 0 行。
    这种情况下退回全量读取数一次行数；结果同样进缓存，后续工具不会重复解析。
    """
    df, meta = _read(path, metadata_only=True)
    number_rows = _row_count(df, meta)
    if number_rows is None:
        # 只有 XPORT 会走到这里：退回全量读取数一次行数，结果进缓存
        number_rows = int(len(_read(path, metadata_only=False)[0]))
    informats = (getattr(meta, "original_variable_informats", None) or {}) if meta is not None else {}
    formats = (getattr(meta, "original_variable_formats", None) or {}) if meta is not None else {}
    user_missing = (getattr(meta, "missing_user_values", None) or {}) if meta is not None else {}
    columns = [
        {
            "name": str(name),
            "label": _meta_item(meta, "column_labels", idx),
            "type": _column_type(meta, str(name), df.dtypes.iloc[idx]),
            "format": "" if formats.get(str(name)) is None else str(formats.get(str(name))),
            "informat": "" if informats.get(str(name)) is None else str(informats.get(str(name))),
        }
        for idx, name in enumerate(df.columns)
    ]
    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "table_name": _meta_scalar(meta, "table_name", None),
        "number_rows": number_rows,
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
    offset: int = 0,
    columns: list[str] | None = None,
    where: str | None = None,
    apply_labels: bool = False,
) -> dict[str, Any]:
    """按 offset/limit 分页读取，支持列子集、pandas where 过滤、套用值标签。

    offset 让模型能翻到数据中部/尾部取样，避免只看 head() 造成误判。
    """
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
        # _read may return an LRU-cached frame; label application must not alter it.
        df = df.copy()
        for var, mapping in value_labels.items():
            if var in df.columns:
                df[var] = df[var].map(lambda value, m=mapping: m.get(value, value))
    effective = max(1, min(int(limit), MAX_ROWS))
    start = max(0, int(offset))
    page = df.iloc[start : start + effective]
    returned = len(page)
    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "total_rows": total_rows,
        "rows_after_filter": rows_after_filter,
        "offset": start,
        "columns": [str(c) for c in page.columns],
        "records": _to_records(page),
        "returned": returned,
        "truncated": rows_after_filter > start + returned,
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


def _sql_identifier(stem: str) -> str:
    """把文件名主干转成合法的 duckdb 表名。"""
    ident = re.sub(r"[^0-9A-Za-z_]", "_", stem).strip("_")
    if not ident:
        ident = "t"
    if ident[0].isdigit():
        ident = f"t_{ident}"
    return ident.lower()


def _merged_value_labels(metas: list[Any]) -> dict[str, dict[str, str]]:
    """合并多个数据集的值标签，靠前的数据集优先。"""
    merged: dict[str, dict[str, str]] = {}
    for meta in metas:
        for var, mapping in (getattr(meta, "variable_value_labels", None) or {}).items():
            merged.setdefault(str(var), _norm_labels(mapping))
    return merged


def query_dataset(
    path: Path,
    sql: str,
    limit: int = 100,
    offset: int = 0,
    extra_paths: list[Path] | None = None,
    apply_labels: bool = False,
) -> dict[str, Any]:
    """对数据集执行 duckdb SQL（只允许 SELECT/WITH）。

    主数据集注册为 `dataset`；extra_paths 里的附加数据集按文件名主干注册为额外表
    （如 `lb`），冲突时回退 `dataset2`/`dataset3`。返回的 `tables` 给出 表名 -> 路径
    映射，便于跨数据集对比（QC 常见：比对两个产出里同一变量的取值）。

    结果上限取 MAX_AGG_ROWS 而非 MAX_ROWS：GROUP BY 的基数常远超预览行数，
    按预览上限截断会让模型读到不完整的分布。
    """
    try:
        import duckdb
    except ImportError as exc:
        raise DatasetError("duckdb 未安装，请执行: pip install duckdb") from exc

    try:
        statements = duckdb.extract_statements(sql)
    except Exception as exc:
        raise DatasetError(f"SQL 解析失败: {exc}") from exc
    if len(statements) != 1 or statements[0].type.name != "SELECT":
        raise DatasetError("只允许单条 SELECT 查询语句（只读服务器）")

    metas: list[Any] = []
    # (表名, DataFrame, 原始路径)
    frames: list[tuple[str, pd.DataFrame, Path]] = []
    main_df, main_meta = _read(path, metadata_only=False)
    metas.append(main_meta)
    frames.append(("dataset", main_df, path))

    used = {"dataset"}
    for extra in extra_paths or []:
        extra_df, extra_meta = _read(extra, metadata_only=False)
        metas.append(extra_meta)
        name = _sql_identifier(extra.stem)
        if name in used:
            name = f"dataset{len(used) + 1}"
        while name in used:
            name = f"{name}_"
        used.add(name)
        frames.append((name, extra_df, extra))

    # SQL functions such as read_csv can otherwise access files outside the data root.
    con = duckdb.connect(config={"enable_external_access": "false"})
    try:
        for name, frame, _ in frames:
            con.register(name, frame)
        result = con.execute(sql).fetch_df()
    except Exception as exc:
        raise DatasetError(f"SQL 执行失败: {exc}") from exc
    finally:
        for name, _, _ in frames:
            with contextlib.suppress(Exception):
                con.unregister(name)
        con.close()

    labels = _merged_value_labels(metas) if apply_labels else {}
    if labels:
        for col in result.columns:
            mapping = labels.get(str(col))
            if mapping:
                result[col] = result[col].map(lambda v, m=mapping: m.get(_label_key(v), v))

    effective = max(1, min(int(limit), MAX_AGG_ROWS))
    start = max(0, int(offset))
    page = result.iloc[start : start + effective]
    returned = len(page)
    return {
        "path": _rel(path),
        "sql": sql,
        "tables": {name: _rel(frame_path) for name, _, frame_path in frames},
        "total_rows": int(len(result)),
        "offset": start,
        "columns": [str(c) for c in page.columns],
        "records": _to_records(page),
        "returned": returned,
        "truncated": len(result) > start + returned,
        "value_labels_applied": bool(labels),
    }


def search_variables(
    variable: str,
    pattern: str | None = None,
    contains: bool = False,
    limit: int = 50,
    max_datasets: int = 200,
) -> dict[str, Any]:
    """在 SAS_DATA_ROOT 下搜索含指定变量的数据集。

    回答"哪个数据集里有 USUBJID / 哪些表有 AEDECOD"这类问题，避免逐个
    get_dataset_metadata 试错。只读取元数据（metadataonly），不加载数据行。
    损坏文件会被记入 errors 而不是中断整个搜索。
    """
    needle = variable.strip()
    if not needle:
        raise DatasetError("variable 不能为空")
    target = needle.lower()
    matches: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    scanned = 0
    scan_limit = max(1, int(max_datasets))
    files = iter(_iter_dataset_files(pattern, readable_only=True))

    while scanned < scan_limit:
        file_path = next(files, None)
        if file_path is None:
            break
        scanned += 1
        try:
            df, meta = _read(file_path, metadata_only=True)
        except DatasetError as exc:
            errors.append({"path": _rel(file_path), "error": str(exc)})
            continue
        names = [str(c) for c in df.columns]
        if contains:
            hits = [(i, n) for i, n in enumerate(names) if target in n.lower()]
        else:
            hits = [(i, n) for i, n in enumerate(names) if n.lower() == target]
        if not hits:
            continue
        labels = _label_lookup(meta, df)
        for idx, name in hits:
            matches.append(
                {
                    "path": _rel(file_path),
                    "dataset": file_path.name,
                    "variable": name,
                    "label": labels.get(name, ""),
                    "type": _column_type(meta, name, df.dtypes.iloc[idx]),
                    "number_rows": _row_count(df, meta),
                }
            )

    matches.sort(key=lambda item: (item["path"], item["variable"]))
    effective = max(1, min(int(limit), MAX_AGG_ROWS))
    scan_truncated = scanned >= scan_limit and next(files, None) is not None
    return {
        "root": str(SAS_DATA_ROOT),
        "variable": needle,
        "contains": bool(contains),
        "datasets_scanned": scanned,
        "datasets_matched": len({m["path"] for m in matches}),
        "matches": matches[:effective],
        "returned": len(matches[:effective]),
        "scan_truncated": scan_truncated,
        "truncated": len(matches) > effective or scan_truncated,
        "errors": errors,
    }


def _pct(numerator: int, denominator: int) -> float | None:
    """占比（百分数，保留 2 位）；分母为 0 时返回 None。"""
    if not denominator:
        return None
    return round(numerator / denominator * 100, 2)


def _norm_labels(mapping: Any) -> dict[str, str]:
    """把 pyreadstat 的值标签规范化为 {字符串化的码值: 标签文本}。"""
    return {_label_key(k): str(v) for k, v in (mapping or {}).items()}


def _labelled(value: Any, mapping: dict[str, str] | None) -> str | None:
    """按规范化后的值标签把码值译为文本；无标签返回 None。"""
    if not mapping:
        return None
    text = mapping.get(_label_key(value))
    return None if text is None else text


def _numeric_stats(series: pd.Series) -> dict[str, Any]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return {k: None for k in ("mean", "std", "min", "p25", "median", "p75", "max")}
    quantiles = clean.quantile([0.25, 0.5, 0.75])
    return {
        "mean": _jsonable(round(float(clean.mean()), 6)),
        "std": _jsonable(round(float(clean.std()), 6)),
        "min": _jsonable(clean.min()),
        "p25": _jsonable(quantiles.loc[0.25]),
        "median": _jsonable(quantiles.loc[0.5]),
        "p75": _jsonable(quantiles.loc[0.75]),
        "max": _jsonable(clean.max()),
    }


def _datetime_stats(series: pd.Series) -> dict[str, Any]:
    clean = series.dropna()
    if clean.empty:
        return {"min": None, "max": None}
    try:
        return {"min": _jsonable(clean.min()), "max": _jsonable(clean.max())}
    except (TypeError, ValueError):
        return {"min": None, "max": None}


def _char_stats(series: pd.Series, mapping: dict[str, str] | None) -> dict[str, Any]:
    clean = series.dropna()
    if clean.empty:
        return {"max_length": 0, "top_values": []}
    lengths = clean.astype(str).str.len()
    counts = clean.value_counts()
    top_values = [
        {
            "value": _jsonable(value),
            "label": _labelled(value, mapping),
            "n": int(count),
            "pct": _pct(int(count), len(clean)),
        }
        for value, count in counts.head(5).items()
    ]
    return {"max_length": int(lengths.max()), "top_values": top_values}


def _label_lookup(meta: Any, df: pd.DataFrame) -> dict[str, str]:
    """列名 -> SAS 变量标签（在筛选列之前建立，避免索引错位）。"""
    raw = getattr(meta, "column_labels", None) or []
    return {
        str(name): str(raw[idx]) if idx < len(raw) and raw[idx] is not None else ""
        for idx, name in enumerate(df.columns)
    }


def summarize_dataset(
    path: Path,
    *,
    columns: list[str] | None = None,
    apply_labels: bool = False,
) -> dict[str, Any]:
    """逐变量概要统计：缺失率、唯一值数，并按类型给出分布指标。

    数值 -> mean/std/min/p25/median/p75/max；日期 -> min/max；字符 -> 最大长度 + Top5 取值。
    带值标签的变量会标 has_value_labels，提示改用 frequency 看解码后的分布。
    """
    df, meta = _read(path, metadata_only=False)
    total_rows = int(len(df))
    label_lookup = _label_lookup(meta, df)
    value_labels = getattr(meta, "variable_value_labels", None) or {}

    names = [str(c) for c in df.columns]
    if columns:
        missing = [str(c) for c in columns if str(c) not in names]
        if missing:
            raise DatasetError(f"未知变量: {missing}；可用变量见 get_dataset_metadata")
        names = [str(c) for c in columns]

    summaries: list[dict[str, Any]] = []
    for name in names:
        series = df[name]
        n_missing = int(series.isna().sum())
        kind = _column_type(meta, name, series.dtype)
        item: dict[str, Any] = {
            "name": name,
            "label": label_lookup.get(name, ""),
            "type": kind,
            "n": total_rows - n_missing,
            "n_missing": n_missing,
            "pct_missing": _pct(n_missing, total_rows),
            "n_distinct": int(series.nunique(dropna=True)),
            "has_value_labels": name in value_labels,
        }
        if kind == "numeric":
            item["stats"] = _numeric_stats(series)
        elif kind == "datetime":
            item["stats"] = _datetime_stats(series)
        else:
            mapping = _norm_labels(value_labels.get(name)) if apply_labels else None
            item["stats"] = _char_stats(series, mapping)
        summaries.append(item)

    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "total_rows": total_rows,
        "n_columns": len(names),
        "summaries": summaries,
    }


def frequency(
    path: Path,
    column: str,
    *,
    limit: int = 50,
    offset: int = 0,
    apply_labels: bool = True,
    include_missing: bool = False,
) -> dict[str, Any]:
    """单变量频数表：每个取值的 n 与占比，默认自动套用 SAS 值标签。

    apply_labels 默认 True，因为 coded 值本身不可读；原始 value 一并保留。
    分母随 include_missing 变化：包含缺失时按总行数，否则按非缺失行数，
    结果里回传 denominator 让调用方明确百分比基准。
    """
    df, meta = _read(path, metadata_only=False)
    if column not in df.columns:
        raise DatasetError(f"未知变量: {column}；可用变量见 get_dataset_metadata")

    total_rows = int(len(df))
    series = df[column]
    mapping = _norm_labels((getattr(meta, "variable_value_labels", None) or {}).get(column))
    n_missing = int(series.isna().sum())
    counts = series.value_counts(dropna=True)
    n_distinct = int(counts.shape[0])
    denominator = total_rows if include_missing else total_rows - n_missing

    levels = [
        {
            "value": _jsonable(value),
            "label": _labelled(value, mapping) if apply_labels else None,
            "n": int(count),
            "pct": _pct(int(count), denominator),
        }
        for value, count in counts.items()
    ]
    if include_missing and n_missing:
        levels.append(
            {"value": None, "label": "<missing>", "n": n_missing, "pct": _pct(n_missing, denominator)}
        )
    levels.sort(key=lambda item: (-item["n"], str(item["value"])))

    effective = max(1, min(int(limit), MAX_AGG_ROWS))
    start = max(0, int(offset))
    page = levels[start : start + effective]
    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "column": str(column),
        "column_label": _label_lookup(meta, df).get(str(column), ""),
        "type": _column_type(meta, str(column), series.dtype),
        "total_rows": total_rows,
        "n_missing": n_missing,
        "n_distinct": n_distinct,
        "denominator": denominator,
        "include_missing": bool(include_missing),
        "value_labels_applied": bool(apply_labels and mapping),
        "offset": start,
        "levels": page,
        "returned": len(page),
        "truncated": len(levels) > start + len(page),
    }
