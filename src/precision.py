"""体积整数化、四位小数格式化、成本舍入、时间串解析与输出。

体积在内存里一律是 int，单位 0.0001 立方米（config.VOLUME_UNIT）。
读入用 Decimal 精确换算，避免二进制浮点误差；输出用固定四位小数字符串。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional, Union

from . import config as cfg

NumberLike = Union[int, float, str, Decimal]

_QUANT_UNIT = Decimal(cfg.VOLUME_UNIT)
_ONE = Decimal(1)
_VOLUME_QUANT = Decimal(1).scaleb(-cfg.VOLUME_DECIMALS)  # 0.0001
_COST_QUANT = Decimal(1).scaleb(-cfg.COST_DECIMALS)      # 0.01


def vol_to_int(x: NumberLike) -> int:
    """把四位小数体积换算成整数单位（0.0001 立方米）。

    None / 空串当作 0；负数、NaN、inf、非数值串抛 ValueError，
    由 loader 捕获后转成带 (行号, 列名, 原值) 的 DataValidationError。
    """
    if x is None:
        return 0
    if isinstance(x, str) and not x.strip():
        return 0
    if isinstance(x, bool):
        raise ValueError(f"体积不能是布尔值：{x!r}")
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        raise ValueError(f"体积不能是 NaN 或 inf：{x!r}")
    try:
        dec = Decimal(str(x).strip()) if isinstance(x, (str, float)) else Decimal(x)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"体积不是合法数值：{x!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"体积不能是 NaN 或 inf：{x!r}")
    if dec < 0:
        raise ValueError(f"体积不能为负：{x!r}")
    scaled = (dec * _QUANT_UNIT).quantize(_ONE, rounding=ROUND_HALF_UP)
    return int(scaled)


def int_to_vol_str(v: int) -> str:
    """整数体积格式化成固定四位小数字符串，如 86993 -> "8.6993"。"""
    dec = (Decimal(int(v)) / _QUANT_UNIT).quantize(_VOLUME_QUANT, rounding=ROUND_HALF_UP)
    return f"{dec:.{cfg.VOLUME_DECIMALS}f}"


def int_to_vol_float(v: int) -> float:
    """整数体积转成 float，仅用于展示与统计拟合，不参与守恒判定。"""
    return int(v) / cfg.VOLUME_UNIT


def format_cost(x: float) -> str:
    """成本保留两位小数，四舍五入。"""
    dec = Decimal(str(x)).quantize(_COST_QUANT, rounding=ROUND_HALF_UP)
    return f"{dec:.{cfg.COST_DECIMALS}f}"


def parse_time(s: Any) -> datetime:
    """严格按 YYYY-MM-DD HH:MM:SS 解析时间串；已是 datetime 则原样返回。"""
    if isinstance(s, datetime):
        return s
    if s is None:
        raise ValueError("时间为空")
    text = str(s).strip()
    try:
        return datetime.strptime(text, cfg.TIME_FORMAT)
    except ValueError as exc:
        raise ValueError(
            f"时间串格式不符，要求 {cfg.TIME_FORMAT}，实际 {text!r}"
        ) from exc


def format_time(dt: datetime) -> str:
    """输出 YYYY-MM-DD HH:MM:SS（提货时刻列）。"""
    return dt.strftime(cfg.TIME_FORMAT)


def parse_hour(s: Any) -> datetime:
    """严格按 YYYY-MM-DD HH:MM 解析产出小时左端时刻。"""
    if isinstance(s, datetime):
        return s
    text = str(s).strip()
    try:
        return datetime.strptime(text, cfg.HOUR_FORMAT)
    except ValueError as exc:
        raise ValueError(
            f"产出小时格式不符，要求 {cfg.HOUR_FORMAT}，实际 {text!r}"
        ) from exc


def format_hour(dt: datetime) -> str:
    """输出 YYYY-MM-DD HH:MM（产出小时列，无秒）。"""
    return dt.strftime(cfg.HOUR_FORMAT)


def parse_date(s: Any) -> date:
    """按 YYYY-MM-DD 解析日期（密度表封箱日期）。"""
    if isinstance(s, datetime):
        return s.date()
    text = str(s).strip()
    try:
        return datetime.strptime(text, cfg.DATE_FORMAT).date()
    except ValueError as exc:
        raise ValueError(
            f"日期格式不符，要求 {cfg.DATE_FORMAT}，实际 {text!r}"
        ) from exc


def format_date(d: date) -> str:
    """输出 YYYY-MM-DD。"""
    return d.strftime(cfg.DATE_FORMAT)


def parse_hour_column_left(column_name: str) -> datetime:
    """从宽表小时列名 ``YYYY-MM-DD HH:MM-HH:MM`` 取左端时刻。

    左端固定 16 个字符，右侧 ``-HH:MM`` 是可配时刻的右端，不参与解析。
    """
    text = str(column_name).strip()
    left = text[: cfg.HOUR_COLUMN_LEFT_LEN]
    return parse_hour(left)


def hour_column_name(left: datetime) -> str:
    """由左端时刻重建宽表小时列名，如 2026-10-01 23:00 -> "…-23:00-24:00"。"""
    right_hour = left.hour + 1
    right = f"{right_hour:02d}:00"
    return f"{format_hour(left)}-{right}"


def is_bad_volume(x: Any) -> Optional[str]:
    """返回体积非法的原因字符串；合法返回 None。供 loader 逐格校验。"""
    if x is None or (isinstance(x, str) and not x.strip()):
        return None
    try:
        vol_to_int(x)
    except ValueError as exc:
        return str(exc)
    return None


def within_volume_tolerance(summed_int: int, original_int: int) -> bool:
    """体积守恒判定：整数差值绝对值不超过 config.VOLUME_TOLERANCE_INT 对应的容差。"""
    return abs(summed_int - original_int) <= cfg.VOLUME_TOLERANCE_INT


def round_int_div(numerator: int, denominator: int) -> int:
    """整数四舍五入除法，用于按比例抽样，避免依赖浮点。"""
    if denominator <= 0:
        raise ValueError("分母必须为正整数")
    return (2 * numerator + denominator) // (2 * denominator)


def declared_total_to_int(x: NumberLike) -> int:
    """把「合计体积」列（两位小数）换算成整数单位，用于差异核对。

    该列是 480 小时列之和四舍五入到两位的结果，只做核对，不作守恒基准。
    """
    value = vol_to_int(x)
    unit = cfg.VOLUME_UNIT // 100
    return ((value + unit // 2) // unit) * unit


__all__ = [
    "vol_to_int", "int_to_vol_str", "int_to_vol_float", "format_cost",
    "parse_time", "format_time", "parse_hour", "format_hour",
    "parse_date", "format_date", "parse_hour_column_left", "hour_column_name",
    "is_bad_volume", "within_volume_tolerance", "round_int_div",
]
