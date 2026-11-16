"""七项指标计算。

统计口径按评估范围分三类，与 ``05_假设清单.md`` B1/B2、``06_指标口径.md`` 一致：
全 480 小时货源都参与配载、明细全量输出；货源方数类指标（敏感品完全匹配率、
时效积压率、主注入点选择率、成本、未配提单货量）只统计 10.3—10.17 的记录
集合 A（产出小时 ∈ 2026-10-03 00:00—2026-10-17 23:00 的配货记录）；
配满率与虚仓提单数量是提单侧口径，按完整配载结果与全部 2652 张提单统计，
不受 A 限制。

三项口径需要单独说明，避免字面误读：
- 「未配提单货量」是供给侧剩余，即 R 内总产出减去 R 内配出方数，
  不是提单缺口；提单缺口由「虚仓提单数量」与「配满率」反映。
- 「配满率」与「虚仓提单数量」按提单整体配载结果判定（该提单从全部
  480 小时货源获得的方数 Q(b)，统计对象为全部提单），因为这两项衡量的是
  提单是否被填满；05/06 文档里另给「评估范围口径」的辅助量。
- 「敏感品完全匹配率 / 时效积压率 / 主注入点选择率 / 成本」严格只统计集合 A。

``compute_from_rows`` 从 allocation.csv 的行 + 重新只读加载的原始表复算，
供自检 V4 使用；两条路径共用同一套 ``_Rec`` 与 ``_compute`` 公式，避免漂移。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import config as cfg
from .config import Config
from .domain import Metrics, Solution
from .injection import classify
from .precision import (
    format_cost,
    format_date,
    int_to_vol_str,
    parse_time,
    vol_to_int,
)
from .time_axis import TimeAxis

# 七项指标的固定名称与顺序。csv_writer.write_indicators 与自检 V4 共用。
INDICATOR_NAMES: Tuple[str, ...] = (
    "提单配满率",
    "敏感品属性完全匹配率",
    "时效积压率",
    "主注入点选择率",
    "成本",
    "未配提单货量",
    "虚仓提单数量",
)

# 虚仓判定容差：小于计划体积 0.0001 立方米（1 个整数单位）才算配满。
VIRTUAL_GAP_INT: int = 1


@dataclass(frozen=True)
class _Rec:
    """一条归一化的配货记录；整数体积、小时索引、提货时刻都解析好了。"""

    bill_no: str
    channel_id: str
    warehouse_id: str
    hour_index: int
    hour_label: str
    pickup_time: datetime
    volume: int


@dataclass
class _Reference:
    """从原始表只读反查出来的复算所需字段。"""

    axis: TimeAxis
    waybills: Dict[str, Any]
    sensitive_by_cw: Dict[Tuple[str, str], int]
    injection_by_cw: Dict[Tuple[str, str], str]
    density_by_cwd: Dict[Tuple[str, str, str], Optional[float]]
    cost_by_port: Dict[str, float]
    cell_volume: Dict[Tuple[str, str, int], int]
    eval_range_supply_volume: int
    total_supply_volume: int
    all_bill_numbers: List[str]


def build_reference(dataset: Any, cfg_obj: Config) -> _Reference:
    """把 Dataset（由 loader 只读解析原始 xlsx 得到）整理成反查结构。"""
    axis = dataset.axis if dataset.axis is not None else TimeAxis.from_config(cfg_obj)
    cell_volume: Dict[Tuple[str, str, int], int] = {}
    for row in dataset.supply_rows:
        for hour_index, volume in enumerate(row.hourly_volumes):
            if volume > 0:
                cell_volume[(row.channel_id, row.warehouse_id, hour_index)] = volume
    sensitive = {
        key: int(level) for key, level in dataset.sensitive_by_cw.items()
    }
    return _Reference(
        axis=axis,
        waybills={w.bill_no: w for w in dataset.waybills},
        sensitive_by_cw=sensitive,
        injection_by_cw=dict(dataset.injection_by_cw),
        density_by_cwd=dict(dataset.density_by_cwd),
        cost_by_port=dict(dataset.cost_rates.by_port),
        cell_volume=cell_volume,
        eval_range_supply_volume=dataset.eval_range_supply_volume,
        total_supply_volume=dataset.total_supply_volume,
        all_bill_numbers=sorted(w.bill_no for w in dataset.waybills),
    )


def _normalize_solution(solution: Solution, axis: TimeAxis) -> List[_Rec]:
    """把 Solution.records 归一化；不信任记录上的 in_eval_range 派生字段。"""
    records: List[_Rec] = []
    for record in solution.records:
        hour_index = axis.hour_index_of_label(record.hour_label)
        records.append(_Rec(
            bill_no=record.bill_no,
            channel_id=record.channel_id,
            warehouse_id=record.warehouse_id,
            hour_index=hour_index,
            hour_label=record.hour_label,
            pickup_time=parse_time(record.pickup_time_str),
            volume=record.volume,
        ))
    return records


def _normalize_rows(rows: Sequence[Mapping[str, str]], axis: TimeAxis) -> List[_Rec]:
    """把 CSV 行（六列字符串）归一化，用于从 allocation.csv 复算。"""
    records: List[_Rec] = []
    for row in rows:
        label = str(row[cfg.ALLOCATION_COLUMNS[3]]).strip()
        records.append(_Rec(
            bill_no=str(row[cfg.ALLOCATION_COLUMNS[0]]).strip(),
            channel_id=str(row[cfg.ALLOCATION_COLUMNS[1]]).strip(),
            warehouse_id=str(row[cfg.ALLOCATION_COLUMNS[2]]).strip(),
            hour_index=axis.hour_index_of_label(label),
            hour_label=label,
            pickup_time=parse_time(row[cfg.ALLOCATION_COLUMNS[4]]),
            volume=vol_to_int(row[cfg.ALLOCATION_COLUMNS[5]]),
        ))
    return records


def _formula_notes(cfg_obj: Config, auxiliary: Mapping[str, Any]) -> Dict[str, str]:
    """生成 indicators.csv 的「口径说明」列，逐项写清公式、分母与统计范围。"""
    start = cfg_obj.eval_range.start.strftime(cfg.HOUR_FORMAT)
    end = cfg_obj.eval_range.end_inclusive.strftime(cfg.HOUR_FORMAT)
    scope = f"评估范围记录集合 A（产出小时 {start} ~ {end}，共 {cfg.EVAL_HOURS} 小时）"
    return {
        "提单配满率": (
            f"Σ_b min(Q(b), plan(b)) / Σ_b plan(b)，b 取全部提单，Q(b) 为该提单从"
            f"全 480 小时货源配到的方数（完整配载结果）；plan 取提单表计划体积。"
            f"提单侧口径，衡量提单被填满的程度；评估范围口径的辅助值见 06 文档。"
        ),
        "敏感品属性完全匹配率": (
            f"Σ_{{等级相同}} q / Σ_A q，方数口径，只统计 {scope} 的配货记录；"
            f"货等级按「渠道 × 子仓」这一行取，提单等级按舱位可走敏感品类型取最高层级，"
            f"相等为完全匹配。"
        ),
        "时效积压率": (
            f"Σ_r max(0, W_r − {cfg_obj.free_backlog_hours}h) × q_r / "
            f"(Σ_A q × {cfg.EVAL_HOURS}h)，W 为库内等待时长 = 提货时刻 − 该批货可配时刻；"
            f"只统计 {scope} 的记录。超 24h 配货方数另作辅助量。"
        ),
        "主注入点选择率": (
            f"主注入点方数 / Σ_A q，只统计 {scope} 的记录；"
            f"渠道注入点 == 提单目的港代码为主注入点，SHJ 目的港全部算备选。"
        ),
        "成本": (
            f"Σ_r (q_r / {cfg.VOLUME_UNIT}) × 密度 × 单kg成本，按提单目的港代码取成本表值，"
            f"密度按 (渠道ID, 子仓ID, 产出日) 从密度表取；密度为空记 0 并统计缺失笔数，"
            f"包机整张计 0，杂费三列不计入；只统计 {scope} 的记录，单位元。"
        ),
        "未配提单货量": (
            "R 内总产出 - R 内配出方数，单位立方米；这是供给侧剩余、没配到提单上的"
            "货源方数，不是提单缺口。提单缺口由「虚仓提单数量」与「配满率」反映。"
            "该值为 0 与虚仓提单数量大于 0 并不矛盾：提单计划体积合计大于全量货源，"
            "评估范围内产出的货源已全部配出，但仍有提单没填满。"
        ),
        "虚仓提单数量": (
            f"全部提单中 Q(b) < plan(b) − 0.0001 立方米的张数，Q(b) 为该提单从全 480 小时"
            f"货源获得的方数（完整配载结果），含完全没配到的 Q(b)=0；"
            f"完全虚仓张数另作辅助量。按整体配载状态判定，与「未配提单货量」口径不同。"
        ),
    }


def _compute(
    records: Sequence[_Rec], reference: _Reference, cfg_obj: Config
) -> Tuple[Metrics, Dict[str, Any]]:
    """同一套公式：既可跑 Solution，也可跑 CSV 复算。"""
    axis = reference.axis
    volume_unit = cfg.VOLUME_UNIT

    # 集合 A：产出小时落在评估范围内的记录
    set_a = [r for r in records if axis.is_in_eval_range(r.hour_index)]
    sum_a = sum(r.volume for r in set_a)

    # 提单整体配载方数 Q(b)（全 480 小时，全部记录）
    q_all: Dict[str, int] = {}
    for record in records:
        q_all[record.bill_no] = q_all.get(record.bill_no, 0) + record.volume

    # 评估范围内每张提单获得的方数 Q_R(b) 与 B_R
    q_eval: Dict[str, int] = {}
    for record in set_a:
        q_eval[record.bill_no] = q_eval.get(record.bill_no, 0) + record.volume

    # 配满率 / 虚仓：全部提单，整体配载结果
    plan_sum = 0
    filled_sum = 0
    virtual = 0
    fully_virtual = 0
    for bill_no in reference.all_bill_numbers:
        waybill = reference.waybills.get(bill_no)
        if waybill is None:
            continue
        plan = waybill.plan_volume
        got = q_all.get(bill_no, 0)
        plan_sum += plan
        filled_sum += got if got < plan else plan
        if got < plan - VIRTUAL_GAP_INT:
            virtual += 1
            if got == 0:
                fully_virtual += 1
    fill_rate = filled_sum / plan_sum if plan_sum else 0.0

    # 评估范围口径的辅助量（严格只取集合 A）
    plan_eval_sum = sum(
        reference.waybills[b].plan_volume for b in q_eval if b in reference.waybills
    )
    filled_eval_sum = sum(
        min(q, reference.waybills[b].plan_volume)
        for b, q in q_eval.items() if b in reference.waybills
    )
    fill_rate_eval = filled_eval_sum / plan_eval_sum if plan_eval_sum else 0.0
    virtual_eval = sum(
        1 for b in q_eval
        if b in reference.waybills
        and q_eval[b] < reference.waybills[b].plan_volume - VIRTUAL_GAP_INT
    )

    # 逐条集合 A 的软目标量
    exact_volume = 0
    primary_volume = 0
    backlog_weighted = 0.0
    over24_volume = 0
    cost_total = 0.0
    density_missing_records = 0
    for record in set_a:
        waybill = reference.waybills.get(record.bill_no)
        if waybill is None:
            continue
        cargo_level = reference.sensitive_by_cw.get(
            (record.channel_id, record.warehouse_id), cfg.LEVEL_NONE
        )
        if cargo_level == int(waybill.level):
            exact_volume += record.volume
        injection = classify(
            waybill.dest_port,
            reference.injection_by_cw.get((record.channel_id, record.warehouse_id), ""),
        )
        if cfg.INJECTION_PRIMARY == injection.value:
            primary_volume += record.volume

        available_at = axis.available_at(record.hour_index)
        waiting_hours = (record.pickup_time - available_at).total_seconds() / 3600.0
        over = max(0.0, waiting_hours - cfg_obj.free_backlog_hours)
        backlog_weighted += over * record.volume
        if over > 0:
            over24_volume += record.volume

        # 成本：包机整张计 0；密度为空记 0；杂费不计入
        if waybill.billing_type != cfg.BILLING_CHARTER:
            pack_date = format_date(axis.left_at(record.hour_index).date())
            density = reference.density_by_cwd.get(
                (record.channel_id, record.warehouse_id, pack_date)
            )
            if density is None:
                density_missing_records += 1
            else:
                rate = float(reference.cost_by_port.get(waybill.dest_port, 0.0))
                cost_total += (record.volume / volume_unit) * float(density) * rate

    exact_match_rate = exact_volume / sum_a if sum_a else 0.0
    primary_injection_rate = primary_volume / sum_a if sum_a else 0.0
    backlog_rate = (
        backlog_weighted / (sum_a * cfg.EVAL_HOURS) if sum_a else 0.0
    )
    unallocated_volume = reference.eval_range_supply_volume - sum_a

    auxiliary: Dict[str, Any] = {
        "a_record_count": len(set_a),
        "total_record_count": len(records),
        "sum_a_volume": sum_a,
        "sum_a_m3": sum_a / volume_unit,
        "eval_range_supply_m3": reference.eval_range_supply_volume / volume_unit,
        "total_supply_m3": reference.total_supply_volume / volume_unit,
        "plan_sum_m3": plan_sum / volume_unit,
        "total_waybills": len(reference.all_bill_numbers),
        "b_r_size": len(q_eval),
        "fill_rate_eval_scope": fill_rate_eval,
        "virtual_eval_scope": virtual_eval,
        "fully_virtual_count": fully_virtual,
        "over24_volume_m3": over24_volume / volume_unit,
        "density_missing_records": density_missing_records,
        "virtual_warehouse_count": virtual,
    }
    metrics = Metrics(
        fill_rate=fill_rate,
        exact_match_rate=exact_match_rate,
        backlog_rate=backlog_rate,
        primary_injection_rate=primary_injection_rate,
        cost=cost_total,
        unallocated_volume=unallocated_volume,
        virtual_warehouse_count=virtual,
        formula_notes=_formula_notes(cfg_obj, auxiliary),
    )
    return metrics, auxiliary


def compute(
    solution: Solution, dataset: Any, cfg_obj: Config
) -> Tuple[Metrics, Dict[str, Any]]:
    """主入口：从 Solution 与 Dataset 计算七项指标，另返回辅助量供报告使用。"""
    reference = build_reference(dataset, cfg_obj)
    records = _normalize_solution(solution, reference.axis)
    return _compute(records, reference, cfg_obj)


def compute_from_rows(
    rows: Sequence[Mapping[str, str]], dataset: Any, cfg_obj: Config
) -> Tuple[Metrics, Dict[str, Any]]:
    """从 allocation.csv 的六列字符串 + 重新只读加载的原始表复算七项指标。"""
    reference = build_reference(dataset, cfg_obj)
    records = _normalize_rows(rows, reference.axis)
    return _compute(records, reference, cfg_obj)


def metric_values(metrics: Metrics, cfg_obj: Config) -> Dict[str, str]:
    """把七项指标格式化成 indicators.csv 的「数值」列字符串。"""
    return {
        "提单配满率": f"{metrics.fill_rate:.6f}",
        "敏感品属性完全匹配率": f"{metrics.exact_match_rate:.6f}",
        "时效积压率": f"{metrics.backlog_rate:.6f}",
        "主注入点选择率": f"{metrics.primary_injection_rate:.6f}",
        "成本": format_cost(metrics.cost),
        "未配提单货量": int_to_vol_str(metrics.unallocated_volume),
        "虚仓提单数量": str(metrics.virtual_warehouse_count),
    }


__all__ = [
    "INDICATOR_NAMES", "VIRTUAL_GAP_INT", "build_reference", "compute",
    "compute_from_rows", "metric_values",
]
