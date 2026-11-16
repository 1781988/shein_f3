"""argparse 子命令、参数校验、退出码、日志初始化、调用编排层。

默认 stage 为 ``all``：加载 → 校验 → 求解 → 指标 → 独立审计 → 诊断 →
CSV / 实验 / 曲线 / 看板 / 报告 / manifest 全流程。
``--self-check`` 跑 V1—V9 自检；``--only`` 可单独重跑某个阶段。
"""
from __future__ import annotations

import argparse
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import config as cfg
from .config import Config
from .errors import (
    ConfigError,
    F3Error,
    OutputConflictError,
    exit_code_for,
)
from .logging_util import setup_logging

STAGE_CHOICES: List[str] = [
    "load", "validate", "solve", "baseline", "diagnose", "core", "all", "self-check",
]
DEFAULT_STAGE: str = "all"


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="F3 多渠道货源与舱位提单动态配载求解程序",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python run.py                      # 默认全流程（all）\n"
            "  python run.py --help               # 参数说明\n"
            "  python run.py --self-check         # V1—V9 自检\n"
            "  python run.py --only solve --scenario S1_文档顺序\n"
            "  python run.py --data-dir <资料包 data 目录> --only core\n"
        ),
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="原始 xlsx 所在目录，默认 ../F3_一舱装满_资料包/data（相对项目根）",
    )
    parser.add_argument(
        "--seed", default=None,
        help=f"随机种子，仅用于规模抽样的确定性；默认 {cfg.DEFAULT_SEED}，必须是整数",
    )
    parser.add_argument(
        "--scenario", default=None,
        help="只跑指定场景，合法取值为 settings.json 里的场景名",
    )
    parser.add_argument(
        "--scale", default=None,
        help="只跑指定规模档，合法取值 (0, 1]，例如 0.25 / 0.5 / 1.0",
    )
    parser.add_argument(
        "--only", default=DEFAULT_STAGE, choices=STAGE_CHOICES,
        help=f"运行阶段，默认 {DEFAULT_STAGE}",
    )
    parser.add_argument(
        "--strict-no-overwrite", action="store_true",
        help="结果文件已存在时报错（退出码 3），而不是移入 results/_prev/",
    )
    parser.add_argument(
        "--log-no-timestamp", action="store_true",
        help="日志不带时间戳，便于复跑逐行比对",
    )
    parser.add_argument(
        "--config", default=None,
        help="指定 settings.json 路径（相对项目根解析）",
    )
    parser.add_argument(
        "--self-check", action="store_true",
        help="运行一键自检，产出 results/selfcheck_report.md 与 selfcheck_hashes.json",
    )
    return parser


def _int_arg(name: str, value: str) -> int:
    """把字符串参数转成整数，失败抛带合法取值的 ConfigError。"""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{name} 必须是整数，当前值 {value!r}；合法取值如 {cfg.DEFAULT_SEED}"
        ) from exc


def _float_arg(name: str, value: str) -> float:
    """把字符串参数转成 float，失败抛带合法取值的 ConfigError。"""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{name} 必须是数值，当前值 {value!r}；合法取值 (0, 1]"
        ) from exc


def _overrides_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    """把命令行参数整理成 Config.apply_overrides 认识的字典型。"""
    overrides: Dict[str, Any] = {}
    if args.data_dir is not None:
        raw = Path(args.data_dir)
        overrides["data_dir"] = str(raw if raw.is_absolute() else Path.cwd() / raw)
    if args.seed is not None:
        overrides["seed"] = _int_arg("seed", args.seed)
    if args.scenario is not None:
        overrides["scenario"] = args.scenario
    if args.scale is not None:
        overrides["scale"] = _float_arg("scale", args.scale)
    if args.strict_no_overwrite:
        overrides["strict_no_overwrite"] = True
    return overrides


def check_output_conflict(cfg_obj: Config) -> None:
    """--strict-no-overwrite 时，结果目录里已有主产出文件就报错退出码 3。

    这是第五节「CSV 已存在」边界的实现；T04 的 csv_writer 会复用本函数。
    """
    if not cfg_obj.strict_no_overwrite:
        return
    names = [
        cfg_obj.outputs.get("allocation", "allocation.csv"),
        cfg_obj.outputs.get("indicators", "indicators.csv"),
    ]
    existing = [str(cfg_obj.results_path() / name)
                for name in names if (cfg_obj.results_path() / name).is_file()]
    if existing:
        raise OutputConflictError(
            "结果文件已存在且启用了 --strict-no-overwrite",
            path=", ".join(existing),
            detail="去掉该参数会自动把旧文件移入 results/_prev/",
        )


def _build_config(args: argparse.Namespace) -> Config:
    """加载配置并套用命令行覆盖。"""
    path: Optional[Path] = None
    if args.config:
        path = Path(args.config)
    return Config.load(path=path, overrides=_overrides_from_args(args))


def _load_dataset(cfg_obj: Config) -> Any:
    """加载并校验五张表；返回 Dataset 与 data_issues.csv 路径。"""
    from .loader import load_all
    from .validate import validate_dataset, write_data_issues

    dataset = load_all(cfg_obj)
    validate_dataset(dataset, cfg_obj)
    issues_path = cfg_obj.results_path() / "data_issues.csv"
    write_data_issues(dataset.issues, issues_path, cfg_obj)
    return dataset


