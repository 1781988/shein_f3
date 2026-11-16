"""后处理审计，独立于求解器。

``recheck_from_csv`` 自己用 openpyxl 只读重新打开两张原始 xlsx，
逐格重建 (渠道, 子仓, 产出小时) 的原始体积、敏感品等级、渠道注入点、
提单计划体积 / 目的港 / 时间窗 / 密度、评估范围小时集合，
再拿 ``allocation.csv`` 逐行复核 C1—C5。它不读 Solution、不读 ledger，
只信输入文件和输出文件。

C1 体积守恒：按「小时 × 渠道 × 子仓」逐格聚合，配出方数 ≤ 该格原始体积；
再按 README 口径做四位小数求和比对，容差来自 config.VOLUME_TOLERANCE_INT。
C2 单提单配货 ≤ 计划体积；C3 单提单只提一个子仓；C4 货等级 ≤ 提单等级；
C5 最早提货 ≤ 提货时刻 ≤ 最晚提货，且提货时刻 ≥ 该批货可配时刻。
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import openpyxl

from . import config as cfg
from .config import Config
from .domain import AuditFinding, AuditReport, Solution
from .errors import DataFileError
from .precision import (
    format_date, parse_date, parse_hour_column_left, parse_time, vol_to_int,
)
from .sensitivity import parse_level
from .time_axis import TimeAxis

C1 = "C1"
C2 = "C2"
C3 = "C3"
C4 = "C4"
C5 = "C5"
FORMAT = "FORMAT"
UNIQUE = "UNIQUE"
CONSTRAINT_IDS: Tuple[str, ...] = (C1, C2, C3, C4, C5, FORMAT, UNIQUE)

CONSTRAINT_NAMES: Dict[str, str] = {
    C1: "货源体积守恒（逐格）",
    C2: "单提单配货不超过计划体积",
    C3: "单提单只提一个子仓",
    C4: "敏感品等级兼容",
    C5: "提货时间窗",
    FORMAT: "明细格式（列名 / 小数位 / 时间格式）",
    UNIQUE: "提单号唯一性与记录可追溯（数据质量检查，不是第 6 条硬约束）",
}


@dataclass
class RawLookup:
    """从原始 xlsx 独立重建的反查索引（不复用 loader / Solution 的任何状态）。"""

    axis: TimeAxis
    cell_volume: Dict[Tuple[str, str, int], int]
    cell_level: Dict[Tuple[str, str], int]
    cell_injection: Dict[Tuple[str, str], str]
    hour_index_by_label: Dict[str, int]
    bill_plan: Dict[str, int]
    bill_level: Dict[str, int]
    bill_dest: Dict[str, str]
    bill_earliest: Dict[str, datetime]
    bill_latest: Dict[str, datetime]
    bill_billing: Dict[str, str]
    density_by_cwd: Dict[Tuple[str, str, str], Optional[float]]
    cost_by_port: Dict[str, float]
    eval_supply_volume: int
    source_hashes: Dict[str, str]


def sha256_file(path: Path) -> str:
    """计算单个原始文件的 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _id(value: Any) -> str:
    """把 ID 单元格统一成字符串，整数去掉 .0 尾巴。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _header_map(ws: Any) -> Dict[str, int]:
    """读首行表头，返回列名 -> 索引。"""
    row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    mapping: Dict[str, int] = {}
    for index, name in enumerate(row):
        if name is None:
            continue
        key = str(name).strip()
        if key and key not in mapping:
            mapping[key] = index
    return mapping


def build_raw_lookup(cfg_obj: Config) -> RawLookup:
    """自己重新只读打开两张 xlsx，逐格重建审计所需的全部原始字段。"""
    wide_path = cfg_obj.wide_path()
    bill_path = cfg_obj.bill_path()
    for path in (wide_path, bill_path):
        if not path.is_file():
            raise DataFileError(
                "审计所需原始数据文件缺失", path=str(path),
                detail="请检查 --data-dir 与 config/settings.json 的文件名",
            )
    hashes = {p.name: sha256_file(p) for p in (wide_path, bill_path)}

    axis = TimeAxis.from_config(cfg_obj)
    hour_index_by_label = {label: i for i, label in enumerate(axis.all_labels())}
    cell_volume: Dict[Tuple[str, str, int], int] = {}
    cell_level: Dict[Tuple[str, str], int] = {}
    cell_injection: Dict[Tuple[str, str], str] = {}
    eval_supply_volume = 0

    wide_wb = openpyxl.load_workbook(wide_path, read_only=True, data_only=True)
    try:
        ws = wide_wb[cfg.SHEET_WIDE]
        header = _header_map(ws)
        fixed = {name: header[name] for name in cfg.WIDE_FIXED_COLUMNS}
        column_names = [
            str(name) for name in
            list(next(ws.iter_rows(min_row=1, max_row=1, values_only=True)))[
                len(cfg.WIDE_FIXED_COLUMNS): len(cfg.WIDE_FIXED_COLUMNS) + cfg.HOURS
            ]
        ]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or all(cell is None for cell in row[: len(cfg.WIDE_FIXED_COLUMNS)]):
                continue
            channel = _id(row[fixed["渠道ID"]])
            warehouse = _id(row[fixed["子仓ID"]])
            injection = _id(row[fixed["渠道注入点"]])
            level = int(parse_level(row[fixed["敏感品"]]))
            cell_level[(channel, warehouse)] = level
            cell_injection[(channel, warehouse)] = injection
            for hour_index, column_name in enumerate(column_names):
                value = row[len(cfg.WIDE_FIXED_COLUMNS) + hour_index]
                volume = vol_to_int(value)
                if volume > 0:
                    cell_volume[(channel, warehouse, hour_index)] = volume
                    if axis.is_in_eval_range(hour_index):
                        eval_supply_volume += volume
        # 用实际列名校验小时索引映射，避免宽表列名被改而审计仍用旧映射
        for hour_index, column_name in enumerate(column_names):
            left = parse_hour_column_left(column_name)
            if left != axis.left_at(hour_index):
                raise DataFileError(
                    "审计发现宽表小时列名与时间轴不一致",
                    path=str(wide_path),
                    detail=f"第 {hour_index} 列 {column_name!r} 左端 {left}",
                )
    finally:
        wide_wb.close()

    density_by_cwd: Dict[Tuple[str, str, str], Optional[float]] = {}
    try:
        wide_wb = openpyxl.load_workbook(wide_path, read_only=True, data_only=True)
        ws = wide_wb[cfg.SHEET_DENSITY]
        header = _header_map(ws)
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[header["渠道ID"]] is None:
                continue
            channel = _id(row[header["渠道ID"]])
            warehouse = _id(row[header["子仓ID"]])
            pack_date = format_date(parse_date(row[header["封箱日期"]]))
            raw_density = row[header["密度(kg/m³)"]]
            density = None if raw_density is None or str(raw_density).strip() == "" \
                else float(raw_density)
            density_by_cwd[(channel, warehouse, pack_date)] = density
    finally:
        wide_wb.close()

    bill_plan: Dict[str, int] = {}
    bill_level: Dict[str, int] = {}
    bill_dest: Dict[str, str] = {}
    bill_earliest: Dict[str, datetime] = {}
    bill_latest: Dict[str, datetime] = {}
    bill_billing: Dict[str, str] = {}
    cost_by_port: Dict[str, float] = {}
    bill_wb = openpyxl.load_workbook(bill_path, read_only=True, data_only=True)
    try:
        ws = bill_wb[cfg.SHEET_WAYBILL]
        header = _header_map(ws)
        col = {key: header[name] for key, name in cfg.WAYBILL_FIELD_COLUMNS.items()}
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[col["bill_no"]] is None:
                continue
            bill_no = _id(row[col["bill_no"]])
            if bill_no in bill_plan:
                continue
            earliest = parse_time(row[col["earliest_pickup"]])
            required = parse_time(row[col["required_arrival"]])
            abnormal = earliest > required
            latest = earliest + timedelta(hours=cfg.ABNORMAL_FALLBACK_HOURS) \
                if abnormal else required
            bill_plan[bill_no] = vol_to_int(row[col["plan_volume"]])
            bill_level[bill_no] = int(parse_level(row[col["sensitive_raw"]]))
            bill_dest[bill_no] = _id(row[col["dest_port"]])
            bill_earliest[bill_no] = earliest
            bill_latest[bill_no] = latest
            bill_billing[bill_no] = _id(row[col["billing_type"]])
        ws = bill_wb[cfg.SHEET_COST]
        header = _header_map(ws)
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[header[cfg.COST_REQUIRED_COLUMNS[0]]] is None:
                continue
            port = _id(row[header[cfg.COST_REQUIRED_COLUMNS[0]]])
            cost_by_port[port] = float(row[header[cfg.COST_REQUIRED_COLUMNS[1]]])
    finally:
        bill_wb.close()

    return RawLookup(
        axis=axis,
        cell_volume=cell_volume,
        cell_level=cell_level,
        cell_injection=cell_injection,
        hour_index_by_label=hour_index_by_label,
        bill_plan=bill_plan,
        bill_level=bill_level,
        bill_dest=bill_dest,
        bill_earliest=bill_earliest,
        bill_latest=bill_latest,
        bill_billing=bill_billing,
        density_by_cwd=density_by_cwd,
        cost_by_port=cost_by_port,
        eval_supply_volume=eval_supply_volume,
        source_hashes=hashes,
    )


def _new_findings() -> Dict[str, AuditFinding]:
    """为每条约束创建一个空的 finding。"""
    return {
        cid: AuditFinding(constraint_id=cid, checked=0, conflicts=0, samples=[])
        for cid in CONSTRAINT_IDS
    }


def _add(finding: AuditFinding, sample: str) -> None:
    """记一次冲突，最多保留 20 条样例。"""
    finding.conflicts += 1
    if len(finding.samples) < 20:
        finding.samples.append(sample)


def check_rows(
    rows: Sequence[Mapping[str, str]],
    lookup: RawLookup,
    cfg_obj: Config,
    header: Optional[Sequence[str]] = None,
) -> AuditReport:
    """逐行复核 C1—C5 + 格式 + 提单号唯一性，返回 AuditReport。"""
    findings = _new_findings()
    axis = lookup.axis

    # 格式：表头、六列、四位小数、时间格式
    if header is not None:
        findings[FORMAT].checked += 1
        if list(header) != list(cfg.ALLOCATION_COLUMNS):
            _add(findings[FORMAT], f"表头不匹配：{list(header)!r}")

    per_cell: Dict[Tuple[str, str, int], int] = {}
    per_bill: Dict[str, int] = {}
    warehouses: Dict[str, set] = {}
    seen_keys: Dict[str, int] = {}
    seen_bills: Dict[str, int] = {}

    for line_no, row in enumerate(rows, start=2):
        findings[FORMAT].checked += 1
        values = list(row.values()) if isinstance(row, dict) else list(row)
        if len(values) != len(cfg.ALLOCATION_COLUMNS):
            _add(findings[FORMAT], f"第 {line_no} 行列数 {len(values)}")
            continue
        bill_no, channel, warehouse, hour_label, pickup_str, vol_str = values
        bill_no = str(bill_no).strip()
        channel = str(channel).strip()
        warehouse = str(warehouse).strip()
        hour_label = str(hour_label).strip()
        pickup_str = str(pickup_str).strip()
        vol_str = str(vol_str).strip()

        # 四位小数与整数化
        decimals = len(vol_str.split(".")[1]) if "." in vol_str else 0
        if decimals > cfg.VOLUME_DECIMALS:
            _add(findings[FORMAT], f"第 {line_no} 行配货方数小数位 {decimals}")
        try:
            volume = vol_to_int(vol_str)
        except ValueError as exc:
            _add(findings[FORMAT], f"第 {line_no} 行配货方数非法：{exc}")
            continue
        if volume <= 0:
            _add(findings[C1], f"第 {line_no} 行配货方数非正：{vol_str}")
        try:
            pickup = parse_time(pickup_str)
        except ValueError as exc:
            _add(findings[FORMAT], f"第 {line_no} 行提货时刻非法：{exc}")
            continue
        hour_index = lookup.hour_index_by_label.get(hour_label)
        if hour_index is None:
            _add(findings[FORMAT], f"第 {line_no} 行产出小时不在时间轴：{hour_label!r}")
            continue

        key = (bill_no, channel, warehouse, hour_label)
        seen_keys[str(key)] = seen_keys.get(str(key), 0) + 1
        seen_bills[bill_no] = seen_bills.get(bill_no, 0) + 1

        # C1 逐格守恒
        findings[C1].checked += 1
        original = lookup.cell_volume.get((channel, warehouse, hour_index))
        cell_key = (channel, warehouse, hour_index)
        per_cell[cell_key] = per_cell.get(cell_key, 0) + volume
        if original is None and volume > 0:
            _add(findings[C1], f"第 {line_no} 行引用了原始表不存在的格 {cell_key}")
        elif original is not None and per_cell[cell_key] > original + cfg.VOLUME_TOLERANCE_INT:
            _add(findings[C1], (
                f"第 {line_no} 行格 {cell_key} 累计 {per_cell[cell_key]} "
                f"超过原始 {original}"
            ))

        # C2 计划体积封顶
        findings[C2].checked += 1
        per_bill[bill_no] = per_bill.get(bill_no, 0) + volume
        plan = lookup.bill_plan.get(bill_no)
        if plan is None:
            _add(findings[C2], f"第 {line_no} 行提单号不在原始表：{bill_no!r}")
        elif per_bill[bill_no] > plan:
            _add(findings[C2], (
                f"第 {line_no} 行提单 {bill_no} 累计 {per_bill[bill_no]} 超过计划 {plan}"
            ))

        # C3 单子仓
        findings[C3].checked += 1
        warehouses.setdefault(bill_no, set()).add(warehouse)
        if len(warehouses[bill_no]) > 1:
            _add(findings[C3], f"第 {line_no} 行提单 {bill_no} 出现多个子仓")

        # C4 敏感品等级兼容
        findings[C4].checked += 1
        cargo_level = lookup.cell_level.get((channel, warehouse))
        wb_level = lookup.bill_level.get(bill_no)
        if cargo_level is None or wb_level is None:
            _add(findings[C4], f"第 {line_no} 行等级反查缺失 ({channel},{warehouse}) / {bill_no}")
        elif cargo_level > wb_level:
            _add(findings[C4], (
                f"第 {line_no} 行货等级 {cargo_level} 高于提单 {bill_no} 等级 {wb_level}"
            ))

        # C5 时间窗（含提货时刻上下界与可配时刻）
        findings[C5].checked += 1
        earliest = lookup.bill_earliest.get(bill_no)
        latest = lookup.bill_latest.get(bill_no)
        available_at = axis.available_at(hour_index)
        if earliest is None or latest is None:
            _add(findings[C5], f"第 {line_no} 行提单时间窗反查缺失：{bill_no}")
        else:
            if not (earliest <= pickup <= latest):
                _add(findings[C5], (
                    f"第 {line_no} 行提货时刻 {pickup_str} 不在 "
                    f"[{earliest}, {latest}]（提单 {bill_no}）"
                ))
            if pickup < available_at:
                _add(findings[C5], (
                    f"第 {line_no} 行提货时刻 {pickup_str} 早于该批货可配时刻 "
                    f"{available_at}（提单 {bill_no}）"
                ))
            if available_at > latest:
                _add(findings[C5], (
                    f"第 {line_no} 行格可配时刻 {available_at} 晚于最晚提货 {latest}"
                ))

    # 四位小数求和容差（README 口径）：逐格把配出方数按四位小数字符串求和再比对
    for cell_key, summed in per_cell.items():
        original = lookup.cell_volume.get(cell_key, 0)
        if abs(summed - original) > cfg.VOLUME_TOLERANCE_INT:
            _add(findings[C1], f"格 {cell_key} 四位小数求和 {summed} 与原值 {original} 超容差")

    # 同一 (提单, 渠道, 子仓, 产出小时) 至多一行；提单号唯一性属数据质量检查
    findings[UNIQUE].checked = len(rows)
    for key, count in seen_keys.items():
        if count > 1:
            _add(findings[UNIQUE], f"同一四元组出现 {count} 行：{key}")
    for bill_no in lookup.bill_plan:
        if seen_bills.get(bill_no, 0) > 0 and lookup.bill_plan[bill_no] <= 0:
            _add(findings[UNIQUE], f"提单 {bill_no} 计划体积非正")

    report = AuditReport(
        findings=[findings[cid] for cid in CONSTRAINT_IDS],
        all_pass=all(f.conflicts == 0 for f in findings.values()),
        hash_before="", hash_after="",
    )
    return report


def recheck_from_csv(
    cfg_obj: Config, allocation_path: Optional[Path] = None
) -> Tuple[AuditReport, RawLookup]:
    """从 allocation.csv 与重新只读打开的原始 xlsx 独立复核，返回 (报告, 反查索引)。"""
    target = Path(allocation_path) if allocation_path is not None \
        else cfg_obj.results_path() / cfg_obj.outputs.get("allocation", "allocation.csv")
    if not target.is_file():
        raise DataFileError("审计目标 allocation.csv 不存在", path=str(target))
    lookup = build_raw_lookup(cfg_obj)
    with target.open("r", encoding=cfg.CSV_ENCODING, newline="") as handle:
        reader = csv.reader(handle, lineterminator=cfg.CSV_LINE_TERMINATOR)
        header = next(reader, [])
        rows = [
            {cfg.ALLOCATION_COLUMNS[i]: row[i] for i in range(len(cfg.ALLOCATION_COLUMNS))}
            for row in reader if row
        ]
    report = check_rows(rows, lookup, cfg_obj, header=list(header))
    after = {p.name: sha256_file(p) for p in (cfg_obj.wide_path(), cfg_obj.bill_path())}
    report.hash_before = str(lookup.source_hashes)
    report.hash_after = str(after)
    if lookup.source_hashes != after:
        report.all_pass = False
    return report, lookup


def audit(
    solution: Solution, dataset: Any, cfg_obj: Config
) -> AuditReport:
    """内存审计入口：把 Solution.records 转成六列行，再用独立反查索引复核。"""
    lookup = build_raw_lookup(cfg_obj)
    rows = [
        {
            cfg.ALLOCATION_COLUMNS[0]: r.bill_no,
            cfg.ALLOCATION_COLUMNS[1]: r.channel_id,
            cfg.ALLOCATION_COLUMNS[2]: r.warehouse_id,
            cfg.ALLOCATION_COLUMNS[3]: r.hour_label,
            cfg.ALLOCATION_COLUMNS[4]: r.pickup_time_str,
            cfg.ALLOCATION_COLUMNS[5]: (
                f"{r.volume // cfg.VOLUME_UNIT}."
                f"{r.volume % cfg.VOLUME_UNIT:0{cfg.VOLUME_DECIMALS}d}"
            ),
        }
        for r in solution.records
    ]
    report = check_rows(rows, lookup, cfg_obj, header=list(cfg.ALLOCATION_COLUMNS))
    report.hash_before = str(lookup.source_hashes)
    report.hash_after = str(lookup.source_hashes)
    return report


def write_report_json(report: AuditReport, path: Path, lookup: Optional[RawLookup] = None) -> Path:
    """把审计结果写成 results/audit_report.json（含逐条 finding 与源文件哈希）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    import json

    payload: Dict[str, Any] = {
        "all_pass": report.all_pass,
        "hash_before": report.hash_before,
        "hash_after": report.hash_after,
        "source_hashes": lookup.source_hashes if lookup is not None else {},
        "findings": [
            {
                "constraint_id": f.constraint_id,
                "name": CONSTRAINT_NAMES.get(f.constraint_id, ""),
                "checked": f.checked,
                "conflicts": f.conflicts,
                "samples": f.samples,
            }
            for f in report.findings
        ],
        "conflict_total": sum(f.conflicts for f in report.findings),
        "hard_constraint_conflicts": sum(
            f.conflicts for f in report.findings if f.constraint_id in (C1, C2, C3, C4, C5)
        ),
    }
    with target.open("w", encoding=cfg.CSV_ENCODING) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return target


__all__ = [
    "C1", "C2", "C3", "C4", "C5", "FORMAT", "UNIQUE", "CONSTRAINT_IDS",
    "CONSTRAINT_NAMES", "RawLookup", "build_raw_lookup", "check_rows",
    "recheck_from_csv", "audit", "write_report_json", "sha256_file",
]
