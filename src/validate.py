"""数值 / 时间 / 主键 / 边界校验，产出 results/data_issues.csv。

设计第五节 20 类边界情况在 loader 与这里分工处理：
loader 负责文件、表结构、逐格数值与时间串；这里负责跨行一致性、
主键唯一性、整数化守恒复核、评估范围口径、反查索引与 CSV 落盘。
"""
from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config as cfg
from .config import Config
from .domain import DataIssue, Dataset
from .errors import DataValidationError

_INFO = cfg.ISSUE_CATEGORY["info"]
_BUSINESS = cfg.ISSUE_CATEGORY["business"]
_TIME = cfg.ISSUE_CATEGORY["time"]
_NUMERIC = cfg.ISSUE_CATEGORY["numeric"]


def validate_dataset(raw: Dataset, cfg_obj: Optional[Config] = None) -> Tuple[Dataset, List[DataIssue]]:
    """对已加载的 Dataset 做跨行校验，返回 (Dataset, issues)。

    校验项：480 列整数化守恒、密度键唯一、提单号唯一、异常提单最晚提货、
    时间窗闭合、评估范围外时段、反查索引一致性。任一硬失败抛
    DataValidationError（退出码 2）；其余写 info / DataIssue。
    """
    if cfg_obj is None:
        cfg_obj = Config.load()
    dataset = raw
    issues = dataset.issues
    axis = dataset.axis
    if axis is None:
        raise DataValidationError("Dataset 缺少 TimeAxis", location="dataset.axis")

    # 1) 480 小时列整数化后求和与原始四位小数一致（不依赖合计体积列）
    max_row_diff = 0
    for row in dataset.supply_rows:
        if len(row.hourly_volumes) != cfg.HOURS:
            raise DataValidationError(
                "货源行小时体积数量不是 480",
                location=f"{cfg.SHEET_WIDE}!第{row.row_index + 2}行",
                detail=f"实际 {len(row.hourly_volumes)} 个",
            )
        summed = sum(row.hourly_volumes)
        exact = row.exact_sum_int
        diff = abs(summed - exact)
        max_row_diff = max(max_row_diff, diff)
        if diff > 0:
            raise DataValidationError(
                "480 小时列整数化求和与原始四位小数精确求和不一致",
                location=f"{cfg.SHEET_WIDE}!第{row.row_index + 2}行",
                detail=(
                    f"整数化求和 {summed}，四位小数精确求和 {exact}，"
                    f"差值 {diff / cfg.VOLUME_UNIT:.4f} 立方米"
                ),
            )
        declared_diff = abs(row.declared_total - summed)
        if declared_diff > cfg.VOLUME_TOLERANCE_INT:
            # 「合计体积」列本身是 480 列之和四舍五入到两位的结果，
            # 与原始四位小数之和相差最多 0.0050 立方米，属正常数据事实。
            issues.append(DataIssue(
                category=_INFO,
                location=f"{cfg.SHEET_WIDE}!第{row.row_index + 2}行",
                raw_value=(
                    f"合计体积列与 480 列之和差 {declared_diff / cfg.VOLUME_UNIT:.4f} 立方米"
                ),
                action="忽略合计体积列，只用 480 小时列的原始四位小数；不算错误",
            ))
    issues.append(DataIssue(
        category=_NUMERIC,
        location=cfg.SHEET_WIDE,
        raw_value=f"整数化求和 vs 四位小数精确求和，单行最大差 {max_row_diff} 整数单位",
        action=(
            "体积守恒基准只取 480 小时列的原始值；合计体积列与密度表体积列一律不用；"
            f"体积守恒判定容差 {cfg.VOLUME_TOLERANCE_TEXT} 立方米"
        ),
    ))

    # 2) 密度键唯一（渠道, 子仓, 封箱日期）
    seen_density: Dict[Tuple[str, str, str], int] = {}
    for index, record in enumerate(dataset.densities):
        key = (record.channel_id, record.warehouse_id, record.pack_date)
        if key in seen_density:
            issues.append(DataIssue(
                category=_NUMERIC,
                location=f"{cfg.SHEET_DENSITY}!第{index + 2}行",
                raw_value=str(key),
                action="密度表重复键，保留首次出现，仅记 info 不报错",
            ))
            continue
        seen_density[key] = index
    dataset.density_by_cwd = {
        key: next(
            r.density for r in dataset.densities
            if (r.channel_id, r.warehouse_id, r.pack_date) == key
        )
        for key in seen_density
    }

    # 3) 提单号唯一（数据质量检查，不是第 6 条硬约束）
    seen_bills: Dict[str, int] = {}
    for index, wb in enumerate(dataset.waybills):
        if wb.bill_no in seen_bills:
            issues.append(DataIssue(
                category=_BUSINESS,
                location=f"{cfg.SHEET_WAYBILL}!第{index + 2}行",
                raw_value=wb.bill_no,
                action="重复提单号（审计额外核对），保留首次出现",
            ))
            continue
        seen_bills[wb.bill_no] = index
        if wb.latest_pickup is None or wb.earliest_pickup is None:
            raise DataValidationError(
                "提单时间窗字段缺失", location=f"提单号 {wb.bill_no}"
            )
        if wb.latest_pickup < wb.earliest_pickup:
            raise DataValidationError(
                "提单最晚提货时间早于最早提货时间",
                location=f"提单号 {wb.bill_no}",
                detail=f"最早 {wb.earliest_pickup}，最晚 {wb.latest_pickup}",
            )
        if wb.is_abnormal:
            expected = wb.earliest_pickup + timedelta(
                hours=cfg.ABNORMAL_FALLBACK_HOURS
            )
            if wb.latest_pickup != expected:
                raise DataValidationError(
                    "异常提单兜底口径不一致",
                    location=f"提单号 {wb.bill_no}",
                    detail=f"期望 {expected}，实际 {wb.latest_pickup}",
                )
    issues.append(DataIssue(
        category=_BUSINESS, location=cfg.SHEET_WAYBILL,
        raw_value=f"提单 {len(dataset.waybills)} 张，唯一编号 {len(seen_bills)} 个",
        action="提单号唯一性属数据质量检查，不是第 6 条硬约束",
    ))

    # 4) 反查索引：敏感品按「渠道 × 子仓」这一行取值，不是按渠道 ID
    channel_levels: Dict[str, set] = {}
    for row in dataset.supply_rows:
        channel_levels.setdefault(row.channel_id, set()).add(int(row.level))
    mixed = sum(1 for levels in channel_levels.values() if len(levels) > 1)
    issues.append(DataIssue(
        category=_INFO, location=cfg.SHEET_WIDE,
        raw_value=f"同一渠道 ID 在不同子仓出现不同敏感等级的行数={mixed}",
        action="敏感品/注入点按「渠道 × 子仓」这一行反查，不按渠道 ID 建对照表",
    ))
    if len(dataset.sensitive_by_cw) != len(dataset.supply_rows):
        issues.append(DataIssue(
            category=_INFO, location=cfg.SHEET_WIDE,
            raw_value=(
                f"反查索引键 {len(dataset.sensitive_by_cw)} 个，货源行 {len(dataset.supply_rows)} 行"
            ),
            action="重复 (渠道, 子仓) 键保留首次取值，仅记 info",
        ))

    # 5) 评估范围是统计范围，不是可配载范围
    eval_count = len(axis.eval_hour_indices())
    issues.append(DataIssue(
        category=_INFO, location="time_axis",
        raw_value=f"评估范围 {eval_count} 小时 / 全量 {cfg.HOURS} 小时",
        action=(
            "全 480 小时非零格都参与配载、明细全量输出；"
            f"货源方数类指标只统计产出小时落在 "
            f"{cfg_obj.eval_range.start.strftime(cfg.HOUR_FORMAT)} ~ "
            f"{cfg_obj.eval_range.end_inclusive.strftime(cfg.HOUR_FORMAT)} 的记录集合 A；"
            "配满率与虚仓提单数量按提单侧完整配载结果与全部提单统计"
        ),
    ))

    # 6) 时间轴外的提单：正常纳入可见性判断，不虚构截断
    axis_start = axis.start
    axis_end = axis.end
    outside = [
        wb for wb in dataset.waybills
        if wb.earliest_pickup is not None
        and (wb.earliest_pickup < axis_start or wb.earliest_pickup > axis_end)
    ]
    issues.append(DataIssue(
        category=_INFO, location=cfg.SHEET_WAYBILL,
        raw_value=f"最早提货时间落在 480 小时轴外的提单 {len(outside)} 张",
        action="属正常数据事实，不报错；正常纳入可见性判断，不截断、不虚构",
    ))

    # 7) 全零货量行 / 某小时全零 / 某 (渠道,子仓) 整行全零
    zero_rows = sum(1 for row in dataset.supply_rows if sum(row.hourly_volumes) == 0)
    zero_hours = sum(
        1 for h in range(cfg.HOURS)
        if all(row.hourly_volumes[h] == 0 for row in dataset.supply_rows)
    )
    issues.append(DataIssue(
        category=_INFO, location=cfg.SHEET_WIDE,
        raw_value=f"整行全零 {zero_rows} 行，全零小时 {zero_hours} 个",
        action="零格不进可用池，不生成记录，不报错",
    ))

    # 8) 源文件哈希前后一致
    if dataset.source_hashes.get("before") != dataset.source_hashes.get("after"):
        raise DataValidationError(
            "源文件哈希在加载前后不一致", location="dataset.source_hashes"
        )
    issues.append(DataIssue(
        category=_INFO, location="source_files",
        raw_value=str(sorted(dataset.source_hashes.get("before", {}))),
        action="两个 xlsx 只读加载，加载前后 SHA-256 一致，源文件未改动",
    ))

    return dataset, issues


