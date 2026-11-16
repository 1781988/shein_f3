"""主 / 备注入点判定。

用「渠道注入点」字段（10 个值）匹配提单目的港代码，不是 250 个渠道 ID。
注入点匹配是软目标，不进硬约束集合，也不进诊断排除项。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, Tuple

from . import config as cfg


class InjectionKind(str, Enum):
    """注入点分类。"""

    PRIMARY = cfg.INJECTION_PRIMARY
    ALTERNATIVE = cfg.INJECTION_ALTERNATIVE
    SHJ_ALT = cfg.INJECTION_SHJ_ALT


def classify(dest_port: str, channel_injection: str) -> InjectionKind:
    """判定某渠道相对某提单是主注入点、备选，还是 SHJ 全部渠道备选。

    - 目的港为 SHJ：所有渠道都算备选，无主注入点；
    - 目的港 == 渠道注入点：主注入点；
    - 其余：备选。
    """
    dest = str(dest_port).strip()
    channel = str(channel_injection).strip()
    if dest == cfg.SHJ_PORT:
        return InjectionKind.SHJ_ALT
    if channel == dest:
        return InjectionKind.PRIMARY
    return InjectionKind.ALTERNATIVE


def is_primary(kind: InjectionKind) -> bool:
    """是否主注入点（SHJ_ALT 不算主）。"""
    return kind is InjectionKind.PRIMARY


def primary_score(kind: InjectionKind) -> int:
    """主注入点打分层：主=1，其余=0（越大越好）。"""
    return 1 if is_primary(kind) else 0


def build_injection_index(rows: Iterable[Any]) -> Dict[Tuple[str, str], str]:
    """按「渠道 × 子仓」这一行建立渠道注入点反查索引。"""
    index: Dict[Tuple[str, str], str] = {}
    for row in rows:
        channel = str(getattr(row, "channel_id"))
        warehouse = str(getattr(row, "warehouse_id"))
        key = (channel, warehouse)
        if key not in index:
            index[key] = str(getattr(row, "channel_injection"))
    return index


__all__ = [
    "InjectionKind", "classify", "is_primary", "primary_score",
    "build_injection_index",
]
