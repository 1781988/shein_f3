"""openpyxl 只读加载五张表、schema 校验、备注结构核对。

体积守恒只取 480 个小时列的原始四位小数值；「合计体积」列只用来算差异，
密度表只取密度列。原始 xlsx 全程只读，加载前后各算一次 SHA-256 确认未变。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import openpyxl

from . import config as cfg
from .config import Config
from .domain import (
    CostRate,
    DataIssue,
    Dataset,
    DensityRecord,
    NotesMeta,
    SupplyRow,
    Waybill,
)
from .errors import DataFileError, DataValidationError, SchemaError
from .injection import build_injection_index
from .precision import (
    format_date,
    declared_total_to_int,
    is_bad_volume,
    parse_date,
    parse_time,
    vol_to_int,
)
from .sensitivity import build_row_level_index, parse_level, unknown_tokens
from .time_axis import TimeAxis

_RULE_RE = re.compile(r"^\s*\d+\s*[、.．]")
_TIME_FAILURE_LIMIT_RATIO = 0.01


def sha256_file(path: Path) -> str:
    """计算文件 SHA-256，用于确认加载前后源文件未被改动。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_header(ws: Any, label: str) -> Tuple[List[Any], Dict[str, int]]:
    """读首行表头，去掉末尾全空列，返回 (原始表头, 列名->索引)。"""
    try:
        row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    except StopIteration as exc:
        raise SchemaError(f"工作表为空：{label}", location=label) from exc
    header = list(row)
    while header and header[-1] is None:
        header.pop()
    mapping: Dict[str, int] = {}
    for index, name in enumerate(header):
        if name is None:
            continue
        key = str(name).strip()
        if key and key not in mapping:
            mapping[key] = index
    if not mapping:
        raise SchemaError(
            f"工作表首行没有可用表头：{label}", location=label,
            detail=f"实际表头 {header!r}",
        )
    return header, mapping


def _require_columns(
    mapping: Mapping[str, int], required: Sequence[str], label: str
) -> None:
    """按必需列名集合校验，报错列出实际表头与缺失列名。"""
    missing = [name for name in required if name not in mapping]
    if missing:
        raise SchemaError(
            f"工作表缺必需列：{label}",
            location=label,
            detail=f"缺失列名 {missing}；实际表头 {list(mapping)}",
        )


def _check_notes(ws: Any) -> NotesMeta:
    """校验「数据使用备注」存在且有 9 条规则，只返回存在性与条数，不存原文。"""
    title_present = False
    rule_count = 0
    for row in ws.iter_rows(values_only=True):
        first = row[0] if row else None
        text = str(first).strip() if first is not None else ""
        if not text:
            continue
        if _RULE_RE.match(text):
            rule_count += 1
        elif "数据说明" in text:
            title_present = True
    meta = NotesMeta(sheet_exists=True, rule_count=rule_count, title_present=title_present)
    if rule_count != cfg.NOTES_RULE_COUNT:
        raise SchemaError(
            "「数据使用备注」规则条数不对",
            location=cfg.SHEET_NOTES,
            detail=(
                f"期望 {cfg.NOTES_RULE_COUNT} 条，实际 {rule_count} 条；"
                "规则本身以代码常量实现，不因文本变化改变行为"
            ),
        )
    return meta


def _sheet(wb: Any, name: str, label: str) -> Any:
    """按表名取工作表，缺表时报 SchemaError 并列出实际表名。"""
    if name not in wb.sheetnames:
        raise SchemaError(
            f"工作表缺失：{label}",
            location=label,
            detail=f"期望表名 {name!r}，实际表名 {list(wb.sheetnames)}",
        )
    ws = wb[name]
    if ws is None:
        raise SchemaError(f"工作表为空：{label}", location=label)
    return ws


def _issue(category: str, location: str, raw: Any, action: str) -> DataIssue:
    """构造一条边界问题记录。"""
    text = "" if raw is None else str(raw)
    return DataIssue(category=category, location=location, raw_value=text, action=action)


