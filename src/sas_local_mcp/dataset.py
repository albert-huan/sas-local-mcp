"""本地 SAS 数据集读取核心。

用 pyreadstat 直接读取本地 .sas7bdat / .xpt 文件，无需 SAS 许可；
pyreadstat 不可用时回退 pandas.read_sas。
"""

from __future__ import annotations

import contextlib
import copy
import math
import os
import re
from datetime import date, datetime, time
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any

import pandas as pd

try:  # pragma: no cover - import guard
    import pyreadstat
except ImportError:  # pragma: no cover
    pyreadstat = None

from .config import (
    CACHE_SIZE, CACHE_MAX_BYTES, CHUNK_ROWS, ENCODING, MAX_AGG_ROWS, MAX_ROWS,
    METADATA_CACHE_SIZE, SAS_DATA_ROOT, SQL_CONCURRENCY, SQL_MEMORY_MB, SQL_THREADS,
)
from .cache import ReadCache
from .errors import DatasetError
from .filtering import RowFilter
from .sql_labels import inspect_query

READABLE_SUFFIXES: dict[str, str] = {".sas7bdat": "sas7bdat", ".xpt": "xport"}
# .sas7bcat 是格式目录文件：可被 list_datasets 发现，但没有读取器。
KNOWN_SUFFIXES: dict[str, str] = {**READABLE_SUFFIXES, ".sas7bcat": "sas7bcat"}

class _DecodedPage(dict):
    """Keep decoding masks out of JSON while the MCP adapter budgets a page."""
    def __init__(self, payload, decoding_masks):
        super().__init__(payload)
        self.decoding_masks = decoding_masks


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
        raise DatasetError(f"路径越界: {path!r} 不在 SAS_DATA_ROOT({SAS_DATA_ROOT}) 内", "PATH_OUTSIDE_ROOT")
    if not normalized.is_file():
        raise DatasetError(f"文件不存在: {path!r} (相对于 {SAS_DATA_ROOT})", "FILE_NOT_FOUND")
    return normalized


def dataset_format(path: Path) -> str:
    return KNOWN_SUFFIXES.get(path.suffix.lower(), "unknown")


def _rel(path: Path) -> str:
    try:
        return Path(os.path.normpath(path)).relative_to(SAS_DATA_ROOT).as_posix()
    except ValueError:
        return str(path)


def _call_reader(reader: Any, path: Path, metadata_only: bool, **options) -> tuple[pd.DataFrame, Any]:
    """Read raw codes; labels are attached independently and applied only to a page."""
    kwargs: dict[str, Any] = dict(options)
    if ENCODING:
        kwargs["encoding"] = ENCODING
    if dataset_format(path) == "sas7bdat":
        kwargs["user_missing"] = True
    return reader(str(path), metadataonly=metadata_only, **kwargs)


def _formats(meta: Any) -> dict:
    # pyreadstat calls SAS formats original_variable_types; accept legacy/test meta too.
    return {**(getattr(meta, "original_variable_types", None) or {}),
            **(getattr(meta, "original_variable_formats", None) or {})}


def _attach_catalog(result: tuple[pd.DataFrame, Any], catalog_path: Path | None):
    if catalog_path is None:
        return result
    if catalog_path.suffix.lower() != ".sas7bcat":
        raise DatasetError("catalog_path 必须是 .sas7bcat 格式目录", "INVALID_CATALOG")
    if pyreadstat is None:
        raise DatasetError("格式目录需要 pyreadstat", "INVALID_CATALOG")
    key = _cache_key(catalog_path, True)
    def load():
        kwargs = {"encoding": ENCODING} if ENCODING else {}
        _, catalog = pyreadstat.read_sas7bcat(str(catalog_path), **kwargs)
        return catalog
    catalog = _CATALOG_CACHE.get_or_load(
        key, load, capacity=METADATA_CACHE_SIZE if CACHE_SIZE else 0,
        max_bytes=32 * 1024 * 1024, weigh=lambda value: len(repr(value.value_labels).encode("utf-8")),
    )
    frame, original = result
    meta = copy.copy(original)
    labels = dict(getattr(meta, "variable_value_labels", None) or {})
    catalog_labels = getattr(catalog, "value_labels", None) or {}
    variable_formats = {**_formats(meta), **(getattr(meta, "variable_to_label", None) or {})}
    for variable, fmt in variable_formats.items():
        match = next((mapping for name, mapping in catalog_labels.items()
                      if re.fullmatch(re.escape(str(name).rstrip(".")) + r"(?:\d+(?:\.\d+)?)?\.?", str(fmt), re.IGNORECASE)), None)
        if match is not None:
            labels[variable] = dict(match)
    meta.variable_value_labels = labels
    return frame, meta


