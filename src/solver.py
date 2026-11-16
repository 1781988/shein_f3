"""滚动时域主求解：逐 τ 释放 / 可见 / 重排改配 / 到期锁定 / 增量补货。

性能设计（对应设计 1.5、4.2 与性能红线）：
- 供给格按可配时刻分 480 个桶，每步只释放本小时的非零格；
- 按 (子仓, 等级) 分桶索引 + 桶顶游标，取货时从桶顶取，不做每步扫 65512 格；
- 仅「可见提单集新增 / 首步 / 末步」等事件步触发全量重排改配（逆序退回 +
  清空子仓锁定 + 按场景优先级重新分配），其余 457 步只给未满提单补本步新释放的货；
- 退回用整数精确恢复，重排后把桶顶游标整体归零；
- 到期只做定稿锁定，不触发全量重排（锁定不释放货源，重排无增益），
  这是为了守住「不做每步全量重算」的性能红线而取的最小口径，已写入运行说明；
- 全 480 小时非零格在结束时都有明确去向：配出或明确记为未配。
"""
from __future__ import annotations

import copy
import heapq
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import config as cfg
from .candidates import CandidateGenerator, merge_records
from .config import Config, Scenario
from .constraints import CONSTRAINT_IDS, check_all, check_time_window
from .domain import (
    AllocationRecord,
    Dataset,
    DiagnosticsRaw,
    Solution,
    SupplyCell,
    Waybill,
)
from .ledger import SupplyLedger
from .precision import format_time
from .scoring import rank_key, waybill_priority_key
from .time_axis import TimeAxis

MAX_LEVEL: int = cfg.LEVEL_LITHIUM
PER_BUCKET_CANDIDATES: int = 8


class WaybillIndex:
    """未满可见提单的优先队列索引，按 (等级, 子仓锁定) 分桶。

    只存 (优先级键, 提单号) 的堆，惰性丢弃已满/已锁定/换仓的陈旧条目，
    避免每步重排 2652 张提单。全部比较键都是可比较的确定值。
    """

    def __init__(self, scenario: Scenario, cost_rates: Any) -> None:
        self.scenario = scenario
        self.cost_rates = cost_rates
        self.state: Dict[str, Waybill] = {}
        self.keys: Dict[str, Tuple[Any, ...]] = {}
        self.unlocked: List[List[Tuple[Tuple[Any, ...], str]]] = [
            [] for _ in range(MAX_LEVEL + 1)
        ]
        self.locked: Dict[Tuple[str, int], List[Tuple[Tuple[Any, ...], str]]] = {}

    def rebuild(self, waybills: Sequence[Waybill]) -> None:
        """重排步后从可见提单重建索引。"""
        self.state = {}
        self.keys = {}
        self.unlocked = [[] for _ in range(MAX_LEVEL + 1)]
        self.locked = {}
        for waybill in waybills:
            self.add(waybill)

    def add(self, waybill: Waybill) -> None:
        """登记一张提单（未锁定子仓时进 unlocked 堆）。"""
        key = waybill_priority_key(waybill, self.scenario, self.cost_rates)
        self.state[waybill.bill_no] = waybill
        self.keys[waybill.bill_no] = key
        level = int(waybill.level)
        if waybill.locked_warehouse:
            bucket = (waybill.locked_warehouse, level)
            heapq.heappush(
                self.locked.setdefault(bucket, []), (key, waybill.bill_no)
            )
        else:
            heapq.heappush(self.unlocked[level], (key, waybill.bill_no))

    def mark_locked(self, waybill: Waybill) -> None:
        """提单首次取货锁定子仓后，补登到 locked 堆。"""
        key = self.keys.get(waybill.bill_no)
        if key is None:
            self.add(waybill)
            return
        bucket = (waybill.locked_warehouse, int(waybill.level))
        heapq.heappush(self.locked.setdefault(bucket, []), (key, waybill.bill_no))

    def _valid(self, bill_no: str, cell: SupplyCell) -> bool:
        waybill = self.state.get(bill_no)
        if waybill is None or waybill.locked or waybill.is_full():
            return False
        if int(waybill.level) < int(cell.level):
            return False
        lock = waybill.locked_warehouse
        if lock and lock != cell.warehouse_id:
            return False
        return check_time_window(waybill, cell)

    def best_for(self, cell: SupplyCell) -> Optional[Waybill]:
        """返回与该格硬约束兼容、且场景优先级最高的未满提单。"""
        candidates: List[Tuple[Tuple[Any, ...], str]] = []
        for level in range(int(cell.level), MAX_LEVEL + 1):
            bucket_unlocked = self.unlocked[level]
            while bucket_unlocked and not self._valid(bucket_unlocked[0][1], cell):
                heapq.heappop(bucket_unlocked)
            if bucket_unlocked:
                candidates.append(bucket_unlocked[0])

            bucket_locked = self.locked.get((cell.warehouse_id, level))
            if bucket_locked:
                while bucket_locked and not self._valid(bucket_locked[0][1], cell):
                    heapq.heappop(bucket_locked)
                if bucket_locked:
                    candidates.append(bucket_locked[0])
        if not candidates:
            return None
        best_key, best_no = min(candidates, key=lambda item: item[0])
        return self.state.get(best_no)


