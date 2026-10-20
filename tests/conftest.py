"""为测试解析出真正的数据根，并让它在 config 读取前生效。

历史背景：测试数据曾直接放在 tests/ 根目录（只有 dm.sas7bdat / lb.sas7bdat），
test_tools.py 里因此出现了一批"目录下只有两个数据集"的硬编码断言。
后来数据按域与用途重新归置：

    tests/sasdata/sdtm/     英文 SDTM（23 个域）
    tests/sasdata/sdtm/../adam/
    tests/sasdata_cn/       中文版（编码回归用）
    tests/xpt/              XPORT 版（格式回归用）

本模块在 tests/ 下递归定位 dm.sas7bdat，取其所在目录作为 SAS_DATA_ROOT，
因此无论数据目录怎么搬动，`path: "dm.sas7bdat"` 这类相对写法都成立。

pytest 会在导入 test_tools.py 之前导入本模块，所以这里的环境变量一定早于
sas_local_mcp.config 的 import-time 读取。
"""

import os
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
# 主锚点文件：定位到它所在目录即视为数据根
ANCHOR = "dm.sas7bdat"
# 中文版 / XPORT 版是给编码与格式回归用的，普通用例优先取英文 sas7bdat 版
DEPRECATED_PARTS = ("sasdata_cn", "xpt")


def _locate_data_root() -> Path:
    # 跳过隐藏目录（.pytest_cache、.sas_data_root 等历史残留）
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