def _read_uncached(path: Path, metadata_only: bool, catalog_path: Path | None = None, **options) -> tuple[pd.DataFrame, Any]:
    """读取数据集，返回 (DataFrame, pyreadstat meta 或 None)。"""
    fmt = dataset_format(path)
    if fmt not in READABLE_SUFFIXES.values():
        raise DatasetError(
            f"不支持的格式 {path.suffix!r}（可读取: .sas7bdat / .xpt；.sas7bcat 用作 catalog_path）", "UNSUPPORTED_FORMAT"
        )
    if pyreadstat is not None:
        reader = pyreadstat.read_sas7bdat if fmt == "sas7bdat" else pyreadstat.read_xport
        try:
            return _attach_catalog(_call_reader(reader, path, metadata_only, **options), catalog_path)
        except DatasetError:
            raise
        except Exception as exc:
            encoding_error = isinstance(exc, UnicodeError) or any(marker in str(exc).lower() for marker in (
                "codec can't decode", "unable to convert string", "unsupported character set"))
            raise DatasetError(
                f"读取 {path.name} 失败: {exc}（文件可能损坏、截断、编码不匹配或版本不受支持）",
                "ENCODING_ERROR" if encoding_error else "READ_FAILED",
            ) from exc
    # pandas 回退：只支持全量读取，无 meta 信息。
    try:
        kwargs = {"encoding": ENCODING} if ENCODING else {}
        if catalog_path:
            raise DatasetError("格式目录需要 pyreadstat", "INVALID_CATALOG")
        frame = pd.read_sas(str(path), format=fmt, **kwargs)
        if options.get("usecols") is not None:
            frame = frame[options["usecols"]]
        start = options.get("row_offset", 0)
        stop = start + options["row_limit"] if options.get("row_limit") else None
        return frame.iloc[start:stop], None
    except Exception as exc:
        raise DatasetError(f"pandas 读取 {path.name} 失败: {exc}", "READ_FAILED") from exc


_CACHE = ReadCache()
_META_CACHE = ReadCache()
_CATALOG_CACHE = ReadCache()
_COUNT_CACHE = ReadCache()
_QUERY_SLOTS = BoundedSemaphore(SQL_CONCURRENCY)


def _cache_key(path: Path, metadata_only: bool, catalog_path: Path | None = None) -> tuple:
    stat = path.stat()
    catalog = None
    if catalog_path is not None:
        cat_stat = catalog_path.stat()
        catalog = (str(catalog_path), cat_stat.st_mtime_ns, cat_stat.st_size)
    return (str(path), stat.st_mtime_ns, stat.st_size, metadata_only, ENCODING, catalog)


def _read(path: Path, metadata_only: bool, catalog_path: Path | None = None) -> tuple[pd.DataFrame, Any]:
    """Separate metadata/data caches, byte budgets, file-version checks and single-flight."""
    key = _cache_key(path, metadata_only, catalog_path)
    def load():
        result = (_read_uncached(path, metadata_only) if catalog_path is None
                  else _read_uncached(path, metadata_only, catalog_path=catalog_path))
        if key != _cache_key(path, metadata_only, catalog_path):
            raise DatasetError(f"读取期间文件发生变化: {path.name}，请重试", "FILE_CHANGED")
        return result
    cache = _META_CACHE if metadata_only else _CACHE
    return cache.get_or_load(
        key, load, capacity=(METADATA_CACHE_SIZE if metadata_only else CACHE_SIZE) if CACHE_SIZE else 0,
        max_bytes=32 * 1024 * 1024 if metadata_only else CACHE_MAX_BYTES,
        weigh=lambda result: int(result[0].memory_usage(index=True, deep=True).sum())
        + len(repr(vars(result[1]) if result[1] is not None else {}).encode("utf-8")),
    )


def clear_caches() -> None:
    for cache in (_CACHE, _META_CACHE, _CATALOG_CACHE, _COUNT_CACHE):
        cache.clear()


def _read_with_catalog(path: Path, metadata_only: bool, catalog_path: Path | None = None):
    return (_read(path, metadata_only=metadata_only) if catalog_path is None
            else _read(path, metadata_only=metadata_only, catalog_path=catalog_path))


def _slices(path: Path, catalog_path: Path | None, columns: list[str] | None = None):
    offset = 0
    revision = _cache_key(path, False, catalog_path)
    while True:
        frame, meta = _read_uncached(path, False, catalog_path=catalog_path,
                                     usecols=columns, row_offset=offset, row_limit=CHUNK_ROWS)
        if revision != _cache_key(path, False, catalog_path):
            raise DatasetError(f"读取期间文件发生变化: {path.name}，请重试", "FILE_CHANGED")
        if frame.empty:
            break
        yield frame, meta
        offset += len(frame)
        if len(frame) < CHUNK_ROWS:
            break