def _load_wide_table(
    ws: Any, axis: TimeAxis, issues: List[DataIssue]
) -> Tuple[List[SupplyRow], List[str], int, int, int]:
    """加载货源宽表，返回 (行, 小时列名, 非零格数, 合计差异总数, 最大差异)。"""
    header, mapping = _read_header(ws, cfg.SHEET_WIDE)
    _require_columns(mapping, cfg.WIDE_FIXED_COLUMNS, cfg.SHEET_WIDE)
    fixed_count = len(cfg.WIDE_FIXED_COLUMNS)
    if len(header) < fixed_count + cfg.HOURS:
        raise SchemaError(
            f"货源宽表列数不足：{cfg.SHEET_WIDE}",
            location=cfg.SHEET_WIDE,
            detail=f"实际 {len(header)} 列，期望至少 {fixed_count + cfg.HOURS} 列",
        )
    column_names = [str(x) for x in header[fixed_count: fixed_count + cfg.HOURS]]
    problems = axis.verify_columns(column_names)
    if problems:
        raise SchemaError(
            f"货源宽表 480 个小时列名校验失败：{cfg.SHEET_WIDE}",
            location=cfg.SHEET_WIDE,
            detail="; ".join(f"第{i}列: {msg}" for i, msg in problems[:5]),
        )

    idx = {name: mapping[name] for name in cfg.WIDE_FIXED_COLUMNS}
    cache: Dict[Any, int] = {}
    rows: List[SupplyRow] = []
    nonzero_cells = 0
    total_diff = 0
    max_diff = 0
    row_number = 1
    for raw in ws.iter_rows(min_row=2, values_only=True):
        row_number += 1
        if not raw or all(cell is None for cell in raw[:fixed_count]):
            continue
        try:
            channel = _as_id(raw[idx["渠道ID"]])
            injection = _as_id(raw[idx["渠道注入点"]])
            warehouse = _as_id(raw[idx["子仓ID"]])
            sensitive_raw = "" if raw[idx["敏感品"]] is None else str(raw[idx["敏感品"]])
        except IndexError as exc:
            raise SchemaError(
                "货源宽表数据行列数不足", location=f"{cfg.SHEET_WIDE}!第{row_number}行"
            ) from exc

        volumes: List[int] = []
        exact_sum = Decimal(0)
        for h in range(cfg.HOURS):
            col = fixed_count + h
            value = raw[col] if col < len(raw) else None
            if value is None or (isinstance(value, str) and not value.strip()):
                volumes.append(0)
                continue
            bad = is_bad_volume(value)
            if bad:
                raise DataValidationError(
                    "货源小时格数值非法",
                    location=f"{cfg.SHEET_WIDE}!第{row_number}行/列{column_names[h]}",
                    detail=f"{bad}；原值 {value!r}",
                )
            key = value if isinstance(value, (int, float, str)) else str(value)
            if key not in cache:
                cache[key] = vol_to_int(value)
            amount = cache[key]
            volumes.append(amount)
            if amount > 0:
                nonzero_cells += 1
            try:
                exact_sum += Decimal(str(value))
            except InvalidOperation:
                pass

        declared = 0
        declared_raw = raw[idx[cfg.WIDE_TOTAL_COLUMN]]
        if declared_raw is not None and str(declared_raw).strip():
            bad = is_bad_volume(declared_raw)
            if bad:
                raise DataValidationError(
                    "「合计体积」列数值非法",
                    location=f"{cfg.SHEET_WIDE}!第{row_number}行/合计体积",
                    detail=f"{bad}；原值 {declared_raw!r}",
                )
            declared = declared_total_to_int(declared_raw)
        summed = sum(volumes)
        exact_sum_int = int((exact_sum * Decimal(cfg.VOLUME_UNIT)).to_integral_value())
        diff = abs(declared - summed)
        total_diff += diff
        max_diff = max(max_diff, diff)
        if diff > 0:
            issues.append(_issue(
                cfg.ISSUE_CATEGORY["numeric"],
                f"{cfg.SHEET_WIDE}!第{row_number}行",
                f"合计体积={declared / cfg.VOLUME_UNIT:.4f}, "
                f"480列之和={summed / cfg.VOLUME_UNIT:.4f}",
                "忽略合计体积列，只用 480 小时列的原始四位小数；差异记入核对，不算错误",
            ))

        for token in unknown_tokens(sensitive_raw):
            issues.append(_issue(
                cfg.ISSUE_CATEGORY["schema"],
                f"{cfg.SHEET_WIDE}!第{row_number}行/敏感品",
                token, "未知敏感品写法按等级 0 处理并记录，不静默丢弃",
            ))
        rows.append(SupplyRow(
            channel_id=channel,
            channel_injection=injection,
            warehouse_id=warehouse,
            sensitive_raw=sensitive_raw,
            level=parse_level(sensitive_raw),
            hourly_volumes=volumes,
            row_index=len(rows),
            declared_total=declared,
            exact_sum_int=exact_sum_int,
        ))

    if not rows:
        raise SchemaError(
            f"工作表没有数据行：{cfg.SHEET_WIDE}", location=cfg.SHEET_WIDE
        )
    issues.append(_issue(
        cfg.ISSUE_CATEGORY["numeric"], cfg.SHEET_WIDE,
        f"{len(rows)} 行",
        f"合计体积列与 480 列之和的四舍五入差异：累计 {total_diff / cfg.VOLUME_UNIT:.4f} "
        f"立方米，单行最大 {max_diff / cfg.VOLUME_UNIT:.4f} 立方米；守恒基准只取 480 列",
    ))
    return rows, column_names, nonzero_cells, total_diff, max_diff


