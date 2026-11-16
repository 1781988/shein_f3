"""生成最小合成样例：空表、缺列、非法时间串、负数与 NaN 体积、重复提单号、
全零货量、SHJ 备选、异常提单、范围外时段、CSV 已存在、参数非法等。

样例全部是内存构造或临时目录里的合成 xlsx，不含任何资料包原始数据。
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import openpyxl

from src import config as cfg
from src.config import Config
from src.domain import (
    CostRate,
    Dataset,
    NotesMeta,
    SupplyRow,
    Waybill,
)
from src.sensitivity import SensitivityLevel
from src.time_axis import TimeAxis

WIDE_NAME = "中东大箱体积_渠道子仓_每小时_1001-1020.xlsx"
BILL_NAME = "提单_1001-1020提交.xlsx"

NOTES_ROWS = [["数据说明"]] + [[f"{i}、规则{i}"] for i in range(1, cfg.NOTES_RULE_COUNT + 1)]

BASE_ROWS = [
    # 渠道ID, 注入点, 子仓ID, 敏感品, 合计体积, 480 个小时值
    ["1001", "DXB", "505", "锂电产品", 0.0],
    ["1001", "DXB", "260", "敏感普货", 0.0],
    ["1002", "SHJ", "101", "无", 0.0],
]
BASE_WAYBILLS = [
    ["235-0001", "DXB", 2.0, "2026-10-01 00:00:00", "2026-10-03 00:00:00",
     "2026-10-01 01:00:00", "按重量", "锂电产品"],
    ["235-0002", "SHJ", 1.5, "2026-10-01 00:00:00", "2026-10-03 00:00:00",
     "2026-10-01 01:00:00", "按重量", "敏感普货"],
    ["235-0003", "DXB", 3.0, "2026-09-29 07:00:00", "2026-10-02 08:00:00",
     "2026-09-30 07:00:00", "包机", "无（普货）"],
    ["235-0004", "AMM", 1.0, "2026-10-25 12:00:00", "2026-10-24 12:00:00",
     "2026-10-24 10:00:00", "按重量", "干电产品"],
]


def _hour_headers() -> List[str]:
    """按时间轴生成 480 个小时列名。"""
    axis = TimeAxis.from_config(Config.load())
    return axis.all_column_names()


def _wide_matrix(rows: List[List[Any]], hour_values: Dict[Tuple[int, int], float]) -> List[List[Any]]:
    """构造宽表数据行；hour_values 的键是 (行号, 小时索引)。"""
    out: List[List[Any]] = []
    for r_index, base in enumerate(rows):
        line = list(base)
        values = [0.0] * cfg.HOURS
        for (ri, h), value in hour_values.items():
            if ri == r_index:
                values[h] = value
        numeric = [v for v in values if isinstance(v, (int, float))]
        line[4] = round(sum(numeric), 2)
        out.append(line + values)
    return out


def _write_wide(ws: Any, rows: List[List[Any]], headers: List[str]) -> None:
    ws.append(list(cfg.WIDE_FIXED_COLUMNS) + headers)
    for row in rows:
        ws.append(row)


def _write_notes(ws: Any, rules: List[List[Any]] = NOTES_ROWS) -> None:
    for row in rules:
        ws.append(row)
        ws.append([None])  # 每条规则后留一个空行，模拟真实排版


def _write_waybills(ws: Any, bills: List[List[Any]]) -> None:
    header = [
        "提单号", "提交时间", "航班路由", "目的港代码", "计划体积(m³)",
        "计划起飞时间", "计划到达时间", "需求到站时间", "最早提货时间",
        "计费类型", "锁版舱位号", "舱位编码", "舱位可走敏感品类型",
        "杂费单价（按重量）（元/kg）", "杂费（按提单）（元/每单）",
        "杂费重量取值", None, None,
    ]
    ws.append(header)
    for bill in bills:
        bill_no, port, plan, earliest, arrival, departure, billing, sensitive = bill
        ws.append([
            bill_no, earliest, "HKG-DXB", port, plan, departure,
            arrival, arrival, earliest, billing, "LOCK", "CODE", sensitive,
            None, None, None, None, None,
        ])


def _write_cost(ws: Any) -> None:
    ws.append(list(cfg.COST_REQUIRED_COLUMNS))
    for port in ("DXB", "SHJ", "AMM", "DWC"):
        ws.append([port, 20.0])


def _build_pair(
    directory: Path,
    *,
    wide_rows: List[List[Any]] = None,
    bills: List[List[Any]] = None,
    notes: List[List[Any]] = NOTES_ROWS,
    headers: List[str] = None,
    omit_notes: bool = False,
    omit_density: bool = False,
    omit_cost: bool = False,
    density_empty: bool = False,
    extra_trailing_columns: int = cfg.WAYBILL_TRAILING_EMPTY_COLUMNS,
) -> Path:
    """在目录里写一对合成 xlsx，返回目录路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    hour_headers = headers if headers is not None else _hour_headers()
    rows = wide_rows if wide_rows is not None else _wide_matrix(
        BASE_ROWS, {(0, 48): 1.5, (1, 48): 0.75, (2, 100): 0.25}
    )
    bills = bills if bills is not None else BASE_WAYBILLS

    wide_wb = openpyxl.Workbook()
    if not omit_notes:
        _write_notes(wide_wb.active, notes)
        wide_wb.active.title = cfg.SHEET_NOTES
    else:
        wide_wb.active.title = "占位"
    wide_ws = wide_wb.create_sheet(cfg.SHEET_WIDE)
    _write_wide(wide_ws, rows, hour_headers)
    if not omit_density:
        density_ws = wide_wb.create_sheet(cfg.SHEET_DENSITY)
        # 列顺序：渠道ID, 子仓ID, 封箱日期, 密度(kg/m³), 体积(m³), 重量(kg)
        density_ws.append([
            "渠道ID", "子仓ID", "封箱日期", "密度(kg/m³)", "体积(m³)", "重量(kg)",
        ])
        for row in BASE_ROWS:
            density_ws.append([
                row[0], row[2], "2026-10-03",
                None if density_empty else 100.0, 1.0, 100.0,
            ])
    wide_wb.save(directory / WIDE_NAME)
    wide_wb.close()

    bill_wb = openpyxl.Workbook()
    ws = bill_wb.active
    ws.title = cfg.SHEET_WAYBILL
    _write_waybills(ws, bills)
    if not omit_cost:
        _write_cost(bill_wb.create_sheet(cfg.SHEET_COST))
    bill_wb.save(directory / BILL_NAME)
    bill_wb.close()
    return directory


