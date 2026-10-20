"""测试环境准备：让 tests/ 直接从源码目录运行，并解析出真正的数据根。

1) `sys.path`：若 `sas_local_mcp` 尚未安装，把 `src/` 加进 `sys.path`，
   这样不必先 `pip install -e .` 也能跑测试（已安装时这步无副作用）。
2) `SAS_DATA_ROOT`：在 `tests/` 下递归定位 `dm.sas7bdat`，取其所在目录作为数据根，
   兼容「tests/ 根目录直放」与「tests/sasdata/sdtm/ 分目录」两种布局，
   因此 `path: "dm.sas7bdat"` 这类相对写法在数据目录搬动后依然成立。

pytest 会在导入 `test_tools.py` 之前导入本模块，所以这里的环境变量
一定早于 `sas_local_mcp.config` 的 import-time 读取。
"""

import os
import sys
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
SRC = TESTS_ROOT.parent / "src"

# 1) 源码目录可直接导入
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 2) 数据根：锚定 dm.sas7bdat 所在目录
ANCHOR = "dm.sas7bdat"
# 中文版 / XPORT 版是给编码与格式回归用的，普通用例优先取英文 sas7bdat 版
DEPRECATED_PARTS = ("sasdata_cn", "xpt")


def _locate_data_root() -> Path:
    # 跳过隐藏目录（.pytest_cache 等历史残留）
    hits = sorted(
        p for p in TESTS_ROOT.rglob(ANCHOR)
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(TESTS_ROOT).parts)
    )
    if not hits:
        raise RuntimeError(f"在 {TESTS_ROOT} 下找不到测试数据 {ANCHOR}，请检查数据布局")
    for hit in hits:
        if not any(part in hit.parent.parts for part in DEPRECATED_PARTS):
            return hit.parent
    return hits[0].parent


SAS_DATA_ROOT = _locate_data_root()
# 显式设置的 SAS_DATA_ROOT 优先，便于临时把测试指向仓库外的数据
os.environ.setdefault("SAS_DATA_ROOT", str(SAS_DATA_ROOT))
