"""随机抽样冒烟测试。

从 tests/ 的每个类别目录（sasdata / sasdata_cn / xpt）随机抽取文件，跑一整套
只读工具，用随机化的 limit/offset/列子集/频段组合压一遍。

抽样约束：
  * 每个类别目录至少抽 2 个文件；
  * 每类必须同时抽到“小”与“大”——小来自按体积排序后的前 1/3，大来自后 1/3，
    这样大文件压实分页/截断/口径上限，小文件压实全量读取路径。

环境变量：
  SAS_LOCAL_MCP_RANDOM_SEED   固定随机种子复现某次抽样（不设则每次随机）
  SAS_LOCAL_MCP_RANDOM_EXTRA  每类在“大/小”之外额外抽取的文件数（默认 1）
"""

import json
import math
import os
import random
import secrets
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("SAS_DATA_ROOT", str(WORKSPACE_ROOT))

from fastmcp import Client  # noqa: E402

from sas_local_mcp import dataset as ds  # noqa: E402
from sas_local_mcp.config import MAX_AGG_ROWS, MAX_ROWS  # noqa: E402
from sas_local_mcp.stdio_server import mcp  # noqa: E402

SEED_VALUE = int(os.getenv("SAS_LOCAL_MCP_RANDOM_SEED") or secrets.randbelow(2**32))
_RNG = random.Random(SEED_VALUE)
EXTRA_PER_CATEGORY = max(0, int(os.getenv("SAS_LOCAL_MCP_RANDOM_EXTRA", "1")))
DATA_SUFFIXES = tuple(ds.READABLE_SUFFIXES)  # (.sas7bdat, .xpt)
_SKIP_DIRS = {"__pycache__", ".ipynb_checkpoints"}


@dataclass(frozen=True)
class Case:
    category: str  # 顶层类别目录：sasdata / sasdata_cn / xpt
    path: str  # 相对 SAS_DATA_ROOT
    size_bytes: int
    size_class: str  # small / large / extra


