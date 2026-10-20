# sas-local-mcp

一个让大模型（LLM）直接读取**本地 SAS 数据集**的 MCP 服务器。

改编自 [sassoftware/sas-mcp-server](https://github.com/sassoftware/sas-mcp-server)（Apache-2.0）：保留其
[FastMCP](https://github.com/jlowin/fastmcp) 架构与 `register(mcp, ...)` 工具注册模式，但把数据源从 SAS Viya 平台
换成本地文件——经 [pyreadstat](https://github.com/Roche/pyreadstat) 直接读取，**无需 Viya、无需联网、无需授权**。

适合的场景：

- 让 LLM 勘察 `/` 浏览本地 `.sas7bdat` / `.xpt` 数据集的结构与取值
- TLF（Tables, Listings, Figures）QC：快速做计数、频数、跨数据集关联核对
- 在 IDE / Agent 中把 SAS 数据当作可查询的上下文来源

所有工具均为**只读**，不会修改任何数据集文件。

## 功能特性

- 支持 SAS 两种常用格式：`.sas7bdat` 与传输格式 `.xpt`（XPORT）
- 路径相对 `SAS_DATA_ROOT` 或绝对路径传入，**越界路径会被拒绝**（安全沙箱）
- 读取时自动处理：缺失值（`NaN`→`null`）、日期/时间（`datetime`→ISO 字符串）、字符编码
- 可选套用 SAS 格式值标签（coded 值解码，便于 QC 阅读）
- 内置 duckdb SQL 查询，适合复杂计数与跨表关联
- 通过 MCP 标准协议以 stdio 方式接入任意兼容客户端（CodeBuddy、Claude Desktop、Cursor 等）

## 工具一览

所有工具路径参数 `path` 支持两种写法：

- 相对 `SAS_DATA_ROOT` 的路径，如 `02Sdtm/dm.sas7bdat`
- 绝对路径，但必须落在 `SAS_DATA_ROOT` 目录树内（否则被拒绝）

| 工具                     | 说明                                                                                                 | 关键参数                                                                 |
| ------------------------ | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `list_datasets`        | 递归列出 `SAS_DATA_ROOT` 下所有数据集文件（`.sas7bdat` / `.xpt` / `.sas7bcat`）              | `pattern`（文件名子串过滤，不区分大小写）、`limit`（默认 500）       |
| `get_dataset_metadata` | 行数、每变量 label/type/format/informat、文件标签、创建时间（不加载数据内容）                        | `path`                                                                 |
| `read_dataset`         | 读取前 N 行返回 JSON records（`NaN`→`null`，日期→ISO），支持列子集、`where` 过滤、套用值标签 | `path`、`limit`（默认 20）、`columns`、`where`、`apply_labels` |
| `get_value_labels`     | 变量的 SAS 格式值标签映射（QC 解码 coded 值用）                                                      | `path`                                                                 |
| `query_dataset`        | duckdb SQL 查询，数据注册为内存表 `dataset`（仅允许 `SELECT` / `WITH`）                        | `path`、`sql`、`limit`（默认 100）                                 |

### 各工具参数与返回

- **`list_datasets(pattern?, limit=500)`**
  - `where` 风格过滤见下；返回匹配文件清单（相对路径、大小、修改时间）。
- **`get_dataset_metadata(path)`**
  - 返回 `{ n_rows, file_label, created, variables: [{ name, label, type, format, informat }] }`。
- **`read_dataset(path, limit=20, columns?, where?, apply_labels=False)`**
  - `columns`：只返回指定变量列表。
  - `where`：pandas 查询表达式，如 `"AGE > 18"` 或 `"SEX == 'F'"`。
  - `apply_labels=True` 时把 coded 值替换为 SAS 值标签文本。
  - 返回 `{ columns, rows: [ {...}, ... ] }`，行数受 `SAS_LOCAL_MCP_MAX_ROWS` 上限约束。
- **`get_value_labels(path)`**
  - 返回 `{ variable: { code: label, ... }, ... }`。
- **`query_dataset(path, sql, limit=100)`**
  - SQL 中表名固定为 `dataset`，例如 `SELECT COUNT(*) AS n FROM dataset`。
  - 返回查询结果 records。

所有错误（文件不存在、格式损坏、路径越界、SQL 语法错误）都会被转成 MCP `ToolError` 以
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

| 变量                       | 默认值                  | 说明                                                              |
| -------------------------- | ----------------------- | ----------------------------------------------------------------- |
| `SAS_DATA_ROOT`          | 进程当前工作目录（CWD） | 数据根目录，所有数据集路径必须落在其内（沙箱边界）                |
| `SAS_LOCAL_MCP_MAX_ROWS` | `200`                 | 单次返回行数上限（`read_dataset` / `query_dataset` 受其约束） |
| `SAS_LOCAL_MCP_NAME`     | `sas-local-mcp`       | 服务名（显示在客户端）                                            |

> 提示：`SAS_DATA_ROOT` 支持 `~` 家目录展开；未设置时退化为启动进程的当前目录。

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

保存后重启客户端，即可在工具列表中看到 `sas-local-mcp` 提供的五个工具。

## 测试

```bash
# 安装测试依赖（若尚未）
pip install pytest pytest-asyncio     # 或 uv sync --extra dev / uv sync

# 运行全部测试
pytest
```

测试通过内存客户端挂载本服务，针对 `SAS_DATA_ROOT`（默认取当前目录）下的真实数据集
逐个调用全部工具；并覆盖以下边界：

- 损坏/截断文件读取报错处理
- 路径越界被拦截
- 日期 / 缺失值的序列化正确性
- `where` 过滤与 `apply_labels` 解码行为

## 与原版 `sassoftware/sas-mcp-server` 的差异

| 维度     | 原版                                    | 本仓库                                            |
| -------- | --------------------------------------- | ------------------------------------------------- |
| 数据源   | SAS Viya 平台（需 OAuth、联网）         | 本地文件（pyreadstat），无需 Viya                 |
| 依赖     | Viya REST SDK、telemetry                | pandas + pyreadstat + duckdb                      |
| 工具模块 | `tools/` 下约 10 个模块、含分层 tiers | 收敛为 1 个 `tools.py`，全部只读                |
| 附加能力 | UI views、landing page                  | 移除；新增 duckdb SQL 查询工具                    |
| 格式目录 | —                                      | `.sas7bcat` 仅能被列出，pyreadstat 无对应读取器 |

## 目录结构

```
sas-local-mcp/
├── pyproject.toml          # 依赖与构建配置
├── README.md
├── src/
│   └── sas_local_mcp/
│       ├── __init__.py
│       ├── __main__.py     # python -m sas_local_mcp 入口
│       ├── config.py       # 环境变量配置（SAS_DATA_ROOT / MAX_ROWS / NAME）
│       ├── dataset.py      # 数据集读取核心（pyreadstat + pandas 回退）
│       ├── stdio_server.py # FastMCP stdio 服务启动
│       └── tools.py        # MCP 工具注册（register_tools）
└── tests/
    └── test_tools.py
```

## 许可证

派生自 [sassoftware/sas-mcp-server](https://github.com/sassoftware/sas-mcp-server)（Apache-2.0）。
