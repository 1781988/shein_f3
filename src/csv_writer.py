"""结果 CSV 的确定性写出。

- ``allocation.csv`` 严格 6 列、不加列；UTF-8 无 BOM、固定 ``\\n`` 换行；
- 排序键 (提单号, 产出小时, 渠道ID, 子仓ID)，同一四元组先合并再写；
- 覆盖前把旧文件移入 ``results/_prev/``；``--strict-no-overwrite`` 由
  cli.check_output_conflict 提前拦截（退出码 3）；
- ``indicators.csv`` 首行恰为 3 列、7 行、口径说明非空。
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from . import config as cfg
from .candidates import merge_records
from .config import Config
from .domain import Metrics, Solution
from .errors import DataFileError, OutputConflictError
from .metrics import INDICATOR_NAMES, metric_values
from .precision import int_to_vol_str


def rotate_existing(path: Path, cfg_obj: Config, logger: Any = None) -> Optional[Path]:
    """覆盖前把已存在文件移入 results/_prev/；strict 模式改为报错退出码 3。"""
    target = Path(path)
    if not target.is_file():
        return None
    if cfg_obj.strict_no_overwrite:
        raise OutputConflictError(
            "结果文件已存在且启用了 --strict-no-overwrite",
            path=str(target),
            detail="去掉该参数会自动把旧文件移入 results/_prev/",
        )
    prev_dir = cfg_obj.results_path() / "_prev"
    prev_dir.mkdir(parents=True, exist_ok=True)
    backup = prev_dir / target.name
    if backup.exists():
        backup.unlink()
    target.replace(backup)
    if logger is not None:
        logger.info("覆盖前把旧文件移入 %s", backup)
    return backup


def _write_table(
    path: Path,
    header: Sequence[str],
    rows: Iterable[Sequence[Any]],
    cfg_obj: Config,
    logger: Any = None,
) -> Path:
    """通用确定性表格写出。"""
    target = Path(path)
    rotate_existing(target, cfg_obj, logger)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding=cfg.CSV_ENCODING, newline="") as handle:
            writer = csv.writer(handle, lineterminator=cfg.CSV_LINE_TERMINATOR)
            writer.writerow(list(header))
            for row in rows:
                writer.writerow([str(cell) for cell in row])
    except OSError as exc:
        raise DataFileError(
            "结果目录不可写或磁盘满", path=str(target), detail=str(exc)
        ) from exc
    return target


def write_allocation(
    solution: Solution, path: Path, cfg_obj: Config, logger: Any = None
) -> Path:
    """写严格 6 列的全量配货明细（覆盖 480 个小时）。"""
    records = merge_records(list(solution.records))
    records.sort(key=lambda r: r.sort_key())
    rows = [r.to_csv_row(int_to_vol_str) for r in records]
    # 覆盖前校验：严格 6 列，任何派生字段都不得写进 CSV
    for row in rows:
        if len(row) != len(cfg.ALLOCATION_COLUMNS):
            raise DataFileError(
                "配货明细列数不是 6", path=str(path),
                detail=f"实际 {len(row)} 列：{row!r}",
            )
    return _write_table(Path(path), cfg.ALLOCATION_COLUMNS, rows, cfg_obj, logger)


def write_indicators(
    metrics: Metrics, path: Path, cfg_obj: Config, logger: Any = None
) -> Path:
    """写 indicators.csv：3 列、7 行、口径说明非空。"""
    values = metric_values(metrics, cfg_obj)
    rows: List[List[str]] = []
    for name in INDICATOR_NAMES:
        note = metrics.formula_notes.get(name, "")
        if not note:
            raise DataFileError(
                "指标口径说明为空", path=str(path), detail=f"指标 {name}"
            )
        rows.append([name, values[name], note])
    return _write_table(Path(path), cfg.INDICATORS_COLUMNS, rows, cfg_obj, logger)


def write_diagnosis(
    rows: Sequence[Any], path: Path, cfg_obj: Config, logger: Any = None
) -> Path:
    """写 diagnosis.csv（未配满原因链条）。"""
    from .diagnose import validate_diagnosis

    validate_diagnosis(rows, cfg_obj)
    data = [r.to_row(int_to_vol_str) for r in rows]
    return _write_table(Path(path), cfg.DIAGNOSIS_COLUMNS, data, cfg_obj, logger)


def write_scenarios(
    rows: Sequence[Mapping[str, Any]], path: Path, cfg_obj: Config, logger: Any = None
) -> Path:
    """写 metrics_scenarios.csv：各场景 × 七项指标。"""
    header = ["场景"] + list(INDICATOR_NAMES)
    data = [[row.get("场景", ""), *[row.get(name, "") for name in INDICATOR_NAMES]]
            for row in rows]
    return _write_table(Path(path), header, data, cfg_obj, logger)


def write_scale_experiments(
    rows: Sequence[Mapping[str, Any]], path: Path, cfg_obj: Config, logger: Any = None
) -> Path:
    """写 scale_experiments.csv。"""
    header = [
        "规模", "方法", "配满率", "时效积压率", "成本", "耗时秒", "记录数",
    ]
    data = [
        [row.get("规模", ""), row.get("方法", ""), row.get("配满率", ""),
         row.get("时效积压率", ""), row.get("成本", ""), row.get("耗时秒", ""),
         row.get("记录数", "")]
        for row in rows
    ]
    return _write_table(Path(path), header, data, cfg_obj, logger)


def write_baseline_comparison(
    rows: Sequence[Mapping[str, Any]], path: Path, cfg_obj: Config, logger: Any = None
) -> Path:
    """写 baseline_comparison.csv：相对贪心基准的改进量。"""
    header = ["指标", "基准值", "本方案值", "改进量", "改进率"]
    data = [
        [row.get("指标", ""), row.get("基准值", ""), row.get("本方案值", ""),
         row.get("改进量", ""), row.get("改进率", "")]
        for row in rows
    ]
    return _write_table(Path(path), header, data, cfg_obj, logger)


def write_data_issues(
    issues: Sequence[Any], path: Path, cfg_obj: Config, logger: Any = None
) -> Path:
    """写 data_issues.csv（T02 validate 也有一份，这里供 pipeline 复用同一格式）。"""
    data = [issue.to_row() for issue in issues]
    return _write_table(Path(path), cfg.DATA_ISSUES_COLUMNS, data, cfg_obj, logger)


__all__ = [
    "rotate_existing", "write_allocation", "write_indicators", "write_diagnosis",
    "write_scenarios", "write_scale_experiments", "write_baseline_comparison",
    "write_data_issues",
]
