"""字典序打分键、场景置换、近似 Pareto 非支配筛选。

五个层次统一「越大越好」：配满贡献、敏感品完全匹配、时效积压、主注入点、成本。
层间严格字典序，不做加权求和；换场景只置换层次顺序，不必重调权重。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from . import config as cfg
from .config import Scenario
from .domain import Candidate, Metrics, Waybill
from .injection import is_primary


def _layer_values(candidate: Candidate, waybill: Waybill) -> Dict[str, float]:
    """每个层次的「越大越好」标量值。"""
    fill = float(candidate.amount)
    sensitive = 1.0 if candidate.exact_sensitive_match else 0.0
    # 时效积压：优先消化库内等待更久的货（超 24h 的时长 × 可取方数）
    backlog = float(candidate.backlog_hours) * (candidate.amount / cfg.VOLUME_UNIT)
    injection = 1.0 if is_primary(candidate.injection) else 0.0
    # 成本越低越好 -> 取负号转成「越大越好」
    cost = -float(candidate.unit_cost) * (candidate.amount / cfg.VOLUME_UNIT)
    return {
        "fill": fill,
        "sensitive": sensitive,
        "backlog": backlog,
        "injection": injection,
        "cost": cost,
    }


def score_key(
    candidate: Candidate, waybill: Waybill, scenario: Scenario
) -> Tuple[float, ...]:
    """返回按场景优先顺序排列的字典序键，越大越好。"""
    values = _layer_values(candidate, waybill)
    return tuple(values[layer] for layer in scenario.priority_order)


def rank_key(
    candidate: Candidate,
    waybill: Waybill,
    scenario: Scenario,
) -> Tuple[Any, ...]:
    """把 score_key 转成可直接 min 的键（取负 + 稳定格键做末级裁决）。"""
    key = score_key(candidate, waybill, scenario)
    negated = tuple(-value for value in key)
    return (negated, candidate.cell.cell_key())


def waybill_priority_key(
    waybill: Waybill, scenario: Scenario, cost_rates: Any = None
) -> Tuple[Any, ...]:
    """提单的处理顺序键：按场景首层优先级 + 提单号，全程确定。

    - 配满优先：最晚提货时间早的先处理（避免到期未配）；
    - 成本优先：单公斤成本低的目的港先处理；
    - 时效优先：最早提货时间早的先处理；
    - 敏感品优先：等级高的提单先处理；
    - 注入点优先：非 SHJ 目的港（有主注入点）先处理。
    """
    first = scenario.priority_order[0] if scenario.priority_order else "fill"
    bill_no = waybill.bill_no
    latest = waybill.latest_pickup
    if first == "fill":
        return (0, latest, bill_no)
    if first == "sensitive":
        return (0, -int(waybill.level), latest, bill_no)
    if first == "backlog":
        return (0, waybill.earliest_pickup, latest, bill_no)
    if first == "injection":
        return (0 if waybill.dest_port != cfg.SHJ_PORT else 1, latest, bill_no)
    if first == "cost":
        rate = cost_rates.rate_for(waybill.dest_port) if cost_rates is not None else 0.0
        return (0, rate, latest, bill_no)
    return (0, latest, bill_no)


def objective_vector(metrics: Metrics) -> Tuple[float, ...]:
    """把七项指标里的五个目标转成统一的「越大越好」向量。"""
    return (
        float(metrics.fill_rate),
        float(metrics.exact_match_rate),
        -float(metrics.backlog_rate),
        float(metrics.primary_injection_rate),
        -float(metrics.cost),
    )


def dominates(a: Sequence[float], b: Sequence[float]) -> bool:
    """a 是否支配 b：各维不差且至少一维严格更好。"""
    if len(a) != len(b):
        raise ValueError("目标向量维度不一致")
    all_not_worse = all(x >= y for x, y in zip(a, b))
    any_better = any(x > y for x, y in zip(a, b))
    return all_not_worse and any_better


def pareto_filter(solutions: Iterable[Any]) -> List[str]:
    """近似 Pareto 非支配筛选，返回非支配解的场景名列表（保持稳定顺序）。

    ``solutions`` 元素可以是 ``(name, objective_vector)`` 二元组，
    也可以是带 ``scenario_name`` / ``metrics`` 属性的对象（T04 的实验结果）。
    """
    points: List[Tuple[str, Tuple[float, ...]]] = []
    for item in solutions:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            name, vector = item
            if hasattr(vector, "fill_rate"):
                vector = objective_vector(vector)
            points.append((str(name), tuple(float(v) for v in vector)))
            continue
        name = getattr(item, "scenario_name", None) or getattr(item, "name", None)
        metrics = getattr(item, "metrics", None)
        if name is None or metrics is None:
            raise ValueError("pareto_filter 元素需是 (name, vector) 或带 scenario_name/metrics")
        points.append((str(name), objective_vector(metrics)))

    non_dominated: List[str] = []
    for index, (name, vector) in enumerate(points):
        dominated = any(
            dominates(other_vector, vector)
            for other_index, (_, other_vector) in enumerate(points)
            if other_index != index
        )
        if not dominated:
            non_dominated.append(name)
    return non_dominated


__all__ = [
    "score_key", "rank_key", "waybill_priority_key", "objective_vector",
    "dominates", "pareto_filter",
]