def build_valid(directory: Path) -> Path:
    """合法的最小样例。"""
    return _build_pair(directory)


def build_empty_wide(directory: Path) -> Path:
    """宽表只有表头、没有数据行。"""
    return _build_pair(directory, wide_rows=[])


def build_missing_column(directory: Path) -> Path:
    """宽表缺「敏感品」列。"""
    directory.mkdir(parents=True, exist_ok=True)
    hour_headers = _hour_headers()
    rows = _wide_matrix(BASE_ROWS, {(0, 48): 1.5})
    wide_wb = openpyxl.Workbook()
    _write_notes(wide_wb.active)
    wide_wb.active.title = cfg.SHEET_NOTES
    ws = wide_wb.create_sheet(cfg.SHEET_WIDE)
    fixed = [c for c in cfg.WIDE_FIXED_COLUMNS if c != "敏感品"]
    ws.append(fixed + hour_headers)
    for row in rows:
        ws.append([row[0], row[1], row[2], row[4]] + row[5:])
    wide_wb.save(directory / WIDE_NAME)
    wide_wb.close()

    bill_wb = openpyxl.Workbook()
    ws = bill_wb.active
    ws.title = cfg.SHEET_WAYBILL
    _write_waybills(ws, BASE_WAYBILLS)
    _write_cost(bill_wb.create_sheet(cfg.SHEET_COST))
    bill_wb.save(directory / BILL_NAME)
    bill_wb.close()
    return directory


def build_bad_notes(directory: Path) -> Path:
    """备注只有 8 条规则。"""
    return _build_pair(directory, notes=[["数据说明"]] + [[f"{i}、规则"] for i in range(1, 8)])