def write_data_issues(
    issues: List[DataIssue], path: Path, cfg_obj: Optional[Config] = None
) -> Path:
    """把边界问题写成 results/data_issues.csv，四列、UTF-8 无 BOM、固定换行。"""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding=cfg.CSV_ENCODING, newline="") as handle:
            writer = csv.writer(handle, lineterminator=cfg.CSV_LINE_TERMINATOR)
            writer.writerow(cfg.DATA_ISSUES_COLUMNS)
            for issue in issues:
                writer.writerow(issue.to_row())
    except OSError as exc:
        from .errors import DataFileError

        raise DataFileError(
            "结果目录不可写或磁盘满", path=str(target), detail=str(exc)
        ) from exc
    return target


def check_integerization(dataset: Dataset) -> Tuple[int, int]:
    """返回 (全量货源整数体积, 评估范围内货源整数体积)，供单测锁定。"""
    axis = dataset.axis
    if axis is None:
        return (0, 0)
    eval_indices = set(axis.eval_hour_indices())
    total = sum(sum(row.hourly_volumes) for row in dataset.supply_rows)
    in_eval = sum(
        row.hourly_volumes[h] for row in dataset.supply_rows for h in eval_indices
    )
    return (total, in_eval)


__all__ = ["validate_dataset", "write_data_issues", "check_integerization"]
