"""看板用的聚合数据：只含汇总值，不含任何原始明细行。

``build(metrics, audit, diagnosis, experiments)`` 产出的 dict 直接写进
``results/dashboard_data.json``；看板模板只消费这些聚合字段。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import config as cfg
from .config import Config
from .domain import AuditReport, Metrics
from .metrics import INDICATOR_NAMES, metric_values


def _hour_timeline(hour_disposition: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """把逐小时去向台账压成看板可画的聚合点（24 小时一档，避免 480 点太密）。"""
    buckets: Dict[int, Dict[str, int]] = {}
    for row in hour_disposition:
        hour = int(row["hour_index"])
        day = hour // 24
        slot = buckets.setdefault(day, {"产出格": 0, "配出格": 0, "未配格": 0})
        slot["产出格"] += int(row["nonzero_cells"])
        slot["配出格"] += int(row["allocated_cells"])
        slot["未配格"] += int(row["unallocated_cells"])
    return [
        {"天序号": day, "产出格": value["产出格"],
         "配出格": value["配出格"], "未配格": value["未配格"]}
        for day, value in sorted(buckets.items())
    ]


def build(
    metrics: Metrics,
    audit: AuditReport,
    diagnosis: Sequence[Any],
    experiments: Mapping[str, Any],
    cfg_obj: Optional[Config] = None,
    auxiliary: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """汇总看板所需全部数值；不写入任何提单号、渠道 ID、原始记录。"""
    values = metric_values(metrics, cfg_obj) if cfg_obj is not None else {}
    cards = [
        {"名称": name, "数值": values.get(name, ""),
         "口径": metrics.formula_notes.get(name, "")}
        for name in INDICATOR_NAMES
    ]
    findings = [
        {
            "约束": f.constraint_id,
            "检查数": f.checked,
            "冲突数": f.conflicts,
        }
        for f in audit.findings
    ]
    reasons = Counter(r.primary_reason for r in diagnosis)
    reason_distribution = [
        {"原因": name, "提单数": count}
        for name, count in sorted(reasons.items())
    ]
    scenario_rows = experiments.get("scenario_rows", [])
    scale_rows = experiments.get("scale_rows", [])
    pareto = experiments.get("pareto", {})
    diagnostic = auxiliary or {}

    return {
        "标题": "多渠道货源与舱位提单动态配载 · 结果看板",
        "说明": "只含聚合值，不含资料包原始数据行与提单明细",
        "指标卡": cards,
        "约束审计": {
            "全部通过": audit.all_pass,
            "冲突总数": sum(f.conflicts for f in audit.findings),
            "逐条": findings,
        },
        "未配满原因分布": reason_distribution,
        "未配满提单数": sum(reasons.values()),
        "小时去向时间轴": _hour_timeline(
            experiments.get("hour_disposition", [])
        ),
        "场景对比": [
            {"场景": row.get("场景", ""), "配满率": row.get("提单配满率", ""),
             "时效积压率": row.get("时效积压率", ""), "成本": row.get("成本", "")}
            for row in scenario_rows
        ],
        "规模曲线": [
            {"规模": row.get("规模", ""), "方法": row.get("方法", ""),
             "配满率": row.get("配满率", ""), "时效积压率": row.get("时效积压率", ""),
             "成本": row.get("成本", ""), "耗时秒": row.get("耗时秒", "")}
            for row in scale_rows
        ],
        "非支配采样点": pareto.get("non_dominated", []),
        "近似前沿说明": pareto.get("note", ""),
        "辅助量": {
            "评估范围记录数": diagnostic.get("a_record_count", 0),
            "全量记录数": diagnostic.get("total_record_count", 0),
            "评估范围配出方数": f"{diagnostic.get('sum_a_m3', 0.0):.4f}",
            "评估范围产出方数": f"{diagnostic.get('eval_range_supply_m3', 0.0):.4f}",
            "全量货源方数": f"{diagnostic.get('total_supply_m3', 0.0):.4f}",
            "完全虚仓张数": diagnostic.get("fully_virtual_count", 0),
            "密度缺失笔数": diagnostic.get("density_missing_records", 0),
        },
        "评估范围": {
            "开始": cfg_obj.eval_range.start.strftime(cfg.HOUR_FORMAT)
            if cfg_obj is not None else cfg.EVAL_START_STR,
            "结束": cfg_obj.eval_range.end_inclusive.strftime(cfg.HOUR_FORMAT)
            if cfg_obj is not None else cfg.EVAL_END_INCLUSIVE_STR,
            "统计口径": "全 480 小时货源都参与配载；货源方数类指标只统计该范围内的配货记录集合 A；配满率与虚仓提单数量是提单侧口径，按完整配载结果与全部 2652 张提单统计",
        },
    }


__all__ = ["build"]