def build_missing_notes_sheet(directory: Path) -> Path:
    """缺备注工作表。"""
    return _build_pair(directory, omit_notes=True)


def build_negative_volume(directory: Path) -> Path:
    """某个小时格为负数。"""
    return _build_pair(directory, wide_rows=_wide_matrix(
        BASE_ROWS, {(0, 48): -1.5, (1, 48): 0.75}
    ))


def build_nan_volume(directory: Path) -> Path:
    """某个小时格是 NaN / inf 字符串（xlsx 会丢弃原生 NaN，故用字符串模拟）。"""
    return _build_pair(directory, wide_rows=_wide_matrix(
        BASE_ROWS, {(0, 48): "nan", (1, 48): "inf"}
    ))


def build_non_numeric_volume(directory: Path) -> Path:
    """某个小时格是非数值字符串。"""
    return _build_pair(directory, wide_rows=_wide_matrix(
        BASE_ROWS, {(0, 48): "abc", (1, 48): 0.75}
    ))


def build_bad_time(directory: Path) -> Path:
    """含非法时间串；同时写足够多的合法行，使失败率低于 1%。"""
    bills: List[List[Any]] = []
    bills.append([
        "235-BAD", "DXB", 1.0, "不是时间", "2026-10-03 00:00:00",
        "2026-10-01 01:00:00", "按重量", "无（普货）",
    ])
    for i in range(150):
        bills.append([
            f"235-{i:04d}", "DXB", 1.0, "2026-10-01 00:00:00",
            "2026-10-03 00:00:00", "2026-10-01 01:00:00",
            "按重量", "无（普货）",
        ])
    return _build_pair(directory, bills=bills)


def build_time_failure_over_limit(directory: Path) -> Path:
    """全部提单时间串非法，失败率 100%，应触发整表报错。"""
    bills = [
        ["235-X", "DXB", 1.0, "非法", "非法", "非法", "按重量", "无（普货）"],
    ]
    return _build_pair(directory, bills=bills)


def build_duplicate_bill(directory: Path) -> Path:
    """重复提单号。"""
    bills = list(BASE_WAYBILLS) + [list(BASE_WAYBILLS[0])]
    return _build_pair(directory, bills=bills)


def build_all_zero(directory: Path) -> Path:
    """全零货量。"""
    return _build_pair(directory, wide_rows=_wide_matrix(BASE_ROWS, {}))


def build_empty_density(directory: Path) -> Path:
    """密度为空。"""
    return _build_pair(directory, density_empty=True)


def build_bad_hour_column(directory: Path) -> Path:
    """小时列名缺 1 个且左端不连续。"""
    headers = _hour_headers()
    headers = headers[:200] + ["2026-10-30 00:00-01:00"] + headers[201:]
    return _build_pair(directory, headers=headers)


def build_existing_csv(directory: Path) -> Path:
    """合法样例 + 结果目录里已存在 allocation.csv。"""
    result = _build_pair(directory)
    out = directory / "results"
    out.mkdir(exist_ok=True)
    (out / "allocation.csv").write_text("已存在\n", encoding=cfg.CSV_ENCODING)
    return result


