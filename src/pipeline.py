"""全流程编排：python run.py（默认 all）与 python run.py --self-check 的共同入口。

顺序：只读加载 → 校验 → 三场景求解 → 七项指标 → 独立审计 → 未配满诊断 →
CSV/JSON 落盘 → 场景/规模实验 → 曲线 → 看板 → 报告 → build_manifest.json。
所有产出都写进 cfg.results_path()；生成期失败不中断主流程，如实记进 manifest。
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Mapping

from . import config as cfg
from .audit import recheck_from_csv, write_report_json
from .baseline import fcfs_baseline
from .config import Config
from .csv_writer import (
    write_allocation,
    write_baseline_comparison,
    write_diagnosis,
    write_indicators,
    write_scale_experiments,
    write_scenarios,
)
from .dashboard_data import build as build_dashboard_data
from .diagnose import diagnose
from .experiments import (
    compute_improvement,
    pareto_points,
    run_scales,
    run_scenarios,
    scale_rows_for_csv,
)
from .loader import load_all
from .logging_util import get_logger
from .metrics import compute
from .plots import plot_scale_curves
from .report_data import build as build_report_data
from .report_data import write as write_report_data
from .solver import RollingSolver
from .validate import validate_dataset, write_data_issues

logger = get_logger()


def _result_path(cfg_obj: Config, name: str) -> Path:
    """按约定文件名在结果目录里定位一个产出。"""
    return cfg_obj.results_path() / name


def sha256_file(path: Path) -> str:
    """计算单个结果文件的 SHA-256。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(cfg_obj: Config) -> Any:
    """只读加载并校验五张表，输出 data_issues.csv。"""
    dataset = load_all(cfg_obj)
    validate_dataset(dataset, cfg_obj)
    write_data_issues(dataset.issues, _result_path(cfg_obj, "data_issues.csv"), cfg_obj)
    logger.info(
        "加载完成 货源行=%d 提单=%d 密度行=%d 目的港成本=%d 非零格=%d "
        "全量货源=%s 立方米 评估范围货源=%s 立方米",
        len(dataset.supply_rows), len(dataset.waybills), len(dataset.densities),
        len(dataset.cost_rates.by_port), dataset.nonzero_cells,
        dataset.total_supply_volume / cfg.VOLUME_UNIT,
        dataset.eval_range_supply_volume / cfg.VOLUME_UNIT,
    )
    return dataset


def _build_dashboard(cfg_obj: Config, data: Mapping[str, Any], manifest: Dict[str, Any]) -> None:
    """把聚合数据注入模板，生成 results/dashboard.html。"""
    import importlib

    try:
        module = importlib.import_module("dashboard.build_dashboard")
        output = module.build(
            data=data,
            template=cfg.PROJECT_ROOT / "dashboard" / "template.html",
            output=_result_path(cfg_obj, "dashboard.html"),
            cfg_obj=cfg_obj,
        )
        manifest["dashboard"] = {
            "status": "generated",
            "path": str(output),
            "note": "单页静态 HTML + 内联 JS + SVG，离线双击可开，只含聚合值",
        }
        logger.info("看板已生成 %s", output)
    except Exception as exc:  # noqa: BLE001 - 生成期失败非致命，如实记录
        manifest["dashboard"] = {
            "status": "failed", "reason": repr(exc),
            "fallback": "dashboard_data.json 仍可用",
        }
        logger.error("看板生成失败：%s", exc)


def _build_report(cfg_obj: Config, data: Mapping[str, Any], manifest: Dict[str, Any]) -> None:
    """调用报告生成器，结果状态写进 manifest。"""
    import importlib

    try:
        module = importlib.import_module("report.build_pdf")
        result = module.build(
            cfg_obj=cfg_obj,
            report_data=data,
            docs_dir=cfg.PROJECT_ROOT / "docs",
            results_dir=cfg_obj.results_path(),
        )
        manifest["report"] = result
        logger.info(
            "报告生成状态 PDF=%s HTML=%s 页数=%s",
            result.get("pdf", {}).get("status"),
            result.get("html", {}).get("status"),
            result.get("pdf", {}).get("pages", "—"),
        )
    except Exception as exc:  # noqa: BLE001 - 生成期失败非致命，如实记录
        manifest["report"] = {
            "pdf": {"status": "failed", "reason": repr(exc)},
            "html": {"status": "failed", "reason": repr(exc)},
            "markdown": {"status": "available", "path": "docs/10_方案报告.md"},
        }
        logger.error("报告生成失败：%s", exc)


