"""场景组、规模档、贪心对照、改进量与近似 Pareto 采样点。

规模抽样只用 ``random.Random(seed)`` 从「渠道 × 子仓行 + 提单 + 小时列」
确定性取子集，不引入外部数据；主求解链路本身不用随机数。
每档都跑主方案（默认场景）与独立实现的贪心基准，记录配满率 / 时效 / 成本 / 耗时。
"""
from __future__ import annotations

import copy
import random
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import config as cfg
from .baseline import fcfs_baseline
from .config import Config, Scenario
from .domain import Dataset, Metrics, ScaleRow, Solution
from .metrics import compute, metric_values
from .precision import format_cost
from .scoring import objective_vector, pareto_filter
from .solver import RollingSolver


def _scenario_rows(
    metrics_by_name: Mapping[str, Metrics], cfg_obj: Config
) -> List[Dict[str, Any]]:
    """把各场景七项指标整理成 metrics_scenarios.csv 的行。"""
    rows: List[Dict[str, Any]] = []
    for scenario in cfg_obj.active_scenarios():
        metrics = metrics_by_name.get(scenario.name)
        if metrics is None:
            continue
        row: Dict[str, Any] = {"场景": scenario.name}
        row.update(metric_values(metrics, cfg_obj))
        rows.append(row)
    return rows


