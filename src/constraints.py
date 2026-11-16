"""五条硬约束的构造期检查纯函数，供求解器与基准共用。

五条：C1 货源体积守恒（逐格）、C2 单提单配货 ≤ 计划体积、
C3 单提单只提一个子仓、C4 敏感品等级兼容、C5 提货时间窗。
黑名单里的六类被排除规则在这层完全不出现，也不作为过滤条件；
注入点匹配是软目标，也不在这里。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Tuple

from . import config as cfg
from .domain import SupplyCell, Waybill
from .sensitivity import is_compatible

C1 = "C1"
C2 = "C2"
C3 = "C3"
C4 = "C4"
C5 = "C5"
CONSTRAINT_IDS: Tuple[str, ...] = (C1, C2, C3, C4, C5)

CONSTRAINT_NAMES = {
    C1: "货源体积守恒",
    C2: "单提单配货不超过计划体积",
    C3: "单提单只提一个子仓",
    C4: "敏感品等级兼容",
    C5: "提货时间窗",
}


def check_cell_cap(cell: SupplyCell, amount: int) -> bool:
    """C1：单格取量必须是正数且不超过该格剩余体积（整数精确）。"""
    return amount > 0 and amount <= cell.remaining_volume


def check_volume_cap(waybill: Waybill, amount: int) -> bool:
    """C2：本次取量后，单提单累计配货不超过计划体积。"""
    return amount > 0 and waybill.allocated() + amount <= waybill.plan_volume


def check_single_warehouse(waybill: Waybill, cell: SupplyCell) -> bool:
    """C3：单提单只提一个子仓；首次分配时锁定，之后所有行同子仓。"""
    locked = waybill.locked_warehouse
    return locked == "" or locked == cell.warehouse_id


def check_sensitive(waybill: Waybill, cell: SupplyCell) -> bool:
    """C4：货等级 ≤ 提单等级（高等级提单可配本级及以下等级的货）。"""
    return is_compatible(cell.level, waybill.level)


def check_time_window(
    waybill: Waybill, cell: SupplyCell, pickup_time: Optional[datetime] = None
) -> bool:
    """C5：该批货的可配时刻不晚于提单最晚提货时间（闭区间含边界）。

    传入 ``pickup_time`` 时额外校验 ``最早提货时间 <= 提货时刻 <= 最晚提货时间``
    且 ``提货时刻 >= 该批货的可配时刻``。计划起飞时间不参与过滤。
    """
    if cell.available_at is None or waybill.latest_pickup is None:
        return False
    if cell.available_at > waybill.latest_pickup:
        return False
    if pickup_time is None:
        return True
    if waybill.earliest_pickup is None:
        return False
    return (
        waybill.earliest_pickup <= pickup_time <= waybill.latest_pickup
        and pickup_time >= cell.available_at
    )


def check_all(
    waybill: Waybill,
    cell: SupplyCell,
    amount: int,
    pickup_time: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """一次性检查五条硬约束，返回 (是否通过, 首个冲突的约束编号)。

    供求解器与基准在每次 take 前调用；审计模块另有一套独立实现。
    """
    if not check_cell_cap(cell, amount):
        return (False, C1)
    if not check_volume_cap(waybill, amount):
        return (False, C2)
    if not check_single_warehouse(waybill, cell):
        return (False, C3)
    if not check_sensitive(waybill, cell):
        return (False, C4)
    if not check_time_window(waybill, cell, pickup_time=pickup_time):
        return (False, C5)
    return (True, "")


def verify_record_time(record: Any, waybill: Waybill, cell: SupplyCell) -> bool:
    """审计用的单条时间窗复核（含提货时刻字段）。"""
    try:
        pickup = datetime.strptime(record.pickup_time_str, cfg.TIME_FORMAT)
    except (ValueError, TypeError):
        return False
    return check_time_window(waybill, cell, pickup_time=pickup)


__all__ = [
    "C1", "C2", "C3", "C4", "C5", "CONSTRAINT_IDS", "CONSTRAINT_NAMES",
    "check_cell_cap", "check_volume_cap", "check_single_warehouse",
    "check_sensitive", "check_time_window", "check_all", "verify_record_time",
]