def run_full(cfg_obj: Config) -> Dict[str, Any]:
    """一键全流程，返回包含全部状态与哈希的 manifest dict。"""
    started = time.perf_counter()
    results = cfg_obj.results_path()
    results.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Any] = {
        "command": "python run.py",
        "seed": cfg_obj.seed,
        "scenarios": [s.name for s in cfg_obj.active_scenarios()],
        "scales": list(cfg_obj.scales),
    }

    dataset = _load(cfg_obj)

    # 1) 三场景求解
    solutions: Dict[str, Any] = {}
    for scenario in cfg_obj.active_scenarios():
        t = time.perf_counter()
        solution = RollingSolver(cfg_obj, dataset).solve(scenario)
        elapsed = time.perf_counter() - t
        solutions[scenario.name] = solution
        logger.info(
            "场景 %s 完成 步数=%d 记录数=%d 配出=%s 立方米 未配=%s 立方米 "
            "构造期冲突=%d 耗时=%.2fs",
            scenario.name, solution.diagnostics.steps, len(solution.records),
            solution.diagnostics.allocated_volume / cfg.VOLUME_UNIT,
            solution.diagnostics.unallocated_volume / cfg.VOLUME_UNIT,
            solution.diagnostics.conflict_total(), elapsed,
        )
    primary_name = cfg_obj.active_scenarios()[0].name
    primary = solutions[primary_name]
    manifest["primary_scenario"] = primary_name

    # 2) 七项指标 + 明细 CSV
    metrics, auxiliary = compute(primary, dataset, cfg_obj)
    allocation_path = _result_path(cfg_obj, "allocation.csv")
    indicators_path = _result_path(cfg_obj, "indicators.csv")
    write_allocation(primary, allocation_path, cfg_obj, logger)
    write_indicators(metrics, indicators_path, cfg_obj, logger)
    logger.info(
        "指标完成 配满率=%.6f 完全匹配率=%.6f 积压率=%.6f 主注入点率=%.6f "
        "成本=%s 元 未配提单货量=%s 立方米 虚仓提单数量=%d",
        metrics.fill_rate, metrics.exact_match_rate, metrics.backlog_rate,
        metrics.primary_injection_rate,
        f"{metrics.cost:.2f}",
        f"{metrics.unallocated_volume / cfg.VOLUME_UNIT:.4f}",
        metrics.virtual_warehouse_count,
    )

    # 3) 独立审计（重新只读打开原始 xlsx，不复用 Solution 状态）
    audit_report, audit_lookup = recheck_from_csv(cfg_obj, allocation_path)
    write_report_json(audit_report, _result_path(cfg_obj, "audit_report.json"), audit_lookup)
    logger.info(
        "独立审计完成 all_pass=%s 硬约束冲突=%d C1=%d C2=%d C3=%d C4=%d C5=%d",
        audit_report.all_pass,
        sum(f.conflicts for f in audit_report.findings
            if f.constraint_id in ("C1", "C2", "C3", "C4", "C5")),
        *[next(f.conflicts for f in audit_report.findings if f.constraint_id == cid)
          for cid in ("C1", "C2", "C3", "C4", "C5")],
    )

    # 4) 未配满诊断（复用 validate_diagnosis 做最后一道黑名单扫描）
    diagnosis = diagnose(primary, dataset, cfg_obj)
    write_diagnosis(diagnosis, _result_path(cfg_obj, "diagnosis.csv"), cfg_obj, logger)
    logger.info("诊断完成 未配满提单=%d", len(diagnosis))

    # 5) 场景 / 规模实验
    metrics_by_name, aux_by_name, scenario_rows = run_scenarios(
        cfg_obj, dataset, solutions=solutions
    )
    write_scenarios(scenario_rows, _result_path(cfg_obj, "metrics_scenarios.csv"), cfg_obj, logger)
    pareto = pareto_points(metrics_by_name, cfg_obj)
    scale_rows = run_scales(cfg_obj, dataset, logger=logger)
    write_scale_experiments(
        scale_rows_for_csv(scale_rows),
        _result_path(cfg_obj, "scale_experiments.csv"), cfg_obj, logger,
    )

    baseline = fcfs_baseline(cfg_obj, dataset)
    baseline_metrics, _ = compute(baseline, dataset, cfg_obj)
    improvement = compute_improvement(metrics, baseline_metrics, cfg_obj)
    write_baseline_comparison(
        improvement, _result_path(cfg_obj, "baseline_comparison.csv"), cfg_obj, logger
    )
    logger.info(
        "实验完成 场景=%d 规模档=%d 非支配采样点=%s",
        len(scenario_rows), len(cfg_obj.scales), pareto["non_dominated"],
    )

    # 6) 规模曲线
    try:
        curves = plot_scale_curves(scale_rows, results / "figures", cfg_obj)
        manifest["figures"] = {
            "status": "generated" if curves else "failed",
            "files": [Path(p).name for p in curves],
            "count": len(curves),
        }
    except Exception as exc:  # noqa: BLE001
        manifest["figures"] = {"status": "failed", "reason": repr(exc), "count": 0}
        logger.error("曲线生成失败：%s", exc)

    # 7) 看板数据 + 看板 HTML
    dashboard_payload = build_dashboard_data(
        metrics=metrics,
        audit=audit_report,
        diagnosis=diagnosis,
        experiments={
            "scenario_rows": scenario_rows,
            "scale_rows": scale_rows_for_csv(scale_rows),
            "pareto": pareto,
            "hour_disposition": primary.diagnostics.hour_disposition,
        },
        cfg_obj=cfg_obj,
        auxiliary=auxiliary,
    )
    import json

    dashboard_json = _result_path(cfg_obj, "dashboard_data.json")
    with dashboard_json.open("w", encoding=cfg.CSV_ENCODING) as handle:
        json.dump(dashboard_payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    _build_dashboard(cfg_obj, dashboard_payload, manifest)

    # 8) 报告数据 + PDF/HTML
    # 全流程自己的哈希写进 build_manifest.json；selfcheck_hashes.json 归 selfcheck.run_all
    # 单独所有（含两次运行比对结果），这里不覆盖它。
    hashes = {
        "allocation.csv": sha256_file(allocation_path),
        "indicators.csv": sha256_file(indicators_path),
        "source_files": audit_lookup.source_hashes,
    }

    dataset_stats = {
        "货源行数": len(dataset.supply_rows),
        "提单数": len(dataset.waybills),
        "密度表行数": len(dataset.densities),
        "密度为空行数": dataset.empty_density_count,
        "非零格数": dataset.nonzero_cells,
        "目的港数": len(dataset.cost_rates.by_port),
        "异常提单数": dataset.abnormal_count,
        "SHJ提单数": dataset.shj_bill_count,
        "全量货源方数": f"{dataset.total_supply_volume / cfg.VOLUME_UNIT:.4f}",
        "评估范围货源方数": f"{dataset.eval_range_supply_volume / cfg.VOLUME_UNIT:.4f}",
        "备注规则条数": dataset.notes_meta.rule_count,
        "数据边界问题数": len(dataset.issues),
    }
    report_payload = build_report_data(
        cfg_obj=cfg_obj,
        metrics=metrics,
        auxiliary=auxiliary,
        audit=audit_report,
        diagnosis_count=len(diagnosis),
        scenario_rows=scenario_rows,
        scale_rows=scale_rows_for_csv(scale_rows),
        improvement=improvement,
        pareto=pareto,
        hashes=hashes,
        manifest=manifest,
        dataset_stats=dataset_stats,
    )
    write_report_data(report_payload, _result_path(cfg_obj, "report_data.json"))
    _build_report(cfg_obj, report_payload, manifest)

    # 9) manifest 落盘（含生成状态与耗时），并回填 report_data 的最终生成状态
    manifest["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    manifest["hashes"] = hashes
    manifest["outputs"] = sorted(p.name for p in results.iterdir() if p.is_file())
    with _result_path(cfg_obj, "build_manifest.json").open(
        "w", encoding=cfg.CSV_ENCODING
    ) as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    report_payload["生成状态"] = dict(manifest)
    write_report_data(report_payload, _result_path(cfg_obj, "report_data.json"))
    logger.info("全流程完成 耗时=%.2fs 结果目录=%s", manifest["elapsed_seconds"], results)
    return manifest


def run_selfcheck(cfg_obj: Config) -> Any:
    """一键自检入口，转发到 selfcheck.run_all。"""
    from .selfcheck import run_all

    return run_all(cfg_obj)


__all__ = ["run_full", "run_selfcheck", "sha256_file"]