def run_scenarios(
    cfg_obj: Config,
    dataset: Dataset,
    solutions: Optional[Mapping[str, Solution]] = None,
) -> Tuple[Dict[str, Metrics], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """每个场景计算七项指标，返回 (指标表, 辅助量表, 场景行)。

    ``solutions`` 由 pipeline 传入已经跑好的解，避免重复求解；
    不传时自行按场景求解（便于单独重跑）。
    """
    supplied = solutions or {}
    metrics_by_name: Dict[str, Metrics] = {}
    aux_by_name: Dict[str, Dict[str, Any]] = {}
    for scenario in cfg_obj.active_scenarios():
        solution = supplied.get(scenario.name)
        if solution is None:
            solution = RollingSolver(cfg_obj, dataset).solve(scenario)
        metrics, auxiliary = compute(solution, dataset, cfg_obj)
        metrics_by_name[scenario.name] = metrics
        aux_by_name[scenario.name] = auxiliary
    return metrics_by_name, aux_by_name, _scenario_rows(metrics_by_name, cfg_obj)


def pareto_points(
    metrics_by_name: Mapping[str, Metrics], cfg_obj: Config
) -> Dict[str, Any]:
    """近似 Pareto：把三个采样解的五维目标向量做非支配筛选。"""
    ordered = [
        (scenario.name, metrics_by_name[scenario.name])
        for scenario in cfg_obj.active_scenarios()
        if scenario.name in metrics_by_name
    ]
    vectors = [
        (name, objective_vector(metrics)) for name, metrics in ordered
    ]
    names = pareto_filter(vectors)
    return {
        "points": [
            {"场景": name, "目标向量": [round(v, 6) for v in vector]}
            for name, vector in vectors
        ],
        "non_dominated": names,
        "note": "启发式采样出的几个点，不是真实 Pareto 前沿；只给这几个解之间的非支配关系",
    }


def sample_dataset(
    dataset: Dataset, cfg_obj: Config, scale: float, seed: int
) -> Dataset:
    """按规模确定性抽样：渠道 × 子仓行、提单、小时列各取一个子集。

    抽样后重算反查索引与总量，保证求解器拿到的是自洽的小实例。
    """
    rng = random.Random(seed)
    rows_all = list(dataset.supply_rows)
    bills_all = list(dataset.waybills)
    hours_all = list(range(cfg.HOURS))

    n_rows = max(1, int(round(len(rows_all) * scale)))
    n_bills = max(1, int(round(len(bills_all) * scale)))
    n_hours = max(1, int(round(len(hours_all) * scale)))

    chosen_rows = sorted(rng.sample(range(len(rows_all)), n_rows))
    chosen_bills = sorted(rng.sample(range(len(bills_all)), n_bills))
    chosen_hours = set(rng.sample(hours_all, n_hours))

    sampled: Dataset = copy.copy(dataset)
    sampled.supply_rows = []
    sampled.waybills = [copy.copy(bills_all[i]) for i in chosen_bills]
    for wb in sampled.waybills:
        wb.records = []
        wb.locked_warehouse = ""
        wb.locked = False
        wb.pickup_time = None

    for new_index, old_index in enumerate(chosen_rows):
        original = rows_all[old_index]
        row = copy.copy(original)
        row.hourly_volumes = [
            volume if hour in chosen_hours else 0
            for hour, volume in enumerate(original.hourly_volumes)
        ]
        row.row_index = new_index
        row.exact_sum_int = sum(row.hourly_volumes)
        sampled.supply_rows.append(row)

    sampled.sensitive_by_cw = {
        (r.channel_id, r.warehouse_id): r.level for r in sampled.supply_rows
    }
    sampled.injection_by_cw = {
        (r.channel_id, r.warehouse_id): r.channel_injection for r in sampled.supply_rows
    }
    sampled.hour_column_names = list(dataset.hour_column_names)
    sampled.issues = list(dataset.issues)
    sampled.source_hashes = dict(dataset.source_hashes)
    sampled.total_supply_volume = sum(
        sum(r.hourly_volumes) for r in sampled.supply_rows
    )
    sampled.eval_range_supply_volume = sum(
        r.hourly_volumes[h] for r in sampled.supply_rows
        for h in dataset.axis.eval_hour_indices()
    )
    sampled.nonzero_cells = sum(
        1 for r in sampled.supply_rows for v in r.hourly_volumes if v > 0
    )
    sampled.densities = list(dataset.densities)
    sampled.density_by_cwd = dict(dataset.density_by_cwd)
    return sampled


def _scale_row(
    scale: float, method: str, metrics: Metrics, elapsed: float, records: int
) -> ScaleRow:
    """构造一行规模实验记录。"""
    return ScaleRow(
        scale=scale,
        method=method,
        fill_rate=metrics.fill_rate,
        backlog_rate=metrics.backlog_rate,
        cost=metrics.cost,
        elapsed_seconds=elapsed,
        record_count=records,
    )


def run_scales(
    cfg_obj: Config,
    dataset: Dataset,
    scenario: Optional[Scenario] = None,
    logger: Any = None,
) -> List[ScaleRow]:
    """每个规模档跑主方案与贪心基准，返回 6 行 ScaleRow。"""
    chosen = scenario or cfg_obj.active_scenarios()[0]
    rows: List[ScaleRow] = []
    for scale in cfg_obj.scales:
        sampled = sample_dataset(dataset, cfg_obj, scale, cfg_obj.seed)

        started = time.perf_counter()
        main_solution = RollingSolver(cfg_obj, sampled).solve(chosen)
        main_elapsed = time.perf_counter() - started
        main_metrics, _ = compute(main_solution, sampled, cfg_obj)
        rows.append(_scale_row(
            scale, "主方案", main_metrics, main_elapsed, len(main_solution.records)
        ))

        started = time.perf_counter()
        base_solution = fcfs_baseline(cfg_obj, sampled)
        base_elapsed = time.perf_counter() - started
        base_metrics, _ = compute(base_solution, sampled, cfg_obj)
        rows.append(_scale_row(
            scale, "贪心基准", base_metrics, base_elapsed, len(base_solution.records)
        ))
        if logger is not None:
            logger.info(
                "规模 %.2f 完成 主方案配满率=%.6f 耗时=%.2fs 基准配满率=%.6f 耗时=%.2fs",
                scale, main_metrics.fill_rate, main_elapsed,
                base_metrics.fill_rate, base_elapsed,
            )
    return rows


def compute_improvement(
    main: Metrics, baseline: Metrics, cfg_obj: Config
) -> List[Dict[str, Any]]:
    """主方案相对贪心基准的改进量；成本与积压率按「越小越好」调整符号。"""
    pairs: List[Tuple[str, float, float, bool]] = [
        ("提单配满率", baseline.fill_rate, main.fill_rate, True),
        ("敏感品属性完全匹配率", baseline.exact_match_rate, main.exact_match_rate, True),
        ("时效积压率", baseline.backlog_rate, main.backlog_rate, False),
        ("主注入点选择率", baseline.primary_injection_rate,
         main.primary_injection_rate, True),
        ("成本", baseline.cost, main.cost, False),
        ("未配提单货量", baseline.unallocated_volume / cfg.VOLUME_UNIT,
         main.unallocated_volume / cfg.VOLUME_UNIT, False),
        ("虚仓提单数量", baseline.virtual_warehouse_count,
         main.virtual_warehouse_count, False),
    ]
    rows: List[Dict[str, Any]] = []
    for name, base, value, higher_better in pairs:
        delta = value - base
        improvement = delta if higher_better else -delta
        rate = improvement / abs(base) if base else 0.0
        rows.append({
            "指标": name,
            "基准值": f"{base:.6f}",
            "本方案值": f"{value:.6f}",
            "改进量": f"{improvement:.6f}",
            "改进率": f"{rate:.6f}",
        })
    return rows


def scale_rows_for_csv(rows: Sequence[ScaleRow]) -> List[Dict[str, Any]]:
    """把 ScaleRow 转成 scale_experiments.csv 的行。"""
    return [
        {
            "规模": f"{row.scale:.2f}",
            "方法": row.method,
            "配满率": f"{row.fill_rate:.6f}",
            "时效积压率": f"{row.backlog_rate:.6f}",
            "成本": format_cost(row.cost),
            "耗时秒": f"{row.elapsed_seconds:.3f}",
            "记录数": row.record_count,
        }
        for row in rows
    ]


__all__ = [
    "run_scenarios", "pareto_points", "sample_dataset", "run_scales",
    "compute_improvement", "scale_rows_for_csv",
]
