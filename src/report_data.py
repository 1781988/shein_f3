"""报告汇总数值：把全流程产生的指标、审计、实验、哈希整理成 report_data.json。

PDF / HTML 都从这一个 dict 取数，避免报告正文与结果文件口径漂移。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from . import config as cfg
from .config import Config
from .domain import AuditReport, Metrics
from .metrics import INDICATOR_NAMES, metric_values


def build(
    cfg_obj: Config,
    metrics: Metrics,
    auxiliary: Mapping[str, Any],
    audit: AuditReport,
    diagnosis_count: int,
    scenario_rows: Sequence[Mapping[str, Any]],
    scale_rows: Sequence[Mapping[str, Any]],
    improvement: Sequence[Mapping[str, Any]],
    pareto: Mapping[str, Any],
    hashes: Mapping[str, str],
    manifest: Mapping[str, Any],
    dataset_stats: Mapping[str, Any],
) -> Dict[str, Any]:
    """汇总报告所需全部数值，返回可 JSON 序列化的 dict。"""
    indicator_values = metric_values(metrics, cfg_obj)
    return {
        "项目名": "f3_load_allocation",
        "版本": "1.0",
        "运行环境": {
            "语言": "Python 3.13.14",
            "必需依赖": "openpyxl 3.1.5",
            "可选依赖": "numpy 2.5.4 / matplotlib 3.11.2 / fpdf2 2.9.0",
        },
        "配置": {
            "随机种子": cfg_obj.seed,
            "评估范围": [
                cfg_obj.eval_range.start.strftime(cfg.HOUR_FORMAT),
                cfg_obj.eval_range.end_inclusive.strftime(cfg.HOUR_FORMAT),
            ],
            "场景": [s.name for s in cfg_obj.active_scenarios()],
            "规模档": list(cfg_obj.scales),
            "体积单位": f"整数单位 = 0.0001 立方米（volume_unit={cfg.VOLUME_UNIT}）",
        },
        "数据事实": dict(dataset_stats),
        "七项指标": {
            name: {
                "数值": indicator_values[name],
                "口径说明": metrics.formula_notes.get(name, ""),
            }
            for name in INDICATOR_NAMES
        },
        "辅助量": dict(auxiliary),
        "约束审计": {
            "全部通过": audit.all_pass,
            "冲突总数": sum(f.conflicts for f in audit.findings),
            "逐条": [
                {"约束": f.constraint_id, "检查数": f.checked, "冲突数": f.conflicts}
                for f in audit.findings
            ],
        },
        "未配满提单数": diagnosis_count,
        "场景对比": [dict(row) for row in scenario_rows],
        "规模实验": [dict(row) for row in scale_rows],
        "基准改进量": [dict(row) for row in improvement],
        "近似Pareto": dict(pareto),
        "复现哈希": dict(hashes),
        "生成状态": dict(manifest),
        "合规": {
            "原始数据": "只读引用，未复制进项目；公开产出不含原始数据行",
            "单位": "报告正文一律写「立方米」或 m3",
            "队伍信息": "队名北方一号、成员高铭禹、指导老师刘环宇、投入14小时、AI费用WorkBuddy积分3100、作品与代码仓库https://github.com/1781988/shein_f3.git 均由用户提供并已填入说明单与协作记录；个人独立开发，无多人协作",
        },
    }


def write(data: Mapping[str, Any], path: Path) -> Path:
    """把汇总数值写成 results/report_data.json。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding=cfg.CSV_ENCODING) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return target


__all__ = ["build", "write"]