def _files_by_category() -> dict[str, list[tuple[Path, int]]]:
    """按顶层目录归类的 (绝对路径, 字节数)，每组按体积升序。"""
    buckets: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(WORKSPACE_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if not name.lower().endswith(DATA_SUFFIXES):
                continue
            full = Path(dirpath) / name
            category = full.relative_to(WORKSPACE_ROOT).as_posix().split("/", 1)[0]
            buckets[category].append((full, full.stat().st_size))
    for items in buckets.values():
        items.sort(key=lambda item: item[1])
    return buckets


def _pick(pool: list[tuple[Path, int]], used: set[Path]) -> tuple[Path, int]:
    fresh = [item for item in pool if item[0] not in used] or pool
    return _RNG.choice(fresh)


def _build_plan() -> tuple[list[Case], dict[str, tuple[int, int]]]:
    cases: list[Case] = []
    bounds: dict[str, tuple[int, int]] = {}
    for category, items in sorted(_files_by_category().items()):
        third = max(1, len(items) // 3)
        small_pool, large_pool = items[:third], items[-third:]
        small_max = max(size for _, size in small_pool)
        large_min = min(size for _, size in large_pool)
        bounds[category] = (small_max, large_min)

        used: set[Path] = set()
        chosen: list[tuple[tuple[Path, int], str]] = []
        for pool, size_class in ((small_pool, "small"), (large_pool, "large")):
            item = _pick(pool, used)
            used.add(item[0])
            chosen.append((item, size_class))

        rest = [item for item in items if item[0] not in used]
        for item in _RNG.sample(rest, min(EXTRA_PER_CATEGORY, len(rest))):
            used.add(item[0])
            chosen.append((item, "extra"))

        for (full, size), size_class in chosen:
            cases.append(
                Case(category, full.relative_to(WORKSPACE_ROOT).as_posix(), size, size_class)
            )
    return cases, bounds


CASES, SIZE_BOUNDS = _build_plan()
CASE_IDS = [f"{c.category}/{c.size_class}/{Path(c.path).name}" for c in CASES]

print(f"[test_random_smoke] seed={SEED_VALUE} picks=" + ", ".join(CASE_IDS))


def _seed_hint() -> str:
    return f"（本次抽样 seed={SEED_VALUE}，可用 SAS_LOCAL_MCP_RANDOM_SEED 复现）"


def _assert_jsonable(records: list[dict]) -> None:
    """records 必须能进 JSON：日期已是字符串、缺失已是 None、没有裸 NaN。"""
    json.loads(json.dumps(records))
    for record in records:
        for name, value in record.items():
            assert not (isinstance(value, float) and math.isnan(value)), f"{name} 含未转换的 NaN"


async def _call(name: str, args: dict):
    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    data = getattr(result, "data", None)
    if data is None:
        data = json.loads(result.content[0].text)
    return data


@pytest.fixture(autouse=True)
def _random_session(monkeypatch):
    """每个用例：数据根临时切到 tests/（覆盖 conftest 给的锚点），结束后清空读取缓存。

    走完整 tests/ 才能同时看到 sasdata / sasdata_cn / xpt 三类目录；
    monkeypatch 保证用例结束后 SAS_DATA_ROOT 自动还原，不影响其它测试模块。
    清缓存是为了避免大文件 DataFrame 长期占内存。
    """
    monkeypatch.setattr(ds, "SAS_DATA_ROOT", WORKSPACE_ROOT)
    yield
    ds._CACHE.clear()


def test_sampling_covers_every_category_with_small_and_large():
    """抽样自身的约束：每类 >=2 个文件，且必须同时有“大”和“小”。"""
    assert CASES, f"tests/ 下没找到 {DATA_SUFFIXES} 文件"

    picked: dict[str, list[Case]] = defaultdict(list)
    for case in CASES:
        picked[case.category].append(case)

    all_categories = sorted(_files_by_category())
    assert all_categories, "没发现任何数据目录"
    for category in all_categories:
        picks = picked.get(category, [])
        assert len(picks) >= 2, f"{category} 至少要抽 2 个文件，实际 {len(picks)} {_seed_hint()}"
        classes = {p.size_class for p in picks}
        assert {"small", "large"} <= classes, f"{category} 未同时覆盖大/小文件 {_seed_hint()}"

        small_max, large_min = SIZE_BOUNDS[category]
        smalls = [p.size_bytes for p in picks if p.size_class == "small"]
        larges = [p.size_bytes for p in picks if p.size_class == "large"]
        assert min(smalls) <= small_max, f"{category} 的“小文件”不在最小 1/3 内"
        assert max(larges) >= large_min, f"{category} 的“大文件”不在最大 1/3 内"
        assert max(p.size_bytes for p in picks) > min(
            p.size_bytes for p in picks
        ), f"{category} 抽样体积没有梯度 {_seed_hint()}"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_random_file_smoke(case: Case):
    """对随机抽到的文件跑一遍全工具链，只校验与文件内容无关的不变量。"""
    # 1) 元数据
    meta = await _call("get_dataset_metadata", {"path": case.path})
    assert meta["path"] == case.path
    assert meta["format"] == ("sas7bdat" if case.path.endswith(".sas7bdat") else "xport")
    rows, columns = meta["number_rows"], meta["columns"]
    assert len(columns) == meta["number_columns"] > 0
    for col in columns:
        assert {"name", "label", "type", "format", "informat"} <= set(col)
        assert col["type"] in {"str", "numeric", "datetime"}
    assert len({c["name"] for c in columns}) == len(columns), "变量名不能重复"

    names = [c["name"] for c in columns]

    # 2) 随机分页读取：随机 limit + 随机 offset（含数据中部与尾部）
    limit = _RNG.choice([1, 3, 20, MAX_ROWS])
    offset = _RNG.choice([0, max(0, rows // 2), rows])
    page = await _call("read_dataset", {"path": case.path, "limit": limit, "offset": offset})
    assert page["total_rows"] == rows
    assert page["rows_after_filter"] == rows
    assert page["offset"] == offset
    assert page["returned"] == len(page["records"]) <= min(limit, MAX_ROWS)
    assert page["truncated"] == (rows > offset + page["returned"])
    _assert_jsonable(page["records"])

    # 越界 offset：不报错，返回空页
    beyond = await _call(
        "read_dataset", {"path": case.path, "limit": 5, "offset": rows + 10**6}
    )
    assert beyond["records"] == [] and beyond["returned"] == 0
    assert beyond["truncated"] is False

    # 3) 随机列子集
    subset = _RNG.sample(names, min(3, len(names)))
    narrowed = await _call(
        "read_dataset", {"path": case.path, "limit": 2, "columns": subset}
    )
    assert narrowed["columns"] == subset
    _assert_jsonable(narrowed["records"])

    # 4) 逐变量概要
    summary = await _call(
        "summarize_dataset", {"path": case.path, "apply_labels": bool(_RNG.getrandbits(1))}
    )
    assert summary["total_rows"] == rows
    assert summary["n_columns"] == len(names)
    assert [s["name"] for s in summary["summaries"]] == names
    by_name = {s["name"]: s for s in summary["summaries"]}
    for item in summary["summaries"]:
        assert item["n"] + item["n_missing"] == rows
        assert 0 <= item["n_distinct"] <= item["n"]
        expected_pct = round(item["n_missing"] / rows * 100, 2) if rows else None
        assert item["pct_missing"] == expected_pct
        assert isinstance(item["stats"], dict)

    # 5) 频数：优先挑取值数 >1 且基数最小的变量，能同时验证多档与分页
    non_empty = [s for s in summary["summaries"] if s["n"] > 0]
    if non_empty:
        multi = [s for s in non_empty if s["n_distinct"] > 1]
        target = (min(multi, key=lambda s: s["n_distinct"]) if multi else non_empty[0])["name"]

        freq = await _call(
            "frequency", {"path": case.path, "column": target, "limit": MAX_AGG_ROWS}
        )
        assert freq["total_rows"] == rows
        assert freq["n_distinct"] == by_name[target]["n_distinct"]
        assert freq["n_missing"] + sum(l["n"] for l in freq["levels"]) == rows
        assert freq["denominator"] == rows - freq["n_missing"]
        counts = [l["n"] for l in freq["levels"]]
        assert counts == sorted(counts, reverse=True), "levels 必须按 n 降序"
        assert freq["returned"] == len(freq["levels"]) <= MAX_AGG_ROWS
        assert freq["truncated"] == (freq["n_distinct"] > freq["returned"])

        with_missing = await _call(
            "frequency",
            {
                "path": case.path,
                "column": target,
                "limit": MAX_AGG_ROWS,
                "include_missing": True,
            },
        )
        assert with_missing["denominator"] == rows
        bucket = [l for l in with_missing["levels"] if l["value"] is None]
        assert len(bucket) == (1 if with_missing["n_missing"] else 0)

    # 6) SQL：COUNT(*) 必须等于元数据行数
    counted = await _call(
        "query_dataset", {"path": case.path, "sql": "SELECT COUNT(*) AS n FROM dataset"}
    )
    assert counted["records"][0]["n"] == rows
    assert counted["truncated"] is False

    # 7) 日期列必须序列化成字符串，且顺序遍历不炸
    dt_cols = [c["name"] for c in columns if c["type"] == "datetime"]
    if dt_cols:
        dt_page = await _call(
            "read_dataset", {"path": case.path, "limit": 3, "columns": dt_cols[:1]}
        )
        values = [r[dt_cols[0]] for r in dt_page["records"] if r[dt_cols[0]] is not None]
        assert all(isinstance(v, str) for v in values), values[:3]

    # 8) 未知变量必须给出明确错误，而不是静默返回空
    with pytest.raises(Exception, match="未知变量"):
        await _call("read_dataset", {"path": case.path, "columns": ["__NOPE__"]})