def _exact_row_count(path: Path, df: pd.DataFrame, meta: Any, catalog_path: Path | None) -> int:
    count = _row_count(df, meta)
    if count is not None:
        return count
    def count_rows():
        if pyreadstat is None or not path.is_file():
            return len(_read_with_catalog(path, False, catalog_path)[0])
        # Counting XPORT requires a scan, but only one column and one chunk at a time.
        return sum(len(chunk) for chunk, _ in _slices(path, catalog_path, list(df.columns[:1])))
    return _COUNT_CACHE.get_or_load(
        _cache_key(path, True, catalog_path), count_rows,
        capacity=METADATA_CACHE_SIZE if CACHE_SIZE else 0, max_bytes=1024 * 1024, weigh=lambda _: 8,
    )


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
        if pd.api.types.is_datetime64_any_dtype(dtype):
            return "datetime"
        return "str" if str(dtype) in {"object", "str", "string"} else "numeric"
    readstat_types = getattr(meta, "readstat_variable_types", None) or {}
    informats = getattr(meta, "original_variable_informats", None) or {}
    formats = _formats(meta)
    kind = readstat_types.get(name)
    if kind is None:  # pandas 回退（无 meta）：对象/字符串 dtype 视为字符
        return "str" if str(dtype) in {"object", "str", "string"} else "numeric"
    if kind != "string":
        text = f"{informats.get(name) or ''} {formats.get(name) or ''}".upper()
        if _temporal_kind(text):
            return "datetime"
        return "numeric"
    return "str"


def _temporal_kind(text: str) -> str | None:
    tokens = text.upper().split()
    datetime_formats = ("DATETIME", "DATEAMPM", "MDYAMPM", "E8601DT", "E8601DZ", "B8601DT", "B8601DZ", "NLDATM", "ANYDTDTM", "DTDATE", "DTMONYY", "DTWKDAT", "DTYEAR")
    time_formats = ("TIME", "HHMM", "HOUR", "MMSS", "E8601TM", "E8601TZ", "B8601TM", "B8601TZ", "ANYDTTME", "NLTIME")
    date_formats = ("DATE", "YYMM", "MMDD", "DDMM", "MMYY", "JULIAN", "JULDAY", "MONYY", "YYMON", "WORDDATE", "WEEKDATE", "E8601DA", "B8601DA", "ANYDTDTE", "NLDATE")
    for kind, formats in (("datetime", datetime_formats), ("time", time_formats), ("date", date_formats)):
        if any(re.fullmatch(re.escape(fmt) + r"\d*(?:\.\d*)?", token) for token in tokens for fmt in formats):
            return kind
    return None


def _logical_type(meta: Any, name: str, dtype: Any) -> str:
    kind = _column_type(meta, name, dtype)
    if kind == "datetime":
        informats = getattr(meta, "original_variable_informats", None) or {}
        return _temporal_kind(f"{informats.get(name, '')} {_formats(meta).get(name, '')}") or "datetime"
    return "string" if kind == "str" else kind


def _special_tags(meta: Any, name: str) -> set[str]:
    return {str(tag).lstrip(".") for tag in (getattr(meta, "missing_user_values", None) or {}).get(name, [])}


def _missing_mask(series: pd.Series, meta: Any, name: str, missing_policy: str = "sas") -> pd.Series:
    if missing_policy not in {"sas", "pandas"}:
        raise DatasetError("missing_policy 必须为 sas 或 pandas", "INVALID_ARGUMENT")
    mask = series.isna()
    tags = _special_tags(meta, name)
    if tags:
        mask = mask | series.isin(tags | {"." + tag for tag in tags})
    if missing_policy == "sas" and _column_type(meta, name, series.dtype) == "str":
        mask = mask | series.map(lambda value: isinstance(value, str) and not value.strip(" "))
    return mask


def _missing_details(series: pd.Series, meta: Any, name: str, missing_policy: str) -> dict[str, int]:
    details = {}
    standard = int(series.isna().sum())
    if standard:
        details["."] = standard
    for tag in sorted(_special_tags(meta, name)):
        count = int(series.isin({tag, "." + tag}).sum())
        if count:
            details["." + tag] = count
    if missing_policy == "sas" and _column_type(meta, name, series.dtype) == "str":
        count = int(series.map(lambda value: isinstance(value, str) and not value.strip(" ")).sum())
        if count:
            details["blank"] = count
    return details


