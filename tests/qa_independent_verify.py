#!/usr/bin/env python3
"""QA 独立复核脚本（不复用 src/audit.py、src/metrics.py 的任何逻辑）。

从 results/allocation.csv 出发，重新只读加载两张原始 xlsx，自己重建
「渠道 × 子仓」行的敏感品等级 / 渠道注入点、480 个小时列的原始体积、
提单计划体积 / 目的港 / 时间窗 / 计费类型、密度与单 kg 成本，逐条核对
C1—C5、CSV 格式，并按 docs/06_指标口径.md 的公式独立复算七项指标。

用法（在项目根执行）：
    python tests/qa_independent_verify.py
    python tests/qa_independent_verify.py --json-out results/qa_independent_raw.json

退出码：0 = 独立复核全部通过；1 = 存在冲突或指标不一致；2 = 环境/文件缺失。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT.parent / "F3_一舱装满_资料包" / "data"
RESULTS = ROOT / "results"

VOLUME_UNIT = 10000            # 1 整数单位 = 0.0001 立方米
TOL_INT = 5                   # 0.0005 立方米 = 5 整数单位
VIRTUAL_GAP_INT = 1           # 0.0001 立方米 = 1 整数单位
EVAL_START = datetime(2026, 10, 3, 0, 0)
EVAL_END = datetime(2026, 10, 17, 23, 0)   # 含端点
EVAL_HOURS = 360
FREE_BACKLOG_HOURS = 24
ABNORMAL_FALLBACK_HOURS = 24
SHJ_PORT = "SHJ"
CHARTER = "包机"

TOKEN_LEVELS = {
    "无": 0, "无（普货）": 0, "无(普货)": 0,
    "敏感普货": 1, "普货": 1,
    "干电产品": 2, "干电": 2,
    "锂电产品": 3, "锂电": 3,
}
SEPARATORS = (",", "，", "、", ";", "；", "/")
SEP_RE = re.compile("[" + "".join(re.escape(c) for c in SEPARATORS) + "]")

H6 = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
H6S = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
VOL_RE = re.compile(r"^\d+\.\d{4}$")

EXPECTED_HEADER = ["提单号", "货源渠道", "子仓", "产出小时", "提货时刻", "配货方数"]


def dec(v) -> Decimal:
    """把 xlsx 单元格值转成 Decimal；空当作 0。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        return Decimal(0)
    return Decimal(str(v))


def to_int(v) -> int:
    """四位小数 -> 整数单位（四舍五入）。"""
    return int((dec(v) * VOLUME_UNIT).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def s_id(v) -> str:
    """ID 统一成字符串（原始表里是 int，CSV 里是 str）。"""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def parse_time(v) -> datetime:
    return datetime.strptime(str(v).strip(), "%Y-%m-%d %H:%M:%S")


def parse_hour(v) -> datetime:
    return datetime.strptime(str(v).strip(), "%Y-%m-%d %H:%M")


def parse_level(raw) -> int:
    """多层级逗号串取最高层级；未登记 token 按 0。"""
    if raw is None:
        return 0
    best = 0
    for tok in SEP_RE.split(str(raw)):
        t = tok.strip()
        if not t:
            continue
        lv = TOKEN_LEVELS.get(t)
        if lv is None:
            folded = t.replace("（", "(").replace("）", ")")
            lv = TOKEN_LEVELS.get(folded, 0)
        best = max(best, lv)
    return best


def fdate(v) -> str:
    """封箱日期统一成 YYYY-MM-DD。"""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v).strip()[:10]


