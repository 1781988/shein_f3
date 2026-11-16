"""未配满原因链条：原因枚举受白名单约束，附证据字段。

诊断只依据五条硬约束与「货源已配尽」这一事实，绝不把软目标当原因，
也绝不出现 config/constraints.json 黑名单里列出的任何被排除规则；
``assert_no_excluded_rules`` 会在输出前逐条扫描关键字并报错。
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .config import Config
from .domain import Dataset, DiagnosisRecord, Solution, Waybill
from .errors import DataValidationError
from .precision import int_to_vol_str
from .time_axis import TimeAxis

# 白名单里的原因按「从整体到局部」排列，用于构造原因链条
REASON_ALL_SUPPLY_USED = "R1_全量可用货源已配尽"
REASON_NO_VISIBLE = "R2_可见窗口内无剩余可配货"
REASON_NO_LEVEL = "R3_敏感品等级无兼容货源"
REASON_NO_WAREHOUSE = "R4_单子仓约束下无兼容货源"
REASON_NO_TIME = "R5_提货时间窗内无兼容货源"
REASON_PLAN_CAP = "R6_提单计划体积封顶后仍有缺口且无兼容货源"


def _unallocated_stats(
    cells: Sequence[Dict[str, Any]], waybill: Waybill
) -> Dict[str, int]:
    """按硬约束逐层统计剩余未配格数量（只数与五条硬约束有关的量）。"""
    level_ok = 0
    time_ok = 0
    warehouse_ok = 0
    all_ok = 0
    for item in cells:
        cell_level = int(item["level"])
        if cell_level > int(waybill.level):
            continue
        level_ok += 1
        hour_index = int(item["hour_index"])
        available_at = waybill.latest_pickup
        if available_at is None:
            continue
        # 用 hour_index 反推可配时刻：由 solution 的 axis 提供，
        # 这里用调用方预置的 _available_at 映射避免重复解析。
        cell_time = item.get("_available_at")
        if cell_time is not None and cell_time > available_at:
            continue
        time_ok += 1
        locked = waybill.locked_warehouse
        if locked and str(item["warehouse_id"]) != locked:
            continue
        warehouse_ok += 1
        all_ok += 1
    return {
        "level_ok": level_ok,
        "time_ok": time_ok,
        "warehouse_ok": warehouse_ok,
        "all_ok": all_ok,
        "total": len(cells),
    }


def _reason_chain(
    stats: Dict[str, int], gap: int, plan: int, allowed: Sequence[str]
) -> List[str]:
    """按白名单构造原因链条，首项为首要原因。"""
    chain: List[str] = []
    if stats["total"] == 0:
        chain.append(REASON_ALL_SUPPLY_USED)
        return _only_allowed(chain, allowed)
    if stats["level_ok"] == 0:
        chain.append(REASON_NO_LEVEL)
    elif stats["time_ok"] == 0:
        chain.append(REASON_NO_TIME)
    elif stats["warehouse_ok"] == 0:
        chain.append(REASON_NO_WAREHOUSE)
    elif stats["all_ok"] == 0:
        chain.append(REASON_NO_VISIBLE)
    else:
        chain.append(REASON_PLAN_CAP)
    if stats["total"] > 0 and stats["all_ok"] == 0 and REASON_NO_VISIBLE not in chain:
        chain.append(REASON_NO_VISIBLE)
    return _only_allowed(chain, allowed)


def _only_allowed(chain: List[str], allowed: Sequence[str]) -> List[str]:
    """丢弃不在白名单里的原因；若全被丢弃则回退到白名单首项。"""
    whitelist = list(allowed)
    kept = [item for item in chain if item in whitelist]
    if not kept:
        return [whitelist[0] if whitelist else REASON_ALL_SUPPLY_USED]
    return kept


def _evidence(
    waybill: Waybill, allocated: int, gap: int, stats: Dict[str, int]
) -> str:
    """证据只用硬约束相关的事实数字，不带任何被排除规则或软目标字样。"""
    return (
        f"计划方数={int_to_vol_str(waybill.plan_volume)};"
        f"已配方数={int_to_vol_str(allocated)};"
        f"缺口方数={int_to_vol_str(gap)};"
        f"剩余未配格数={stats['total']};"
        f"等级兼容格数={stats['level_ok']};"
        f"时间窗内格数={stats['time_ok']};"
        f"同子仓格数={stats['warehouse_ok']};"
        f"全部兼容格数={stats['all_ok']}"
    )


def assert_no_excluded_rules(text: str, cfg_obj: Config) -> None:
    """确保文本里搜不到六类被排除规则；发现即报错而不是静默放行。"""
    for keyword in cfg_obj.excluded_rule_keywords():
        if keyword and keyword in text:
            raise DataValidationError(
                "诊断输出出现被排除规则",
                location="diagnosis",
                detail=f"关键字 {keyword!r} 不允许出现在诊断里",
            )


def validate_diagnosis(
    records: Sequence[DiagnosisRecord], cfg_obj: Config
) -> None:
    """校验每条诊断：原因只在白名单内，全文搜不到被排除规则。"""
    allowed = list(cfg_obj.allowed_reasons())
    for record in records:
        parts = record.reason_chain.split(" -> ")
        for reason in parts:
            if reason not in allowed:
                raise DataValidationError(
                    "诊断原因不在白名单内",
                    location=record.bill_no, detail=reason,
                )
        if record.primary_reason not in allowed:
            raise DataValidationError(
                "诊断首要原因不在白名单内",
                location=record.bill_no, detail=record.primary_reason,
            )
        combined = " ".join([
            record.bill_no, record.primary_reason, record.reason_chain, record.evidence,
        ])
        assert_no_excluded_rules(combined, cfg_obj)


def diagnose(
    solution: Solution, dataset: Dataset, cfg_obj: Config
) -> List[DiagnosisRecord]:
    """为所有未配满提单生成原因链条，返回按提单号升序的稳定列表。"""
    axis = dataset.axis if dataset.axis is not None else TimeAxis.from_config(cfg_obj)
    cells: List[Dict[str, Any]] = []
    for item in solution.diagnostics.unallocated_cells:
        enriched = dict(item)
        enriched["_available_at"] = axis.available_at(int(item["hour_index"]))
        cells.append(enriched)

    allowed = list(cfg_obj.allowed_reasons())
    records: List[DiagnosisRecord] = []
    for bill_no in sorted(solution.waybill_state.keys()):
        waybill = solution.waybill_state[bill_no]
        allocated = waybill.allocated()
        gap = waybill.plan_volume - allocated
        if gap <= 0:
            continue
        stats = _unallocated_stats(cells, waybill)
        chain = _reason_chain(stats, gap, waybill.plan_volume, allowed)
        records.append(DiagnosisRecord(
            bill_no=bill_no,
            gap_volume=gap,
            primary_reason=chain[0],
            reason_chain=" -> ".join(chain),
            evidence=_evidence(waybill, allocated, gap, stats),
        ))
    validate_diagnosis(records, cfg_obj)
    return records


__all__ = [
    "diagnose", "validate_diagnosis", "assert_no_excluded_rules",
    "REASON_ALL_SUPPLY_USED", "REASON_NO_VISIBLE", "REASON_NO_LEVEL",
    "REASON_NO_WAREHOUSE", "REASON_NO_TIME", "REASON_PLAN_CAP",
]
