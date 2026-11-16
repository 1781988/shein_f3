"""集中常量与配置加载。

项目里所有精度单位、时间格式、评估范围、敏感品等级映射、注入点特例、
CSV 列名与排序键都只在本文件定义，其他模块一律引用这里的常量，
不在源码里另写精度或等级字面量。配置值来自 ``config/settings.json``，
命令行可以覆盖；覆盖后统一走 ``Config.validate()`` 校验。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import ConfigError

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
SETTINGS_PATH: Path = PROJECT_ROOT / "config" / "settings.json"
CONSTRAINTS_PATH: Path = PROJECT_ROOT / "config" / "constraints.json"
DEFAULT_DATA_DIR: str = "../F3_一舱装满_资料包/data"

# ---------------------------------------------------------------------------
# 单位与精度
# ---------------------------------------------------------------------------
VOLUME_UNIT: int = 10000                 # 1 整数单位 = 0.0001 立方米
VOLUME_DECIMALS: int = 4
VOLUME_TOLERANCE_INT: int = 5            # 0.0005 立方米 = 5 个整数单位
VOLUME_TOLERANCE_TEXT: str = "0.0005"
COST_DECIMALS: int = 2
DENSITY_DECIMALS: int = 4

# ---------------------------------------------------------------------------
# 时间格式与时间轴
# ---------------------------------------------------------------------------
TIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"
HOUR_FORMAT: str = "%Y-%m-%d %H:%M"
DATE_FORMAT: str = "%Y-%m-%d"
HOUR_COLUMN_LEFT_LEN: int = 16          # "YYYY-MM-DD HH:MM" 的长度
HOURS: int = 480
DAYS: int = 20
AXIS_START_STR: str = "2026-10-01 00:00:00"
AXIS_END_STR: str = "2026-10-21 00:00:00"
EVAL_START_STR: str = "2026-10-03 00:00"
EVAL_END_INCLUSIVE_STR: str = "2026-10-17 23:00"
EVAL_HOURS: int = 360
WINDOW_DAYS: int = 2
ROLL_STEP: str = "hour"
FREE_BACKLOG_HOURS_DEFAULT: int = 24
ABNORMAL_FALLBACK_HOURS: int = 24
ABNORMAL_FALLBACK_TEXT: str = "最早提货时间 + 24 小时"
DEFAULT_SEED: int = 20261001

# ---------------------------------------------------------------------------
# 业务常量
# ---------------------------------------------------------------------------
SHJ_PORT: str = "SHJ"
BILLING_CHARTER: str = "包机"
BILLING_BY_WEIGHT: str = "按重量"

# 敏感品等级。数值只在这里定义，映射表也只在这里给出；
# sensitivity.py 是唯一实现「字符串 -> 等级」解析逻辑的模块。
LEVEL_NONE: int = 0
LEVEL_SENSITIVE_GENERAL: int = 1
LEVEL_DRY: int = 2
LEVEL_LITHIUM: int = 3

LEVEL_NAMES: Dict[int, str] = {
    LEVEL_NONE: "无（普货）",
    LEVEL_SENSITIVE_GENERAL: "敏感普货",
    LEVEL_DRY: "干电",
    LEVEL_LITHIUM: "锂电",
}

# 提单表写法 / 货源表写法 / 备注写法 的并集，取最高层级。
# 注意「无（普货）」是等级 0，「普货」是等级 1（敏感普货），二者不能混。
SENSITIVITY_TOKEN_LEVELS: Dict[str, int] = {
    "无": LEVEL_NONE,
    "无（普货）": LEVEL_NONE,
    "无(普货)": LEVEL_NONE,
    "敏感普货": LEVEL_SENSITIVE_GENERAL,
    "普货": LEVEL_SENSITIVE_GENERAL,
    "干电产品": LEVEL_DRY,
    "干电": LEVEL_DRY,
    "锂电产品": LEVEL_LITHIUM,
    "锂电": LEVEL_LITHIUM,
}
SENSITIVITY_SEPARATORS: Tuple[str, ...] = (",", "，", "、", ";", "；", "/")

# 注入点分类
INJECTION_PRIMARY: str = "PRIMARY"
INJECTION_ALTERNATIVE: str = "ALTERNATIVE"
INJECTION_SHJ_ALT: str = "SHJ_ALT"

# 字典序打分的五个层次（顺序可由场景配置置换）
PRIORITY_LAYERS: Tuple[str, ...] = ("fill", "sensitive", "backlog", "injection", "cost")
DEFAULT_PRIORITY_ORDER: Tuple[str, ...] = PRIORITY_LAYERS

# ---------------------------------------------------------------------------
# CSV 列名与编码（allocation.csv 严格 6 列，不加列；csv_writer 的接口预留）
# ---------------------------------------------------------------------------
ALLOCATION_COLUMNS: Tuple[str, ...] = (
    "提单号", "货源渠道", "子仓", "产出小时", "提货时刻", "配货方数",
)
ALLOCATION_HEADER: str = ",".join(ALLOCATION_COLUMNS)
INDICATORS_COLUMNS: Tuple[str, ...] = ("指标", "数值", "口径说明")
DATA_ISSUES_COLUMNS: Tuple[str, ...] = ("类别", "位置", "原值", "处理动作")
DIAGNOSIS_COLUMNS: Tuple[str, ...] = ("提单号", "缺口方数", "首要原因", "原因链条", "证据")
CSV_ENCODING: str = "utf-8"
CSV_LINE_TERMINATOR: str = "\n"

# 全局确定的排序键名（禁止用集合迭代结果直接做决策或输出）
SORT_KEY_WAYBILL: str = "scenario_priority,bill_no"
SORT_KEY_CELL: str = "warehouse,level,injection,available_hour,channel"
SORT_KEY_RECORD: str = "bill_no,hour_label,channel_id,warehouse_id"

# ---------------------------------------------------------------------------
# 五张表的表名与列名
# ---------------------------------------------------------------------------
SHEET_NOTES: str = "数据使用备注"
SHEET_WIDE: str = "每小时体积_宽表"
SHEET_DENSITY: str = "每天体积重量密度"
SHEET_WAYBILL: str = "提单明细"
SHEET_COST: str = "目的港单kg成本"
NOTES_RULE_COUNT: int = 9
WAYBILL_TRAILING_EMPTY_COLUMNS: int = 2

WIDE_FIXED_COLUMNS: Tuple[str, ...] = (
    "渠道ID", "渠道注入点", "子仓ID", "敏感品", "合计体积",
)
WIDE_TOTAL_COLUMN: str = "合计体积"        # 只用于差异核对，绝不作为守恒基准
DENSITY_REQUIRED_COLUMNS: Tuple[str, ...] = (
    "渠道ID", "子仓ID", "封箱日期", "密度(kg/m³)",
)
COST_REQUIRED_COLUMNS: Tuple[str, ...] = ("目的港", "1001-1020整体")
WAYBILL_REQUIRED_COLUMNS: Tuple[str, ...] = (
    "提单号", "目的港代码", "计划体积(m³)", "计划起飞时间",
    "需求到站时间", "最早提货时间", "计费类型", "舱位可走敏感品类型",
)

# 列名到语义的映射（loader 按表头名定位，不写死列序号）
WAYBILL_FIELD_COLUMNS: Dict[str, str] = {
    "bill_no": "提单号",
    "dest_port": "目的港代码",
    "plan_volume": "计划体积(m³)",
    "planned_departure": "计划起飞时间",
    "required_arrival": "需求到站时间",
    "earliest_pickup": "最早提货时间",
    "billing_type": "计费类型",
    "sensitive_raw": "舱位可走敏感品类型",
}

# ---------------------------------------------------------------------------
# 诊断白名单与被排除规则黑名单（config/constraints.json 是权威来源，
# 这里只是默认兜底，避免配置文件缺失时程序无法启动）
# ---------------------------------------------------------------------------
DEFAULT_ALLOWED_REASONS: Tuple[str, ...] = (
    "R1_全量可用货源已配尽",
    "R2_可见窗口内无剩余可配货",
    "R3_敏感品等级无兼容货源",
    "R4_单子仓约束下无兼容货源",
    "R5_提货时间窗内无兼容货源",
    "R6_提单计划体积封顶后仍有缺口且无兼容货源",
)
DEFAULT_EXCLUDED_RULE_KEYWORDS: Tuple[str, ...] = (
    "限重", "耗材", "计费重", "集装板", "清关资质", "可混配",
    "重量上限", "载重", "板位尺寸", "混配组合",
)

# 数据边界问题的固定类别（写入 data_issues.csv 的「类别」列）
ISSUE_CATEGORY: Dict[str, str] = {
    "notes": "备注结构",
    "schema": "表结构",
    "time": "时间",
    "numeric": "数值",
    "density": "密度",
    "business": "业务",
    "output": "输出",
    "param": "参数",
    "info": "info",
}


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvalRange:
    """评估范围记录集合 A：产出小时落在 [start, end_inclusive] 的配货记录。

    全 480 小时货源都参与配载；货源方数类指标（敏感品完全匹配率、时效积压率、
    主注入点选择率、成本、未配提单货量）只统计 A；配满率与虚仓提单数量是提单侧
    口径，按完整配载结果与全部提单统计，不受 A 限制。
    """

    start: datetime
    end_inclusive: datetime

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EvalRange":
        """从 settings.json 的 eval_range 段构造，格式 ``YYYY-MM-DD HH:MM``。"""
        try:
            start = datetime.strptime(str(raw["start"]), HOUR_FORMAT)
            end = datetime.strptime(str(raw["end_inclusive_hour"]), HOUR_FORMAT)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(
                "eval_range 格式非法，合法格式 YYYY-MM-DD HH:MM，"
                f"示例 {EVAL_START_STR} / {EVAL_END_INCLUSIVE_STR}；原始值 {dict(raw)!r}"
            ) from exc
        return cls(start=start, end_inclusive=end)


@dataclass(frozen=True)
class Scenario:
    """一个优先级顺序场景。priority_order 是五个层次的严格字典序。"""

    name: str
    priority_order: Tuple[str, ...] = DEFAULT_PRIORITY_ORDER

    def score_key(self, candidate: Any, waybill: Any) -> Tuple[float, ...]:
        """委托给 scoring.score_key，延迟导入避免与 domain 形成循环依赖。"""
        from .scoring import score_key as _score_key

        return _score_key(candidate, waybill, self)

    def rank(self, layer: str) -> int:
        """返回某层次在本场景里的序号，序号越小优先级越高。"""
        try:
            return self.priority_order.index(layer)
        except ValueError as exc:
            raise ConfigError(
                f"场景 {self.name} 缺少打分层次 {layer!r}，"
                f"合法层次 {list(PRIORITY_LAYERS)}"
            ) from exc


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Config:
    """运行配置。字段与 settings.json 一一对应，命令行覆盖后统一校验。"""

    data_dir: str = DEFAULT_DATA_DIR
    wide_file: str = "中东大箱体积_渠道子仓_每小时_1001-1020.xlsx"
    bill_file: str = "提单_1001-1020提交.xlsx"
    seed: int = DEFAULT_SEED
    volume_unit: int = VOLUME_UNIT
    eval_range: EvalRange = field(
        default_factory=lambda: EvalRange(
            datetime.strptime(EVAL_START_STR, HOUR_FORMAT),
            datetime.strptime(EVAL_END_INCLUSIVE_STR, HOUR_FORMAT),
        )
    )
    window_days: int = WINDOW_DAYS
    roll_step: str = ROLL_STEP
    free_backlog_hours: int = FREE_BACKLOG_HOURS_DEFAULT
    scenarios: List[Scenario] = field(default_factory=list)
    scales: List[float] = field(default_factory=lambda: [0.25, 0.5, 1.0])
    enable_reassign: bool = True
    strict_no_overwrite: bool = False
    extra_csv_columns: bool = False
    outputs: Dict[str, str] = field(
        default_factory=lambda: {
            "dir": "results", "allocation": "allocation.csv", "indicators": "indicators.csv",
        }
    )
    constraints: Dict[str, Any] = field(default_factory=dict)
    project_root: Path = PROJECT_ROOT
    settings_path: Path = SETTINGS_PATH
    constraints_path: Path = CONSTRAINTS_PATH
    selected_scenario: str = ""

    # -- 构造 --------------------------------------------------------------
    @classmethod
    def load(
        cls,
        path: Optional[Path] = None,
        overrides: Optional[Mapping[str, Any]] = None,
    ) -> "Config":
        """读取 settings.json 与 constraints.json，套用命令行覆盖并校验。"""
        settings_path = Path(path) if path is not None else SETTINGS_PATH
        if not settings_path.is_absolute():
            settings_path = (PROJECT_ROOT / settings_path).resolve()
        raw = _read_json(settings_path, "settings.json")
        constraints = _read_json(CONSTRAINTS_PATH, "constraints.json")

        scenarios = [
            Scenario(name=str(item["name"]), priority_order=tuple(item["priority_order"]))
            for item in raw.get("scenarios", [])
        ]
        cfg = cls(
            data_dir=str(raw.get("data_dir", DEFAULT_DATA_DIR)),
            wide_file=str(raw.get("wide_file", cls.wide_file)),
            bill_file=str(raw.get("bill_file", cls.bill_file)),
            seed=int(raw.get("seed", DEFAULT_SEED)),
            volume_unit=int(raw.get("volume_unit", VOLUME_UNIT)),
            eval_range=EvalRange.from_mapping(raw.get("eval_range", {})),
            window_days=int(raw.get("window_days", WINDOW_DAYS)),
            roll_step=str(raw.get("roll_step", ROLL_STEP)),
            free_backlog_hours=int(raw.get("free_backlog_hours", FREE_BACKLOG_HOURS_DEFAULT)),
            scenarios=scenarios,
            scales=[float(x) for x in raw.get("scales", [0.25, 0.5, 1.0])],
            enable_reassign=bool(raw.get("enable_reassign", True)),
            strict_no_overwrite=bool(raw.get("strict_no_overwrite", False)),
            extra_csv_columns=bool(raw.get("extra_csv_columns", False)),
            outputs=dict(raw.get("outputs", cls().outputs)),
            constraints=constraints,
            project_root=PROJECT_ROOT,
            settings_path=settings_path,
            constraints_path=CONSTRAINTS_PATH,
        )
        if overrides:
            cfg = cfg.apply_overrides(overrides)
        cfg.validate()
        return cfg

    def apply_overrides(self, overrides: Mapping[str, Any]) -> "Config":
        """套用命令行覆盖。未知键直接报配置错误，不静默忽略。"""
        allowed = {
            "data_dir", "seed", "scenario", "scale", "strict_no_overwrite",
            "enable_reassign", "roll_step", "window_days", "free_backlog_hours",
            "log_no_timestamp", "only", "self_check",
        }
        unknown = sorted(set(overrides) - allowed)
        if unknown:
            raise ConfigError(
                f"未知配置项 {unknown}；合法命令行参数见 python run.py --help"
            )
        patch: Dict[str, Any] = {}
        if "data_dir" in overrides and overrides["data_dir"] is not None:
            patch["data_dir"] = str(overrides["data_dir"])
        if "seed" in overrides and overrides["seed"] is not None:
            patch["seed"] = overrides["seed"]
        if "strict_no_overwrite" in overrides and overrides["strict_no_overwrite"]:
            patch["strict_no_overwrite"] = True
        if "enable_reassign" in overrides and overrides["enable_reassign"] is not None:
            patch["enable_reassign"] = bool(overrides["enable_reassign"])
        if "roll_step" in overrides and overrides["roll_step"] is not None:
            patch["roll_step"] = str(overrides["roll_step"])
        if "window_days" in overrides and overrides["window_days"] is not None:
            patch["window_days"] = int(overrides["window_days"])
        if "free_backlog_hours" in overrides and overrides["free_backlog_hours"] is not None:
            patch["free_backlog_hours"] = int(overrides["free_backlog_hours"])
        cfg = replace(self, **patch) if patch else self

        selected = overrides.get("scenario")
        if selected:
            names = [s.name for s in cfg.scenarios]
            if selected not in names:
                raise ConfigError(
                    f"未知场景 {selected!r}；合法取值 {names}"
                )
            cfg.selected_scenario = str(selected)
            cfg.scenarios = [s for s in cfg.scenarios if s.name == selected]
        scale = overrides.get("scale")
        if scale is not None:
            value = float(scale)
            if not (0.0 < value <= 1.0):
                raise ConfigError(
                    f"scale 必须在 (0, 1] 内，当前值 {value}；合法取值如 0.25 / 0.5 / 1.0"
                )
            cfg.scales = [value]
        return cfg

    # -- 校验 --------------------------------------------------------------
    def validate(self) -> None:
        """集中校验。任一不合法抛 ConfigError（退出码 2）。"""
        if self.volume_unit != VOLUME_UNIT:
            raise ConfigError(
                f"volume_unit 必须是 {VOLUME_UNIT}（四位小数整数化），当前值 {self.volume_unit}"
            )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ConfigError(
                f"seed 必须是整数，当前值 {self.seed!r}；合法取值如 {DEFAULT_SEED}"
            )
        if self.roll_step != ROLL_STEP:
            raise ConfigError(
                f"roll_step 只支持 {ROLL_STEP!r}，当前值 {self.roll_step!r}"
            )
        if self.window_days < 0 or self.window_days > DAYS:
            raise ConfigError(
                f"window_days 必须在 [0, {DAYS}]，当前值 {self.window_days}"
            )
        if self.free_backlog_hours < 0:
            raise ConfigError(
                f"free_backlog_hours 必须非负，当前值 {self.free_backlog_hours}"
            )
        if self.eval_range.start > self.eval_range.end_inclusive:
            raise ConfigError(
                "eval_range.start 不能晚于 eval_range.end_inclusive_hour"
            )
        axis_end = datetime.strptime(AXIS_END_STR, TIME_FORMAT)
        if self.eval_range.start < datetime.strptime(AXIS_START_STR, TIME_FORMAT) \
                or self.eval_range.end_inclusive >= axis_end:
            raise ConfigError(
                f"eval_range 必须落在时间轴 {AXIS_START_STR} ~ {AXIS_END_STR} 内"
            )
        if not self.scenarios:
            raise ConfigError("scenarios 不能为空，至少需要一组优先级顺序")
        for scenario in self.scenarios:
            order = tuple(scenario.priority_order)
            if sorted(order) != sorted(PRIORITY_LAYERS):
                raise ConfigError(
                    f"场景 {scenario.name} 的 priority_order 必须是 "
                    f"{list(PRIORITY_LAYERS)} 的一个排列，当前值 {list(order)}"
                )
        if not self.scales:
            raise ConfigError("scales 不能为空")
        for value in self.scales:
            if not (0.0 < float(value) <= 1.0):
                raise ConfigError(
                    f"scales 每一项必须在 (0, 1]，当前值 {value}"
                )
        if self.extra_csv_columns:
            raise ConfigError(
                "allocation.csv 严格 6 列、不加列；extra_csv_columns 必须为 false"
            )
        if not self.data_dir:
            raise ConfigError("data_dir 不能为空；可用 --data-dir 指定原始资料包 data 目录")
        data_path = self.data_path()
        if not data_path.is_dir():
            raise ConfigError(
                f"data_dir 不存在或不是目录：{data_path}；"
                "请用 --data-dir 指向资料包 data 目录"
            )
        for name in (self.wide_file, self.bill_file):
            target = data_path / name
            if not target.is_file():
                raise ConfigError(
                    f"原始数据文件缺失：{target}；请检查 data_dir 与文件名"
                )

    # -- 派生路径 ----------------------------------------------------------
    def data_path(self) -> Path:
        """返回 data_dir 的绝对路径；相对路径按项目根解析。"""
        raw = Path(self.data_dir)
        return raw.resolve() if raw.is_absolute() else (self.project_root / raw).resolve()

    def wide_path(self) -> Path:
        """货源宽表绝对路径。"""
        return self.data_path() / self.wide_file

    def bill_path(self) -> Path:
        """提单表绝对路径。"""
        return self.data_path() / self.bill_file

    def results_path(self) -> Path:
        """结果目录绝对路径。"""
        raw = Path(self.outputs.get("dir", "results"))
        return raw.resolve() if raw.is_absolute() else (self.project_root / raw).resolve()

    def allowed_reasons(self) -> Tuple[str, ...]:
        """诊断原因白名单，来自 constraints.json。"""
        raw = self.constraints.get("allowed_reasons")
        return tuple(raw) if raw else DEFAULT_ALLOWED_REASONS

    def excluded_rule_keywords(self) -> Tuple[str, ...]:
        """六类被排除规则的关键字黑名单。"""
        raw = self.constraints.get("excluded_rule_keywords")
        return tuple(raw) if raw else DEFAULT_EXCLUDED_RULE_KEYWORDS

    def hard_constraints(self) -> Tuple[Mapping[str, Any], ...]:
        """五条硬约束元数据（C1—C5）。"""
        return tuple(self.constraints.get("hard_constraints", []))

    def active_scenarios(self) -> List[Scenario]:
        """本轮真正参与求解的场景（命令行 --scenario 可只选一个）。"""
        return list(self.scenarios)


def _read_json(path: Path, label: str) -> Dict[str, Any]:
    """读取 JSON 配置，失败时给出带路径的明确错误。"""
    if not path.is_file():
        raise ConfigError(f"配置文件缺失：{path}（{label}）")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ConfigError(f"配置文件不可读：{path}（{label}）：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{path}（{label}）：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是对象：{path}（{label}）")
    return data


def build_time_axis(cfg: Config) -> Any:
    """按配置构造 TimeAxis，延迟导入避免模块循环。"""
    from .time_axis import TimeAxis

    return TimeAxis.from_config(cfg)
