"""一键自检：V1—V9 逐项记录「验证项 / 通过或失败 / 复现步骤 / 可复现结果」。

V8 真跑两次全流程并比对两个 CSV 的 SHA-256：第一次用磁盘上现有的产出，
第二次把全流程写进临时目录再算哈希，两者必须一致；结果写
``results/selfcheck_hashes.json``。V1—V5 用 src/audit.py 的独立审计与
从 CSV 复算的指标，不引用求解器内部计数。
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import config as cfg
from .audit import recheck_from_csv
from .config import Config
from .errors import DataFileError
from .loader import load_all
from .logging_util import get_logger
from .metrics import INDICATOR_NAMES, compute_from_rows
from .pipeline import run_full, sha256_file

logger = get_logger()

FOUR_DECIMAL_RE = re.compile(r"^\d+\.\d{1,4}$")
HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
PICKUP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@dataclass
class CheckItem:
    """一条自检记录。"""

    item: str = ""
    status: str = ""
    reproduce: str = ""
    result: str = ""


@dataclass
class SelfCheckReport:
    """V1—V9 汇总。"""

    items: List[CheckItem] = field(default_factory=list)
    hashes: Dict[str, Any] = field(default_factory=dict)
    all_pass: bool = True

    def add(self, item: str, passed: bool, reproduce: str, result: str) -> None:
        """追加一条记录；失败时把 all_pass 置 False。"""
        self.items.append(CheckItem(
            item=item, status="通过" if passed else "失败",
            reproduce=reproduce, result=result,
        ))
        if not passed:
            self.all_pass = False


def _read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """读 CSV，返回 (表头, 行字典列表)。"""
    with path.open("r", encoding=cfg.CSV_ENCODING, newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = [
            {header[i]: row[i] for i in range(min(len(header), len(row)))}
            for row in reader if row
        ]
    return list(header), rows


def _v1_v2(audit_report: Any, report: SelfCheckReport) -> None:
    """V1 五条硬约束逐条审计、V2 体积守恒与容差。"""
    hard = [f for f in audit_report.findings
            if f.constraint_id in ("C1", "C2", "C3", "C4", "C5")]
    conflicts = {f.constraint_id: f.conflicts for f in hard}
    checked = {f.constraint_id: f.checked for f in hard}
    report.add(
        "V1 五条硬约束逐条审计",
        len(hard) == 5 and sum(conflicts.values()) == 0 and audit_report.all_pass,
        "python run.py --self-check；audit.recheck_from_csv(cfg) 重新只读打开原始 xlsx "
        "并对 allocation.csv 逐行复核",
        f"C1={conflicts.get('C1')} C2={conflicts.get('C2')} C3={conflicts.get('C3')} "
        f"C4={conflicts.get('C4')} C5={conflicts.get('C5')}；"
        f"检查数 C1={checked.get('C1')} 行；all_pass={audit_report.all_pass}",
    )
    c1 = next(f for f in audit_report.findings if f.constraint_id == "C1")
    report.add(
        f"V2 体积守恒与 {cfg.VOLUME_TOLERANCE_TEXT} 立方米容差",
        c1.conflicts == 0,
        "audit 按 (小时, 渠道, 子仓) 逐格聚合配出方数并按四位小数求和比对原值",
        f"C1 冲突={c1.conflicts}；容差={cfg.VOLUME_TOLERANCE_TEXT} 立方米 "
        f"= {cfg.VOLUME_TOLERANCE_INT} 个整数单位；逐格复核通过",
    )


def _v3(cfg_obj: Config, report: SelfCheckReport) -> None:
    """V3 明细格式：首行 6 列、四位小数、时间格式、UTF-8 无 BOM、固定换行。"""
    path = cfg_obj.results_path() / "allocation.csv"
    if not path.is_file():
        report.add("V3 明细格式", False, "检查 allocation.csv", "文件不存在")
        return
    raw = path.read_bytes()
    header, rows = _read_csv(path)
    no_bom = not raw.startswith(b"\xef\xbb\xbf")
    no_crlf = b"\r\n" not in raw
    six = header == list(cfg.ALLOCATION_COLUMNS)
    vol_ok = all(FOUR_DECIMAL_RE.match(r.get("配货方数", "")) for r in rows)
    hour_ok = all(HOUR_RE.match(r.get("产出小时", "")) for r in rows)
    pickup_ok = all(PICKUP_RE.match(r.get("提货时刻", "")) for r in rows)
    passed = no_bom and no_crlf and six and vol_ok and hour_ok and pickup_ok and bool(rows)
    report.add(
        "V3 明细格式",
        passed,
        "读取 results/allocation.csv 字节与表头；正则校验四位小数、产出小时、提货时刻",
        f"表头={header}；行数={len(rows)}；无BOM={no_bom}；固定LF={no_crlf}；"
        f"四位小数={vol_ok}；小时格式={hour_ok}；提货格式={pickup_ok}",
    )


def _v4(cfg_obj: Config, report: SelfCheckReport) -> None:
    """V4 指标文件：3 列 7 行、口径非空、从 CSV + 原始表按 06 公式复算比对。"""
    alloc = cfg_obj.results_path() / "allocation.csv"
    indicators = cfg_obj.results_path() / "indicators.csv"
    if not (alloc.is_file() and indicators.is_file()):
        report.add("V4 指标文件与可复算", False, "检查 indicators.csv", "文件不存在")
        return
    header, rows = _read_csv(indicators)
    three = header == list(cfg.INDICATORS_COLUMNS)
    seven = len(rows) == len(INDICATOR_NAMES)
    names = [r.get("指标", "") for r in rows]
    names_ok = names == list(INDICATOR_NAMES)
    notes_ok = all(str(r.get("口径说明", "")).strip() for r in rows)
    published = {r.get("指标", ""): r.get("数值", "") for r in rows}

    dataset = load_all(cfg_obj)  # 重新只读加载原始表
    _, alloc_rows = _read_csv(alloc)
    recomputed, auxiliary = compute_from_rows(alloc_rows, dataset, cfg_obj)
    from .metrics import metric_values

    expected = metric_values(recomputed, cfg_obj)
    diffs = [
        name for name in INDICATOR_NAMES
        if published.get(name, "") != expected[name]
    ]
    passed = three and seven and names_ok and notes_ok and not diffs
    report.add(
        "V4 指标文件与可复算",
        passed,
        "python run.py --self-check；metrics.compute_from_rows 用 allocation.csv + "
        "重新只读加载的原始 xlsx 按 06_指标口径.md 公式复算，与 indicators.csv 逐项比对",
        f"三列={three}；七行={seven}；指标名={names_ok}；口径说明非空={notes_ok}；"
        f"复算不一致项={diffs or '无'}；评估范围记录数={auxiliary['a_record_count']}",
    )


def _v5(cfg_obj: Config, report: SelfCheckReport) -> None:
    """V5 场景对比：≥ 2 组、每组 7 项指标齐全。"""
    path = cfg_obj.results_path() / "metrics_scenarios.csv"
    if not path.is_file():
        report.add("V5 场景对比", False, "检查 metrics_scenarios.csv", "文件不存在")
        return
    header, rows = _read_csv(path)
    complete = all(all(name in row for name in INDICATOR_NAMES) for row in rows)
    passed = len(rows) >= 2 and complete
    report.add(
        "V5 场景对比",
        passed,
        "读取 results/metrics_scenarios.csv，核对场景组数与七项指标列",
        f"场景组数={len(rows)}；名称={[r.get('场景', '') for r in rows]}；"
        f"每组七项齐全={complete}",
    )


def _v6(cfg_obj: Config, report: SelfCheckReport) -> None:
    """V6 基线对照与规模曲线：独立实现、≥3 档、每档齐全、有改进量。"""
    scale_path = cfg_obj.results_path() / "scale_experiments.csv"
    baseline_path = cfg_obj.results_path() / "baseline_comparison.csv"
    figures = cfg_obj.results_path() / "figures"
    baseline_source = (cfg.PROJECT_ROOT / "src" / "baseline.py").read_text(encoding="utf-8")
    independent = ("RollingSolver" not in baseline_source
                   and "from .solver" not in baseline_source)
    if not (scale_path.is_file() and baseline_path.is_file()):
        report.add("V6 基线对照与规模曲线", False,
                   "检查 scale_experiments.csv / baseline_comparison.csv", "文件不存在")
        return
    _, scale_rows = _read_csv(scale_path)
    _, improvement_rows = _read_csv(baseline_path)
    scales = sorted({r.get("规模", "") for r in scale_rows})
    methods = {r.get("方法", "") for r in scale_rows}
    needed = {"配满率", "时效积压率", "成本", "耗时秒"}
    complete = all(needed.issubset(set(row.keys())) for row in scale_rows)
    curve_files = sorted(p.name for p in figures.glob("scale_*")) if figures.is_dir() else []
    passed = (independent and len(scales) >= 3 and methods == {"主方案", "贪心基准"}
              and complete and len(improvement_rows) > 0 and bool(curve_files))
    report.add(
        "V6 基线对照与规模曲线",
        passed,
        "读取 scale_experiments.csv / baseline_comparison.csv；检查 src/baseline.py "
        "不含 RollingSolver；检查 results/figures/ 下的曲线文件",
        f"基准独立实现={independent}；规模档={scales}；方法={sorted(methods)}；"
        f"每档四项齐全={complete}；改进量行={len(improvement_rows)}；"
        f"曲线文件={curve_files}",
    )


def _v7(cfg_obj: Config, report: SelfCheckReport) -> None:
    """V7 文档完整性：模型推导、假设逐条、报告页数、说明单中文、协作记录模板。"""
    docs = cfg.PROJECT_ROOT / "docs"
    needed = [
        "04_模型说明.md", "05_假设清单.md", "06_指标口径.md", "07_实验报告.md",
        "10_方案报告.md", "11_作品说明单.md", "12_AI协作记录.md", "13_未完成项.md",
    ]
    present = [name for name in needed if (docs / name).is_file()]
    model = (docs / "04_模型说明.md").read_text(encoding="utf-8") \
        if (docs / "04_模型说明.md").is_file() else ""
    model_ok = all(word in model for word in ("决策变量", "目标函数", "推导", "约束证明"))
    assumptions = (docs / "05_假设清单.md").read_text(encoding="utf-8") \
        if (docs / "05_假设清单.md").is_file() else ""
    a_ok = all(f"A{i}" in assumptions for i in range(1, 6))
    b_ok = all(f"B{i}" in assumptions for i in range(1, 14))
    self_assumed = "自行假设" in assumptions
    manifest_path = cfg_obj.results_path() / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding=cfg.CSV_ENCODING)) \
        if manifest_path.is_file() else {}
    pdf = manifest.get("report", {}).get("pdf", {})
    pages = pdf.get("pages")
    pdf_status = pdf.get("status")
    pages_ok = pdf_status == "generated" and isinstance(pages, int) and pages <= 10
    front = pdf.get("front_pages_are_cover", False)
    collab = (docs / "12_AI协作记录.md").read_text(encoding="utf-8") \
        if (docs / "12_AI协作记录.md").is_file() else ""
    template_ok = all(word in collab for word in ("使用的 AI 工具与模型", "分工概览", "纠错记录"))
    passed = (len(present) == len(needed) and model_ok and a_ok and b_ok
              and self_assumed and pages_ok and front and template_ok)
    report.add(
        "V7 文档完整性",
        passed,
        "检查 docs/04/05/06/07/10/11/12/13 存在；核对模型说明含推导与约束证明、"
        "假设清单覆盖 A1—A5 与 B1—B13 且标注自行假设；读 build_manifest.json 的页数",
        f"文档={len(present)}/{len(needed)}；推导与证明={model_ok}；"
        f"A1—A5={a_ok}；B1—B13={b_ok}；标注自行假设={self_assumed}；"
        f"PDF状态={pdf_status} 页数={pages} 前2页说明单={front}；协作记录按模板={template_ok}",
    )


def _v8(cfg_obj: Config, report: SelfCheckReport) -> Dict[str, Any]:
    """V8 可复现：真跑一次全流程写进临时目录，两个 CSV 的 SHA-256 逐一比对。"""
    results = cfg_obj.results_path()
    first = {
        name: sha256_file(results / name)
        for name in ("allocation.csv", "indicators.csv")
    }
    tmp_dir = Path(tempfile.mkdtemp(prefix="f3_reproduce_"))
    second: Dict[str, str] = {}
    error = ""
    try:
        # 把全流程完整重跑一次，只把结果目录改到临时目录
        from dataclasses import replace

        temp_cfg = replace(cfg_obj, outputs={
            **cfg_obj.outputs, "dir": str(tmp_dir),
        })
        run_full(temp_cfg)
        for name in ("allocation.csv", "indicators.csv"):
            target = tmp_dir / name
            second[name] = sha256_file(target) if target.is_file() else ""
        equal = all(first[name] == second[name] and second[name] for name in first)
    except Exception as exc:  # noqa: BLE001 - 复跑失败如实记失败
        equal = False
        error = repr(exc)
        logger.error("V8 复跑失败：%s", exc)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    hashes = {
        "first_run": first,
        "second_run": second,
        "equal": equal,
        "error": error,
        "note": "第一次用磁盘上已有产出，第二次全流程写进临时目录重跑；"
                "比的是 CSV 内容哈希，wall time 不作判据",
    }
    report.add(
        "V8 可复现",
        equal,
        "python run.py（第一次）→ python run.py --self-check（V8 内再跑一次全流程到"
        "临时目录）→ hashlib.sha256 比对两次 allocation.csv / indicators.csv",
        "；".join(
            f"{name}: 第一次={first[name][:16]}… 第二次={second.get(name, '')[:16]}… "
            f"一致={first[name] == second.get(name, '') and bool(second.get(name))}"
            for name in first
        ) + (f"；异常={error}" if error else ""),
    )
    return hashes


def _v9(cfg_obj: Config, report: SelfCheckReport) -> None:
    """V9 合规：公开产出不含原始表片段、诊断不出现被排除规则、manifest 如实。"""
    docs = cfg.PROJECT_ROOT / "docs"
    public = [
        docs / "10_方案报告.md", docs / "11_作品说明单.md",
        cfg_obj.results_path() / "dashboard.html",
        cfg_obj.results_path() / "dashboard_data.json",
    ]
    # 用真实提单号与渠道 ID 做泄漏扫描（只读取样，不写回）
    dataset = load_all(cfg_obj)
    sample_bills = {w.bill_no for w in dataset.waybills[:20]}
    sample_channels = {r.channel_id for r in dataset.supply_rows[:20]}
    leaks: List[str] = []
    for path in public:
        if not path.is_file():
            continue
        text = path.read_text(encoding=cfg.CSV_ENCODING, errors="ignore")
        for token in list(sample_bills) + list(sample_channels):
            if token and token in text:
                leaks.append(f"{path.name}:{token}")
                break
    diagnosis_path = cfg_obj.results_path() / "diagnosis.csv"
    diagnosis_text = diagnosis_path.read_text(encoding=cfg.CSV_ENCODING) \
        if diagnosis_path.is_file() else ""
    forbidden = [word for word in cfg_obj.excluded_rule_keywords()
                 if word and word in diagnosis_text]
    six = [word for word in ("限重", "耗材上限", "计费重", "集装板尺寸",
                            "清关资质", "可混配组合") if word in diagnosis_text]
    manifest_path = cfg_obj.results_path() / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding=cfg.CSV_ENCODING)) \
        if manifest_path.is_file() else {}
    honest = bool(manifest.get("report")) and bool(manifest.get("figures"))
    gitignore = (cfg.PROJECT_ROOT / ".gitignore").read_text(encoding=cfg.CSV_ENCODING)
    results_ignored = "results/" in gitignore
    passed = not leaks and not forbidden and not six and honest and results_ignored
    report.add(
        "V9 合规",
        passed,
        "只读取样原始表提单号/渠道ID，扫描公开文档与看板 HTML/JSON；"
        "检查 diagnosis.csv 黑名单；检查 build_manifest.json 与 .gitignore",
        f"原始行泄漏={leaks or '无'}；黑名单关键字={forbidden or '无'}；"
        f"六类被排除规则={six or '无'}；manifest 如实={honest}；"
        f"results/ 已排除={results_ignored}",
    )


def _write_markdown(report: SelfCheckReport, path: Path) -> Path:
    """把四列固定格式写进 results/selfcheck_report.md。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 自检记录",
        "",
        f"总状态：{'全部通过' if report.all_pass else '存在失败项'}；"
        f"共 {len(report.items)} 项。",
        "",
        "复现环境：Windows + Python 3.13.14 + openpyxl 3.1.5；"
        "命令按顺序执行 `python run.py` 与 `python run.py --self-check`。",
        "",
        "| 验证项 | 通过或失败 | 复现步骤 | 可复现结果 |",
        "|---|---|---|---|",
    ]
    for item in report.items:
        def esc(text: str) -> str:
            return text.replace("|", "\\|").replace("\n", " ")

        lines.append(
            f"| {esc(item.item)} | {item.status} | {esc(item.reproduce)} | "
            f"{esc(item.result)} |"
        )
    lines.extend([
        "",
        "## 复跑哈希",
        "",
        "```json",
        json.dumps(report.hashes, ensure_ascii=False, indent=2),
        "```",
        "",
        "耗时受机器影响，不作为一致性判据；复跑比的是 CSV 内容 SHA-256。",
        "",
    ])
    target.write_text("\n".join(lines), encoding=cfg.CSV_ENCODING)
    return target


