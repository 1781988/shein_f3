"""把 dashboard_data.json 的聚合数据注入单页模板，输出 results/dashboard.html。

模板里 ``__DASHBOARD_DATA__`` 是唯一占位符；数据只含聚合值，页面离线可开。
模板缺失时给出带提示的页面，不产出空白页。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from src import config as cfg

PLACEHOLDER = "__DASHBOARD_DATA__"


def build(
    data: Mapping[str, Any],
    template: Path,
    output: Path,
    cfg_obj: Any = None,
) -> Path:
    """注入数据并写 HTML；返回实际写出的路径。"""
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(data), ensure_ascii=False)
    source = Path(template)
    if source.is_file():
        html = source.read_text(encoding=cfg.CSV_ENCODING).replace(PLACEHOLDER, payload)
    else:
        html = (
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>配载结果看板</title></head><body>"
            "<p>模板缺失，无法渲染看板；聚合数据见 results/dashboard_data.json。</p>"
            "</body></html>"
        )
    if PLACEHOLDER in html:
        html = html.replace(PLACEHOLDER, payload)
    target.write_text(html, encoding=cfg.CSV_ENCODING)
    return target


__all__ = ["build", "PLACEHOLDER"]