def make_all(root: Path) -> Dict[str, Path]:
    """一次生成全部边界样例，返回 {样例名: 目录}。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    builders = {
        "valid": build_valid,
        "empty_wide": build_empty_wide,
        "missing_column": build_missing_column,
        "bad_notes": build_bad_notes,
        "missing_notes_sheet": build_missing_notes_sheet,
        "negative_volume": build_negative_volume,
        "nan_volume": build_nan_volume,
        "non_numeric_volume": build_non_numeric_volume,
        "bad_time": build_bad_time,
        "time_failure_over_limit": build_time_failure_over_limit,
        "duplicate_bill": build_duplicate_bill,
        "all_zero": build_all_zero,
        "empty_density": build_empty_density,
        "bad_hour_column": build_bad_hour_column,
        "existing_csv": build_existing_csv,
    }
    return {name: builder(root / name) for name, builder in builders.items()}


def config_for(directory: Path, **overrides: Any) -> Config:
    """构造指向合成样例目录的 Config。"""
    base: Dict[str, Any] = {"data_dir": str(directory)}
    base.update(overrides)
    return Config.load(overrides=base)


def tiny_dataset() -> Dataset:
    """构造一个纯内存的极小 Dataset，供求解器单测使用（不读 xlsx）。"""
    axis = TimeAxis.from_config(Config.load())
    rows: List[SupplyRow] = []
    specs = [
        ("C1", "DXB", "W1", "锂电产品", {(48): 1.5, (49): 1.0}),
        ("C2", "DXB", "W2", "敏感普货", {(48): 0.75, (50): 2.0}),
        ("C3", "SHJ", "W1", "无", {(60): 0.5}),
    ]
    for index, (channel, injection, warehouse, sensitive, cells) in enumerate(specs):
        volumes = [0] * cfg.HOURS
        for h, value in cells.items():
            volumes[h] = int(round(value * cfg.VOLUME_UNIT))
        rows.append(SupplyRow(
            channel_id=channel, channel_injection=injection,
            warehouse_id=warehouse, sensitive_raw=sensitive,
            level=SensitivityLevel(cfg.LEVEL_LITHIUM) if "锂电" in sensitive
            else SensitivityLevel(cfg.LEVEL_SENSITIVE_GENERAL) if "敏感" in sensitive
            else SensitivityLevel(cfg.LEVEL_NONE),
            hourly_volumes=volumes, row_index=index,
            exact_sum_int=sum(volumes),
        ))
    base = datetime.strptime(cfg.AXIS_START_STR, cfg.TIME_FORMAT)
    waybills = [
        Waybill(
            bill_no="B1", dest_port="DXB", plan_volume=30000,
            earliest_pickup=base + timedelta(hours=48),
            required_arrival=base + timedelta(hours=72),
            latest_pickup=base + timedelta(hours=72),
            planned_departure=base + timedelta(hours=48),
            billing_type=cfg.BILLING_BY_WEIGHT,
            sensitive_raw="锂电产品", level=SensitivityLevel(cfg.LEVEL_LITHIUM),
        ),
        Waybill(
            bill_no="B2", dest_port="SHJ", plan_volume=10000,
            earliest_pickup=base + timedelta(hours=48),
            required_arrival=base + timedelta(hours=80),
            latest_pickup=base + timedelta(hours=80),
            planned_departure=base + timedelta(hours=48),
            billing_type=cfg.BILLING_BY_WEIGHT,
            sensitive_raw="无（普货）", level=SensitivityLevel(cfg.LEVEL_NONE),
        ),
        Waybill(
            bill_no="B3", dest_port="DXB", plan_volume=5000,
            earliest_pickup=base + timedelta(days=30),
            required_arrival=base + timedelta(days=31),
            latest_pickup=base + timedelta(days=31),
            planned_departure=base + timedelta(days=30),
            billing_type=cfg.BILLING_CHARTER,
            sensitive_raw="敏感普货", level=SensitivityLevel(cfg.LEVEL_SENSITIVE_GENERAL),
        ),
    ]
    cost = CostRate(by_port={"DXB": 25.0, "SHJ": 20.0, "AMM": 30.0})
    from src.injection import build_injection_index
    from src.sensitivity import build_row_level_index

    return Dataset(
        supply_rows=rows, densities=[], waybills=waybills, cost_rates=cost,
        axis=axis, notes_meta=NotesMeta(True, cfg.NOTES_RULE_COUNT, True),
        hour_column_names=axis.all_column_names(),
        source_hashes={"before": {}, "after": {}},
        sensitive_by_cw=build_row_level_index(rows),
        injection_by_cw=build_injection_index(rows),
        density_by_cwd={},
        total_supply_volume=sum(sum(r.hourly_volumes) for r in rows),
        nonzero_cells=sum(1 for r in rows for v in r.hourly_volumes if v > 0),
    )


__all__ = [
    "make_all", "config_for", "tiny_dataset", "WIDE_NAME", "BILL_NAME",
    "build_valid", "build_existing_csv", "BASE_WAYBILLS", "BASE_ROWS",
]