def _raw_special_values(frame: pd.DataFrame, meta: Any) -> pd.DataFrame:
    result = frame
    for name in frame.columns:
        tags = _special_tags(meta, str(name))
        if tags:
            if result is frame:
                result = frame.copy()
            result[name] = result[name].map(lambda value: "." + value if isinstance(value, str) and value in tags else value)
    return result


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
    if isinstance(value, float) and not math.isfinite(value):
        return None
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
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            suffix = Path(name).suffix.lower()
            if suffix not in KNOWN_SUFFIXES:
                continue
            if readable_only and suffix not in READABLE_SUFFIXES:
                continue
            if pattern and pattern.lower() not in name.lower():
                continue
            yield Path(dirpath) / name


def list_datasets(pattern: str | None = None, limit: int = 500, offset: int = 0) -> dict[str, Any]:
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
    start = max(0, int(offset))
    page = found[start:start + effective]
    truncated = len(found) > start + len(page)
    return {
        "root": str(SAS_DATA_ROOT),
        "count": len(found),
        "datasets": page,
        "offset": start,
        "returned": len(page),
        "truncated": truncated,
        "next_offset": start + len(page) if truncated else None,
    }


def read_metadata(path: Path, *, catalog_path: Path | None = None, include_row_count: bool = True,
                  column_offset: int = 0, column_limit: int = 500) -> dict[str, Any]:
    """元数据与变量分页；XPORT 精确行数按块数一列，可跳过计数返回 None。"""
    df, meta = _read_with_catalog(path, True, catalog_path)
    number_rows = _row_count(df, meta)
    if number_rows is None and include_row_count:
        number_rows = _exact_row_count(path, df, meta, catalog_path)
    informats = (getattr(meta, "original_variable_informats", None) or {}) if meta is not None else {}
    formats = _formats(meta)
    user_missing = (getattr(meta, "missing_user_values", None) or {}) if meta is not None else {}
    columns = [
        {
            "name": str(name),
            "label": _meta_item(meta, "column_labels", idx),
            "type": _column_type(meta, str(name), df.dtypes.iloc[idx]),
            "logical_type": _logical_type(meta, str(name), df.dtypes.iloc[idx]),
            "storage_width": (getattr(meta, "variable_storage_width", None) or {}).get(str(name)),
            "sas_unit": {"date": "days_since_1960-01-01", "time": "seconds_since_midnight",
                         "datetime": "seconds_since_1960-01-01T00:00:00"}.get(_logical_type(meta, str(name), df.dtypes.iloc[idx])),
            "format": "" if formats.get(str(name)) is None else str(formats.get(str(name))),
            "informat": "" if informats.get(str(name)) is None else str(informats.get(str(name))),
        }
        for idx, name in enumerate(df.columns)
    ]
    start = max(0, int(column_offset))
    effective = max(1, min(int(column_limit), 5000))
    page = columns[start:start + effective]
    truncated = len(columns) > start + len(page)
    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "table_name": _meta_scalar(meta, "table_name", None),
        "number_rows": number_rows,
        "row_count_exact": number_rows is not None,
        "number_columns": len(df.columns),
        "file_label": _meta_scalar(meta, "file_label", ""),
        "file_encoding": _meta_scalar(meta, "file_encoding", None),
        "created": _jsonable(_meta_scalar(meta, "creation_time", None)),
        "modified": _jsonable(_meta_scalar(meta, "modification_time", None)),
        "user_missing": {str(k): v for k, v in user_missing.items()},
        "columns": page,
        "catalog_path": _rel(catalog_path) if catalog_path else None,
        "column_offset": start,
        "returned_columns": len(page),
        "truncated": truncated,
        "next_column_offset": start + len(page) if truncated else None,
    }