# ---------------------------------------------------------------------------
# 1) 只读重建原始表反查索引
# ---------------------------------------------------------------------------
def load_raw(data_dir: Path) -> dict:
    wide = data_dir / "中东大箱体积_渠道子仓_每小时_1001-1020.xlsx"
    bill = data_dir / "提单_1001-1020提交.xlsx"
    for p in (wide, bill):
        if not p.is_file():
            raise FileNotFoundError(f"原始 xlsx 缺失：{p}")

    src_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (wide, bill)}

    # --- 宽表 ---
    wb = openpyxl.load_workbook(wide, read_only=True, data_only=True)
    try:
        ws = wb["每小时体积_宽表"]
        header = [str(c) if c is not None else "" for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        assert header[:5] == ["渠道ID", "渠道注入点", "子仓ID", "敏感品", "合计体积"], header[:5]
        hour_cols = header[5:5 + 480]
        assert len(hour_cols) == 480, len(hour_cols)
        hour_left = [c[:16] for c in hour_cols]
        left_set = set(hour_left)
        assert len(left_set) == 480, len(left_set)

        cell_m3: dict[tuple[str, str, str], Decimal] = {}
        level_by_cw: dict[tuple[str, str], int] = {}
        injection_by_cw: dict[tuple[str, str], str] = {}
        level_by_channel_only: dict[str, set[int]] = defaultdict(set)
        cw_rows = 0
        dup_cw = 0
        total_supply = Decimal(0)
        eval_supply = Decimal(0)
        nonzero_cells = 0

        for r in ws.iter_rows(min_row=2, values_only=True):
            if r[0] is None or all(x is None for x in r[:5]):
                continue
            cw_rows += 1
            c, inj, w = s_id(r[0]), s_id(r[1]), s_id(r[2])
            key = (c, w)
            if key in level_by_cw:
                dup_cw += 1
            lv = parse_level(r[3])
            level_by_cw.setdefault(key, lv)
            injection_by_cw.setdefault(key, inj)
            level_by_channel_only[c].add(lv)
            # 只用 480 个小时列的原始值；合计体积列、密度表体积列一律不用
            for i, v in enumerate(r[5:5 + 480]):
                d = dec(v)
                total_supply += d
                left = hour_left[i]
                if d > 0:
                    nonzero_cells += 1
                    cell_m3[(c, w, left)] = cell_m3.get((c, w, left), Decimal(0)) + d
                if EVAL_START <= parse_hour(left) <= EVAL_END:
                    eval_supply += d

        # --- 密度表：只取密度列 ---
        dws = wb["每天体积重量密度"]
        dh = [str(c) if c is not None else "" for c in next(dws.iter_rows(min_row=1, max_row=1, values_only=True))]
        di = {n: dh.index(n) for n in ["渠道ID", "子仓ID", "封箱日期", "密度(kg/m³)"]}
        density: dict[tuple[str, str, str], float | None] = {}
        density_empty = 0
        for r in dws.iter_rows(min_row=2, values_only=True):
            if r[di["渠道ID"]] is None:
                continue
            k = (s_id(r[di["渠道ID"]]), s_id(r[di["子仓ID"]]), fdate(r[di["封箱日期"]]))
            raw = r[di["密度(kg/m³)"]]
            if raw is None or str(raw).strip() == "":
                density[k] = None
                density_empty += 1
            else:
                density[k] = float(raw)
    finally:
        wb.close()

    # --- 提单表 ---
    wb = openpyxl.load_workbook(bill, read_only=True, data_only=True)
    try:
        ws = wb["提单明细"]
        bh = [str(c) if c is not None else "" for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        need = ["提单号", "目的港代码", "计划体积(m³)", "计划起飞时间", "需求到站时间",
                "最早提货时间", "计费类型", "舱位可走敏感品类型"]
        bi = {n: bh.index(n) for n in need}
        bills: dict[str, dict] = {}
        dup_bills = 0
        abnormal = 0
        shj = 0
        charter = 0
        plan_sum = 0
        for r in ws.iter_rows(min_row=2, values_only=True):
            if r[bi["提单号"]] is None:
                continue
            b = s_id(r[bi["提单号"]])
            if b in bills:
                dup_bills += 1
                continue
            earliest = parse_time(r[bi["最早提货时间"]])
            required = parse_time(r[bi["需求到站时间"]])
            is_abn = earliest > required
            latest = earliest + timedelta(hours=ABNORMAL_FALLBACK_HOURS) if is_abn else required
            plan = to_int(r[bi["计划体积(m³)"]])
            dest = s_id(r[bi["目的港代码"]])
            bills[b] = {
                "plan": plan, "dest": dest, "level": parse_level(r[bi["舱位可走敏感品类型"]]),
                "earliest": earliest, "required": required, "latest": latest,
                "departure": parse_time(r[bi["计划起飞时间"]]),
                "billing": s_id(r[bi["计费类型"]]), "abnormal": is_abn,
            }
            plan_sum += plan
            abnormal += int(is_abn)
            shj += int(dest == SHJ_PORT)
            charter += int(bills[b]["billing"] == CHARTER)

        # --- 成本表 ---
        cws = wb["目的港单kg成本"]
        cost: dict[str, float] = {}
        for r in cws.iter_rows(min_row=2, values_only=True):
            if r[0] is None:
                continue
            cost[s_id(r[0])] = float(r[1])
    finally:
        wb.close()

    return {
        "src_hashes": src_hashes,
        "hour_left": hour_left, "left_set": left_set,
        "cell_m3": cell_m3, "level_by_cw": level_by_cw,
        "injection_by_cw": injection_by_cw, "level_by_channel_only": dict(level_by_channel_only),
        "cw_rows": cw_rows, "dup_cw": dup_cw, "nonzero_cells": nonzero_cells,
        "total_supply": total_supply, "eval_supply": eval_supply,
        "density": density, "density_empty": density_empty,
        "bills": bills, "dup_bills": dup_bills, "abnormal": abnormal,
        "shj": shj, "charter": charter, "plan_sum": plan_sum, "cost": cost,
    }


# ---------------------------------------------------------------------------
# 2) 读 allocation.csv（字节级 + 格式）
# ---------------------------------------------------------------------------
def read_allocation(path: Path, raw: dict) -> dict:
    raw_bytes = path.read_bytes()
    fmt = {
        "bytes": len(raw_bytes),
        "bom": raw_bytes.startswith(b"\xef\xbb\xbf"),
        "crlf": b"\r\n" in raw_bytes,
        "lone_cr": b"\r" in raw_bytes.replace(b"\r\n", b""),
    }
    try:
        text = raw_bytes.decode("utf-8")
        fmt["utf8"] = True
    except UnicodeDecodeError:
        fmt["utf8"] = False
        text = raw_bytes.decode("utf-8", errors="replace")

    reader = csv.reader(text.splitlines())
    header = next(reader)
    fmt["header_ok"] = header == EXPECTED_HEADER
    fmt["header"] = header

    rows = []
    bad_cols = bad_vol = bad_hour = bad_pickup = negative = 0
    seen_key = Counter()
    pickup_by_bill: dict[str, set[str]] = defaultdict(set)
    for lineno, cells in enumerate(reader, start=2):
        if not cells:
            continue
        if len(cells) != 6:
            bad_cols += 1
            continue
        b, c, w, hour, pickup, vol = [x.strip() for x in cells]
        if not VOL_RE.match(vol):
            bad_vol += 1
        if not H6.match(hour):
            bad_hour += 1
        if not H6S.match(pickup):
            bad_pickup += 1
        if vol.startswith("-"):
            negative += 1
        seen_key[(b, c, w, hour)] += 1
        pickup_by_bill[b].add(pickup)
        rows.append({
            "line": lineno, "bill": b, "c": c, "w": w, "hour": hour,
            "pickup": pickup, "vol_str": vol,
            "vol_int": to_int(vol), "vol_m3": Decimal(vol),
        })

    fmt.update({
        "row_count": len(rows), "bad_cols": bad_cols, "bad_vol": bad_vol,
        "bad_hour_format": bad_hour, "bad_pickup_format": bad_pickup,
        "negative": negative,
        "hour_not_in_wide": sum(1 for r in rows if r["hour"] not in raw["left_set"]),
        "dup_quad": sum(1 for k, n in seen_key.items() if n > 1),
        "bills_multi_pickup": sum(1 for b, s in pickup_by_bill.items() if len(s) > 1),
    })
    return {"fmt": fmt, "rows": rows, "seen_key": seen_key}


# ---------------------------------------------------------------------------
# 3) C1—C5 独立复核
# ---------------------------------------------------------------------------
def check_constraints(rows: list[dict], raw: dict) -> dict:
    c1_missing = c1_over = c1_tol = 0
    c2 = c3 = c4 = c5 = 0
    c2_missing = c4_missing = c5_missing = 0
    c5_before_available = 0
    c5_available_after_latest = 0
    per_cell: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    per_bill: dict[str, int] = defaultdict(int)
    wh_by_bill: dict[str, set[str]] = defaultdict(set)
    per_bill_excess: list[tuple[str, int]] = []

    cell_diff_max = Decimal(0)
    cell_over_max = Decimal(0)
    # 若误把容差按「每条记录」判定会产生多少假冲突（用于佐证判定对象）
    per_record_false_conflict = sum(1 for r in rows if r["vol_m3"] > Decimal("0.0005"))

    pickup_after_departure = 0
    abnormal_records_after_required = 0

    for r in rows:
        key = (r["c"], r["w"], r["hour"])
        original = raw["cell_m3"].get(key)
        per_cell[key] += r["vol_m3"]
        if original is None:
            c1_missing += 1
        bill = raw["bills"].get(r["bill"])
        per_bill[r["bill"]] += r["vol_int"]
        wh_by_bill[r["bill"]].add(r["w"])

        # C4
        cargo_lv = raw["level_by_cw"].get((r["c"], r["w"]))
        if cargo_lv is None or bill is None:
            c4_missing += 1
        elif cargo_lv > bill["level"]:
            c4 += 1

        # C5
        if bill is None:
            c5_missing += 1
        else:
            pk = parse_time(r["pickup"])
            available_at = parse_hour(r["hour"]) + timedelta(hours=1)
            if not (bill["earliest"] <= pk <= bill["latest"]):
                c5 += 1
            if pk < available_at:
                c5_before_available += 1
            if available_at > bill["latest"]:
                c5_available_after_latest += 1
            if pk > bill["departure"]:
                pickup_after_departure += 1
            if bill["abnormal"] and pk > bill["required"]:
                abnormal_records_after_required += 1

    # C1 逐格：四位小数求和 vs 原值（容差判定对象是「逐格聚合」不是每条记录）
    cells_checked = 0
    cells_fully_allocated = 0
    for key, summed in per_cell.items():
        original = raw["cell_m3"].get(key, Decimal(0))
        diff = abs(summed - original)
        cell_diff_max = max(cell_diff_max, diff)
        cells_checked += 1
        if summed > original:
            cell_over_max = max(cell_over_max, summed - original)
            c1_over += 1
        if diff > Decimal("0.0005"):
            c1_tol += 1
        if summed == original:
            cells_fully_allocated += 1

    # 非零格是否都有去向
    nonzero_with_records = sum(1 for k in raw["cell_m3"] if k in per_cell)
    nonzero_no_record = len(raw["cell_m3"]) - nonzero_with_records

    # C2 / C3
    for b, got in per_bill.items():
        bill = raw["bills"].get(b)
        if bill is None:
            c2_missing += 1
            continue
        if got > bill["plan"]:
            c2 += 1
            per_bill_excess.append((b, got - bill["plan"]))
    for b, ws in wh_by_bill.items():
        if len(ws) > 1:
            c3 += 1

    return {
        "C1": {"missing_cell": c1_missing, "over_upper": c1_over, "over_tolerance": c1_tol,
               "cells_checked": cells_checked, "cells_fully_allocated": cells_fully_allocated,
               "nonzero_cells": len(raw["cell_m3"]), "nonzero_no_record": nonzero_no_record,
               "max_abs_diff_m3": str(cell_diff_max), "max_over_m3": str(cell_over_max),
               "tolerance_object": "逐格四位小数求和 vs 该格 480 小时列原值（非每条记录）",
               "per_record_false_conflict_if_misapplied": per_record_false_conflict},
        "C2": {"conflicts": c2, "bill_not_in_raw": c2_missing, "max_excess_int": max((e for _, e in per_bill_excess), default=0)},
        "C3": {"conflicts": c3},
        "C4": {"conflicts": c4, "lookup_missing": c4_missing,
               "channels_with_multi_levels": sum(1 for _, s in raw["level_by_channel_only"].items() if len(s) > 1)},
        "C5": {"conflicts": c5 + c5_before_available + c5_available_after_latest,
               "out_of_window": c5, "lookup_missing": c5_missing,
               "pickup_before_available": c5_before_available,
               "available_after_latest": c5_available_after_latest,
               "pickup_after_planned_departure": pickup_after_departure,
               "abnormal_records_after_required": abnormal_records_after_required},
        "conflict_total": (c1_missing + c1_over + c1_tol + c2 + c2_missing + c3
                           + c4 + c4_missing + c5 + c5_before_available
                           + c5_available_after_latest + c5_missing),
    }


# ---------------------------------------------------------------------------
# 4) 按 06 文档公式独立复算七项指标
# ---------------------------------------------------------------------------
def recompute_metrics(rows: list[dict], raw: dict) -> dict:
    set_a = [r for r in rows if EVAL_START <= parse_hour(r["hour"]) <= EVAL_END]
    sum_a = sum(r["vol_int"] for r in set_a)
    sum_a_m3 = Decimal(sum_a) / VOLUME_UNIT

    q_all: dict[str, int] = defaultdict(int)
    for r in rows:
        q_all[r["bill"]] += r["vol_int"]

    bills = raw["bills"]
    plan_sum = raw["plan_sum"]
    filled = 0
    virtual = 0
    fully_virtual = 0
    for b, wb in bills.items():
        got = q_all.get(b, 0)
        filled += min(got, wb["plan"])
        if got < wb["plan"] - VIRTUAL_GAP_INT:
            virtual += 1
            if got == 0:
                fully_virtual += 1
    fill_rate = filled / plan_sum if plan_sum else 0.0

    exact = 0
    primary = 0
    backlog_weighted = 0.0
    over24 = 0
    cost_total = 0.0
    density_missing = 0
    primary_by_dest: dict[str, int] = defaultdict(int)

    for r in set_a:
        wb = bills.get(r["bill"])
        if wb is None:
            continue
        cargo_lv = raw["level_by_cw"].get((r["c"], r["w"]), 0)
        if cargo_lv == wb["level"]:
            exact += r["vol_int"]
        inj = raw["injection_by_cw"].get((r["c"], r["w"]), "")
        is_primary = (wb["dest"] != SHJ_PORT and inj == wb["dest"])
        if is_primary:
            primary += r["vol_int"]
            primary_by_dest[wb["dest"]] += 1

        available_at = parse_hour(r["hour"]) + timedelta(hours=1)
        wait_h = (parse_time(r["pickup"]) - available_at).total_seconds() / 3600.0
        over = max(0.0, wait_h - FREE_BACKLOG_HOURS)
        backlog_weighted += over * r["vol_int"]
        if over > 0:
            over24 += r["vol_int"]

        if wb["billing"] != CHARTER:
            pack_date = parse_hour(r["hour"]).strftime("%Y-%m-%d")
            dens = raw["density"].get((r["c"], r["w"], pack_date))
            if dens is None:
                density_missing += 1
            else:
                rate = raw["cost"].get(wb["dest"], 0.0)
                cost_total += (r["vol_int"] / VOLUME_UNIT) * dens * rate

    backlog_rate = backlog_weighted / (sum_a * EVAL_HOURS) if sum_a else 0.0
    unallocated_m3 = raw["eval_supply"] - sum_a_m3

    return {
        "values": {
            "提单配满率": round(fill_rate, 6),
            "敏感品属性完全匹配率": round(exact / sum_a, 6) if sum_a else 0.0,
            "时效积压率": round(backlog_rate, 6),
            "主注入点选择率": round(primary / sum_a, 6) if sum_a else 0.0,
            "成本": round(cost_total, 2),
            "未配提单货量": f"{unallocated_m3:.4f}",
            "虚仓提单数量": str(virtual),
        },
        "aux": {
            "A记录数": len(set_a), "全量记录数": len(rows),
            "A方数_m3": str(sum_a_m3), "评估范围产出_m3": str(raw["eval_supply"]),
            "计划体积合计_m3": str(Decimal(plan_sum) / VOLUME_UNIT),
            "全量货源_m3": str(raw["total_supply"]),
            "完全虚仓张数": fully_virtual, "密度缺失笔数": density_missing,
            "超24h方数_m3": str(Decimal(over24) / VOLUME_UNIT),
            "主注入点命中的目的港数": len(primary_by_dest),
        },
    }


# ---------------------------------------------------------------------------
# 5) 隐含风险抽查
# ---------------------------------------------------------------------------
def spot_checks(rows: list[dict], raw: dict, metrics: dict) -> dict:
    out_of_range = [r for r in rows if not (EVAL_START <= parse_hour(r["hour"]) <= EVAL_END)]
    out_m3 = sum(r["vol_m3"] for r in out_of_range)

    shj_bills = {b for b, wb in raw["bills"].items() if wb["dest"] == SHJ_PORT}
    shj_records = [r for r in rows if r["bill"] in shj_bills]
    shj_injections = {raw["injection_by_cw"].get((r["c"], r["w"]), "") for r in shj_records}
    shj_bills_with_records = {r["bill"] for r in shj_records}

    abnormal_bills = {b for b, wb in raw["bills"].items() if wb["abnormal"]}
    abnormal_records = [r for r in rows if r["bill"] in abnormal_bills]
    departure_early = sum(1 for wb in raw["bills"].values() if wb["departure"] < wb["earliest"])

    # 计划起飞早于提货时刻但仍被输出（证明计划起飞不参与过滤）
    dep_violating = sum(
        1 for r in rows
        if r["bill"] in raw["bills"] and parse_time(r["pickup"]) > raw["bills"][r["bill"]]["departure"]
    )

    return {
        "计划体积合计_m3": str(Decimal(raw["plan_sum"]) / VOLUME_UNIT),
        "全量货源_m3": str(raw["total_supply"]),
        "计划体积_大于_全量货源": raw["plan_sum"] > to_int(raw["total_supply"]),
        "未配提单货量": metrics["values"]["未配提单货量"],
        "虚仓提单数量": metrics["values"]["虚仓提单数量"],
        "评估范围产出_等于_评估范围配出": raw["eval_supply"] == Decimal(metrics["aux"]["A方数_m3"]),
        "范围外记录数": len(out_of_range),
        "范围外配出方数_m3": str(out_m3),
        "A记录数": metrics["aux"]["A记录数"],
        "SHJ提单数": len(shj_bills),
        "SHJ有配货记录的提单数": len(shj_bills_with_records),
        "SHJ配货覆盖的渠道注入点数": len(shj_injections),
        "SHJ注入点样例": sorted(shj_injections)[:12],
        "异常提单数": len(abnormal_bills),
        "异常提单配货记录数": len(abnormal_records),
        "异常提单中提货时刻超过需求到站的记录数": sum(
            1 for r in abnormal_records
            if parse_time(r["pickup"]) > raw["bills"][r["bill"]]["required"]
        ),
        "计划起飞早于最早提货的提单数": departure_early,
        "提货时刻晚于计划起飞的记录数": dep_violating,
        "按渠道ID存在多等级的行组数": sum(
            1 for _, s in raw["level_by_channel_only"].items() if len(s) > 1
        ),
    }


# ---------------------------------------------------------------------------
# 6) 与 indicators.csv 比对
# ---------------------------------------------------------------------------
def compare_indicators(path: Path, values: dict) -> dict:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    reported = {r["指标"]: r for r in rows}
    checks = []
    ok = True
    for name, mine in values.items():
        r = reported.get(name)
        if r is None:
            checks.append({"指标": name, "状态": "缺失", "CSV值": "", "复算值": str(mine)})
            ok = False
            continue
        got = r["数值"].strip()
        note_empty = not r["口径说明"].strip()
        if isinstance(mine, float):
            same = abs(float(got) - mine) <= 5e-7
        elif isinstance(mine, str) and re.match(r"^\d+\.\d{4}$", mine):
            same = abs(Decimal(got) - Decimal(mine)) <= Decimal("0.00005")
        else:
            same = got == str(mine)
        if note_empty or not same:
            ok = False
        checks.append({
            "指标": name, "状态": "一致" if (same and not note_empty) else "不一致",
            "CSV值": got, "复算值": str(mine), "口径说明非空": not note_empty,
        })
    ind_fmt = {
        "数据行数": len(rows),
        "表头三列": list(rows[0].keys()) if rows else [],
        "指标名齐全": set(values) == set(reported),
    }
    return {"all_match": ok and ind_fmt["指标名齐全"], "indicator_format": ind_fmt, "checks": checks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--allocation", default=str(RESULTS / "allocation.csv"))
    ap.add_argument("--indicators", default=str(RESULTS / "indicators.csv"))
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    print("=" * 78)
    print("QA 独立复核 · 不 import 任何 src 模块，重新只读加载原始 xlsx")
    print("=" * 78)
    print("data_dir =", data_dir)

    try:
        raw = load_raw(data_dir)
    except Exception as exc:  # noqa: BLE001
        print("原始表加载失败：", exc)
        return 2

    print("\n[原始表重建]")
    print("  源文件 SHA-256:", json.dumps(raw["src_hashes"], ensure_ascii=False))
    print(f"  宽表行数={raw['cw_rows']} 小时列=480 非零格={raw['nonzero_cells']} "
          f"(渠道×子仓)重复键={raw['dup_cw']}")
    print(f"  全量货源(480小时列和)={raw['total_supply']} m3  "
          f"评估范围产出(360小时)={raw['eval_supply']} m3")
    print(f"  提单数={len(raw['bills'])} 重复提单号={raw['dup_bills']} "
          f"计划体积合计={Decimal(raw['plan_sum'])/VOLUME_UNIT} m3")
    print(f"  异常提单={raw['abnormal']} SHJ提单={raw['shj']} 包机提单={raw['charter']} "
          f"密度为空行={raw['density_empty']} 目的港成本={len(raw['cost'])}")
    print(f"  同一渠道ID存在多个敏感等级的行组数="
          f"{sum(1 for _, s in raw['level_by_channel_only'].items() if len(s) > 1)}"
          " （>0 说明必须按「渠道×子仓」取值）")

    alloc = read_allocation(Path(args.allocation), raw)
    print("\n[allocation.csv 字节与格式]")
    for k, v in alloc["fmt"].items():
        print(f"  {k} = {v}")

    cons = check_constraints(alloc["rows"], raw)
    print("\n[C1—C5 独立复核]")
    print(json.dumps(cons, ensure_ascii=False, indent=2))

    met = recompute_metrics(alloc["rows"], raw)
    print("\n[七项指标独立复算]")
    print(json.dumps(met, ensure_ascii=False, indent=2))

    cmp_ = compare_indicators(Path(args.indicators), met["values"])
    print("\n[与 indicators.csv 比对]")
    print(json.dumps(cmp_, ensure_ascii=False, indent=2))

    spot = spot_checks(alloc["rows"], raw, met)
    print("\n[隐含风险抽查]")
    print(json.dumps(spot, ensure_ascii=False, indent=2))

    fmt = alloc["fmt"]
    format_ok = (
        fmt["utf8"] and not fmt["bom"] and not fmt["crlf"] and not fmt["lone_cr"]
        and fmt["header_ok"] and fmt["bad_cols"] == 0 and fmt["bad_vol"] == 0
        and fmt["bad_hour_format"] == 0 and fmt["bad_pickup_format"] == 0
        and fmt["negative"] == 0 and fmt["hour_not_in_wide"] == 0
        and fmt["dup_quad"] == 0 and fmt["bills_multi_pickup"] == 0
    )
    passed = (cons["conflict_total"] == 0 and format_ok and cmp_["all_match"])
    print("\n[结论]")
    print(f"  C1—C5 冲突总数 = {cons['conflict_total']}")
    print(f"  CSV 格式全通过 = {format_ok}")
    print(f"  七项指标复算一致 = {cmp_['all_match']}")
    print(f"  独立复核总判定 = {'通过' if passed else '失败'}")

    if args.json_out:
        payload = {
            "source_hashes": raw["src_hashes"], "format": fmt,
            "constraints": cons, "metrics": met, "indicator_compare": cmp_,
            "spot_checks": spot, "format_ok": format_ok, "all_pass": passed,
        }
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  结构化结果已写入", args.json_out)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