def _load_density(
    ws: Any, issues: List[DataIssue]
) -> Tuple[List[DensityRecord], int]:
    """加载密度表，只取密度列；空密度记 None 并统计缺失笔数。"""
    _, mapping = _read_header(ws, cfg.SHEET_DENSITY)
    _require_columns(mapping, cfg.DENSITY_REQUIRED_COLUMNS, cfg.SHEET_DENSITY)
    records: List[DensityRecord] = []
    empty_count = 0
    row_number = 1
    for raw in ws.iter_rows(min_row=2, values_only=True):
        row_number += 1
        if not raw or all(cell is None for cell in raw[: len(cfg.DENSITY_REQUIRED_COLUMNS)]):
            continue
        channel = _as_id(raw[mapping["渠道ID"]])
        warehouse = _as_id(raw[mapping["子仓ID"]])
        date_raw = raw[mapping["封箱日期"]]
        density_raw = raw[mapping["密度(kg/m³)"]]
        try:
            pack_date = format_date(parse_date(date_raw))
        except (ValueError, TypeError):
            issues.append(_issue(
                cfg.ISSUE_CATEGORY["density"],
                f"{cfg.SHEET_DENSITY}!第{row_number}行/封箱日期",
                date_raw, "跳过该密度记录，相关配货成本记 0",
            ))
            continue
        density: Optional[float] = None
        if density_raw is None or (isinstance(density_raw, str) and not density_raw.strip()):
            empty_count += 1
        else:
            try:
                value = float(density_raw)
                if value != value or value in (float("inf"), float("-inf")) or value < 0:
                    raise ValueError("非有限或非负")
                density = value
            except (TypeError, ValueError) as exc:
                raise DataValidationError(
                    "密度表密度列数值非法",
                    location=f"{cfg.SHEET_DENSITY}!第{row_number}行/密度(kg/m³)",
                    detail=f"原值 {density_raw!r}；{exc}",
                ) from exc
        records.append(DensityRecord(
            channel_id=channel, warehouse_id=warehouse,
            pack_date=pack_date, density=density,
        ))
    issues.append(_issue(
        cfg.ISSUE_CATEGORY["density"], cfg.SHEET_DENSITY,
        f"密度为空 {empty_count} 行",
        "密度为空（当天体积为 0）时该笔成本记 0，不插值、不用均值填充",
    ))
    issues.append(_issue(
        cfg.ISSUE_CATEGORY["numeric"], cfg.SHEET_DENSITY,
        "体积/重量列",
        "只取密度列；体积/重量列与宽表按日汇总存在 0.0004 立方米量级差异，不用于守恒",
    ))
    return records, empty_count


