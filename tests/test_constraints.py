"""五条硬约束的构造与审计一致性、供给账户 take/refund 精确恢复、反查索引。"""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config as cfg  # noqa: E402
from src.config import Config  # noqa: E402
from src.constraints import (  # noqa: E402
    C1, C2, C3, C4, C5,
    check_all,
    check_cell_cap,
    check_sensitive,
    check_single_warehouse,
    check_time_window,
    check_volume_cap,
)
from src.domain import AllocationRecord, SupplyCell, Waybill  # noqa: E402
from src.injection import build_injection_index, classify, InjectionKind  # noqa: E402
from src.ledger import SupplyLedger  # noqa: E402
from src.sensitivity import SensitivityLevel  # noqa: E402
from src.time_axis import TimeAxis  # noqa: E402
from tests.fixtures.make_fixtures import tiny_dataset  # noqa: E402


def _cell(axis: TimeAxis, hour: int = 48, level: int = cfg.LEVEL_DRY,
          warehouse: str = "W1", remaining: int = 10000) -> SupplyCell:
    """构造一个测试用供给格。"""
    return SupplyCell(
        row_index=0, hour_index=hour,
        original_volume=remaining, remaining_volume=remaining,
        available_at=axis.available_at(hour),
        channel_id="C1", channel_injection="DXB",
        warehouse_id=warehouse, level=SensitivityLevel(level),
        key=f"0:{hour}",
    )


def _waybill(axis: TimeAxis, level: int = cfg.LEVEL_LITHIUM,
             plan: int = 20000, latest_hour: int = 72) -> Waybill:
    """构造一个测试用提单。"""
    base = axis.left_at(0)
    return Waybill(
        bill_no="B1", dest_port="DXB", plan_volume=plan,
        earliest_pickup=base + timedelta(hours=48),
        required_arrival=base + timedelta(hours=latest_hour),
        latest_pickup=base + timedelta(hours=latest_hour),
        billing_type=cfg.BILLING_BY_WEIGHT, level=SensitivityLevel(level),
    )


class TestHardConstraints(unittest.TestCase):
    """五条硬约束的纯函数行为。"""

    def setUp(self) -> None:
        self.axis = TimeAxis.from_config(Config.load())

    def test_c1_cell_cap(self) -> None:
        cell = _cell(self.axis, remaining=10000)
        self.assertTrue(check_cell_cap(cell, 10000))
        self.assertFalse(check_cell_cap(cell, 10001))
        self.assertFalse(check_cell_cap(cell, 0))
        self.assertFalse(check_cell_cap(cell, -1))

    def test_c2_volume_cap(self) -> None:
        wb = _waybill(self.axis, plan=20000)
        self.assertTrue(check_volume_cap(wb, 20000))
        self.assertFalse(check_volume_cap(wb, 20001))
        wb.records.append(AllocationRecord(volume=15000))
        self.assertTrue(check_volume_cap(wb, 5000))
        self.assertFalse(check_volume_cap(wb, 5001))

    def test_c3_single_warehouse(self) -> None:
        wb = _waybill(self.axis)
        self.assertTrue(check_single_warehouse(wb, _cell(self.axis, warehouse="W1")))
        wb.locked_warehouse = "W1"
        self.assertTrue(check_single_warehouse(wb, _cell(self.axis, warehouse="W1")))
        self.assertFalse(check_single_warehouse(wb, _cell(self.axis, warehouse="W2")))

    def test_c4_sensitive(self) -> None:
        wb = _waybill(self.axis, level=cfg.LEVEL_DRY)
        self.assertTrue(check_sensitive(wb, _cell(self.axis, level=cfg.LEVEL_DRY)))
        self.assertTrue(check_sensitive(wb, _cell(self.axis, level=cfg.LEVEL_NONE)))
        self.assertFalse(check_sensitive(wb, _cell(self.axis, level=cfg.LEVEL_LITHIUM)))

    def test_c5_time_window(self) -> None:
        wb = _waybill(self.axis, latest_hour=72)
        self.assertTrue(check_time_window(wb, _cell(self.axis, hour=48)))
        # 小时 72 的可配时刻是 73:00，晚于最晚提货 72:00
        self.assertFalse(check_time_window(wb, _cell(self.axis, hour=72)))
        pickup = self.axis.left_at(60)
        self.assertTrue(check_time_window(wb, _cell(self.axis, hour=48), pickup))
        self.assertFalse(check_time_window(
            wb, _cell(self.axis, hour=48), self.axis.left_at(0)
        ))

    def test_check_all_conflict_ids(self) -> None:
        wb = _waybill(self.axis)
        cell = _cell(self.axis, remaining=10000)
        ok, cid = check_all(wb, cell, 10000)
        self.assertTrue(ok)
        self.assertEqual(cid, "")
        self.assertEqual(check_all(wb, cell, 10001)[1], C1)
        self.assertEqual(check_all(_waybill(self.axis, plan=5000), cell, 10000)[1], C2)
        wb.locked_warehouse = "W9"
        self.assertEqual(check_all(wb, cell, 10000)[1], C3)
        wb.locked_warehouse = ""
        self.assertEqual(check_all(_waybill(self.axis, level=cfg.LEVEL_NONE),
                                    cell, 10000)[1], C4)
        self.assertEqual(check_all(_waybill(self.axis, latest_hour=40),
                                    cell, 10000)[1], C5)


