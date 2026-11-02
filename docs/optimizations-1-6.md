# 优化 1–6 使用说明

本次改造保持八个工具名称和主要数据字段，新增大表读取、可靠的 SQL 标签来源、独立缓存、SAS 缺失语义、分页和输出 schema。

## 升级与兼容变化

先在仓库中运行 `uv sync`，或使用虚拟环境的 Python 执行 `-m pip install -e .`，然后重启 MCP 客户端中的服务进程。
新增直接依赖为 `sqlglot>=26.0` 和 `pydantic>=2.0`。

- `where` 先过滤后投影，过滤字段可以不包含在 `columns` 中。
- `where` 支持比较、数值算术、`in/not in`、`is None/is not None`、`and/or/not` 和带括号的布尔位运算。不支持 pandas 方法调用、属性、反引号变量名或 `@` 外部变量；复杂筛选使用 SQL。
- `missing_policy="sas"` 是默认值：字符空字符串和仅由 ASCII 空格组成的字符串计为缺失，制表符、全角空格等实际字符不计为缺失。`"pandas"` 保留只把字符 null/NaN 当作缺失的口径。特殊数值缺失在两种模式中都属于缺失。
- SQL 标签不再按结果名称猜测。来源不明的列保持原值，解码信息以 `label_decoding` 为准。
- 成功结果通过输出 schema 发布。Python FastMCP 客户端应优先读取 `result.structured_content`；`result.data` 可能是客户端生成的 Pydantic 对象。

## 读取大文件的一小部分

以下工具参数示例以 `SAS_DATA_ROOT` 指向本项目的 `tests/` 为前提。

```json
{
  "path": "sasdata/sdtm/dm.sas7bdat",
  "columns": ["USUBJID", "SEX"],
  "where": "AGE >= 65 and SEX == 'F'",
  "offset": 0,
  "limit": 20,
  "include_row_count": false
}
```

将上述参数传给 `read_dataset`。无过滤预览直接把列投影、行偏移和页长交给 pyreadstat；有过滤时只读取输出列与条件列，分块扫描，保留当前页及一条额外记录判断是否有后续结果。

`include_row_count=True` 为默认值，会完成过滤计数。设为 `false` 可以提前停止；未完成扫描时 `rows_after_filter=null`。SAS 文件头已知的总行数仍可返回。
XPORT 的元数据通常不包含行数；精确计数会按块读取一列，不建立整表 DataFrame。调用 `get_dataset_metadata(include_row_count=false)` 可以跳过计数，未知行数为 `null`，不会谎报为 0。

`row_count_exact` 表示 `total_rows` 或元数据行数是否已知。`rows_after_filter` 是否精确以该字段是否为 `null` 判断。
`apply_labels=true` 只处理最终返回页，不修改原始缓存。

## 给 SQL 结果正确解码

`query_dataset` 的分页在 DuckDB 关系上执行，最多物化请求页和一条额外记录。

```json
{
  "path": "sasdata/sdtm/dm.sas7bdat",
  "sql": "SELECT USUBJID, SEX AS sex_code FROM dataset ORDER BY USUBJID",
  "limit": 20,
  "offset": 0,
  "include_total": false,
  "apply_labels": true
}
```

`include_total=true` 默认会额外执行精确计数；设为 `false` 后 `total_rows=null`、`row_count_exact=false`。
计数尊重用户 SQL 自己的 `LIMIT`。非确定性查询的计数与分页是独立求值；需要一致结果时应使用确定性的 SQL。

自动解码仅处理简单 SELECT 中来源唯一的原始列，包括列别名、限定表名及无歧义的星号投影。
聚合、算术、重复输出名称、CTE、子查询、集合运算以及透视变换保持原值。
例如 `SUM(ARM) AS ARM` 的结果不会因为列名相同被替换成治疗标签。

复杂查询可以显式提供 `label_sources`。下面是存在 `ARM` 自定义格式时的参数片段：

```json
{
  "apply_labels": true,
  "label_sources": {
    "treatment": {"table": "dataset", "column": "ARM"}
  }
}
```

`table` 使用服务注册表名，原始列名放在 `column`。显式来源由调用方确认语义，不能把聚合统计当作原始码值。

每个结果列的 `label_decoding` 包含 `source_table`、`source_column`、`status`、`values_decoded`。
状态为 `not_requested`、`source_unknown`、`no_labels`、`no_matching_values` 或 `decoded`；计数始终针对实际返回页。
仅在本页确实替换了值时，`value_labels_applied=true`。

