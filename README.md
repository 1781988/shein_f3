# F3 多渠道货源与舱位提单动态配载

把按小时产出的多渠道货源配到有时间窗、敏感品等级和单子仓限制的舱位提单上，输出逐条可核对的配货明细、七项指标、独立审计、实验对比、离线看板和方案报告 PDF。程序只读解析资料包里的两个 xlsx，用整数化体积做精确守恒。

## 目录导览

| 路径 | 内容 |
|---|---|
| `run.py` | 入口：`python run.py` 跑全流程，`python run.py --self-check` 跑自检 |
| `config/` | 路径、评估范围、种子、场景、规模、精度与约束配置 |
| `src/` | 求解、校验、指标、审计等模块，见 `docs/04_模型说明.md` |
| `tests/` | 标准库 unittest 用例与合成边界样例，不含资料包原始数据 |
| `docs/` | `04` 模型说明至 `13` 未完成项，共十份交付文档 |
| `dashboard/`、`report/` | 离线看板模板与构建脚本；报告 PDF 生成与分页 |
| `results/` | 运行产出，不进公开仓库（`.gitignore` 已排除） |

## 快速开始

```
python -m pip install -r requirements.txt
python run.py
python run.py --self-check
python tests/run_tests.py
```

`python run.py` 默认跑全流程，在 `results/` 产出 `allocation.csv`、`indicators.csv`、`audit_report.json`、`diagnosis.csv`、`metrics_scenarios.csv`、`scale_experiments.csv`、`baseline_comparison.csv`、`dashboard.html`、`方案报告.pdf`、`figures/`、`build_manifest.json`。默认 `data_dir` 为 `../F3_一舱装满_资料包/data`，可用 `--data-dir` 覆盖；两个 xlsx 只读引用。命令细节、退出码与复现步骤见 `docs/08_运行说明.md`，依赖与降级见 `docs/09_依赖说明.md`。

## 实现边界

- 五条硬约束：货源体积守恒、单提单配货不超过计划体积、单提单只提一个子仓、敏感品等级兼容、提货时间窗。
- 注入点主备匹配是软目标。
- 限重、耗材上限、计费重、集装板尺寸、清关资质、可混配组合六类规则不实现，也不进未配满诊断。
- 评估范围是统计范围，不是配载范围；全 480 小时货源都参与配载，明细输出全量。
- 七项指标的公式、分母与统计范围见 `docs/06_指标口径.md`；「未配提单货量」是供给侧剩余，不是提单缺口。
- 启发式不保证全局最优或真实 Pareto 前沿。假设见 `docs/05_假设清单.md`，未完成与降级见 `docs/13_未完成项.md` 与 `results/build_manifest.json`。

## 队伍信息

| 项目 | 内容 |
|---|---|
| 队名 | 北方一号 |
| 成员 | 高铭禹（负责完整流程） |
| 指导老师 | 刘环宇（提供建议与意见） |
| 仓库地址 | https://github.com/1781988/shein_f3.git |

费用、投入时长、协作日期等字段仍待填写，见 `docs/12_AI协作记录.md`。

## 合规声明

资料包数据与材料仅用于本赛事，不外传、不出售、不公开发布。赛后删除下载的 `F3_一舱装满_资料包/` 原始数据及本地结果副本。公开产出不含资料包原始数据行。作品链接保持可访问至 2026-12-25。
