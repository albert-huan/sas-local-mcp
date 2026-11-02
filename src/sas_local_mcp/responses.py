"""Published tool output schemas and an exact UTF-8 JSON payload budget."""

import json
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .config import MAX_RESPONSE_BYTES
from .errors import DatasetError


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    response_bytes: int = 0
    response_truncated: bool = False


class DatasetEntry(BaseModel):
    path: str
    name: str
    format: str
    size_bytes: int
    modified: str


class Listing(Response):
    root: str
    count: int
    datasets: list[DatasetEntry]
    offset: int
    returned: int
    truncated: bool
    next_offset: int | None


class Column(BaseModel):
    name: str
    label: str
    type: Literal["str", "numeric", "datetime"]
    logical_type: Literal["string", "numeric", "date", "time", "datetime"]
    format: str
    informat: str
    storage_width: int | None
    sas_unit: str | None


class Metadata(Response):
    path: str
    format: str
    table_name: str | None
    number_rows: int | None
    row_count_exact: bool
    number_columns: int
    file_label: str
    file_encoding: str | None
    created: str | None
    modified: str | None
    user_missing: dict[str, list[Any]]
    columns: list[Column]
    catalog_path: str | None
    column_offset: int
    returned_columns: int
    truncated: bool
    next_column_offset: int | None


class Rows(Response):
    path: str
    format: str
    total_rows: int | None
    rows_after_filter: int | None
    row_count_exact: bool
    offset: int
    columns: list[str]
    records: list[dict[str, Any]]
    returned: int
    truncated: bool
    next_offset: int | None
    value_labels_applied: bool
    decoded_columns: list[str]
    missing_policy: Literal["sas", "pandas"]
    catalog_path: str | None


class VariableSummary(BaseModel):
    name: str
    label: str
    type: str
    logical_type: str
    n: int
    n_missing: int
    pct_missing: float | None
    n_distinct: int
    has_value_labels: bool
    missing_details: dict[str, int]
    stats: dict[str, Any]


class Summary(Response):
    path: str
    format: str
    total_rows: int
    n_columns: int
    summaries: list[VariableSummary]
    offset: int
    returned: int
    truncated: bool
    next_offset: int | None
    stats_mode: Literal["full", "counts"]
    missing_policy: Literal["sas", "pandas"]
    catalog_path: str | None


class Level(BaseModel):
    value: Any
    label: str | None
    n: int
    pct: float | None


class Frequencies(Response):
    path: str
    format: str
    column: str
    column_label: str
    type: str
    total_rows: int
    n_missing: int
    n_distinct: int
    denominator: int
    include_missing: bool
    value_labels_applied: bool
    offset: int
    levels: list[Level]
    returned: int
    truncated: bool
    next_offset: int | None
    missing_details: dict[str, int]
    missing_policy: Literal["sas", "pandas"]
    catalog_path: str | None


class ValueLabels(Response):
    path: str
    format: str
    variables: dict[str, dict[str, str]]
    catalog_path: str | None


class LabelSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str = Field(description="注册表名，如 dataset 或 lb")
    column: str = Field(description="该表中的原始变量名")


class Decoding(BaseModel):
    source_table: str | None
    source_column: str | None
    status: Literal["not_requested", "source_unknown", "no_labels", "no_matching_values", "decoded"]
    values_decoded: int


class Query(Response):
    path: str
    sql: str
    tables: dict[str, str]
    total_rows: int | None
    row_count_exact: bool
    offset: int
    columns: list[str]
    records: list[dict[str, Any]]
    returned: int
    truncated: bool
    next_offset: int | None
    value_labels_applied: bool
    label_decoding: dict[str, Decoding]
    order_by_present: bool
    warnings: list[str]
    missing_policy: Literal["sas", "pandas"]
    special_missing_values: dict[str, dict[str, list[str]]]


class Match(BaseModel):
    path: str
    dataset: str
    variable: str
    label: str
    type: str
    number_rows: int | None


class SearchError(BaseModel):
    path: str
    error: str


class Search(Response):
    root: str
    variable: str
    contains: bool
    datasets_scanned: int
    datasets_matched: int
    matches: list[Match]
    returned: int
    offset: int
    scan_offset: int
    next_offset: int | None
    next_scan_offset: int | None
    scan_truncated: bool
    truncated: bool
    errors: list[SearchError]


R = TypeVar("R", bound=Response)


def _measure(payload: dict) -> int:
    # Account for the size field itself as well as non-ASCII text and JSON escaping.
    for _ in range(5):
        size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if payload["response_bytes"] == size:
            return size
        payload["response_bytes"] = size
    return size


def respond(model: type[R], payload: dict) -> R:
    decoding_masks = getattr(payload, "decoding_masks", {})
    payload = model.model_validate(payload).model_dump(mode="json")
    if _measure(payload) <= MAX_RESPONSE_BYTES:
        return model.model_validate(payload)
    collection = next((name for name in ("records", "datasets", "matches", "levels", "summaries", "columns")
                       if name in payload and isinstance(payload[name], list)), None)
    if collection is None or not payload[collection]:
        raise DatasetError("响应超过字节上限，请缩小 columns、查询输出、值标签范围或 max_datasets", "RESPONSE_TOO_LARGE")
    original = payload[collection]
    payload["response_truncated"] = True
    payload["truncated"] = True
    offset_key, next_key, returned_key = ("column_offset", "next_column_offset", "returned_columns") if model is Metadata else ("offset", "next_offset", "returned")

    def keep(count):
        payload[collection] = original[:count]
        payload[returned_key] = count
        payload[next_key] = payload.get(offset_key, 0) + count
        if model is Query:
            for col, info in payload["label_decoding"].items():
                if col in decoding_masks:
                    info["values_decoded"] = sum(decoding_masks[col][:count])
                    info["status"] = "decoded" if info["values_decoded"] else "no_matching_values"
            payload["value_labels_applied"] = any(info["values_decoded"] for info in payload["label_decoding"].values())
        elif model is Rows:
            payload["decoded_columns"] = [col for col, mask in decoding_masks.items() if any(mask[:count])]
            payload["value_labels_applied"] = bool(payload["decoded_columns"])
        elif model is Frequencies:
            payload["value_labels_applied"] = any(item["label"] is not None and item["value"] is not None
                                                  for item in payload["levels"])
        return _measure(payload)

    low, high = 0, len(original)
    while low < high:
        middle = (low + high + 1) // 2
        if keep(middle) <= MAX_RESPONSE_BYTES:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        raise DatasetError("单条结果或响应元数据超过字节上限，请缩小 columns 或查询输出", "RESPONSE_TOO_LARGE")
    keep(low)
    return model.model_validate(payload)
