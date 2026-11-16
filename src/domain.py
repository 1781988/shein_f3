"""全部数据类定义。

字段与类型按五条硬约束和指标审计需要定义；
额外补充的字段都是求解/审计需要的内部派生量，不写进 allocation.csv。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    ALLOCATION_COLUMNS,
    Config,
    EvalRange,
    Scenario,
)
from .injection import InjectionKind
from .sensitivity import SensitivityLevel
from .time_axis import TimeAxis


# ---------------------------------------------------------------------------
# 输入侧
# ---------------------------------------------------------------------------
@dataclass
class SupplyRow:
    """货源宽表的一行（渠道 × 子仓），480 个小时体积的整数值。"""

    channel_id: str = ""
    channel_injection: str = ""
    warehouse_id: str = ""
    sensitive_raw: str = ""
    level: SensitivityLevel = SensitivityLevel.NONE
    hourly_volumes: List[int] = field(default_factory=list)
    row_index: int = 0
    declared_total: int = 0               # 「合计体积」列，仅用于差异核对
    exact_sum_int: int = 0                 # 480 列原始四位小数的精确整数和（守恒基准）


@dataclass
class SupplyCell:
    """一个 (渠道, 子仓, 产出小时) 供给格，整数体积。"""

    row_index: int = 0
    hour_index: int = 0
    original_volume: int = 0
    remaining_volume: int = 0
    available_at: Optional[datetime] = None
    channel_id: str = ""
    channel_injection: str = ""
    warehouse_id: str = ""
    level: SensitivityLevel = SensitivityLevel.NONE
    pack_date: str = ""
    density: float = 0.0
    density_missing: bool = False
    key: str = ""

    def cell_key(self) -> str:
        """稳定唯一的格键 ``row_index:hour_index``。"""
        return self.key or f"{self.row_index}:{self.hour_index}"


@dataclass
class DensityRecord:
    """密度表一行；只取密度列，体积/重量列不用。"""

    channel_id: str = ""
    warehouse_id: str = ""
    pack_date: str = ""
    density: Optional[float] = None


@dataclass
class CostRate:
    """目的港单公斤成本。"""

    by_port: Dict[str, float] = field(default_factory=dict)

    def rate_for(self, port: str) -> float:
        """未知目的港返回 0，由校验层记录 issues。"""
        return float(self.by_port.get(str(port).strip(), 0.0))


@dataclass
class AllocationRecord:
    """一条配货明细，只保留 CSV 六列需要的字段 + 内部回溯字段。

    ``to_csv_row()`` 是 csv_writer 的接口预留：严格 6 列、不加列。
    """

    bill_no: str = ""
    channel_id: str = ""
    warehouse_id: str = ""
    hour_index: int = 0
    hour_label: str = ""
    pickup_time_str: str = ""
    volume: int = 0
    cell_key: str = ""                     # 内部：退回时定位供给格
    in_eval_range: bool = False            # 内部：指标统计标记

    def to_csv_row(self, volume_formatter: Any) -> List[str]:
        """返回严格 6 列的字符串行，顺序与 ALLOCATION_COLUMNS 一致。"""
        return [
            self.bill_no,
            self.channel_id,
            self.warehouse_id,
            self.hour_label,
            self.pickup_time_str,
            volume_formatter(self.volume),
        ]

    def sort_key(self) -> Tuple[str, str, str, str, int]:
        """输出确定性排序键 (提单号, 产出小时, 渠道ID, 子仓ID, 体积)。"""
        return (
            self.bill_no, self.hour_label, self.channel_id,
            self.warehouse_id, self.volume,
        )


@dataclass
class Waybill:
    """一张舱位提单。"""

    bill_no: str = ""
    dest_port: str = ""
    plan_volume: int = 0
    earliest_pickup: Optional[datetime] = None
    required_arrival: Optional[datetime] = None
    latest_pickup: Optional[datetime] = None
    planned_departure: Optional[datetime] = None
    billing_type: str = ""
    sensitive_raw: str = ""
    level: SensitivityLevel = SensitivityLevel.NONE
    is_abnormal: bool = False
    pickup_time: Optional[datetime] = None
    locked_warehouse: str = ""
    records: List[AllocationRecord] = field(default_factory=list)
    visible_from: Optional[datetime] = None
    locked: bool = False

    def allocated(self) -> int:
        """已配方数（整数单位）。"""
        return sum(record.volume for record in self.records)

    def remaining(self) -> int:
        """剩余可配方数，永不为负。"""
        value = self.plan_volume - self.allocated()
        return value if value > 0 else 0

    def is_full(self) -> bool:
        """是否已配满计划体积。"""
        return self.remaining() <= 0


@dataclass
class DataIssue:
    """一条数据边界问题（results/data_issues.csv 的一行）。"""

    category: str = ""
    location: str = ""
    raw_value: str = ""
    action: str = ""

    def to_row(self) -> List[str]:
        """转成 CSV 行。"""
        return [self.category, self.location, self.raw_value, self.action]


@dataclass
class NotesMeta:
    """备注表结构信息，只存存在性与条数，不存原文。"""

    sheet_exists: bool = False
    rule_count: int = 0
    title_present: bool = False


@dataclass
class Dataset:
    """五张表加载后的聚合结果。"""

    supply_rows: List[SupplyRow] = field(default_factory=list)
    densities: List[DensityRecord] = field(default_factory=list)
    waybills: List[Waybill] = field(default_factory=list)
    cost_rates: CostRate = field(default_factory=CostRate)
    issues: List[DataIssue] = field(default_factory=list)
    axis: Optional[TimeAxis] = None
    notes_meta: NotesMeta = field(default_factory=NotesMeta)
    hour_column_names: List[str] = field(default_factory=list)
    source_hashes: Dict[str, str] = field(default_factory=dict)
    # 反查索引（按「渠道 × 子仓」这一行，不是按渠道 ID）
    sensitive_by_cw: Dict[Tuple[str, str], SensitivityLevel] = field(default_factory=dict)
    injection_by_cw: Dict[Tuple[str, str], str] = field(default_factory=dict)
    density_by_cwd: Dict[Tuple[str, str, str], Optional[float]] = field(default_factory=dict)
    skipped_waybills: int = 0
    duplicate_bill_count: int = 0
    abnormal_count: int = 0
    shj_bill_count: int = 0
    empty_density_count: int = 0
    total_declared_diff: int = 0
    max_declared_diff: int = 0
    total_supply_volume: int = 0
    eval_range_supply_volume: int = 0
    nonzero_cells: int = 0


# ---------------------------------------------------------------------------
# 求解侧
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    """一个 (供给格, 可取量) 候选，附带字典序打分所需的软目标量。"""

    cell: SupplyCell = field(default_factory=SupplyCell)
    amount: int = 0
    injection: InjectionKind = InjectionKind.ALTERNATIVE
    exact_sensitive_match: bool = False
    backlog_hours: float = 0.0
    unit_cost: float = 0.0


@dataclass
class DiagnosticsRaw:
    """求解期诊断原料：构造期冲突计数与全 480 小时非零格去向。"""

    steps: int = 0
    event_steps: int = 0
    reassign_steps: int = 0
    incremental_steps: int = 0
    refund_count: int = 0
    construction_conflicts: Dict[str, int] = field(default_factory=dict)
    construction_samples: List[str] = field(default_factory=list)
    total_nonzero_cells: int = 0
    released_cells: int = 0
    allocated_volume: int = 0
    unallocated_volume: int = 0
    unallocated_cells: List[Dict[str, Any]] = field(default_factory=list)
    hour_disposition: List[Dict[str, Any]] = field(default_factory=list)
    disposition_covered: bool = False
    locked_waybills: int = 0
    visible_peak: int = 0

    def conflict_total(self) -> int:
        """构造期五条硬约束的冲突总数。"""
        return sum(self.construction_conflicts.values())


@dataclass
class Solution:
    """一个场景的完整求解结果。"""

    scenario_name: str = ""
    records: List[AllocationRecord] = field(default_factory=list)
    waybill_state: Dict[str, Waybill] = field(default_factory=dict)
    diagnostics: DiagnosticsRaw = field(default_factory=DiagnosticsRaw)

    def total_volume(self) -> int:
        """全部配出方数（整数单位）。"""
        return sum(record.volume for record in self.records)


# ---------------------------------------------------------------------------
# 输出侧（T04/T05 使用，此处先定义完整字段）
# ---------------------------------------------------------------------------
@dataclass
class Metrics:
    """七项指标 + 口径说明。"""

    fill_rate: float = 0.0
    exact_match_rate: float = 0.0
    backlog_rate: float = 0.0
    primary_injection_rate: float = 0.0
    cost: float = 0.0
    unallocated_volume: int = 0
    virtual_warehouse_count: int = 0
    formula_notes: Dict[str, str] = field(default_factory=dict)


@dataclass
class AuditFinding:
    """一条硬约束的审计结果。"""

    constraint_id: str = ""
    checked: int = 0
    conflicts: int = 0
    samples: List[str] = field(default_factory=list)


@dataclass
class AuditReport:
    """后处理审计报告。"""

    findings: List[AuditFinding] = field(default_factory=list)
    all_pass: bool = True
    hash_before: str = ""
    hash_after: str = ""


@dataclass
class DiagnosisRecord:
    """一张未配满提单的原因链条。"""

    bill_no: str = ""
    gap_volume: int = 0
    primary_reason: str = ""
    reason_chain: str = ""
    evidence: str = ""

    def to_row(self, volume_formatter: Any) -> List[str]:
        """转成 CSV 行。"""
        return [
            self.bill_no, volume_formatter(self.gap_volume),
            self.primary_reason, self.reason_chain, self.evidence,
        ]


@dataclass
class ScaleRow:
    """一行规模实验结果。"""

    scale: float = 1.0
    method: str = ""
    fill_rate: float = 0.0
    backlog_rate: float = 0.0
    cost: float = 0.0
    elapsed_seconds: float = 0.0
    record_count: int = 0


__all__ = [
    "SupplyRow", "SupplyCell", "DensityRecord", "CostRate", "AllocationRecord",
    "Waybill", "DataIssue", "NotesMeta", "Dataset", "Candidate",
    "DiagnosticsRaw", "Solution", "Metrics", "AuditFinding", "AuditReport",
    "DiagnosisRecord", "ScaleRow", "EvalRange", "Scenario", "Config",
    "ALLOCATION_COLUMNS", "TimeAxis", "SensitivityLevel", "InjectionKind",
]
