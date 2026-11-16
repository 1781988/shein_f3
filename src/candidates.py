"""候选生成：硬约束过滤、子仓锁定、按 (子仓, 等级) 分桶索引、同格合并。

性能约定：不做每步全表扫描。``CandidateGenerator`` 在每个 (子仓, 等级) 桶上
维护一个只前进的桶顶游标；已释放格是桶内按产出小时排序后的前缀。生成候选时
只查看每个相关桶桶顶的少量格，退回重排时把游标整体归零。
"""
from __future__ import annotations

import copy
from bisect import bisect_right
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config as cfg
from .config import Config
from .constraints import check_sensitive, check_single_warehouse, check_time_window
from .domain import AllocationRecord, Candidate, Dataset, SupplyCell, Waybill
from .injection import classify
from .ledger import SupplyLedger
from .sensitivity import is_exact_match
from .time_axis import TimeAxis

PER_BUCKET_CANDIDATES: int = 3


def build_candidate(
    cell: SupplyCell,
    waybill: Waybill,
    now: datetime,
    cfg_obj: Config,
    cost_rates: Any,
) -> Candidate:
    """把一个供给格构造成候选，附带字典序打分需要的软目标量。"""
    remaining_need = waybill.remaining()
    amount = cell.remaining_volume if cell.remaining_volume < remaining_need else remaining_need
    injection = classify(waybill.dest_port, cell.channel_injection)
    waiting_hours = 0.0
    if cell.available_at is not None:
        waiting_hours = (now - cell.available_at).total_seconds() / 3600.0
    backlog_hours = max(0.0, waiting_hours - cfg_obj.free_backlog_hours)
    unit_cost = 0.0
    if waybill.billing_type != cfg.BILLING_CHARTER and not cell.density_missing:
        unit_cost = cell.density * cost_rates.rate_for(waybill.dest_port)
    return Candidate(
        cell=cell,
        amount=amount,
        injection=injection,
        exact_sensitive_match=is_exact_match(cell.level, waybill.level),
        backlog_hours=backlog_hours,
        unit_cost=unit_cost,
    )


class CandidateGenerator:
    """持有桶顶游标的候选生成器（求解器复用同一个实例）。"""

    def __init__(
        self,
        ledger: SupplyLedger,
        cfg_obj: Config,
        dataset: Dataset,
        per_bucket: int = PER_BUCKET_CANDIDATES,
    ) -> None:
        self.ledger = ledger
        self.cfg = cfg_obj
        self.dataset = dataset
        self.axis: TimeAxis = ledger.axis
        self.per_bucket = per_bucket
        self.cursors: Dict[Tuple[str, int], int] = {
            bucket: 0 for bucket in ledger.by_wh_level
        }
        self.bucket_hours: Dict[Tuple[str, int], List[int]] = {
            bucket: [ledger.cells[k].hour_index for k in keys]
            for bucket, keys in ledger.by_wh_level.items()
        }
        self.warehouses: List[str] = sorted({bucket[0] for bucket in ledger.by_wh_level})
        self.current_hour: int = 0
        self.current_time: datetime = self.axis.start

    def set_step(self, hour_index: int) -> None:
        """设置当前决策步，供释放前缀与积压时长计算。"""
        self.current_hour = hour_index
        self.current_time = self.axis.decision_time(hour_index)
        self.ledger.current_hour = hour_index

    def reset_cursors(self) -> None:
        """退回重排后把所有桶顶游标归零。"""
        for bucket in self.cursors:
            self.cursors[bucket] = 0

    def _released_limit(self, bucket: Tuple[str, int]) -> int:
        """桶内已释放格的前缀长度（桶按产出小时升序）。"""
        return bisect_right(self.bucket_hours[bucket], self.current_hour)

    def generate(
        self,
        waybill: Waybill,
        locked_warehouse: str = "",
        per_bucket: Optional[int] = None,
    ) -> List[Candidate]:
        """为一张提单生成候选格列表。

        - 单提单只提一个子仓：未锁定时看全部子仓，已锁定时只看锁定子仓；
        - 敏感品等级兼容：只看等级不高于提单等级的桶；
        - 时间窗：只看已释放（可配时刻不晚于提单最晚提货时间）的格；
        - 被排除的六类规则在这层完全不出现。
        """
        if waybill.is_full():
            return []
        budget = self.per_bucket if per_bucket is None else per_bucket
        warehouses = [locked_warehouse] if locked_warehouse else self.warehouses
        max_level = int(waybill.level)
        result: List[Candidate] = []
        for warehouse in warehouses:
            for level in range(max_level + 1):
                bucket = (warehouse, level)
                keys = self.ledger.by_wh_level.get(bucket)
                if not keys:
                    continue
                limit = self._released_limit(bucket)
                head = self.cursors.get(bucket, 0)
                while head < limit and self.ledger.cells[keys[head]].remaining_volume <= 0:
                    head += 1
                self.cursors[bucket] = head
                index = head
                taken = 0
                while index < limit and taken < budget:
                    cell = self.ledger.cells[keys[index]]
                    if (cell.remaining_volume > 0
                            and check_sensitive(waybill, cell)
                            and check_single_warehouse(waybill, cell)
                            and check_time_window(waybill, cell)):
                        result.append(build_candidate(
                            cell, waybill, self.current_time, self.cfg,
                            self.dataset.cost_rates,
                        ))
                        taken += 1
                    index += 1
        return result


def generate(
    waybill: Waybill,
    ledger: SupplyLedger,
    axis: TimeAxis,
    locked_warehouse: str = "",
) -> List[Candidate]:
    """模块级候选生成入口（无状态版本，供基准与单测调用）。"""
    cfg_obj = ledger.cfg
    dataset = ledger.dataset
    generator = CandidateGenerator(ledger, cfg_obj, dataset)
    generator.set_step(getattr(ledger, "current_hour", 0))
    return generator.generate(waybill, locked_warehouse=locked_warehouse)


def merge_records(records: Sequence[AllocationRecord]) -> List[AllocationRecord]:
    """同一 (提单号, 渠道, 子仓, 产出小时) 合并成一行，取货时先合并再写。"""
    merged: Dict[Tuple[str, str, str, str], AllocationRecord] = {}
    order: List[Tuple[str, str, str, str]] = []
    for record in records:
        key = (record.bill_no, record.channel_id, record.warehouse_id, record.hour_label)
        if key not in merged:
            merged[key] = copy.copy(record)
            order.append(key)
        else:
            merged[key].volume += record.volume
    return [merged[key] for key in order]


__all__ = [
    "CandidateGenerator", "generate", "build_candidate", "merge_records",
    "PER_BUCKET_CANDIDATES",
]