class RollingSolver:
    """480 步滚动求解器；每个场景构造一个独立实例，互不共享状态。"""

    def __init__(self, cfg_obj: Config, dataset: Dataset) -> None:
        self.cfg = cfg_obj
        self.dataset = dataset
        self.axis: TimeAxis = dataset.axis if dataset.axis is not None \
            else TimeAxis.from_config(cfg_obj)

    # ------------------------------------------------------------------
    # 准备
    # ------------------------------------------------------------------
    def _clone_waybills(self) -> Dict[str, Waybill]:
        """复制提单状态，避免污染 Dataset（一个 Dataset 要跑多个场景）。"""
        state: Dict[str, Waybill] = {}
        for source in self.dataset.waybills:
            clone = copy.copy(source)
            clone.records = []
            clone.locked_warehouse = ""
            clone.locked = False
            clone.pickup_time = None
            clone.visible_from = None
            state[clone.bill_no] = clone
        return state

    def _max_earliest(self, state: Dict[str, Waybill]) -> datetime:
        """全部提单最早提货时间的最大值，用于末步扩展可见窗口上界。"""
        values = [w.earliest_pickup for w in state.values()
                  if w.earliest_pickup is not None]
        return max(values) if values else self.axis.start

    # ------------------------------------------------------------------
    # 可见集与到期
    # ------------------------------------------------------------------
    def _visible(
        self, state: Dict[str, Waybill], window_end: datetime
    ) -> List[Waybill]:
        """可见性按「已获知」累积：最早提货时间不晚于窗口上界且未定稿。

        轴外时段（早于 10-01 或晚于 10-20）的提单正常纳入可见性判断，不截断。
        """
        return [
            waybill for waybill in state.values()
            if not waybill.locked
            and waybill.earliest_pickup is not None
            and waybill.earliest_pickup <= window_end
        ]

    def _finalize(
        self, waybill: Waybill, ledger: SupplyLedger, now: datetime
    ) -> None:
        """按 B12 构造提货时刻并锁定记录，锁定后不再改动。"""
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

    # ------------------------------------------------------------------
    # 记录写入
    # ------------------------------------------------------------------
    def _write_record(
        self,
        waybill: Waybill,
        cell: SupplyCell,
        amount: int,
        index: WaybillIndex,
    ) -> None:
        """写入一条配货记录，并在首次取货时锁定子仓。"""
        record = AllocationRecord(
            bill_no=waybill.bill_no,
            channel_id=cell.channel_id,
            warehouse_id=cell.warehouse_id,
            hour_index=cell.hour_index,
            hour_label=self.axis.hour_label(cell.hour_index),
            pickup_time_str="",
            volume=amount,
            cell_key=cell.cell_key(),
            in_eval_range=self.axis.is_in_eval_range(cell.hour_index),
        )
        waybill.records.append(record)
        if not waybill.locked_warehouse:
            waybill.locked_warehouse = cell.warehouse_id
            index.mark_locked(waybill)

    def _conflict(
        self, diagnostics: DiagnosticsRaw, cid: str,
        waybill: Waybill, cell: SupplyCell, amount: int,
    ) -> None:
        """记录一次构造期硬约束冲突（正常应为 0）。"""
        diagnostics.construction_conflicts[cid] = (
            diagnostics.construction_conflicts.get(cid, 0) + 1
        )
        if len(diagnostics.construction_samples) < 20:
            diagnostics.construction_samples.append(
                f"{cid} 提单={waybill.bill_no} 格={cell.cell_key()} 量={amount}"
            )

    # ------------------------------------------------------------------
    # 增量步：只补本步新释放的货
    # ------------------------------------------------------------------
    def _incremental(
        self,
        cell_keys: Sequence[str],
        ledger: SupplyLedger,
        index: WaybillIndex,
        diagnostics: DiagnosticsRaw,
        now: datetime,
    ) -> None:
        """给未满可见提单补本步新释放的货，不做全量重排。"""
        for key in cell_keys:
            cell = ledger.cells[key]
            guard = 0
            while cell.remaining_volume > 0 and guard < 64:
                guard += 1
                waybill = index.best_for(cell)
                if waybill is None:
                    break
                amount = min(cell.remaining_volume, waybill.remaining())
                if amount <= 0:
                    break
                ok, cid = check_all(waybill, cell, amount)
                if not ok:
                    self._conflict(diagnostics, cid, waybill, cell, amount)
                    break
                actual = ledger.take(key, amount)
                if actual <= 0:
                    break
                self._write_record(waybill, cell, actual, index)
                diagnostics.allocated_volume += actual

    # ------------------------------------------------------------------
    # 重排步：全量退回 + 按场景优先级重新分配
    # ------------------------------------------------------------------
    def _fill_waybill(
        self,
        waybill: Waybill,
        generator: CandidateGenerator,
        ledger: SupplyLedger,
        scenario: Scenario,
        index: WaybillIndex,
        diagnostics: DiagnosticsRaw,
    ) -> None:
        """按轮次「候选生成 → 硬约束过滤 → 字典序打分 → 取货」，不每次重扫全池。"""
        guard = 0
        while not waybill.is_full() and ledger.available_remaining > 0 and guard < 512:
            guard += 1
            candidates = generator.generate(waybill, waybill.locked_warehouse)
            if not candidates:
                break
            candidates.sort(key=lambda c: rank_key(c, waybill, scenario))
            progressed = False
            for candidate in candidates:
                if waybill.is_full():
                    break
                cell = candidate.cell
                # 未锁定子仓时候选来自全部子仓；一旦锁定，其余子仓的候选
                # 直接跳过（不是硬约束冲突，只是本轮不再可配）。
                if waybill.locked_warehouse and cell.warehouse_id != waybill.locked_warehouse:
                    continue
                amount = min(candidate.amount, waybill.remaining(), cell.remaining_volume)
                if amount <= 0:
                    continue
                ok, cid = check_all(waybill, cell, amount)
                if not ok:
                    self._conflict(diagnostics, cid, waybill, cell, amount)
                    continue
                actual = ledger.take(cell.cell_key(), amount)
                if actual <= 0:
                    continue
                self._write_record(waybill, cell, actual, index)
                diagnostics.allocated_volume += actual
                progressed = True
            if not progressed:
                break

    def _reassign(
        self,
        state: Dict[str, Waybill],
        generator: CandidateGenerator,
        ledger: SupplyLedger,
        visible: Sequence[Waybill],
        scenario: Scenario,
        index: WaybillIndex,
        diagnostics: DiagnosticsRaw,
    ) -> None:
        """逆序退回所有未锁定记录，清空子仓锁定，再按优先级重新分配。"""
        refunded = 0
        for waybill in reversed(sorted(state.values(), key=lambda w: w.bill_no)):
            if waybill.locked:
                continue
            for record in reversed(waybill.records):
                ledger.refund(record)
                refunded += 1
            waybill.records = []
            waybill.locked_warehouse = ""
        diagnostics.refund_count += refunded
        diagnostics.allocated_volume = 0
        generator.reset_cursors()
        index.rebuild(visible)

        ordered = sorted(
            visible,
            key=lambda w: waybill_priority_key(w, scenario, self.dataset.cost_rates),
        )
        for waybill in ordered:
            if ledger.available_remaining <= 0:
                break
            if waybill.locked or waybill.is_full():
                continue
            self._fill_waybill(
                waybill, generator, ledger, scenario, index, diagnostics
            )
        diagnostics.reassign_steps += 1

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def solve(self, scenario: Scenario) -> Solution:
        """跑满 480 步，返回一个场景的完整 Solution。"""
        ledger = SupplyLedger(self.dataset, self.cfg)
        generator = CandidateGenerator(
            ledger, self.cfg, self.dataset, per_bucket=PER_BUCKET_CANDIDATES
        )
        state = self._clone_waybills()
        max_earliest = self._max_earliest(state)
        index = WaybillIndex(scenario, self.dataset.cost_rates)
        diagnostics = DiagnosticsRaw(
            construction_conflicts={cid: 0 for cid in CONSTRAINT_IDS},
            total_nonzero_cells=ledger.nonzero_cell_count(),
        )

        previous_visible: Set[str] = set()
        for hour_index in range(cfg.HOURS):
            generator.set_step(hour_index)
            new_keys = ledger.release(hour_index)
            now = self.axis.decision_time(hour_index)

            # 1) 到期定稿锁定（最晚提货时间已过），锁定后不再改动
            for waybill in state.values():
                if (not waybill.locked
                        and waybill.latest_pickup is not None
                        and waybill.latest_pickup < now):
                    self._finalize(waybill, ledger, now)

            # 2) 可见窗口；末步把上界延伸到全部提单的最早提货时间，不截断轴外提单
            upper_bound = max_earliest if hour_index == cfg.HOURS - 1 else None
            _, window_end = self.axis.visible_window_for_step(
                hour_index, upper_bound=upper_bound
            )
            visible = self._visible(state, window_end)
            visible_names = {w.bill_no for w in visible}
            diagnostics.visible_peak = max(diagnostics.visible_peak, len(visible))
            new_entrants = visible_names - previous_visible

            # 3) 事件步 = 首步 / 末步 / 可见集新增；其余增量补货
            is_event = (
                hour_index == 0
                or hour_index == cfg.HOURS - 1
                or bool(new_entrants)
            )
            if is_event and self.cfg.enable_reassign:
                self._reassign(
                    state, generator, ledger, visible, scenario, index, diagnostics
                )
                diagnostics.event_steps += 1
            else:
                self._incremental(new_keys, ledger, index, diagnostics, now)
                diagnostics.incremental_steps += 1
            previous_visible = visible_names
            diagnostics.steps += 1

        # 4) 时间轴结束后，把全部未锁定提单定稿（构造提货时刻）
        now = self.axis.decision_time(cfg.HOURS - 1)
        for waybill in state.values():
            if not waybill.locked:
                self._finalize(waybill, ledger, now)
        diagnostics.locked_waybills = sum(1 for w in state.values() if w.locked)

        # 5) 汇总记录：先合并同格，再按 (提单号, 产出小时, 渠道, 子仓) 稳定排序
        all_records: List[AllocationRecord] = []
        for bill_no in sorted(state.keys()):
            all_records.extend(state[bill_no].records)
        merged = merge_records(all_records)
        merged.sort(key=lambda r: r.sort_key())

        # 6) 全 480 小时非零格去向：配出方数 + 未配方数 = 原始货源方数
        diagnostics.released_cells = sum(
            len(keys) for hour, keys in enumerate(ledger.by_hour)
            if ledger.released_hours[hour]
        )
        diagnostics.allocated_volume = sum(r.volume for r in merged)
        diagnostics.unallocated_volume = ledger.remaining_all()
        diagnostics.unallocated_cells = ledger.unallocated_snapshot()

        # 7) 逐小时去向台账：每个非零格必须「配出」或「明确记为未配」
        allocated_cells: Set[str] = set()
        for record in merged:
            allocated_cells.add(record.cell_key)
        for hour_index in range(cfg.HOURS):
            keys = ledger.by_hour[hour_index]
            if not keys:
                continue
            configured = sum(1 for k in keys if k in allocated_cells)
            leftover = sum(1 for k in keys if k not in allocated_cells)
            diagnostics.hour_disposition.append({
                "hour_index": hour_index,
                "hour_label": self.axis.hour_label(hour_index),
                "nonzero_cells": len(keys),
                "allocated_cells": configured,
                "unallocated_cells": leftover,
                "released": ledger.released_hours[hour_index],
            })
        diagnostics.disposition_covered = (
            sum(row["nonzero_cells"] for row in diagnostics.hour_disposition)
            == diagnostics.total_nonzero_cells
            and all(row["released"] for row in diagnostics.hour_disposition)
            and diagnostics.allocated_volume + diagnostics.unallocated_volume
            == ledger.total_original
        )
        ok, violations = ledger.conservation_check()
        if not ok or violations:
            diagnostics.construction_conflicts["C1"] = (
                diagnostics.construction_conflicts.get("C1", 0) + violations
            )
        return Solution(
            scenario_name=scenario.name,
            records=merged,
            waybill_state=state,
            diagnostics=diagnostics,
        )


__all__ = ["RollingSolver", "WaybillIndex"]