def read_rows(
    path: Path,
    *,
    limit: int = 20,
    offset: int = 0,
    columns: list[str] | None = None,
    where: str | None = None,
    apply_labels: bool = False,
    catalog_path: Path | None = None,
    include_row_count: bool = True,
    missing_policy: str = "sas",
) -> dict[str, Any]:
    """Read only a requested page; filter chunks before projecting output columns."""
    actual_file = pyreadstat is not None and path.is_file()
    initial_revision = _cache_key(path, False, catalog_path) if actual_file else None
    df, meta = _read_with_catalog(path, True, catalog_path)
    names = list(df.columns)
    if columns:
        missing = [c for c in columns if c not in names]
        if missing:
            raise DatasetError(f"未知变量: {missing}；可用变量见 get_dataset_metadata", "UNKNOWN_VARIABLE")
    selected = list(dict.fromkeys(columns or names))
    effective = max(1, min(int(limit), MAX_ROWS))
    start = max(0, int(offset))
    predicate = RowFilter(where, names) if where else None
    if missing_policy not in {"sas", "pandas"}:
        raise DatasetError("missing_policy 必须为 sas 或 pandas", "INVALID_ARGUMENT")
    total_rows = _row_count(df, meta)
    if actual_file and not predicate:
        if total_rows is None and include_row_count:
            total_rows = _exact_row_count(path, df, meta, catalog_path)
        revision = _cache_key(path, False, catalog_path)
        if total_rows is not None and start >= total_rows:
            page = df[selected].iloc[:0]
        else:
            page, meta = _read_uncached(path, False, catalog_path=catalog_path,
                                       usecols=selected, row_offset=start, row_limit=effective + 1)
            page = page[selected]
        if revision != _cache_key(path, False, catalog_path):
            raise DatasetError(f"读取期间文件发生变化: {path.name}，请重试", "FILE_CHANGED")
        rows_after_filter = total_rows
        more = len(page) > effective or (total_rows is not None and total_rows > start + effective)
        page = page.iloc[:effective]
    else:
        needed = list(dict.fromkeys(selected + sorted(predicate.columns if predicate else [])))
        chunks = (_slices(path, catalog_path, needed) if actual_file
                  else [_read_with_catalog(path, False, catalog_path)])
        matched = scanned = 0
        pages = []
        exhausted = True
        for chunk, chunk_meta in chunks:
            if chunk_meta is not None and meta is not None:
                meta = copy.copy(meta)
                tags = dict(getattr(meta, "missing_user_values", None) or {})
                for name, values in (getattr(chunk_meta, "missing_user_values", None) or {}).items():
                    tags[name] = sorted(set(tags.get(name, [])) | set(values))
                meta.missing_user_values = tags
            scanned += len(chunk)
            if predicate:
                filter_data = chunk.copy()
                for name in predicate.columns:
                    mask = _missing_mask(filter_data[name], chunk_meta, name, missing_policy)
                    filter_data[name] = filter_data[name].mask(mask)
                    if _column_type(chunk_meta, name, chunk[name].dtype) == "numeric":
                        filter_data[name] = pd.to_numeric(filter_data[name], errors="coerce")
                chunk = chunk.loc[predicate.apply(filter_data)]
            local_start = max(0, start - matched)
            local_stop = max(0, start + effective + 1 - matched)
            if local_start < len(chunk) and local_stop > local_start:
                pages.append(chunk.iloc[local_start:local_stop][selected])
            matched += len(chunk)
            if not include_row_count and matched > start + effective:
                exhausted = False
                break
        page = pd.concat(pages, ignore_index=True) if pages else df[selected].iloc[:0]
        more = len(page) > effective
        page = page.iloc[:effective]
        rows_after_filter = matched if exhausted else None
        if exhausted:
            total_rows = scanned
    if initial_revision is not None and initial_revision != _cache_key(path, False, catalog_path):
        raise DatasetError(f"读取期间文件发生变化: {path.name}，请重试", "FILE_CHANGED")
    page = _raw_special_values(page, meta)
    value_labels = getattr(meta, "variable_value_labels", None) or {}
    decoded = []
    decoding_masks = {}
    if apply_labels:
        page = page.copy()
        for var in page.columns:
            mapping = _norm_labels(value_labels.get(var))
            if mapping:
                mask = page[var].map(lambda value: _label_key(value) in mapping)
                decoding_masks[str(var)] = mask.tolist()
                if mask.any():
                    decoded.append(str(var))
                    page[var] = page[var].map(lambda value: mapping.get(_label_key(value), value))
    returned = len(page)
    return _DecodedPage({
        "path": _rel(path),
        "format": dataset_format(path),
        "total_rows": total_rows,
        "rows_after_filter": rows_after_filter,
        "row_count_exact": total_rows is not None,
        "offset": start,
        "columns": [str(c) for c in page.columns],
        "records": _to_records(page),
        "returned": returned,
        "truncated": more,
        "next_offset": start + returned if more else None,
        "value_labels_applied": bool(decoded),
        "decoded_columns": decoded,
        "missing_policy": missing_policy,
        "catalog_path": _rel(catalog_path) if catalog_path else None,
    }, decoding_masks)


def _label_key(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def get_value_labels(path: Path, *, catalog_path: Path | None = None,
                     columns: list[str] | None = None) -> dict[str, Any]:
    """变量的 SAS 格式值标签映射（QC 解码 coded 值用）。"""
    _df, meta = _read_with_catalog(path, True, catalog_path)
    if columns and set(columns) - set(_df.columns):
        raise DatasetError(f"未知变量: {sorted(set(columns) - set(_df.columns))}", "UNKNOWN_VARIABLE")
    raw = getattr(meta, "variable_value_labels", None) or {}
    variables = {
        str(var): {_label_key(k): str(v) for k, v in mapping.items()}
        for var, mapping in raw.items() if columns is None or var in columns
    }
    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "variables": variables,
        "catalog_path": _rel(catalog_path) if catalog_path else None,
    }