`order_by_present` 只说明 SQL 最外层有无 `ORDER BY`，不保证排序键唯一。稳定翻页应使用唯一排序键；未排序的分页会返回提示。

SQL 仍需解析并注册源表，复杂 JOIN 和排序仍有计算成本。每次最多注册 16 张表，DuckDB 的外部访问关闭，磁盘溢写关闭。

## 独立管理缓存与并发

元数据、完整数据、格式目录和 XPORT 精确行数分别缓存。元数据扫描不占用完整数据缓存的条目。
缓存键包含路径、mtime、大小、编码和格式目录版本；读取期间检测到文件版本变化会返回 `FILE_CHANGED`。
同一缓存键的并发读取合并为一次；缓存和 LRU 淘汰受锁保护，读取失败可以重试。

完整数据同时受条目数和内存预算限制，超预算单表可以使用但不缓存。
元数据与格式目录各有 32 MiB 预算；计数缓存有 1 MiB 预算。
`SAS_LOCAL_MCP_CACHE_SIZE=0` 关闭这些缓存。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `SAS_LOCAL_MCP_CACHE_SIZE` | 8 | 完整数据缓存条目数 |
| `SAS_LOCAL_MCP_METADATA_CACHE_SIZE` | 512 | 元数据、目录及计数各自的条目数上限 |
| `SAS_LOCAL_MCP_CACHE_MB` | 256 | 完整数据缓存的 MiB 预算 |
| `SAS_LOCAL_MCP_CHUNK_ROWS` | 10000 | 分块过滤和 XPORT 计数的块大小 |
| `SAS_LOCAL_MCP_SQL_CONCURRENCY` | 2 | SQL 调用的并发数，覆盖源表读取 |
| `SAS_LOCAL_MCP_SQL_THREADS` | 2 | 每个 DuckDB 连接的线程数 |
| `SAS_LOCAL_MCP_SQL_MEMORY_MB` | 512 | 每个 DuckDB 连接的 MB 内存上限 |
| `SAS_LOCAL_MCP_MAX_RESPONSE_BYTES` | 524288 | 结构化 JSON 的 UTF-8 字节预算 |

这些预算不等于进程总内存上限。正在解析的数据、pandas 对象、返回结果以及不同缓存需要额外空间；DuckDB 内存限制也不包含 pandas 输入。

## 接入格式目录与解释缺失值

在数据根内放置实际 `.sas7bcat` 文件后，为 `get_dataset_metadata`、`read_dataset`、`summarize_dataset`、`frequency`、`get_value_labels` 传入 `catalog_path`。
SQL 的 `catalog_paths` 按注册表名配置，例如 `{"dataset": "formats.sas7bcat", "lb": "labformats.sas7bcat"}`。

格式目录独立解析，仅把标签映射附加到元数据中。原始码值不会被读取器提前替换；不同目录和目录更新不会复用错误的标签缓存。

原始记录中，普通数值缺失为 `null`，特殊数值缺失保留为 `".A"`–`".Z"`、`"._"`。
字符空白在原始记录中保留，在 `missing_policy="sas"` 的统计与过滤中视为缺失。
`summarize_dataset` 和 `frequency` 的 `missing_details` 分别给出 `"."`、各特殊缺失和 `"blank"` 的数量。
`frequency(include_missing=true)` 仍将所有缺失合并为一个缺失档，分母为全部记录；细分类型从 `missing_details` 获取。

SQL 将 SAS 特殊缺失转换成 `NULL`，确保数值运算正常，并在 `special_missing_values` 列出各源表遇到的特殊缺失类型。
SQL 不提供 SAS 特殊缺失值的排序或比较语义；需要区分特殊缺失时，先使用原始读取或频数工具。

元数据保留兼容的 `type` 字段，另外提供 `logical_type`、`sas_unit`、`storage_width` 和原始 `format`。
`logical_type` 细分 `date`、`time`、`datetime`；SAS 的原始单位分别为自 1960-01-01 起的天数、自午夜起的秒数、自 1960-01-01 起的秒数。
正常读取输出 ISO 日期/时间字符串，不额外返回原始数值日期。

## 翻页、精简摘要和限制响应大小

| 工具 | 分页参数 | 下一页字段 |
| --- | --- | --- |
| `list_datasets` | `offset`、`limit` | `next_offset` |
| `read_dataset` / `query_dataset` / `frequency` | `offset`、`limit` | `next_offset` |
| `get_dataset_metadata` | `column_offset`、`column_limit` | `next_column_offset` |
| `summarize_dataset` | 变量 `offset`、`limit`，可配合 `columns` | `next_offset` |
| `search_variables` | 窗口内匹配 `offset`、`limit`；扫描 `scan_offset`、`max_datasets` | `next_offset`、`next_scan_offset` |

