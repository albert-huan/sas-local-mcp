"""In-memory Client tests against the real TLF workspace datasets."""

import json
import os
from pathlib import Path

import pytest

# tests/ -> sas-local-mcp -> 10Mcp -> TLF workspace root
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("SAS_DATA_ROOT", str(WORKSPACE_ROOT))

from fastmcp import Client  # noqa: E402

from sas_local_mcp.stdio_server import mcp  # noqa: E402

MAX_FILES_TO_SCAN = 40


async def _call(name: str, args: dict):
    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    data = getattr(result, "data", None)
    if data is None:
        data = json.loads(result.content[0].text)
    return data


async def _sas7bdat_files() -> list[str]:
    listing = await _call("list_datasets", {"limit": 2000})
    return [d["path"] for d in listing["datasets"] if d["format"] == "sas7bdat"]


async def _first_sas7bdat() -> str:
    files = await _sas7bdat_files()
    if not files:
        pytest.skip("SAS_DATA_ROOT 下没有 .sas7bdat 文件")
    return files[0]


async def _find_file_with(predicate) -> str:
    """扫描前若干个 sas7bdat，返回第一个满足 predicate(meta, labels) 的文件。"""
    for path in (await _sas7bdat_files())[:MAX_FILES_TO_SCAN]:
        meta = await _call("get_dataset_metadata", {"path": path})
        labels = await _call("get_value_labels", {"path": path})
        if predicate(meta, labels):
            return path
    pytest.skip("前若干个数据集中没有满足条件的文件")


async def test_list_datasets():
    listing = await _call("list_datasets", {})
    assert listing["root"]
    assert listing["count"] > 0
    assert all({"path", "name", "format", "size_bytes"} <= set(d) for d in listing["datasets"])


async def test_get_dataset_metadata():
    path = await _first_sas7bdat()
    meta = await _call("get_dataset_metadata", {"path": path})
    assert meta["path"] == path
    assert meta["number_rows"] > 0
    assert meta["number_columns"] == len(meta["columns"])
    assert all({"name", "label", "type", "informat"} <= set(c) for c in meta["columns"])
    assert all(c["type"] in {"str", "numeric", "datetime"} for c in meta["columns"])


async def test_read_dataset_limit_and_columns():
    path = await _first_sas7bdat()
    meta = await _call("get_dataset_metadata", {"path": path})
    columns = [c["name"] for c in meta["columns"]][:2]

    page = await _call("read_dataset", {"path": path, "limit": 5, "columns": columns})
    assert page["columns"] == columns
    assert len(page["records"]) <= 5
    assert page["truncated"] == (page["rows_after_filter"] > len(page["records"]))
    assert page["records"] == json.loads(json.dumps(page["records"]))  # 可 JSON 序列化


async def test_read_dataset_where():
    path = await _find_file_with(lambda meta, _: any(c["type"] == "numeric" for c in meta["columns"]))
    meta = await _call("get_dataset_metadata", {"path": path})
    numeric = next(c["name"] for c in meta["columns"] if c["type"] == "numeric")

    page = await _call("read_dataset", {"path": path, "where": f"{numeric} >= 0", "limit": 5})
    assert page["rows_after_filter"] <= page["total_rows"]
    assert len(page["records"]) <= 5


async def test_read_dataset_datetime_serialization():
    path = await _find_file_with(lambda meta, _: any(c["type"] == "datetime" for c in meta["columns"]))
    meta = await _call("get_dataset_metadata", {"path": path})
    dt_col = next(c["name"] for c in meta["columns"] if c["type"] == "datetime")

    page = await _call("read_dataset", {"path": path, "columns": [dt_col], "limit": 3})
    values = [r[dt_col] for r in page["records"] if r[dt_col] is not None]
    assert values, f"{dt_col} 应有非空日期值"
    assert all(isinstance(v, str) and len(v) >= 8 for v in values), values[:3]


async def test_apply_value_labels():
    path = await _find_file_with(lambda _, labels: bool(labels["variables"]))
    page = await _call("read_dataset", {"path": path, "limit": 5, "apply_labels": True})
    assert page["value_labels_applied"] is True


async def test_query_dataset_count():
    path = await _first_sas7bdat()
    meta = await _call("get_dataset_metadata", {"path": path})
    result = await _call(
        "query_dataset",
        {"path": path, "sql": "SELECT COUNT(*) AS n FROM dataset"},
    )
    assert result["records"][0]["n"] == meta["number_rows"]


async def test_query_dataset_datetime_column():
    path = await _find_file_with(lambda meta, _: any(c["type"] == "datetime" for c in meta["columns"]))
    meta = await _call("get_dataset_metadata", {"path": path})
    dt_col = next(c["name"] for c in meta["columns"] if c["type"] == "datetime")

    result = await _call(
        "query_dataset",
        {"path": path, "sql": f'SELECT MIN("{dt_col}") AS first_dt FROM dataset'},
    )
    assert result["records"][0]["first_dt"] is not None


async def test_corrupt_file_reports_clean_error():
    path = "04Report/Data/l16_2_10.sas7bdat"
    if not (WORKSPACE_ROOT / path).exists():
        pytest.skip("损坏样本文件不存在")
    with pytest.raises(Exception, match="损坏|截断|不受支持|失败"):
        await _call("get_dataset_metadata", {"path": path})


async def test_path_escape_rejected():
    with pytest.raises(Exception):
        await _call("read_dataset", {"path": "../../../Windows/System32", "limit": 1})
