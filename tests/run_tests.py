#!/usr/bin/env python3
"""测试汇总入口：等价于在项目根执行
``python -m unittest discover -s tests -p "test_*.py"``。

不引入 pytest，只用标准库 unittest；退出码 0 表示全部通过。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    """发现并运行 tests/ 下所有 test_*.py，返回进程退出码。"""
    start_dir = str(Path(__file__).resolve().parent)
    suite = unittest.defaultTestLoader.discover(
        start_dir=start_dir, pattern="test_*.py", top_level_dir=str(_PROJECT_ROOT)
    )
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print(f"\n用例总数={result.testsRun} 失败={len(result.failures)} "
          f"错误={len(result.errors)} 跳过={len(result.skipped)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
