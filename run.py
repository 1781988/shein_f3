#!/usr/bin/env python3
"""一键入口：`python run.py` 跑全流程，`python run.py --self-check` 跑自检。

实际参数解析与编排在 src/cli.py；此文件只负责把项目根加入 sys.path。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
