"""In-memory Client tests against the datasets shipped in tests/.

dm.sas7bdat: 306 rows x 25 cols (Demographics)
lb.sas7bdat: 59580 rows x 23 cols (Laboratory Data) — 用于大文件/缺失值场景
"""

import json
import os
from pathlib import Path

import pandas as pd
import pytest

# 数据根由 conftest.py 解析（兼容 tests/ 根目录直放与 tests/sasdata/sdtm/ 分目录两种布局）
WORKSPACE_ROOT = Path(os.environ.setdefault("SAS_DATA_ROOT", str(Path(__file__).resolve().parent)))

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


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "WITH x AS (SELECT 1) DELETE FROM dataset",
    ],
)
async def test_query_dataset_rejects_multiple_or_mutating_statements(sql):
    with pytest.raises(Exception, match="单条 SELECT"):
        await _call("query_dataset", {"path": "dm.sas7bdat", "sql": sql})


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


# --------------------------------------------------------------------------
# 统计层：summarize_dataset
# --------------------------------------------------------------------------

DM_ROWS = 306
DM_COLS = 25
LB_ROWS = 59580


async def test_summarize_dataset_numeric_and_char():
    result = await _call("summarize_dataset", {"path": "dm.sas7bdat"})
    assert result["total_rows"] == DM_ROWS
    assert result["n_columns"] == DM_COLS

    by_name = {s["name"]: s for s in result["summaries"]}
    age = by_name["AGE"]
    assert age["type"] == "numeric"
    assert age["n"] == DM_ROWS and age["n_missing"] == 0 and age["pct_missing"] == 0.0
    assert age["stats"]["min"] == 50.0
    assert age["stats"]["max"] == 89.0
    assert age["stats"]["median"] == 77.0
    assert abs(age["stats"]["mean"] - 75.09) < 0.01

    sex = by_name["SEX"]
    assert sex["type"] == "str"
    assert sex["n_distinct"] == 2
    assert sex["stats"]["top_values"][0]["n"] == 179
    # dm.sas7bdat 不含任何 SAS 值标签
    assert not any(s["has_value_labels"] for s in result["summaries"])


async def test_summarize_dataset_columns_subset():
    result = await _call("summarize_dataset", {"path": "dm.sas7bdat", "columns": ["AGE", "SEX"]})
    assert result["n_columns"] == 2
    assert [s["name"] for s in result["summaries"]] == ["AGE", "SEX"]
    # 筛选列后变量标签不能错位
    assert result["summaries"][0]["name"] == "AGE"


async def test_summarize_dataset_unknown_column():
    with pytest.raises(Exception, match="未知变量"):
        await _call("summarize_dataset", {"path": "dm.sas7bdat", "columns": ["NOPE"]})


# --------------------------------------------------------------------------
# 统计层：frequency
# --------------------------------------------------------------------------

async def test_frequency_basic():
    result = await _call("frequency", {"path": "dm.sas7bdat", "column": "SEX"})
    assert result["total_rows"] == DM_ROWS
    assert result["n_distinct"] == 2
    assert result["n_missing"] == 0
    assert result["denominator"] == DM_ROWS
    assert result["returned"] == 2
    assert result["truncated"] is False

    levels = result["levels"]
    assert [l["n"] for l in levels] == [179, 127]  # 按 n 降序
    assert levels[0]["value"] == "F"
    assert sum(l["n"] for l in levels) == DM_ROWS
    assert abs(sum(l["pct"] for l in levels) - 100) < 0.05


async def test_frequency_cardinality_not_capped_at_preview_limit():
    """核心回归：USUBJID 有 306 个取值，超过行预览上限 200 但低于聚合上限 1000。

    旧实现所有 limit 都被 MAX_ROWS(200) 夹住，模型会读到不完整的分布。
    """
    result = await _call("frequency", {"path": "dm.sas7bdat", "column": "USUBJID", "limit": 500})
    assert result["n_distinct"] == 306
    assert result["returned"] == 306
    assert result["truncated"] is False


async def test_frequency_limit_clamped_by_aggregate_cap():
    from sas_local_mcp.config import MAX_AGG_ROWS

    result = await _call("frequency", {"path": "dm.sas7bdat", "column": "USUBJID", "limit": 10**9})
    assert result["returned"] == min(306, MAX_AGG_ROWS)


async def test_frequency_paging():
    first = await _call("frequency", {"path": "dm.sas7bdat", "column": "USUBJID", "limit": 10})
    assert first["offset"] == 0
    assert first["returned"] == 10
    assert first["truncated"] is True

    tail = await _call("frequency", {"path": "dm.sas7bdat", "column": "USUBJID", "limit": 10, "offset": 300})
    assert tail["offset"] == 300
    assert tail["returned"] == 6
    assert tail["truncated"] is False