def _sql_identifier(stem: str) -> str:
    """把文件名主干转成合法的 duckdb 表名。"""
    ident = re.sub(r"[^0-9A-Za-z_]", "_", stem).strip("_")
    if not ident:
        ident = "t"
    if ident[0].isdigit():
        ident = f"t_{ident}"
    return ident.lower()


def _sql_frame(frame: pd.DataFrame, meta: Any, missing_policy: str) -> pd.DataFrame:
    result = frame
    for name in frame.columns:
        series = frame[name]
        mask = _missing_mask(series, meta, str(name), missing_policy) & series.notna()
        if mask.any():
            if result is frame:
                result = frame.copy()
            clean = series.mask(mask)
            result[name] = (pd.to_numeric(clean, errors="coerce")
                            if _column_type(meta, str(name), series.dtype) == "numeric" else clean)
    return result


def query_dataset(
    path: Path,
    sql: str,
    limit: int = 100,
    offset: int = 0,
    extra_paths: list[Path] | None = None,
    apply_labels: bool = False,
    *,
    include_total: bool = True,
    label_sources: dict[str, dict[str, str]] | None = None,
    catalog_paths: dict[str, Path] | None = None,
    missing_policy: str = "sas",
) -> dict[str, Any]:
    """Bound concurrent reads/queries as well as DuckDB's per-connection resources."""
    with _QUERY_SLOTS:
        return _query_dataset(path, sql, limit, offset, extra_paths, apply_labels,
                              include_total, label_sources, catalog_paths, missing_policy)