def _load_waybills(
    ws: Any, issues: List[DataIssue]
) -> Tuple[List[Waybill], int, int, int, int]:
    """加载提单明细，返回 (提单, 跳过行数, 重复数, 异常数, SHJ 数)。"""
    header, mapping = _read_header(ws, cfg.SHEET_WAYBILL)
    trailing = sum(1 for cell in header if cell is None)
    _require_columns(mapping, cfg.WAYBILL_REQUIRED_COLUMNS, cfg.SHEET_WAYBILL)
    if trailing != cfg.WAYBILL_TRAILING_EMPTY_COLUMNS:
        issues.append(_issue(
            cfg.ISSUE_CATEGORY["schema"], cfg.SHEET_WAYBILL,
            f"实际表头 {len(header)} 列，末尾空列 {trailing} 个",
            f"按预期忽略末尾 {cfg.WAYBILL_TRAILING_EMPTY_COLUMNS} 个全空列，不报错",
        ))

    col = {key: mapping[name] for key, name in cfg.WAYBILL_FIELD_COLUMNS.items()}
    waybills: List[Waybill] = []
    seen: Dict[str, int] = {}
    skipped = 0
    duplicates = 0
    abnormal = 0
    shj = 0
    total_rows = 0
    row_number = 1
    for raw in ws.iter_rows(min_row=2, values_only=True):
        row_number += 1
        if not raw or all(cell is None for cell in raw[: len(header)]):
            continue
        total_rows += 1
        location = f"{cfg.SHEET_WAYBILL}!第{row_number}行"
        bill_no = _as_id(raw[col["bill_no"]])
        if not bill_no:
            skipped += 1
            issues.append(_issue(
                cfg.ISSUE_CATEGORY["time"], location, raw[col["bill_no"]],
                "提单号为空，跳过该行",
            ))
            continue
        if bill_no in seen:
            duplicates += 1
            issues.append(_issue(
                cfg.ISSUE_CATEGORY["business"], location, bill_no,
                f"重复提单号，保留第 {seen[bill_no]} 行首次出现，跳过本行",
            ))
            continue
        try:
            earliest = parse_time(raw[col["earliest_pickup"]])
            required = parse_time(raw[col["required_arrival"]])
            departure = parse_time(raw[col["planned_departure"]])
        except (ValueError, TypeError) as exc:
            skipped += 1
            issues.append(_issue(
                cfg.ISSUE_CATEGORY["time"], location,
                f"最早={raw[col['earliest_pickup']]!r},"
                f"到站={raw[col['required_arrival']]!r},"
                f"起飞={raw[col['planned_departure']]!r}",
                f"时间串解析失败，跳过该行：{exc}",
            ))
            continue
        plan_raw = raw[col["plan_volume"]]
        bad = is_bad_volume(plan_raw)
        if bad:
            raise DataValidationError(
                "提单计划体积非法", location=f"{location}/计划体积(m³)",
                detail=f"{bad}；原值 {plan_raw!r}",
            )
        plan_volume = vol_to_int(plan_raw)
        dest_port = _as_id(raw[col["dest_port"]])
        billing = _as_id(raw[col["billing_type"]])
        sensitive_raw = "" if raw[col["sensitive_raw"]] is None else str(raw[col["sensitive_raw"]])

        is_abnormal = earliest > required
        if is_abnormal:
            abnormal += 1
            latest = earliest + timedelta(hours=cfg.ABNORMAL_FALLBACK_HOURS)
        else:
            latest = required
        if dest_port == cfg.SHJ_PORT:
            shj += 1
        if departure < earliest:
            issues.append(_issue(
                cfg.ISSUE_CATEGORY["info"], location,
                f"起飞={departure}, 最早提货={earliest}",
                "计划起飞早于最早提货，仅作参考字段，不参与过滤、不进诊断",
            ))
        if earliest < datetime.strptime(cfg.AXIS_START_STR, cfg.TIME_FORMAT) \
                or earliest > datetime.strptime(cfg.AXIS_END_STR, cfg.TIME_FORMAT):
            issues.append(_issue(
                cfg.ISSUE_CATEGORY["info"], location, earliest,
                "最早提货时间落在时间轴外，属正常数据事实，正常纳入可见性判断，不虚构截断",
            ))
        for token in unknown_tokens(sensitive_raw):
            issues.append(_issue(
                cfg.ISSUE_CATEGORY["schema"], f"{location}/舱位可走敏感品类型",
                token, "未知敏感品写法按等级 0 处理并记录",
            ))

        seen[bill_no] = row_number
        waybills.append(Waybill(
            bill_no=bill_no, dest_port=dest_port, plan_volume=plan_volume,
            earliest_pickup=earliest, required_arrival=required,
            latest_pickup=latest, planned_departure=departure,
            billing_type=billing, sensitive_raw=sensitive_raw,
            level=parse_level(sensitive_raw), is_abnormal=is_abnormal,
        ))

    failure_ratio = skipped / total_rows if total_rows else 0.0
    if failure_ratio > _TIME_FAILURE_LIMIT_RATIO:
        raise DataValidationError(
            "提单表时间/主键解析失败率超过 1%",
            location=cfg.SHEET_WAYBILL,
            detail=f"跳过 {skipped}/{total_rows} 行，失败率 {failure_ratio:.4f}",
        )
    issues.append(_issue(
        cfg.ISSUE_CATEGORY["business"], cfg.SHEET_WAYBILL,
        f"异常提单 {abnormal} 张",
        f"最晚提货 = {cfg.ABNORMAL_FALLBACK_TEXT}；计划起飞不约束；标记 is_abnormal 供审计核对",
    ))
    issues.append(_issue(
        cfg.ISSUE_CATEGORY["business"], cfg.SHEET_WAYBILL,
        f"SHJ 提单 {shj} 张",
        "目的港为 SHJ 时所有渠道候选都算备选，无主注入点，不因此产生硬约束",
    ))
    issues.append(_issue(
        cfg.ISSUE_CATEGORY["business"], cfg.SHEET_WAYBILL,
        f"评估范围外时段（10.1—10.2、10.18—10.20）",
        "全 480 小时货源都参与配载、明细全量输出；货源方数类指标只统计 10.3—10.17 的记录集合 A，"
        "配满率与虚仓提单数量按提单侧完整配载结果与全部提单统计",
    ))
    issues.append(_issue(
        cfg.ISSUE_CATEGORY["business"], cfg.SHEET_WAYBILL,
        "杂费三列",
        "杂费单价/杂费/杂费重量取值不计入成本（A4），只有 2 张提单有值，量级可忽略",
    ))
    if not waybills:
        raise SchemaError(
            f"工作表没有有效提单：{cfg.SHEET_WAYBILL}", location=cfg.SHEET_WAYBILL
        )
    return waybills, skipped, duplicates, abnormal, shj


