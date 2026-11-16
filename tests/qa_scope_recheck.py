#!/usr/bin/env python3
"""QA 补充复核：配满率 / 虚仓提单数量的统计口径（主理人追加指令）。

独立验证四件事：
  ① docs/06_指标口径.md、results/indicators.csv 口径说明、src/metrics.py
     的 _formula_notes / _compute 三者是否一致；
  ② 评估范围口径的两个辅助值 0.915633 / 327 张是否如实给出且可独立复算
     （按 03 第九节 B1/B2 原式 Q_R(b)/B_R，不调用 metrics.py）；
  ③ 有没有把这项「自行假设」写成文档已明确要求；
  ④ 05 B1/B2 的归因与 06 第一节的自相矛盾措辞是否已修正，以及
     源码/报告里「七项指标只统计 A」旧文案的当前状态（只读，不修改）。

用法（项目根执行）：
    python tests/qa_scope_recheck.py
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.qa_independent_verify import (  # noqa: E402
    EVAL_START, EVAL_END, VOLUME_UNIT, VIRTUAL_GAP_INT, parse_hour,
    load_raw, read_allocation, DEFAULT_DATA_DIR,
)


def aux_from_csv(rows: list[dict], raw: dict) -> dict:
    """按 03 B1/B2 原式独立复算评估范围口径辅助值，不 import src.metrics。"""
    q_eval: dict[str, int] = defaultdict(int)
    for r in rows:
        if EVAL_START <= parse_hour(r["hour"]) <= EVAL_END:
            q_eval[r["bill"]] += r["vol_int"]
    bills = raw["bills"]
    plan_eval_sum = sum(bills[b]["plan"] for b in q_eval if b in bills)
    filled_eval_sum = sum(
        min(q, bills[b]["plan"]) for b, q in q_eval.items() if b in bills
    )
    virtual_eval = sum(
        1 for b, q in q_eval.items()
        if b in bills and q < bills[b]["plan"] - VIRTUAL_GAP_INT
    )
    return {
        "b_r_size": len(q_eval),
        "plan_eval_sum_m3": f"{Decimal(plan_eval_sum) / VOLUME_UNIT:.4f}",
        "fill_rate_eval_scope": filled_eval_sum / plan_eval_sum,
        "virtual_eval_scope": virtual_eval,
    }


def main() -> int:
    print("=" * 78)
    print("QA 补充复核 · 配满率 / 虚仓提单数量统计口径")
    print("=" * 78)

    raw = load_raw(DEFAULT_DATA_DIR)
    alloc = read_allocation(ROOT / "results" / "allocation.csv", raw)
    aux = aux_from_csv(alloc["rows"], raw)
    report = json.loads((ROOT / "results" / "report_data.json").read_text(encoding="utf-8"))
    rep_aux = report["辅助量"]

    print("\n[②] 评估范围口径辅助值：独立复算 vs report_data.json")
    print(f"  独立复算 B_R 提单数 = {aux['b_r_size']}  | report_data.b_r_size = {rep_aux['b_r_size']}"
          f"  | 一致 = {aux['b_r_size'] == rep_aux['b_r_size']}")
    print(f"  独立复算 B_R 计划体积合计 = {aux['plan_eval_sum_m3']} m3")
    print(f"  配满率(评估范围口径) 复算 = {aux['fill_rate_eval_scope']:.15f}"
          f"  | report_data = {rep_aux['fill_rate_eval_scope']:.15f}")
    f_ok = round(aux["fill_rate_eval_scope"], 6) == round(rep_aux["fill_rate_eval_scope"], 6)
    v_ok = aux["virtual_eval_scope"] == rep_aux["virtual_eval_scope"]
    print(f"  虚仓(评估范围口径) 复算 = {aux['virtual_eval_scope']} 张"
          f"  | report_data = {rep_aux['virtual_eval_scope']} 张")
    print(f"  配满率一致 = {f_ok}   虚仓一致 = {v_ok}")

    doc06 = (ROOT / "docs" / "06_指标口径.md").read_text(encoding="utf-8")
    doc06_has = ("0.915633" in doc06) and ("327" in doc06)
    print(f"  06 文档写明 0.915633 / 327 = {doc06_has}")
    print(f"  06 文档第 39-41 行: {doc06.splitlines()[38].strip()[:100]}")
    print(f"  06 文档第 117-118 行: {doc06.splitlines()[116].strip()[:100]}")

    # ① 三者一致性：indicators.csv 口径说明 vs metrics.py._formula_notes 逐字
    print("\n[①] 06 文档 / indicators.csv 口径说明 / metrics.py 三者一致性")
    from src.config import Config
    from src.metrics import _formula_notes
    notes = _formula_notes(Config.load(), {})
    ind = {r["指标"]: r["口径说明"] for r in csv.DictReader(
        (ROOT / "results" / "indicators.csv").open(encoding="utf-8"))}
    word_eq = {k: (ind.get(k) == v) for k, v in notes.items()}
    print(f"  indicators.csv 口径说明 == metrics.py._formula_notes 逐字一致: "
          f"{sum(word_eq.values())}/{len(word_eq)}  {word_eq}")
    key_checks = {
        "06: 配满率公式用全部提单 B": "Σ_{b∈B}" in doc06 or "b\\in B" in doc06,
        "06: 配满率分母 36031.55": "36031.55" in doc06,
        "06: 配满率明确不受 A 限制": "不受 A 限制" in doc06,
        "06: 虚仓公式 Q(b) < p_b − 0.0001": "Q(b) < p_b" in doc06,
        "06: 体积类 5 项严格只取 A": "严格只取 A" in doc06,
        "06: 第一节不再说体积类分子/分母统一取 A": "体积类分子 / 分母严格只取 A" not in doc06,
        "metrics.py: 配满率/虚仓用提单整体 Q(b)": "按提单整体配载结果判定" in
            (ROOT / "src" / "metrics.py").read_text(encoding="utf-8"),
        "indicators: 配满率口径说明为提单侧": "提单侧口径" in ind.get("提单配满率", ""),
        "indicators: 虚仓口径说明为整体配载": "整体配载状态判定" in ind.get("虚仓提单数量", ""),
    }
    for k, v in key_checks.items():
        print(f"  {k}: {v}")

    # ③ 是否被写成「文档已明确要求」
    print("\n[③] 该项自行假设是否被写成文档已明确要求")
    doc05 = (ROOT / "docs" / "05_假设清单.md").read_text(encoding="utf-8")
    doc13 = (ROOT / "docs" / "13_未完成项.md").read_text(encoding="utf-8")
    doc03 = (ROOT / "docs" / "03_系统设计.md").read_text(encoding="utf-8")
    checks3 = {
        "05 开头声明全部自行假设": "下面每一条都是自行假设，非文档要求" in doc05,
        "05 B1 标注自行假设": "### B1" in doc05 and doc05.split("### B1")[1].split("### B2")[0].count("自行假设，非文档要求") >= 1,
        "05 B2 标注自行假设": "### B2" in doc05 and doc05.split("### B2")[1].split("### B3")[0].count("自行假设，非文档要求") >= 1,
        "06 开头声明量化全部自定义": "全部是本项目自定义" in doc06 and "自行假设，非文档要求" in doc06,
        "03 第九节声明全部自行假设": "以下全部是**自行假设**" in doc03,
        "13 明确记为实现阶段偏离": "实现阶段对设计原式的偏离" in doc13,
        "05 不再写「按主理人拍板保留」": "按主理人拍板保留" not in doc05,
        "05 归因如实（实现阶段偏离 + 交付复核确认接受）":
            "实现阶段对设计原式的偏离" in doc05 and "交付复核时确认接受" in doc05,
    }
    bad = re.compile(r"(文档|资料包|赛题|题目)(明确)?(规定|要求)[^。]{0,40}(配满率|虚仓)")
    files = ["docs/05_假设清单.md", "docs/06_指标口径.md", "docs/13_未完成项.md",
             "docs/10_方案报告.md", "docs/07_实验报告.md", "README.md"]
    hits = []
    for f in files:
        for i, line in enumerate((ROOT / f).read_text(encoding="utf-8").splitlines(), 1):
            if bad.search(line):
                hits.append(f"{f}:{i}: {line.strip()[:100]}")
    checks3["全文无『文档规定配满率/虚仓』式措辞"] = (len(hits) == 0)
    for k, v in checks3.items():
        print(f"  {k}: {v}")
    if hits:
        for h in hits:
            print("   可疑:", h)

    # ④ 修正状态：旧文案「七项指标只统计 A」
    print("\n[④] 「七项指标只统计 A」旧文案当前状态（只读盘点）")
    pat = re.compile(r"七项指标只统计|体积类分子 / 分母统一取自|七项指标中的体积类")
    for f in ["src/time_axis.py", "src/loader.py", "src/validate.py",
              "src/dashboard_data.py", "src/metrics.py", "results/dashboard_data.json",
              "results/data_issues.csv", "docs/03_系统设计.md"]:
        p = ROOT / f
        lines = [f"{i}: {l.strip()[:95]}" for i, l in enumerate(
            p.read_text(encoding="utf-8").splitlines(), 1) if pat.search(l)]
        print(f"  {f}: 命中 {len(lines)} 处")
        for l in lines[:3]:
            print("     ", l)

    all_ok = (f_ok and v_ok and doc06_has and all(word_eq.values())
              and all(key_checks.values()) and all(checks3.values()))
    print("\n[结论]")
    print(f"  ① 三者一致 = {all(word_eq.values()) and all(key_checks.values())}")
    print(f"  ② 辅助值如实且可独立复算 = {f_ok and v_ok and doc06_has}")
    print(f"  ③ 未写成文档已明确要求 = {all(checks3.values())}")
    print(f"  补充复核总判定 = {'通过' if all_ok else '失败'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