def _query_dataset(path, sql, limit, offset, extra_paths, apply_labels,
                   include_total, label_sources, catalog_paths, missing_policy):
    try:
        import duckdb
    except ImportError as exc:
        raise DatasetError("duckdb 未安装，请执行: pip install duckdb", "MISSING_DEPENDENCY") from exc

    try:
        statements = duckdb.extract_statements(sql)
    except Exception as exc:
        raise DatasetError(f"SQL 解析失败: {exc}", "INVALID_SQL") from exc
    if len(statements) != 1 or statements[0].type.name != "SELECT":
        raise DatasetError("只允许单条 SELECT 查询语句（只读服务器）", "READ_ONLY_SQL")
    if missing_policy not in {"sas", "pandas"}:
        raise DatasetError("missing_policy 必须为 sas 或 pandas", "INVALID_ARGUMENT")
    if len(extra_paths or []) > 15:
        raise DatasetError("一次查询最多注册 16 张表", "INVALID_ARGUMENT")

    metas: dict[str, Any] = {}
    # (表名, DataFrame, 原始路径)
    frames: list[tuple[str, pd.DataFrame, Path]] = []
    catalog_paths = catalog_paths or {}
    main_df, main_meta = _read_with_catalog(path, False, catalog_paths.get("dataset"))
    metas["dataset"] = main_meta
    frames.append(("dataset", main_df, path))

    used = {"dataset"}
    for extra in extra_paths or []:
        name = _sql_identifier(extra.stem)
        if name in used:
            name = f"dataset{len(used) + 1}"
        while name in used:
            name = f"{name}_"
        used.add(name)
        extra_df, extra_meta = _read_with_catalog(extra, False, catalog_paths.get(name))
        metas[name] = extra_meta
        frames.append((name, extra_df, extra))

    if set(catalog_paths) - used:
        raise DatasetError(f"catalog_paths 含未知表名: {sorted(set(catalog_paths) - used)}", "INVALID_CATALOG")
    table_columns = {name: list(frame.columns) for name, frame, _ in frames}
    automatic_sources, ordered = inspect_query(sql, table_columns)
    effective = max(1, min(int(limit), MAX_AGG_ROWS))
    start = max(0, int(offset))

    # SQL functions such as read_csv can otherwise access files outside the data root.
    con = None
    try:
        con = duckdb.connect(config={"enable_external_access": "false", "memory_limit": f"{SQL_MEMORY_MB}MB",
                                     "threads": str(SQL_THREADS), "max_temp_directory_size": "0B"})
        for name, frame, _ in frames:
            con.register(name, _sql_frame(frame, metas[name], missing_policy))
        relation = con.sql(sql)
        # Fetch at most a page and a sentinel; no full result DataFrame is created.
        result = relation.limit(effective + 1, offset=start).fetchdf()
        total_rows = int(relation.aggregate("count(*) AS n").fetchone()[0]) if include_total else None
    except Exception as exc:
        code = "QUERY_RESOURCE_LIMIT" if isinstance(exc, duckdb.OutOfMemoryException) else "QUERY_FAILED"
        raise DatasetError(f"SQL 执行失败: {exc}", code) from exc
    finally:
        if con is not None:
            for name, _, _ in frames:
                with contextlib.suppress(Exception):
                    con.unregister(name)
            con.close()

    more = len(result) > effective
    page = result.iloc[:effective].copy()
    sources = dict(automatic_sources)
    for output, origin in (label_sources or {}).items():
        table, column = origin.get("table"), origin.get("column")
        if output not in page.columns or table not in table_columns or column not in table_columns[table]:
            raise DatasetError(f"无效的标签来源: {output} -> {origin}", "INVALID_LABEL_SOURCE")
        sources[output] = (table, column)
    decoding = {}
    decoding_masks = {}
    for col in page.columns:
        origin = sources.get(str(col))
        status = "not_requested" if not apply_labels else "source_unknown"
        count = 0
        if apply_labels and origin:
            table, variable = origin
            mapping = _norm_labels((getattr(metas[table], "variable_value_labels", None) or {}).get(variable))
            status = "no_labels" if not mapping else "no_matching_values"
            if mapping:
                mask = page[col].map(lambda value: _label_key(value) in mapping)
                decoding_masks[str(col)] = mask.tolist()
                count = int(mask.sum())
                if count:
                    page[col] = page[col].map(lambda value: mapping.get(_label_key(value), value))
                    status = "decoded"
        decoding[str(col)] = {"source_table": origin[0] if origin else None,
                              "source_column": origin[1] if origin else None,
                              "status": status, "values_decoded": count}
    returned = len(page)
    warnings = []
    if not ordered and (more or start):
        warnings.append("UNORDERED_PAGINATION: 请在 SQL 最外层使用具有唯一排序键的 ORDER BY，以便稳定翻页")
    if apply_labels and any(info["status"] == "source_unknown" for info in decoding.values()):
        warnings.append("LABEL_SOURCE_UNKNOWN: 表达式、歧义列和复杂查询保留原值；可显式提供 label_sources")
    return _DecodedPage({
        "path": _rel(path),
        "sql": sql,
        "tables": {name: _rel(frame_path) for name, _, frame_path in frames},
        "total_rows": total_rows,
        "row_count_exact": include_total,
        "offset": start,
        "columns": [str(c) for c in page.columns],
        "records": _to_records(page),
        "returned": returned,
        "truncated": more,
        "next_offset": start + returned if more else None,
        "value_labels_applied": any(info["values_decoded"] for info in decoding.values()),
        "label_decoding": decoding,
        "order_by_present": ordered,
        "warnings": warnings,
        "missing_policy": missing_policy,
        "special_missing_values": {name: {str(var): ["." + tag for tag in sorted(_special_tags(meta, str(var)))]
                                   for var in (getattr(meta, "missing_user_values", None) or {})}
                                   for name, meta in metas.items()},
    }, decoding_masks)


