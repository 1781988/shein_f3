"""先到先得贪心基准，独立实现，不调用主求解器。

做法（对应假设 B9）：
- 提单按 (最早提货时间, 提单号) 升序逐个处理，离线单趟跑完 480 小时；
- 锁定第一个有可用货的子仓，之后所有行都用这个子仓；
- 供给按 (产出小时, 渠道ID, 子仓ID) 先到先给，填到计划体积或无货为止；
- 不回头、不改配、不优化注入点 / 敏感品 / 成本 / 时效等软目标；
- 只过五条硬约束，与主方案共用 SupplyLedger 与 constraints。
"""
from __future__ import annotations

import copy
from typing import Dict, List, Optional, Tuple

from .candidates import merge_records
from .config import Config
from .constraints import CONSTRAINT_IDS, check_all
from .domain import (
    AllocationRecord,
    Dataset,
    DiagnosticsRaw,
    Solution,
    SupplyCell,
    Waybill,
)
from .ledger import SupplyLedger, global_cell_key
from .precision import format_time
from .time_axis import TimeAxis


class _BucketCursor:
    """按 (子仓, 等级) 桶维护只前进的游标，跳过已耗尽的格。"""

    def __init__(self, ledger: SupplyLedger) -> None:
        self.ledger = ledger
        self.cursors: Dict[Tuple[str, int], int] = {
            bucket: 0 for bucket in ledger.by_wh_level
        }

    def head(self, warehouse: str, level: int) -> Optional[SupplyCell]:
        """返回桶内第一个仍有剩余且已释放的格（桶按产出小时升序）。"""
        bucket = (warehouse, level)
        keys = self.ledger.by_wh_level.get(bucket)
        if not keys:
            return None
        index = self.cursors.get(bucket, 0)
        while index < len(keys):
            cell = self.ledger.cells[keys[index]]
            if cell.remaining_volume > 0:
                self.cursors[bucket] = index
                return cell
            index += 1
        self.cursors[bucket] = index
        return None


class FcfsBaseline:
    """先到先得贪心基准求解器（与主求解器完全独立）。"""

    def __init__(self, cfg_obj: Config, dataset: Dataset) -> None:
        self.cfg = cfg_obj
        self.dataset = dataset
        self.axis: TimeAxis = dataset.axis if dataset.axis is not None \
            else TimeAxis.from_config(cfg_obj)

    def _clone_waybills(self) -> Dict[str, Waybill]:
        """复制提单状态，不带入任何主求解器的记录。"""
        state: Dict[str, Waybill] = {}
        for source in self.dataset.waybills:
            clone = copy.copy(source)
            clone.records = []
            clone.locked_warehouse = ""
            clone.locked = False
            clone.pickup_time = None
            state[clone.bill_no] = clone
        return state

    def _finalize(self, waybill: Waybill, ledger: SupplyLedger) -> None:
        """按 B12 构造提货时刻并锁定记录。"""
        max_available = self.axis.start
        for record in waybill.records:
            cell = ledger.cells.get(record.cell_key)
            if cell is not None and cell.available_at is not None \
                    and cell.available_at > max_available:
                max_available = cell.available_at
        earliest = waybill.earliest_pickup or self.axis.start
        latest = waybill.latest_pickup or earliest
        pickup = self.axis.clamp_pickup_time(max_available, earliest, latest)
        waybill.pickup_time = pickup
        pickup_str = format_time(pickup)
        for record in waybill.records:
            record.pickup_time_str = pickup_str
        waybill.locked = True

    def run(self) -> Solution:
        """单趟离线贪心，返回基准 Solution。"""
        ledger = SupplyLedger(self.dataset, self.cfg)
        ledger.release_all()
        cursor = _BucketCursor(ledger)
        state = self._clone_waybills()
        diagnostics = DiagnosticsRaw(
            construction_conflicts={cid: 0 for cid in CONSTRAINT_IDS},
            total_nonzero_cells=ledger.nonzero_cell_count(),
        )
        warehouses: List[str] = sorted({row.warehouse_id for row in self.dataset.supply_rows})

        ordered = sorted(
            state.values(),
            key=lambda w: (w.earliest_pickup or self.axis.start, w.bill_no),
        )
        for waybill in ordered:
            # 1) 锁定第一个有可用货的子仓（按子仓 ID 升序）
            chosen: Optional[str] = None
            for warehouse in warehouses:
                for level in range(int(waybill.level) + 1):
                    cell = cursor.head(warehouse, level)
                    if cell is None:
                        continue
                    if cell.available_at is not None \
                            and waybill.latest_pickup is not None \
                            and cell.available_at > waybill.latest_pickup:
                        # 桶按产出小时升序，本桶后续格可配时刻更晚，整桶跳过
                        continue
                    chosen = warehouse
                    break
                if chosen:
                    break
            if chosen is None:
                continue
            waybill.locked_warehouse = chosen

            # 2) 供给按 (产出小时, 渠道ID, 子仓ID) 先到先给
            guard = 0
            while not waybill.is_full() and ledger.available_remaining > 0 and guard < 4096:
                guard += 1
                heads: List[SupplyCell] = []
                for level in range(int(waybill.level) + 1):
                    cell = cursor.head(chosen, level)
                    if cell is None:
                        continue
                    if cell.available_at is not None \
                            and waybill.latest_pickup is not None \
                            and cell.available_at > waybill.latest_pickup:
                        continue
                    heads.append(cell)
                if not heads:
                    break
                cell = min(heads, key=global_cell_key)
                amount = min(cell.remaining_volume, waybill.remaining())
                ok, cid = check_all(waybill, cell, amount)
                if not ok:
                    diagnostics.construction_conflicts[cid] = (
                        diagnostics.construction_conflicts.get(cid, 0) + 1
                    )
                    break
                actual = ledger.take(cell.cell_key(), amount)
                if actual <= 0:
                    break
                waybill.records.append(AllocationRecord(
                    bill_no=waybill.bill_no,
                    channel_id=cell.channel_id,
                    warehouse_id=cell.warehouse_id,
                    hour_index=cell.hour_index,
                    hour_label=self.axis.hour_label(cell.hour_index),
                    pickup_time_str="",
                    volume=actual,
                    cell_key=cell.cell_key(),
                    in_eval_range=self.axis.is_in_eval_range(cell.hour_index),
                ))
            self._finalize(waybill, ledger)

        all_records: List[AllocationRecord] = []
        for bill_no in sorted(state.keys()):
            all_records.extend(state[bill_no].records)
        merged = merge_records(all_records)
        merged.sort(key=lambda r: r.sort_key())

        diagnostics.steps = 0  # 离线单趟，不是 480 步滚动
        diagnostics.released_cells = ledger.nonzero_cell_count()
        diagnostics.allocated_volume = sum(r.volume for r in merged)
        diagnostics.unallocated_volume = ledger.remaining_all()
        diagnostics.unallocated_cells = ledger.unallocated_snapshot()
        diagnostics.locked_waybills = sum(1 for w in state.values() if w.locked)
        ok, violations = ledger.conservation_check()
        if not ok:
            diagnostics.construction_conflicts["C1"] += violations
        return Solution(
            scenario_name="FCFS_BASELINE",
            records=merged,
            waybill_state=state,
            diagnostics=diagnostics,
        )


def fcfs_baseline(cfg_obj: Config, dataset: Dataset) -> Solution:
    """模块级入口：先到先得贪心基准。"""
    return FcfsBaseline(cfg_obj, dataset).run()


__all__ = ["FcfsBaseline", "fcfs_baseline"]