def _load_cost_rates(ws: Any, issues: List[DataIssue]) -> CostRate:
    """加载目的港单公斤成本。"""
    _, mapping = _read_header(ws, cfg.SHEET_COST)
    _require_columns(mapping, cfg.COST_REQUIRED_COLUMNS, cfg.SHEET_COST)
    port_col = mapping[cfg.COST_REQUIRED_COLUMNS[0]]
    rate_col = mapping[cfg.COST_REQUIRED_COLUMNS[1]]
    by_port: Dict[str, float] = {}
    for raw in ws.iter_rows(min_row=2, values_only=True):
        if not raw or raw[port_col] is None:
            continue
        port = _as_id(raw[port_col])
        value = raw[rate_col]
        try:
            by_port[port] = float(value)
        except (TypeError, ValueError) as exc:
            raise DataValidationError(
                "目的港单公斤成本数值非法",
                location=f"{cfg.SHEET_COST}!目的港={port}",
                detail=f"原值 {value!r}；{exc}",
            ) from exc
    if not by_port:
        raise SchemaError(
            f"工作表没有数据行：{cfg.SHEET_COST}", location=cfg.SHEET_COST
        )
    issues.append(_issue(
        cfg.ISSUE_CATEGORY["info"], cfg.SHEET_COST,
        f"{len(by_port)} 个目的港",
        "单公斤成本按提单目的港代码取值；包机整张计 0；密度为空则该笔成本记 0",
    ))
    return CostRate(by_port=by_port)


