"""随机冒烟测试：以 stdio 子进程拉起真实的 MCP 服务，对 tests/ 下各类别数据随机抽检。

与 `test_tools.py`（in-memory Client）不同，这里走的是**完整 MCP 链路**：
客户端 -> 子进程（.venv/Scripts/sas-local-mcp.exe）-> stdio 协议 -> 工具实现，
因此能顺带验证「安装后的入口命令是否可用」以及 SAS_DATA_ROOT 是否生效。

抽检规则：
- 自动发现 tests/ 下所有含数据文件的类别目录（sasdata/sdtm、sasdata/adam、
  sasdata_cn/sdtm、sasdata_cn/adam、xpt/sdtm、xpt/adam ...）
- 每个类别抽 `--per-category` 个文件，并**强制包含该类别中最小与最大的文件**，
  保证同时覆盖小文件与大文件（几十 MB）的读取路径
- 每个被抽中的文件依次跑全部 8 个工具，参数（limit / offset / columns / where /
  apply_labels / include_missing / SQL 模板）随机生成

用法：
    python tests/random_smoke.py                       # 随机种子
    python tests/random_smoke.py --seed 20261020       # 可复现
    python tests/random_smoke.py --per-category 3      # 每类抽几个（含最小/最大）
    python tests/random_smoke.py --out README.md       # 报告写回 README 标记之间
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent

START_MARKER = "<!-- RANDOM-SMOKE-START -->"
END_MARKER = "<!-- RANDOM-SMOKE-END -->"

SUFFIXES = {".sas7bdat", ".xpt"}


# --------------------------------------------------------------------------
# 结果收集
# --------------------------------------------------------------------------


@dataclass
class Case:
    tool: str
    ok: bool
    detail: str
    ms: float


@dataclass
class FileReport:
    category: str
    rel_path: str
    size_bytes: int
    rows: int | None = None
    cols: int | None = None
    has_usubjid: bool = False
    cases: list[Case] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.ok)

    @property
    def failed(self) -> int:
        return len(self.cases) - self.passed

    @property
    def ms(self) -> float:
        return sum(c.ms for c in self.cases)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.0f} B" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


# --------------------------------------------------------------------------
# 数据发现与抽样
# --------------------------------------------------------------------------


def discover_categories(root: Path) -> dict[str, list[Path]]:
    """类别目录 -> 该目录下的数据文件（按大小升序）。"""
    buckets: dict[str, list[Path]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue  # 隐藏目录（.pytest_cache 等）不参与
        buckets.setdefault(str(rel.parent).replace("\\", "/"), []).append(path)
    for files in buckets.values():
        files.sort(key=lambda p: p.stat().st_size)
    return dict(sorted(buckets.items()))


def pick_samples(files: list[Path], rng: random.Random, per_category: int) -> list[Path]:
    """每类抽 per_category 个：最小 + 最大 + 随机补齐（保证「有大有小」）。"""
    if len(files) <= per_category:
        return list(files)
    chosen = [files[0], files[-1]]
    middle = files[1:-1]
    chosen.extend(rng.sample(middle, k=min(per_category - 2, len(middle))))
    return chosen


# --------------------------------------------------------------------------
# 工具调用
# --------------------------------------------------------------------------


async def _call(client, name: str, args: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    result = await client.call_tool(name, args)
    elapsed = (time.perf_counter() - started) * 1000
    data = getattr(result, "data", None)
    if data is None:
        data = json.loads(result.content[0].text)
    return data, elapsed


def _rel(path: Path) -> str:
    return str(path.relative_to(TESTS_ROOT)).replace("\\", "/")


async def run_battery(client, category: str, path: Path, rng: random.Random) -> FileReport:
    rel_path = _rel(path)
    report = FileReport(category=category, rel_path=rel_path, size_bytes=path.stat().st_size)

    async def attempt(tool: str, args: dict, check) -> dict | None:
        try:
            data, ms = await _call(client, tool, args)
            ok, detail = check(data)
            report.cases.append(Case(tool, ok, detail, ms))
            return data if ok else None
        except Exception as exc:  # ToolError / 解析异常都记为失败
            report.cases.append(Case(tool, False, f"{type(exc).__name__}: {exc}"[:160], 0.0))
            return None

    # 1) list_datasets：文件名前 3 个字符做子串过滤
    stem = path.stem[:3]
    await attempt(
        "list_datasets",
        {"pattern": stem, "limit": rng.randint(1, 50)},
        lambda d: (
            d["count"] > 0 and all(stem.lower() in x["name"].lower() for x in d["datasets"]),
            f"pattern='{stem}' 命中 {d['count']} 个",
        ),
    )

    # 2) get_dataset_metadata
    meta = await attempt(
        "get_dataset_metadata",
        {"path": rel_path},
        lambda d: (
            d["number_columns"] == len(d["columns"]),
            f"{d['number_rows']} 行 × {d['number_columns']} 列",
        ),
    )
    if meta is None:
        return report  # 元数据都读不出来，后续用例无从谈起

    report.rows, report.cols = meta["number_rows"], meta["number_columns"]
    columns = [c["name"] for c in meta["columns"]]
    report.has_usubjid = "USUBJID" in columns
    numeric = [c["name"] for c in meta["columns"] if c["type"] == "numeric"]

    # 3) summarize_dataset：随机全量或列子集
    subset = rng.sample(columns, k=rng.randint(2, min(6, len(columns)))) if len(columns) > 6 and rng.random() < 0.5 else None
    summary = await attempt(
        "summarize_dataset",
        {"path": rel_path, "columns": subset},
        lambda d: (
            d["n_columns"] == (len(subset) if subset else len(columns))
            and all({"name", "type", "n_missing", "pct_missing", "n_distinct"} <= set(s) for s in d["summaries"]),
            f"{d['n_columns']} 变量概要" + ("（列子集）" if subset else "（全量）"),
        ),
    )

    # 4) get_value_labels
    labels = await attempt(
        "get_value_labels",
        {"path": rel_path},
        lambda d: (
            isinstance(d.get("variables"), dict),
            f"{len(d['variables'])} 变量带值标签" if d["variables"] else "无值标签",
        ),
    )

    # 5) frequency：挑取值数最少但 >=2 的列，随机 include_missing
    if columns:
        summaries = (summary or {}).get("summaries") or [{"name": columns[0], "n_distinct": 0}]
        pick = min((s for s in summaries if (s["n_distinct"] or 0) >= 2), key=lambda s: s["n_distinct"], default=summaries[0])
        freq_col = pick["name"]
        include_missing = rng.random() < 0.5

        def check_freq(d):
            ns = [l["n"] for l in d["levels"]]
            return (
                (not ns) or ns == sorted(ns, reverse=True),
                f"{freq_col}: {d['n_distinct']} 取值 / 返回 {d['returned']} / 分母 {d['denominator']} / include_missing={include_missing}",
            )

        await attempt(
            "frequency",
            {"path": rel_path, "column": freq_col, "limit": rng.choice([5, 10, 50]), "include_missing": include_missing},
            check_freq,
        )

    # 6) read_dataset：随机 limit / offset / 列子集 / where / apply_labels
    read_args: dict = {"path": rel_path, "limit": rng.choice([1, 3, 10, 25])}
    if report.rows:
        read_args["offset"] = rng.randint(0, max(0, report.rows - 1))
    selected = columns
    if len(columns) > 3 and rng.random() < 0.6:
        k = min(len(columns), rng.randint(2, 6))
        selected = rng.sample(columns, k=k)
        read_args["columns"] = selected
    # where 是在列投影之后用 df.query 求值的，只能引用被选中的列
    if numeric_selected := [c for c in numeric if c in selected]:
        if rng.random() < 0.4:
            read_args["where"] = f"{rng.choice(numeric_selected)} >= 0"
    if (labels and labels["variables"]) or rng.random() < 0.3:
        read_args["apply_labels"] = True

    def check_read(d):
        return (
            len(d["records"]) <= read_args["limit"] and d["records"] == json.loads(json.dumps(d["records"])),
            f"返回 {d['returned']} 行 / 过滤后 {d['rows_after_filter']} 行 / offset={d['offset']}",
        )

    await attempt("read_dataset", read_args, check_read)

    # 7) query_dataset：随机 SQL 模板
    template = rng.choice(["count", "group_by", "projection"])
    if template == "group_by" and columns:
        col = columns[0]
        sql = f'SELECT "{col}" AS col, COUNT(*) AS n FROM dataset GROUP BY 1 ORDER BY n DESC'
    elif template == "projection" and columns:
        sql = f'SELECT "{columns[0]}" AS col FROM dataset'
    else:
        template = "count"
        sql = "SELECT COUNT(*) AS n FROM dataset"

    def check_query(d):
        if template == "count":
            got = d["records"][0]["n"]
            return (got == report.rows, f"COUNT(*)={got}（元数据 {report.rows} 行）")
        return (len(d["records"]) > 0, f"{len(d['records'])} 行结果（{template}）")

    await attempt(
        "query_dataset",
        {"path": rel_path, "sql": sql, "limit": rng.choice([5, 20, 100])},
        check_query,
    )

    # 8) search_variables：拿该文件里随机一个变量去全库搜
    if columns:
        probe = rng.choice(columns)
        contains = rng.random() < 0.5

        def check_search(d):
            return (
                d["datasets_matched"] > 0 and all(probe.lower() in m["variable"].lower() for m in d["matches"]),
                f"{probe}: 命中 {d['datasets_matched']} 个数据集 / contains={contains}",
            )

        await attempt(
            "search_variables",
            {"variable": probe, "contains": contains, "limit": 20},
            check_search,
        )

    return report


async def run_join_case(client, left_rel: str, right_rel: str) -> Case:
    """跨数据集 JOIN：同一类别下两个含 USUBJID 的文件，验证 extra_paths 多表注册。"""
    # 附加表的表名取自文件名主干（lb.sas7bdat -> lb）
    table = Path(right_rel).stem.lower().replace("-", "_")
    args = {
        "path": left_rel,
        "extra_paths": [right_rel],
        "sql": f"SELECT COUNT(*) AS n FROM dataset d INNER JOIN {table} e USING (USUBJID)",
        "limit": 10,
    }
    started = time.perf_counter()
    try:
        data, _ = await _call(client, "query_dataset", args)
        n = data["records"][0]["n"]
        return Case(
            "query_dataset(JOIN)",
            n > 0,
            f"{Path(left_rel).stem} ⋈ {Path(right_rel).stem} = {n} 行",
            (time.perf_counter() - started) * 1000,
        )
    except Exception as exc:
        return Case("query_dataset(JOIN)", False, f"{type(exc).__name__}: {exc}"[:160], (time.perf_counter() - started) * 1000)


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def render_markdown(reports: list[FileReport], joins: list[Case], seed: int, elapsed: float) -> str:
    total_calls = sum(len(r.cases) for r in reports) + len(joins)
    total_pass = sum(r.passed for r in reports) + sum(1 for c in joins if c.ok)
    total_fail = total_calls - total_pass
    categories = sorted({r.category for r in reports})

    lines = [
        "## 随机冒烟测试（tests 数据）",
        "",
        "以 **stdio 子进程**方式拉起安装后的 MCP 服务（`.venv/Scripts/sas-local-mcp.exe`），对 `tests/` "
        "下各类别目录随机抽检：每类强制包含该类中**最小**与**最大**的文件，每类至少 2 个文件；每个文件依次调用"
        "全部 8 个工具，limit / offset / columns / where / apply_labels / include_missing / SQL 模板均随机生成。",
        "",
        "| 类别 | 文件 | 大小 | 行数 × 列数 | 工具调用（通过/总数） | 耗时 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in reports:
        shape = f"{r.rows} × {r.cols}" if r.rows is not None else "—"
        lines.append(
            f"| `{r.category}` | `{r.rel_path}` | {human_size(r.size_bytes)} | {shape} "
            f"| {r.passed}/{len(r.cases)} | {r.ms:.0f} ms |"
        )

    lines += ["", "<details>", "<summary>逐条调用明细</summary>", ""]
    for r in reports:
        lines += [f"**`{r.rel_path}`**（`{r.category}`，{human_size(r.size_bytes)}）", ""]
        for c in r.cases:
            flag = "PASS" if c.ok else "FAIL"
            lines.append(f"- [{flag}] `{c.tool}` {c.ms:.0f} ms — {c.detail}")
        lines.append("")
    for c in joins:
        flag = "PASS" if c.ok else "FAIL"
        lines += [f"**跨数据集 JOIN**", "", f"- [{flag}] `{c.tool}` {c.ms:.0f} ms — {c.detail}", ""]
    lines.append("</details>")

    failures = [(r, c) for r in reports for c in r.cases if not c.ok]
    failed_joins = [c for c in joins if not c.ok]

    lines += [
        "",
        "**汇总**",
        "",
        f"- 随机种子：`{seed}`（`--seed` 可复现本次抽样）",
        f"- 覆盖类别：{len(categories)} 个（{'、'.join(f'`{c}`' for c in categories)}）",
        f"- 抽检文件：{len(reports)} 个",
        f"- 工具调用：{total_calls} 次，成功 {total_pass} 次，失败 {total_fail} 次",
        f"- 端到端耗时：{elapsed:.1f} s",
    ]

    if failures or failed_joins:
        lines += ["", "**失败明细**", ""]
        for r, c in failures:
            lines.append(f"- `{r.rel_path}` → `{c.tool}`：{c.detail}")
        for c in failed_joins:
            lines.append(f"- `{c.tool}`：{c.detail}")
    else:
        lines += ["", "全部用例通过（无失败明细）。"]

    return "\n".join(lines) + "\n"


def write_into(path: Path, report: str) -> None:
    """把报告插到目标 Markdown 的标记之间（首次运行时追加标记）。"""
    text = path.read_text(encoding="utf-8")
    block = f"{START_MARKER}\n\n{report}\n{END_MARKER}"
    if START_MARKER in text and END_MARKER in text:
        head, rest = text.split(START_MARKER, 1)
        _, tail = rest.split(END_MARKER, 1)
        text = head + block + tail
    else:
        text = text.rstrip() + f"\n\n{block}\n"
    path.write_text(text, encoding="utf-8")


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对 tests 数据做随机 MCP 冒烟测试")
    parser.add_argument("--seed", type=int, default=None, help="随机种子；不传则随机")
    parser.add_argument("--per-category", type=int, default=3, help="每个类别抽检文件数（至少 2，含最小/最大）")
    parser.add_argument("--exe", default=None, help="MCP 入口可执行文件路径")
    parser.add_argument("--out", default=None, help="把报告写回指定 Markdown 文件（置于标记之间）")
    args = parser.parse_args(argv)

    per_category = max(2, args.per_category)
    seed = args.seed if args.seed is not None else random.SystemRandom().randint(1, 10**9)
    rng = random.Random(seed)

    exe = Path(args.exe) if args.exe else Path(sys.executable).with_name("sas-local-mcp.exe")
    if not exe.is_file():
        print(f"找不到 MCP 入口: {exe}", file=sys.stderr)
        return 2

    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "PYTHONIOENCODING": "utf-8",
        "SAS_DATA_ROOT": str(TESTS_ROOT),
    }
    transport = StdioTransport(command=str(exe), args=[], env=env, cwd=str(TESTS_ROOT))

    started = time.perf_counter()
    reports: list[FileReport] = []
    joins: list[Case] = []

    async with Client(transport) as client:
        for category, files in discover_categories(TESTS_ROOT).items():
            samples = pick_samples(files, rng, per_category)
            cat_reports = []
            for path in samples:
                rep = await run_battery(client, category, path, rng)
                reports.append(rep)
                cat_reports.append(rep)

            # 同一类别内取两个含 USUBJID 的文件做一次跨数据集 JOIN
            joinable = [r.rel_path for r in cat_reports if r.has_usubjid][:2]
            if len(joinable) == 2:
                joins.append(await run_join_case(client, joinable[0], joinable[1]))

    elapsed = time.perf_counter() - started
    report = render_markdown(reports, joins, seed, elapsed)
    print(report)

    if args.out:
        out_path = Path(args.out)
        write_into(out_path, report)
        print(f"报告已写入 {out_path}")

    return 1 if (any(r.failed for r in reports) or any(not c.ok for c in joins)) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