def search_variables(
    variable: str,
    pattern: str | None = None,
    contains: bool = False,
    limit: int = 50,
    max_datasets: int = 200,
    offset: int = 0,
    scan_offset: int = 0,
) -> dict[str, Any]:
    """在 SAS_DATA_ROOT 下搜索含指定变量的数据集。

    回答"哪个数据集里有 USUBJID / 哪些表有 AEDECOD"这类问题，避免逐个
    get_dataset_metadata 试错。只读取元数据（metadataonly），不加载数据行。
    损坏文件会被记入 errors 而不是中断整个搜索。
    """
    needle = variable.strip()
    if not needle:
        raise DatasetError("variable 不能为空", "INVALID_ARGUMENT")
    target = needle.lower()
    matches: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    scanned = 0
    scan_limit = max(1, int(max_datasets))
    files = iter(_iter_dataset_files(pattern, readable_only=True))
    start = max(0, int(offset))
    scan_start = max(0, int(scan_offset))
    for _ in range(scan_start):
        if next(files, None) is None:
            break

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
    page = matches[start:start + effective]
    more_matches = len(matches) > start + len(page)
    return {
        "root": str(SAS_DATA_ROOT),
        "variable": needle,
        "contains": bool(contains),
        "datasets_scanned": scanned,
        "datasets_matched": len({m["path"] for m in matches}),
        "matches": page,
        "returned": len(page),
        "offset": start,
        "scan_offset": scan_start,
        "next_offset": start + len(page) if more_matches else None,
        "next_scan_offset": scan_start + scanned if scan_truncated else None,
        "scan_truncated": scan_truncated,
        "truncated": more_matches or scan_truncated,
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
    catalog_path: Path | None = None,
    missing_policy: str = "sas",
    stats_mode: str = "full",
    offset: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    """逐变量概要统计：缺失率、唯一值数，并按类型给出分布指标。

    数值 -> mean/std/min/p25/median/p75/max；日期 -> min/max；字符 -> 最大长度 + Top5 取值。
    带值标签的变量会标 has_value_labels，提示改用 frequency 看解码后的分布。
    """
    if stats_mode not in {"full", "counts"}:
        raise DatasetError("stats_mode 必须为 full 或 counts", "INVALID_ARGUMENT")
    if missing_policy not in {"sas", "pandas"}:
        raise DatasetError("missing_policy 必须为 sas 或 pandas", "INVALID_ARGUMENT")
    df, meta = _read_with_catalog(path, False, catalog_path)
    total_rows = int(len(df))
    label_lookup = _label_lookup(meta, df)
    value_labels = getattr(meta, "variable_value_labels", None) or {}

    names = [str(c) for c in df.columns]
    if columns:
        missing = [str(c) for c in columns if str(c) not in names]
        if missing:
            raise DatasetError(f"未知变量: {missing}；可用变量见 get_dataset_metadata", "UNKNOWN_VARIABLE")
        names = [str(c) for c in columns]

    start = max(0, int(offset))
    effective = max(1, min(int(limit), MAX_AGG_ROWS))
    summaries: list[dict[str, Any]] = []
    for name in names[start:start + effective]:
        series = df[name]
        mask = _missing_mask(series, meta, name, missing_policy)
        n_missing = int(mask.sum())
        clean = series.loc[~mask]
        kind = _column_type(meta, name, series.dtype)
        item: dict[str, Any] = {
            "name": name,
            "label": label_lookup.get(name, ""),
            "type": kind,
            "logical_type": _logical_type(meta, name, series.dtype),
            "n": total_rows - n_missing,
            "n_missing": n_missing,
            "pct_missing": _pct(n_missing, total_rows),
            "n_distinct": int(clean.nunique(dropna=True)),
            "has_value_labels": name in value_labels,
            "missing_details": _missing_details(series, meta, name, missing_policy),
        }
        if stats_mode == "counts":
            item["stats"] = {}
        elif kind == "numeric":
            item["stats"] = _numeric_stats(clean)
        elif kind == "datetime":
            item["stats"] = _datetime_stats(clean)
        else:
            mapping = _norm_labels(value_labels.get(name)) if apply_labels else None
            item["stats"] = _char_stats(clean, mapping)
        summaries.append(item)

    return {
        "path": _rel(path),
        "format": dataset_format(path),
        "total_rows": total_rows,
        "n_columns": len(names),
        "summaries": summaries,
        "offset": start,
        "returned": len(summaries),
        "truncated": len(names) > start + len(summaries),
        "next_offset": start + len(summaries) if len(names) > start + len(summaries) else None,
        "stats_mode": stats_mode,
        "missing_policy": missing_policy,
        "catalog_path": _rel(catalog_path) if catalog_path else None,
    }


def frequency(
    path: Path,
    column: str,
    *,
    limit: int = 50,
    offset: int = 0,
    apply_labels: bool = True,
    include_missing: bool = False,
    catalog_path: Path | None = None,
    missing_policy: str = "sas",
) -> dict[str, Any]:
    """单变量频数表：每个取值的 n 与占比，默认自动套用 SAS 值标签。

    apply_labels 默认 True，因为 coded 值本身不可读；原始 value 一并保留。
    分母随 include_missing 变化：包含缺失时按总行数，否则按非缺失行数，
    结果里回传 denominator 让调用方明确百分比基准。
    """
    df, meta = _read_with_catalog(path, False, catalog_path)
    if column not in df.columns:
        raise DatasetError(f"未知变量: {column}；可用变量见 get_dataset_metadata", "UNKNOWN_VARIABLE")

    total_rows = int(len(df))
    series = df[column]
    mapping = _norm_labels((getattr(meta, "variable_value_labels", None) or {}).get(column))
    mask = _missing_mask(series, meta, column, missing_policy)
    n_missing = int(mask.sum())
    counts = series.loc[~mask].value_counts(dropna=True)
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
        "value_labels_applied": bool(apply_labels and any(item["label"] is not None and item["value"] is not None for item in page)),
        "offset": start,
        "levels": page,
        "returned": len(page),
        "truncated": len(levels) > start + len(page),
        "next_offset": start + len(page) if len(levels) > start + len(page) else None,
        "missing_details": _missing_details(series, meta, column, missing_policy),
        "missing_policy": missing_policy,
        "catalog_path": _rel(catalog_path) if catalog_path else None,
    }
