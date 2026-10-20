# sas-local-mcp

一个让大模型（LLM）直接读取**本地 SAS 数据集**的 MCP 服务器。

改编自 [sassoftware/sas-mcp-server](https://github.com/sassoftware/sas-mcp-server)（Apache-2.0）：保留其
[FastMCP](https://github.com/jlowin/fastmcp) 架构与 `register(mcp, ...)` 工具注册模式，但把数据源从 SAS Viya 平台
换成本地文件——经 [pyreadstat](https://github.com/Roche/pyreadstat) 直接读取，**无需 Viya、无需联网、无需 SAS 授权**。

适合的场景：

- 让 LLM 勘察 `/` 浏览本地 `.sas7bdat` / `.xpt` 数据集的结构与取值
- TLF（Tables, Listings, Figures）QC：快速做计数、频数、跨数据集关联核对
- 在 IDE / Agent 中把 SAS 数据当作可查询的上下文来源

所有工具均为**只读**，不会修改任何数据集文件。

## 功能特性

- 支持 SAS 两种常用格式：`.sas7bdat` 与传输格式 `.xpt`（XPORT）
- **统计语义层**：`summarize_dataset` 逐变量给缺失率/唯一值数/分布指标，`frequency` 给频数表并自动解码值标签——
  模型无需手写 SQL 就能读懂一张表
- 路径相对 `SAS_DATA_ROOT` 或绝对路径传入，**越界路径会被拒绝**（安全沙箱）
- 读取时自动处理：缺失值（`NaN`→`null`）、日期/时间（`datetime`→ISO 字符串）、字符编码
- 行预览与聚合结果**分开限流**：`GROUP BY` 的基数不会被行预览上限截断（见环境变量）
- 内置 duckdb SQL 查询：仅执行单条 `SELECT`（支持 `WITH`/多表 JOIN），并支持结果值标签解码
- `search_variables` 跨数据集变量搜索：直接回答"哪个数据集里有 `USUBJID`"
- 解析结果按 (路径, mtime, size) 缓存，多轮问答不必反复解析大文件
- 通过 MCP 标准协议以 stdio 方式接入任意兼容客户端（CodeBuddy、Claude Desktop、Cursor 等）

## 工具一览

所有工具路径参数 `path` 支持两种写法：

- 相对 `SAS_DATA_ROOT` 的路径，如 `02Sdtm/dm.sas7bdat`
- 绝对路径，但必须落在 `SAS_DATA_ROOT` 目录树内（否则被拒绝）

| 工具 | 说明 | 关键参数 |
| --- | --- | --- |
| `list_datasets` | 递归列出 `SAS_DATA_ROOT` 下所有数据集文件（`.sas7bdat` / `.xpt` / `.sas7bcat`） | `pattern`（文件名子串过滤，不区分大小写）、`limit`（默认 500） |
| `get_dataset_metadata` | 行数、每变量 label/type/informat、文件标签、创建时间（不加载数据内容；`.xpt` 例外，见下） | `path` |
| `summarize_dataset` | 逐变量概要统计——了解一张表的首选入口：缺失率、唯一值数，按类型给分布指标 | `path`、`columns`、`apply_labels` |
| `frequency` | 单变量频数表：各取值的 `n` 与占比，**默认自动套用 SAS 值标签** | `path`、`column`、`limit`、`offset`、`apply_labels`、`include_missing` |
| `read_dataset` | 按 `offset`/`limit` 分页读取 JSON records（`NaN`→`null`，日期→ISO），支持列子集、`where` 过滤、套用值标签 | `path`、`limit`（默认 20）、`offset`、`columns`、`where`、`apply_labels` |
| `get_value_labels` | 变量的 SAS 格式值标签映射（QC 解码 coded 值用） | `path` |
| `query_dataset` | duckdb SQL 查询（仅允许单条 `SELECT` / `WITH` 查询），主表为 `dataset`，可追加多表做 JOIN | `path`、`sql`、`limit`（默认 100）、`offset`、`extra_paths`、`apply_labels` |
| `search_variables` | 跨数据集搜索含指定变量的数据集（只读元数据） | `variable`、`pattern`、`contains`、`limit`、`max_datasets` |

建议调用顺序：`list_datasets` / `search_variables` → `get_dataset_metadata` → `summarize_dataset` →
`frequency`（分类变量）→ `read_dataset` / `query_dataset`（按需取细节）。

### 各工具参数与返回

- **`list_datasets(pattern?, limit=500)`**
  - 返回 `{ root, count, datasets: [{ path, name, format, size_bytes, modified }] }`。
- **`get_dataset_metadata(path)`**
  - 返回 `{ number_rows, number_columns, file_label, created, modified, columns: [{ name, label, type, format, informat }] }`。
  - `type` 的日期判定同时看 `informat` 与 `format`（SAS 日期列常只带 format，如 `DATE9.`）。
  - **`.xpt` 会多读一次**：pyreadstat 对 XPORT 的 metadata-only 读取不返回行数
    （`meta.number_rows` 为 `None`、df 为空），直接采信会把 6 万行的数据集报成 0 行。
    此时本工具会退回全量读取数一次行数，结果同样进缓存，后续调用不会再重复解析。
- **`summarize_dataset(path, columns?, apply_labels=False)`**
  - 每个变量返回 `{ name, label, type, n, n_missing, pct_missing, n_distinct, has_value_labels, stats }`。
  - `stats` 按类型不同：
    - 数值 → `{ mean, std, min, p25, median, p75, max }`
    - 日期 → `{ min, max }`
    - 字符 → `{ max_length, top_values: [{ value, label, n, pct }] }`（Top5）
  - `has_value_labels` 为 `True` 时提示改用 `frequency` 看解码后的分布。
  - `columns` 不传则统计全部变量。
- **`frequency(path, column, limit=50, offset=0, apply_labels=True, include_missing=False)`**
  - 返回 `{ total_rows, n_missing, n_distinct, denominator, value_labels_applied, levels, returned, truncated }`。
  - `levels` 每项 `{ value, label, n, pct }`，按 `n` 降序；`value` 是原始码值，`label` 是解码后的文本。
  - `apply_labels` 默认 `True`——coded 值本身不可读，默认直接译出标签。
  - `denominator` 回传百分比分母：`include_missing=True` 时是总行数，否则是非缺失行数。
    `include_missing=True` 会把缺失单列为一档（`value=null`、label 为 `<missing>`）。
  - 结果上限取 `SAS_LOCAL_MCP_MAX_AGG_ROWS`（默认 1000），**不受行预览上限影响**。
- **`read_dataset(path, limit=20, offset=0, columns?, where?, apply_labels=False)`**
  - `columns`：只返回指定变量列表。
  - `where`：pandas 查询表达式，如 `"AGE > 18"` 或 `"SEX == 'F'"`。
  - `where` 是在 `columns` 投影**之后**求值的：过滤条件只能引用 `columns` 里保留的变量，
    否则会返回「where 条件无法应用」。想按某列过滤时，记得把它一并放进 `columns`。
  - `apply_labels=True` 时把 coded 值替换为 SAS 值标签文本。
  - `offset` 用于翻到数据中部/尾部取样——只看前几行容易误判（临床数据前几行常属同一受试者）。
  - 返回 `{ columns, records, total_rows, rows_after_filter, offset, returned, truncated }`，
    行数受 `SAS_LOCAL_MCP_MAX_ROWS` 上限约束。
- **`get_value_labels(path)`**
  - 返回 `{ path, format, variables: { 变量: { 码值: 标签 } } }`。
- **`query_dataset(path, sql, limit=100, offset=0, extra_paths?, apply_labels=False)`**
  - 只接受一条 `SELECT` 查询（可以使用 `WITH` CTE）；多条语句和 `INSERT` / `UPDATE` / `DELETE` / DDL 会被拒绝，DuckDB 外部文件与网络访问也会关闭。
  - 主数据集表名固定为 `dataset`，例如 `SELECT COUNT(*) AS n FROM dataset`。
  - `extra_paths` 传入附加数据集，按文件名主干注册为额外表（`lb.sas7bdat` → `lb`），
    冲突时回退 `dataset2`/`dataset3`；返回的 `tables` 给出 表名 → 路径 映射。跨数据集对比示例：

    ```sql
    SELECT COUNT(*) AS n FROM dataset d INNER JOIN lb l USING (USUBJID)
    ```

  - `apply_labels=True` 把结果里的 coded 值译为值标签文本。
  - 返回查询结果 records，上限取 `SAS_LOCAL_MCP_MAX_AGG_ROWS`，支持 `offset` 翻页。
- **`search_variables(variable, pattern?, contains=False, limit=50, max_datasets=200)`**
  - 回答"哪个数据集里有某个变量"。默认**精确匹配**（不区分大小写），`contains=True` 改为子串匹配
    （如 `AGE` 命中 `AGE` / `AGEU`）。
  - `pattern` 先按文件名过滤数据集以缩小扫描范围；只读取元数据，不加载数据行。
  - 返回 `{ datasets_scanned, datasets_matched, matches: [{ path, dataset, variable, label, type, number_rows }], scan_truncated, truncated, errors }`；
    `scan_truncated=true` 表示达到 `max_datasets` 后仍有未扫描的数据集，结果可能不完整。
    损坏文件记入 `errors` 而不中断整个搜索。

所有错误（文件不存在、格式损坏、路径越界、未知变量、SQL 语法错误）都会被转成 MCP `ToolError` 以
干净的消息返回给客户端，不会让服务进程崩溃。

## 环境要求

- Python **>= 3.11**
- 支持的包（见 `pyproject.toml`）：
  - `fastmcp` 4.x
  - `pandas` >= 2.0
  - `pyreadstat` >= 1.2.8（主力读取器）
  - `duckdb` >= 1.1.0（SQL 查询）
- 开发/测试依赖：`pytest`、`pytest-asyncio`

## 安装

克隆或下载本仓库后，在仓库根目录执行。

### 方式一：使用 `uv`（推荐）

```bash
# 安装依赖并进入环境
uv sync

# 运行
uv run sas-local-mcp
```

### 方式二：使用 `pip` + 虚拟环境

```bash
cd <repo>                 # 仓库根目录
python -m venv .venv

# Windows
.venv\Scripts\python.exe -m pip install -e .
# macOS / Linux
.venv/bin/python -m pip install -e .
```

安装后会在环境中生成一个 `sas-local-mcp` 可执行入口（脚本/控制台命令）。

> 若是把仓库从一个目录搬到另一个目录后再用既有 `.venv`，editable 安装记录的路径可能仍指向旧位置
> （`.venv/Lib/site-packages/sas_local_mcp.pth`）。此时重新执行上面的 `pip install -e .` 即可。

### 方式三：仅起本地开发服务（不安装）

```bash
# 确保已 pip install -e . 或依赖已就位
python -m sas_local_mcp
```

服务以 **stdio** 协议运行：通常由 MCP 客户端在配置后自动拉起，无需手动常驻。
如需手动调试，可直接运行入口可执行文件（如 `.venv/bin/sas-local-mcp` 或
`.venv\Scripts\sas-local-mcp.exe`）。

## 配置（环境变量）

服务通过环境变量配置，在 MCP 客户端配置中以 `env` 字段传入即可。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SAS_DATA_ROOT` | 进程当前工作目录（CWD） | 数据根目录，所有数据集路径必须落在其内（沙箱边界） |
| `SAS_LOCAL_MCP_MAX_ROWS` | `200` | **行预览**上限：`read_dataset` 一次最多返回多少条原始记录 |
| `SAS_LOCAL_MCP_MAX_AGG_ROWS` | `1000` | **聚合结果**上限：`frequency` 的取值个数、`query_dataset` 的返回行数 |
| `SAS_LOCAL_MCP_ENCODING` | 空（pyreadstat 默认） | 数据集字符编码；中文数据常需 `utf-8` 或 `gbk` |
| `SAS_LOCAL_MCP_CACHE_SIZE` | `8` | 缓存多少个已解析数据集（按 mtime/size 失效）；`0` 关闭 |
| `SAS_LOCAL_MCP_NAME` | `sas-local-mcp` | 服务名（显示在客户端） |

> 两个上限分开的意义：`GROUP BY` 的基数常远超预览行数（例如 306 个受试者的 `USUBJID` 频数表）。
> 若聚合也被 200 行截断，模型会读到不完整的分布并可能得出错误结论。
>
> `SAS_DATA_ROOT` 支持 `~` 家目录展开；未设置时退化为启动进程的当前目录。

## MCP 客户端配置示例

在标准 MCP 客户端配置文件（如 `mcp.json` / `claude_desktop_config.json` / 各 IDE 的 MCP 设置）中加入：

```json
{
  "mcpServers": {
    "sas-local-mcp": {
      "type": "stdio",
      "command": "/绝对/路径/到/sas-local-mcp",
      "args": [],
      "env": {
        "SAS_DATA_ROOT": "/绝对/路径/到/你的数据根目录"
      },
      "description": "读取本地 SAS 数据集（sas7bdat/xpt）的只读 MCP 服务"
    }
  }
}
```

- `command`：填入上一步安装得到的 `sas-local-mcp` 入口绝对路径
  - 使用 `uv`：`uv run sas-local-mcp` 的等效绝对路径，或直接 `"command": "uv", "args": ["--directory", "/绝对/路径/到/repo", "run", "sas-local-mcp"]`
  - 使用 `pip` 虚拟环境：`.venv/bin/sas-local-mcp`（macOS/Linux）或 `.venv\Scripts\sas-local-mcp.exe`（Windows）
- `env.SAS_DATA_ROOT`：务必设为你的数据根目录，否则只能用 CWD 作为根。

保存后重启客户端，即可在工具列表中看到 `sas-local-mcp` 提供的八个工具。

## 测试

```bash
# 安装测试依赖（若尚未）
pip install pytest pytest-asyncio     # 或 uv sync

# 运行全部测试
pytest
```

测试不依赖外部环境：`tests/` 下随仓库附带真实数据集（来源见下）。

以数据目录作为 `SAS_DATA_ROOT`（由 `tests/conftest.py` 自动解析），通过内存客户端挂载本服务逐个调用全部工具，并覆盖：

- 概要统计的数值/字符分支、列子集、未知变量报错
- 频数表的降序、占比、分母语义（`include_missing`）
- **聚合基数不被行预览上限截断**
- `offset` 分页与越界行为
- 多表注册与跨数据集 JOIN、SQL 结果值标签解码
- 跨数据集变量搜索（精确/子串、文件名过滤、未命中）
- 读取缓存命中（重复读取不再解析）
- 值标签解码与缺失分档（注入 meta 覆盖）
- 路径越界被拦截

另有少量用例依赖仓库外的旧工作区数据，找不到时会自动 skip。

### 测试数据来源

`tests/` 下的测试数据取自 R Consortium 的 submissions-pilot5 公开示例提交包：

- 上游仓库：<https://github.com/RConsortium/submissions-pilot5-datasetjson>
- 取用目录：
  - [`original-sdtmdata/`](https://github.com/RConsortium/submissions-pilot5-datasetjson/tree/main/original-sdtmdata)
    — SDTM 域的 XPORT（V5）文件，22 个（`ae` / `cm` / `dm` / `lb` / `qs` / `supplb` ...）
  - [`original-adamdata/`](https://github.com/RConsortium/submissions-pilot5-datasetjson/tree/main/original-adamdata)
    — ADaM 数据集的 XPORT（V5）文件，12 个（`adsl` / `adlbc` / `adtte` ...）

在此之上派生出三套等价数据，用于交叉验证同一份数据在不同格式 / 编码下的读取结果：

| 目录 | 内容 | 说明 |
| --- | --- | --- |
| `tests/xpt/` | 原始 XPORT 文件 | 上游两个 `original-*data/` 目录直接拷贝；源带 Windows-1252 字节（如 `ts.xpt`） |
| `tests/sasdata/` | UTF-8 编码的 sas7bdat | 由 `tests/xpt2sas7bdat.sas` 转换 |
| `tests/sasdata_cn/` | EUC-CN（GBK）编码的 sas7bdat | 同上，用于中文 / 非 UTF-8 编码回归 |

文件规模跨度：4.8 KB（`xpt/sdtm/suppds.xpt`）～ 54.6 MB（`sasdata/sdtm/supplb.sas7bdat`，64403 × 10），
随机冒烟测试即按「每类别最大 + 最小」抽样来同时覆盖两端。

> 转换脚本需在 **WLATIN1** 会话下运行：`sas.exe -sysin tests/xpt2sas7bdat.sas -encoding wlatin1`，
> 由 SAS 在写出时经 `ENCODING=` 数据集选项转码。

### 随机冒烟测试

`tests/random_smoke.py` 与上面的单元测试互补：它**不走内存直连**，而是以
**stdio 子进程**拉起安装后的 MCP 服务，端到端验证「客户端 → 协议 → 工具」的完整链路。

```bash
python tests/random_smoke.py --seed 20261020 --per-category 3 --out README.md
```

- 自动发现 `tests/` 下所有类别目录，每类至少抽 2 个文件，**并强制包含该类中最小与最大的文件**
- 每个文件依次调用全部 8 个工具，limit / offset / columns / where / apply_labels /
  include_missing / SQL 模板随机生成
- `--out README.md` 把结果写回下一节的两行标记之间（重复运行就地覆盖）

<!-- RANDOM-SMOKE-START -->

## 随机冒烟测试（tests 数据）

以 **stdio 子进程**方式拉起安装后的 MCP 服务（`.venv/Scripts/sas-local-mcp.exe`），对 `tests/` 下各类别目录随机抽检：每类强制包含该类中**最小**与**最大**的文件，每类至少 2 个文件；每个文件依次调用全部 8 个工具，limit / offset / columns / where / apply_labels / include_missing / SQL 模板均随机生成。

| 类别 | 文件 | 大小 | 行数 × 列数 | 工具调用（通过/总数） | 耗时 |
| --- | --- | --- | --- | --- | --- |
| `sasdata/adam` | `sasdata/adam/adsl.sas7bdat` | 192.0 KB | 254 × 49 | 8/8 | 752 ms |
| `sasdata/adam` | `sasdata/adam/adlbcpv.sas7bdat` | 16.0 MB | 37132 × 46 | 8/8 | 687 ms |
| `sasdata/adam` | `sasdata/adam/adlbhpv.sas7bdat` | 11.2 MB | 24966 × 46 | 8/8 | 838 ms |
| `sasdata/sdtm` | `sasdata/sdtm/sc.sas7bdat` | 128.0 KB | 254 × 14 | 8/8 | 403 ms |
| `sasdata/sdtm` | `sasdata/sdtm/supplb.sas7bdat` | 54.6 MB | 64403 × 10 | 8/8 | 846 ms |
| `sasdata/sdtm` | `sasdata/sdtm/sv.sas7bdat` | 384.0 KB | 3559 × 8 | 8/8 | 400 ms |
| `sasdata_cn/adam` | `sasdata_cn/adam/adsl.sas7bdat` | 192.0 KB | 254 × 49 | 8/8 | 457 ms |
| `sasdata_cn/adam` | `sasdata_cn/adam/adlbcpv.sas7bdat` | 16.0 MB | 37132 × 46 | 8/8 | 857 ms |
| `sasdata_cn/adam` | `sasdata_cn/adam/adlbh.sas7bdat` | 9.9 MB | 24966 × 46 | 8/8 | 1006 ms |
| `sasdata_cn/sdtm` | `sasdata_cn/sdtm/sc.sas7bdat` | 128.0 KB | 254 × 14 | 8/8 | 488 ms |
| `sasdata_cn/sdtm` | `sasdata_cn/sdtm/supplb.sas7bdat` | 54.6 MB | 64403 × 10 | 8/8 | 936 ms |
| `sasdata_cn/sdtm` | `sasdata_cn/sdtm/ts.sas7bdat` | 128.0 KB | 33 × 6 | 8/8 | 417 ms |
| `xpt/adam` | `xpt/adam/adtte.xpt` | 71.8 KB | 254 × 26 | 8/8 | 424 ms |
| `xpt/adam` | `xpt/adam/adlbcpv.xpt` | 15.8 MB | 37132 × 46 | 8/8 | 1138 ms |
| `xpt/adam` | `xpt/adam/adcibc.xpt` | 283.7 KB | 730 × 36 | 8/8 | 419 ms |
| `xpt/sdtm` | `xpt/sdtm/suppds.xpt` | 4.8 KB | 3 × 10 | 8/8 | 528 ms |
| `xpt/sdtm` | `xpt/sdtm/supplb.xpt` | 54.1 MB | 64403 × 10 | 8/8 | 2048 ms |
| `xpt/sdtm` | `xpt/sdtm/cm.xpt` | 3.6 MB | 7510 × 21 | 8/8 | 698 ms |

<details>
<summary>逐条调用明细</summary>

**`sasdata/adam/adsl.sas7bdat`**（`sasdata/adam`，192.0 KB）

- [PASS] `list_datasets` 348 ms — pattern='ads' 命中 3 个
- [PASS] `get_dataset_metadata` 21 ms — 254 行 × 49 列
- [PASS] `summarize_dataset` 34 ms — 49 变量概要（全量）
- [PASS] `get_value_labels` 3 ms — 无值标签
- [PASS] `frequency` 4 ms — SEX: 2 取值 / 返回 2 / 分母 254 / include_missing=True
- [PASS] `read_dataset` 5 ms — 返回 1 行 / 过滤后 254 行 / offset=166
- [PASS] `query_dataset` 63 ms — 1 行结果（group_by）
- [PASS] `search_variables` 273 ms — AVGDD: 命中 3 个数据集 / contains=True

**`sasdata/adam/adlbcpv.sas7bdat`**（`sasdata/adam`，16.0 MB）

- [PASS] `list_datasets` 4 ms — pattern='adl' 命中 15 个
- [PASS] `get_dataset_metadata` 13 ms — 37132 行 × 46 列
- [PASS] `summarize_dataset` 179 ms — 3 变量概要（列子集）
- [PASS] `get_value_labels` 4 ms — 无值标签
- [PASS] `frequency` 6 ms — AENTMTFL: 2 取值 / 返回 2 / 分母 37132 / include_missing=True
- [PASS] `read_dataset` 39 ms — 返回 1 行 / 过滤后 37122 行 / offset=10368
- [PASS] `query_dataset` 144 ms — 1 行结果（group_by）
- [PASS] `search_variables` 298 ms — RACEN: 命中 36 个数据集 / contains=False

**`sasdata/adam/adlbhpv.sas7bdat`**（`sasdata/adam`，11.2 MB）

- [PASS] `list_datasets` 8 ms — pattern='adl' 命中 15 个
- [PASS] `get_dataset_metadata` 14 ms — 24966 行 × 46 列
- [PASS] `summarize_dataset` 320 ms — 46 变量概要（全量）
- [PASS] `get_value_labels` 4 ms — 无值标签
- [PASS] `frequency` 4 ms — SEX: 2 取值 / 返回 2 / 分母 24966 / include_missing=False
- [PASS] `read_dataset` 4 ms — 返回 3 行 / 过滤后 24966 行 / offset=3050
- [PASS] `query_dataset` 107 ms — COUNT(*)=24966（元数据 24966 行）
- [PASS] `search_variables` 377 ms — RACEN: 命中 36 个数据集 / contains=True

**`sasdata/sdtm/sc.sas7bdat`**（`sasdata/sdtm`，128.0 KB）

- [PASS] `list_datasets` 4 ms — pattern='sc' 命中 5 个
- [PASS] `get_dataset_metadata` 5 ms — 254 行 × 14 列
- [PASS] `summarize_dataset` 9 ms — 6 变量概要（列子集）
- [PASS] `get_value_labels` 2 ms — 无值标签
- [PASS] `frequency` 5 ms — SCDTC: 207 取值 / 返回 10 / 分母 254 / include_missing=False
- [PASS] `read_dataset` 4 ms — 返回 3 行 / 过滤后 254 行 / offset=1
- [PASS] `query_dataset` 23 ms — COUNT(*)=254（元数据 254 行）
- [PASS] `search_variables` 352 ms — SCSTRESN: 命中 3 个数据集 / contains=True

**`sasdata/sdtm/supplb.sas7bdat`**（`sasdata/sdtm`，54.6 MB）

- [PASS] `list_datasets` 5 ms — pattern='sup' 命中 12 个
- [PASS] `get_dataset_metadata` 30 ms — 64403 行 × 10 列
- [PASS] `summarize_dataset` 284 ms — 5 变量概要（列子集）
- [PASS] `get_value_labels` 3 ms — 无值标签
- [PASS] `frequency` 10 ms — QVAL: 61 取值 / 返回 50 / 分母 64403 / include_missing=False
- [PASS] `read_dataset` 4 ms — 返回 3 行 / 过滤后 64403 行 / offset=4184
- [PASS] `query_dataset` 123 ms — 100 行结果（projection）
- [PASS] `search_variables` 386 ms — USUBJID: 命中 87 个数据集 / contains=False

**`sasdata/sdtm/sv.sas7bdat`**（`sasdata/sdtm`，384.0 KB）

- [PASS] `list_datasets` 5 ms — pattern='sv' 命中 3 个
- [PASS] `get_dataset_metadata` 4 ms — 3559 行 × 8 列
- [PASS] `summarize_dataset` 12 ms — 3 变量概要（列子集）
- [PASS] `get_value_labels` 3 ms — 无值标签
- [PASS] `frequency` 3 ms — VISIT: 36 取值 / 返回 10 / 分母 3559 / include_missing=False
- [PASS] `read_dataset` 4 ms — 返回 0 行 / 过滤后 2803 行 / offset=3335
- [PASS] `query_dataset` 26 ms — 20 行结果（projection）
- [PASS] `search_variables` 341 ms — VISITNUM: 命中 54 个数据集 / contains=True

**`sasdata_cn/adam/adsl.sas7bdat`**（`sasdata_cn/adam`，192.0 KB）

- [PASS] `list_datasets` 6 ms — pattern='ads' 命中 3 个
- [PASS] `get_dataset_metadata` 8 ms — 254 行 × 49 列
- [PASS] `summarize_dataset` 9 ms — 2 变量概要（列子集）
- [PASS] `get_value_labels` 3 ms — 无值标签
- [PASS] `frequency` 5 ms — AGEGR1N: 3 取值 / 返回 3 / 分母 254 / include_missing=False
- [PASS] `read_dataset` 3 ms — 返回 1 行 / 过滤后 254 行 / offset=171
- [PASS] `query_dataset` 28 ms — 5 行结果（projection）
- [PASS] `search_variables` 395 ms — COMP16FL: 命中 3 个数据集 / contains=True

**`sasdata_cn/adam/adlbcpv.sas7bdat`**（`sasdata_cn/adam`，16.0 MB）

- [PASS] `list_datasets` 5 ms — pattern='adl' 命中 15 个
- [PASS] `get_dataset_metadata` 15 ms — 37132 行 × 46 列
- [PASS] `summarize_dataset` 229 ms — 2 变量概要（列子集）
- [PASS] `get_value_labels` 2 ms — 无值标签
- [PASS] `frequency` 4 ms — PARAMN: 18 取值 / 返回 18 / 分母 37132 / include_missing=True
- [PASS] `read_dataset` 5 ms — 返回 10 行 / 过滤后 37132 行 / offset=27397
- [PASS] `query_dataset` 175 ms — 1 行结果（group_by）
- [PASS] `search_variables` 422 ms — R2A1HI: 命中 15 个数据集 / contains=True

**`sasdata_cn/adam/adlbh.sas7bdat`**（`sasdata_cn/adam`，9.9 MB）

- [PASS] `list_datasets` 4 ms — pattern='adl' 命中 15 个
- [PASS] `get_dataset_metadata` 10 ms — 24966 行 × 46 列
- [PASS] `summarize_dataset` 451 ms — 46 变量概要（全量）
- [PASS] `get_value_labels` 4 ms — 无值标签
- [PASS] `frequency` 7 ms — SEX: 2 取值 / 返回 2 / 分母 24966 / include_missing=False
- [PASS] `read_dataset` 4 ms — 返回 25 行 / 过滤后 24966 行 / offset=3395
- [PASS] `query_dataset` 141 ms — 20 行结果（projection）
- [PASS] `search_variables` 385 ms — LBSEQ: 命中 15 个数据集 / contains=False

**`sasdata_cn/sdtm/sc.sas7bdat`**（`sasdata_cn/sdtm`，128.0 KB）

- [PASS] `list_datasets` 5 ms — pattern='sc' 命中 5 个
- [PASS] `get_dataset_metadata` 4 ms — 254 行 × 14 列
- [PASS] `summarize_dataset` 20 ms — 14 变量概要（全量）
- [PASS] `get_value_labels` 3 ms — 无值标签
- [PASS] `frequency` 6 ms — SCSTRESN: 19 取值 / 返回 19 / 分母 254 / include_missing=True
- [PASS] `read_dataset` 7 ms — 返回 3 行 / 过滤后 254 行 / offset=22
- [PASS] `query_dataset` 24 ms — 5 行结果（projection）
- [PASS] `search_variables` 419 ms — SCCAT: 命中 3 个数据集 / contains=True

**`sasdata_cn/sdtm/supplb.sas7bdat`**（`sasdata_cn/sdtm`，54.6 MB）

- [PASS] `list_datasets` 6 ms — pattern='sup' 命中 12 个
- [PASS] `get_dataset_metadata` 38 ms — 64403 行 × 10 列
- [PASS] `summarize_dataset` 349 ms — 6 变量概要（列子集）
- [PASS] `get_value_labels` 2 ms — 无值标签
- [PASS] `frequency` 13 ms — QVAL: 61 取值 / 返回 50 / 分母 64403 / include_missing=False
- [PASS] `read_dataset` 7 ms — 返回 10 行 / 过滤后 64403 行 / offset=43487
- [PASS] `query_dataset` 149 ms — COUNT(*)=64403（元数据 64403 行）
- [PASS] `search_variables` 372 ms — STUDYID: 命中 102 个数据集 / contains=True

**`sasdata_cn/sdtm/ts.sas7bdat`**（`sasdata_cn/sdtm`，128.0 KB）

- [PASS] `list_datasets` 4 ms — pattern='ts' 命中 3 个
- [PASS] `get_dataset_metadata` 3 ms — 33 行 × 6 列
- [PASS] `summarize_dataset` 9 ms — 6 变量概要（全量）
- [PASS] `get_value_labels` 2 ms — 无值标签
- [PASS] `frequency` 4 ms — TSSEQ: 4 取值 / 返回 4 / 分母 33 / include_missing=False
- [PASS] `read_dataset` 3 ms — 返回 10 行 / 过滤后 33 行 / offset=7
- [PASS] `query_dataset` 27 ms — COUNT(*)=33（元数据 33 行）
- [PASS] `search_variables` 365 ms — STUDYID: 命中 102 个数据集 / contains=True

**`xpt/adam/adtte.xpt`**（`xpt/adam`，71.8 KB）

- [PASS] `list_datasets` 5 ms — pattern='adt' 命中 3 个
- [PASS] `get_dataset_metadata` 17 ms — 254 行 × 26 列
- [PASS] `summarize_dataset` 25 ms — 26 变量概要（全量）
- [PASS] `get_value_labels` 2 ms — 无值标签
- [PASS] `frequency` 4 ms — SEX: 2 取值 / 返回 2 / 分母 254 / include_missing=True
- [PASS] `read_dataset` 3 ms — 返回 25 行 / 过滤后 254 行 / offset=153
- [PASS] `query_dataset` 28 ms — 100 行结果（projection）
- [PASS] `search_variables` 340 ms — TRTSDT: 命中 36 个数据集 / contains=True

**`xpt/adam/adlbcpv.xpt`**（`xpt/adam`，15.8 MB）

- [PASS] `list_datasets` 5 ms — pattern='adl' 命中 15 个
- [PASS] `get_dataset_metadata` 586 ms — 37132 行 × 46 列
- [PASS] `summarize_dataset` 17 ms — 6 变量概要（列子集）
- [PASS] `get_value_labels` 3 ms — 无值标签
- [PASS] `frequency` 4 ms — COMP24FL: 2 取值 / 返回 2 / 分母 37132 / include_missing=True
- [PASS] `read_dataset` 6 ms — 返回 10 行 / 过滤后 37132 行 / offset=19184
- [PASS] `query_dataset` 160 ms — 1 行结果（group_by）
- [PASS] `search_variables` 357 ms — ALBTRVAL: 命中 12 个数据集 / contains=True

**`xpt/adam/adcibc.xpt`**（`xpt/adam`，283.7 KB）

- [PASS] `list_datasets` 4 ms — pattern='adc' 命中 1 个
- [PASS] `get_dataset_metadata` 29 ms — 730 行 × 36 列
- [PASS] `summarize_dataset` 7 ms — 4 变量概要（列子集）
- [PASS] `get_value_labels` 2 ms — 无值标签
- [PASS] `frequency` 2 ms — DTYPE: 2 取值 / 返回 2 / 分母 730 / include_missing=False
- [PASS] `read_dataset` 5 ms — 返回 3 行 / 过滤后 730 行 / offset=673
- [PASS] `query_dataset` 30 ms — 1 行结果（group_by）
- [PASS] `search_variables` 339 ms — ADT: 命中 30 个数据集 / contains=True

**`xpt/sdtm/suppds.xpt`**（`xpt/sdtm`，4.8 KB）

- [PASS] `list_datasets` 6 ms — pattern='sup' 命中 12 个
- [PASS] `get_dataset_metadata` 10 ms — 3 行 × 10 列
- [PASS] `summarize_dataset` 8 ms — 3 变量概要（列子集）
- [PASS] `get_value_labels` 4 ms — 无值标签
- [PASS] `frequency` 4 ms — QEVAL: 1 取值 / 返回 1 / 分母 3 / include_missing=False
- [PASS] `read_dataset` 4 ms — 返回 2 行 / 过滤后 3 行 / offset=1
- [PASS] `query_dataset` 27 ms — COUNT(*)=3（元数据 3 行）
- [PASS] `search_variables` 465 ms — QNAM: 命中 12 个数据集 / contains=False

**`xpt/sdtm/supplb.xpt`**（`xpt/sdtm`，54.1 MB）

- [PASS] `list_datasets` 8 ms — pattern='sup' 命中 12 个
- [PASS] `get_dataset_metadata` 1082 ms — 64403 行 × 10 列
- [PASS] `summarize_dataset` 162 ms — 4 变量概要（列子集）
- [PASS] `get_value_labels` 6 ms — 无值标签
- [PASS] `frequency` 23 ms — QLABEL: 2 取值 / 返回 2 / 分母 64403 / include_missing=False
- [PASS] `read_dataset` 16 ms — 返回 1 行 / 过滤后 64403 行 / offset=30747
- [PASS] `query_dataset` 237 ms — 5 行结果（projection）
- [PASS] `search_variables` 515 ms — QNAM: 命中 12 个数据集 / contains=True

**`xpt/sdtm/cm.xpt`**（`xpt/sdtm`，3.6 MB）

- [PASS] `list_datasets` 5 ms — pattern='cm' 命中 3 个
- [PASS] `get_dataset_metadata` 141 ms — 7510 行 × 21 列
- [PASS] `summarize_dataset` 28 ms — 5 变量概要（列子集）
- [PASS] `get_value_labels` 4 ms — 无值标签
- [PASS] `frequency` 7 ms — VISIT: 15 取值 / 返回 10 / 分母 7510 / include_missing=False
- [PASS] `read_dataset` 6 ms — 返回 3 行 / 过滤后 7510 行 / offset=3856
- [PASS] `query_dataset` 73 ms — 1 行结果（group_by）
- [PASS] `search_variables` 435 ms — CMSPID: 命中 3 个数据集 / contains=True

**跨数据集 JOIN**

- [PASS] `query_dataset(JOIN)` 449 ms — adsl ⋈ adlbcpv = 37132 行

**跨数据集 JOIN**

- [PASS] `query_dataset(JOIN)` 329 ms — sc ⋈ supplb = 64403 行

**跨数据集 JOIN**

- [PASS] `query_dataset(JOIN)` 480 ms — adsl ⋈ adlbcpv = 37132 行

**跨数据集 JOIN**

- [PASS] `query_dataset(JOIN)` 351 ms — sc ⋈ supplb = 64403 行

**跨数据集 JOIN**

- [PASS] `query_dataset(JOIN)` 1050 ms — adtte ⋈ adlbcpv = 37132 行

**跨数据集 JOIN**

- [PASS] `query_dataset(JOIN)` 1288 ms — suppds ⋈ supplb = 162 行

</details>

**汇总**

- 随机种子：`20261020`（`--seed` 可复现本次抽样）
- 覆盖类别：6 个（`sasdata/adam`、`sasdata/sdtm`、`sasdata_cn/adam`、`sasdata_cn/sdtm`、`xpt/adam`、`xpt/sdtm`）
- 抽检文件：18 个
- 工具调用：150 次，成功 150 次，失败 0 次
- 端到端耗时：18.8 s

全部用例通过（无失败明细）。

<!-- RANDOM-SMOKE-END -->

## 与原版 `sassoftware/sas-mcp-server` 的差异

上游 [sassoftware/sas-mcp-server](https://github.com/sassoftware/sas-mcp-server) 是连接
**SAS Viya** 的 MCP 服务器（v1.18.0：92 个工具、10 个可选 tier，覆盖执行 SAS 代码、AutoML、
模型打分等分析全生命周期）。本仓库沿用其 FastMCP 架构与 `register(mcp, ...)` 工具注册模式，
把数据源换成本地文件并大幅收敛范围——从"在 Viya 上干活"变成"把本地数据读给模型看"。

| 维度 | 原版 | 本仓库 |
| --- | --- | --- |
| 数据源 | Viya 平台（Compute / CAS / 模型 REST API） | 本地 `.sas7bdat` / `.xpt` 文件 |
| 认证 | OAuth2 PKCE 流程 + 本地 token 缓存 | 无需认证（无网络出口） |
| 运行形态 | HTTP（默认）、stdio、Docker、Kubernetes | 仅 stdio，由 MCP 客户端按需拉起 |
| 工具数量 | 92 个，按 10 个 tier 选择启用 | 8 个，全部只读（无写入 / 代码执行能力） |
| 代码组织 | `tools/` 下约 10 个模块 | 单个 `tools.py` + 核心 `dataset.py` |
| Python 版本 | `>= 3.12` | `>= 3.11` |
| 运行依赖 | `fastmcp`、`httpx`、`pydantic`、`uvicorn`、`anyio`、`python-dotenv` | `fastmcp`、`pandas`、`pyreadstat`、`duckdb` |
| 统计语义层 | 无——需自行执行 SAS / SQL 取数后再推断 | `summarize_dataset` / `frequency` 直接给出缺失率、唯一值数、按类型的分布指标，并自动套用 SAS 值标签 |
| SQL 能力 | 依赖 Viya / CAS | 内置 duckdb：仅允许单条 `SELECT`（支持 `WITH`），关闭外部文件与网络访问 |
| 结果限流 | — | 行预览（`SAS_LOCAL_MCP_MAX_ROWS`）与聚合结果（`SAS_LOCAL_MCP_MAX_AGG_ROWS`）**分开**限流，聚合基数不被预览上限截断 |
| 字符编码 | 由 Viya 侧决定 | `SAS_LOCAL_MCP_ENCODING`（中文数据常用 `utf-8` / `gbk`） |
| 解析缓存 | — | 按 `(路径, mtime, size, metadata_only)` 失效的 LRU 缓存，多轮问答不重复解析大文件 |
| 安全边界 | OAuth + Viya 侧权限体系 | 路径沙箱（越界即拒绝）+ 只读工具 + SQL 白名单 |
| 错误处理 | — | `DatasetError` 统一转 MCP `ToolError`，返回干净消息而不崩溃进程 |
| 格式覆盖 | — | `.sas7bcat` 仅能被 `list_datasets` 列出，pyreadstat 无对应读取器 |
| 测试数据 | — | 取自 R Consortium [submissions-pilot5](https://github.com/RConsortium/submissions-pilot5-datasetjson) 公开数据，派生为 XPORT / UTF-8 sas7bdat / GBK sas7bdat 三套（见[测试数据来源](#测试数据来源)） |

## 目录结构

```
sas-local-mcp/
├── pyproject.toml          # 依赖与构建配置
├── README.md
├── src/
│   └── sas_local_mcp/
│       ├── __init__.py
│       ├── __main__.py     # python -m sas_local_mcp 入口
│       ├── config.py       # 环境变量配置（数据根、两个行数上限、服务名）
│       ├── dataset.py      # 数据集读取 + 统计核心（pyreadstat + pandas 回退）
│       ├── stdio_server.py # FastMCP stdio 服务启动
│       └── tools.py        # MCP 工具注册（register_tools）
└── tests/
    ├── conftest.py         # 解析数据根（兼容根目录直放 / 分目录两种布局）
    ├── test_tools.py       # 单元测试：in-memory Client 逐个调用全部工具
    ├── test_regressions.py # 不依赖数据集的定点回归（注入 meta）
    ├── random_smoke.py     # 随机冒烟：stdio 子进程 + 各类别大小文件抽检
    ├── sasdata/            # 测试数据：英文 sdtm / adam
    ├── sasdata_cn/         # 测试数据：中文版（编码回归）
    └── xpt/                # 测试数据：XPORT 版（格式回归）
```

## 许可证

派生自 [sassoftware/sas-mcp-server](https://github.com/sassoftware/sas-mcp-server)（Apache-2.0）。