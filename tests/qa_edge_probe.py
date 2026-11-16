#!/usr/bin/env python3
"""QA 边界 / 错误路径独立实测：用合成样例跑真实 CLI，核对退出码与 data_issues.csv。

与工程师单测的区别：这里走 `python run.py --only load` 真实命令行，
检查进程退出码、控制台报错文本，以及 data_issues.csv 是否真的落盘记录。
合成样例由 tests/fixtures/make_fixtures.py 生成（纯合成数据，不含资料包原始数据），
输出到临时目录，跑完自动删除。

用法（项目根执行）：
    python tests/qa_edge_probe.py
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fixtures.make_fixtures import make_all  # noqa: E402

TMP = ROOT / "_qa_edge_tmp"
FIXTURES = TMP / "fixtures"
OUT = TMP / "out"
BASE_SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))

# 03 系统设计第五节期望：退出码 / 控制台异常标签 / data_issues.csv 里的关键字
# error_kw 致命类查控制台；issue_kw 非致命类查落盘的 data_issues.csv 全文。
EXPECT = {
    "valid":                {"code": 0, "error_kw": "", "issue_kw": ""},
    "empty_wide":           {"code": 2, "error_kw": "表结构", "issue_kw": ""},
    "missing_column":       {"code": 2, "error_kw": "表结构", "issue_kw": ""},
    "bad_notes":            {"code": 2, "error_kw": "表结构", "issue_kw": ""},
    "missing_notes_sheet":  {"code": 2, "error_kw": "表结构", "issue_kw": ""},
    "negative_volume":      {"code": 2, "error_kw": "数据校验", "issue_kw": ""},
    "nan_volume":           {"code": 2, "error_kw": "数据校验", "issue_kw": ""},
    "non_numeric_volume":   {"code": 2, "error_kw": "数据校验", "issue_kw": ""},
    "bad_time":             {"code": 0, "error_kw": "", "issue_kw": "时间"},
    "time_failure_over_limit": {"code": 2, "error_kw": "数据校验", "issue_kw": ""},
    "duplicate_bill":       {"code": 0, "error_kw": "", "issue_kw": "重复"},
    "all_zero":             {"code": 0, "error_kw": "", "issue_kw": ""},
    "empty_density":        {"code": 0, "error_kw": "", "issue_kw": "密度为空"},
    "bad_hour_column":      {"code": 2, "error_kw": "表结构", "issue_kw": ""},
    "existing_csv":         {"code": 0, "error_kw": "", "issue_kw": ""},
}

PARAM_CASES = [
    ("缺 xlsx（--data-dir 不存在）", ["--data-dir", str(TMP / "no_such_dir"), "--only", "load"], 2, "配置错误"),
    ("非法 seed",                    ["--seed", "abc", "--only", "load"], 2, "配置错误"),
    ("未知场景",                     ["--scenario", "NO_SUCH_SCENARIO", "--only", "load"], 2, "配置错误"),
    ("scale 1.5 越界",               ["--scale", "1.5", "--only", "load"], 2, "配置错误"),
    ("--strict-no-overwrite 且 CSV 已存在", ["--strict-no-overwrite", "--only", "load"], 3, "输出冲突"),
]


def run_cli(args: list[str], env: dict) -> dict:
    cmd = [sys.executable, "run.py"] + args
    p = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return {"cmd": "python " + " ".join(["run.py"] + args), "code": p.returncode,
            "lines": lines, "tail": lines[-4:] if lines else []}


def read_issues(path: Path) -> dict:
    if not path.is_file():
        return {"exists": False, "rows": 0, "categories": {}, "sample": "", "text": ""}
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    cats: dict[str, int] = {}
    for r in rows:
        cats[r.get("类别", "")] = cats.get(r.get("类别", ""), 0) + 1
    text = "\n".join(" ".join(str(v) for v in r.values()) for r in rows)
    return {"exists": True, "rows": len(rows), "categories": cats, "text": text,
            "sample": (rows[0]["处理动作"][:90] if rows else "")}


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    made = make_all(FIXTURES)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    print("=" * 78)
    print("QA 边界 / 错误路径独立实测（真实 CLI，非单测）")
    print("=" * 78)

    results = []
    all_ok = True

    # A) 合成样例：每个样例用独立 settings.json 把 data_issues.csv 写进临时目录
    print("\n[A] 合成数据边界样例 → python run.py --config <临时settings> --only load")
    for name, data_dir in made.items():
        out_dir = OUT / name
        out_dir.mkdir(parents=True, exist_ok=True)
        cfg = dict(BASE_SETTINGS)
        cfg["data_dir"] = str(data_dir)
        cfg["outputs"] = {"dir": str(out_dir),
                          "allocation": "allocation.csv", "indicators": "indicators.csv"}
        cfg_path = TMP / f"settings_{name}.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        res = run_cli(["--config", str(cfg_path), "--only", "load"], env)
        iss = read_issues(out_dir / "data_issues.csv")
        exp = EXPECT[name]
        console = "\n".join(res["lines"])
        ok = (res["code"] == exp["code"]
              and (exp["error_kw"] in console if exp["error_kw"] else True)
              and (exp["issue_kw"] in iss["text"] if exp["issue_kw"] else True))
        all_ok &= ok
        results.append({"group": "A", "name": name, "expect_code": exp["code"],
                        "got_code": res["code"],
                        "issues": {k: v for k, v in iss.items() if k != "text"},
                        "ok": ok, "cmd": res["cmd"], "tail": res["tail"]})
        print(f"\n  · {name}: exit={res['code']} (期望 {exp['code']}) "
              f"issues={iss['rows']} 类别={iss['categories']} "
              f"校验(error_kw={exp['error_kw']!r}, issue_kw={exp['issue_kw']!r}) "
              f"判定={'通过' if ok else '失败'}")
        for l in res["tail"]:
            print(f"      | {l[:170]}")
        if iss["rows"]:
            print(f"      | issues 首条: {iss['sample']}")

    # B) 参数 / 输出冲突：直接对正式 results/ 触发
    print("\n[B] 参数与输出冲突（正式 config）")
    for label, args, exp_code, kw in PARAM_CASES:
        res = run_cli(args, env)
        ok = res["code"] == exp_code and (kw in "\n".join(res["lines"]))
        all_ok &= ok
        results.append({"group": "B", "name": label, "expect_code": exp_code,
                        "got_code": res["code"], "ok": ok,
                        "cmd": res["cmd"], "tail": res["tail"]})
        print(f"\n  · {label}: exit={res['code']} (期望 {exp_code}) 判定={'通过' if ok else '失败'}")
        print(f"      命令: {res['cmd']}")
        for l in res["tail"]:
            print(f"      | {l[:170]}")

    print("\n[汇总]")
    print(f"  用例总数 = {len(results)}  通过 = {sum(1 for r in results if r['ok'])}  "
          f"失败 = {sum(1 for r in results if not r['ok'])}")
    print(f"  总判定 = {'通过' if all_ok else '失败'}")

    payload = {"all_pass": all_ok, "cases": results}
    (ROOT / "results" / "qa_edge_probe.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  结构化结果已写入 results/qa_edge_probe.json")

    shutil.rmtree(TMP, ignore_errors=True)
    print("  临时样例目录已删除:", TMP)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