async def test_frequency_unknown_column():
    with pytest.raises(Exception, match="未知变量"):
        await _call("frequency", {"path": "dm.sas7bdat", "column": "NOPE"})


async def test_frequency_include_missing_changes_denominator():
    """lb 的 LBSTRESN 有 880 个缺失；分母随 include_missing 变化。"""
    without = await _call("frequency", {"path": "lb.sas7bdat", "column": "LBSTRESN", "limit": 50})
    assert without["n_missing"] == 880
    assert without["denominator"] == LB_ROWS - 880
    assert without["include_missing"] is False

    with_missing = await _call(
        "frequency", {"path": "lb.sas7bdat", "column": "LBSTRESN", "limit": 50, "include_missing": True}
    )
    assert with_missing["include_missing"] is True
    assert with_missing["denominator"] == LB_ROWS


async def test_frequency_value_labels_and_missing_bucket(monkeypatch):
    """真实测试数据无值标签，注入 meta 覆盖解码与缺失分档这两条分支。"""
    import types

    from sas_local_mcp import dataset as ds

    frame = pd.DataFrame({"ARMN": [1.0, 2.0, 2.0, 3.0], "SEX": ["M", "F", "F", None]})
    meta = types.SimpleNamespace(
        readstat_variable_types={"ARMN": "double", "SEX": "string"},
        original_variable_informats={},
        column_labels=["治疗组", "性别"],
        variable_value_labels={"ARMN": {1.0: "Placebo", 2.0: "Low", 3.0: "High"}},
    )
    monkeypatch.setattr(ds, "SAS_DATA_ROOT", WORKSPACE_ROOT)
    monkeypatch.setattr(ds, "_read", lambda _path, metadata_only=False: (frame, meta))

    labelled = await _call("frequency", {"path": "dm.sas7bdat", "column": "ARMN"})
    assert labelled["value_labels_applied"] is True
    assert labelled["column_label"] == "治疗组"
    by_value = {l["value"]: l for l in labelled["levels"]}
    assert by_value[1.0]["label"] == "Placebo"
    assert by_value[2.0]["label"] == "Low" and by_value[2.0]["n"] == 2
    assert by_value[3.0]["label"] == "High"

    with_bucket = await _call("frequency", {"path": "dm.sas7bdat", "column": "SEX", "include_missing": True})
    assert with_bucket["n_missing"] == 1
    assert with_bucket["denominator"] == 4
    bucket = [l for l in with_bucket["levels"] if l["value"] is None]
    assert bucket and bucket[0]["n"] == 1 and bucket[0]["label"] == "<missing>"


# --------------------------------------------------------------------------
# 分页：read_dataset / query_dataset 的 offset
# --------------------------------------------------------------------------

async def test_read_dataset_offset():
    page0 = await _call("read_dataset", {"path": "dm.sas7bdat", "limit": 5, "offset": 0})
    page1 = await _call("read_dataset", {"path": "dm.sas7bdat", "limit": 5, "offset": 5})
    assert page0["offset"] == 0 and page1["offset"] == 5
    assert len(page0["records"]) == len(page1["records"]) == 5
    assert page0["records"] != page1["records"]  # 确实翻页了
    assert page0["truncated"] is True

    tail = await _call("read_dataset", {"path": "dm.sas7bdat", "limit": 10, "offset": 300})
    assert tail["returned"] == 6
    assert tail["truncated"] is False


async def test_read_dataset_offset_beyond_end():
    page = await _call("read_dataset", {"path": "dm.sas7bdat", "limit": 10, "offset": 9999})
    assert page["records"] == []
    assert page["returned"] == 0
    assert page["truncated"] is False


async def test_query_dataset_offset():
    result = await _call(
        "query_dataset",
        {
            "path": "dm.sas7bdat",
            "sql": "SELECT USUBJID FROM dataset ORDER BY USUBJID",
            "limit": 10,
            "offset": 300,
        },
    )
    assert result["total_rows"] == DM_ROWS
    assert result["offset"] == 300
    assert result["returned"] == 6
    assert result["truncated"] is False


# --------------------------------------------------------------------------
# 多表 JOIN 与 SQL 结果解码
# --------------------------------------------------------------------------

async def test_query_dataset_extra_tables():
    result = await _call(
        "query_dataset",
        {
            "path": "dm.sas7bdat",
            "extra_paths": ["lb.sas7bdat"],
            "sql": "SELECT COUNT(DISTINCT USUBJID) AS n_subj FROM lb",
            "limit": 10,
        },
    )
    # 主表 dataset，附加表按文件名主干命名
    assert result["tables"]["dataset"] == "dm.sas7bdat"
    assert result["tables"]["lb"] == "lb.sas7bdat"
    assert result["records"][0]["n_subj"] == 254