class TestLedger(unittest.TestCase):
    """take/refund 往返余额精确恢复、480 桶与 (子仓, 等级) 索引。"""

    def setUp(self) -> None:
        self.dataset = tiny_dataset()
        self.cfg = Config.load()
        self.ledger = SupplyLedger(self.dataset, self.cfg)

    def test_take_refund_roundtrip(self) -> None:
        keys = self.ledger.release(48)
        self.assertTrue(keys)
        key = keys[0]
        original = self.ledger.cells[key].original_volume
        taken = self.ledger.take(key, 999999999)
        self.assertEqual(taken, original)
        self.assertEqual(self.ledger.remaining(key), 0)
        record = AllocationRecord(cell_key=key, volume=taken)
        self.assertEqual(self.ledger.refund(record), taken)
        self.assertEqual(self.ledger.remaining(key), original)
        self.assertEqual(self.ledger.cells[key].remaining_volume, original)

    def test_partial_take_refund(self) -> None:
        keys = self.ledger.release(48)
        key = max(keys, key=lambda k: self.ledger.cells[k].original_volume)
        original = self.ledger.cells[key].original_volume
        first = self.ledger.take(key, 3333)
        second = self.ledger.take(key, 2222)
        self.assertEqual(first + second, 5555)
        for amount in (second, first):
            self.ledger.refund(AllocationRecord(cell_key=key, volume=amount))
        self.assertEqual(self.ledger.remaining(key), original)
        self.assertTrue(self.ledger.conservation_check()[0])

    def test_refund_never_exceeds_original(self) -> None:
        keys = self.ledger.release(48)
        key = keys[0]
        cell = self.ledger.cells[key]
        self.ledger.refund(AllocationRecord(cell_key=key, volume=cell.original_volume * 5))
        self.assertEqual(cell.remaining_volume, cell.original_volume)

    def test_release_is_idempotent(self) -> None:
        first = self.ledger.release(48)
        self.assertTrue(first)
        self.assertEqual(self.ledger.release(48), [])

    def test_candidates_bucketed(self) -> None:
        self.ledger.release_all()
        axis = self.dataset.axis
        before = axis.available_at(60)
        cells = self.ledger.candidates("W1", cfg.LEVEL_LITHIUM, before)
        self.assertTrue(cells)
        for cell in cells:
            self.assertEqual(cell.warehouse_id, "W1")
            self.assertLessEqual(int(cell.level), cfg.LEVEL_LITHIUM)
            self.assertLessEqual(cell.available_at, before)
            self.assertGreater(cell.remaining_volume, 0)
        # 低等级提单不应看到锂电货
        general = self.ledger.candidates("W1", cfg.LEVEL_SENSITIVE_GENERAL, before)
        self.assertTrue(all(int(c.level) <= cfg.LEVEL_SENSITIVE_GENERAL for c in general))

    def test_unreleased_cell_has_no_remaining(self) -> None:
        key = next(iter(self.ledger.cells))
        self.assertEqual(self.ledger.remaining(key), 0)
        self.assertEqual(self.ledger.take(key, 100), 0)

    def test_conservation(self) -> None:
        self.ledger.release_all()
        self.assertEqual(self.ledger.remaining_all(), self.ledger.total_original)
        self.assertTrue(self.ledger.conservation_check()[0])


class TestReverseIndex(unittest.TestCase):
    """敏感品 / 注入点反查索引用单测锁住。"""

    def test_index_keys(self) -> None:
        dataset = tiny_dataset()
        self.assertEqual(
            dataset.sensitive_by_cw[("C1", "W1")], SensitivityLevel.LITHIUM
        )
        self.assertEqual(
            dataset.sensitive_by_cw[("C3", "W1")], SensitivityLevel.NONE
        )
        self.assertEqual(dataset.injection_by_cw[("C3", "W1")], "SHJ")

    def test_injection_classify(self) -> None:
        self.assertEqual(classify("DXB", "DXB"), InjectionKind.PRIMARY)
        self.assertEqual(classify("DXB", "DWC"), InjectionKind.ALTERNATIVE)
        self.assertEqual(classify("SHJ", "DXB"), InjectionKind.SHJ_ALT)
        self.assertEqual(classify("SHJ", "SHJ"), InjectionKind.SHJ_ALT)

    def test_build_injection_index(self) -> None:
        from types import SimpleNamespace

        rows = [
            SimpleNamespace(channel_id="C1", warehouse_id="W1", channel_injection="DXB"),
            SimpleNamespace(channel_id="C1", warehouse_id="W2", channel_injection="DWC"),
        ]
        index = build_injection_index(rows)
        self.assertEqual(index, {("C1", "W1"): "DXB", ("C1", "W2"): "DWC"})


if __name__ == "__main__":
    unittest.main()