目录发现与变量扫描采用确定性的文件遍历顺序。搜索时，先保持 `scan_offset` 不变，沿 `next_offset` 取完当前窗口；然后把 `scan_offset` 改为 `next_scan_offset`、将 `offset` 重置为 0。
扫描期间新增、删除或重命名文件会改变偏移，连续翻页应使用稳定的数据目录。

`summarize_dataset(stats_mode="counts")` 只计算非缺失数、缺失率、唯一值数和缺失细分，`stats={}`，跳过分位数、均值和 Top5 等分布计算。
目前摘要和频数仍解析整张源表；分页限制本次统计的变量和输出，不代表源数据流式聚合。

响应同时受行数和字节预算约束。`response_bytes` 使用 `json.dumps(..., ensure_ascii=False, allow_nan=False)` 的 UTF-8 字节数，包含预算字段自身；MCP 文本副本和协议封装另有开销。
超预算时只保留完整的列表条目，更新 `returned` 和下一页偏移，并令 `response_truncated=true`；不会截断字符串、修改原始值或把精确计数改成抽样。
单条结果、映射或元数据本身超过预算时返回 `RESPONSE_TOO_LARGE`。此时缩小 `columns`、`max_datasets` 或 SQL 输出；值标签可以使用 `columns` 限制变量范围。
非有限数值 NaN、正负 Infinity 统一输出 `null`，保证严格 JSON 可序列化。

## 处理错误和验证行为

工具执行错误保留 MCP `ToolError`，消息前缀提供稳定代码：

| 代码 | 处理方式 |
| --- | --- |
| `PATH_OUTSIDE_ROOT` / `FILE_NOT_FOUND` | 修正数据根或相对路径 |
| `UNSUPPORTED_FORMAT` / `INVALID_CATALOG` | 检查数据和格式目录的文件类型、注册表名 |
| `READ_FAILED` / `ENCODING_ERROR` / `IO_ERROR` | 检查文件、权限及 `SAS_LOCAL_MCP_ENCODING` |
| `FILE_CHANGED` | 等文件写入完成后重试 |
| `UNKNOWN_VARIABLE` / `INVALID_FILTER` / `INVALID_ARGUMENT` | 根据元数据及工具输入 schema 修正参数 |
| `READ_ONLY_SQL` / `INVALID_SQL` / `QUERY_FAILED` | 修正 SQL；仅允许单条 SELECT |
| `QUERY_RESOURCE_LIMIT` | 减少查询规模或调整资源预算 |
| `INVALID_LABEL_SOURCE` | 检查结果列名及来源表、来源变量 |
| `RESPONSE_TOO_LARGE` | 缩小输出范围或调整响应预算 |

输入 schema 校验错误仍由 MCP 框架处理。输出模型见 `src/sas_local_mcp/responses.py`。
可运行以下命令验证本次改造：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_optimizations.py
.\.venv\Scripts\python.exe -m pytest -q
```

新增回归覆盖 SQL 标签冲突与表达式、局部读取、分块过滤、真实 XPORT 分块计数、SQL 有界物化、并发单次解析、缓存失效、SAS 缺失、格式目录绑定和 MCP schema。
格式目录与特殊缺失使用受控读取器验证；仓库现有数据没有真实格式目录样本，因此这些测试不等于真实业务格式目录的兼容性验收。

2026-11-02 本机验证记录：

- 全量 pytest 使用 `SAS_LOCAL_MCP_RANDOM_SEED=12`，结果为 **76 passed、4 skipped**，包含 CP1252 编码 `ts.xpt` 的读取与测试编码回退。新增优化回归共 30 项，全部通过。
- 4 个跳过来自已有测试：3 项未找到符合筛选条件的数据，1 项缺少损坏样本。
- 安装后的入口以 stdio 子进程运行，随机种子 `20261102`、每类别 2 个文件；覆盖 6 类、12 个文件和跨表 JOIN，**102 次调用全部成功**，命令退出码为 0。
- stdio 报告保存在 `.pytest_cache/stdio-optimizations-1-6.md`；报告为本机验证产物，未写回 README 的历史冒烟结果。
- `git diff --check` 通过。Windows 沙箱创建 pytest 临时目录及 asyncio 命名管道曾遇到 `WinError 5`；最终验证使用正常 Windows 权限，测试临时文件与报告均位于项目 `.pytest_cache/`。