def _print_config(cfg_obj: Config, logger: Any) -> None:
    """打印一行配置摘要，便于复跑核对。"""
    logger.info(
        "配置 data_dir=%s seed=%d 场景=%s 规模=%s 评估范围=%s ~ %s",
        cfg_obj.data_path(), cfg_obj.seed,
        [s.name for s in cfg_obj.active_scenarios()], cfg_obj.scales,
        cfg_obj.eval_range.start.strftime(cfg.HOUR_FORMAT),
        cfg_obj.eval_range.end_inclusive.strftime(cfg.HOUR_FORMAT),
    )


def _print_dataset(dataset: Any, logger: Any) -> None:
    """打印加载统计。"""
    logger.info(
        "加载完成 货源行=%d 提单=%d 密度行=%d 目的港成本=%d 非零格=%d "
        "全量货源=%s 立方米 评估范围货源=%s 立方米 data_issues=%d",
        len(dataset.supply_rows), len(dataset.waybills), len(dataset.densities),
        len(dataset.cost_rates.by_port), dataset.nonzero_cells,
        dataset.total_supply_volume / cfg.VOLUME_UNIT,
        dataset.eval_range_supply_volume / cfg.VOLUME_UNIT,
        len(dataset.issues),
    )


def _run_solve(cfg_obj: Config, dataset: Any, logger: Any) -> Any:
    """跑全部激活场景的滚动求解，返回 {场景名: Solution}。"""
    from .solver import RollingSolver

    solutions: Dict[str, Any] = {}
    for scenario in cfg_obj.active_scenarios():
        started = time.perf_counter()
        solver = RollingSolver(cfg_obj, dataset)
        solution = solver.solve(scenario)
        elapsed = time.perf_counter() - started
        diag = solution.diagnostics
        logger.info(
            "场景 %s 完成 步数=%d 事件步=%d 重排步=%d 增量步=%d 退回=%d "
            "配出=%s 立方米 未配=%s 立方米 提单数=%d 记录数=%d "
            "构造期冲突=%d 耗时=%.2fs",
            scenario.name, diag.steps, diag.event_steps, diag.reassign_steps,
            diag.incremental_steps, diag.refund_count,
            diag.allocated_volume / cfg.VOLUME_UNIT,
            diag.unallocated_volume / cfg.VOLUME_UNIT,
            len(dataset.waybills), len(solution.records),
            diag.conflict_total(), elapsed,
        )
        solutions[scenario.name] = solution
    return solutions


def _run_baseline(cfg_obj: Config, dataset: Any, logger: Any) -> Any:
    """跑独立贪心基准。"""
    from .baseline import fcfs_baseline

    started = time.perf_counter()
    solution = fcfs_baseline(cfg_obj, dataset)
    elapsed = time.perf_counter() - started
    logger.info(
        "贪心基准完成 记录数=%d 配出=%s 立方米 提单数=%d 构造期冲突=%d 耗时=%.2fs",
        len(solution.records), solution.total_volume() / cfg.VOLUME_UNIT,
        len(dataset.waybills), solution.diagnostics.conflict_total(), elapsed,
    )
    return solution


def _run_diagnose(cfg_obj: Config, dataset: Any, solutions: Any, logger: Any) -> Any:
    """对首个场景跑未配满原因诊断。"""
    from .diagnose import diagnose

    first = next(iter(solutions.values())) if solutions else None
    if first is None:
        return []
    rows = diagnose(first, dataset, cfg_obj)
    logger.info("诊断完成 未配满提单=%d 原因白名单=%s", len(rows), cfg_obj.allowed_reasons())
    return rows


def _dispatch(cfg_obj: Config, args: argparse.Namespace, logger: Any) -> int:
    """按 --only 分发到各阶段。"""
    check_output_conflict(cfg_obj)
    _print_config(cfg_obj, logger)

    stage = "self-check" if args.self_check else args.only
    if stage in ("all", "self-check"):
        return _deferred_stage(stage, cfg_obj, logger)

    dataset = _load_dataset(cfg_obj)
    _print_dataset(dataset, logger)
    if stage in ("load", "validate"):
        logger.info("阶段 %s 完成，仅加载与校验，未求解", stage)
        return 0

    solutions: Dict[str, Any] = {}
    if stage in ("solve", "diagnose", "core"):
        solutions = _run_solve(cfg_obj, dataset, logger)
    if stage in ("baseline", "core"):
        _run_baseline(cfg_obj, dataset, logger)
    if stage in ("diagnose", "core"):
        _run_diagnose(cfg_obj, dataset, solutions, logger)

    logger.info("阶段 %s 完成（未写全流程产出；默认 python run.py 为 all）", stage)
    return 0


def _deferred_stage(stage: str, cfg_obj: Config, logger: Any) -> int:
    """all / self-check 阶段：调用 pipeline.run_full 或 pipeline.run_selfcheck。"""
    from . import pipeline

    if stage == "all":
        pipeline.run_full(cfg_obj)
        return 0
    report = pipeline.run_selfcheck(cfg_obj)
    return 0 if report.all_pass else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """程序入口，返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    log_path = cfg.PROJECT_ROOT / "results" / "run.log"
    logger = setup_logging(log_path=log_path, no_timestamp=args.log_no_timestamp)
    try:
        cfg_obj = _build_config(args)
        return _dispatch(cfg_obj, args, logger)
    except F3Error as exc:
        logger.error("%s", exc)
        return exit_code_for(exc)
    except Exception as exc:  # noqa: BLE001 - 未预期异常按设计记堆栈并退出码 1
        logger.error("未预期异常：%s\n%s", exc, traceback.format_exc())
        return exit_code_for(exc)


__all__ = [
    "build_parser", "main", "check_output_conflict", "STAGE_CHOICES", "DEFAULT_STAGE",
]