def _as_id(value: Any) -> str:
    """把 ID 单元格统一成字符串，整数去掉 .0 尾巴。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def load_all(cfg_obj: Config) -> Dataset:
    """只读打开两个 xlsx，逐张表加载，返回 Dataset。

    内部按 ``_check_notes / _load_wide_table / _load_density / _load_waybills /
    _load_cost_rates`` 顺序执行；加载前后各算一次 SHA-256 确认源文件未变。
    """
    wide_path = cfg_obj.wide_path()
    bill_path = cfg_obj.bill_path()
    for target in (wide_path, bill_path):
        if not target.is_file():
            raise DataFileError(
                "原始数据文件缺失或路径写错",
                path=str(target),
                detail="请检查 --data-dir 与 config/settings.json 的文件名",
            )
        if not Path(target).stat().st_size > 0:
            raise DataFileError("原始数据文件为空", path=str(target))

    hashes_before = {
        wide_path.name: sha256_file(wide_path),
        bill_path.name: sha256_file(bill_path),
    }
    wide_wb = bill_wb = None
    try:
        wide_wb = openpyxl.load_workbook(wide_path, read_only=True, data_only=True)
        bill_wb = openpyxl.load_workbook(bill_path, read_only=True, data_only=True)

        axis = TimeAxis.from_config(cfg_obj)
        issues: List[DataIssue] = []

        notes_ws = _sheet(wide_wb, cfg.SHEET_NOTES, "货源表/数据使用备注")
        notes_meta = _check_notes(notes_ws)

        wide_ws = _sheet(wide_wb, cfg.SHEET_WIDE, "货源表/每小时体积_宽表")
        supply_rows, column_names, nonzero, total_diff, max_diff = _load_wide_table(
            wide_ws, axis, issues
        )
        density_ws = _sheet(wide_wb, cfg.SHEET_DENSITY, "货源表/每天体积重量密度")
        densities, empty_density = _load_density(density_ws, issues)

        waybill_ws = _sheet(bill_wb, cfg.SHEET_WAYBILL, "提单表/提单明细")
        waybills, skipped, duplicates, abnormal, shj = _load_waybills(waybill_ws, issues)
        cost_ws = _sheet(bill_wb, cfg.SHEET_COST, "提单表/目的港单kg成本")
        cost_rates = _load_cost_rates(cost_ws, issues)
    except (DataFileError, SchemaError, DataValidationError):
        raise
    except OSError as exc:
        raise DataFileError(
            "原始数据文件损坏或被占用，无法只读打开",
            path=f"{wide_path} / {bill_path}", detail=str(exc),
        ) from exc
    except Exception as exc:  # openpyxl 解析异常统一转成 DataFileError
        raise DataFileError(
            "openpyxl 只读解析失败",
            path=f"{wide_path} / {bill_path}", detail=repr(exc),
        ) from exc
    finally:
        for wb in (wide_wb, bill_wb):
            if wb is not None:
                try:
                    wb.close()
                except OSError:
                    pass

    hashes_after = {
        wide_path.name: sha256_file(wide_path),
        bill_path.name: sha256_file(bill_path),
    }
    if hashes_before != hashes_after:
        raise DataFileError(
            "只读加载过程中源文件哈希发生变化",
            path=f"{wide_path} / {bill_path}",
            detail=f"加载前 {hashes_before}，加载后 {hashes_after}",
        )

    sensitive_index = build_row_level_index(supply_rows)
    injection_index = build_injection_index(supply_rows)
    density_index: Dict[Tuple[str, str, str], Optional[float]] = {}
    for record in densities:
        key = (record.channel_id, record.warehouse_id, record.pack_date)
        if key not in density_index:
            density_index[key] = record.density

    total_supply = sum(sum(row.hourly_volumes) for row in supply_rows)
    eval_indices = set(axis.eval_hour_indices())
    eval_supply = sum(
        row.hourly_volumes[h]
        for row in supply_rows for h in eval_indices
    )
    issues.append(_issue(
        cfg.ISSUE_CATEGORY["business"], cfg.SHEET_WIDE,
        f"非零格 {nonzero} 个 / 全零格 {len(supply_rows) * cfg.HOURS - nonzero} 个",
        "零格不进可用池，不生成记录，不报错；全零行、全零小时按正常数据事实处理",
    ))

    return Dataset(
        supply_rows=supply_rows,
        densities=densities,
        waybills=waybills,
        cost_rates=cost_rates,
        issues=issues,
        axis=axis,
        notes_meta=notes_meta,
        hour_column_names=column_names,
        source_hashes={"before": hashes_before, "after": hashes_after},
        sensitive_by_cw=sensitive_index,
        injection_by_cw=injection_index,
        density_by_cwd=density_index,
        skipped_waybills=skipped,
        duplicate_bill_count=duplicates,
        abnormal_count=abnormal,
        shj_bill_count=shj,
        empty_density_count=empty_density,
        total_declared_diff=total_diff,
        max_declared_diff=max_diff,
        total_supply_volume=total_supply,
        eval_range_supply_volume=eval_supply,
        nonzero_cells=nonzero,
    )


__all__ = ["load_all", "sha256_file"]