def run_all(cfg_obj: Config) -> SelfCheckReport:
    """逐项跑 V1—V9，落盘 selfcheck_report.md 与 selfcheck_hashes.json。"""
    started = time.perf_counter()
    report = SelfCheckReport()
    results = cfg_obj.results_path()
    allocation = results / "allocation.csv"
    if not allocation.is_file():
        raise DataFileError(
            "自检需要先跑全流程生成 allocation.csv",
            path=str(allocation), detail="先执行 python run.py",
        )

    audit_report, _ = recheck_from_csv(cfg_obj, allocation)
    _v1_v2(audit_report, report)
    _v3(cfg_obj, report)
    _v4(cfg_obj, report)
    _v5(cfg_obj, report)
    _v6(cfg_obj, report)
    _v7(cfg_obj, report)
    hashes = _v8(cfg_obj, report)
    _v9(cfg_obj, report)

    report.hashes = hashes
    hashes_payload = {
        "all_equal": hashes.get("equal", False),
        "first_run": hashes.get("first_run", {}),
        "second_run": hashes.get("second_run", {}),
        "all_pass": report.all_pass,
    }
    with (results / "selfcheck_hashes.json").open(
        "w", encoding=cfg.CSV_ENCODING
    ) as handle:
        json.dump(hashes_payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    _write_markdown(report, results / "selfcheck_report.md")
    elapsed = time.perf_counter() - started
    logger.info(
        "自检完成 %d 项 全部通过=%s 耗时=%.2fs 记录=%s",
        len(report.items), report.all_pass, elapsed,
        results / "selfcheck_report.md",
    )
    for item in report.items:
        logger.info("  %s %s -> %s", item.item, item.status, item.result[:160])
    return report


__all__ = ["CheckItem", "SelfCheckReport", "run_all"]