async def test_query_dataset_join_across_datasets():
    result = await _call(
        "query_dataset",
        {
            "path": "dm.sas7bdat",
            "extra_paths": ["lb.sas7bdat"],
            "sql": "SELECT COUNT(*) AS n FROM dataset d INNER JOIN lb l USING (USUBJID)",
            "limit": 10,
        },
    )
    assert result["records"][0]["n"] > 0


async def test_query_dataset_apply_labels(monkeypatch):
    """SQL 结果解码 coded 值（真实测试数据无值标签，注入 meta 覆盖）。"""
    import types

    from sas_local_mcp import dataset as ds

    frame = pd.DataFrame({"ARMN": [1.0, 2.0]})
    meta = types.SimpleNamespace(
        readstat_variable_types={"ARMN": "double"},
        original_variable_informats={},
        original_variable_formats={},
        column_labels=["治疗组"],
        variable_value_labels={"ARMN": {1.0: "Placebo", 2.0: "Low"}},
    )
    monkeypatch.setattr(ds, "SAS_DATA_ROOT", WORKSPACE_ROOT)
    monkeypatch.setattr(ds, "_read", lambda _path, metadata_only=False: (frame, meta))

    result = await _call(
        "query_dataset",
        {"path": "dm.sas7bdat", "sql": "SELECT ARMN FROM dataset", "apply_labels": True},
    )
    assert result["value_labels_applied"] is True
    assert [r["ARMN"] for r in result["records"]] == ["Placebo", "Low"]


# --------------------------------------------------------------------------
# 跨数据集变量搜索
# --------------------------------------------------------------------------

async def test_search_variables_exact():
    result = await _call("search_variables", {"variable": "USUBJID"})
    assert result["variable"] == "USUBJID"
    assert result["contains"] is False
    matched = {m["dataset"] for m in result["matches"]}
    # 数据目录下数据集个数随布局变化，只保证两个已知域都被命中、且命中的就是 USUBJID 本身
    assert {"dm.sas7bdat", "lb.sas7bdat"} <= matched
    assert result["datasets_matched"] == len(matched)
    assert all(m["variable"] == "USUBJID" for m in result["matches"])
    assert result["errors"] == []


async def test_search_variables_reports_scan_truncation():
    result = await _call("search_variables", {"variable": "USUBJID", "max_datasets": 1})
    assert result["datasets_scanned"] == 1
    assert result["scan_truncated"] is True
    assert result["truncated"] is True


async def test_search_variables_contains():
    result = await _call("search_variables", {"variable": "AGE", "contains": True})
    names = {m["variable"] for m in result["matches"]}
    assert {"AGE", "AGEU"} <= names
    assert "USUBJID" not in names


async def test_search_variables_pattern_filter():
    result = await _call("search_variables", {"variable": "USUBJID", "pattern": "dm"})
    assert result["datasets_scanned"] > 0
    matched = {m["dataset"] for m in result["matches"]}
    # pattern 是文件名子串过滤：被扫到的数据集文件名都必须含 dm（如 suppdm.sas7bdat）
    assert "dm.sas7bdat" in matched
    assert all("dm" in d for d in matched)


async def test_search_variables_not_found():
    result = await _call("search_variables", {"variable": "NOPE"})
    assert result["matches"] == []
    assert result["datasets_matched"] == 0
    assert result["truncated"] is False


# --------------------------------------------------------------------------
# 元数据 format / 读取缓存
# --------------------------------------------------------------------------

async def test_metadata_includes_format():
    meta = await _call("get_dataset_metadata", {"path": "dm.sas7bdat"})
    assert all({"name", "label", "type", "format", "informat"} <= set(c) for c in meta["columns"])


async def test_read_cache_reuses_parsed_dataset(monkeypatch):
    """同一文件重复读取应命中缓存，不再重新解析。"""
    from sas_local_mcp import dataset as ds

    calls: list[str] = []
    original = ds._read_uncached

    def counting(path, metadata_only):
        calls.append(str(path))
        return original(path, metadata_only)

    monkeypatch.setattr(ds, "_read_uncached", counting)
    ds._CACHE.clear()

    await _call("get_dataset_metadata", {"path": "dm.sas7bdat"})
    after_first = len(calls)
    assert after_first > 0
    await _call("get_dataset_metadata", {"path": "dm.sas7bdat"})
    assert len(calls) == after_first, "第二次读取应命中缓存"
