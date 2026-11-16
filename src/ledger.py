"""SupplyLedger 供给账户：释放、取用、退回、封顶，全部整数精确运算。

性能约定（对应设计 1.5 / 4.2）：
- 按可配时刻分 480 个桶，``release(t)`` 只处理第 t 小时的非零格；
- 按 (子仓ID, 敏感品等级) 分桶索引，桶内按
  (渠道注入点, 产出小时, 渠道ID, 行号) 稳定排序，取货时从桶顶取；
- 不做每步全表扫描；退回用整数精确恢复，并把该格重新放回桶顶。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Sequence, Tuple

from . import config as cfg
from .config import Config
from .domain import AllocationRecord, Dataset, SupplyCell
from .precision import format_date
from .time_axis import TimeAxis


def cell_sort_key(cell: SupplyCell) -> Tuple[str, int, str, int, int]:
    """桶内稳定排序键（不含提单相关的注入点匹配，那层由打分处理）。"""
    return (
        cell.channel_injection,
        cell.hour_index,
        cell.channel_id,
        cell.row_index,
        int(cell.level),
    )


def global_cell_key(cell: SupplyCell) -> Tuple[int, str, int, str, str, int]:
    """跨桶的静态全局顺序：产出小时优先，其次子仓/等级/注入点/渠道。"""
    return (
        cell.hour_index,
        cell.warehouse_id,
        int(cell.level),
        cell.channel_injection,
        cell.channel_id,
        cell.row_index,
    )


class SupplyLedger:
    """整数体积供给账户，持有全部非零格。"""

    def __init__(self, dataset: Dataset, cfg_obj: Config) -> None:
        self.cfg = cfg_obj
        self.dataset = dataset
        self.axis: TimeAxis = dataset.axis if dataset.axis is not None else TimeAxis.from_config(cfg_obj)
        self.cells: Dict[str, SupplyCell] = {}
        self.by_hour: List[List[str]] = [[] for _ in range(cfg.HOURS)]
        self.by_wh_level: Dict[Tuple[str, int], List[str]] = {}
        self.released_hours: List[bool] = [False] * cfg.HOURS
        self.total_original: int = 0
        self.available_remaining: int = 0
        self.current_hour: int = 0
        self._build_cells()

    # -- 构建 --------------------------------------------------------------
    def _build_cells(self) -> None:
        """把 301 行 × 480 小时展开成非零格，并建立分桶索引。"""
        density_index = self.dataset.density_by_cwd
        for row in self.dataset.supply_rows:
            for hour_index, volume in enumerate(row.hourly_volumes):
                if volume <= 0:
                    continue
                left = self.axis.left_at(hour_index)
                pack_date = format_date(left.date())
                density_raw = density_index.get(
                    (row.channel_id, row.warehouse_id, pack_date)
                )
                density_missing = density_raw is None
                density = 0.0 if density_missing else float(density_raw)
                cell = SupplyCell(
                    row_index=row.row_index,
                    hour_index=hour_index,
                    original_volume=volume,
                    remaining_volume=volume,
                    available_at=self.axis.available_at(hour_index),
                    channel_id=row.channel_id,
                    channel_injection=row.channel_injection,
                    warehouse_id=row.warehouse_id,
                    level=row.level,
                    pack_date=pack_date,
                    density=density,
                    density_missing=density_missing,
                    key=f"{row.row_index}:{hour_index}",
                )
                self.cells[cell.key] = cell
                self.by_hour[hour_index].append(cell.key)
                self.total_original += volume
        for hour_keys in self.by_hour:
            hour_keys.sort(key=lambda k: global_cell_key(self.cells[k]))
        for key, cell in self.cells.items():
            bucket = (cell.warehouse_id, int(cell.level))
            self.by_wh_level.setdefault(bucket, []).append(key)
        # 桶内按产出小时升序（global_cell_key 首字段是 hour_index），
        # 保证「已释放格 = 桶内前缀」，候选生成器可用二分定位前缀长度。
        for keys in self.by_wh_level.values():
            keys.sort(key=lambda k: global_cell_key(self.cells[k]))

    # -- 释放 --------------------------------------------------------------
    def release(self, hour_idx: int) -> List[str]:
        """释放第 hour_idx 小时产出的非零格，返回本次新释放的格键（稳定顺序）。

        重复释放同一小时返回空列表，不重复扣减、不破坏守恒。
        """
        if hour_idx < 0 or hour_idx >= cfg.HOURS:
            raise ValueError(f"释放小时越界：{hour_idx}，合法 0..{cfg.HOURS - 1}")
        if self.released_hours[hour_idx]:
            return []
        self.released_hours[hour_idx] = True
        keys = list(self.by_hour[hour_idx])
        self.available_remaining += sum(self.cells[k].remaining_volume for k in keys)
        self.current_hour = hour_idx
        return keys

    def release_all(self) -> List[str]:
        """一次性释放全部 480 小时（贪心基准用），返回全部格键。"""
        keys: List[str] = []
        for hour in range(cfg.HOURS):
            keys.extend(self.release(hour))
        return keys

    def is_released(self, cell_key: str) -> bool:
        """该格是否已释放到可用池。"""
        cell = self.cells.get(cell_key)
        if cell is None:
            return False
        return self.released_hours[cell.hour_index]

    # -- 取用 / 退回 -------------------------------------------------------
    def take(self, cell_key: str, amount: int) -> int:
        """从某格取不超过 amount 的整数体积，返回实际取到的整数。

        不校验硬约束（由 constraints.check_all 在调用前完成），只保证
        已释放、剩余量封顶、整数精确扣减。
        """
        cell = self.cells.get(cell_key)
        if cell is None or not self.released_hours[cell.hour_index]:
            return 0
        if amount <= 0 or cell.remaining_volume <= 0:
            return 0
        actual = amount if amount <= cell.remaining_volume else cell.remaining_volume
        cell.remaining_volume -= actual
        self.available_remaining -= actual
        return actual

    def refund(self, record: AllocationRecord) -> int:
        """按配货记录精确退回整数体积，返回实际退回量。

        退回量不超过该格「原始体积 - 当前剩余」，保证守恒与封顶。
        """
        cell = self.cells.get(record.cell_key)
        if cell is None:
            return 0
        room = cell.original_volume - cell.remaining_volume
        amount = record.volume if record.volume <= room else room
        cell.remaining_volume += amount
        self.available_remaining += amount
        return amount

    def remaining(self, cell_key: str) -> int:
        """某格剩余整数体积；未释放的格返回 0（不可取）。"""
        cell = self.cells.get(cell_key)
        if cell is None or not self.released_hours[cell.hour_index]:
            return 0
        return cell.remaining_volume

    def remaining_all(self) -> int:
        """全部已释放格的剩余整数体积之和（O(1) 计数维护）。"""
        return self.available_remaining

    # -- 候选 ---------------------------------------------------------------
    def candidates(
        self,
        warehouse: str,
        level: int,
        before: datetime,
        limit: int = 256,
    ) -> List[SupplyCell]:
        """返回某子仓、等级不高于 level、可配时刻不晚于 before 的可用格。

        只扫描 (子仓, 等级) 索引桶，桶顶先取；不做全表扫描。
        ``limit`` 控制单次返回的格数上限，避免构造过多候选对象。
        """
        result: List[SupplyCell] = []
        for lvl in range(int(level) + 1):
            keys = self.by_wh_level.get((str(warehouse), lvl))
            if not keys:
                continue
            for key in keys:
                cell = self.cells[key]
                if cell.remaining_volume <= 0:
                    continue
                if not self.released_hours[cell.hour_index]:
                    continue
                if cell.available_at is None or cell.available_at > before:
                    continue
                result.append(cell)
                if len(result) >= limit:
                    return result
        return result

    def candidates_for_warehouses(
        self,
        warehouses: Sequence[str],
        level: int,
        before: datetime,
        limit: int = 256,
    ) -> List[SupplyCell]:
        """多子仓版本的候选枚举（求解器在未锁定子仓时使用）。"""
        result: List[SupplyCell] = []
        for wh in warehouses:
            result.extend(self.candidates(wh, level, before, limit=limit))
            if len(result) >= limit:
                break
        return result[:limit] if limit > 0 else result

    def released_cell_keys(self, only_remaining: bool = True) -> List[str]:
        """返回全部已释放格键，按全局静态顺序（产出小时优先）稳定排序。"""
        keys: List[str] = []
        for hour in range(cfg.HOURS):
            if self.released_hours[hour]:
                keys.extend(self.by_hour[hour])
        if only_remaining:
            keys = [k for k in keys if self.cells[k].remaining_volume > 0]
        keys.sort(key=lambda k: global_cell_key(self.cells[k]))
        return keys

    def unallocated_snapshot(self) -> List[Dict[str, Any]]:
        """返回全部仍有剩余的已释放格明细，用于「非零格明确记为未配」。"""
        snapshot: List[Dict[str, Any]] = []
        for key in self.released_cell_keys(only_remaining=True):
            cell = self.cells[key]
            snapshot.append({
                "cell_key": key,
                "hour_index": cell.hour_index,
                "hour_label": self.axis.hour_label(cell.hour_index),
                "channel_id": cell.channel_id,
                "warehouse_id": cell.warehouse_id,
                "level": int(cell.level),
                "remaining_volume": cell.remaining_volume,
                "in_eval_range": self.axis.is_in_eval_range(cell.hour_index),
            })
        return snapshot

    def nonzero_cell_count(self) -> int:
        """非零格总数。"""
        return len(self.cells)

    def conservation_check(self) -> Tuple[bool, int]:
        """整数守恒自检：返回 (是否通过, 违反该格原始体积的格数)。"""
        violations = sum(
            1 for cell in self.cells.values()
            if cell.remaining_volume < 0 or cell.remaining_volume > cell.original_volume
        )
        return (violations == 0, violations)


__all__ = ["SupplyLedger", "cell_sort_key", "global_cell_key"]
