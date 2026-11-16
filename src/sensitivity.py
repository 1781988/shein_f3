"""敏感等级映射表、多层逗号串取最高、兼容判定、完全匹配判定。

本模块是「字符串 -> 等级」映射逻辑的唯一来源；等级数值与映射表本身
集中在 config.py（SENSITIVITY_TOKEN_LEVELS / LEVEL_*），源码里不散落字面量。
等级按「渠道 × 子仓」这一行取值，不按渠道 ID 建对照表。
"""
from __future__ import annotations

import re
from enum import IntEnum
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config as cfg


class SensitivityLevel(IntEnum):
    """敏感品等级，数值越大层级越高。"""

    NONE = cfg.LEVEL_NONE
    SENSITIVE_GENERAL = cfg.LEVEL_SENSITIVE_GENERAL
    DRY = cfg.LEVEL_DRY
    LITHIUM = cfg.LEVEL_LITHIUM


_SEPARATOR_RE = re.compile(
    "[" + "".join(re.escape(ch) for ch in cfg.SENSITIVITY_SEPARATORS) + "]"
)


def split_tokens(raw: Any) -> List[str]:
    """把多层级逗号串拆成去空白的 token 列表。"""
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    return [part.strip() for part in _SEPARATOR_RE.split(text) if part.strip()]


def token_level(token: str) -> Optional[int]:
    """单个 token 的等级；未登记返回 None。全角/半角括号统一按字符比对。"""
    normalized = token.strip()
    if normalized in cfg.SENSITIVITY_TOKEN_LEVELS:
        return cfg.SENSITIVITY_TOKEN_LEVELS[normalized]
    # 兼容全角括号与半角括号的等价写法
    folded = normalized.replace("（", "(").replace("）", ")")
    if folded in cfg.SENSITIVITY_TOKEN_LEVELS:
        return cfg.SENSITIVITY_TOKEN_LEVELS[folded]
    return None


def parse_level(raw: Any) -> SensitivityLevel:
    """解析敏感品原始串，多层级逗号串取最高层级。

    备注写法「普货」映射到「敏感普货」（等级 1）；「无（普货）」是等级 0。
    未登记 token 按等级 0 处理，由 validate 记录到 data_issues.csv。
    """
    best = cfg.LEVEL_NONE
    for token in split_tokens(raw):
        level = token_level(token)
        if level is not None and level > best:
            best = level
    return SensitivityLevel(best)


def unknown_tokens(raw: Any) -> List[str]:
    """返回串里未登记的 token，供校验层写 data_issues.csv。"""
    return [t for t in split_tokens(raw) if token_level(t) is None]


def level_name(level: SensitivityLevel) -> str:
    """等级对应的中文名。"""
    return cfg.LEVEL_NAMES.get(int(level), str(level))


def _coerce_level(value: Any) -> SensitivityLevel:
    """把枚举、整数、字符串或带 .level 的对象统一成 SensitivityLevel。"""
    if isinstance(value, SensitivityLevel):
        return value
    if hasattr(value, "level"):
        return _coerce_level(getattr(value, "level"))
    if isinstance(value, int) and not isinstance(value, bool):
        return SensitivityLevel(value)
    if isinstance(value, str):
        try:
            return SensitivityLevel(int(value))
        except ValueError:
            return parse_level(value)
    return parse_level(value)


def is_compatible(cargo: Any, waybill: Any) -> bool:
    """兼容判定：货等级 <= 提单等级（高等级提单可配本级及以下等级的货）。"""
    return int(_coerce_level(cargo)) <= int(_coerce_level(waybill))


def is_exact_match(cargo: Any, waybill: Any) -> bool:
    """完全匹配判定：货等级 == 提单等级（软目标，不是硬约束）。"""
    return int(_coerce_level(cargo)) == int(_coerce_level(waybill))


def build_row_level_index(rows: Iterable[Any]) -> Dict[Tuple[str, str], SensitivityLevel]:
    """按「渠道 × 子仓」这一行建立敏感等级索引。

    同一渠道在不同子仓可以不同；后出现的同键行不会覆盖首次结果，
    由 loader 的重复键校验负责报告冲突。
    """
    index: Dict[Tuple[str, str], SensitivityLevel] = {}
    for row in rows:
        channel = str(getattr(row, "channel_id"))
        warehouse = str(getattr(row, "warehouse_id"))
        key = (channel, warehouse)
        if key not in index:
            index[key] = parse_level(getattr(row, "sensitive_raw"))
    return index


__all__ = [
    "SensitivityLevel", "split_tokens", "token_level", "parse_level",
    "unknown_tokens", "level_name", "is_compatible", "is_exact_match",
    "build_row_level_index",
]
